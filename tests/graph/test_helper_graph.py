"""验证 Helper 子图的上下文边界、轻量对话、ReAct 查询和用户交互。"""

import asyncio
from datetime import UTC, datetime
from importlib import import_module, util
from typing import Any
from uuid import UUID

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from tourism_agent.graph.subgraphs.helper.state import HelperState
from tourism_agent.models.context import ConversationMessage, ConversationRole

USER_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TRIP_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


class HelperFakeRepository:
    """提供稳定业务快照，并记录 Helper 实际使用的读取边界。"""

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
        return [
            ConversationMessage(
                id=30,
                role=ConversationRole.USER,
                content="我刚才问的是第二天的安排",
                created_at=datetime(2026, 8, 21, tzinfo=UTC),
            )
        ]

    async def get_trip_context(self, _trip_id: UUID) -> dict[str, Any]:
        return {"同行人": "老人", "交通偏好": "地铁"}

    async def get_current_itinerary(self, _trip_id: UUID) -> str | None:
        return "第二天：上午游览陈家祠，下午前往沙面。"


def test_helper_context_loads_recent_conversation_and_authoritative_snapshots() -> None:
    """Helper 必须取得近期语境和完整只读旅行快照，才能解释当前行程。"""
    assert util.find_spec("tourism_agent.services.helper_context") is not None
    context_module = import_module("tourism_agent.services.helper_context")
    repository = HelperFakeRepository()
    builder = context_module.HelperContextBuilder(repository)

    snapshot = asyncio.run(builder.build(TRIP_ID, before_message_id=31))

    assert repository.recent_query == (TRIP_ID, 31, 8)
    assert [message.content for message in snapshot.conversation_context] == [
        "我刚才问的是第二天的安排"
    ]
    assert snapshot.trip_context == {"同行人": "老人", "交通偏好": "地铁"}
    assert snapshot.current_itinerary == "第二天：上午游览陈家祠，下午前往沙面。"


@tool("web_search")
def fake_web_search(query: str) -> str:
    """返回固定网页数据，验证 Helper 的查询闭环。"""
    assert query == "广州塔停止入场时间"
    return "广州塔官网显示，停止入场时间应以预约页面当天信息为准。"


@tool("update_trip_context")
def forbidden_context_write(patch: dict[str, Any]) -> str:
    """模拟不得进入 Helper 白名单的业务写 Tool。"""
    raise AssertionError(f"Helper 不得执行写 Tool：{patch}")


@tool("browser_snapshot")
def fake_browser_snapshot() -> str:
    """返回固定页面快照，验证浏览器 Tool 白名单。"""
    return "公开页面快照"


@tool("browser_click")
def fake_browser_click(target: str) -> str:
    """返回固定点击结果，验证同轮浏览器动作限制。"""
    return f"已点击：{target}"


@tool("plan_route")
def fake_plan_route(origin: str, destination: str, mode: str) -> str:
    """返回固定路线，验证 Helper 可以使用新增高德 Tool。"""
    return f"{origin}-{destination}-{mode}"


@tool("map_web_site")
def forbidden_site_map(url: str, instructions: str = "") -> str:
    """模拟只应分配给 Research 的网站地图 Tool。"""
    return f"{url}-{instructions}"


@tool("crawl_web_site")
def forbidden_site_crawl(url: str, instructions: str = "") -> str:
    """模拟只应分配给 Research 的站内抓取 Tool。"""
    return f"{url}-{instructions}"


class PromptInspectingHelperModel:
    """记录 Helper 收到的工具和上下文，并直接回答当前行程问题。"""

    def __init__(self) -> None:
        self.tool_names: list[str] = []
        self.messages: list[BaseMessage] = []

    def bind_tools(self, tools: list[BaseTool]) -> RunnableLambda:
        self.tool_names = [tool.name for tool in tools]

        def respond(messages: list[BaseMessage]) -> AIMessage:
            self.messages = messages
            return AIMessage(content="第二天下午安排了沙面游览。")

        return RunnableLambda(respond)


