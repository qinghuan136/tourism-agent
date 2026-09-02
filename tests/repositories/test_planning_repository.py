"""验证 PlanningRepository 与真实 PostgreSQL 表的映射。"""

import asyncio
import hashlib
import os
from collections.abc import Awaitable
from uuid import UUID, uuid4

import pytest

from tourism_agent.infrastructure.database import DatabaseSettings, PostgresDatabase

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="仅在显式启用 PostgreSQL 集成测试时运行",
)


def run_async[T](awaitable: Awaitable[T]) -> T:
    """在 Windows 上使用 psycopg 支持的 Selector 事件循环。"""
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(awaitable)


async def seed_scope(
    database: PostgresDatabase,
    user_id: UUID,
    trip_id: UUID,
    itinerary: str | None = None,
) -> None:
    """创建单个测试拥有的用户和旅行作用域。"""
    async with database.connection() as connection:
        await connection.execute(
            "INSERT INTO tourism_agent.users (id) VALUES (%s)",
            (user_id,),
        )
        await connection.execute(
            "INSERT INTO tourism_agent.trips (id, user_id) VALUES (%s, %s)",
            (trip_id, user_id),
        )
        if itinerary is not None:
            await connection.execute(
                """
                INSERT INTO tourism_agent.current_itineraries (trip_id, content)
                VALUES (%s, %s)
                """,
                (trip_id, itinerary),
            )


async def delete_scope(database: PostgresDatabase, user_id: UUID) -> None:
    """依赖外键级联规则尚未启用，因此按从属关系清理测试数据。"""
    async with database.connection() as connection:
        await connection.execute(
            """
            DELETE FROM tourism_agent.idempotency_requests
            WHERE trip_id IN (SELECT id FROM tourism_agent.trips WHERE user_id = %s)
            """,
            (user_id,),
        )
        await connection.execute(
            """
            DELETE FROM tourism_agent.current_itineraries
            WHERE trip_id IN (SELECT id FROM tourism_agent.trips WHERE user_id = %s)
            """,
            (user_id,),
        )
        await connection.execute(
            """
            DELETE FROM tourism_agent.conversation_messages
            WHERE trip_id IN (SELECT id FROM tourism_agent.trips WHERE user_id = %s)
            """,
            (user_id,),
        )
        await connection.execute(
            """
            DELETE FROM tourism_agent.trip_contexts
            WHERE trip_id IN (SELECT id FROM tourism_agent.trips WHERE user_id = %s)
            """,
            (user_id,),
        )
        await connection.execute(
            "DELETE FROM tourism_agent.trips WHERE user_id = %s",
            (user_id,),
        )
        await connection.execute(
            "DELETE FROM tourism_agent.users WHERE id = %s",
            (user_id,),
        )


def test_recent_conversation_excludes_current_message_and_respects_limit() -> None:
    """当前用户消息已单独传入图，预加载历史不能把它再次加入上下文。"""

    async def scenario() -> None:
        from tourism_agent.models.context import ConversationRole
        from tourism_agent.repositories.planning import PlanningRepository

        user_id = uuid4()
        trip_id = uuid4()
        database = PostgresDatabase(DatabaseSettings())
        await database.open()
        try:
            await seed_scope(database, user_id, trip_id)
            repository = PlanningRepository(database)
            await repository.append_conversation(trip_id, ConversationRole.USER, "第一条")
            second = await repository.append_conversation(
                trip_id, ConversationRole.ASSISTANT, "第二条"
            )
            third = await repository.append_conversation(trip_id, ConversationRole.USER, "第三条")
            current = await repository.append_conversation(
                trip_id, ConversationRole.USER, "当前请求"
            )

            messages = await repository.get_recent_conversation(
                trip_id,
                before_message_id=current.id,
                limit=2,
            )

            assert [message.id for message in messages] == [second.id, third.id]
            assert [message.content for message in messages] == ["第二条", "第三条"]
        finally:
            await delete_scope(database, user_id)
            await database.close()

    run_async(scenario())


