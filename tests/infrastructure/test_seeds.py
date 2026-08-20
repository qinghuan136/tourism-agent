"""验证本地调试使用的演示数据 seed。"""

import asyncio
import os
from collections.abc import Coroutine
from importlib import import_module
from typing import Any

import pytest


def run_async[ResultT](coroutine: Coroutine[Any, Any, ResultT]) -> ResultT:
    """在 Windows 上使用 psycopg 支持的 SelectorEventLoop。"""
    loop_factory = asyncio.SelectorEventLoop if os.name == "nt" else None
    return asyncio.run(coroutine, loop_factory=loop_factory)


@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="仅在显式开启时写入本地 PostgreSQL 演示数据",
)
def test_demo_seed_is_idempotent_and_creates_owned_trip() -> None:
    """重复执行 seed 不应重复数据，并且演示旅行必须归属于演示用户。"""
    database_module = import_module("tourism_agent.infrastructure.database")
    migration_module = import_module("tourism_agent.infrastructure.migrations")
    seed_module = import_module("tourism_agent.infrastructure.seeds")
    database = database_module.PostgresDatabase(database_module.DatabaseSettings())

    async def seed_twice_and_read_result() -> dict[str, Any]:
        await database.open()
        try:
            await migration_module.apply_migrations(database)
            await seed_module.seed_demo_data(database)
            await seed_module.seed_demo_data(database)
            async with database.connection() as connection:
                cursor = await connection.execute(
                    """
                    SELECT
                        (SELECT count(*)
                         FROM tourism_agent.users
                         WHERE id = '00000000-0000-4000-8000-000000000001') AS user_count,
                        (SELECT count(*)
                         FROM tourism_agent.trips
                         WHERE id = '00000000-0000-4000-8000-000000000002') AS trip_count,
                        EXISTS (
                            SELECT 1
                            FROM tourism_agent.trips
                            WHERE id = '00000000-0000-4000-8000-000000000002'
                              AND user_id = '00000000-0000-4000-8000-000000000001'
                              AND archived_at IS NULL
                        ) AS trip_is_available
                    """
                )
                return await cursor.fetchone()
        finally:
            await database.close()

    assert run_async(seed_twice_and_read_result()) == {
        "user_count": 1,
        "trip_count": 1,
        "trip_is_available": True,
    }
