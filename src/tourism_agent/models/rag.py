"""定义 Conversation RAG 持久化边界使用的数据模型。"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ConversationChunkDraft:
    """一段已经完成向量化、等待写入数据库的 Conversation Chunk。"""

    trip_id: UUID
    exchange_id: UUID
    user_message_id: int
    assistant_message_id: int
    retrieval_text: str
    retrieval_text_sha256: str
    source_token_count: int
    retrieval_token_count: int
    enhancement_model: str
    enhancement_version: int
    embedding_model: str
    embedding: list[float]


@dataclass(frozen=True, slots=True)
class ConversationChunkCandidate:
    """数据库初步召回的候选 Chunk，仅在检索 Service 内部使用。"""

    exchange_id: UUID
    retrieval_text: str
    similarity: float
    created_at: datetime
    embedding: list[float]


@dataclass(frozen=True, slots=True)
class ConversationChunkMatch:
    """经过 Rerank 和语义去重后返回的派生检索文本。"""

    exchange_id: UUID
    retrieval_text: str
    similarity: float
    created_at: datetime
    rerank_score: float = 0.0


@dataclass(frozen=True, slots=True)
class ConversationExchange:
    """根据 Exchange ID 读取的一组原始用户与 Assistant 消息。"""

    exchange_id: UUID
    user_message: str
    assistant_message: str
    user_created_at: datetime
    assistant_created_at: datetime
