"""组装 Orchestrator 顺序调度与当前业务子图。"""

import logging
from collections.abc import Sequence
from typing import Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from tourism_agent.graph.history import ConversationHistorySearcher
from tourism_agent.graph.nodes.orchestrator import (
    create_finalize_node,
    create_plan_node,
    create_review_node,
)
from tourism_agent.graph.state import RootState
from tourism_agent.graph.subgraphs.explore.graph import build_explore_graph
from tourism_agent.graph.subgraphs.helper.graph import build_helper_graph
from tourism_agent.graph.subgraphs.planning.graph import build_planning_graph
from tourism_agent.graph.subgraphs.research.graph import build_research_graph
from tourism_agent.infrastructure.logging_config import log_preview
from tourism_agent.models.orchestration import (
    ReviewAction,
    TaskResult,
    TaskStatus,
    TaskType,
)
from tourism_agent.repositories.planning import PlanningRepository

logger = logging.getLogger(__name__)
ROUTING_CONVERSATION_LIMIT = 4
MAX_TASKS_PER_TURN = 5


def build_task_message(state: RootState) -> str:
    """构造仅供子图执行当前 Task 的内部消息，不写入 Conversation。"""
    task = state["current_task"]
    assert task is not None
    handoff = state.get("handoff_context", "").strip()
    return (
        f"【原始用户目标】\n{state['user_input']}\n\n"
        f"【当前子任务】\n{task.instruction}\n\n"
        "【Orchestrator整理的现有结果】\n"
        f"{handoff or '当前没有已完成任务的有效结果'}"
    )


def build_retrieval_query(state: RootState) -> str:
    """构造面向历史召回的紧凑查询，避免直接嵌入完整执行消息。"""
    task = state["current_task"]
    assert task is not None
    parts = [
        f"【当前检索目标】\n{task.instruction}",
        f"【用户总体目标】\n{state['user_input']}",
    ]
    handoff = state.get("handoff_context", "").strip()
    if handoff:
        parts.append(f"【Orchestrator整理的现有结果】\n{handoff}")
    return "\n\n".join(parts)


def prepare_next_task(state: RootState) -> dict[str, object]:
    """从待办队列取出下一项，并在进入可能 interrupt 的子图前记录路由。"""
    pending = state.get("pending_tasks", [])
    if not pending or state.get("executed_task_count", 0) >= MAX_TASKS_PER_TURN:
        return {"current_task": None}
    return {
        "current_task": pending[0],
        "pending_tasks": pending[1:],
        "route": pending[0].task_type,
    }


def select_task(state: RootState) -> TaskType | Literal["finalize"]:
    """根据已记录的当前 Task 确定子图或最终汇总节点。"""
    task = state.get("current_task")
    return task.task_type if task is not None else "finalize"


def record_task_result(state: RootState) -> dict[str, object]:
    """确定性记录刚完成的 Task，并清理当前执行槽位。"""
    latest = state["latest_task_result"]
    assert latest is not None
    logger.info(
        "Orchestrator任务结果 task_id=%s task_type=%s status=%s result_preview=%s",
        latest.task_id,
        latest.task_type.value,
        latest.status.value,
        log_preview(latest.result, limit=200),
    )
    return {
        "task_results": [*state.get("task_results", []), latest],
        "latest_task_result": None,
        "current_task": None,
        "executed_task_count": state.get("executed_task_count", 0) + 1,
    }


def apply_review_decision(state: RootState) -> dict[str, object]:
    """应用复核决定，只替换尚未执行的 Task。"""
    decision = state["review_decision"]
    assert decision is not None
    if decision.action is ReviewAction.REPLACE_REMAINING:
        return {
            "pending_tasks": decision.replacement_tasks,
            "handoff_context": decision.handoff_context,
        }
    if decision.action is ReviewAction.FINISH:
        return {"pending_tasks": [], "handoff_context": ""}
    return {"handoff_context": decision.handoff_context}


