"""验证 Planning Agent 通过 Tool 更新动态业务上下文。"""

import asyncio
from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool

from tourism_agent.graph.subgraphs.planning.graph import build_planning_graph
from tourism_agent.graph.subgraphs.planning.tools import create_planning_tools

USER_ID = UUID("33333333-3333-3333-3333-333333333333")
TRIP_ID = UUID("44444444-4444-4444-4444-444444444444")


@tool
def get_weather(location: str, time_range: str) -> str:
    """测试用天气查询。"""
    return f"{location}：{time_range}"


@tool
def search_places(query: str) -> str:
    """测试用地点查询。"""
    return query


@tool
def web_search(query: str) -> str:
    """测试用网页查询。"""
    return query


class WritableContextFakeRepository:
    """记录 Tool 写入，并向 load_context 提供空的初始快照。"""

    def __init__(self) -> None:
        self.trip_context: dict[str, object] = {}
        self.write_count = 0

    async def get_recent_conversation(
        self,
        trip_id: UUID,
        *,
        before_message_id: int,
        limit: int,
    ) -> list[object]:
        return []

    async def get_trip_context(self, trip_id: UUID) -> dict[str, object]:
        return dict(self.trip_context)

    async def get_current_itinerary(self, trip_id: UUID) -> None:
        return None

    async def patch_trip_context(
        self,
        trip_id: UUID,
        patch: dict[str, object],
    ) -> dict[str, object]:
        assert trip_id == TRIP_ID
        self.write_count += 1
        self.trip_context.update(patch)
        return dict(self.trip_context)


class TripContextWritingFakeModel:
    """先更新 TripContext，再依据 ToolMessage 结束本轮。"""

    def bind_tools(self, _tools: list[object]) -> RunnableLambda:
        def respond(messages: list[BaseMessage]) -> AIMessage:
            if any(isinstance(message, ToolMessage) for message in messages):
                return AIMessage(content="预算已经记住了")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "update_trip_context",
                        "args": {"patch": {"预算": "5000元"}},
                        "id": "trip-context-call-1",
                        "type": "tool_call",
                    }
                ],
            )

        return RunnableLambda(respond)


class MultipleTripContextWritesFakeModel:
    """先违规分两次写入，收到拒绝后合并为一次 patch。"""

    def bind_tools(self, _tools: list[object]) -> RunnableLambda:
        def respond(messages: list[BaseMessage]) -> AIMessage:
            tool_messages = [
                message for message in messages if isinstance(message, ToolMessage)
            ]
            if tool_messages and "多个 TripContext 写 Tool" in str(
                tool_messages[-1].content
            ):
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "update_trip_context",
                            "args": {"patch": {"目的地": "南京", "预算": "5000元"}},
                            "id": "merged-context-write",
                            "type": "tool_call",
                        }
                    ],
                )
            if tool_messages:
                return AIMessage(content="旅行信息已合并更新")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "update_trip_context",
                        "args": {"patch": {"目的地": "南京"}},
                        "id": "split-context-write-1",
                        "type": "tool_call",
                    },
                    {
                        "name": "update_trip_context",
                        "args": {"patch": {"预算": "5000元"}},
                        "id": "split-context-write-2",
                        "type": "tool_call",
                    },
                ],
            )

        return RunnableLambda(respond)


def test_planning_exposes_only_current_stage_tools() -> None:
    """Planning 只暴露查询、Context、候选提交和必要信息询问能力。"""
    repository = WritableContextFakeRepository()

    tools = create_planning_tools(
        repository,
        [get_weather, search_places, web_search],
    )

    assert [tool.name for tool in tools] == [
        "get_weather",
        "search_places",
        "web_search",
        "update_trip_context",
        "delete_trip_context_keys",
        "submit_candidate_itinerary",
        "ask_user",
    ]


def test_context_write_tool_updates_database_and_current_state() -> None:
    """一次 Tool 写入必须同时更新长期事实和本轮工作快照。"""
    repository = WritableContextFakeRepository()
    graph = build_planning_graph(TripContextWritingFakeModel(), repository)

    result = asyncio.run(
        graph.ainvoke(
            {
                "user_id": USER_ID,
                "trip_id": TRIP_ID,
                "user_message_id": 10,
                "messages": [("user", "这次预算是5000元")],
            },
            {"recursion_limit": 8},
        )
    )

    assert repository.trip_context == {"预算": "5000元"}
    assert result["trip_context"] == {"预算": "5000元"}
    assert result["assistant_message"] == "预算已经记住了"


def test_multiple_trip_context_writes_are_rejected_before_execution() -> None:
    """同轮多个 Context 写入必须整批拒绝，合并后才能执行一次。"""
    repository = WritableContextFakeRepository()
    graph = build_planning_graph(MultipleTripContextWritesFakeModel(), repository)

    result = asyncio.run(
        graph.ainvoke(
            {
                "user_id": USER_ID,
                "trip_id": TRIP_ID,
                "user_message_id": 10,
                "messages": [("user", "目的地是南京，预算5000元")],
            },
            {"recursion_limit": 10},
        )
    )

    assert repository.write_count == 1
    assert repository.trip_context == {"目的地": "南京", "预算": "5000元"}
    assert result["trip_context"] == {"目的地": "南京", "预算": "5000元"}
    assert result["assistant_message"] == "旅行信息已合并更新"
