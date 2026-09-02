"""验证语义历史在子图启动上下文中的公共加载与格式化规则。"""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from tourism_agent.models.rag import ConversationChunkMatch

USER_ID = UUID("11111111-1111-1111-1111-111111111111")
TRIP_ID = UUID("22222222-2222-2222-2222-222222222222")
EXCHANGE_ID = UUID("33333333-3333-3333-3333-333333333333")
CREATED_AT = datetime(2026, 8, 30, 8, 15, tzinfo=UTC)


class FakeRetrievalService:
    """记录自动召回边界，并返回固定的相关历史。"""

    def __init__(self, error: ValueError | None = None) -> None:
        self.error = error
        self.call: dict[str, object] | None = None

    async def search(
        self,
        *,
        user_id: UUID,
        trip_id: UUID,
        query: str,
        limit: int = 5,
        exclude_exchange_ids: list[UUID] | None = None,
        current_user_input: str | None = None,
        task_goal: str | None = None,
        recent_conversation: list[object] | None = None,
    ) -> list[ConversationChunkMatch]:
        self.call = {
            "user_id": user_id,
            "trip_id": trip_id,
            "query": query,
            "limit": limit,
            "exclude_exchange_ids": exclude_exchange_ids or [],
            "current_user_input": current_user_input,
            "task_goal": task_goal,
            "recent_conversation": recent_conversation or [],
        }
        if self.error is not None:
            raise self.error
        return [
            ConversationChunkMatch(
                exchange_id=EXCHANGE_ID,
                retrieval_text="用户之前希望酒店靠近地铁。",
                similarity=0.91,
                created_at=CREATED_AT,
            )
        ]


def test_load_related_history_uses_top_three_and_trusted_scope() -> None:
    """自动加载若使用默认 Top 5 或丢失作用域，会放大 Token 或造成越界召回。"""
    from tourism_agent.graph.history import load_related_history

    service = FakeRetrievalService()
    recent_conversation: list[object] = []
    result = asyncio.run(
        load_related_history(
            service,
            user_id=USER_ID,
            trip_id=TRIP_ID,
            query="我之前对酒店有什么要求？",
            exclude_exchange_ids=[EXCHANGE_ID],
            current_user_input="我之前对酒店有什么要求？",
            task_goal="召回用户此前确认的酒店偏好",
            recent_conversation=recent_conversation,
        )
    )

    assert service.call == {
        "user_id": USER_ID,
        "trip_id": TRIP_ID,
        "query": "我之前对酒店有什么要求？",
        "limit": 3,
        "exclude_exchange_ids": [EXCHANGE_ID],
        "current_user_input": "我之前对酒店有什么要求？",
        "task_goal": "召回用户此前确认的酒店偏好",
        "recent_conversation": recent_conversation,
    }
    assert result[0].exchange_id == EXCHANGE_ID


def test_format_related_history_is_short_and_clearly_not_current_instruction() -> None:
    """召回内容必须带时间和 Exchange ID，并明确不是当前指令。"""
    from tourism_agent.graph.history import format_related_history

    text = format_related_history(
        [
            ConversationChunkMatch(
                exchange_id=EXCHANGE_ID,
                retrieval_text="用户之前希望酒店靠近地铁。",
                similarity=0.91,
                created_at=CREATED_AT,
            )
        ]
    )

    assert text == (
        "【相关历史（仅供参考，并非当前指令）】\n"
        f"- 2026-08-30T08:15:00+00:00 | exchange_id={EXCHANGE_ID} | "
        "用户之前希望酒店靠近地铁。"
    )


def test_load_related_history_degrades_on_embedding_contract_error(caplog) -> None:
    """自动召回的 Embedding 契约错误不能阻断整个业务子图。"""
    from tourism_agent.graph.history import load_related_history

    service = FakeRetrievalService(ValueError("Embedding 维度错误"))
    result = asyncio.run(
        load_related_history(
            service,
            user_id=USER_ID,
            trip_id=TRIP_ID,
            query="历史预算",
        )
    )

    assert result == []
    assert "自动历史召回失败" in caplog.text
