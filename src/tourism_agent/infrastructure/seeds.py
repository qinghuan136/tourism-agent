"""创建本地调试和演示所需的最小基础数据。"""

import asyncio
import os
from uuid import UUID

from tourism_agent.infrastructure.database import DatabaseSettings, PostgresDatabase
from tourism_agent.infrastructure.migrations import apply_migrations

DEMO_USER_ID = UUID("00000000-0000-4000-8000-000000000001")
DEMO_TRIP_ID = UUID("00000000-0000-4000-8000-000000000002")


async def seed_demo_data(database: PostgresDatabase) -> None:
    """幂等创建一个演示用户及其可用旅行。"""
    async with database.connection() as connection, connection.transaction():
        await connection.execute(
            """
            INSERT INTO tourism_agent.users (id)
            VALUES (%s)
            ON CONFLICT (id) DO NOTHING
            """,
            (DEMO_USER_ID,),
        )
        await connection.execute(
            """
            INSERT INTO tourism_agent.trips (id, user_id)
            VALUES (%s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (DEMO_TRIP_ID, DEMO_USER_ID),
        )


async def _run() -> None:
    """初始化表结构后写入演示数据。"""
    database = PostgresDatabase(DatabaseSettings())
    await database.open()
    try:
        await apply_migrations(database)
        await seed_demo_data(database)
    finally:
        await database.close()


def main() -> None:
    """执行 seed，并输出可直接用于 API 调试的固定标识。"""
    loop_factory = asyncio.SelectorEventLoop if os.name == "nt" else None
    asyncio.run(_run(), loop_factory=loop_factory)
    print("演示数据已就绪：")
    print(f"user_id={DEMO_USER_ID}")
    print(f"trip_id={DEMO_TRIP_ID}")


if __name__ == "__main__":
    main()