def test_repository_persists_trip_context_and_reads_current_itinerary() -> None:
    """TripContext 保持动态 JSONB，CurrentItinerary 能按旅行作用域读取。"""

    async def scenario() -> None:
        from tourism_agent.repositories.planning import PlanningRepository

        user_id = uuid4()
        other_user_id = uuid4()
        trip_id = uuid4()
        database = PostgresDatabase(DatabaseSettings())
        await database.open()
        try:
            await seed_scope(database, user_id, trip_id, "已确认的杭州三日行程")
            repository = PlanningRepository(database)

            assert await repository.trip_belongs_to_user(user_id, trip_id) is True
            assert await repository.trip_belongs_to_user(other_user_id, trip_id) is False

            assert await repository.patch_trip_context(
                trip_id, {"预算": "5000元", "同行人": "父母"}
            ) == {"预算": "5000元", "同行人": "父母"}
            assert await repository.delete_trip_context_keys(trip_id, ["同行人"]) == {
                "预算": "5000元"
            }

            assert await repository.get_trip_context(trip_id) == {"预算": "5000元"}
            assert await repository.get_current_itinerary(trip_id) == "已确认的杭州三日行程"
        finally:
            await delete_scope(database, user_id)
            await database.close()

    run_async(scenario())


def test_repository_upserts_current_itinerary() -> None:
    """已确认行程应写入当前版本，并允许后续确认覆盖同一版本。"""

    async def scenario() -> None:
        from tourism_agent.repositories.planning import PlanningRepository

        user_id = uuid4()
        trip_id = uuid4()
        database = PostgresDatabase(DatabaseSettings())
        await database.open()
        try:
            await seed_scope(database, user_id, trip_id)
            repository = PlanningRepository(database)

            first = await repository.write_current_itinerary(trip_id, "杭州三日确认方案")
            second = await repository.write_current_itinerary(trip_id, "杭州四日确认方案")

            assert first == "杭州三日确认方案"
            assert second == "杭州四日确认方案"
            assert await repository.get_current_itinerary(trip_id) == "杭州四日确认方案"
        finally:
            await delete_scope(database, user_id)
            await database.close()

    run_async(scenario())


def test_repository_atomically_links_exchange_and_saves_conversation_chunk() -> None:
    """Chunk 入库时应同时把两条原始消息绑定到同一 Exchange。"""

    async def scenario() -> None:
        from tourism_agent.models.context import ConversationRole
        from tourism_agent.models.rag import ConversationChunkDraft
        from tourism_agent.repositories.planning import PlanningRepository

        user_id = uuid4()
        trip_id = uuid4()
        exchange_id = uuid4()
        retrieval_text = "用户：广州亲子游去哪？\n助手：可以考虑长隆。"
        database = PostgresDatabase(DatabaseSettings())
        await database.open()
        try:
            await seed_scope(database, user_id, trip_id)
            repository = PlanningRepository(database)
            user_message = await repository.append_conversation(
                trip_id,
                ConversationRole.USER,
                "广州亲子游去哪？",
            )
            assistant_message = await repository.append_conversation(
                trip_id,
                ConversationRole.ASSISTANT,
                "可以考虑长隆。",
            )

            await repository.save_conversation_chunk(
                ConversationChunkDraft(
                    trip_id=trip_id,
                    exchange_id=exchange_id,
                    user_message_id=user_message.id,
                    assistant_message_id=assistant_message.id,
                    retrieval_text=retrieval_text,
                    retrieval_text_sha256=hashlib.sha256(
                        retrieval_text.encode("utf-8")
                    ).hexdigest(),
                    source_token_count=21,
                    retrieval_token_count=21,
                    enhancement_model="none",
                    enhancement_version=1,
                    embedding_model="qwen3.7-text-embedding",
                    embedding=[0.25] * 1024,
                )
            )

            async with database.connection() as connection:
                message_cursor = await connection.execute(
                    """
                    SELECT id, exchange_id
                    FROM tourism_agent.conversation_messages
                    WHERE id IN (%s, %s)
                    ORDER BY id
                    """,
                    (user_message.id, assistant_message.id),
                )
                chunk_cursor = await connection.execute(
                    """
                    SELECT exchange_id, user_message_id, assistant_message_id,
                           retrieval_text, enhancement_model, embedding_model,
                           vector_dims(embedding) AS dimensions
                    FROM tourism_agent.conversation_rag_chunks
                    WHERE trip_id = %s AND exchange_id = %s
                    """,
                    (trip_id, exchange_id),
                )
                message_rows = await message_cursor.fetchall()
                chunk_row = await chunk_cursor.fetchone()

            assert [row["exchange_id"] for row in message_rows] == [
                exchange_id,
                exchange_id,
            ]
            recent = await repository.get_recent_conversation(
                trip_id,
                before_message_id=assistant_message.id + 1,
                limit=8,
            )
            assert [message.exchange_id for message in recent] == [
                exchange_id,
                exchange_id,
            ]
            assert chunk_row == {
                "exchange_id": exchange_id,
                "user_message_id": user_message.id,
                "assistant_message_id": assistant_message.id,
                "retrieval_text": retrieval_text,
                "enhancement_model": "none",
                "embedding_model": "qwen3.7-text-embedding",
                "dimensions": 1024,
            }
        finally:
            await delete_scope(database, user_id)
            await database.close()

    run_async(scenario())


