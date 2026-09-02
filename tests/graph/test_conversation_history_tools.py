"""验证 Conversation 两阶段召回 Tools 的安全边界与输出。"""

import asyncio
import json
from datetime import UTC, datetime
from typing import Annotated, NotRequired, TypedDict
from uuid import UUID

from langchain_core.messages import AIMessage, AnyMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from tourism_agent.models.context import ConversationMessage, ConversationRole
from tourism_agent.models.rag import ConversationChunkMatch, ConversationExchange

USER_ID = UUID("11111111-1111-1111-1111-111111111111")
TRIP_ID = UUID("22222222-2222-2222-2222-222222222222")
EXCHANGE_ID = UUID("33333333-3333-3333-3333-333333333333")
RECENT_EXCHANGE_ID = UUID("44444444-4444-4444-4444-444444444444")
CHUNK_CREATED_AT = datetime(2026, 8, 30, 8, 15, tzinfo=UTC)
USER_CREATED_AT = datetime(2026, 8, 30, 8, 14, tzinfo=UTC)
ASSISTANT_CREATED_AT = datetime(2026, 8, 30, 8, 15, tzinfo=UTC)


class ToolTestState(TypedDict):
    """为真实 ToolNode 提供运行时 State。"""

    user_id: UUID
    trip_id: UUID
    messages: Annotated[list[AnyMessage], add_messages]
    conversation_context: NotRequired[list[ConversationMessage]]
    retrieval_user_input: NotRequired[str]
    retrieval_task_goal: NotRequired[str]


def invoke_tools(tools: list[object], message: AIMessage) -> dict:
    """在最小 LangGraph 中运行 ToolNode，使 ToolRuntime 获得真实 State。"""
    builder = StateGraph(ToolTestState)
    builder.add_node("tools", ToolNode(tools))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    graph = builder.compile()
    return asyncio.run(
        graph.ainvoke(
            {
                "user_id": USER_ID,
                "trip_id": TRIP_ID,
                "messages": [message],
                "conversation_context": [
                    ConversationMessage(
                        id=1,
                        role=ConversationRole.USER,
                        content="已加载的近期消息",
                        created_at=USER_CREATED_AT,
                        exchange_id=RECENT_EXCHANGE_ID,
                    )
                ],
                "retrieval_user_input": "我之前定过多少预算？",
                "retrieval_task_goal": "召回用户此前确认的旅行预算",
            }
        )
    )


class FakeRetrievalService:
    """返回固定召回内容，并记录 Tool 使用的可信作用域。"""

    def __init__(self) -> None:
        self.search_call: dict[str, object] | None = None
        self.read_call: tuple[UUID, UUID, list[UUID]] | None = None

    async def search(
        self,
        *,
        user_id: UUID,
        trip_id: UUID,
        query: str,
        exclude_exchange_ids: list[UUID] | None = None,
        current_user_input: str | None = None,
        task_goal: str | None = None,
        recent_conversation: list[ConversationMessage] | None = None,
    ) -> list[ConversationChunkMatch]:
        self.search_call = {
            "user_id": user_id,
            "trip_id": trip_id,
            "query": query,
            "exclude_exchange_ids": exclude_exchange_ids or [],
            "current_user_input": current_user_input,
            "task_goal": task_goal,
            "recent_conversation": recent_conversation or [],
        }
        return [
            ConversationChunkMatch(
                exchange_id=EXCHANGE_ID,
                retrieval_text="用户曾说明旅行预算为5000元。",
                similarity=0.912345,
                created_at=CHUNK_CREATED_AT,
                rerank_score=0.876543,
            )
        ]

    async def read_exchanges(
        self,
        *,
        user_id: UUID,
        trip_id: UUID,
        exchange_ids: list[UUID],
    ) -> list[ConversationExchange]:
        self.read_call = (user_id, trip_id, exchange_ids)
        return [
            ConversationExchange(
                exchange_id=EXCHANGE_ID,
                user_message="这次预算控制在5000元。",
                assistant_message="好的，我会按5000元规划。",
                user_created_at=USER_CREATED_AT,
                assistant_created_at=ASSISTANT_CREATED_AT,
            )
        ]


def test_search_tool_returns_retrieval_text_without_exposing_scope_arguments() -> None:
    """搜索 Tool 只能接收查询文本，并返回精简检索文本和 Exchange ID。"""
    from tourism_agent.graph.tools.conversation_history import (
        create_conversation_history_tools,
    )

    service = FakeRetrievalService()
    tools = create_conversation_history_tools(service)
    tool = {item.name: item for item in tools}["search_conversation_history"]

    state_update = invoke_tools(
        tools,
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "search_conversation_history",
                    "args": {"query": "之前说过的预算"},
                    "id": "history-search-1",
                    "type": "tool_call",
                }
            ],
        )
    )
    result = json.loads(state_update["messages"][-1].content)

    assert set(tool.args) == {"query"}
    assert service.search_call is not None
    assert service.search_call["user_id"] == USER_ID
    assert service.search_call["trip_id"] == TRIP_ID
    assert service.search_call["query"] == "之前说过的预算"
    assert service.search_call["exclude_exchange_ids"] == [RECENT_EXCHANGE_ID]
    assert service.search_call["current_user_input"] == "我之前定过多少预算？"
    assert service.search_call["task_goal"] == "召回用户此前确认的旅行预算"
    runtime_history = service.search_call["recent_conversation"]
    assert isinstance(runtime_history, list)
    assert [message.content for message in runtime_history] == ["已加载的近期消息"]
    assert result == {
        "matches": [
            {
                "exchange_id": str(EXCHANGE_ID),
                "retrieval_text": "用户曾说明旅行预算为5000元。",
                "similarity": 0.912345,
                "rerank_score": 0.876543,
                "created_at": "2026-08-30T08:15:00+00:00",
            }
        ]
    }


def test_read_tool_returns_raw_exchange_without_exposing_scope_arguments() -> None:
    """原文 Tool 只能按搜索结果中的 Exchange ID 读取当前作用域原始消息。"""
    from tourism_agent.graph.tools.conversation_history import (
        create_conversation_history_tools,
    )

    service = FakeRetrievalService()
    tools = create_conversation_history_tools(service)
    tool = {item.name: item for item in tools}["read_conversation_exchanges"]

    state_update = invoke_tools(
        tools,
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_conversation_exchanges",
                    "args": {"exchange_ids": [str(EXCHANGE_ID)]},
                    "id": "history-read-1",
                    "type": "tool_call",
                }
            ],
        )
    )
    result = json.loads(state_update["messages"][-1].content)

    assert set(tool.args) == {"exchange_ids"}
    assert service.read_call == (USER_ID, TRIP_ID, [EXCHANGE_ID])
    assert result == {
        "exchanges": [
            {
                "exchange_id": str(EXCHANGE_ID),
                "user_message": "这次预算控制在5000元。",
                "assistant_message": "好的，我会按5000元规划。",
                "user_created_at": "2026-08-30T08:14:00+00:00",
                "assistant_created_at": "2026-08-30T08:15:00+00:00",
            }
        ]
    }
