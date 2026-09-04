"""验证 Research 子图的上下文、研究循环、重规划和用户交互。"""

import asyncio
import logging
from datetime import UTC, datetime
from importlib import import_module
from typing import Any
from uuid import UUID

import pytest
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from pydantic import ValidationError

from tourism_agent.graph.subgraphs.research.state import ResearchPlan, ResearchState
from tourism_agent.models.context import ConversationMessage, ConversationRole
from tourism_agent.models.rag import ConversationChunkMatch

USER_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TRIP_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
HISTORY_EXCHANGE_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
RECENT_EXCHANGE_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")


class ResearchFakeRetrievalService:
    """返回固定语义历史，并记录 Research 自动召回参数。"""

    def __init__(self) -> None:
        self.call: tuple[UUID, UUID, str, int, list[UUID]] | None = None

    async def search(
        self,
        *,
        user_id: UUID,
        trip_id: UUID,
        query: str,
        limit: int = 5,
        exclude_exchange_ids: list[UUID] | None = None,
        **_enhancement_context: object,
    ) -> list[ConversationChunkMatch]:
        self.call = (
            user_id,
            trip_id,
            query,
            limit,
            exclude_exchange_ids or [],
        )
        return [
            ConversationChunkMatch(
                exchange_id=HISTORY_EXCHANGE_ID,
                retrieval_text="用户之前表示缺少冰雪路面驾驶经验。",
                similarity=0.94,
                created_at=datetime(2026, 8, 11, tzinfo=UTC),
            )
        ]


class ResearchFakeRepository:
    """提供稳定的只读业务快照，并记录 Research 的读取边界。"""

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
                id=20,
                role=ConversationRole.USER,
                content="我冬季自驾经验不多",
                created_at=created_at,
                exchange_id=RECENT_EXCHANGE_ID,
            )
        ]

    async def get_trip_context(self, _trip_id: UUID) -> dict[str, Any]:
        return {"同行人": "老人", "交通偏好": "自驾"}

    async def get_current_itinerary(self, _trip_id: UUID) -> str | None:
        return "第一天：成都出发前往康定"


def test_research_context_loads_recent_conversation_and_authoritative_snapshots() -> None:
    """历史条数错误或遗漏权威快照会让深度研究缺少关键约束。"""
    context_module = import_module("tourism_agent.services.research_context")
    repository = ResearchFakeRepository()
    builder = context_module.ResearchContextBuilder(repository)

    snapshot = asyncio.run(builder.build(TRIP_ID, before_message_id=21))

    assert repository.recent_query == (TRIP_ID, 21, 8)
    assert [message.content for message in snapshot.conversation_context] == [
        "我冬季自驾经验不多"
    ]
    assert snapshot.trip_context == {"同行人": "老人", "交通偏好": "自驾"}
    assert snapshot.current_itinerary == "第一天：成都出发前往康定"


def make_plan(goal: str = "判断冬季川西自驾是否适合当前用户") -> ResearchPlan:
    """创建满足工作流契约的固定研究计划。"""
    return ResearchPlan(
        goal=goal,
        tasks=[
            "核实主要道路的冬季风险",
            "调查所需驾驶经验（补充：关注冰雪路面）",
            "比较可用的替代交通",
        ],
        source_strategy=["官方道路信息", "官方天气信息", "近期旅行经验"],
        success_criteria=["核实主要风险", "说明信息缺口"],
        notes="道路信息具有时效性，应优先采用近期官方来源。",
    )


