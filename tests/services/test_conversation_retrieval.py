"""验证 Conversation 两阶段召回 Service。"""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest

USER_ID = UUID("11111111-1111-1111-1111-111111111111")
TRIP_ID = UUID("22222222-2222-2222-2222-222222222222")
EXCHANGE_ID = UUID("33333333-3333-3333-3333-333333333333")
EXCLUDED_EXCHANGE_ID = UUID("44444444-4444-4444-4444-444444444444")
CHUNK_CREATED_AT = datetime(2026, 8, 30, 8, 15, tzinfo=UTC)
USER_CREATED_AT = datetime(2026, 8, 30, 8, 14, tzinfo=UTC)
ASSISTANT_CREATED_AT = datetime(2026, 8, 30, 8, 15, tzinfo=UTC)


class FakeEmbeddings:
    """用固定向量替代外部 Embedding API。"""

    def __init__(self, dimensions: int = 1024) -> None:
        self.dimensions = dimensions
        self.inputs: list[str] = []

    async def aembed_query(self, text: str) -> list[float]:
        self.inputs.append(text)
        return [0.5] * self.dimensions


class FakeRetrievalRepository:
    """记录 Service 传入数据库边界的可信作用域。"""

    def __init__(self, search_results: list[object], exchanges: list[object]) -> None:
        self.search_results = search_results
        self.exchanges = exchanges
        self.search_call: tuple[UUID, UUID, list[float], int, list[UUID]] | None = None
        self.read_call: tuple[UUID, UUID, list[UUID]] | None = None

    async def search_conversation_chunks(
        self,
        user_id: UUID,
        trip_id: UUID,
        embedding: list[float],
        limit: int,
        exclude_exchange_ids: list[UUID],
    ) -> list[object]:
        self.search_call = (
            user_id,
            trip_id,
            embedding,
            limit,
            exclude_exchange_ids,
        )
        return self.search_results

    async def get_conversation_exchanges(
        self,
        user_id: UUID,
        trip_id: UUID,
        exchange_ids: list[UUID],
    ) -> list[object]:
        self.read_call = (user_id, trip_id, exchange_ids)
        return self.exchanges


class FakeSemanticEnhancer:
    """返回固定增强查询，并记录查询上下文。"""

    def __init__(self, enhanced_query: str) -> None:
        self.enhanced_query = enhanced_query
        self.call: dict[str, object] | None = None

    async def enhance_query(self, **kwargs: object) -> str:
        self.call = kwargs
        return self.enhanced_query


class FakeReranker:
    """按候选输入顺序返回固定分数。"""

    def __init__(self, scores: list[float]) -> None:
        self.scores = scores
        self.call: tuple[str, list[str]] | None = None

    async def rerank(self, *, query: str, documents: list[str]) -> list[float]:
        self.call = (query, documents)
        return self.scores


def test_retrieval_service_reranks_filters_and_deduplicates_candidates() -> None:
    """检索结果应先按 Rerank 过滤排序，再保留语义重复项中的高分项。"""
    from tourism_agent.models.rag import ConversationChunkCandidate
    from tourism_agent.services.conversation_retrieval import (
        ConversationRetrievalService,
    )

    second_exchange_id = UUID("55555555-5555-5555-5555-555555555555")
    third_exchange_id = UUID("66666666-6666-6666-6666-666666666666")
    fourth_exchange_id = UUID("77777777-7777-7777-7777-777777777777")
    zero_tail = [0.0] * 1022
    candidates = [
        ConversationChunkCandidate(
            exchange_id=EXCHANGE_ID,
            retrieval_text="用户确认旅行预算为5000元。",
            similarity=0.92,
            created_at=CHUNK_CREATED_AT,
            embedding=[1.0, 0.0, *zero_tail],
        ),
        ConversationChunkCandidate(
            exchange_id=second_exchange_id,
            retrieval_text="用户要求总预算不超过6000元。",
            similarity=0.9,
            created_at=CHUNK_CREATED_AT,
            embedding=[0.999, 0.001, *zero_tail],
        ),
        ConversationChunkCandidate(
            exchange_id=third_exchange_id,
            retrieval_text="用户喜欢海边。",
            similarity=0.88,
            created_at=CHUNK_CREATED_AT,
            embedding=[0.0, 1.0, *zero_tail],
        ),
        ConversationChunkCandidate(
            exchange_id=fourth_exchange_id,
            retrieval_text="用户希望住在市中心。",
            similarity=0.82,
            created_at=CHUNK_CREATED_AT,
            embedding=[0.7, 0.7, *zero_tail],
        ),
    ]
    repository = FakeRetrievalRepository(candidates, [])
    reranker = FakeReranker([0.85, 0.93, 0.2, 0.8])
    service = ConversationRetrievalService(
        repository,
        FakeEmbeddings(),
        FakeSemanticEnhancer("查询用户已确认的旅行预算与住宿要求。"),
        reranker,
        candidate_limit=20,
        score_threshold=0.5,
        dedup_similarity_threshold=0.95,
    )

    result = asyncio.run(
        service.search(
            user_id=USER_ID,
            trip_id=TRIP_ID,
            query="我之前有什么要求？",
            limit=2,
        )
    )

    assert [match.exchange_id for match in result] == [
        second_exchange_id,
        fourth_exchange_id,
    ]
    assert [match.rerank_score for match in result] == [0.93, 0.8]
    assert [match.similarity for match in result] == [0.9, 0.82]
    assert repository.search_call == (
        USER_ID,
        TRIP_ID,
        [0.5] * 1024,
        20,
        [],
    )
    assert reranker.call == (
        "查询用户已确认的旅行预算与住宿要求。",
        [candidate.retrieval_text for candidate in candidates],
    )