def test_conversation_retrieval_filters_user_and_trip_before_vector_ranking() -> None:
    """更相似的跨作用域 Chunk 也不得进入搜索结果或原文读取结果。"""

    async def scenario() -> None:
        from tourism_agent.models.context import ConversationRole
        from tourism_agent.models.rag import ConversationChunkDraft
        from tourism_agent.repositories.planning import PlanningRepository

        user_id = uuid4()
        other_user_id = uuid4()
        trip_id = uuid4()
        same_user_other_trip_id = uuid4()
        other_user_trip_id = uuid4()
        database = PostgresDatabase(DatabaseSettings())
        await database.open()

        async def persist_exchange(
            repository: PlanningRepository,
            scope_trip_id: UUID,
            exchange_id: UUID,
            user_text: str,
            assistant_text: str,
            embedding: list[float],
        ) -> None:
            user_message = await repository.append_conversation(
                scope_trip_id,
                ConversationRole.USER,
                user_text,
            )
            assistant_message = await repository.append_conversation(
                scope_trip_id,
                ConversationRole.ASSISTANT,
                assistant_text,
            )
            retrieval_text = f"用户：{user_text}\n助手：{assistant_text}"
            await repository.save_conversation_chunk(
                ConversationChunkDraft(
                    trip_id=scope_trip_id,
                    exchange_id=exchange_id,
                    user_message_id=user_message.id,
                    assistant_message_id=assistant_message.id,
                    retrieval_text=retrieval_text,
                    retrieval_text_sha256=hashlib.sha256(
                        retrieval_text.encode("utf-8")
                    ).hexdigest(),
                    source_token_count=10,
                    retrieval_token_count=10,
                    enhancement_model="none",
                    enhancement_version=1,
                    embedding_model="qwen3.7-text-embedding",
                    embedding=embedding,
                )
            )

        current_exchange_id = uuid4()
        current_other_exchange_id = uuid4()
        same_user_other_exchange_id = uuid4()
        other_user_exchange_id = uuid4()
        query_embedding = [1.0, 0.0] + [0.0] * 1022
        try:
            await seed_scope(database, user_id, trip_id)
            async with database.connection() as connection:
                await connection.execute(
                    "INSERT INTO tourism_agent.trips (id, user_id) VALUES (%s, %s)",
                    (same_user_other_trip_id, user_id),
                )
            await seed_scope(database, other_user_id, other_user_trip_id)
            repository = PlanningRepository(database)

            await persist_exchange(
                repository,
                trip_id,
                current_exchange_id,
                "本次预算5000元",
                "已记录预算",
                [0.8, 0.6] + [0.0] * 1022,
            )
            await persist_exchange(
                repository,
                trip_id,
                current_other_exchange_id,
                "喜欢海边",
                "已记录偏好",
                [0.0, 1.0] + [0.0] * 1022,
            )
            await persist_exchange(
                repository,
                same_user_other_trip_id,
                same_user_other_exchange_id,
                "其他旅行的预算",
                "不属于当前Trip",
                query_embedding,
            )
            await persist_exchange(
                repository,
                other_user_trip_id,
                other_user_exchange_id,
                "其他用户的预算",
                "不属于当前用户",
                query_embedding,
            )

            matches = await repository.search_conversation_chunks(
                user_id,
                trip_id,
                query_embedding,
                5,
                [],
            )
            wrong_user_matches = await repository.search_conversation_chunks(
                other_user_id,
                trip_id,
                query_embedding,
                5,
                [],
            )
            matches_after_exclusion = await repository.search_conversation_chunks(
                user_id,
                trip_id,
                query_embedding,
                1,
                [current_exchange_id],
            )
            exchanges = await repository.get_conversation_exchanges(
                user_id,
                trip_id,
                [
                    current_exchange_id,
                    same_user_other_exchange_id,
                    other_user_exchange_id,
                ],
            )

            assert [match.exchange_id for match in matches] == [
                current_exchange_id,
                current_other_exchange_id,
            ]
            assert matches[0].similarity == pytest.approx(0.8)
            assert matches[1].similarity == pytest.approx(0.0)
            assert [match.exchange_id for match in matches_after_exclusion] == [
                current_other_exchange_id
            ]
            assert matches[0].created_at.tzinfo is not None
            assert matches[1].created_at.tzinfo is not None
            assert wrong_user_matches == []
            assert [(item.exchange_id, item.user_message, item.assistant_message) for item in exchanges] == [
                (current_exchange_id, "本次预算5000元", "已记录预算")
            ]
            assert exchanges[0].user_created_at.tzinfo is not None
            assert exchanges[0].assistant_created_at.tzinfo is not None
            assert exchanges[0].user_created_at <= exchanges[0].assistant_created_at
        finally:
            await delete_scope(database, user_id)
            await delete_scope(database, other_user_id)
            await database.close()

    run_async(scenario())