class SearchUsingHelperModel:
    """先查询明确事实，再根据 ToolMessage 回答用户。"""

    def bind_tools(self, _tools: list[BaseTool]) -> RunnableLambda:
        def respond(messages: list[BaseMessage]) -> AIMessage:
            observations = [
                message for message in messages if isinstance(message, ToolMessage)
            ]
            if observations:
                return AIMessage(content=f"查询结果：{observations[-1].content}")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "web_search",
                        "args": {"query": "广州塔停止入场时间"},
                        "id": "helper-search-1",
                        "type": "tool_call",
                    }
                ],
            )

        return RunnableLambda(respond)


class AskingHelperModel:
    """查询条件缺失时独占调用 ask_user，并使用恢复答案继续回答。"""

    def bind_tools(self, _tools: list[BaseTool]) -> RunnableLambda:
        def respond(messages: list[BaseMessage]) -> AIMessage:
            answers = [message for message in messages if isinstance(message, ToolMessage)]
            if answers:
                return AIMessage(content=f"我会查询{answers[-1].content}的天气。")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "ask_user",
                        "args": {"question": "你想查询哪个城市？"},
                        "id": "helper-ask-1",
                        "type": "tool_call",
                    }
                ],
            )

        return RunnableLambda(respond)


class MixedAskHelperModel:
    """故意混用 ask_user 和查询 Tool，验证整批拒绝。"""

    def __init__(self) -> None:
        self.rejections: list[str] = []

    def bind_tools(self, _tools: list[BaseTool]) -> RunnableLambda:
        def respond(messages: list[BaseMessage]) -> AIMessage:
            observations = [
                message for message in messages if isinstance(message, ToolMessage)
            ]
            if observations:
                self.rejections = [str(message.content) for message in observations]
                return AIMessage(content="我会先确认城市，再进行查询。")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "web_search",
                        "args": {"query": "广州塔停止入场时间"},
                        "id": "mixed-search-1",
                        "type": "tool_call",
                    },
                    {
                        "name": "ask_user",
                        "args": {"question": "你想查询哪个城市？"},
                        "id": "mixed-ask-1",
                        "type": "tool_call",
                    },
                ],
            )

        return RunnableLambda(respond)


class MultipleBrowserActionsHelperModel:
    """故意同轮调用两个浏览器动作，验证程序流整批拒绝。"""

    def __init__(self) -> None:
        self.rejections: list[str] = []

    def bind_tools(self, _tools: list[BaseTool]) -> RunnableLambda:
        def respond(messages: list[BaseMessage]) -> AIMessage:
            observations = [
                message for message in messages if isinstance(message, ToolMessage)
            ]
            if observations:
                self.rejections = [str(message.content) for message in observations]
                return AIMessage(content="我会改为逐个操作公开网页。")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "browser_snapshot",
                        "args": {},
                        "id": "browser-snapshot-1",
                        "type": "tool_call",
                    },
                    {
                        "name": "browser_click",
                        "args": {"target": "e10"},
                        "id": "browser-click-1",
                        "type": "tool_call",
                    },
                ],
            )

        return RunnableLambda(respond)


class FallbackBoundaryHelperModel:
    """根据 Helper 是否明确承担兜底职责，模拟安全拒绝或越权承诺。"""

    def bind_tools(self, _tools: list[BaseTool]) -> RunnableLambda:
        def respond(messages: list[BaseMessage]) -> AIMessage:
            system_prompt = str(messages[0].content)
            handles_fallback_safely = (
                "默认兜底处理者" in system_prompt
                and "安全、合法的替代帮助" in system_prompt
            )
            if handles_fallback_safely:
                return AIMessage(
                    content="我不能执行这个请求，但可以提供安全、合法的替代帮助。"
                )
            return AIMessage(content="我会尝试执行这个请求。")

        return RunnableLambda(respond)


