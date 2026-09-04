"""验证 Orchestrator 计划、复核与最终回复节点的边界。"""

import asyncio
from datetime import UTC, datetime
from typing import Any, NotRequired

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel

from tourism_agent.graph.nodes.orchestrator import (
    create_finalize_node,
    create_plan_node,
    create_review_node,
)
from tourism_agent.graph.state import RootState
from tourism_agent.models.context import ConversationMessage, ConversationRole
from tourism_agent.models.orchestration import (
    OrchestrationPlan,
    PlanReviewDecision,
    ReviewAction,
    TaskResult,
    TaskSpec,
    TaskType,
)

RECENT_CONVERSATION = [
    ConversationMessage(
        id=1,
        role=ConversationRole.USER,
        content="我刚才在比较沙面和二沙岛",
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
]


class StructuredOrchestratorFakeModel:
    """以不同结构化契约模拟同一基础模型的三个配置。"""

    def __init__(self) -> None:
        self.plan_messages: list[BaseMessage] = []
        self.review_messages: list[BaseMessage] = []
        self.finalize_messages: list[BaseMessage] = []
        self.finalize_tags: list[str] = []

    def with_structured_output(self, schema: type[BaseModel]) -> RunnableLambda:
        def respond(messages: list[BaseMessage]) -> BaseModel:
            if schema is OrchestrationPlan:
                self.plan_messages = messages
                return schema(
                    goal="研究候选并加入行程",
                    tasks=[
                        TaskSpec(
                            task_id="task_1",
                            task_type="research",
                            instruction="研究刚才选中的候选",
                        ),
                        TaskSpec(
                            task_id="task_2",
                            task_type="planning",
                            instruction="将合适候选加入行程",
                        ),
                    ],
                )
            self.review_messages = messages
            return PlanReviewDecision(
                action="replace_remaining",
                reason="Explore 已经给出明确候选",
                replacement_tasks=[
                    TaskSpec(
                        task_id="task_3",
                        task_type="research",
                        instruction="深入调查沙面",
                    )
                ],
                handoff_context="沙面是当前候选，下一步需要核实开放时间。",
            )

        return RunnableLambda(respond)

    def with_config(self, **kwargs: object) -> RunnableLambda:
        def respond(messages: list[BaseMessage]) -> AIMessage:
            if kwargs["tags"] == ["orchestrator", "finalize", "public_output"]:
                self.finalize_messages = messages
                self.finalize_tags = kwargs["tags"]  # type: ignore[assignment]
            return AIMessage(
                content="已筛选并调研沙面，等待你确认行程调整。"
            )

        return RunnableLambda(respond)


def test_plan_node_builds_labeled_context_and_initializes_execution_state() -> None:
    """遗漏历史标签或执行初始值会导致后续任务误解上下文或继承旧状态。"""
    model = StructuredOrchestratorFakeModel()
    node = create_plan_node(model)  # type: ignore[arg-type]

    result = asyncio.run(
        node(
            {
                "user_input": "就按刚才的地点深入研究后加入行程",
                "routing_context": RECENT_CONVERSATION,
                "current_task": TaskSpec(
                    task_id="old_task",
                    task_type="helper",
                    instruction="旧任务",
                ),
                "latest_task_result": TaskResult(
                    task_id="old_task",
                    task_type="helper",
                    status="success",
                    result="旧结果",
                ),
                "review_decision": PlanReviewDecision(
                    action="finish",
                    reason="旧决策",
                ),
            }
        )
    )

    assert all(
        str(message.content).startswith("【历史消息】")
        for message in model.plan_messages[1:-1]
    )
    assert str(model.plan_messages[-1].content).startswith("【当前消息】")
    assert result["orchestration_goal"] == "研究候选并加入行程"
    assert len(result["pending_tasks"]) == 2
    assert result["task_results"] == []
    assert result["executed_task_count"] == 0
    assert result["current_task"] is None
    assert result["latest_task_result"] is None
    assert result["review_decision"] is None


def test_root_state_review_decision_allows_plan_node_cleanup_none() -> None:
    """计划节点清空旧复核决定时，RootState 契约必须允许 None。"""
    review_decision_type = RootState.__annotations__["review_decision"]

    assert review_decision_type == NotRequired[PlanReviewDecision | None]


def test_review_node_receives_results_and_returns_structured_decision() -> None:
    """复核模型必须基于执行结果产出可由图消费的结构化决策。"""
    model = StructuredOrchestratorFakeModel()
    state: dict[str, Any] = {
        "user_input": "寻找合适地点后加入行程",
        "orchestration_goal": "寻找合适地点后加入行程",
        "pending_tasks": [
            TaskSpec(
                task_id="task_2",
                task_type="planning",
                instruction="将合适候选加入行程",
            )
        ],
        "task_results": [
            TaskResult(
                task_id="task_1",
                task_type="explore",
                status="success",
                result="沙面最适合进一步调研。",
            )
        ],
    }

    result = asyncio.run(create_review_node(model)(state))  # type: ignore[arg-type]

    assert result["review_decision"].action is ReviewAction.REPLACE_REMAINING
    assert result["review_decision"].replacement_tasks[0].task_type is TaskType.RESEARCH
    assert (
        result["review_decision"].handoff_context
        == "沙面是当前候选，下一步需要核实开放时间。"
    )
    review_input = str(model.review_messages[-1].content)
    assert "寻找合适地点后加入行程" in review_input
    assert "沙面最适合进一步调研。" in review_input
    assert '"task_type": "explore"' in review_input
    assert "将合适候选加入行程" in review_input
    assert '"task_type": "planning"' in review_input


def test_finalize_node_does_not_repeat_full_itinerary() -> None:
    """最终回复只总结结论，不能重复由后端单独返回的完整行程。"""
    model = StructuredOrchestratorFakeModel()
    state: dict[str, Any] = {
        "user_input": "寻找合适地点后加入行程",
        "orchestration_goal": "寻找合适地点后加入行程",
        "executed_task_count": 2,
        "current_itinerary": "SENTINEL_CURRENT_ITINERARY_DO_NOT_SEND",
        "candidate_itinerary": "SENTINEL_CANDIDATE_ITINERARY_DO_NOT_SEND",
        "task_results": [
            TaskResult(
                task_id="task_2",
                task_type="planning",
                status="success",
                result="已生成沙面候选行程，完整行程由后端单独返回。",
            )
        ],
    }

    result = asyncio.run(create_finalize_node(model)(state))  # type: ignore[arg-type]

    assert result == {"response": "已筛选并调研沙面，等待你确认行程调整。"}
    assert model.finalize_tags == ["orchestrator", "finalize", "public_output"]
    assert "第一天：" not in result["response"]
    finalize_input = str(model.finalize_messages[-1].content)
    assert "寻找合适地点后加入行程" in finalize_input
    assert "已生成沙面候选行程，完整行程由后端单独返回。" in finalize_input
    finalize_context = "\n".join(str(message.content) for message in model.finalize_messages)
    assert "SENTINEL_CURRENT_ITINERARY_DO_NOT_SEND" not in finalize_context
    assert "SENTINEL_CANDIDATE_ITINERARY_DO_NOT_SEND" not in finalize_context