def test_retrieval_service_searches_top_five_in_trusted_scope() -> None:
    """语义搜索必须把可信用户和 Trip 作用域传给 Repository。"""
    from tourism_agent.models.rag import (
        ConversationChunkCandidate,
        ConversationChunkMatch,
    )
    from tourism_agent.services.conversation_retrieval import (
        ConversationRetrievalService,
    )

    candidates = [
        ConversationChunkCandidate(
            exchange_id=EXCHANGE_ID,
            retrieval_text="用户希望预算控制在5000元。",
            similarity=0.91,
            created_at=CHUNK_CREATED_AT,
            embedding=[0.5] * 1024,
        )
    ]
    expected = [
        ConversationChunkMatch(
            exchange_id=EXCHANGE_ID,
            retrieval_text="用户希望预算控制在5000元。",
            similarity=0.91,
            created_at=CHUNK_CREATED_AT,
            rerank_score=0.87,
        )
    ]
    embeddings = FakeEmbeddings()
    repository = FakeRetrievalRepository(candidates, [])
    enhancer = FakeSemanticEnhancer("查询用户此前确认的旅行预算。")
    reranker = FakeReranker([0.87])
    service = ConversationRetrievalService(repository, embeddings, enhancer, reranker)
    history: list[object] = []

    result = asyncio.run(
        service.search(
            user_id=USER_ID,
            trip_id=TRIP_ID,
            query="之前说过的预算",
            current_user_input="我之前说过多少预算？",
            task_goal="召回用户此前确认的旅行预算",
            recent_conversation=history,
        )
    )

    assert result == expected
    assert embeddings.inputs == ["查询用户此前确认的旅行预算。"]
    assert enhancer.call == {
        "query": "之前说过的预算",
        "current_user_input": "我之前说过多少预算？",
        "task_goal": "召回用户此前确认的旅行预算",
        "recent_conversation": history,
    }
    assert repository.search_call == (
        USER_ID,
        TRIP_ID,
        [0.5] * 1024,
        20,
        [],
    )
    assert reranker.call == (
        "查询用户此前确认的旅行预算。",
        ["用户希望预算控制在5000元。"],
    )


def test_retrieval_service_accepts_smaller_limit_for_automatic_loading() -> None:
    """自动加载必须能够覆盖默认 Top 5，而不影响 Tool 的默认行为。"""
    from tourism_agent.services.conversation_retrieval import (
        ConversationRetrievalService,
    )

    repository = FakeRetrievalRepository([], [])
    service = ConversationRetrievalService(
        repository,
        FakeEmbeddings(),
        FakeSemanticEnhancer("增强后的历史预算查询"),
        FakeReranker([]),
    )

    asyncio.run(
        service.search(
            user_id=USER_ID,
            trip_id=TRIP_ID,
            query="历史预算",
            limit=3,
            exclude_exchange_ids=[EXCLUDED_EXCHANGE_ID],
        )
    )

    assert repository.search_call == (
        USER_ID,
        TRIP_ID,
        [0.5] * 1024,
        20,
        [EXCLUDED_EXCHANGE_ID],
    )


def test_retrieval_service_reads_exact_exchanges_without_embedding() -> None:
    """读取原始 Conversation 只依赖 Exchange ID，不应再次生成向量。"""
    from tourism_agent.models.rag import ConversationExchange
    from tourism_agent.services.conversation_retrieval import (
        ConversationRetrievalService,
    )

    expected = [
        ConversationExchange(
            exchange_id=EXCHANGE_ID,
            user_message="预算控制在5000元。",
            assistant_message="我会按5000元规划。",
            user_created_at=USER_CREATED_AT,
            assistant_created_at=ASSISTANT_CREATED_AT,
        )
    ]
    embeddings = FakeEmbeddings()
    repository = FakeRetrievalRepository([], expected)
    service = ConversationRetrievalService(
        repository,
        embeddings,
        FakeSemanticEnhancer("不会用于原文读取"),
        FakeReranker([]),
    )

    result = asyncio.run(
        service.read_exchanges(
            user_id=USER_ID,
            trip_id=TRIP_ID,
            exchange_ids=[EXCHANGE_ID],
        )
    )

    assert result == expected
    assert embeddings.inputs == []
    assert repository.read_call == (USER_ID, TRIP_ID, [EXCHANGE_ID])


def test_retrieval_service_rejects_wrong_embedding_dimensions() -> None:
    """查询向量不是固定 1024 维时不得访问数据库。"""
    from tourism_agent.services.conversation_retrieval import (
        ConversationRetrievalService,
    )

    repository = FakeRetrievalRepository([], [])
    service = ConversationRetrievalService(
        repository,
        FakeEmbeddings(dimensions=3),
        FakeSemanticEnhancer("增强后的历史预算查询"),
        FakeReranker([]),
    )

    with pytest.raises(ValueError, match="1024"):
        asyncio.run(
            service.search(
                user_id=USER_ID,
                trip_id=TRIP_ID,
                query="历史预算",
            )
        )

    assert repository.search_call is None
