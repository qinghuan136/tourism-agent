"""验证 Conversation Chunk 的构造与提交规则。"""

import asyncio
import hashlib
from datetime import UTC, datetime
from uuid import UUID

import pytest

from tourism_agent.models.context import ConversationMessage, ConversationRole

TRIP_ID = UUID("11111111-1111-1111-1111-111111111111")
EXCHANGE_ID = UUID("22222222-2222-2222-2222-222222222222")


class FakeEmbeddings:
    """用固定向量替代外部 Embedding 服务。"""

    def __init__(self, dimensions: int = 1024) -> None:
        self.dimensions = dimensions
        self.inputs: list[str] = []

    async def aembed_query(self, text: str) -> list[float]:
        self.inputs.append(text)
        return [0.25] * self.dimensions


class FakeChunkRepository:
    """记录 Service 交给持久化边界的 Chunk。"""

    def __init__(self) -> None:
        self.chunks: list[object] = []

    async def save_conversation_chunk(self, chunk: object) -> None:
        self.chunks.append(chunk)


class FakeSemanticEnhancer:
    """返回固定增强文本，并记录 Chunk 上下文。"""

    model_name = "demo-chat-model"

    def __init__(self, retrieval_text: str) -> None:
        self.retrieval_text = retrieval_text
        self.call: dict[str, object] | None = None

    async def enhance_exchange(self, **kwargs: object) -> str:
        self.call = kwargs
        return self.retrieval_text


def message(
    message_id: int,
    role: ConversationRole,
    content: str,
) -> ConversationMessage:
    """构造一条已持久化的原始 Conversation。"""
    return ConversationMessage(
        id=message_id,
        role=role,
        content=content,
        created_at=datetime(2026, 8, 30, tzinfo=UTC),
    )


def test_chunk_service_embeds_and_saves_only_enhanced_retrieval_text() -> None:
    """把原始 Exchange 写进 Chunk 会破坏原文与派生索引的分层。"""
    from tourism_agent.services.conversation_chunk import ConversationChunkService

    embeddings = FakeEmbeddings()
    repository = FakeChunkRepository()
    enhancer = FakeSemanticEnhancer("用户寻找广州亲子地点，推荐长隆和广东科学中心。")
    service = ConversationChunkService(repository, embeddings, enhancer)
    user_message = message(10, ConversationRole.USER, "广州有哪些适合亲子的地方？")
    assistant_message = message(11, ConversationRole.ASSISTANT, "可以考虑长隆和广东科学中心。")
    history = [message(8, ConversationRole.ASSISTANT, "用户计划带孩子去广州。")]

    asyncio.run(
        service.submit(
            trip_id=TRIP_ID,
            exchange_id=EXCHANGE_ID,
            user_message=user_message,
            assistant_message=assistant_message,
            context_goal="为用户推荐广州亲子旅行地点",
            recent_conversation=history,
        )
    )

    expected_text = "用户寻找广州亲子地点，推荐长隆和广东科学中心。"
    assert embeddings.inputs == [expected_text]
    assert enhancer.call == {
        "user_message": user_message.content,
        "assistant_message": assistant_message.content,
        "context_goal": "为用户推荐广州亲子旅行地点",
        "recent_conversation": history,
    }
    assert len(repository.chunks) == 1
    chunk = repository.chunks[0]
    assert chunk.trip_id == TRIP_ID
    assert chunk.exchange_id == EXCHANGE_ID
    assert chunk.user_message_id == 10
    assert chunk.assistant_message_id == 11
    assert chunk.retrieval_text == expected_text
    assert chunk.retrieval_text_sha256 == hashlib.sha256(
        expected_text.encode("utf-8")
    ).hexdigest()
    assert chunk.source_token_count > 0
    assert chunk.retrieval_token_count > 0
    assert chunk.source_token_count != chunk.retrieval_token_count
    assert chunk.enhancement_model == "demo-chat-model"
    assert chunk.enhancement_version == 1
    assert chunk.embedding_model == "qwen3.7-text-embedding"
    assert chunk.embedding == [0.25] * 1024


def test_chunk_service_rejects_embedding_with_wrong_dimensions() -> None:
    """供应商返回非 1024 维向量时不得写入不兼容 Chunk。"""
    from tourism_agent.services.conversation_chunk import ConversationChunkService

    repository = FakeChunkRepository()
    service = ConversationChunkService(
        repository,
        FakeEmbeddings(dimensions=3),
        FakeSemanticEnhancer("增强后的用户问题与助手回答"),
    )

    with pytest.raises(ValueError, match="1024"):
        asyncio.run(
            service.submit(
                trip_id=TRIP_ID,
                exchange_id=EXCHANGE_ID,
                user_message=message(10, ConversationRole.USER, "用户问题"),
                assistant_message=message(11, ConversationRole.ASSISTANT, "助手回答"),
                context_goal="回答用户问题",
                recent_conversation=[],
            )
        )

    assert repository.chunks == []
