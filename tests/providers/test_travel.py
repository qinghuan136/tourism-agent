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


@tool("tavily_extract")
async def unused_tavily_extract(urls: list[str]) -> dict[str, object]:
    """为只验证搜索的测试提供不会被调用的 Extract 依赖。"""
    raise AssertionError(f"不应调用 Tavily Extract：{urls}")


@tool("tavily_map")
async def unused_tavily_map(url: str) -> dict[str, object]:
    """为非网站结构测试提供不会被调用的 Map 依赖。"""
    raise AssertionError(f"不应调用 Tavily Map：{url}")


@tool("tavily_crawl")
async def unused_tavily_crawl(url: str) -> dict[str, object]:
    """为非站内抓取测试提供不会被调用的 Crawl 依赖。"""
    raise AssertionError(f"不应调用 Tavily Crawl：{url}")


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


def test_amap_place_details_returns_selected_poi_business_data() -> None:
    """地点详情客户端应保留商业信息和大型地点的导航出入口。"""
    def handle_request(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v5/place/detail"
        assert request.url.params["id"] == "B000A8UIN8"
        assert request.url.params["show_fields"] == "business,navi"
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
                        "business": {
                            "opentime_today": "08:30-17:00",
                            "opentime_week": "周二至周日 08:30-17:00",
                            "tel": "010-85007421",
                            "rating": "4.8",
                            "cost": "60",
                        },
                        "navi": {
                            "entr_location": "116.397100,39.918100",
                            "exit_location": "116.396900,39.918000",
                        },
                    }
                ],
            },
        )

    async def get_details() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http:
            travel = import_module("tourism_agent.providers.travel")
            client = travel.AmapPlacesClient("amap-key", http)
            return await client.get_place_details("B000A8UIN8")

    result = asyncio.run(get_details())

    assert "高德地点详情：故宫博物院" in result
    assert "周二至周日 08:30-17:00" in result
    assert "010-85007421" in result
    assert "评分：4.8" in result
    assert "参考人均：60元" in result
    assert "导航入口：116.397100,39.918100" in result
    assert "导航出口：116.396900,39.918000" in result


