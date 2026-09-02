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
def test_migrations_create_current_business_tables() -> None:
    """迁移后应包含当前业务表、幂等记录表和 Conversation RAG 表。"""
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
        "conversation_rag_chunks",
        "current_itineraries",
        "idempotency_requests",
        "trip_contexts",
        "trips",
        "users",
    ]


@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="仅在显式开启时验证 Conversation RAG 数据库结构",
)
def test_conversation_rag_schema_uses_fixed_exchange_and_embedding_contract() -> None:
    """RAG 表应约束一请求一回答，并固定模型与 1024 维向量。"""
    database_module = import_module("tourism_agent.infrastructure.database")
    migration_module = import_module("tourism_agent.infrastructure.migrations")
    database = database_module.PostgresDatabase(database_module.DatabaseSettings())

    async def migrate_and_read_schema() -> tuple[bool, dict[str, str], set[str]]:
        await database.open()
        try:
            await migration_module.apply_migrations(database)
            async with database.connection() as connection:
                extension_cursor = await connection.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') AS enabled"
                )
                extension_enabled = bool((await extension_cursor.fetchone())["enabled"])

                column_cursor = await connection.execute(
                    """
                    SELECT
                        table_name,
                        column_name,
                        CASE
                            WHEN table_name = 'conversation_rag_chunks'
                                 AND column_name = 'embedding'
                            THEN format_type(attribute.atttypid, attribute.atttypmod)
                            ELSE columns.udt_name
                        END AS data_type
                    FROM information_schema.columns AS columns
                    LEFT JOIN pg_class AS relation
                      ON relation.relname = columns.table_name
                    LEFT JOIN pg_namespace AS namespace
                      ON namespace.oid = relation.relnamespace
                     AND namespace.nspname = columns.table_schema
                    LEFT JOIN pg_attribute AS attribute
                      ON attribute.attrelid = relation.oid
                     AND attribute.attname = columns.column_name
                    WHERE columns.table_schema = 'tourism_agent'
                      AND (
                          (table_name = 'conversation_messages' AND column_name = 'exchange_id')
                          OR
                          (table_name = 'conversation_rag_chunks' AND column_name IN (
                              'exchange_id', 'user_message_id', 'assistant_message_id',
                              'retrieval_text', 'embedding_model', 'embedding'
                          ))
                      )
                    """
                )
                columns = {
                    f"{row['table_name']}.{row['column_name']}": row["data_type"]
                    for row in await column_cursor.fetchall()
                }

                constraint_cursor = await connection.execute(
                    """
                    SELECT constraint_name
                    FROM information_schema.table_constraints
                    WHERE table_schema = 'tourism_agent'
                      AND table_name = 'conversation_rag_chunks'
                    """
                )
                constraints = {
                    row["constraint_name"] for row in await constraint_cursor.fetchall()
                }
                return extension_enabled, columns, constraints
        finally:
            await database.close()

    extension_enabled, columns, constraints = run_async(migrate_and_read_schema())

    assert extension_enabled is True
    assert columns == {
        "conversation_messages.exchange_id": "uuid",
        "conversation_rag_chunks.exchange_id": "uuid",
        "conversation_rag_chunks.user_message_id": "int8",
        "conversation_rag_chunks.assistant_message_id": "int8",
        "conversation_rag_chunks.retrieval_text": "text",
        "conversation_rag_chunks.embedding_model": "text",
        "conversation_rag_chunks.embedding": "vector(1024)",
    }
    assert {
        "uq_conversation_rag_chunks_trip_exchange",
        "ck_conversation_rag_chunks_embedding_model",
        "fk_conversation_rag_chunks_user_message",
        "fk_conversation_rag_chunks_assistant_message",
    } <= constraints
