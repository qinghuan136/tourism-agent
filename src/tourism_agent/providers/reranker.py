"""封装千问文本重排序模型的 HTTP Client。"""

import asyncio
import logging
import math
from collections.abc import Sequence
from urllib.parse import urlsplit, urlunsplit

import httpx

from tourism_agent.providers.model import ModelSettings

RERANK_MODEL = "qwen3.7-text-rerank"
RERANK_INSTRUCTION = (
    "Given a memory retrieval query, retrieve conversation memories "
    "that are relevant and useful for answering the query."
)
DEFAULT_RETRY_DELAYS = (0.25, 0.75)
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
DEFAULT_MODEL_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
RERANK_PATH = "/api/v1/services/rerank/text-rerank/text-rerank"

logger = logging.getLogger(__name__)


def resolve_qwen_rerank_url(*, base_url: str | None, rerank_url: str | None) -> str:
    """优先使用显式地址，否则复用当前模型服务的主机。"""
    if rerank_url:
        return rerank_url.rstrip("/")
    parsed = urlsplit(base_url or DEFAULT_MODEL_BASE_URL)
    host = parsed.hostname or ""
    if host != "dashscope.aliyuncs.com" and not host.endswith(".maas.aliyuncs.com"):
        raise ValueError(
            "当前模型 Host 无法推导千问 Rerank 地址，请配置 TOURISM_AGENT_RERANK_URL"
        )
    return urlunsplit((parsed.scheme, parsed.netloc, RERANK_PATH, "", ""))


def create_qwen_reranker(settings: ModelSettings | None = None) -> "QwenTextReranker":
    """复用现有 API Key 创建固定模型的 Reranker Client。"""
    settings = settings or ModelSettings()
    if settings.api_key is None:
        raise ValueError("调用 Reranker 前需要配置 OPENAI_API_KEY")
    return QwenTextReranker(
        api_key=settings.api_key.get_secret_value(),
        api_url=resolve_qwen_rerank_url(
            base_url=settings.base_url,
            rerank_url=settings.rerank_url,
        ),
    )


class QwenTextReranker:
    """调用千问专用 Rerank API，并按输入文档顺序返回分数。"""

    def __init__(
        self,
        *,
        api_key: str,
        api_url: str,
        http: httpx.AsyncClient | None = None,
        retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS,
    ) -> None:
        self._api_key = api_key
        self._api_url = api_url
        self._http = http or httpx.AsyncClient(timeout=30.0)
        self._owns_http = http is None
        self._retry_delays = tuple(retry_delays)

    async def rerank(self, *, query: str, documents: Sequence[str]) -> list[float]:
        """批量评估查询与候选文档的相关性。"""
        if not documents:
            return []

        payload = {
            "model": RERANK_MODEL,
            "input": {"query": query, "documents": list(documents)},
            "parameters": {
                "top_n": len(documents),
                "instruct": RERANK_INSTRUCTION,
            },
        }
        response = await self._post_with_retry(payload)
        results = response.json()["output"]["results"]
        scores: list[float | None] = [None] * len(documents)
        for item in results:
            index = item["index"]
            score = float(item["relevance_score"])
            if (
                type(index) is not int
                or not 0 <= index < len(documents)
                or scores[index] is not None
            ):
                raise ValueError("Reranker 返回了无效或重复的文档索引")
            if not math.isfinite(score) or not 0.0 <= score <= 1.0:
                raise ValueError("Reranker 返回了无效的相关性分数")
            scores[index] = score
        if any(score is None for score in scores):
            raise ValueError("Reranker 返回结果未覆盖全部候选文档")
        return [float(score) for score in scores]

    async def aclose(self) -> None:
        """只关闭由当前 Client 创建的 HTTP 连接池。"""
        if self._owns_http:
            await self._http.aclose()

    async def _post_with_retry(self, payload: dict[str, object]) -> httpx.Response:
        """仅对瞬时网络错误和临时 HTTP 状态进行短退避重试。"""
        attempt = 0
        while True:
            try:
                response = await self._http.post(
                    self._api_url,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=payload,
                )
                response.raise_for_status()
                return response
            except Exception as error:
                if attempt >= len(self._retry_delays) or not _is_retryable(error):
                    raise
                delay = self._retry_delays[attempt]
                attempt += 1
                logger.warning(
                    "Reranker调用瞬时失败，准备重试 attempt=%d/%d error_type=%s",
                    attempt + 1,
                    len(self._retry_delays) + 1,
                    type(error).__name__,
                )
                await asyncio.sleep(delay)


def _is_retryable(error: Exception) -> bool:
    """识别适合安全重试的网络错误。"""
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in RETRYABLE_STATUS_CODES
    return isinstance(
        error,
        (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError),
    )
