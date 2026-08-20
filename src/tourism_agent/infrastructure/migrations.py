"""按顺序执行项目内置的 PostgreSQL 迁移。"""

import asyncio
import os
from importlib import resources

from tourism_agent.infrastructure.database import DatabaseSettings, PostgresDatabase

MIGRATIONS = (
    "001_initial_planning.sql",
    "002_remove_user_contexts.sql",
    "003_add_idempotency_requests.sql",
)


async def apply_migrations(database: PostgresDatabase) -> None:
    """执行尚未应用的迁移，并在同一事务中记录迁移版本。"""
    async with database.connection() as connection, connection.transaction():
        await connection.execute("CREATE SCHEMA IF NOT EXISTS tourism_agent")
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tourism_agent.schema_migrations (
                version text PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )

        for migration_name in MIGRATIONS:
            cursor = await connection.execute(
                """
                SELECT 1
                FROM tourism_agent.schema_migrations
                WHERE version = %s
                """,
                (migration_name,),
            )
            if await cursor.fetchone():
                continue

            migration_sql = (
                resources.files("tourism_agent.infrastructure.sql")
                .joinpath(migration_name)
                .read_text(encoding="utf-8")
            )
            await connection.execute(migration_sql)
            await connection.execute(
                """
                INSERT INTO tourism_agent.schema_migrations (version)
                VALUES (%s)
                """,
                (migration_name,),
            )


async def _run() -> None:
    """连接数据库并执行迁移，供命令行入口调用。"""
    database = PostgresDatabase(DatabaseSettings())
    await database.open()
    try:
        await apply_migrations(database)
    finally:
        await database.close()


def main() -> None:
    """执行数据库初始化，并输出不包含连接凭据的结果。"""
    loop_factory = asyncio.SelectorEventLoop if os.name == "nt" else None
    asyncio.run(_run(), loop_factory=loop_factory)
    print("PostgreSQL 连接成功，数据库迁移已完成。")


if __name__ == "__main__":
    main()
