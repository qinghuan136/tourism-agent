"""定义消息请求幂等记录的领域模型。"""

from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class IdempotencyStatus(StrEnum):
    """表示一次消息请求在后端的处理状态。"""

    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class IdempotencyRecord(BaseModel):
    """保存请求指纹以及可供后续重放的终态 HTTP 响应。"""

    idempotency_id: UUID
    user_id: UUID
    trip_id: UUID
    request_hash: str
    status: IdempotencyStatus
    response_status: int | None = None
    response_body: dict[str, Any] | None = None
