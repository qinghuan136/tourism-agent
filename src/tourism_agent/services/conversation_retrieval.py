"""提供限定用户与 Trip 作用域的两阶段 Conversation 召回。"""

import logging
import math
from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from langchain_core.embeddings import Embeddings

from tourism_agent.models.context import ConversationMessage
from tourism_agent.models.rag import (
    ConversationChunkCandidate,
    ConversationChunkMatch,
    ConversationExchange,
)
from tourism_agent.services.conversation_chunk import EMBEDDING_DIMENSIONS
from tourism_agent.services.semantic_enhancement import SemanticEnhancementService

DEFAULT_SEARCH_LIMIT = 5
DEFAULT_CANDIDATE_LIMIT = 20
DEFAULT_RERANK_SCORE_THRESHOLD = 0.81
DEFAULT_DEDUP_SIMILARITY_THRESHOLD = 0.98

logger = logging.getLogger(__name__)


class ConversationRetrievalRepository(Protocol):
    """声明召回 Service 所需的最小数据库接口。"""

    async def search_conversation_chunks(
        self,
        user_id: UUID,
        trip_id: UUID,
        embedding: list[float],
        limit: int,
        exclude_exchange_ids: list[UUID],
    ) -> list[ConversationChunkCandidate]: ...

    async def get_conversation_exchanges(
        self,
        user_id: UUID,
        trip_id: UUID,
        exchange_ids: list[UUID],
    ) -> list[ConversationExchange]: ...


class ConversationReranker(Protocol):
    """声明 Conversation 召回所需的最小 Reranker 接口。"""

    async def rerank(self, *, query: str, documents: Sequence[str]) -> list[float]: ...


class ConversationRetrievalService:
    """先检索精简文本，需要时再读取选中 Exchange 的原始消息。"""

    def __init__(
        self,
        repository: ConversationRetrievalRepository,
        embeddings: Embeddings,
        enhancer: SemanticEnhancementService,
        reranker: ConversationReranker,
        *,
        candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
        score_threshold: float = DEFAULT_RERANK_SCORE_THRESHOLD,
        dedup_similarity_threshold: float = DEFAULT_DEDUP_SIMILARITY_THRESHOLD,
    ) -> None:
        self._repository = repository
        self._embeddings = embeddings
        self._enhancer = enhancer
        self._reranker = reranker
        self._candidate_limit = candidate_limit
        self._score_threshold = score_threshold
        self._dedup_similarity_threshold = dedup_similarity_threshold

    async def search(
        self,
        *,
        user_id: UUID,
        trip_id: UUID,
        query: str,
        limit: int = DEFAULT_SEARCH_LIMIT,
        exclude_exchange_ids: list[UUID] | None = None,
        current_user_input: str | None = None,
        task_goal: str | None = None,
        recent_conversation: Sequence[ConversationMessage] = (),
    ) -> list[ConversationChunkMatch]:
        """在可信用户和 Trip 作用域内返回最相近的检索文本。"""
        enhanced_query = await self._enhancer.enhance_query(
            query=query,
            current_user_input=current_user_input or query,
            task_goal=task_goal or query,
            recent_conversation=recent_conversation,
        )
        embedding = await self._embeddings.aembed_query(enhanced_query)
        if len(embedding) != EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"Embedding 维度必须为 {EMBEDDING_DIMENSIONS}，实际为 {len(embedding)}"
            )
        candidates = await self._repository.search_conversation_chunks(
            user_id,
            trip_id,
            embedding,
            max(limit, self._candidate_limit),
            exclude_exchange_ids or [],
        )
        if not candidates:
            return []

        scores = await self._reranker.rerank(
            query=enhanced_query,
            documents=[candidate.retrieval_text for candidate in candidates],
        )
        ranked_candidates = sorted(
            (
                (candidate, score)
                for candidate, score in zip(candidates, scores, strict=True)
                if score >= self._score_threshold
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        selected: list[tuple[ConversationChunkCandidate, float]] = []
        for candidate, score in ranked_candidates:
            if any(
                _cosine_similarity(candidate.embedding, kept.embedding)
                >= self._dedup_similarity_threshold
                for kept, _ in selected
            ):
                continue
            selected.append((candidate, score))
            if len(selected) == limit:
                break

        logger.info(
            "Conversation召回完成 candidates=%d threshold_passed=%d selected=%d",
            len(candidates),
            len(ranked_candidates),
            len(selected),
        )
        return [
            ConversationChunkMatch(
                exchange_id=candidate.exchange_id,
                retrieval_text=candidate.retrieval_text,
                similarity=candidate.similarity,
                created_at=candidate.created_at,
                rerank_score=score,
            )
            for candidate, score in selected
        ]

    async def read_exchanges(
        self,
        *,
        user_id: UUID,
        trip_id: UUID,
        exchange_ids: list[UUID],
    ) -> list[ConversationExchange]:
        """在同一可信作用域内读取选中 Exchange 的原始 Conversation。"""
        if not exchange_ids:
            return []
        return await self._repository.get_conversation_exchanges(
            user_id,
            trip_id,
            exchange_ids,
        )


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """计算两个已存 Chunk Embedding 的余弦相似度。"""
    dot_product = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot_product / (left_norm * right_norm)