def test_research_plan_accepts_two_tasks_and_overall_notes() -> None:
    """若仍强制三个问题或缺少备注字段，简单明确的研究计划会被无意义地拆分。"""
    plan = ResearchPlan(
        goal="判断指定日期是否适合游览宝墨园",
        tasks=["核实逐日天气", "结合景区环境评估游览适宜性"],
        source_strategy=["官方天气服务", "景区官方信息"],
        success_criteria=["能够给出逐日建议"],
        notes="天气预报超出可靠范围时应明确说明限制。",
    )

    assert plan.tasks == ["核实逐日天气", "结合景区环境评估游览适宜性"]
    assert plan.notes == "天气预报超出可靠范围时应明确说明限制。"


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("goal", " "),
        ("tasks", ["", "核实交通费用"]),
        ("tasks", [", ", "核实交通费用"]),
        ("source_strategy", []),
        ("source_strategy", [", "]),
        ("success_criteria", []),
        ("success_criteria", ["--"]),
    ],
)
def test_research_plan_rejects_empty_or_placeholder_content(
    field_name: str,
    invalid_value: object,
) -> None:
    """空字符串、纯标点或空清单会让 ResearchPlan 失去实际指导作用。"""
    plan_data = make_plan().model_dump()
    plan_data[field_name] = invalid_value

    with pytest.raises(ValidationError):
        ResearchPlan.model_validate(plan_data)


def test_research_plan_allows_empty_overall_notes() -> None:
    """整体备注没有内容时仍应使用空字符串，不应制造无意义占位文字。"""
    plan_data = make_plan().model_dump()
    plan_data["notes"] = ""

    plan = ResearchPlan.model_validate(plan_data)

    assert plan.notes == ""


@tool("web_search")
def fake_web_search(query: str) -> str:
    """返回固定搜索证据，验证 Researcher 的 ReAct 闭环。"""
    assert query == "川西冬季道路风险"
    return "来源：https://example.com/road；冬季部分路段可能结冰。"


@tool("update_trip_context")
def forbidden_context_write(patch: dict[str, Any]) -> str:
    """模拟不得进入 Research 白名单的业务写 Tool。"""
    raise AssertionError(f"Research 不得执行写 Tool：{patch}")


@tool("plan_route")
def fake_plan_route(origin: str, destination: str, mode: str) -> str:
    """模拟 Research 用于核查可达性的路线 Tool。"""
    return f"{origin}-{destination}-{mode}"


@tool("measure_travel_distance")
def fake_measure_travel_distance(origins: list[str], destination: str) -> str:
    """模拟 Research 用于比较交通成本的距离 Tool。"""
    return f"{','.join(origins)}-{destination}"


@tool("map_web_site")
def fake_map_web_site(url: str, instructions: str = "") -> str:
    """模拟 Research 获准使用的网站结构发现 Tool。"""
    return f"{url}-{instructions}"


@tool("crawl_web_site")
def fake_crawl_web_site(url: str, instructions: str = "") -> str:
    """模拟 Research 获准使用的站内抓取 Tool。"""
    return f"{url}-{instructions}"


class ThreeStageResearchModel:
    """分别记录 Planner、Researcher 和 Synthesis 收到的上下文。"""

    def __init__(self) -> None:
        self.planner_messages: list[BaseMessage] = []
        self.researcher_messages: list[BaseMessage] = []
        self.synthesis_messages: list[BaseMessage] = []
        self.tool_names: list[str] = []

    def with_structured_output(self, schema: type[ResearchPlan]) -> RunnableLambda:
        def plan(messages: list[BaseMessage]) -> ResearchPlan:
            self.planner_messages = messages
            return schema(**make_plan().model_dump())

        return RunnableLambda(plan)

    def bind_tools(self, tools: list[BaseTool]) -> RunnableLambda:
        self.tool_names = [item.name for item in tools]

        def research(messages: list[BaseMessage]) -> AIMessage:
            self.researcher_messages = messages
            observations = [item for item in messages if isinstance(item, ToolMessage)]
            if observations:
                return AIMessage(content="证据采集完成")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "web_search",
                        "args": {"query": "川西冬季道路风险"},
                        "id": "research-search-1",
                        "type": "tool_call",
                    }
                ],
            )

        return RunnableLambda(research)

    def with_config(self, **_kwargs: Any) -> RunnableLambda:
        def synthesize(messages: list[BaseMessage]) -> AIMessage:
            self.synthesis_messages = messages
            return AIMessage(
                content=(
                    "## 结论摘要\n不建议经验不足的用户在结冰风险较高时自驾。\n\n"
                    "## 信息来源\nhttps://example.com/road"
                )
            )

        return RunnableLambda(synthesize)


