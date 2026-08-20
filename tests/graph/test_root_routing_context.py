"""验证根图使用少量历史对话理解当前待路由消息。"""

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableLambda

from tourism_agent.graph.root import build_root_graph
from tourism_agent.models.context import ConversationMessage, ConversationRole
from tourism_agent.models.contracts import IntentDecision, RouteTarget

USER_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TRIP_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


class RoutingContextRepository:
    """返回当前消息之前的历史，并记录根图使用的查询边界。"""

    def __init__(self) -> None:
        self.recent_query: tuple[UUID, int, int] | None = None

    async def get_recent_conversation(
        self,
        trip_id: UUID,
        *,
        before_message_id: int,
        limit: int,
    ) -> list[ConversationMessage]:
        self.recent_query = (trip_id, before_message_id, limit)
        created_at = datetime(2026, 8, 19, tzinfo=UTC)
        return [
            ConversationMessage(
                id=38,
                role=ConversationRole.USER,
                content="给我一些适合放空的海岛旅行灵感",
                created_at=created_at,
            ),
            ConversationMessage(
                id=39,
                role=ConversationRole.ASSISTANT,
                content="可以考虑涠洲岛或东山岛。",
                created_at=created_at,
            ),
        ]


class BoundaryAwareRoutingModel:
    """只有明确识别历史区和当前消息区时才采用历史语义。"""

    def with_structured_output(self, schema: type[IntentDecision]) -> RunnableLambda:
        def decide(messages: list[BaseMessage]) -> Any:
            system_prompt = str(messages[0].content)
            history = messages[1:-1]
            current = str(messages[-1].content)
            boundaries_are_clear = (
                "历史消息" in system_prompt
                and "当前消息" in system_prompt
                and "只为" in system_prompt
                and all(str(message.content).startswith("【历史消息】") for message in history)
                and current.startswith("【当前消息】")
            )
            has_inspiration_context = any(
                "海岛旅行灵感" in str(message.content) for message in history
            )
            route = (
                RouteTarget.INSPIRATION
                if boundaries_are_clear and has_inspiration_context and "就按这个" in current
                else RouteTarget.UNSUPPORTED
            )
            return schema(route=route)

        return RunnableLambda(decide)

    def bind_tools(self, _tools: list[object]) -> RunnableLambda:
        return RunnableLambda(lambda _messages: AIMessage(content="未进入 Planning"))


def test_root_routes_current_message_with_clearly_labeled_recent_history() -> None:
    """根图必须查询当前消息之前四条记录，并明确标注历史与当前消息。"""
    repository = RoutingContextRepository()
    graph = build_root_graph(BoundaryAwareRoutingModel(), repository)

    result = asyncio.run(
        graph.ainvoke(
            {
                "user_id": USER_ID,
                "trip_id": TRIP_ID,
                "user_message_id": 40,
                "user_input": "就按这个继续",
            },
            {"configurable": {"thread_id": str(TRIP_ID)}},
        )
    )

    assert repository.recent_query == (TRIP_ID, 40, 4)
    assert result["route"] is RouteTarget.INSPIRATION
