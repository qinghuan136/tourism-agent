"""验证千问文本 Reranker Client 的外部接口契约。"""

import asyncio

import httpx
import pytest


def test_resolve_qwen_rerank_url_uses_explicit_override() -> None:
    """显式配置专用地址时，不应根据 Chat Base URL 再次推导。"""
    from tourism_agent.providers.reranker import resolve_qwen_rerank_url

    assert (
        resolve_qwen_rerank_url(
            base_url="https://model.example/compatible-mode/v1",
            rerank_url="https://rerank.example/custom",
        )
        == "https://rerank.example/custom"
    )


def test_resolve_qwen_rerank_url_reuses_existing_provider_host() -> None:
    """默认应复用现有模型服务主机，并切换到千问专用 Rerank 路径。"""
    from tourism_agent.providers.reranker import resolve_qwen_rerank_url

    assert resolve_qwen_rerank_url(
        base_url="https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        rerank_url=None,
    ) == (
        "https://workspace.cn-beijing.maas.aliyuncs.com/"
        "api/v1/services/rerank/text-rerank/text-rerank"
    )


def test_resolve_qwen_rerank_url_requires_override_for_unknown_provider() -> None:
    """未知兼容服务不应被误拼成 DashScope 专用接口。"""
    from tourism_agent.providers.reranker import resolve_qwen_rerank_url

    with pytest.raises(ValueError, match="TOURISM_AGENT_RERANK_URL"):
        resolve_qwen_rerank_url(
            base_url="https://api.openai.com/v1",
            rerank_url=None,
        )


def test_create_qwen_reranker_reuses_existing_api_key(monkeypatch) -> None:
    """Reranker 工厂必须复用现有模型 API Key，而不增加第二套密钥。"""
    from tourism_agent.providers import reranker as reranker_module
    from tourism_agent.providers.model import ModelSettings

    captured: dict[str, object] = {}
    sentinel = object()

    def fake_reranker(**kwargs: object) -> object:
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(reranker_module, "QwenTextReranker", fake_reranker)
    settings = ModelSettings(
        api_key="shared-api-key",
        base_url="https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        rerank_url=None,
        _env_file=None,
    )

    result = reranker_module.create_qwen_reranker(settings)

    assert result is sentinel
    assert captured == {
        "api_key": "shared-api-key",
        "api_url": (
            "https://workspace.cn-beijing.maas.aliyuncs.com/"
            "api/v1/services/rerank/text-rerank/text-rerank"
        ),
    }


def test_qwen_reranker_uses_official_payload_and_restores_input_order() -> None:
    """供应商按分数排序返回时，Client 仍应恢复为输入文档顺序的分数。"""
    from tourism_agent.providers.reranker import QwenTextReranker

    captured: dict[str, object] = {}

    def handle_request(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["Authorization"]
        captured["payload"] = __import__("json").loads(request.content)
        return httpx.Response(
            200,
            json={
                "output": {
                    "results": [
                        {"index": 2, "relevance_score": 0.95},
                        {"index": 0, "relevance_score": 0.9},
                        {"index": 1, "relevance_score": 0.2},
                    ]
                },
                "usage": {"total_tokens": 42},
                "request_id": "request-1",
            },
        )

    async def scenario() -> list[float]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http:
            reranker = QwenTextReranker(
                api_key="test-api-key",
                api_url="https://model.example/api/v1/services/rerank/text-rerank/text-rerank",
                http=http,
            )
            return await reranker.rerank(
                query="用户以前确认的旅行预算",
                documents=["预算5000元", "喜欢海边", "预算不超过6000元"],
            )

    scores = asyncio.run(scenario())

    assert scores == [0.9, 0.2, 0.95]
    assert captured == {
        "url": "https://model.example/api/v1/services/rerank/text-rerank/text-rerank",
        "authorization": "Bearer test-api-key",
        "payload": {
            "model": "qwen3.7-text-rerank",
            "input": {
                "query": "用户以前确认的旅行预算",
                "documents": ["预算5000元", "喜欢海边", "预算不超过6000元"],
            },
            "parameters": {
                "top_n": 3,
                "instruct": (
                    "Given a memory retrieval query, retrieve conversation memories "
                    "that are relevant and useful for answering the query."
                ),
            },
        },
    }


def test_qwen_reranker_retries_transient_network_failure() -> None:
    """短暂连接失败不应立即终止一次只读 Rerank 请求。"""
    from tourism_agent.providers.reranker import QwenTextReranker

    attempts = 0

    def handle_request(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("模拟连接抖动", request=request)
        return httpx.Response(
            200,
            json={
                "output": {
                    "results": [{"index": 0, "relevance_score": 0.8}]
                }
            },
        )

    async def scenario() -> list[float]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http:
            reranker = QwenTextReranker(
                api_key="test-api-key",
                api_url="https://model.example/rerank",
                http=http,
                retry_delays=(0.0,),
            )
            return await reranker.rerank(query="广州预算", documents=["预算5000元"])

    assert asyncio.run(scenario()) == [0.8]
    assert attempts == 2


def test_qwen_reranker_rejects_invalid_result_index() -> None:
    """供应商返回负索引时，不得把分数静默关联到错误文档。"""
    from tourism_agent.providers.reranker import QwenTextReranker

    def handle_request(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "output": {
                    "results": [
                        {"index": 0, "relevance_score": 0.9},
                        {"index": -1, "relevance_score": 0.8},
                    ]
                }
            },
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http:
            reranker = QwenTextReranker(
                api_key="test-api-key",
                api_url="https://model.example/rerank",
                http=http,
            )
            await reranker.rerank(query="预算", documents=["预算5000元", "喜欢海边"])

    with pytest.raises(ValueError, match="索引"):
        asyncio.run(scenario())
