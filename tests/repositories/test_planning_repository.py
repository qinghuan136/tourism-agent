"""验证 PlanningRepository 与真实 PostgreSQL 表的映射。"""

import asyncio
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
