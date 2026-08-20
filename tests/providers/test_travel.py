"""验证旅行查询供应商客户端的稳定边界。"""

import asyncio
import json
import os
from datetime import datetime, timedelta
from importlib import import_module
from zoneinfo import ZoneInfo

import httpx
import pytest
from langchain_core.tools import tool


def test_travel_tool_settings_load_provider_credentials(monkeypatch) -> None:
    """三个外部服务应统一从环境变量读取连接配置。"""
    monkeypatch.setenv("QWEATHER_API_HOST", "weather.example")
    monkeypatch.setenv("QWEATHER_API_KEY", "weather-key")
    monkeypatch.setenv("AMAP_WEB_SERVICE_KEY", "amap-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")

    travel = import_module("tourism_agent.providers.travel")
    settings = travel.TravelToolSettings(_env_file=None)

    assert settings.qweather_api_host == "weather.example"
    assert settings.qweather_api_key.get_secret_value() == "weather-key"
    assert settings.amap_web_service_key.get_secret_value() == "amap-key"
    assert settings.tavily_api_key.get_secret_value() == "tavily-key"


def test_qweather_resolves_location_and_filters_requested_dates() -> None:
    """天气客户端应先查询 Location ID，再只返回用户要求的预报日期。"""
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    start = today + timedelta(days=1)
    end = today + timedelta(days=4)
    outside = end + timedelta(days=1)
    requested_paths: list[str] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        assert request.headers["X-QW-Api-Key"] == "weather-key"
        if request.url.path == "/geo/v2/city/lookup":
            assert request.url.params["location"] == "北京"
            return httpx.Response(
                200,
                json={
                    "code": "200",
                    "location": [
                        {
                            "name": "北京",
                            "id": "101010100",
                            "adm1": "北京市",
                            "country": "中国",
                        }
                    ],
                },
            )
        assert request.url.path == "/v7/weather/7d"
        assert request.url.params["location"] == "101010100"
        return httpx.Response(
            200,
            json={
                "code": "200",
                "updateTime": "2026-08-19T10:00+08:00",
                "daily": [
                    {
                        "fxDate": start.isoformat(),
                        "textDay": "晴",
                        "textNight": "多云",
                        "tempMin": "20",
                        "tempMax": "30",
                        "precip": "0.0",
                        "humidity": "45",
                        "windDirDay": "东南风",
                        "windScaleDay": "2",
                    },
                    {
                        "fxDate": end.isoformat(),
                        "textDay": "小雨",
                        "textNight": "阴",
                        "tempMin": "18",
                        "tempMax": "25",
                        "precip": "4.2",
                        "humidity": "80",
                        "windDirDay": "北风",
                        "windScaleDay": "3",
                    },
                    {"fxDate": outside.isoformat(), "textDay": "晴"},
                ],
                "refer": {"sources": ["QWeather"]},
            },
        )

    async def query_weather() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http:
            travel = import_module("tourism_agent.providers.travel")
            client = travel.QWeatherClient("weather.example", "weather-key", http)
            return await client.get_weather(
                "北京",
                f"{start.isoformat()}/{end.isoformat()}",
            )

    result = asyncio.run(query_weather())

    assert requested_paths == ["/geo/v2/city/lookup", "/v7/weather/7d"]
    assert "查询地点：北京，北京市，中国" in result
    assert f"{start.isoformat()}：晴/多云，20~30°C" in result
    assert f"{end.isoformat()}：小雨/阴，18~25°C" in result
    assert outside.isoformat() not in result
    assert "数据源：QWeather" in result


