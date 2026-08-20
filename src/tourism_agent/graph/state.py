"""定义根图执行理解、路由和模块调用所需的最小状态。"""

from typing import NotRequired, TypedDict
from uuid import UUID

from tourism_agent.models.context import ConversationMessage
from tourism_agent.models.contracts import RouteTarget


class RootState(TypedDict):
    """保存单次根图调用中理解、路由和返回所需的数据。"""

    user_id: UUID
    trip_id: UUID
    user_message_id: int
    user_input: str
    routing_context: NotRequired[list[ConversationMessage]]
    route: NotRequired[RouteTarget]
    response: NotRequired[str]
    candidate_itinerary: NotRequired[str | None]
    current_itinerary: NotRequired[str | None]