def test_amap_nearby_search_returns_distance_sorted_pois() -> None:
    """周边搜索客户端应按中心坐标和半径查询，并返回候选地点距离。"""
    def handle_request(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v5/place/around"
        assert request.url.params["keywords"] == "亲子餐厅"
        assert request.url.params["location"] == "116.397029,39.918058"
        assert request.url.params["radius"] == "3000"
        assert request.url.params["sortrule"] == "distance"
        assert request.url.params["page_size"] == "5"
        return httpx.Response(
            200,
            json={
                "status": "1",
                "infocode": "10000",
                "pois": [
                    {
                        "id": "B001",
                        "name": "亲子餐厅",
                        "type": "餐饮服务",
                        "address": "东城区示例路1号",
                        "location": "116.400000,39.920000",
                        "distance": "850",
                        "business": {"rating": "4.6", "cost": "120"},
                    }
                ],
            },
        )

    async def search_nearby() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http:
            travel = import_module("tourism_agent.providers.travel")
            client = travel.AmapPlacesClient("amap-key", http)
            return await client.search_nearby_places(
                "亲子餐厅",
                "116.397029,39.918058",
                3000,
            )

    result = asyncio.run(search_nearby())

    assert "高德周边地点：1 个结果" in result
    assert "距离中心：850米" in result
    assert "评分：4.6" in result
    assert "参考人均：120元" in result


def test_amap_driving_route_resolves_places_and_returns_compact_paths() -> None:
    """驾车路线应先解析地点，再使用确定性策略返回裁剪后的候选方案。"""
    requested_paths: list[str] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/v5/place/text":
            keyword = request.url.params["keywords"]
            poi = {
                "广州南站": {
                    "id": "P_ORIGIN",
                    "name": "广州南站",
                    "location": "113.269000,22.989000",
                    "citycode": "020",
                    "adcode": "440113",
                },
                "广州塔": {
                    "id": "P_DEST",
                    "name": "广州塔",
                    "location": "113.324500,23.106500",
                    "citycode": "020",
                    "adcode": "440105",
                },
            }[keyword]
            assert request.url.params["region"] == "广州"
            assert request.url.params["page_size"] == "1"
            return httpx.Response(200, json={"status": "1", "pois": [poi]})

        assert request.url.path == "/v5/direction/driving"
        assert request.url.params["origin"] == "113.269000,22.989000"
        assert request.url.params["destination"] == "113.324500,23.106500"
        assert request.url.params["origin_id"] == "P_ORIGIN"
        assert request.url.params["destination_id"] == "P_DEST"
        assert request.url.params["strategy"] == "33"
        assert request.url.params["show_fields"] == "cost"
        return httpx.Response(
            200,
            json={
                "status": "1",
                "route": {
                    "taxi_cost": "85",
                    "paths": [
                        {
                            "distance": "24500",
                            "cost": {"duration": "2400", "tolls": "0"},
                            "steps": [
                                {"instruction": "沿汉溪大道向东行驶"},
                                {"instruction": "进入新光快速路"},
                            ],
                        }
                    ],
                },
            },
        )

    async def plan_route() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http:
            travel = import_module("tourism_agent.providers.travel")
            client = travel.AmapRouteClient("amap-key", http)
            return await client.plan_route(
                "广州南站",
                "广州塔",
                "driving",
                "广州",
                preference="避开拥堵",
            )

    result = asyncio.run(plan_route())

    assert requested_paths == [
        "/v5/place/text",
        "/v5/place/text",
        "/v5/direction/driving",
    ]
    assert "广州南站 → 广州塔" in result
    assert "方案1：24.5公里；预计40分钟；道路收费0元" in result
    assert "沿汉溪大道向东行驶 → 进入新光快速路" in result
    assert "完整轨迹" not in result


def test_amap_transit_route_maps_city_time_and_preference() -> None:
    """公交路线应传入两端城市、出发时间和确定性的换乘策略。"""
    def handle_request(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v5/place/text":
            keyword = request.url.params["keywords"]
            poi = {
                "东莞站": {
                    "id": "P_DG",
                    "name": "东莞站",
                    "location": "113.858000,23.089000",
                    "citycode": "0769",
                    "adcode": "441900",
                },
                "广州塔": {
                    "id": "P_GZ",
                    "name": "广州塔",
                    "location": "113.324500,23.106500",
                    "citycode": "020",
                    "adcode": "440105",
                },
            }[keyword]
            return httpx.Response(200, json={"status": "1", "pois": [poi]})

        assert request.url.path == "/v5/direction/transit/integrated"
        assert request.url.params["city1"] == "0769"
        assert request.url.params["city2"] == "020"
        assert request.url.params["originpoi"] == "P_DG"
        assert request.url.params["destinationpoi"] == "P_GZ"
        assert request.url.params["strategy"] == "1"
        assert request.url.params["date"] == "2026-08-28"
        assert request.url.params["time"] == "09-30"
        assert request.url.params["AlternativeRoute"] == "3"
        return httpx.Response(
            200,
            json={
                "status": "1",
                "route": {
                    "transits": [
                        {
                            "distance": "68000",
                            "cost": {"duration": "5400", "transit_fee": "42"},
                            "segments": [
                                {
                                    "bus": {
                                        "buslines": [
                                            {"name": "城际列车C7001"},
                                            {"name": "地铁3号线"},
                                        ]
                                    }
                                }
                            ],
                        }
                    ]
                },
            },
        )

    async def plan_route() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http:
            travel = import_module("tourism_agent.providers.travel")
            client = travel.AmapRouteClient("amap-key", http)
            return await client.plan_route(
                "东莞站",
                "广州塔",
                "transit",
                "广东",
                departure_time="2026-08-28 09:30",
                preference="票价最低",
            )

    result = asyncio.run(plan_route())

    assert "方式：公交" in result
    assert "68公里；预计1小时30分钟；公交费用42元" in result
    assert "城际列车C7001 → 地铁3号线" in result


def test_amap_distance_resolves_multiple_origins_and_preserves_result_order() -> None:
    """距离比较应按输入顺序解析多个起点并对应高德返回序号。"""
    def handle_request(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v5/place/text":
            keyword = request.url.params["keywords"]
            locations = {
                "酒店A": "113.300000,23.100000",
                "酒店B": "113.310000,23.110000",
                "广州塔": "113.324500,23.106500",
            }
            return httpx.Response(
                200,
                json={
                    "status": "1",
                    "pois": [
                        {
                            "id": f"P_{keyword}",
                            "name": keyword,
                            "location": locations[keyword],
                            "citycode": "020",
                            "adcode": "440105",
                        }
                    ],
                },
            )

        assert request.url.path == "/v3/distance"
        assert request.url.params["origins"] == (
            "113.300000,23.100000|113.310000,23.110000"
        )
        assert request.url.params["destination"] == "113.324500,23.106500"
        assert request.url.params["type"] == "1"
        return httpx.Response(
            200,
            json={
                "status": "1",
                "results": [
                    {"origin_id": "1", "distance": "4200", "duration": "900"},
                    {"origin_id": "2", "distance": "2600", "duration": "600"},
                ],
            },
        )

    async def measure_distance() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http:
            travel = import_module("tourism_agent.providers.travel")
            client = travel.AmapRouteClient("amap-key", http)
            return await client.measure_travel_distance(
                ["酒店A", "酒店B"],
                "广州塔",
                "driving",
                "广州",
            )

    result = asyncio.run(measure_distance())

    assert "1. 酒店A：4.2公里；预计15分钟" in result
    assert "2. 酒店B：2.6公里；预计10分钟" in result
    assert result.index("酒店A") < result.index("酒店B")


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
    client = travel.TavilyWebSearchClient(
        tavily_search,
        unused_tavily_extract,
        unused_tavily_map,
        unused_tavily_crawl,
    )

    result = asyncio.run(client.search("故宫近期预约规则"))

    assert result == (
        "Tavily 网页搜索：1 个结果\n"
        "1. 故宫博物院参观须知\n"
        "   参观需要提前实名预约。\n"
        "   来源：https://example.test/palace"
    )


def test_tavily_mcp_extract_is_reduced_to_focused_page_content() -> None:
    """Tavily Extract 应提取少量 URL，并把关注内容映射到 query 参数。"""
    @tool("tavily_search")
    async def tavily_search(query: str) -> dict[str, object]:
        """本测试不调用搜索。"""
        return {"query": query, "results": []}

    @tool("tavily_extract")
    async def tavily_extract(
        urls: list[str],
        extract_depth: str,
        include_images: bool,
        format: str,
        query: str,
    ) -> list[dict[str, str]]:
        """模拟 MCP Adapter 返回的网页提取 content block。"""
        assert urls == ["https://example.test/notice"]
        assert extract_depth == "basic"
        assert include_images is False
        assert format == "markdown"
        assert query == "预约规则"
        return [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "results": [
                            {
                                "url": "https://example.test/notice",
                                "raw_content": "## 预约说明\n参观需要提前实名预约。",
                            }
                        ],
                        "failed_results": [],
                    },
                    ensure_ascii=False,
                ),
            }
        ]

    travel = import_module("tourism_agent.providers.travel")
    client = travel.TavilyWebSearchClient(
        tavily_search,
        tavily_extract,
        unused_tavily_map,
        unused_tavily_crawl,
    )

    result = asyncio.run(
        client.extract(["https://example.test/notice"], "预约规则")
    )

    assert result == (
        "Tavily 网页提取：1 个结果\n"
        "1. 来源：https://example.test/notice\n"
        "## 预约说明\n"
        "参观需要提前实名预约。"
    )


