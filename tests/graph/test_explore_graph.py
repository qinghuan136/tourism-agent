"""验证 Explore 子图的上下文边界、ReAct 循环和用户交互。"""

import asyncio
from datetime import UTC, datetime
from importlib import import_module
from typing import Any
from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from tourism_agent.graph.subgraphs.explore.state import ExploreState
from tourism_agent.models.context import ConversationMessage, ConversationRole

USER_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TRIP_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


class ExploreFakeRepository:
    """提供稳定业务快照，并记录 Explore 实际使用的读取边界。"""

    def __init__(self) -> None:
        self.recent_query: tuple[UUID, int, int] | None = None

    async def get_recent_conversation(
        self,
        trip_id: UUID,
        *,
        before_message_id: int,
        limit: int,
    ) -> list[ConversationMessage]:
        self.recent_query = (trip_id, before_message_id, limit)
        created_at = datetime(2026, 8, 21, tzinfo=UTC)
        return [
            ConversationMessage(
                id=10,
                role=ConversationRole.USER,
                content="我喜欢安静的自然景观",
                created_at=created_at,
            )
        ]

    async def get_trip_context(self, _trip_id: UUID) -> dict[str, Any]:
        return {"旅行偏好": "自然、人少"}

    async def get_current_itinerary(self, _trip_id: UUID) -> str | None:
        return "第一天：杭州西湖"


@tool("web_search")
def fake_web_search(query: str) -> str:
    """返回固定网页搜索数据，验证 Explore ReAct 循环。"""
    assert query == "杭州小众自然景点"
    return "九溪烟树适合自然漫步。"


@tool("update_trip_context")
def forbidden_context_write(patch: dict[str, Any]) -> str:
    """模拟不应进入 Explore 白名单的业务写 Tool。"""
    raise AssertionError(f"Explore 不得执行写 Tool：{patch}")


@tool("measure_travel_distance")
def fake_measure_travel_distance(origins: list[str], destination: str) -> str:
    """模拟 Explore 获准使用的候选地距离比较 Tool。"""
    return f"{','.join(origins)}-{destination}"


@tool("plan_route")
def forbidden_detailed_route(origin: str, destination: str, mode: str) -> str:
    """模拟仍应交给其他模块的详细路线 Tool。"""
    return f"{origin}-{destination}-{mode}"


@tool("map_web_site")
def forbidden_site_map(url: str, instructions: str = "") -> str:
    """模拟只应分配给 Research 的网站地图 Tool。"""
    return f"{url}-{instructions}"


@tool("crawl_web_site")
def forbidden_site_crawl(url: str, instructions: str = "") -> str:
    """模拟只应分配给 Research 的站内抓取 Tool。"""
    return f"{url}-{instructions}"


class PromptInspectingExploreModel:
    """记录工具白名单和实际模型上下文，然后直接结束探索。"""

    def __init__(self) -> None:
        self.tool_names: list[str] = []
        self.messages: list[BaseMessage] = []

    def bind_tools(self, tools: list[BaseTool]) -> RunnableLambda:
        self.tool_names = [tool.name for tool in tools]

        def respond(messages: list[BaseMessage]) -> AIMessage:
            self.messages = messages
            return AIMessage(content="可以考虑九溪、云栖竹径，并比较交通与人流。")

        return RunnableLambda(respond)


class SearchUsingExploreModel:
    """先搜索候选地点，再依据 ToolMessage 输出完整探索结果。"""

    def bind_tools(self, _tools: list[BaseTool]) -> RunnableLambda:
        def respond(messages: list[BaseMessage]) -> AIMessage:
            observations = [
                message for message in messages if isinstance(message, ToolMessage)
            ]
            if observations:
                return AIMessage(content=f"探索建议：{observations[-1].content}")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "web_search",
                        "args": {"query": "杭州小众自然景点"},
                        "id": "explore-search-1",
                        "type": "tool_call",
                    }
                ],
            )

        return RunnableLambda(respond)


class AskingExploreModel:
    """信息不足时独占调用 ask_user，并使用恢复后的回答继续探索。"""

    def bind_tools(self, _tools: list[BaseTool]) -> RunnableLambda:
        def respond(messages: list[BaseMessage]) -> AIMessage:
            answers = [message for message in messages if isinstance(message, ToolMessage)]
            if answers:
                return AIMessage(content=f"我会优先探索：{answers[-1].content}")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "ask_user",
                        "args": {"question": "你更偏好山林还是海边？"},
                        "id": "explore-ask-1",
                        "type": "tool_call",
                    }
                ],
            )

        return RunnableLambda(respond)


