"""验证根图使用少量历史对话理解当前待路由消息。"""

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableLambda

from tourism_agent.graph.root import build_root_graph
from tourism_agent.models.context import ConversationMessage, ConversationRole
from tourism_agent.models.orchestration import (
    OrchestrationPlan,
    PlanReviewDecision,
    TaskSpec,
    TaskType,
)

USER_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TRIP_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


class RoutingContextRepository:
    """返回当前消息之前的历史，并记录根图使用的查询边界。"""

    def __init__(self) -> None:
        self.recent_queries: list[tuple[UUID, int, int]] = []

    async def get_recent_conversation(
        self,
        trip_id: UUID,
        *,
        before_message_id: int,
        limit: int,
    ) -> list[ConversationMessage]:
        self.recent_queries.append((trip_id, before_message_id, limit))
        created_at = datetime(2026, 8, 19, tzinfo=UTC)
        return [
            ConversationMessage(
                id=38,
                role=ConversationRole.USER,
                content="给我一些适合放空的海岛旅行灵感",
                created_at=created_at,
            ),
            ConversationMessage(
                id=39,
                role=ConversationRole.ASSISTANT,
                content="可以考虑涠洲岛或东山岛。",
                created_at=created_at,
            ),
        ]

    async def get_trip_context(self, _trip_id: UUID) -> dict[str, object]:
        return {}

    async def get_current_itinerary(self, _trip_id: UUID) -> str | None:
        return None


class BoundaryAwareRoutingModel:
    """只有明确识别历史区和当前消息区时才采用历史语义。"""

    def with_structured_output(self, schema: type[Any]) -> RunnableLambda:
        def decide(messages: list[BaseMessage]) -> Any:
            if schema is PlanReviewDecision:
                return schema(action="finish", reason="当前任务已经完成")
            system_prompt = str(messages[0].content)
            history = messages[1:-1]
            current = str(messages[-1].content)
            boundaries_are_clear = (
                "历史消息" in system_prompt
                and "当前消息" in system_prompt
                and "只能处理最后一条" in system_prompt
                and all(str(message.content).startswith("【历史消息】") for message in history)
                and current.startswith("【当前消息】")
            )
            has_explore_context = any(
                "海岛旅行灵感" in str(message.content) for message in history
            )
            task_type = (
                TaskType.EXPLORE
                if boundaries_are_clear and has_explore_context and "就按这个" in current
                else TaskType.HELPER
            )
            return OrchestrationPlan(
                goal="继续处理上一轮旅行话题",
                tasks=[
                    TaskSpec(
                        task_id="task_1",
                        task_type=task_type,
                        instruction="根据历史语义处理当前请求",
                    )
                ],
            )

        return RunnableLambda(decide)

    def bind_tools(self, _tools: list[object]) -> RunnableLambda:
        return RunnableLambda(lambda _messages: AIMessage(content="未进入 Planning"))

    def with_config(self, **_kwargs: Any) -> RunnableLambda:
        return RunnableLambda(lambda _messages: AIMessage(content="已完成当前请求"))


def test_root_routes_current_message_with_clearly_labeled_recent_history() -> None:
    """根图必须查询当前消息之前四条记录，并明确标注历史与当前消息。"""
    repository = RoutingContextRepository()
    graph = build_root_graph(BoundaryAwareRoutingModel(), repository)

    result = asyncio.run(
        graph.ainvoke(
            {
                "user_id": USER_ID,
                "trip_id": TRIP_ID,
                "user_message_id": 40,
                "user_input": "就按这个继续",
            },
            {"configurable": {"thread_id": str(TRIP_ID)}},
        )
    )

    assert repository.recent_queries == [
        (TRIP_ID, 40, 4),
        (TRIP_ID, 40, 8),
    ]
    assert result["route"] is TaskType.EXPLORE