def invoke_research(
    model: object,
    query_tools: list[BaseTool] | None = None,
    retrieval_service: object | None = None,
) -> dict:
    """以固定业务作用域运行一次 Research，减少测试启动噪音。"""
    graph_module = import_module("tourism_agent.graph.subgraphs.research.graph")
    graph = graph_module.build_research_graph(
        model,
        ResearchFakeRepository(),
        query_tools or [],
        retrieval_service=retrieval_service,
    )
    return asyncio.run(
        graph.ainvoke(
            {
                "user_id": USER_ID,
                "trip_id": TRIP_ID,
                "user_message_id": 21,
                "messages": [HumanMessage(content="深入研究冬季川西自驾是否合适")],
                "retrieval_query": "Research专用检索查询",
            }
        )
    )


def test_research_uses_three_model_stages_and_synthesizes_tool_evidence() -> None:
    """若调查结果绕过独立综合节点，最终报告会退化成 Researcher 的阶段性文本。"""
    model = ThreeStageResearchModel()
    retrieval_service = ResearchFakeRetrievalService()
    history_tools_module = import_module(
        "tourism_agent.graph.tools.conversation_history"
    )
    history_tools = history_tools_module.create_conversation_history_tools(
        retrieval_service
    )

    result = invoke_research(
        model,
        [
            fake_web_search,
            fake_plan_route,
            fake_measure_travel_distance,
            fake_map_web_site,
            fake_crawl_web_site,
            forbidden_context_write,
            *history_tools,
        ],
        retrieval_service,
    )

    assert model.tool_names == [
        "web_search",
        "plan_route",
        "measure_travel_distance",
        "map_web_site",
        "crawl_web_site",
        "search_conversation_history",
        "read_conversation_exchanges",
        "ask_user",
        "revise_research_plan",
    ]
    assert isinstance(model.planner_messages[0], SystemMessage)
    assert retrieval_service.call == (
        USER_ID,
        TRIP_ID,
        "Research专用检索查询",
        3,
        [RECENT_EXCHANGE_ID],
    )
    assert str(model.planner_messages[1].content).startswith("【历史消息】")
    assert str(model.planner_messages[2].content).startswith("【当前消息】")
    assert any(isinstance(item, ToolMessage) for item in model.synthesis_messages)
    assert result["research_plan"].tasks[1] == "调查所需驾驶经验（补充：关注冰雪路面）"
    assert result["research_plan"].notes == "道路信息具有时效性，应优先采用近期官方来源。"
    assert result["assistant_message"].startswith("## 结论摘要")
    assert result["assistant_message"] != "证据采集完成"


def test_research_logs_complete_plan(caplog: Any) -> None:
    """若日志只保留目标和任务数量，调试时无法还原 Planner 的完整输出。"""
    caplog.set_level(
        logging.INFO,
        logger="tourism_agent.graph.subgraphs.research.graph",
    )

    invoke_research(ThreeStageResearchModel(), [fake_web_search])

    plan_log = next(
        record.getMessage()
        for record in caplog.records
        if "Research规划完成" in record.getMessage()
    )
    assert 'research_plan={"goal":"判断冬季川西自驾是否适合当前用户"' in plan_log
    assert '"tasks":["核实主要道路的冬季风险"' in plan_log
    assert '"source_strategy":["官方道路信息"' in plan_log
    assert '"success_criteria":["核实主要风险"' in plan_log
    assert '"notes":"道路信息具有时效性，应优先采用近期官方来源。"}' in plan_log