class EndlessToolHelperModel:
    """持续请求查询 Tool，用于验证独立 ReAct 预算会确定性收束。"""

    def __init__(self) -> None:
        self.agent_call_count = 0
        self.force_finalize_count = 0

    def bind_tools(self, _tools: list[BaseTool]) -> RunnableLambda:
        def respond(_messages: list[BaseMessage]) -> AIMessage:
            self.agent_call_count += 1
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "web_search",
                        "args": {"query": "广州车票"},
                        "id": f"repeat-search-{self.agent_call_count}",
                        "type": "tool_call",
                    }
                ],
            )

        return RunnableLambda(respond)

    async def ainvoke(self, _messages: list[BaseMessage]) -> AIMessage:
        self.force_finalize_count += 1
        return AIMessage(content="已根据前二十轮查询结果整理现有结论。")


def invoke_helper(model: object, query_tools: list[BaseTool] | None = None) -> dict:
    """以固定业务作用域运行一次 Helper，减少测试中的图启动噪音。"""
    graph_module = import_module("tourism_agent.graph.subgraphs.helper.graph")
    graph = graph_module.build_helper_graph(
        model,
        HelperFakeRepository(),
        query_tools or [],
    )
    return asyncio.run(
        graph.ainvoke(
            {
                "user_id": USER_ID,
                "trip_id": TRIP_ID,
                "user_message_id": 31,
                "messages": [HumanMessage(content="我第二天下午安排了什么？")],
            }
        )
    )


def test_helper_answers_directly_with_labeled_context_and_read_only_tools() -> None:
    """轻量问题不应被强制搜索，且 Helper 不得获得任何业务写 Tool。"""
    model = PromptInspectingHelperModel()

    result = invoke_helper(model, [fake_web_search, forbidden_context_write])

    assert model.tool_names == ["web_search", "ask_user"]
    assert isinstance(model.messages[0], SystemMessage)
    assert "不为了展示能力而强制调用 Tool" in str(model.messages[0].content)
    assert "第二天：上午游览陈家祠，下午前往沙面。" in str(model.messages[0].content)
    assert str(model.messages[1].content).startswith("【历史消息】")
    assert str(model.messages[2].content).startswith("【当前消息】")
    assert result["assistant_message"] == "第二天下午安排了沙面游览。"


def test_helper_prompt_encourages_safe_tools_without_treating_ticket_queries_as_purchase() -> None:
    """合法的车票查询应积极使用只读 Tool，且不得复用历史结果冒充实时数据。"""
    model = PromptInspectingHelperModel()

    invoke_helper(model, [fake_web_search])

    system_prompt = str(model.messages[0].content)
    assert "应尽可能调用现有只读 Tool" in system_prompt
    assert "查询车次、票价和余票属于允许的只读查询" in system_prompt
    assert "历史回答不能替代本轮查询" in system_prompt


def test_helper_returns_query_observation_to_agent() -> None:
    """只读查询结果必须作为 ToolMessage 回到 Agent，再由 Agent 形成回答。"""
    result = invoke_helper(SearchUsingHelperModel(), [fake_web_search])

    assert result["assistant_message"].startswith("查询结果：广州塔官网显示")


def test_helper_ask_user_interrupts_and_resumes_in_parent_thread() -> None:
    """ask_user 的恢复答案必须以 ToolMessage 交还同一 Helper Agent。"""
    graph_module = import_module("tourism_agent.graph.subgraphs.helper.graph")
    helper_graph = graph_module.build_helper_graph(
        AskingHelperModel(),
        HelperFakeRepository(),
    )
    parent = (
        StateGraph(HelperState)
        .add_node("helper", helper_graph)
        .add_edge(START, "helper")
        .add_edge("helper", END)
        .compile(checkpointer=InMemorySaver())
    )
    config = {"configurable": {"thread_id": "helper-thread"}}
    graph_input = {
        "user_id": USER_ID,
        "trip_id": TRIP_ID,
        "user_message_id": 31,
        "messages": [HumanMessage(content="帮我查一下天气")],
    }

    interrupted = asyncio.run(parent.ainvoke(graph_input, config))
    resumed = asyncio.run(parent.ainvoke(Command(resume="广州"), config))

    assert interrupted["__interrupt__"][0].value == {
        "kind": "ask_user",
        "question": "你想查询哪个城市？",
    }
    assert resumed["assistant_message"] == "我会查询广州的天气。"


