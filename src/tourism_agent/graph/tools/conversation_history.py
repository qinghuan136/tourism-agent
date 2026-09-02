"""提供限定当前用户与 Trip 的两阶段 Conversation 召回 Tools。"""

import json
import logging
from typing import NotRequired, TypedDict
from uuid import UUID

from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import ToolRuntime

from tourism_agent.graph.history import conversation_exchange_ids
from tourism_agent.models.context import ConversationMessage
from tourism_agent.services.conversation_retrieval import (
    ConversationRetrievalService,
)

logger = logging.getLogger(__name__)


class ConversationHistoryToolState(TypedDict):
    """声明历史 Tool 从业务子图 State 读取的可信作用域。"""

    user_id: UUID
    trip_id: UUID
    conversation_context: NotRequired[list[ConversationMessage]]
    retrieval_user_input: NotRequired[str]
    retrieval_task_goal: NotRequired[str]


def create_conversation_history_tools(
    service: ConversationRetrievalService,
) -> list[BaseTool]:
    """创建在执行时从 State 读取用户和 Trip 作用域的只读历史 Tools。"""

    @tool
    async def search_conversation_history(
        query: str,
        runtime: ToolRuntime[None, ConversationHistoryToolState],
    ) -> str:
        """搜索当前旅行历史，返回检索文本、时间、向量相似度、Rerank 分数和 Exchange ID。"""
        user_id = runtime.state["user_id"]
        trip_id = runtime.state["trip_id"]
        exclude_exchange_ids = conversation_exchange_ids(
            runtime.state.get("conversation_context", [])
        )
        recent_conversation = runtime.state.get("conversation_context", [])
        logger.info(
            "Tool调用开始 name=search_conversation_history user_id=%s trip_id=%s",
            user_id,
            trip_id,
        )
        matches = await service.search(
            user_id=user_id,
            trip_id=trip_id,
            query=query,
            exclude_exchange_ids=exclude_exchange_ids,
            current_user_input=runtime.state.get("retrieval_user_input", query),
            task_goal=runtime.state.get("retrieval_task_goal", query),
            recent_conversation=recent_conversation,
        )
        logger.info(
            "Tool调用完成 name=search_conversation_history trip_id=%s match_count=%d",
            trip_id,
            len(matches),
        )
        return json.dumps(
            {
                "matches": [
                    {
                        "exchange_id": str(match.exchange_id),
                        "retrieval_text": match.retrieval_text,
                        "similarity": match.similarity,
                        "rerank_score": match.rerank_score,
                        "created_at": match.created_at.isoformat(),
                    }
                    for match in matches
                ]
            },
            ensure_ascii=False,
        )

    @tool
    async def read_conversation_exchanges(
        exchange_ids: list[UUID],
        runtime: ToolRuntime[None, ConversationHistoryToolState],
    ) -> str:
        """按搜索结果中的 Exchange ID 读取原始双方消息及各自创建时间。"""
        user_id = runtime.state["user_id"]
        trip_id = runtime.state["trip_id"]
        logger.info(
            "Tool调用开始 name=read_conversation_exchanges user_id=%s trip_id=%s "
            "exchange_count=%d",
            user_id,
            trip_id,
            len(exchange_ids),
        )
        exchanges = await service.read_exchanges(
            user_id=user_id,
            trip_id=trip_id,
            exchange_ids=exchange_ids,
        )
        logger.info(
            "Tool调用完成 name=read_conversation_exchanges trip_id=%s exchange_count=%d",
            trip_id,
            len(exchanges),
        )
        return json.dumps(
            {
                "exchanges": [
                    {
                        "exchange_id": str(exchange.exchange_id),
                        "user_message": exchange.user_message,
                        "assistant_message": exchange.assistant_message,
                        "user_created_at": exchange.user_created_at.isoformat(),
                        "assistant_created_at": (
                            exchange.assistant_created_at.isoformat()
                        ),
                    }
                    for exchange in exchanges
                ]
            },
            ensure_ascii=False,
        )

    return [search_conversation_history, read_conversation_exchanges]
