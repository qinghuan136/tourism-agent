"""定义 Planning 阶段从业务数据库读取的上下文模型。"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class ConversationRole(StrEnum):
    """业务对话只保存用户可见的双方消息。"""

    USER = "user"
    ASSISTANT = "assistant"


class ConversationMessage(BaseModel):
    """一条已持久化的用户可见对话。"""

    id: int
    role: ConversationRole
    content: str
    created_at: datetime
