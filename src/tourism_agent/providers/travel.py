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
from typing import Any, ClassVar
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

    async def get_place_details(self, place_id: str) -> str:
        """根据高德 POI ID 返回一个地点的详细信息。"""
        logger.info("高德地点详情查询开始 place_id=%s", log_preview(place_id))
        started_at = time.perf_counter()
        response = await get_with_retry(
            self._http,
            "https://restapi.amap.com/v5/place/detail",
            provider="高德 Places",
            params={
                "key": self._api_key,
                "id": place_id,
                "show_fields": "business,navi",
            },
        )
        payload = response.json()
        ensure_amap_success(payload, "高德地点详情查询")
        pois = payload.get("pois", [])[:1]
        if not pois:
            return "高德地点详情：没有找到对应地点。"

        poi = pois[0]
        business = poi.get("business") or {}
        navi = poi.get("navi") or {}
        lines = [
            f"高德地点详情：{poi.get('name', '名称未知')}",
            f"类型：{poi.get('type', '未知')}；POI ID：{poi.get('id', place_id)}",
            (
                f"地址：{normalize_text(poi.get('address')) or '未知'}；"
                f"坐标：{poi.get('location', '未知')}"
            ),
            f"今日营业：{business.get('opentime_today', '未知')}",
            f"每周营业：{business.get('opentime_week', '未知')}",
            f"电话：{normalize_text(business.get('tel')) or '未知'}",
            f"评分：{business.get('rating', '未知')}；参考人均：{business.get('cost', '未知')}元",
            f"导航入口：{normalize_text(navi.get('entr_location')) or '未知'}",
            f"导航出口：{normalize_text(navi.get('exit_location')) or '未知'}",
        ]
        logger.info(
            "高德地点详情查询完成 place_id=%s elapsed_ms=%d",
            place_id,
            round((time.perf_counter() - started_at) * 1000),
        )
        return "\n".join(lines)

    async def search_nearby_places(
        self,
        query: str,
        center: str,
        radius_m: int = 5000,
    ) -> str:
        """围绕一个高德坐标点返回按距离排序的地点摘要。"""
        logger.info(
            "高德周边地点查询开始 query=%s center=%s radius_m=%d",
            log_preview(query),
            log_preview(center),
            radius_m,
        )
        started_at = time.perf_counter()
        response = await get_with_retry(
            self._http,
            "https://restapi.amap.com/v5/place/around",
            provider="高德 Places",
            params={
                "key": self._api_key,
                "keywords": query,
                "location": center,
                "radius": radius_m,
                "sortrule": "distance",
                "show_fields": "business",
                "page_size": 5,
                "page_num": 1,
            },
        )
        payload = response.json()
        ensure_amap_success(payload, "高德周边地点查询")
        pois = payload.get("pois", [])[:5]
        if not pois:
            return "高德周边地点：没有找到匹配地点。"

        lines = [f"高德周边地点：{len(pois)} 个结果"]
        for index, poi in enumerate(pois, start=1):
            business = poi.get("business") or {}
            lines.extend(
                [
                    f"{index}. {poi.get('name', '名称未知')}（{poi.get('type', '类型未知')}）",
                    (
                        f"   地址：{normalize_text(poi.get('address')) or '未知'}；"
                        f"距离中心：{poi.get('distance', '未知')}米"
                    ),
                    (
                        f"   评分：{business.get('rating', '未知')}；"
                        f"参考人均：{business.get('cost', '未知')}元；"
                        f"POI ID：{poi.get('id', '未知')}"
                    ),
                ]
            )
        logger.info(
            "高德周边地点查询完成 result_count=%d elapsed_ms=%d",
            len(pois),
            round((time.perf_counter() - started_at) * 1000),
        )
        return "\n".join(lines)


@dataclass(frozen=True)
class AmapResolvedPlace:
    """保存路线查询真正需要的高德地点字段。"""

    query: str
    name: str
    location: str
    place_id: str = ""
    citycode: str = ""
    adcode: str = ""