def test_qweather_uses_region_and_top_ranked_location() -> None:
    """region 应缩小 GeoAPI 范围，多个候选时继续使用排名第一的地点。"""
    requested_paths: list[str] = []
    target = datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)

    def handle_request(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/geo/v2/city/lookup":
            assert request.url.params["adm"] == "北京"
            return httpx.Response(
                200,
                json={
                    "code": "200",
                    "location": [
                        {
                            "name": "朝阳",
                            "id": "101010300",
                            "adm2": "北京",
                            "adm1": "北京市",
                            "country": "中国",
                        },
                        {
                            "name": "朝阳",
                            "id": "101071201",
                            "adm2": "朝阳",
                            "adm1": "辽宁省",
                            "country": "中国",
                        },
                    ],
                },
            )

        assert request.url.params["location"] == "101010300"
        return httpx.Response(
            200,
            json={
                "code": "200",
                "updateTime": "2026-08-19T10:00+08:00",
                "daily": [
                    {
                        "fxDate": target.isoformat(),
                        "textDay": "晴",
                        "textNight": "晴",
                        "tempMin": "20",
                        "tempMax": "30",
                    }
                ],
                "refer": {"sources": ["QWeather"]},
            },
        )

    async def query_weather() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http:
            travel = import_module("tourism_agent.providers.travel")
            client = travel.QWeatherClient("weather.example", "weather-key", http)
            return await client.get_weather("朝阳", target.isoformat(), "北京")

    result = asyncio.run(query_weather())

    assert requested_paths == ["/geo/v2/city/lookup", "/v7/weather/3d"]
    assert "查询地点：朝阳，北京，北京市，中国" in result
    assert "地点存在歧义" not in result


def test_amap_places_returns_compact_mainland_poi_observation() -> None:
    """地点客户端应把高德响应裁剪为适合 Agent 阅读的 POI 摘要。"""
    def handle_request(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v5/place/text"
        assert request.url.params["key"] == "amap-key"
        assert request.url.params["keywords"] == "故宫博物院"
        assert request.url.params["region"] == "北京"
        assert request.url.params["page_size"] == "5"
        return httpx.Response(
            200,
            json={
                "status": "1",
                "infocode": "10000",
                "pois": [
                    {
                        "id": "B000A8UIN8",
                        "name": "故宫博物院",
                        "type": "科教文化服务;博物馆",
                        "address": "景山前街4号",
                        "location": "116.397029,39.918058",
                        "business": {"opentime_today": "08:30-17:00"},
                    }
                ],
            },
        )

    async def search_places() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http:
            travel = import_module("tourism_agent.providers.travel")
            client = travel.AmapPlacesClient("amap-key", http)
            return await client.search_places("故宫博物院", "北京")

    result = asyncio.run(search_places())

    assert "高德地点查询：1 个结果" in result
    assert "故宫博物院" in result
    assert "景山前街4号" in result
    assert "116.397029,39.918058" in result
    assert "08:30-17:00" in result
    assert "POI ID：B000A8UIN8" in result


def test_tavily_mcp_search_is_reduced_to_source_summary() -> None:
    """Tavily MCP 原始结果应转换成有限条带来源的网页摘要。"""
    @tool("tavily_search")
    async def tavily_search(query: str) -> list[dict[str, str]]:
        """模拟 MCP Adapter 返回的文本 content block。"""
        assert query == "故宫近期预约规则"
        return [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "results": [
                            {
                                "title": "故宫博物院参观须知",
                                "url": "https://example.test/palace",
                                "content": "参观需要提前实名预约。",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
            }
        ]

    travel = import_module("tourism_agent.providers.travel")
    client = travel.TavilyWebSearchClient(tavily_search)

    result = asyncio.run(client.search("故宫近期预约规则"))

    assert result == (
        "Tavily 网页搜索：1 个结果\n"
        "1. 故宫博物院参观须知\n"
        "   参观需要提前实名预约。\n"
        "   来源：https://example.test/palace"
    )


def test_qweather_retries_transient_geo_http_status(monkeypatch) -> None:
    """GeoAPI 短暂返回 503 时应重试，成功后继续执行天气查询。"""
    target = datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)
    geo_attempts = 0

    async def skip_retry_delay(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", skip_retry_delay)

    def handle_request(request: httpx.Request) -> httpx.Response:
        nonlocal geo_attempts
        if request.url.path == "/geo/v2/city/lookup":
            geo_attempts += 1
            if geo_attempts < 3:
                return httpx.Response(503)
            return httpx.Response(
                200,
                json={
                    "code": "200",
                    "location": [{"name": "北京", "id": "101010100"}],
                },
            )
        return httpx.Response(
            200,
            json={
                "code": "200",
                "daily": [{"fxDate": target.isoformat()}],
                "refer": {"sources": ["QWeather"]},
            },
        )

    async def query_weather() -> str | None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http:
            travel = import_module("tourism_agent.providers.travel")
            client = travel.QWeatherClient("weather.example", "weather-key", http)
            try:
                return await client.get_weather("北京", target.isoformat())
            except httpx.HTTPStatusError:
                return None

    result = asyncio.run(query_weather())

    assert geo_attempts == 3
    assert result is not None
    assert target.isoformat() in result


def test_amap_retries_transient_connection_error(monkeypatch) -> None:
    """高德请求遇到短暂连接错误时应在 Client 内重试。"""
    attempts = 0

    async def skip_retry_delay(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", skip_retry_delay)

    def handle_request(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ConnectError("模拟连接抖动", request=request)
        return httpx.Response(200, json={"status": "1", "pois": []})

    async def search_places() -> str | None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http:
            travel = import_module("tourism_agent.providers.travel")
            client = travel.AmapPlacesClient("amap-key", http)
            try:
                return await client.search_places("故宫", "北京")
            except httpx.ConnectError:
                return None

    result = asyncio.run(search_places())

    assert attempts == 3
    assert result == "高德地点查询：没有找到匹配地点。"


def test_tavily_retries_transient_mcp_timeout(monkeypatch) -> None:
    """Tavily MCP 调用短暂超时时应重试同一次只读搜索。"""
    attempts = 0

    async def skip_retry_delay(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", skip_retry_delay)

    @tool("tavily_search")
    async def unstable_tavily_search(query: str) -> dict[str, object]:
        """前两次模拟 MCP 传输超时，第三次返回结果。"""
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise TimeoutError("模拟 MCP 超时")
        return {
            "results": [
                {
                    "title": "故宫参观须知",
                    "url": "https://example.test/palace",
                    "content": query,
                }
            ]
        }

    travel = import_module("tourism_agent.providers.travel")
    client = travel.TavilyWebSearchClient(unstable_tavily_search)
    try:
        result = asyncio.run(client.search("故宫预约"))
    except TimeoutError:
        result = None

    assert attempts == 3
    assert result is not None
    assert "故宫参观须知" in result


@pytest.mark.skipif(
    os.getenv("RUN_MCP_INTEGRATION") != "1",
    reason="仅在显式开启时启动本地 Tavily MCP Server",
)
def test_local_tavily_mcp_server_exposes_web_search_client(monkeypatch) -> None:
    """npm Tavily Server 应能通过 LangChain MCP Adapter 建立持久会话。"""
    monkeypatch.setenv("QWEATHER_API_HOST", "weather.example")
    monkeypatch.setenv("QWEATHER_API_KEY", "weather-key")
    monkeypatch.setenv("AMAP_WEB_SERVICE_KEY", "amap-key")
    monkeypatch.setenv("TAVILY_API_KEY", "test-tavily-key")
    travel = import_module("tourism_agent.providers.travel")
    settings = travel.TravelToolSettings(_env_file=None)

    async def open_clients() -> str:
        async with travel.open_travel_query_clients(settings) as clients:
            return type(clients.web_search).__name__

    assert asyncio.run(open_clients()) == "TavilyWebSearchClient"