def test_idempotency_repository_claims_once_and_replays_terminal_response() -> None:
    """同一幂等 ID 只能被首次请求认领，终态响应必须能够原样读取。"""

    async def scenario() -> None:
        from tourism_agent.infrastructure.migrations import apply_migrations
        from tourism_agent.models.idempotency import IdempotencyStatus
        from tourism_agent.repositories.idempotency import IdempotencyRepository

        user_id = uuid4()
        trip_id = uuid4()
        idempotency_id = uuid4()
        database = PostgresDatabase(DatabaseSettings())
        await database.open()
        try:
            await apply_migrations(database)
            await seed_scope(database, user_id, trip_id)
            repository = IdempotencyRepository(database)

            first = await repository.claim(
                idempotency_id,
                user_id=user_id,
                trip_id=trip_id,
                request_hash="request-hash",
            )
            duplicate = await repository.claim(
                idempotency_id,
                user_id=user_id,
                trip_id=trip_id,
                request_hash="request-hash",
            )

            assert first.created is True
            assert first.record.status is IdempotencyStatus.PROCESSING
            assert duplicate.created is False
            assert duplicate.record.status is IdempotencyStatus.PROCESSING

            await repository.finish(
                idempotency_id,
                status=IdempotencyStatus.COMPLETED,
                response_status=200,
                response_body={"message": "已完成"},
            )
            replay = await repository.claim(
                idempotency_id,
                user_id=user_id,
                trip_id=trip_id,
                request_hash="request-hash",
            )

            assert replay.created is False
            assert replay.record.status is IdempotencyStatus.COMPLETED
            assert replay.record.response_status == 200
            assert replay.record.response_body == {"message": "已完成"}
        finally:
            await delete_scope(database, user_id)
            await database.close()

    run_async(scenario())
