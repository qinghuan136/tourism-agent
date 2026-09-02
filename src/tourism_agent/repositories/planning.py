"""封装 Planning 阶段使用的 PostgreSQL 业务查询。"""

import json
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from tourism_agent.infrastructure.database import PostgresDatabase
from tourism_agent.models.context import ConversationMessage, ConversationRole
from tourism_agent.models.rag import (
    ConversationChunkCandidate,
    ConversationChunkDraft,
    ConversationExchange,
)


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
                SELECT id, role, content, created_at, exchange_id
                FROM (
                    SELECT id, role, content, created_at, exchange_id
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

    async def save_conversation_chunk(self, chunk: ConversationChunkDraft) -> None:
        """原子绑定 Exchange，并保存一问一答对应的向量 Chunk。"""
        embedding_literal = json.dumps(chunk.embedding, separators=(",", ":"))
        async with self._database.connection() as connection, connection.transaction():
            await connection.execute(
                """
                UPDATE tourism_agent.conversation_messages
                SET exchange_id = %s
                WHERE trip_id = %s AND id IN (%s, %s)
                """,
                (
                    chunk.exchange_id,
                    chunk.trip_id,
                    chunk.user_message_id,
                    chunk.assistant_message_id,
                ),
            )
            await connection.execute(
                """
                INSERT INTO tourism_agent.conversation_rag_chunks (
                    trip_id,
                    exchange_id,
                    user_message_id,
                    assistant_message_id,
                    retrieval_text,
                    retrieval_text_sha256,
                    source_token_count,
                    retrieval_token_count,
                    enhancement_model,
                    enhancement_version,
                    embedding_model,
                    embedding
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector
                )
                """,
                (
                    chunk.trip_id,
                    chunk.exchange_id,
                    chunk.user_message_id,
                    chunk.assistant_message_id,
                    chunk.retrieval_text,
                    chunk.retrieval_text_sha256,
                    chunk.source_token_count,
                    chunk.retrieval_token_count,
                    chunk.enhancement_model,
                    chunk.enhancement_version,
                    chunk.embedding_model,
                    embedding_literal,
                ),
            )

    async def search_conversation_chunks(
        self,
        user_id: UUID,
        trip_id: UUID,
        embedding: list[float],
        limit: int,
        exclude_exchange_ids: list[UUID],
    ) -> list[ConversationChunkCandidate]:
        """先限制用户和 Trip，再按余弦相似度精确排序 Chunk。"""
        embedding_literal = json.dumps(embedding, separators=(",", ":"))
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT chunk.exchange_id,
                       chunk.retrieval_text,
                       1 - (chunk.embedding <=> %s::vector) AS similarity,
                       chunk.created_at,
                       chunk.embedding::text AS embedding
                FROM tourism_agent.conversation_rag_chunks AS chunk
                JOIN tourism_agent.trips AS trip
                  ON trip.id = chunk.trip_id
                 AND trip.user_id = %s
                 AND trip.archived_at IS NULL
                WHERE chunk.trip_id = %s
                  AND NOT (chunk.exchange_id = ANY(%s::uuid[]))
                ORDER BY similarity DESC, chunk.id DESC
                LIMIT %s
                """,
                (
                    embedding_literal,
                    user_id,
                    trip_id,
                    exclude_exchange_ids,
                    limit,
                ),
            )
            rows = await cursor.fetchall()
        return [
            ConversationChunkCandidate(
                exchange_id=row["exchange_id"],
                retrieval_text=row["retrieval_text"],
                similarity=float(row["similarity"]),
                created_at=row["created_at"],
                embedding=json.loads(row["embedding"]),
            )
            for row in rows
        ]

    async def get_conversation_exchanges(
        self,
        user_id: UUID,
        trip_id: UUID,
        exchange_ids: list[UUID],
    ) -> list[ConversationExchange]:
        """在同一可信作用域内按 Exchange ID 读取原始双方消息。"""
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT chunk.exchange_id,
                       user_message.content AS user_message,
                       assistant_message.content AS assistant_message,
                       user_message.created_at AS user_created_at,
                       assistant_message.created_at AS assistant_created_at
                FROM tourism_agent.conversation_rag_chunks AS chunk
                JOIN tourism_agent.trips AS trip
                  ON trip.id = chunk.trip_id
                 AND trip.user_id = %s
                 AND trip.archived_at IS NULL
                JOIN tourism_agent.conversation_messages AS user_message
                  ON user_message.trip_id = chunk.trip_id
                 AND user_message.id = chunk.user_message_id
                JOIN tourism_agent.conversation_messages AS assistant_message
                  ON assistant_message.trip_id = chunk.trip_id
                 AND assistant_message.id = chunk.assistant_message_id
                WHERE chunk.trip_id = %s
                  AND chunk.exchange_id = ANY(%s::uuid[])
                ORDER BY chunk.id ASC
                """,
                (user_id, trip_id, exchange_ids),
            )
            rows = await cursor.fetchall()
        return [
            ConversationExchange(
                exchange_id=row["exchange_id"],
                user_message=row["user_message"],
                assistant_message=row["assistant_message"],
                user_created_at=row["user_created_at"],
                assistant_created_at=row["assistant_created_at"],
            )
            for row in rows
        ]

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
