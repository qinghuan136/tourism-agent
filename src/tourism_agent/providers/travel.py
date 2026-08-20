"""封装天气、地点和网页搜索供应商的调用边界。"""

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import anyio
import httpx
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from tourism_agent.infrastructure.logging_config import log_preview

logger = logging.getLogger(__name__)
NETWORK_RETRY_DELAYS = (0.5, 1.0)
RETRYABLE_HTTP_STATUS_CODES = {408, 429, 500, 502, 503, 504}


class TravelToolSettings(BaseSettings):
    """从项目根目录的 .env 或进程环境读取旅行查询配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    qweather_api_host: str = Field(validation_alias="QWEATHER_API_HOST")
    qweather_api_key: SecretStr = Field(validation_alias="QWEATHER_API_KEY")
    amap_web_service_key: SecretStr = Field(validation_alias="AMAP_WEB_SERVICE_KEY")
    tavily_api_key: SecretStr = Field(validation_alias="TAVILY_API_KEY")


class QWeatherClient:
    """通过 QWeather GeoAPI 和天气 API 查询中国大陆天气。"""

    def __init__(self, api_host: str, api_key: str, http: httpx.AsyncClient) -> None:
        self._base_url = f"https://{api_host.removeprefix('https://').rstrip('/')}"
        self._headers = {"X-QW-Api-Key": api_key}
        self._http = http

    async def get_weather(
        self,
        location: str,
        time_range: str,
        region: str = "",
    ) -> str:
        """解析地点并返回指定绝对日期范围内的逐日预报。"""
        logger.info(
            "QWeather查询开始 location=%s time_range=%s region=%s",
            log_preview(location),
            log_preview(time_range),
            log_preview(region),
        )
        started_at = time.perf_counter()
        start, end = parse_date_range(time_range)
        forecast_days = select_forecast_days(end)
        locations = await self._resolve_locations(location, region)
        resolved = locations[0]
        logger.info(
            "QWeather地点解析完成 location_id=%s name=%s adm1=%s adm2=%s",
            resolved.get("id"),
            resolved.get("name"),
            resolved.get("adm1"),
            resolved.get("adm2"),
        )
        response = await get_with_retry(
            self._http,
            f"{self._base_url}/v7/weather/{forecast_days}d",
            provider="QWeather",
            params={"location": resolved["id"], "lang": "zh"},
            headers=self._headers,
        )
        payload = response.json()
        ensure_provider_success(payload, "QWeather")

        daily = [
            item
            for item in payload.get("daily", [])
            if start <= date.fromisoformat(item["fxDate"]) <= end
        ]
        if not daily:
            raise ValueError("QWeather 没有返回目标时间段的天气数据")

        location_parts = unique_texts(
            resolved.get("name"),
            resolved.get("adm2"),
            resolved.get("adm1"),
            resolved.get("country"),
        )
        lines = [
            (
                f"QWeather 天气；查询地点：{'，'.join(location_parts)}；更新时间："
                f"{payload.get('updateTime', '未知')}"
            )
        ]
        for item in daily:
            lines.append(
                f"{item['fxDate']}：{item.get('textDay', '未知')}/"
                f"{item.get('textNight', '未知')}，"
                f"{item.get('tempMin', '?')}~{item.get('tempMax', '?')}°C，"
                f"降水 {item.get('precip', '未知')}mm，"
                f"湿度 {item.get('humidity', '未知')}%，"
                f"{item.get('windDirDay', '风向未知')} {item.get('windScaleDay', '?')}级"
            )
        sources = payload.get("refer", {}).get("sources") or ["QWeather"]
        lines.append(f"数据源：{', '.join(sources)}")
        logger.info(
            "QWeather查询完成 location_id=%s daily_count=%d elapsed_ms=%d",
            resolved.get("id"),
            len(daily),
            round((time.perf_counter() - started_at) * 1000),
        )
        return "\n".join(lines)

    async def _resolve_locations(
        self,
        location: str,
        region: str = "",
    ) -> list[dict[str, Any]]:
        """通过 GeoAPI 获取天气查询候选地点。"""
        params = {
            "location": location,
            "range": "cn",
            "number": 3,
            "lang": "zh",
        }
        if region:
            params["adm"] = region
        response = await get_with_retry(
            self._http,
            f"{self._base_url}/geo/v2/city/lookup",
            provider="QWeather GeoAPI",
            params=params,
            headers=self._headers,
        )
        payload = response.json()
        ensure_provider_success(payload, "QWeather GeoAPI")
        locations = payload.get("location", [])
        if not locations:
            raise ValueError(f"QWeather 无法识别地点：{location}")
        return locations


class AmapPlacesClient:
    """通过高德地点搜索 2.0 查询中国大陆 POI。"""

    def __init__(self, api_key: str, http: httpx.AsyncClient) -> None:
        self._api_key = api_key
        self._http = http

    async def search_places(self, query: str, region: str = "") -> str:
        """返回最多五个带 POI ID 的地点摘要。"""
        logger.info(
            "高德地点查询开始 query=%s region=%s",
            log_preview(query),
            log_preview(region),
        )
        started_at = time.perf_counter()
        params = {
            "key": self._api_key,
            "keywords": query,
            "show_fields": "business",
            "page_size": 5,
            "page_num": 1,
        }
        if region:
            params["region"] = region
        response = await get_with_retry(
            self._http,
            "https://restapi.amap.com/v5/place/text",
            provider="高德 Places",
            params=params,
        )
        payload = response.json()
        if payload.get("status") != "1":
            raise ValueError(f"高德地点查询失败：{payload.get('info', '未知错误')}")

        pois = payload.get("pois", [])[:5]
        if not pois:
            logger.info(
                "高德地点查询完成 result_count=0 elapsed_ms=%d",
                round((time.perf_counter() - started_at) * 1000),
            )
            return "高德地点查询：没有找到匹配地点。"

        lines = [f"高德地点查询：{len(pois)} 个结果"]
        for index, poi in enumerate(pois, start=1):
            business = poi.get("business") or {}
            address = normalize_text(poi.get("address")) or "地址未知"
            lines.extend(
                [
                    f"{index}. {poi.get('name', '名称未知')}（{poi.get('type', '类型未知')}）",
                    f"   地址：{address}；坐标：{poi.get('location', '未知')}",
                    (
                        f"   今日营业：{business.get('opentime_today', '未知')}；"
                        f"POI ID：{poi.get('id', '未知')}"
                    ),
                ]
            )
        logger.info(
            "高德地点查询完成 result_count=%d elapsed_ms=%d",
            len(pois),
            round((time.perf_counter() - started_at) * 1000),
        )
        return "\n".join(lines)


class TavilyWebSearchClient:
    """把 Tavily MCP Tool 包装成稳定、紧凑的网页搜索接口。"""

    def __init__(self, tavily_search_tool: BaseTool) -> None:
        self._tool = tavily_search_tool

    async def search(self, query: str) -> str:
        """调用 Tavily 搜索并保留前五条摘要及来源。"""
        logger.info("Tavily查询开始 query=%s", log_preview(query))
        started_at = time.perf_counter()
        raw_result = await retry_network_call(
            "Tavily MCP",
            lambda: self._tool.ainvoke({"query": query}),
            is_retryable_mcp_error,
        )
        payload = parse_tavily_result(raw_result)
        results = payload.get("results", [])[:5]
        if not results:
            logger.info(
                "Tavily查询完成 result_count=0 elapsed_ms=%d",
                round((time.perf_counter() - started_at) * 1000),
            )
            return "Tavily 网页搜索：没有找到匹配结果。"

        lines = [f"Tavily 网页搜索：{len(results)} 个结果"]
        for index, result in enumerate(results, start=1):
            content = normalize_text(result.get("content"))[:500]
            lines.extend(
                [
                    f"{index}. {result.get('title', '无标题')}",
                    f"   {content}",
                    f"   来源：{result.get('url', '未知')}",
                ]
            )
        logger.info(
            "Tavily查询完成 result_count=%d elapsed_ms=%d",
            len(results),
            round((time.perf_counter() - started_at) * 1000),
        )
        return "\n".join(lines)


@dataclass(frozen=True)
class TravelQueryClients:
    """汇集一次应用生命周期内复用的三个只读查询客户端。"""

    weather: QWeatherClient
    places: AmapPlacesClient
    web_search: TavilyWebSearchClient


@asynccontextmanager
async def open_travel_query_clients(
    settings: TravelToolSettings,
) -> AsyncIterator[TravelQueryClients]:
    """打开 HTTP 客户端和持久 Tavily MCP Session。"""
    logger.info("旅行查询客户端启动开始 provider=qweather,amap,tavily_mcp")
    mcp_client = MultiServerMCPClient(
        {
            "tavily": {
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "tavily-mcp@0.2.22"],
                "env": {
                    "TAVILY_API_KEY": settings.tavily_api_key.get_secret_value(),
                    "DEFAULT_PARAMETERS": json.dumps(
                        {
                            "include_images": False,
                            "include_raw_content": False,
                            "max_results": 5,
                            "search_depth": "basic",
                        }
                    ),
                },
            }
        }
    )
    async with (
        httpx.AsyncClient(timeout=15.0) as http,
        mcp_client.session("tavily") as session,
    ):
        mcp_tools = await load_mcp_tools(session)
        tavily_search = next(
            (tool for tool in mcp_tools if tool.name == "tavily_search"),
            None,
        )
        if tavily_search is None:
            available_tools = "、".join(tool.name for tool in mcp_tools) or "无"
            raise RuntimeError(
                f"Tavily MCP Server 未提供 tavily_search Tool，实际加载：{available_tools}"
            )

        clients = TravelQueryClients(
            weather=QWeatherClient(
                settings.qweather_api_host,
                settings.qweather_api_key.get_secret_value(),
                http,
            ),
            places=AmapPlacesClient(
                settings.amap_web_service_key.get_secret_value(),
                http,
            ),
            web_search=TavilyWebSearchClient(tavily_search),
        )
        logger.info("旅行查询客户端启动完成 tavily_tool=%s", tavily_search.name)
        try:
            yield clients
        finally:
            logger.info("旅行查询客户端关闭")


def parse_date_range(value: str) -> tuple[date, date]:
    """从字符串中读取一个日期或起止两个 ISO 日期。"""
    matches = re.findall(r"\d{4}-\d{2}-\d{2}", value)
    if not matches:
        raise ValueError("天气时间段需要包含 YYYY-MM-DD 格式的绝对日期")
    start = date.fromisoformat(matches[0])
    end = date.fromisoformat(matches[1] if len(matches) > 1 else matches[0])
    if end < start:
        raise ValueError("天气时间段的结束日期不能早于开始日期")
    return start, end


async def retry_network_call[ResultT](
    provider: str,
    operation: Callable[[], Awaitable[ResultT]],
    is_retryable: Callable[[Exception], bool],
) -> ResultT:
    """对只读网络调用执行两次短退避重试，最终异常保持原样。"""
    attempt = 1
    while True:
        try:
            return await operation()
        except Exception as error:
            retry_index = attempt - 1
            if retry_index >= len(NETWORK_RETRY_DELAYS) or not is_retryable(error):
                raise
            delay = NETWORK_RETRY_DELAYS[retry_index]
            logger.warning(
                "外部查询瞬时失败，准备重试 provider=%s attempt=%d/%d delay=%.1fs "
                "error_type=%s error=%s",
                provider,
                attempt + 1,
                len(NETWORK_RETRY_DELAYS) + 1,
                delay,
                type(error).__name__,
                error,
            )
            await asyncio.sleep(delay)
            attempt += 1


async def get_with_retry(
    http: httpx.AsyncClient,
    url: str,
    *,
    provider: str,
    **request_kwargs: Any,
) -> httpx.Response:
    """执行带选择性重试的 HTTP GET，并在每次响应后校验状态码。"""

    async def request() -> httpx.Response:
        response = await http.get(url, **request_kwargs)
        response.raise_for_status()
        return response

    return await retry_network_call(provider, request, is_retryable_http_error)


def is_retryable_http_error(error: Exception) -> bool:
    """只把连接、超时、协议错误和明确的临时 HTTP 状态视为可重试。"""
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in RETRYABLE_HTTP_STATUS_CODES
    return isinstance(
        error,
        (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
        ),
    )


def is_retryable_mcp_error(error: Exception) -> bool:
    """识别 MCP 调用期间的传输中断或超时，不重试业务执行错误。"""
    return isinstance(
        error,
        (
            TimeoutError,
            ConnectionError,
            EOFError,
            anyio.BrokenResourceError,
            anyio.ClosedResourceError,
            anyio.EndOfStream,
        ),
    ) or is_retryable_http_error(error)


def select_forecast_days(end: date) -> int:
    """选择能够覆盖结束日期的最小 QWeather 预报窗口。"""
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    required_days = (end - today).days + 1
    if required_days < 1:
        raise ValueError("当前天气 Tool 暂不查询历史天气")
    for days in (3, 7, 10, 15, 30):
        if required_days <= days:
            return days
    raise ValueError("目标日期超出 QWeather 未来30天预报范围")


def ensure_provider_success(payload: dict[str, Any], provider: str) -> None:
    """把供应商业务错误转换成 Agent 可理解的明确异常。"""
    if payload.get("code") != "200":
        raise ValueError(f"{provider} 查询失败，错误码：{payload.get('code', '未知')}")


def parse_tavily_result(raw_result: Any) -> dict[str, Any]:
    """解析 Tavily MCP 返回的 JSON 文本或结构化结果。"""
    if isinstance(raw_result, dict):
        return raw_result
    if isinstance(raw_result, list):
        # LangChain MCP Adapter 会把 TextContent 转换成 content block 列表。
        raw_result = "\n".join(
            str(block["text"])
            for block in raw_result
            if isinstance(block, dict)
            and block.get("type") == "text"
            and block.get("text")
        )
    if isinstance(raw_result, str):
        try:
            parsed = json.loads(raw_result)
        except json.JSONDecodeError:
            return {
                "results": [
                    {"title": "Tavily 搜索结果", "content": raw_result, "url": "未提供"}
                ]
            }
        return parsed if isinstance(parsed, dict) else {"results": []}
    return {"results": []}


def unique_texts(*values: Any) -> list[str]:
    """保持原顺序去除地点描述中的空值和重复值。"""
    result: list[str] = []
    for value in values:
        text = normalize_text(value)
        if text and text not in result:
            result.append(text)
    return result


def normalize_text(value: Any) -> str:
    """把供应商可能返回的字符串或字符串数组统一为短文本。"""
    if isinstance(value, list):
        return "、".join(str(item) for item in value)
    return str(value) if value else ""