class AmapRouteClient:
    """把自然语言地点解析后调用高德路线与距离接口。"""

    ROUTE_ENDPOINTS: ClassVar[dict[str, str]] = {
        "driving": "https://restapi.amap.com/v5/direction/driving",
        "walking": "https://restapi.amap.com/v5/direction/walking",
        "cycling": "https://restapi.amap.com/v5/direction/bicycling",
        "transit": "https://restapi.amap.com/v5/direction/transit/integrated",
    }
    DISTANCE_TYPES: ClassVar[dict[str, str]] = {
        "straight": "0",
        "driving": "1",
        "walking": "3",
    }

    def __init__(self, api_key: str, http: httpx.AsyncClient) -> None:
        self._api_key = api_key
        self._http = http

    async def plan_route(
        self,
        origin: str,
        destination: str,
        mode: str,
        region: str = "",
        departure_time: str = "",
        preference: str = "",
    ) -> str:
        """查询两地之间的驾车、步行、骑行或公交方案。"""
        if mode not in self.ROUTE_ENDPOINTS:
            raise ValueError("路线方式只支持 driving、walking、cycling 或 transit")
        logger.info(
            "高德路线查询开始 origin=%s destination=%s mode=%s region=%s",
            log_preview(origin),
            log_preview(destination),
            mode,
            log_preview(region),
        )
        started_at = time.perf_counter()
        start = await self._resolve_place(origin, region)
        end = await self._resolve_place(destination, region)
        params = self._route_params(start, end, mode, departure_time, preference)
        response = await get_with_retry(
            self._http,
            self.ROUTE_ENDPOINTS[mode],
            provider="高德路线规划",
            params=params,
        )
        payload = response.json()
        ensure_amap_success(payload, "高德路线规划")
        result = self._format_route(payload, start, end, mode)
        logger.info(
            "高德路线查询完成 mode=%s elapsed_ms=%d",
            mode,
            round((time.perf_counter() - started_at) * 1000),
        )
        return result

    async def measure_travel_distance(
        self,
        origins: list[str],
        destination: str,
        mode: str = "driving",
        region: str = "",
    ) -> str:
        """比较多个自然语言起点到同一目的地的距离和耗时。"""
        if not origins:
            raise ValueError("距离比较至少需要一个起点")
        if len(origins) > 10:
            raise ValueError("距离比较一次最多接受10个起点")
        if mode not in self.DISTANCE_TYPES:
            raise ValueError("距离方式只支持 driving、walking 或 straight")
        logger.info(
            "高德距离比较开始 origin_count=%d destination=%s mode=%s region=%s",
            len(origins),
            log_preview(destination),
            mode,
            log_preview(region),
        )
        started_at = time.perf_counter()
        resolved_origins = [
            await self._resolve_place(origin, region) for origin in origins
        ]
        resolved_destination = await self._resolve_place(destination, region)
        response = await get_with_retry(
            self._http,
            "https://restapi.amap.com/v3/distance",
            provider="高德距离测量",
            params={
                "key": self._api_key,
                "origins": "|".join(place.location for place in resolved_origins),
                "destination": resolved_destination.location,
                "type": self.DISTANCE_TYPES[mode],
            },
        )
        payload = response.json()
        ensure_amap_success(payload, "高德距离测量")
        results_by_origin = {
            int(result["origin_id"]): result
            for result in payload.get("results", [])
            if str(result.get("origin_id", "")).isdigit()
        }
        lines = [
            (
                f"高德距离比较：目的地 {resolved_destination.name}；方式："
                f"{route_mode_label(mode)}；查询时间：{current_query_time()}"
            )
        ]
        for index, place in enumerate(resolved_origins, start=1):
            result = results_by_origin.get(index, {})
            if result.get("info"):
                lines.append(f"{index}. {place.name}：无法计算（{result['info']}）")
                continue
            lines.append(
                f"{index}. {place.name}：{format_distance(result.get('distance'))}；"
                f"预计{format_duration(result.get('duration'))}"
            )
        logger.info(
            "高德距离比较完成 origin_count=%d elapsed_ms=%d",
            len(resolved_origins),
            round((time.perf_counter() - started_at) * 1000),
        )
        return "\n".join(lines)

    async def _resolve_place(self, query: str, region: str) -> AmapResolvedPlace:
        """将坐标或自然语言地点转换成路线接口需要的稳定字段。"""
        if re.fullmatch(r"-?\d{1,3}(?:\.\d{1,6})?,-?\d{1,2}(?:\.\d{1,6})?", query):
            return AmapResolvedPlace(query=query, name=query, location=query)
        params = {
            "key": self._api_key,
            "keywords": query,
            "page_size": 1,
            "page_num": 1,
        }
        if region:
            params["region"] = region
        response = await get_with_retry(
            self._http,
            "https://restapi.amap.com/v5/place/text",
            provider="高德路线地点解析",
            params=params,
        )
        payload = response.json()
        ensure_amap_success(payload, "高德路线地点解析")
        pois = payload.get("pois", [])[:1]
        if not pois:
            raise ValueError(f"高德无法识别路线地点：{query}")
        poi = pois[0]
        return AmapResolvedPlace(
            query=query,
            name=normalize_text(poi.get("name")) or query,
            location=normalize_text(poi.get("location")),
            place_id=normalize_text(poi.get("id")),
            citycode=normalize_text(poi.get("citycode")),
            adcode=normalize_text(poi.get("adcode")),
        )

    def _route_params(
        self,
        origin: AmapResolvedPlace,
        destination: AmapResolvedPlace,
        mode: str,
        departure_time: str,
        preference: str,
    ) -> dict[str, str | int]:
        """根据出行方式构造对应高德接口的确定性参数。"""
        params: dict[str, str | int] = {
            "key": self._api_key,
            "origin": origin.location,
            "destination": destination.location,
            "show_fields": "cost",
        }
        if mode in {"driving", "walking"}:
            if origin.place_id:
                params["origin_id"] = origin.place_id
            if destination.place_id:
                params["destination_id"] = destination.place_id
        if mode == "driving":
            params["strategy"] = driving_strategy(preference)
        elif mode in {"walking", "cycling"}:
            params["alternative_route"] = 3
        else:
            if not origin.citycode or not destination.citycode:
                raise ValueError("公交路线需要能够识别起点和终点城市，请使用地点名而非裸坐标")
            params.update(
                {
                    "city1": origin.citycode,
                    "city2": destination.citycode,
                    "strategy": transit_strategy(preference),
                    "AlternativeRoute": 3,
                }
            )
            if origin.place_id and destination.place_id:
                params["originpoi"] = origin.place_id
                params["destinationpoi"] = destination.place_id
            if origin.adcode:
                params["ad1"] = origin.adcode
            if destination.adcode:
                params["ad2"] = destination.adcode
            if departure_time:
                date_value, time_value = parse_departure_time(departure_time)
                params["date"] = date_value
                params["time"] = time_value.replace(":", "-")
        return params

    def _format_route(
        self,
        payload: dict[str, Any],
        origin: AmapResolvedPlace,
        destination: AmapResolvedPlace,
        mode: str,
    ) -> str:
        """将不同路线接口的结果统一成紧凑的 Agent Observation。"""
        route = payload.get("route") or {}
        candidates = (
            route.get("transits", []) if mode == "transit" else route.get("paths", [])
        )[:3]
        if not candidates:
            return "高德路线规划：没有找到可用路线。"
        lines = [
            (
                f"高德路线规划：{origin.name} → {destination.name}；"
                f"方式：{route_mode_label(mode)}；查询时间：{current_query_time()}"
            )
        ]
        for index, candidate in enumerate(candidates, start=1):
            cost = candidate.get("cost") or {}
            fee_label = route_fee_label(mode, cost)
            summary = (
                f"方案{index}：{format_distance(candidate.get('distance'))}；"
                f"预计{format_duration(cost.get('duration') or candidate.get('duration'))}"
            )
            if fee_label:
                summary += f"；{fee_label}"
            lines.append(summary)
            steps = (
                transit_step_names(candidate)
                if mode == "transit"
                else route_step_instructions(candidate)
            )
            if steps:
                lines.append(f"   关键步骤：{' → '.join(steps[:5])}")
        return "\n".join(lines)