class InvalidPlanThenSuccessResearchModel(ThreeStageResearchModel):
    """首次返回字段不合格的计划，第二次返回正常计划。"""

    def __init__(self) -> None:
        super().__init__()
        self.planner_calls = 0
        self.planner_attempts: list[list[BaseMessage]] = []

    def with_structured_output(self, schema: type[ResearchPlan]) -> RunnableLambda:
        def plan(messages: list[BaseMessage]) -> ResearchPlan:
            self.planner_calls += 1
            self.planner_attempts.append(messages)
            if self.planner_calls == 1:
                return schema.model_validate(
                    {
                        "goal": "原始无效输出不得进入日志或重试提示 PRIVATE_PLAN_VALUE",
                        "tasks": [": ", ": "],
                        "source_strategy": [": "],
                        "success_criteria": [": "],
                        "notes": "",
                    }
                )
            return schema.model_validate(make_plan().model_dump())

        return RunnableLambda(plan)


def test_research_retries_once_after_invalid_structured_plan() -> None:
    """字段校验失败时应给 Planner 一次修正机会，而不是直接终止整轮研究。"""
    model = InvalidPlanThenSuccessResearchModel()

    result = invoke_research(model, [fake_web_search])

    assert model.planner_calls == 2
    retry_feedback = model.planner_attempts[1][-1]
    assert isinstance(retry_feedback, SystemMessage)
    assert "ResearchPlan 未通过校验" in str(retry_feedback.content)
    assert "tasks.0" in str(retry_feedback.content)
    assert "PRIVATE_PLAN_VALUE" not in str(retry_feedback.content)
    assert result["research_plan"].goal == "判断冬季川西自驾是否适合当前用户"


class InvalidPlanTwiceResearchModel(ThreeStageResearchModel):
    """两次返回不同字段错误的计划，用于验证重试上限。"""

    def __init__(self) -> None:
        super().__init__()
        self.planner_calls = 0

    def with_structured_output(self, schema: type[ResearchPlan]) -> RunnableLambda:
        def plan(_messages: list[BaseMessage]) -> ResearchPlan:
            self.planner_calls += 1
            if self.planner_calls == 1:
                return schema.model_validate(
                    {
                        "goal": "核实冬季川西自驾风险",
                        "tasks": [": ", ": "],
                        "source_strategy": ["官方道路信息"],
                        "success_criteria": ["确认主要风险"],
                        "notes": "",
                    }
                )
            return schema.model_validate(
                {
                    "goal": ": ",
                    "tasks": ["核实主要道路的冬季风险", "调查驾驶所需经验"],
                    "source_strategy": ["官方道路信息"],
                    "success_criteria": ["确认主要风险"],
                    "notes": "",
                }
            )

        return RunnableLambda(plan)


def test_research_propagates_second_invalid_structured_plan() -> None:
    """第二次仍无效时不得继续重试或吞掉真实校验异常。"""
    model = InvalidPlanTwiceResearchModel()

    with pytest.raises(ValidationError, match="goal"):
        invoke_research(model, [fake_web_search])

    assert model.planner_calls == 2


class MixedToolResearchModel(ThreeStageResearchModel):
    """故意混用独占 Tool 和查询 Tool，验证整批拒绝。"""

    def __init__(self) -> None:
        super().__init__()
        self.rejections: list[str] = []

    def bind_tools(self, tools: list[BaseTool]) -> RunnableLambda:
        self.tool_names = [item.name for item in tools]

        def research(messages: list[BaseMessage]) -> AIMessage:
            observations = [item for item in messages if isinstance(item, ToolMessage)]
            if observations:
                self.rejections = [str(item.content) for item in observations]
                return AIMessage(content="停止查询并交给综合节点")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "web_search",
                        "args": {"query": "川西冬季道路风险"},
                        "id": "mixed-search-1",
                        "type": "tool_call",
                    },
                    {
                        "name": "ask_user",
                        "args": {"question": "你是否有冰雪驾驶经验？"},
                        "id": "mixed-ask-1",
                        "type": "tool_call",
                    },
                ],
            )

        return RunnableLambda(research)


def test_research_rejects_entire_batch_when_exclusive_tool_is_mixed() -> None:
    """独占 Tool 混用时任何查询都不得执行，避免暂停前产生半轮结果。"""
    model = MixedToolResearchModel()

    invoke_research(model, [fake_web_search])

    assert len(model.rejections) == 2
    assert all("必须独占一轮" in message for message in model.rejections)


