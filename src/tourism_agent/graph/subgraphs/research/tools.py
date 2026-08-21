"""提供 Research 私有交互 Tool、重规划 Tool 和只读查询白名单。"""

import logging
from collections.abc import Sequence
from typing import cast

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command, interrupt

from tourism_agent.graph.subgraphs.research.state import ResearchState
from tourism_agent.infrastructure.logging_config import log_preview

RESEARCH_QUERY_TOOL_NAMES = {
    "get_weather",
    "search_places",
    "get_place_details",
    "search_nearby_places",
    "web_search",
    "extract_web_content",
    "plan_route",
    "measure_travel_distance",
    "map_web_site",
    "crawl_web_site",
}
MAX_PLAN_REVISIONS = 2
logger = logging.getLogger(__name__)


@tool
def ask_user(
    question: str,
    runtime: ToolRuntime[None, ResearchState],
) -> Command:
    """只在缺失信息会显著改变研究范围或结论时暂停询问用户。"""
    logger.info(
        "Tool调用开始 name=ask_user module=research trip_id=%s "
        "tool_call_id=%s question=%s",
        runtime.state["trip_id"],
        runtime.tool_call_id,
        log_preview(question),
    )
    answer = interrupt({"kind": "ask_user", "question": question})
    logger.info(
        "Tool恢复完成 name=ask_user module=research trip_id=%s answer=%s",
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


@tool
def revise_research_plan(
    reason: str,
    runtime: ToolRuntime[None, ResearchState],
) -> Command:
    """请求确定性程序流重新生成研究计划，不允许 Agent 直接覆盖计划。"""
    revision_count = runtime.state.get("plan_revision_count", 0)
    logger.info(
        "Tool调用开始 name=revise_research_plan trip_id=%s "
        "tool_call_id=%s revision_count=%d reason=%s",
        runtime.state["trip_id"],
        runtime.tool_call_id,
        revision_count,
        log_preview(reason),
    )
    tool_call_id = cast(str, runtime.tool_call_id)
    if revision_count >= MAX_PLAN_REVISIONS:
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content=(
                            "研究计划已达到两次重规划上限。请基于现有计划和证据完成研究，"
                            "并在报告中说明仍未解决的问题。"
                        ),
                        tool_call_id=tool_call_id,
                    )
                ]
            }
        )

    return Command(
        update={
            "replan_reason": reason,
            "messages": [
                ToolMessage(
                    content="重规划请求已接受，系统将根据已有证据重新生成研究计划。",
                    tool_call_id=tool_call_id,
                )
            ],
        }
    )


def create_research_tools(query_tools: Sequence[BaseTool] = ()) -> list[BaseTool]:
    """只选择 Research 获准使用的公共只读能力并追加两个私有 Tool。"""
    selected_query_tools = [
        query_tool
        for query_tool in query_tools
        if query_tool.name in RESEARCH_QUERY_TOOL_NAMES
    ]
    return [*selected_query_tools, ask_user, revise_research_plan]
