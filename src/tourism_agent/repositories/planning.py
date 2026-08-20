"""封装 Planning 阶段使用的 PostgreSQL 业务查询。"""

from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from tourism_agent.infrastructure.database import PostgresDatabase
from tourism_agent.models.context import ConversationMessage, ConversationRole


class PlanningRepository:
    """以用户和旅行作用域读写对话、动态上下文与当前行程。"""

    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

    async def trip_belongs_to_user(self, user_id: UUID, trip_id: UUID) -> bool:
        """确认旅行存在且属于当前用户。"""
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM tourism_agent.trips
                    WHERE id = %s AND user_id = %s AND archived_at IS NULL
                ) AS belongs
                """,
                (trip_id, user_id),
            )
            row = await cursor.fetchone()
        return bool(row["belongs"])

    async def append_conversation(
        self,
        trip_id: UUID,
        role: ConversationRole,
        content: str,
    ) -> ConversationMessage:
        """追加一条用户可见消息，并返回数据库生成的消息 ID。"""
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO tourism_agent.conversation_messages (trip_id, role, content)
                VALUES (%s, %s, %s)
                RETURNING id, role, content, created_at
                """,
                (trip_id, role.value, content),
            )
            row = await cursor.fetchone()
        return ConversationMessage.model_validate(row)

    async def get_recent_conversation(
        self,
        trip_id: UUID,
        *,
        before_message_id: int,
        limit: int,
    ) -> list[ConversationMessage]:
        """读取当前请求之前的近期消息，并恢复为正向时间顺序。"""
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT id, role, content, created_at
                FROM (
                    SELECT id, role, content, created_at
                    FROM tourism_agent.conversation_messages
                    WHERE trip_id = %s AND id < %s
                    ORDER BY id DESC
                    LIMIT %s
                ) AS recent
                ORDER BY id ASC
                """,
                (trip_id, before_message_id, limit),
            )
            rows = await cursor.fetchall()
        return [ConversationMessage.model_validate(row) for row in rows]

    async def get_trip_context(self, trip_id: UUID) -> dict[str, Any]:
        """读取当前旅行上下文；尚未建立时返回空对象。"""
        return await self._get_context(
            "SELECT data FROM tourism_agent.trip_contexts WHERE trip_id = %s",
            trip_id,
        )

    async def patch_trip_context(
        self,
        trip_id: UUID,
        patch: dict[str, Any],
    ) -> dict[str, Any]:
        """按顶层键合并当前旅行上下文。"""
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO tourism_agent.trip_contexts (trip_id, data)
                VALUES (%s, %s)
                ON CONFLICT (trip_id) DO UPDATE
                SET data = tourism_agent.trip_contexts.data || EXCLUDED.data,
                    updated_at = now()
                RETURNING data
                """,
                (trip_id, Jsonb(patch)),
            )
            row = await cursor.fetchone()
        return row["data"]

    async def delete_trip_context_keys(
        self,
        trip_id: UUID,
        keys: list[str],
    ) -> dict[str, Any]:
        """删除当前旅行上下文中的指定顶层键。"""
        return await self._delete_context_keys(
            """
            UPDATE tourism_agent.trip_contexts
            SET data = data - %s::text[], updated_at = now()
            WHERE trip_id = %s
            RETURNING data
            """,
            keys,
            trip_id,
        )

    async def get_current_itinerary(self, trip_id: UUID) -> str | None:
        """读取已确认的当前行程。"""
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT content
                FROM tourism_agent.current_itineraries
                WHERE trip_id = %s
                """,
                (trip_id,),
            )
            row = await cursor.fetchone()
        return row["content"] if row else None

    async def write_current_itinerary(self, trip_id: UUID, content: str) -> str:
        """写入已确认行程；当前阶段只保留每次旅行的最新版本。"""
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO tourism_agent.current_itineraries (trip_id, content)
                VALUES (%s, %s)
                ON CONFLICT (trip_id) DO UPDATE
                SET content = EXCLUDED.content, updated_at = now()
                RETURNING content
                """,
                (trip_id, content),
            )
            row = await cursor.fetchone()
        return row["content"]

    async def _get_context(self, query: str, scope_id: UUID) -> dict[str, Any]:
        """读取单行动态 Context。"""
        async with self._database.connection() as connection:
            cursor = await connection.execute(query, (scope_id,))
            row = await cursor.fetchone()
        return row["data"] if row else {}

    async def _delete_context_keys(
        self,
        query: str,
        keys: list[str],
        scope_id: UUID,
    ) -> dict[str, Any]:
        """执行动态 Context 的顶层键删除。"""
        async with self._database.connection() as connection:
            cursor = await connection.execute(query, (keys, scope_id))
            row = await cursor.fetchone()
        return row["data"] if row else {}
