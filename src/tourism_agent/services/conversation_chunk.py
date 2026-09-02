"""把一次用户请求与一条可见回答转换成可检索 Chunk。"""

import hashlib
import re
from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from langchain_core.embeddings import Embeddings

from tourism_agent.models.context import ConversationMessage
from tourism_agent.models.rag import ConversationChunkDraft
from tourism_agent.services.semantic_enhancement import SemanticEnhancementService

EMBEDDING_MODEL = "qwen3.7-text-embedding"
EMBEDDING_DIMENSIONS = 1024


class ConversationChunkRepository(Protocol):
    """声明 Chunk Service 所需的最小持久化接口。"""

    async def save_conversation_chunk(self, chunk: ConversationChunkDraft) -> None: ...


class ConversationChunkService:
    """生成原始 Exchange 文本、向量，并交给 Repository 原子写入。"""

    def __init__(
        self,
        repository: ConversationChunkRepository,
        embeddings: Embeddings,
        enhancer: SemanticEnhancementService,
    ) -> None:
        self._repository = repository
        self._embeddings = embeddings
        self._enhancer = enhancer

    async def submit(
        self,
        *,
        trip_id: UUID,
        exchange_id: UUID,
        user_message: ConversationMessage,
        assistant_message: ConversationMessage,
        context_goal: str,
        recent_conversation: Sequence[ConversationMessage],
    ) -> None:
        """增强一问一答的检索语义并提交派生 Chunk。"""
        source_text = (
            f"用户：{user_message.content}\n"
            f"助手：{assistant_message.content}"
        )
        retrieval_text = await self._enhancer.enhance_exchange(
            user_message=user_message.content,
            assistant_message=assistant_message.content,
            context_goal=context_goal,
            recent_conversation=recent_conversation,
        )
        embedding = await self._embeddings.aembed_query(retrieval_text)
        if len(embedding) != EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"Embedding 维度必须为 {EMBEDDING_DIMENSIONS}，实际为 {len(embedding)}"
            )

        chunk = ConversationChunkDraft(
            trip_id=trip_id,
            exchange_id=exchange_id,
            user_message_id=user_message.id,
            assistant_message_id=assistant_message.id,
            retrieval_text=retrieval_text,
            retrieval_text_sha256=hashlib.sha256(
                retrieval_text.encode("utf-8")
            ).hexdigest(),
            source_token_count=_estimate_token_count(source_text),
            retrieval_token_count=_estimate_token_count(retrieval_text),
            enhancement_model=self._enhancer.model_name,
            enhancement_version=1,
            embedding_model=EMBEDDING_MODEL,
            embedding=embedding,
        )
        await self._repository.save_conversation_chunk(chunk)


def _estimate_token_count(text: str) -> int:
    """在尚未接入模型 Tokenizer 时，近似统计中英文 Token 数供调试。"""
    parts = re.findall(r"[\u4e00-\u9fff]|[A-Za-z0-9_]+|[^\s]", text)
    return max(1, len(parts))
