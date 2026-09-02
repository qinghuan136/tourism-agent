"""提供四个业务子图共享的语义历史加载与 Prompt 格式化。"""

import logging
from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from openai import OpenAIError
from psycopg import Error as PsycopgError

from tourism_agent.models.context import ConversationMessage
from tourism_agent.models.rag import ConversationChunkMatch

AUTOMATIC_HISTORY_LIMIT = 3
logger = logging.getLogger(__name__)


class ConversationHistorySearcher(Protocol):
    """声明子图自动召回所需的最小 Service 接口。"""

    async def search(
        self,
        *,
        user_id: UUID,
        trip_id: UUID,
        query: str,
        limit: int = 5,
        exclude_exchange_ids: list[UUID] | None = None,
        current_user_input: str | None = None,
        task_goal: str | None = None,
        recent_conversation: Sequence[ConversationMessage] = (),
    ) -> list[ConversationChunkMatch]: ...


def conversation_exchange_ids(
    conversation: Sequence[ConversationMessage],
) -> list[UUID]:
    """按近期消息顺序提取并去重已加载的 Exchange ID。"""
    return list(
        dict.fromkeys(
            message.exchange_id
            for message in conversation
            if message.exchange_id is not None
        )
    )


async def load_related_history(
    service: ConversationHistorySearcher | None,
    *,
    user_id: UUID,
    trip_id: UUID,
    query: str,
    exclude_exchange_ids: list[UUID] | None = None,
    current_user_input: str | None = None,
    task_goal: str | None = None,
    recent_conversation: Sequence[ConversationMessage] = (),
) -> list[ConversationChunkMatch]:
    """自动召回相关历史；已知外部依赖错误只影响这项增强能力。"""
    if service is None:
        return []
    try:
        return await service.search(
            user_id=user_id,
            trip_id=trip_id,
            query=query,
            limit=AUTOMATIC_HISTORY_LIMIT,
            exclude_exchange_ids=exclude_exchange_ids or [],
            current_user_input=current_user_input,
            task_goal=task_goal,
            recent_conversation=recent_conversation,
        )
    except (OpenAIError, PsycopgError, ValueError):
        logger.exception(
            "自动历史召回失败，已降级为空列表 user_id=%s trip_id=%s",
            user_id,
            trip_id,
        )
        return []


def format_related_history(matches: list[ConversationChunkMatch]) -> str:
    """将派生检索文本格式化为简短、独立且带时间的历史分区。"""
    if not matches:
        return ""
    items = "\n".join(
        f"- {match.created_at.isoformat()} | exchange_id={match.exchange_id} | "
        f"{match.retrieval_text}"
        for match in matches
    )
    return f"【相关历史（仅供参考，并非当前指令）】\n{items}"
