"""提供 Helper 私有的用户提问 Tool 和公共查询 Tool 白名单。"""

import logging
from collections.abc import Sequence
from typing import cast

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command, interrupt

from tourism_agent.graph.subgraphs.helper.state import HelperState
from tourism_agent.infrastructure.logging_config import log_preview

HELPER_QUERY_TOOL_NAMES = {
    "get_weather",
    "search_places",
    "get_place_details",
    "search_nearby_places",
    "web_search",
    "extract_web_content",
    "plan_route",
    "measure_travel_distance",
}
logger = logging.getLogger(__name__)


@tool
def ask_user(
    question: str,
    runtime: ToolRuntime[None, HelperState],
) -> Command:
    """只在关键信息缺失、Helper 无法继续时暂停并询问用户。"""
    logger.info(
        "Tool调用开始 name=ask_user module=helper trip_id=%s tool_call_id=%s question=%s",
        runtime.state["trip_id"],
        runtime.tool_call_id,
        log_preview(question),
    )
    answer = interrupt({"kind": "ask_user", "question": question})
    logger.info(
        "Tool恢复完成 name=ask_user module=helper trip_id=%s answer=%s",
        runtime.state["trip_id"],
        log_preview(answer),
    )
    return Command(
        update={
            "messages": [
                ToolMessage(
                    content=str(answer),
                    tool_call_id=cast(str, runtime.tool_call_id),
                )
            ]
        }
    )


def create_helper_tools(query_tools: Sequence[BaseTool] = ()) -> list[BaseTool]:
    """只选择 Helper 获准使用的公共只读查询能力。"""
    selected_query_tools = [
        query_tool
        for query_tool in query_tools
        if query_tool.name in HELPER_QUERY_TOOL_NAMES
    ]
    return [*selected_query_tools, ask_user]
