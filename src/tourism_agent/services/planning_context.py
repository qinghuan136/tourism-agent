"""组装 Planning 子图单次运行需要的业务上下文快照。"""

from typing import Any
from uuid import UUID

from pydantic import BaseModel

from tourism_agent.models.context import ConversationMessage
from tourism_agent.repositories.planning import PlanningRepository

RECENT_CONVERSATION_LIMIT = 8


class PlanningContextSnapshot(BaseModel):
    """从长期事实来源加载到本轮 PlanningState 的只读起始快照。"""

    conversation_context: list[ConversationMessage]
    trip_context: dict[str, Any]
    current_itinerary: str | None


class PlanningContextBuilder:
    """按 Planning 子图的需要读取上下文，避免根图承载业务细节。"""

    def __init__(self, repository: PlanningRepository) -> None:
        self._repository = repository

    async def build(
        self,
        trip_id: UUID,
        before_message_id: int,
    ) -> PlanningContextSnapshot:
        """加载当前请求之前的近期对话及三个权威业务快照。"""
        conversation = await self._repository.get_recent_conversation(
            trip_id,
            before_message_id=before_message_id,
            limit=RECENT_CONVERSATION_LIMIT,
        )
        trip_context = await self._repository.get_trip_context(trip_id)
        current_itinerary = await self._repository.get_current_itinerary(trip_id)
        return PlanningContextSnapshot(
            conversation_context=conversation,
            trip_context=trip_context,
            current_itinerary=current_itinerary,
        )
