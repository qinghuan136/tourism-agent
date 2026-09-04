"""提供 Planning 私有的 Context、候选方案和用户交互 Tools。"""

import json
import logging
from collections.abc import Sequence
from typing import Any, cast

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command, interrupt

from tourism_agent.graph.subgraphs.planning.state import PlanningState
from tourism_agent.infrastructure.logging_config import log_preview
from tourism_agent.repositories.planning import PlanningRepository

PLANNING_QUERY_TOOL_NAMES = {
    "get_current_datetime",
    "calculate_date",
    "calculate_trip_duration",
    "get_weather",
    "search_places",
    "get_place_details",
    "search_nearby_places",
    "web_search",
    "extract_web_content",
    "plan_route",
    "measure_travel_distance",
    "search_conversation_history",
    "read_conversation_exchanges",
}
logger = logging.getLogger(__name__)


@tool
def ask_user(
    question: str,
    runtime: ToolRuntime[None, PlanningState],
) -> Command:
    """只在缺少继续规划所必需的信息时暂停并询问用户。"""
    logger.info(
        "Tool调用开始 name=ask_user trip_id=%s tool_call_id=%s question=%s",
        runtime.state["trip_id"],
        runtime.tool_call_id,
        log_preview(question),
    )
    payload = {"kind": "ask_user", "question": question}
    answer = interrupt(payload)
    logger.info(
        "Tool恢复完成 name=ask_user trip_id=%s answer=%s",
        runtime.state["trip_id"],
        log_preview(answer),
    )
    tool_message = ToolMessage(
        content=str(answer),
        tool_call_id=cast(str, runtime.tool_call_id),
    )
    return Command(
        update={
            "consecutive_candidate_rejections": 0,
            "messages": [tool_message],
        }
    )


def create_planning_tools(
    repository: PlanningRepository,
    query_tools: Sequence[BaseTool] = (),
) -> list[BaseTool]:
    """为当前 Planning 图创建作用域正确的查询和写入 Tools。"""

    # 公共 Tool 由应用统一创建，但 Planning 只绑定本模块明确获准的能力。
    selected_query_tools = [
        query_tool
        for query_tool in query_tools
        if query_tool.name in PLANNING_QUERY_TOOL_NAMES
    ]

    @tool
    async def update_trip_context(
        patch: dict[str, Any],
        runtime: ToolRuntime[None, PlanningState],
    ) -> Command:
        """增量新增或修改当前旅行信息；patch 使用自由的键值结构。"""
        logger.info(
            "Tool调用开始 name=update_trip_context trip_id=%s tool_call_id=%s keys=%s",
            runtime.state["trip_id"],
            runtime.tool_call_id,
            list(patch),
        )
        updated = await repository.patch_trip_context(runtime.state["trip_id"], patch)
        logger.info(
            "Tool调用完成 name=update_trip_context trip_id=%s result_keys=%s",
            runtime.state["trip_id"],
            list(updated),
        )
        return context_update_command("trip_context", updated, runtime)

    @tool
    async def delete_trip_context_keys(
        keys: list[str],
        runtime: ToolRuntime[None, PlanningState],
    ) -> Command:
        """删除当前旅行信息中的指定顶层键。"""
        logger.info(
            "Tool调用开始 name=delete_trip_context_keys trip_id=%s "
            "tool_call_id=%s keys=%s",
            runtime.state["trip_id"],
            runtime.tool_call_id,
            keys,
        )
        updated = await repository.delete_trip_context_keys(runtime.state["trip_id"], keys)
        logger.info(
            "Tool调用完成 name=delete_trip_context_keys trip_id=%s result_keys=%s",
            runtime.state["trip_id"],
            list(updated),
        )
        return context_update_command("trip_context", updated, runtime)

    @tool
    def submit_candidate_itinerary(
        itinerary: str,
        runtime: ToolRuntime[None, PlanningState],
    ) -> Command:
        """提交完整候选行程；确认与数据库写入由后续确定性节点负责。"""
        logger.info(
            "Tool调用完成 name=submit_candidate_itinerary trip_id=%s "
            "tool_call_id=%s itinerary=%s",
            runtime.state["trip_id"],
            runtime.tool_call_id,
            log_preview(itinerary),
        )
        tool_message = ToolMessage(
            content="候选方案已提交，系统将请求用户确认。",
            tool_call_id=cast(str, runtime.tool_call_id),
        )
        return Command(
            update={
                "candidate_itinerary": itinerary,
                "messages": [tool_message],
            }
        )

    return [
        *selected_query_tools,
        update_trip_context,
        delete_trip_context_keys,
        submit_candidate_itinerary,
        ask_user,
    ]


def context_update_command(
    field: str,
    updated: dict[str, Any],
    runtime: ToolRuntime[None, PlanningState],
) -> Command:
    """把数据库写入结果同步回 State，并完成当前 Tool Call。"""
    tool_message = ToolMessage(
        content=json.dumps(updated, ensure_ascii=False),
        tool_call_id=cast(str, runtime.tool_call_id),
    )
    return Command(update={field: updated, "messages": [tool_message]})