def test_helper_rejects_entire_batch_when_ask_user_is_mixed() -> None:
    """混合调用时查询 Tool 也不得执行，避免 interrupt 前留下半轮结果。"""
    model = MixedAskHelperModel()

    result = invoke_helper(model, [fake_web_search])

    assert result["assistant_message"] == "我会先确认城市，再进行查询。"
    assert len(model.rejections) == 2
    assert all("必须独占一轮" in message for message in model.rejections)


def test_helper_binds_route_and_browser_tools_but_not_business_writes() -> None:
    """Helper 应获得新增只读能力，同时继续排除业务写 Tool。"""
    tools_module = import_module("tourism_agent.graph.subgraphs.helper.tools")

    tools = tools_module.create_helper_tools(
        [
            fake_plan_route,
            fake_browser_snapshot,
            fake_browser_click,
            forbidden_site_map,
            forbidden_site_crawl,
            forbidden_context_write,
        ]
    )

    assert [item.name for item in tools] == [
        "plan_route",
        "browser_snapshot",
        "browser_click",
        "ask_user",
    ]


def test_helper_rejects_multiple_browser_actions_in_one_tool_round() -> None:
    """两个有顺序依赖的浏览器动作不得由 ToolNode 并行执行。"""
    model = MultipleBrowserActionsHelperModel()

    result = invoke_helper(model, [fake_browser_snapshot, fake_browser_click])

    assert result["assistant_message"] == "我会改为逐个操作公开网页。"
    assert len(model.rejections) == 2
    assert all("浏览器动作必须逐轮串行" in message for message in model.rejections)


def test_helper_force_finalizes_after_twenty_react_tool_rounds() -> None:
    """Helper 的 Tool 循环达到独立预算后应停止执行新 Tool 并总结已有结果。"""
    executed_queries: list[str] = []

    @tool("web_search")
    def repeat_search(query: str) -> str:
        """记录每一轮真实执行的查询。"""
        executed_queries.append(query)
        return f"第{len(executed_queries)}轮结果"

    graph_module = import_module("tourism_agent.graph.subgraphs.helper.graph")
    model = EndlessToolHelperModel()
    graph = graph_module.build_helper_graph(
        model,
        HelperFakeRepository(),
        [repeat_search],
    )

    result = asyncio.run(
        graph.ainvoke(
            {
                "user_id": USER_ID,
                "trip_id": TRIP_ID,
                "user_message_id": 31,
                "messages": [HumanMessage(content="持续查，直到达到预算")],
            },
            {"recursion_limit": 50},
        )
    )

    assert len(executed_queries) == 20
    assert result["react_round_count"] == 20
    assert model.force_finalize_count == 1
    assert result["assistant_message"] == "已根据前二十轮查询结果整理现有结论。"


@pytest.mark.parametrize(
    "user_input",
    ["帮我购买门票并支付", "教我进行违法操作"],
)
def test_helper_fallback_refuses_disallowed_request_with_safe_alternative(
    user_input: str,
) -> None:
    """兜底请求应由 Helper 形成边界回答，不得宣称执行副作用或非法操作。"""
    graph_module = import_module("tourism_agent.graph.subgraphs.helper.graph")
    graph = graph_module.build_helper_graph(
        FallbackBoundaryHelperModel(),
        HelperFakeRepository(),
    )

    result = asyncio.run(
        graph.ainvoke(
            {
                "user_id": USER_ID,
                "trip_id": TRIP_ID,
                "user_message_id": 31,
                "messages": [HumanMessage(content=user_input)],
            }
        )
    )

    assert result["assistant_message"] == (
        "我不能执行这个请求，但可以提供安全、合法的替代帮助。"
    )