class ResearchRoutingModel:
    """验证根图能把深度调查请求交给完整 Research 子图。"""

    def with_structured_output(self, schema: type[Any]) -> RunnableLambda:
        def respond(_messages: list[BaseMessage]) -> Any:
            if schema is OrchestrationPlan:
                return schema(
                    goal="核实冬季川西自驾安全性",
                    tasks=[
                        TaskSpec(
                            task_id="task_1",
                            task_type="research",
                            instruction="深入调查冬季川西自驾安全性",
                        )
                    ],
                )
            if schema is PlanReviewDecision:
                return schema(action="finish", reason="调查任务已经完成")
            return schema(
                goal="核实冬季川西自驾安全性",
                tasks=["核实道路风险", "调查驾驶要求", "比较替代交通"],
                source_strategy=["官方道路信息", "官方天气信息"],
                success_criteria=["形成风险结论", "说明不确定性"],
                notes="道路信息具有时效性。",
            )

        return RunnableLambda(respond)

    def bind_tools(self, _tools: list[object]) -> RunnableLambda:
        return RunnableLambda(lambda _messages: AIMessage(content="已有证据足以综合"))

    def with_config(self, **_kwargs: Any) -> RunnableLambda:
        return RunnableLambda(
            lambda _messages: AIMessage(content="## 结论摘要\n冬季自驾存在结冰风险。")
        )


def test_root_routes_research_request_and_maps_report_to_response() -> None:
    """缺少 Research 根图映射会让已实现子图仍无法从 API 主链路到达。"""
    repository = RoutingContextRepository()
    graph = build_root_graph(ResearchRoutingModel(), repository)

    result = asyncio.run(
        graph.ainvoke(
            {
                "user_id": USER_ID,
                "trip_id": TRIP_ID,
                "user_message_id": 40,
                "user_input": "深入研究冬季川西自驾是否安全",
            },
            {"configurable": {"thread_id": "research-root-thread"}},
        )
    )

    assert result["route"].value == "research"
    assert result["response"] == "## 结论摘要\n冬季自驾存在结冰风险。"
    assert repository.recent_queries == [
        (TRIP_ID, 40, 4),
        (TRIP_ID, 40, 8),
    ]


class HelperRoutingModel:
    """验证根图能把轻量查询交给真实 Helper 子图。"""

    def with_structured_output(self, schema: type[Any]) -> RunnableLambda:
        def respond(_messages: list[BaseMessage]) -> Any:
            if schema is OrchestrationPlan:
                return schema(
                    goal="查询广州塔停止入场时间",
                    tasks=[
                        TaskSpec(
                            task_id="task_1",
                            task_type="helper",
                            instruction="回答广州塔停止入场时间",
                        )
                    ],
                )
            if schema is PlanReviewDecision:
                return schema(action="finish", reason="轻量查询已经完成")
            return schema(
                goal="备用研究目标",
                tasks=["核实备用信息", "比较备用信息"],
                source_strategy=["查询官方来源"],
                success_criteria=["取得可用结论"],
                notes="",
            )

        return RunnableLambda(respond)

    def bind_tools(self, _tools: list[object]) -> RunnableLambda:
        return RunnableLambda(
            lambda _messages: AIMessage(content="广州塔停止入场时间应以当天预约页面为准。")
        )

    def with_config(self, **_kwargs: Any) -> RunnableLambda:
        return RunnableLambda(
            lambda _messages: AIMessage(content="广州塔停止入场时间应以当天预约页面为准。")
        )


def test_root_routes_lightweight_request_and_maps_helper_response() -> None:
    """Helper 缺少根图映射时，轻量查询无法从 API 主链路到达。"""
    repository = RoutingContextRepository()
    graph = build_root_graph(HelperRoutingModel(), repository)

    result = asyncio.run(
        graph.ainvoke(
            {
                "user_id": USER_ID,
                "trip_id": TRIP_ID,
                "user_message_id": 40,
                "user_input": "广州塔几点停止入场？",
            },
            {"configurable": {"thread_id": "helper-root-thread"}},
        )
    )

    assert result["route"].value == "helper"
    assert result["response"] == "广州塔停止入场时间应以当天预约页面为准。"
    assert repository.recent_queries == [
        (TRIP_ID, 40, 4),
        (TRIP_ID, 40, 8),
    ]
