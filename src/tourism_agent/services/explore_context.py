"""组装 Explore 子图单次运行需要的只读业务上下文。"""

from typing import Any
from uuid import UUID

from pydantic import BaseModel

from tourism_agent.models.context import ConversationMessage
from tourism_agent.repositories.planning import PlanningRepository

RECENT_CONVERSATION_LIMIT = 8


class ExploreContextSnapshot(BaseModel):
    """从数据库加载到本轮 ExploreState 的权威信息快照。"""

    conversation_context: list[ConversationMessage]
    trip_context: dict[str, Any]
    current_itinerary: str | None


class ExploreContextBuilder:
    """只读取 Explore 所需信息，不承担任何业务数据写入。"""

    def __init__(self, repository: PlanningRepository) -> None:
        self._repository = repository

    async def build(
        self,
        trip_id: UUID,
        before_message_id: int,
    ) -> ExploreContextSnapshot:
        """加载当前消息之前的近期对话和完整旅行快照。"""
        conversation = await self._repository.get_recent_conversation(
            trip_id,
            before_message_id=before_message_id,
            limit=RECENT_CONVERSATION_LIMIT,
        )
        trip_context = await self._repository.get_trip_context(trip_id)
        current_itinerary = await self._repository.get_current_itinerary(trip_id)
        return ExploreContextSnapshot(
            conversation_context=conversation,
            trip_context=trip_context,
            current_itinerary=current_itinerary,
        )