def test_tavily_mcp_map_and_crawl_use_bounded_parameters_and_compact_results() -> None:
    """Map/Crawl 必须限制站内规模，并把 MCP 文本转换成稳定的紧凑结果。"""
    calls: dict[str, dict[str, object]] = {}

    @tool("tavily_search")
    async def tavily_search(query: str) -> dict[str, object]:
        """本测试不调用搜索。"""
        return {"query": query, "results": []}

    @tool("tavily_extract")
    async def tavily_extract(urls: list[str]) -> dict[str, object]:
        """本测试不调用网页提取。"""
        return {"urls": urls, "results": []}

    @tool("tavily_map")
    async def tavily_map(
        url: str,
        max_depth: int,
        max_breadth: int,
        limit: int,
        instructions: str,
        select_paths: list[str],
        select_domains: list[str],
        allow_external: bool,
    ) -> list[dict[str, str]]:
        """模拟 MCP Server 返回格式化的网站地图文本。"""
        calls["map"] = {
            "url": url,
            "max_depth": max_depth,
            "max_breadth": max_breadth,
            "limit": limit,
            "instructions": instructions,
            "select_paths": select_paths,
            "select_domains": select_domains,
            "allow_external": allow_external,
        }
        return [
            {
                "type": "text",
                "text": (
                    "Site Map Results:\n"
                    "Base URL: https://example.test/travel\n\n"
                    "Mapped Pages:\n\n"
                    "[1] URL: https://example.test/travel/transport\n\n"
                    "[2] URL: https://example.test/travel/hours"
                ),
            }
        ]

    @tool("tavily_crawl")
    async def tavily_crawl(
        url: str,
        max_depth: int,
        max_breadth: int,
        limit: int,
        instructions: str,
        select_paths: list[str],
        select_domains: list[str],
        allow_external: bool,
        extract_depth: str,
        format: str,
        include_favicon: bool,
    ) -> list[dict[str, str]]:
        """模拟 MCP Server 返回格式化的站内抓取文本。"""
        calls["crawl"] = {
            "url": url,
            "max_depth": max_depth,
            "max_breadth": max_breadth,
            "limit": limit,
            "instructions": instructions,
            "select_paths": select_paths,
            "select_domains": select_domains,
            "allow_external": allow_external,
            "extract_depth": extract_depth,
            "format": format,
            "include_favicon": include_favicon,
        }
        return [
            {
                "type": "text",
                "text": (
                    "Crawl Results:\n"
                    "Base URL: https://example.test/travel\n\n"
                    "Crawled Pages:\n\n"
                    "[1] URL: https://example.test/travel/transport\n"
                    "Content: 地铁每日运营至23:00。\n\n"
                    "[2] URL: https://example.test/travel/hours\n"
                    "Content: 景区开放时间为08:30-17:00。"
                ),
            }
        ]

    travel = import_module("tourism_agent.providers.travel")
    client = travel.TavilyWebSearchClient(
        tavily_search,
        tavily_extract,
        tavily_map,
        tavily_crawl,
    )

    map_result = asyncio.run(
        client.map_site(
            "https://example.test/travel",
            "寻找交通和开放时间页面",
        )
    )
    crawl_result = asyncio.run(
        client.crawl_site(
            "https://example.test/travel",
            "只抓取交通和开放时间内容",
        )
    )

    assert calls["map"] == {
        "url": "https://example.test/travel",
        "max_depth": 2,
        "max_breadth": 10,
        "limit": 30,
        "instructions": "寻找交通和开放时间页面",
        "select_paths": [],
        "select_domains": [],
        "allow_external": False,
    }
    assert calls["crawl"] == {
        "url": "https://example.test/travel",
        "max_depth": 2,
        "max_breadth": 8,
        "limit": 10,
        "instructions": "只抓取交通和开放时间内容",
        "select_paths": [],
        "select_domains": [],
        "allow_external": False,
        "extract_depth": "basic",
        "format": "markdown",
        "include_favicon": False,
    }
    assert map_result == (
        "Tavily 网站地图：2 个页面\n"
        "根地址：https://example.test/travel\n"
        "1. https://example.test/travel/transport\n"
        "2. https://example.test/travel/hours"
    )
    assert crawl_result == (
        "Tavily 网站抓取：2 个页面\n"
        "根地址：https://example.test/travel\n"
        "1. 来源：https://example.test/travel/transport\n"
        "地铁每日运营至23:00。\n"
        "2. 来源：https://example.test/travel/hours\n"
        "景区开放时间为08:30-17:00。"
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
    client = travel.TavilyWebSearchClient(
        unstable_tavily_search,
        unused_tavily_extract,
        unused_tavily_map,
        unused_tavily_crawl,
    )
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
