"""验证 PostgreSQL 基础设施的配置与真实连接行为。"""

import asyncio
import os
from collections.abc import Coroutine
from importlib import import_module
from typing import Any

import pytest


def run_async[ResultT](coroutine: Coroutine[Any, Any, ResultT]) -> ResultT:
    """测试在 Windows 上使用 psycopg 支持的 SelectorEventLoop。"""
    loop_factory = asyncio.SelectorEventLoop if os.name == "nt" else None
    return asyncio.run(coroutine, loop_factory=loop_factory)


def test_database_settings_load_split_postgres_variables(monkeypatch) -> None:
    """分项环境变量应生成连接池所需参数，缺省主机使用 localhost。"""
    monkeypatch.setenv("POSTGRES_DB", "travel_agent")
    monkeypatch.setenv("POSTGRES_USER", "tourism_user")
    monkeypatch.setenv("POSTGRES_PASSWORD", "local-password")
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    monkeypatch.delenv("POSTGRES_HOST", raising=False)

    database_module = import_module("tourism_agent.infrastructure.database")
    settings = database_module.DatabaseSettings(_env_file=None)

    assert settings.connection_parameters() == {
        "host": "localhost",
        "port": 5432,
        "dbname": "travel_agent",
        "user": "tourism_user",
        "password": "local-password",
    }


@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="仅在显式开启时连接本地 PostgreSQL",
)
def test_database_pool_can_query_configured_postgres() -> None:
    """项目连接池应能通过真实 PostgreSQL 执行并读取查询。"""
    database_module = import_module("tourism_agent.infrastructure.database")
    settings = database_module.DatabaseSettings()
    database = database_module.PostgresDatabase(settings)

    async def query_database_name() -> str:
        await database.open()
        try:
            async with database.connection() as connection:
                cursor = await connection.execute("SELECT current_database() AS name")
                row = await cursor.fetchone()
                return row["name"]
        finally:
            await database.close()

    assert run_async(query_database_name()) == settings.database


@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="仅在显式开启时初始化本地 PostgreSQL",
)
def test_initial_migration_creates_planning_business_tables() -> None:
    """迁移后应包含 Planning 业务表和消息请求幂等记录表。"""
    database_module = import_module("tourism_agent.infrastructure.database")
    migration_module = import_module("tourism_agent.infrastructure.migrations")
    database = database_module.PostgresDatabase(database_module.DatabaseSettings())

    async def migrate_and_read_tables() -> list[str]:
        await database.open()
        try:
            await migration_module.apply_migrations(database)
            async with database.connection() as connection:
                cursor = await connection.execute(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'tourism_agent'
                      AND table_name <> 'schema_migrations'
                    ORDER BY table_name
                    """
                )
                return [row["table_name"] for row in await cursor.fetchall()]
        finally:
            await database.close()

    assert run_async(migrate_and_read_tables()) == [
        "conversation_messages",
        "current_itineraries",
        "idempotency_requests",
        "trip_contexts",
        "trips",
        "users",
    ]
