"""封装消息请求幂等记录的 PostgreSQL 访问。"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from tourism_agent.infrastructure.database import PostgresDatabase
from tourism_agent.models.idempotency import IdempotencyRecord, IdempotencyStatus


@dataclass(frozen=True)
class IdempotencyClaim:
    """区分本次是否首次认领，同时返回数据库中的权威记录。"""

    record: IdempotencyRecord
    created: bool


class IdempotencyRepository:
    """原子认领消息请求，并在请求结束后保存可重放响应。"""

    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

    async def claim(
        self,
        idempotency_id: UUID,
        *,
        user_id: UUID,
        trip_id: UUID,
        request_hash: str,
    ) -> IdempotencyClaim:
        """首次调用插入 processing；重复调用读取已有记录。"""
        async with self._database.connection() as connection, connection.transaction():
            cursor = await connection.execute(
                """
                INSERT INTO tourism_agent.idempotency_requests (
                    idempotency_id,
                    user_id,
                    trip_id,
                    request_hash,
                    status
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (idempotency_id) DO NOTHING
                RETURNING idempotency_id, user_id, trip_id, request_hash, status,
                          response_status, response_body
                """,
                (
                    idempotency_id,
                    user_id,
                    trip_id,
                    request_hash,
                    IdempotencyStatus.PROCESSING.value,
                ),
            )
            row = await cursor.fetchone()
            if row is not None:
                return IdempotencyClaim(
                    record=IdempotencyRecord.model_validate(row),
                    created=True,
                )

            cursor = await connection.execute(
                """
                SELECT idempotency_id, user_id, trip_id, request_hash, status,
                       response_status, response_body
                FROM tourism_agent.idempotency_requests
                WHERE idempotency_id = %s
                """,
                (idempotency_id,),
            )
            existing = await cursor.fetchone()

        if existing is None:
            raise RuntimeError("幂等请求认领后未能读取数据库记录")
        return IdempotencyClaim(
            record=IdempotencyRecord.model_validate(existing),
            created=False,
        )

    async def finish(
        self,
        idempotency_id: UUID,
        *,
        status: IdempotencyStatus,
        response_status: int,
        response_body: dict[str, Any],
    ) -> IdempotencyRecord:
        """写入请求终态及完整 JSON 响应，供重复请求原样重放。"""
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                UPDATE tourism_agent.idempotency_requests
                SET status = %s,
                    response_status = %s,
                    response_body = %s,
                    updated_at = now()
                WHERE idempotency_id = %s
                RETURNING idempotency_id, user_id, trip_id, request_hash, status,
                          response_status, response_body
                """,
                (
                    status.value,
                    response_status,
                    Jsonb(response_body),
                    idempotency_id,
                ),
            )
            row = await cursor.fetchone()

        if row is None:
            raise RuntimeError("要结束的幂等请求记录不存在")
        return IdempotencyRecord.model_validate(row)