def select_after_review(
    state: RootState,
) -> Literal["prepare_next_task", "finalize"]:
    """根据复核动作、完成上限和剩余任务选择确定性出口。"""
    decision = state["review_decision"]
    assert decision is not None
    if decision.action is ReviewAction.FINISH:
        return "finalize"
    if state.get("executed_task_count", 0) >= MAX_TASKS_PER_TURN:
        return "finalize"
    if not state.get("pending_tasks"):
        return "finalize"
    return "prepare_next_task"


def build_root_graph(
    model: BaseChatModel,
    repository: PlanningRepository,
    checkpointer: BaseCheckpointSaver | None = None,
    *,
    query_tools: Sequence[BaseTool] = (),
    retrieval_service: ConversationHistorySearcher | None = None,
) -> CompiledStateGraph:
    """构建按结构化计划顺序执行当前业务子图的根图。"""
    # 根图只传递公共能力；每个子图自行维护最小 Tool 白名单。
    planning_graph = build_planning_graph(
        model,
        repository,
        query_tools,
        retrieval_service=retrieval_service,
    )
    explore_graph = build_explore_graph(
        model,
        repository,
        query_tools,
        retrieval_service=retrieval_service,
    )
    research_graph = build_research_graph(
        model,
        repository,
        query_tools,
        retrieval_service=retrieval_service,
    )
    helper_graph = build_helper_graph(
        model,
        repository,
        query_tools,
        retrieval_service=retrieval_service,
    )

    async def load_orchestrator_context(state: RootState) -> dict[str, object]:
        """只加载编排所需的少量历史，不读取模块业务 Context。"""
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
    ) -> dict[str, object]:
        """在根图与 PlanningState 之间执行明确的输入输出映射。"""
        task = state["current_task"]
        assert task is not None
        logger.info("进入Planning子图 trip_id=%s", state["trip_id"])
        result = await planning_graph.ainvoke(
            {
                "user_id": state["user_id"],
                "trip_id": state["trip_id"],
                "user_message_id": state["user_message_id"],
                "retrieval_query": build_retrieval_query(state),
                "retrieval_user_input": state["user_input"],
                "retrieval_task_goal": task.instruction,
                "messages": [HumanMessage(content=build_task_message(state))],
            },
            config=config,
        )
        logger.info(
            "Planning子图返回 trip_id=%s response_length=%d has_candidate=%s",
            state["trip_id"],
            len(result["assistant_message"]),
            bool(result.get("candidate_itinerary")),
        )
        return {
            "latest_task_result": TaskResult(
                task_id=task.task_id,
                task_type=task.task_type,
                status=TaskStatus.SUCCESS,
                result=result["assistant_message"],
            ),
            "candidate_itinerary": result.get("candidate_itinerary"),
            "current_itinerary": result.get("current_itinerary"),
        }

    async def run_explore(
        state: RootState,
        config: RunnableConfig,
    ) -> dict[str, object]:
        """在根图与 ExploreState 之间执行明确的输入输出映射。"""
        task = state["current_task"]
        assert task is not None
        logger.info("进入Explore子图 trip_id=%s", state["trip_id"])
        result = await explore_graph.ainvoke(
            {
                "user_id": state["user_id"],
                "trip_id": state["trip_id"],
                "user_message_id": state["user_message_id"],
                "retrieval_query": build_retrieval_query(state),
                "retrieval_user_input": state["user_input"],
                "retrieval_task_goal": task.instruction,
                "messages": [HumanMessage(content=build_task_message(state))],
            },
            config=config,
        )
        logger.info(
            "Explore子图返回 trip_id=%s response_length=%d has_candidate=%s",
            state["trip_id"],
            len(result["assistant_message"]),
            bool(result.get("candidate_itinerary")),
        )
        return {
            "latest_task_result": TaskResult(
                task_id=task.task_id,
                task_type=task.task_type,
                status=TaskStatus.SUCCESS,
                result=result["assistant_message"],
            ),
            "candidate_itinerary": result.get("candidate_itinerary"),
            "current_itinerary": result.get("current_itinerary"),
        }

    async def run_research(
        state: RootState,
        config: RunnableConfig,
    ) -> dict[str, object]:
        """在根图与 ResearchState 之间执行明确的输入输出映射。"""
        task = state["current_task"]
        assert task is not None
        logger.info("进入Research子图 trip_id=%s", state["trip_id"])
        result = await research_graph.ainvoke(
            {
                "user_id": state["user_id"],
                "trip_id": state["trip_id"],
                "user_message_id": state["user_message_id"],
                "retrieval_query": build_retrieval_query(state),
                "retrieval_user_input": state["user_input"],
                "retrieval_task_goal": task.instruction,
                "messages": [HumanMessage(content=build_task_message(state))],
            },
            config=config,
        )
        logger.info(
            "Research子图返回 trip_id=%s response_length=%d",
            state["trip_id"],
            len(result["assistant_message"]),
        )
        return {
            "latest_task_result": TaskResult(
                task_id=task.task_id,
                task_type=task.task_type,
                status=TaskStatus.SUCCESS,
                result=result["assistant_message"],
            ),
            "candidate_itinerary": result.get("candidate_itinerary"),
            "current_itinerary": result.get("current_itinerary"),
        }

    async def run_helper(
        state: RootState,
        config: RunnableConfig,
    ) -> dict[str, object]:
        """在根图与 HelperState 之间执行明确的输入输出映射。"""
        task = state["current_task"]
        assert task is not None
        logger.info("进入Helper子图 trip_id=%s", state["trip_id"])
        result = await helper_graph.ainvoke(
            {
                "user_id": state["user_id"],
                "trip_id": state["trip_id"],
                "user_message_id": state["user_message_id"],
                "retrieval_query": build_retrieval_query(state),
                "retrieval_user_input": state["user_input"],
                "retrieval_task_goal": task.instruction,
                "messages": [HumanMessage(content=build_task_message(state))],
            },
            config=config,
        )
        logger.info(
            "Helper子图返回 trip_id=%s response_length=%d has_candidate=%s",
            state["trip_id"],
            len(result["assistant_message"]),
            bool(result.get("candidate_itinerary")),
        )
        return {
            "latest_task_result": TaskResult(
                task_id=task.task_id,
                task_type=task.task_type,
                status=TaskStatus.SUCCESS,
                result=result["assistant_message"],
            ),
            "candidate_itinerary": result.get("candidate_itinerary"),
            "current_itinerary": result.get("current_itinerary"),
        }

    builder = StateGraph(RootState)
    builder.add_node("load_orchestrator_context", load_orchestrator_context)
    builder.add_node("create_plan", create_plan_node(model))
    builder.add_node("prepare_next_task", prepare_next_task)
    builder.add_node("planning", run_planning)
    builder.add_node("explore", run_explore)
    builder.add_node("research", run_research)
    builder.add_node("helper", run_helper)
    builder.add_node("record_task_result", record_task_result)
    builder.add_node("review_plan", create_review_node(model))
    builder.add_node("apply_review_decision", apply_review_decision)
    builder.add_node("finalize", create_finalize_node(model))

    builder.add_edge(START, "load_orchestrator_context")
    builder.add_edge("load_orchestrator_context", "create_plan")
    builder.add_edge("create_plan", "prepare_next_task")
    builder.add_conditional_edges(
        "prepare_next_task",
        select_task,
        {
            TaskType.PLANNING: "planning",
            TaskType.EXPLORE: "explore",
            TaskType.RESEARCH: "research",
            TaskType.HELPER: "helper",
            "finalize": "finalize",
        },
    )
    builder.add_edge("planning", "record_task_result")
    builder.add_edge("explore", "record_task_result")
    builder.add_edge("research", "record_task_result")
    builder.add_edge("helper", "record_task_result")
    builder.add_edge("record_task_result", "review_plan")
    builder.add_edge("review_plan", "apply_review_decision")
    builder.add_conditional_edges(
        "apply_review_decision",
        select_after_review,
        ["prepare_next_task", "finalize"],
    )
    builder.add_edge("finalize", END)

    # 当前阶段明确使用进程内 checkpoint；生产持久化方案留到后续阶段。
    root_checkpointer = checkpointer if checkpointer is not None else InMemorySaver()
    return builder.compile(checkpointer=root_checkpointer)
