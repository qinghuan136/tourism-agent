"""提供根图 Orchestrator 的计划、复核和最终回复节点。"""

import json
import logging
from collections.abc import Awaitable, Callable
from typing import cast

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from tourism_agent.graph.messages import conversation_to_messages
from tourism_agent.graph.state import RootState
from tourism_agent.models.orchestration import OrchestrationPlan, PlanReviewDecision

logger = logging.getLogger(__name__)

ORCHESTRATOR_PLAN_PROMPT = """
你是旅行 Agent 根图中的 Orchestrator Planner，只负责为当前请求生成结构化任务计划。

你不能回答用户、不能调用 Tool，也不能生成完整行程。根据【历史消息】理解指代，但只能处理最后一条
【当前消息】。返回的 goal 概括本轮目标；tasks 应按需要列出 1～5 个独立、可执行且按顺序执行的任务。
每个任务必须使用允许的 task_type，instruction 必须清晰描述对应模块要完成的工作。

planning、explore、research、helper 的职责边界如下：
- planning：生成、调整或确认具体行程安排；
- explore：发现、推荐或比较目的地、地点、活动和旅行风格；
- research：围绕明确旅行对象进行深入调查、核实来源或分析风险；
- helper：处理轻量对话、已有信息解释或局部公开事实查询。

这些任务会由领域模块执行，因此不要把未经执行的事实、推荐或行程写成结论。
""".strip()

ORCHESTRATOR_REVIEW_PROMPT = """
你是旅行 Agent 根图中的 Orchestrator Reviewer，只负责复核任务执行结果并返回结构化决策。

输入 JSON 包含本轮原始目标、已完成任务结果和剩余任务。根据真实 TaskResult 决定：继续剩余任务、
用 replacement_tasks 替换剩余任务，或结束。不能调用 Tool，不能回答用户，不能把计划内容当成事实。
replacement_tasks 仅在 action 为 replace_remaining 时填写，并保持 1～5 个明确可执行的任务；其他
action 返回空列表。

如果后续仍有任务，必须在 handoff_context 中根据全部已完成 TaskResult 整理下一项实际任务需要的
有效结果、约束、证据线索和待确认问题。只能整理已有结果，不得补充新事实，也不要暴露内部 task_id、
JSON 或调度过程。如果 action 为 finish，handoff_context 必须为空字符串。
""".strip()

ORCHESTRATOR_FINALIZE_PROMPT = """
你是旅行 Agent 根图中的最终回复节点，只根据输入 JSON 的原始目标与真实 TaskResult 向用户总结结果。

回复必须说明最终选择、关键理由，以及是否产生或修改行程。不要调用 Tool，不要编造未执行任务的事实，
不要输出完整 CurrentItinerary，也不要输出完整行程；完整行程由后端单独返回。输出简洁自然的中文回复。
不要暴露 task_id、TaskSpec/TaskResult JSON 或内部调度过程。
""".strip()

OrchestratorNode = Callable[[RootState], Awaitable[dict[str, object]]]


def create_plan_node(model: BaseChatModel) -> OrchestratorNode:
    """创建仅生成结构化任务计划的 Orchestrator 节点。"""
    planner = model.with_structured_output(OrchestrationPlan).with_config(
        tags=["orchestrator", "planner"]
    )

    async def create_plan(state: RootState) -> dict[str, object]:
        history = conversation_to_messages(
            state.get("routing_context", []),
            label="【历史消息】",
        )
        plan = cast(
            OrchestrationPlan,
            await planner.ainvoke(
                [
                    SystemMessage(content=ORCHESTRATOR_PLAN_PROMPT),
                    *history,
                    HumanMessage(content=f"【当前消息】\n{state['user_input']}"),
                ]
            ),
        )
        logger.info(
            "Orchestrator初始计划 task_count=%d tasks=%s",
            len(plan.tasks),
            ",".join(f"{task.task_id}:{task.task_type.value}" for task in plan.tasks),
        )
        return {
            "orchestration_goal": plan.goal,
            "pending_tasks": plan.tasks,
            "task_results": [],
            "executed_task_count": 0,
            "current_task": None,
            "latest_task_result": None,
            "review_decision": None,
            "handoff_context": "",
        }

    return create_plan


def create_review_node(model: BaseChatModel) -> OrchestratorNode:
    """创建根据执行结果返回结构化续办决策的 Orchestrator 节点。"""
    reviewer = model.with_structured_output(PlanReviewDecision).with_config(
        tags=["orchestrator", "reviewer"]
    )

    async def review_plan(state: RootState) -> dict[str, object]:
        review_context = {
            "user_input": state["user_input"],
            "orchestration_goal": state["orchestration_goal"],
            "completed_tasks": [
                result.model_dump(mode="json") for result in state.get("task_results", [])
            ],
            "remaining_tasks": [
                task.model_dump(mode="json") for task in state.get("pending_tasks", [])
            ],
        }
        decision = cast(
            PlanReviewDecision,
            await reviewer.ainvoke(
                [
                    SystemMessage(content=ORCHESTRATOR_REVIEW_PROMPT),
                    HumanMessage(
                        content=(
                            "【任务复核上下文】\n"
                            f"{json.dumps(review_context, ensure_ascii=False)}"
                        )
                    ),
                ]
            ),
        )
        logger.info(
            "Orchestrator复核决定 action=%s pending_count=%d replacement_count=%d",
            decision.action.value,
            len(state.get("pending_tasks", [])),
            len(decision.replacement_tasks),
        )
        return {"review_decision": decision}

    return review_plan


def create_finalize_node(model: BaseChatModel) -> OrchestratorNode:
    """创建仅汇总任务结果且不重复完整行程的最终回复节点。"""
    finalizer = model.with_config(tags=["orchestrator", "finalize"])

    async def finalize_response(state: RootState) -> dict[str, object]:
        final_context = {
            "user_input": state["user_input"],
            "orchestration_goal": state["orchestration_goal"],
            "executed_task_count": state.get("executed_task_count", 0),
            "task_results": [
                result.model_dump(mode="json") for result in state.get("task_results", [])
            ],
        }
        response = cast(
            AIMessage,
            await finalizer.ainvoke(
                [
                    SystemMessage(content=ORCHESTRATOR_FINALIZE_PROMPT),
                    HumanMessage(
                        content=(
                            "【最终回复上下文】\n"
                            f"{json.dumps(final_context, ensure_ascii=False)}"
                        )
                    ),
                ]
            ),
        )
        logger.info(
            "Orchestrator执行结束 executed_task_count=%d",
            state.get("executed_task_count", 0),
        )
        return {"response": response.text}

    return finalize_response
