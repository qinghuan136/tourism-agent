"""组装理解节点、确定性路由与当前业务子图。"""

import logging
from collections.abc import Sequence

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from tourism_agent.graph.nodes.intent import create_intent_node
from tourism_agent.graph.state import RootState
from tourism_agent.graph.subgraphs.explore.graph import build_explore_graph
from tourism_agent.graph.subgraphs.helper.graph import build_helper_graph
from tourism_agent.graph.subgraphs.planning.graph import build_planning_graph
from tourism_agent.graph.subgraphs.research.graph import build_research_graph
from tourism_agent.infrastructure.logging_config import log_preview
from tourism_agent.models.contracts import RouteTarget
from tourism_agent.repositories.planning import PlanningRepository

logger = logging.getLogger(__name__)
ROUTING_CONVERSATION_LIMIT = 4


def select_route(state: RootState) -> RouteTarget:
    """把理解 Agent 的受约束结果交给 LangGraph 做确定性跳转。"""
    logger.info(
        "根图路由 route=%s trip_id=%s",
        state["route"].value,
        state["trip_id"],
    )
    return state["route"]


def build_root_graph(
    model: BaseChatModel,
    repository: PlanningRepository,
    checkpointer: BaseCheckpointSaver | None = None,
    *,
    query_tools: Sequence[BaseTool] = (),
) -> CompiledStateGraph:
    """构建从意图理解到当前业务子图的可执行根图。"""
    # 根图只传递公共能力；每个子图自行维护最小 Tool 白名单。
    planning_graph = build_planning_graph(model, repository, query_tools)
    explore_graph = build_explore_graph(model, repository, query_tools)
    research_graph = build_research_graph(model, repository, query_tools)
    helper_graph = build_helper_graph(model, repository, query_tools)

    async def load_routing_context(state: RootState) -> dict[str, object]:
        """只加载路由所需的少量历史，不读取模块业务 Context。"""
        logger.info(
            "根图路由上下文加载开始 trip_id=%s user_message_id=%s",
            state["trip_id"],
            state["user_message_id"],
        )
        conversation = await repository.get_recent_conversation(
            state["trip_id"],
            before_message_id=state["user_message_id"],
            limit=ROUTING_CONVERSATION_LIMIT,
        )
        logger.info(
            "根图路由上下文加载完成 trip_id=%s conversation_count=%d",
            state["trip_id"],
            len(conversation),
        )
        return {"routing_context": conversation}

    async def run_planning(
        state: RootState,
        config: RunnableConfig,
    ) -> dict[str, str | None]:
        """在根图与 PlanningState 之间执行明确的输入输出映射。"""
        logger.info("进入Planning子图 trip_id=%s", state["trip_id"])
        result = await planning_graph.ainvoke(
            {
                "user_id": state["user_id"],
                "trip_id": state["trip_id"],
                "user_message_id": state["user_message_id"],
                "messages": [HumanMessage(content=state["user_input"])],
            },
            config=config,
        )
        logger.info(
            "Planning子图返回 trip_id=%s response=%s has_candidate=%s",
            state["trip_id"],
            log_preview(result["assistant_message"]),
            bool(result.get("candidate_itinerary")),
        )
        return {
            "response": result["assistant_message"],
            "candidate_itinerary": result.get("candidate_itinerary"),
            "current_itinerary": result.get("current_itinerary"),
        }

    async def run_explore(
        state: RootState,
        config: RunnableConfig,
    ) -> dict[str, str]:
        """在根图与 ExploreState 之间执行明确的输入输出映射。"""
        logger.info("进入Explore子图 trip_id=%s", state["trip_id"])
        result = await explore_graph.ainvoke(
            {
                "user_id": state["user_id"],
                "trip_id": state["trip_id"],
                "user_message_id": state["user_message_id"],
                "messages": [HumanMessage(content=state["user_input"])],
            },
            config=config,
        )
        logger.info(
            "Explore子图返回 trip_id=%s response=%s",
            state["trip_id"],
            log_preview(result["assistant_message"]),
        )
        return {"response": result["assistant_message"]}

    async def run_research(
        state: RootState,
        config: RunnableConfig,
    ) -> dict[str, str]:
        """在根图与 ResearchState 之间执行明确的输入输出映射。"""
        logger.info("进入Research子图 trip_id=%s", state["trip_id"])
        result = await research_graph.ainvoke(
            {
                "user_id": state["user_id"],
                "trip_id": state["trip_id"],
                "user_message_id": state["user_message_id"],
                "messages": [HumanMessage(content=state["user_input"])],
            },
            config=config,
        )
        logger.info(
            "Research子图返回 trip_id=%s response=%s",
            state["trip_id"],
            log_preview(result["assistant_message"]),
        )
        return {"response": result["assistant_message"]}

    async def run_helper(
        state: RootState,
        config: RunnableConfig,
    ) -> dict[str, str]:
        """在根图与 HelperState 之间执行明确的输入输出映射。"""
        logger.info("进入Helper子图 trip_id=%s", state["trip_id"])
        result = await helper_graph.ainvoke(
            {
                "user_id": state["user_id"],
                "trip_id": state["trip_id"],
                "user_message_id": state["user_message_id"],
                "messages": [HumanMessage(content=state["user_input"])],
            },
            config=config,
        )
        logger.info(
            "Helper子图返回 trip_id=%s response=%s",
            state["trip_id"],
            log_preview(result["assistant_message"]),
        )
        return {"response": result["assistant_message"]}

    builder = StateGraph(RootState)
    builder.add_node("load_routing_context", load_routing_context)
    builder.add_node("understand_intent", create_intent_node(model))
    builder.add_node("planning", run_planning)
    builder.add_node("explore", run_explore)
    builder.add_node("research", run_research)
    builder.add_node("helper", run_helper)

    builder.add_edge(START, "load_routing_context")
    builder.add_edge("load_routing_context", "understand_intent")
    builder.add_conditional_edges(
        "understand_intent",
        select_route,
        {
            RouteTarget.PLANNING: "planning",
            RouteTarget.EXPLORE: "explore",
            RouteTarget.RESEARCH: "research",
            RouteTarget.HELPER: "helper",
        },
    )
    builder.add_edge("planning", END)
    builder.add_edge("explore", END)
    builder.add_edge("research", END)
    builder.add_edge("helper", END)

    # 当前阶段明确使用进程内 checkpoint；生产持久化方案留到后续阶段。
    root_checkpointer = checkpointer if checkpointer is not None else InMemorySaver()
    return builder.compile(checkpointer=root_checkpointer)