class TavilyWebSearchClient:
    """把 Tavily MCP Tools 包装成稳定、紧凑的网页查询接口。"""

    def __init__(
        self,
        tavily_search_tool: BaseTool,
        tavily_extract_tool: BaseTool,
        tavily_map_tool: BaseTool,
        tavily_crawl_tool: BaseTool,
    ) -> None:
        self._search_tool = tavily_search_tool
        self._extract_tool = tavily_extract_tool
        self._map_tool = tavily_map_tool
        self._crawl_tool = tavily_crawl_tool

    async def search(self, query: str) -> str:
        """调用 Tavily 搜索并保留前五条摘要及来源。"""
        logger.info("Tavily查询开始 query=%s", log_preview(query))
        started_at = time.perf_counter()
        raw_result = await retry_network_call(
            "Tavily MCP",
            lambda: self._search_tool.ainvoke({"query": query}),
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

    async def extract(self, urls: list[str], focus: str = "") -> str:
        """提取最多三个网页，并保留与当前任务相关的正文。"""
        selected_urls = urls[:3]
        logger.info(
            "Tavily网页提取开始 url_count=%d focus=%s",
            len(selected_urls),
            log_preview(focus),
        )
        started_at = time.perf_counter()
        raw_result = await retry_network_call(
            "Tavily Extract MCP",
            lambda: self._extract_tool.ainvoke(
                {
                    "urls": selected_urls,
                    "extract_depth": "basic",
                    "include_images": False,
                    "format": "markdown",
                    "query": focus,
                }
            ),
            is_retryable_mcp_error,
        )
        payload = parse_tavily_result(raw_result)
        results = payload.get("results", [])[: len(selected_urls)]
        if not results:
            return "Tavily 网页提取：没有获得可用正文。"

        lines = [f"Tavily 网页提取：{len(results)} 个结果"]
        for index, result in enumerate(results, start=1):
            content = normalize_text(
                result.get("raw_content") or result.get("content")
            )[:3000]
            lines.extend(
                [
                    f"{index}. 来源：{result.get('url', '未知')}",
                    content,
                ]
            )
        logger.info(
            "Tavily网页提取完成 result_count=%d elapsed_ms=%d",
            len(results),
            round((time.perf_counter() - started_at) * 1000),
        )
        return "\n".join(lines)

    async def map_site(self, url: str, instructions: str = "") -> str:
        """发现单一网站的结构，并最多返回三十个站内页面。"""
        logger.info(
            "Tavily网站地图开始 url=%s instructions=%s",
            log_preview(url),
            log_preview(instructions),
        )
        started_at = time.perf_counter()
        raw_result = await retry_network_call(
            "Tavily Map MCP",
            lambda: self._map_tool.ainvoke(
                {
                    "url": url,
                    "max_depth": 2,
                    "max_breadth": 10,
                    "limit": 30,
                    "instructions": instructions,
                    "select_paths": [],
                    "select_domains": [],
                    "allow_external": False,
                }
            ),
            is_retryable_mcp_error,
        )
        base_url, pages = parse_tavily_map_result(raw_result)
        if not pages:
            return "Tavily 网站地图：没有发现可用页面。"

        lines = [
            f"Tavily 网站地图：{len(pages)} 个页面",
            f"根地址：{base_url or url}",
        ]
        lines.extend(f"{index}. {page}" for index, page in enumerate(pages, start=1))
        logger.info(
            "Tavily网站地图完成 page_count=%d elapsed_ms=%d",
            len(pages),
            round((time.perf_counter() - started_at) * 1000),
        )
        return "\n".join(lines)

    async def crawl_site(self, url: str, instructions: str = "") -> str:
        """抓取单一网站的少量相关页面，并裁剪每页正文。"""
        logger.info(
            "Tavily站内抓取开始 url=%s instructions=%s",
            log_preview(url),
            log_preview(instructions),
        )
        started_at = time.perf_counter()
        raw_result = await retry_network_call(
            "Tavily Crawl MCP",
            lambda: self._crawl_tool.ainvoke(
                {
                    "url": url,
                    "max_depth": 2,
                    "max_breadth": 8,
                    "limit": 10,
                    "instructions": instructions,
                    "select_paths": [],
                    "select_domains": [],
                    "allow_external": False,
                    "extract_depth": "basic",
                    "format": "markdown",
                    "include_favicon": False,
                }
            ),
            is_retryable_mcp_error,
        )
        base_url, pages = parse_tavily_crawl_result(raw_result)
        if not pages:
            return "Tavily 网站抓取：没有获得可用页面。"

        lines = [
            f"Tavily 网站抓取：{len(pages)} 个页面",
            f"根地址：{base_url or url}",
        ]
        for index, (page_url, content) in enumerate(pages, start=1):
            lines.extend([f"{index}. 来源：{page_url}", content[:1500]])
        logger.info(
            "Tavily站内抓取完成 page_count=%d elapsed_ms=%d",
            len(pages),
            round((time.perf_counter() - started_at) * 1000),
        )
        return "\n".join(lines)


@dataclass(frozen=True)
class TravelQueryClients:
    """汇集一次应用生命周期内复用的公共只读查询客户端。"""

    weather: QWeatherClient
    places: AmapPlacesClient
    routes: AmapRouteClient
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
        tools_by_name = {tool.name: tool for tool in mcp_tools}
        required_names = {
            "tavily_search",
            "tavily_extract",
            "tavily_map",
            "tavily_crawl",
        }
        missing_names = required_names - tools_by_name.keys()
        if missing_names:
            available_tools = "、".join(tool.name for tool in mcp_tools) or "无"
            raise RuntimeError(
                f"Tavily MCP Server 缺少 Tools：{'、'.join(sorted(missing_names))}；"
                f"实际加载：{available_tools}"
            )

        tavily_search = tools_by_name["tavily_search"]
        tavily_extract = tools_by_name["tavily_extract"]
        tavily_map = tools_by_name["tavily_map"]
        tavily_crawl = tools_by_name["tavily_crawl"]

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
            routes=AmapRouteClient(
                settings.amap_web_service_key.get_secret_value(),
                http,
            ),
            web_search=TavilyWebSearchClient(
                tavily_search,
                tavily_extract,
                tavily_map,
                tavily_crawl,
            ),
        )
        logger.info(
            "旅行查询客户端启动完成 tavily_tools=%s",
            [
                tavily_search.name,
                tavily_extract.name,
                tavily_map.name,
                tavily_crawl.name,
            ],
        )
        yield clients
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