class AskingResearchModel(ThreeStageResearchModel):
    """先询问关键驾驶经验，再让综合节点使用恢复答案。"""

    def bind_tools(self, tools: list[BaseTool]) -> RunnableLambda:
        self.tool_names = [item.name for item in tools]

        def research(messages: list[BaseMessage]) -> AIMessage:
            answers = [item for item in messages if isinstance(item, ToolMessage)]
            if answers:
                return AIMessage(content=f"用户补充：{answers[-1].content}")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "ask_user",
                        "args": {"question": "你是否有冰雪路面驾驶经验？"},
                        "id": "research-ask-1",
                        "type": "tool_call",
                    }
                ],
            )

        return RunnableLambda(research)

    def with_config(self, **_kwargs: Any) -> RunnableLambda:
        def synthesize(messages: list[BaseMessage]) -> AIMessage:
            answer = next(
                str(item.content)
                for item in messages
                if isinstance(item, ToolMessage) and item.tool_call_id == "research-ask-1"
            )
            return AIMessage(content=f"研究结论已结合用户回答：{answer}")

        return RunnableLambda(synthesize)


def test_research_ask_user_interrupts_and_resumes_in_parent_thread() -> None:
    """恢复答案必须作为 ToolMessage 交回 Researcher 和最终综合节点。"""
    graph_module = import_module("tourism_agent.graph.subgraphs.research.graph")
    research_graph = graph_module.build_research_graph(
        AskingResearchModel(),
        ResearchFakeRepository(),
    )
    parent = (
        StateGraph(ResearchState)
        .add_node("research", research_graph)
        .add_edge(START, "research")
        .add_edge("research", END)
        .compile(checkpointer=InMemorySaver())
    )
    config = {"configurable": {"thread_id": "research-thread"}}
    graph_input = {
        "user_id": USER_ID,
        "trip_id": TRIP_ID,
        "user_message_id": 21,
        "messages": [HumanMessage(content="深入研究冬季川西自驾是否合适")],
    }

    interrupted = asyncio.run(parent.ainvoke(graph_input, config))
    resumed = asyncio.run(parent.ainvoke(Command(resume="没有"), config))

    assert interrupted["__interrupt__"][0].value == {
        "kind": "ask_user",
        "question": "你是否有冰雪路面驾驶经验？",
    }
    assert resumed["assistant_message"] == "研究结论已结合用户回答：没有"


class ReplanningResearchModel(ThreeStageResearchModel):
    """持续请求重规划，用于验证确定性上限。"""

    def __init__(self) -> None:
        super().__init__()
        self.plan_calls = 0

    def with_structured_output(self, schema: type[ResearchPlan]) -> RunnableLambda:
        def plan(_messages: list[BaseMessage]) -> ResearchPlan:
            self.plan_calls += 1
            return schema(**make_plan(goal=f"第{self.plan_calls}版研究目标").model_dump())

        return RunnableLambda(plan)

    def bind_tools(self, tools: list[BaseTool]) -> RunnableLambda:
        self.tool_names = [item.name for item in tools]

        def research(messages: list[BaseMessage]) -> AIMessage:
            observations = [item for item in messages if isinstance(item, ToolMessage)]
            if observations and "已达到" in str(observations[-1].content):
                return AIMessage(content="按现有证据结束")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "revise_research_plan",
                        "args": {"reason": "发现关键道路前提变化"},
                        "id": f"replan-{len(observations) + 1}",
                        "type": "tool_call",
                    }
                ],
            )

        return RunnableLambda(research)


def test_research_replanning_is_limited_to_two_successful_revisions() -> None:
    """缺少确定性上限会让持续要求改计划的模型形成无限循环。"""
    model = ReplanningResearchModel()

    result = invoke_research(model)

    assert model.plan_calls == 3
    assert result["plan_revision_count"] == 2
    assert result["research_plan"].goal == "第3版研究目标"