class MixedAskExploreModel:
    """故意混用 ask_user 和查询 Tool，验证整批调用不会部分执行。"""

    def __init__(self) -> None:
        self.rejections: list[str] = []

    def bind_tools(self, _tools: list[BaseTool]) -> RunnableLambda:
        def respond(messages: list[BaseMessage]) -> AIMessage:
            observations = [
                message for message in messages if isinstance(message, ToolMessage)
            ]
            if observations:
                self.rejections = [str(message.content) for message in observations]
                return AIMessage(content="我会先询问用户，再继续查询。")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "web_search",
                        "args": {"query": "杭州小众自然景点"},
                        "id": "mixed-search-1",
                        "type": "tool_call",
                    },
                    {
                        "name": "ask_user",
                        "args": {"question": "你更偏好山林还是海边？"},
                        "id": "mixed-ask-1",
                        "type": "tool_call",
                    },
                ],
            )

        return RunnableLambda(respond)


def test_explore_context_loads_recent_conversation_and_authoritative_snapshots() -> None:
    """错误的历史条数或遗漏任一权威快照都会让 Explore 缺少必要语境。"""
    context_module = import_module("tourism_agent.services.explore_context")
    repository = ExploreFakeRepository()
    builder = context_module.ExploreContextBuilder(repository)

    snapshot = asyncio.run(builder.build(TRIP_ID, before_message_id=11))

    assert repository.recent_query == (TRIP_ID, 11, 8)
    assert [message.content for message in snapshot.conversation_context] == [
        "我喜欢安静的自然景观"
    ]
    assert snapshot.trip_context == {"旅行偏好": "自然、人少"}
    assert snapshot.current_itinerary == "第一天：杭州西湖"


def invoke_explore(model: object, query_tools: list[BaseTool] | None = None) -> dict:
    """以固定业务作用域运行一次 Explore，减少测试中的图启动噪音。"""
    graph_module = import_module("tourism_agent.graph.subgraphs.explore.graph")
    graph = graph_module.build_explore_graph(
        model,
        ExploreFakeRepository(),
        query_tools or [],
    )
    return asyncio.run(
        graph.ainvoke(
            {
                "user_id": USER_ID,
                "trip_id": TRIP_ID,
                "user_message_id": 11,
                "messages": [HumanMessage(content="帮我找杭州安静的自然景点")],
            }
        )
    )


def test_explore_injects_labeled_context_and_only_read_tools() -> None:
    """缺少上下文分区或误绑定写 Tool 都会破坏 Explore 的只读语义。"""
    model = PromptInspectingExploreModel()

    result = invoke_explore(
        model,
        [
            fake_web_search,
            fake_measure_travel_distance,
            forbidden_detailed_route,
            forbidden_site_map,
            forbidden_site_crawl,
            forbidden_context_write,
        ],
    )

    assert model.tool_names == [
        "web_search",
        "measure_travel_distance",
        "ask_user",
    ]
    assert isinstance(model.messages[0], SystemMessage)
    assert "旅行偏好" in str(model.messages[0].content)
    assert "第一天：杭州西湖" in str(model.messages[0].content)
    assert str(model.messages[1].content).startswith("【历史消息】")
    assert str(model.messages[2].content).startswith("【当前消息】")
    assert result["assistant_message"] == "可以考虑九溪、云栖竹径，并比较交通与人流。"


def test_explore_returns_tool_observation_to_agent() -> None:
    """查询结果必须作为 ToolMessage 回到 Agent，不能在 Tool 节点直接结束。"""
    result = invoke_explore(SearchUsingExploreModel(), [fake_web_search])

    assert result["assistant_message"] == "探索建议：九溪烟树适合自然漫步。"


def test_explore_ask_user_interrupts_and_resumes_in_parent_thread() -> None:
    """ask_user 的恢复答案必须以 ToolMessage 重新交给 Explore Agent。"""
    graph_module = import_module("tourism_agent.graph.subgraphs.explore.graph")
    explore_graph = graph_module.build_explore_graph(
        AskingExploreModel(),
        ExploreFakeRepository(),
    )
    parent = (
        StateGraph(ExploreState)
        .add_node("explore", explore_graph)
        .add_edge(START, "explore")
        .add_edge("explore", END)
        .compile(checkpointer=InMemorySaver())
    )
    config = {"configurable": {"thread_id": "explore-thread"}}
    graph_input = {
        "user_id": USER_ID,
        "trip_id": TRIP_ID,
        "user_message_id": 11,
        "messages": [HumanMessage(content="给我一些旅行方向")],
    }

    interrupted = asyncio.run(parent.ainvoke(graph_input, config))
    resumed = asyncio.run(parent.ainvoke(Command(resume="海边"), config))

    assert interrupted["__interrupt__"][0].value == {
        "kind": "ask_user",
        "question": "你更偏好山林还是海边？",
    }
    assert resumed["assistant_message"] == "我会优先探索：海边"


def test_explore_rejects_entire_batch_when_ask_user_is_mixed() -> None:
    """ask_user 混合调用时查询 Tool 也不得执行，避免 interrupt 留下半轮副作用。"""
    model = MixedAskExploreModel()

    result = invoke_explore(model, [fake_web_search])

    assert result["assistant_message"] == "我会先询问用户，再继续查询。"
    assert len(model.rejections) == 2
    assert all("必须独占一轮" in message for message in model.rejections)