def ensure_amap_success(payload: dict[str, Any], operation: str) -> None:
    """把高德业务错误转换成明确异常。"""
    if payload.get("status") != "1":
        raise ValueError(f"{operation}失败：{payload.get('info', '未知错误')}")


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


def parse_tavily_map_result(raw_result: Any) -> tuple[str, list[str]]:
    """把 Tavily Map 的结构化或 MCP 文本结果转换成页面列表。"""
    if isinstance(raw_result, dict):
        pages = [str(page) for page in raw_result.get("results", []) if page]
        return normalize_text(raw_result.get("base_url")), pages[:30]

    text = tavily_mcp_text(raw_result)
    base_match = re.search(r"^Base URL:\s*(\S+)", text, re.MULTILINE)
    pages = re.findall(r"^\[\d+\]\s+URL:\s*(\S+)", text, re.MULTILINE)
    return base_match.group(1) if base_match else "", pages[:30]


def parse_tavily_crawl_result(raw_result: Any) -> tuple[str, list[tuple[str, str]]]:
    """把 Tavily Crawl 结果转换成受限的 URL 与正文列表。"""
    if isinstance(raw_result, dict):
        pages = [
            (
                normalize_text(page.get("url")),
                normalize_text(page.get("raw_content") or page.get("content")),
            )
            for page in raw_result.get("results", [])
            if isinstance(page, dict) and page.get("url")
        ]
        return normalize_text(raw_result.get("base_url")), pages[:10]

    text = tavily_mcp_text(raw_result)
    base_match = re.search(r"^Base URL:\s*(\S+)", text, re.MULTILINE)
    page_matches = re.findall(
        r"^\[\d+\]\s+URL:\s*(\S+)\s*\nContent:\s*(.*?)"
        r"(?=\n\s*\[\d+\]\s+URL:|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    pages = [(page_url, content.strip()) for page_url, content in page_matches]
    return base_match.group(1) if base_match else "", pages[:10]


def tavily_mcp_text(raw_result: Any) -> str:
    """读取 LangChain MCP Adapter 返回的文本 content blocks。"""
    if isinstance(raw_result, str):
        return raw_result
    if isinstance(raw_result, list):
        return "\n".join(
            str(block["text"])
            for block in raw_result
            if isinstance(block, dict)
            and block.get("type") == "text"
            and block.get("text")
        )
    return ""


def driving_strategy(preference: str) -> str:
    """把自然语言驾车偏好映射为高德路线策略。"""
    normalized = preference.replace("避开", "躲避")
    mappings = (
        ("躲避拥堵+不走高速", "40"),
        ("躲避拥堵+少收费", "41"),
        ("少收费+不走高速", "42"),
        ("躲避拥堵", "33"),
        ("高速优先", "34"),
        ("不走高速", "35"),
        ("少收费", "36"),
        ("费用优先", "36"),
        ("大路优先", "37"),
        ("速度最快", "38"),
        ("最快", "38"),
    )
    return next((strategy for keyword, strategy in mappings if keyword in normalized), "32")


def transit_strategy(preference: str) -> str:
    """把自然语言公交偏好映射为高德换乘策略。"""
    mappings = (
        (("票价最低", "最省钱", "最经济"), "1"),
        (("最少换乘", "少换乘"), "2"),
        (("最少步行", "少步行"), "3"),
        (("最舒适", "舒适"), "4"),
        (("不乘地铁", "不坐地铁"), "5"),
        (("地铁优先",), "7"),
        (("时间短", "最快"), "8"),
    )
    return next(
        (
            strategy
            for keywords, strategy in mappings
            if any(keyword in preference for keyword in keywords)
        ),
        "0",
    )


def parse_departure_time(value: str) -> tuple[str, str]:
    """从用户表达中提取高德公交接口需要的绝对日期和时间。"""
    match = re.search(r"(\d{4}-\d{2}-\d{2}).*?(\d{1,2}:\d{2})", value)
    if not match:
        raise ValueError("公交出发时间需要包含 YYYY-MM-DD 和 HH:MM")
    return match.group(1), match.group(2)


def route_mode_label(mode: str) -> str:
    """返回适合 Agent 阅读的出行方式名称。"""
    return {
        "driving": "驾车",
        "walking": "步行",
        "cycling": "骑行",
        "transit": "公交",
        "straight": "直线",
    }.get(mode, mode)


def format_distance(value: Any) -> str:
    """把供应商返回的米数格式化为紧凑距离。"""
    if value in (None, ""):
        return "距离未知"
    meters = float(value)
    if meters < 1000:
        return f"{round(meters)}米"
    kilometers = meters / 1000
    number = f"{kilometers:.1f}".rstrip("0").rstrip(".")
    return f"{number}公里"


def format_duration(value: Any) -> str:
    """把供应商返回的秒数格式化为小时和分钟。"""
    if value in (None, ""):
        return "耗时未知"
    minutes = max(1, round(float(value) / 60))
    hours, remaining_minutes = divmod(minutes, 60)
    if hours and remaining_minutes:
        return f"{hours}小时{remaining_minutes}分钟"
    if hours:
        return f"{hours}小时"
    return f"{remaining_minutes}分钟"


def route_fee_label(mode: str, cost: dict[str, Any]) -> str:
    """从不同路线方式中提取最有意义的费用字段。"""
    if mode == "transit" and cost.get("transit_fee") not in (None, ""):
        return f"公交费用{cost['transit_fee']}元"
    if mode == "driving" and cost.get("tolls") not in (None, ""):
        return f"道路收费{cost['tolls']}元"
    return ""


def route_step_instructions(candidate: dict[str, Any]) -> list[str]:
    """提取路线前几段有效文字指示，不保留轨迹点。"""
    return [
        instruction
        for step in candidate.get("steps", [])
        if (instruction := normalize_text(step.get("instruction")))
    ]


def transit_step_names(candidate: dict[str, Any]) -> list[str]:
    """提取公交方案中的线路名称作为关键换乘摘要。"""
    names: list[str] = []
    for segment in candidate.get("segments", []):
        bus = segment.get("bus") or {}
        for busline in bus.get("buslines", []):
            name = normalize_text(busline.get("name"))
            if name and name not in names:
                names.append(name)
    return names


def current_query_time() -> str:
    """以中国时区记录外部数据查询时间。"""
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


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
