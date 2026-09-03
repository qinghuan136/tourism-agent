"""验证 Trip 页面初始化与 Conversation 游标分页接口。"""

from datetime import UTC, datetime
from importlib import import_module
from uuid import UUID

from fastapi.testclient import TestClient

from tourism_agent.models.context import ConversationMessage, ConversationRole

USER_ID = UUID("11111111-1111-1111-1111-111111111111")
TRIP_ID = UUID("22222222-2222-2222-2222-222222222222")


def conversation(message_id: int, role: ConversationRole, content: str) -> ConversationMessage:
    """构造按 ID 递增的可见消息。"""
    return ConversationMessage(
        id=message_id,
        role=role,
        content=content,
        created_at=datetime(2026, 9, 2, 10, message_id, tzinfo=UTC),
    )


class TripReadFakeRepository:
    """只模拟两个读取接口依赖的数据库边界。"""

    def __init__(self, *, belongs: bool = True) -> None:
        self.belongs = belongs
        self.messages = [
            conversation(1, ConversationRole.USER, "最早的用户消息"),
            conversation(2, ConversationRole.ASSISTANT, "最早的助手回答"),
            conversation(3, ConversationRole.USER, "最近的用户消息"),
            conversation(4, ConversationRole.ASSISTANT, "最近的助手回答"),
        ]

    async def trip_belongs_to_user(self, user_id: UUID, trip_id: UUID) -> bool:
        assert user_id == USER_ID
        assert trip_id == TRIP_ID
        return self.belongs

    async def get_conversation_page(
        self,
        trip_id: UUID,
        *,
        before_message_id: int | None,
        limit: int,
    ) -> list[ConversationMessage]:
        assert trip_id == TRIP_ID
        eligible = [
            message
            for message in self.messages
            if before_message_id is None or message.id < before_message_id
        ]
        return eligible[-limit:]

    async def get_current_itinerary(self, trip_id: UUID) -> str | None:
        assert trip_id == TRIP_ID
        return "广州两日已确认行程"


def override_repository(api: object, repository: TripReadFakeRepository) -> None:
    """替换读取接口的外部数据库依赖。"""
    api.app.dependency_overrides[api.get_planning_repository] = lambda: repository


def test_bootstrap_returns_latest_conversation_page_and_current_itinerary() -> None:
    """首屏接口必须返回最新消息、翻页游标和当前已确认行程。"""
    api = import_module("tourism_agent.api")
    override_repository(api, TripReadFakeRepository())
    try:
        response = TestClient(api.app).get(
            f"/trips/{TRIP_ID}/bootstrap",
            params={"user_id": str(USER_ID), "message_limit": 2},
        )
    finally:
        api.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "trip_id": str(TRIP_ID),
        "conversations": {
            "items": [
                {
                    "id": 3,
                    "role": "user",
                    "content": "最近的用户消息",
                    "created_at": "2026-09-02T10:03:00Z",
                    "exchange_id": None,
                },
                {
                    "id": 4,
                    "role": "assistant",
                    "content": "最近的助手回答",
                    "created_at": "2026-09-02T10:04:00Z",
                    "exchange_id": None,
                },
            ],
            "next_before_id": 3,
            "has_more": True,
        },
        "current_itinerary": "广州两日已确认行程",
    }


def test_conversations_returns_messages_before_cursor_in_chronological_order() -> None:
    """向上翻页必须排除游标消息，并保持页面内部时间正序。"""
    api = import_module("tourism_agent.api")
    override_repository(api, TripReadFakeRepository())
    try:
        response = TestClient(api.app).get(
            f"/trips/{TRIP_ID}/conversations",
            params={
                "user_id": str(USER_ID),
                "before_id": 3,
                "limit": 2,
            },
        )
    finally:
        api.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "id": 1,
                "role": "user",
                "content": "最早的用户消息",
                "created_at": "2026-09-02T10:01:00Z",
                "exchange_id": None,
            },
            {
                "id": 2,
                "role": "assistant",
                "content": "最早的助手回答",
                "created_at": "2026-09-02T10:02:00Z",
                "exchange_id": None,
            },
        ],
        "next_before_id": None,
        "has_more": False,
    }


def test_trip_read_endpoints_hide_trips_outside_user_scope() -> None:
    """两个读取接口都不能泄露其他用户的 Trip 数据。"""
    api = import_module("tourism_agent.api")
    override_repository(api, TripReadFakeRepository(belongs=False))
    client = TestClient(api.app)
    try:
        bootstrap = client.get(
            f"/trips/{TRIP_ID}/bootstrap",
            params={"user_id": str(USER_ID)},
        )
        conversations = client.get(
            f"/trips/{TRIP_ID}/conversations",
            params={"user_id": str(USER_ID)},
        )
    finally:
        api.app.dependency_overrides.clear()

    assert bootstrap.status_code == 404
    assert bootstrap.json() == {"detail": "未找到当前用户对应的旅行"}
    assert conversations.status_code == 404
    assert conversations.json() == {"detail": "未找到当前用户对应的旅行"}
