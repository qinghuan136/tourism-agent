"""验证 Orchestrator 根图使用的数据契约。"""

import pytest
from pydantic import ValidationError

from tourism_agent.models.orchestration import (
    OrchestrationPlan,
    PlanReviewDecision,
    ReviewAction,
    TaskSpec,
    TaskType,
)


def test_orchestration_plan_accepts_one_registered_task() -> None:
    """计划任务应使用已注册模块类型，且至少包含一个任务。"""
    plan = OrchestrationPlan(
        goal="找到合适地点并加入行程",
        tasks=[
            TaskSpec(
                task_id="task_1",
                task_type="explore",
                instruction="寻找附近适合闲逛的地点",
            )
        ],
    )

    assert plan.tasks[0].task_type is TaskType.EXPLORE


def test_orchestration_plan_rejects_zero_tasks() -> None:
    """任务计划必须包含至少一个可执行任务。"""
    with pytest.raises(ValidationError):
        OrchestrationPlan(goal="空计划", tasks=[])


def test_orchestration_plan_accepts_five_tasks() -> None:
    """单轮任务计划达到上限时仍应可执行。"""
    plan = OrchestrationPlan(
        goal="完整计划",
        tasks=[
            TaskSpec(
                task_id=f"task_{index}",
                task_type="helper",
                instruction=f"执行任务 {index}",
            )
            for index in range(5)
        ],
    )

    assert len(plan.tasks) == 5


def test_orchestration_plan_rejects_more_than_five_tasks() -> None:
    """单轮执行最多五个任务，避免根图无限扩展。"""
    with pytest.raises(ValidationError):
        OrchestrationPlan(
            goal="过长计划",
            tasks=[
                TaskSpec(
                    task_id=f"task_{index}",
                    task_type="helper",
                    instruction=f"执行任务 {index}",
                )
                for index in range(6)
            ],
        )


def test_review_decision_rejects_unknown_action() -> None:
    """复核阶段只能发出已注册的流程动作。"""
    with pytest.raises(ValidationError):
        PlanReviewDecision(action="retry_forever", reason="无效动作")


def test_review_decision_rejects_replace_remaining_without_tasks() -> None:
    """替换剩余计划时，空任务列表会让根图无任务可执行。"""
    with pytest.raises(ValidationError, match="替换剩余计划时必须提供 1～5 个任务"):
        PlanReviewDecision(
            action="replace_remaining",
            reason="需要重新规划",
            replacement_tasks=[],
        )


@pytest.mark.parametrize("action", ["continue", "finish"])
def test_review_decision_rejects_unused_replacement_tasks(action: str) -> None:
    """继续或结束时携带替换任务会产生被静默忽略的模型输出。"""
    with pytest.raises(ValidationError, match="只有替换剩余计划时才能提供替换任务"):
        PlanReviewDecision(
            action=action,
            reason="不需要替换计划",
            replacement_tasks=[
                TaskSpec(
                    task_id="task_2",
                    task_type="research",
                    instruction="调查候选地点",
                )
            ],
        )


def test_review_decision_accepts_replace_remaining_with_tasks() -> None:
    """合法替换决定应保留新的有限任务列表。"""
    decision = PlanReviewDecision(
        action="replace_remaining",
        reason="需要调查明确候选",
        replacement_tasks=[
            TaskSpec(
                task_id="task_2",
                task_type="research",
                instruction="调查候选地点",
            )
        ],
        handoff_context="沙面是当前候选，下一步需要核实开放时间。",
    )

    assert decision.action is ReviewAction.REPLACE_REMAINING
    assert [task.task_id for task in decision.replacement_tasks] == ["task_2"]


def test_review_decision_requires_handoff_when_execution_continues() -> None:
    """继续执行却缺少交接上下文时，下一个 Task 会失去已有结果。"""
    with pytest.raises(ValidationError, match="必须提供交接上下文"):
        PlanReviewDecision(action="continue", reason="继续执行")


def test_review_decision_rejects_handoff_when_execution_finishes() -> None:
    """已经结束的计划不得保留一个永远不会被消费的交接上下文。"""
    with pytest.raises(ValidationError, match="不能提供交接上下文"):
        PlanReviewDecision(
            action="finish",
            reason="目标已经完成",
            handoff_context="不应继续传递",
        )


def test_review_decision_rejects_more_than_five_replacement_tasks() -> None:
    """替换剩余计划也必须遵守单轮最多五个任务的限制。"""
    with pytest.raises(ValidationError, match="替换剩余计划时必须提供 1～5 个任务"):
        PlanReviewDecision(
            action="replace_remaining",
            reason="需要重新规划",
            replacement_tasks=[
                TaskSpec(
                    task_id=f"task_{index}",
                    task_type="research",
                    instruction=f"调查候选地点 {index}",
                )
                for index in range(6)
            ],
        )
