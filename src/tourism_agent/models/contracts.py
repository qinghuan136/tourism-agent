"""定义 API、Orchestrator 根图与消息接口共享的最小数据契约。"""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, StringConstraints

from tourism_agent.models.context import ConversationMessage
from tourism_agent.models.orchestration import TaskType

NormalizedText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

# 用户消息在进入图之前限制长度，避免单次请求过度占用模型上下文。
UserMessageText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4000),
]


class MessageRequest(BaseModel):
    """API 接收到的单条用户消息及其业务作用域。"""

    user_id: UUID
    trip_id: UUID
    idempotency_id: UUID
    message: UserMessageText


class MessageResponse(BaseModel):
    """分别返回简短对话内容、候选方案和已确认行程。"""

    route: TaskType
    message: NormalizedText
    candidate_itinerary: NormalizedText | None = None
    current_itinerary: NormalizedText | None = None


class IdempotencyProcessingResponse(BaseModel):
    """表示同一幂等请求仍由另一个 HTTP 调用处理。"""

    idempotency_id: UUID
    status: Literal["processing"] = "processing"


class CancelRunRequest(BaseModel):
    """取消某次旅行当前图运行所需的用户作用域。"""

    user_id: UUID


class CancelRunResponse(BaseModel):
    """表示执行中任务或待恢复 checkpoint 是否被取消。"""

    cancelled: bool


class ConversationPage(BaseModel):
    """返回一页按时间正序排列的原始 Conversation。"""

    items: list[ConversationMessage]
    next_before_id: int | None
    has_more: bool


class TripBootstrapResponse(BaseModel):
    """返回进入 Trip 页面所需的首屏只读数据。"""

    trip_id: UUID
    conversations: ConversationPage
    current_itinerary: NormalizedText | None = None
