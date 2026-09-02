"""定义根图执行理解、路由和模块调用所需的最小状态。"""

from typing import NotRequired, TypedDict
from uuid import UUID

from tourism_agent.models.context import ConversationMessage
from tourism_agent.models.orchestration import (
    PlanReviewDecision,
    TaskResult,
    TaskSpec,
    TaskType,
)


class RootState(TypedDict):
    """保存单次根图调用中任务编排、路由和返回所需的数据。"""

    user_id: UUID
    trip_id: UUID
    user_message_id: int
    user_input: str
    routing_context: NotRequired[list[ConversationMessage]]
    orchestration_goal: NotRequired[str]
    pending_tasks: NotRequired[list[TaskSpec]]
    current_task: NotRequired[TaskSpec | None]
    task_results: NotRequired[list[TaskResult]]
    latest_task_result: NotRequired[TaskResult | None]
    review_decision: NotRequired[PlanReviewDecision | None]
    handoff_context: NotRequired[str]
    executed_task_count: NotRequired[int]
    route: NotRequired[TaskType]
    response: NotRequired[str]
    candidate_itinerary: NotRequired[str | None]
    current_itinerary: NotRequired[str | None]
