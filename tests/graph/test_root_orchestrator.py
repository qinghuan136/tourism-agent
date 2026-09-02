"""验证根图按 Orchestrator 计划顺序调度业务子图。"""

import asyncio
import logging
from importlib import import_module
from uuid import UUID

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableLambda
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel

from tourism_agent.models.context import ConversationMessage
from tourism_agent.models.orchestration import (
    OrchestrationPlan,
    PlanReviewDecision,
    TaskSpec,
    TaskType,
)

USER_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TRIP_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
ROOT_INPUT = {
    "user_id": USER_ID,
    "trip_id": TRIP_ID,
    "user_message_id": 40,
    "user_input": "寻找广州塔附近适合闲逛的地点，调研后加入行程",
}
ROOT_CONFIG = {"configurable": {"thread_id": str(TRIP_ID)}}


class RoutingContextRepository:
    """为根图测试提供空的只读旅行上下文。"""

    async def get_recent_conversation(
        self,
        _trip_id: UUID,
        *,
        before_message_id: int,
        limit: int,
    ) -> list[ConversationMessage]:
        assert before_message_id == 40
        assert limit in {4, 8}
        return []

    async def get_trip_context(self, _trip_id: UUID) -> dict[str, object]:
        return {}

    async def get_current_itinerary(self, _trip_id: UUID) -> str | None:
        return None


class ScriptedOrchestratorModel:
    """按测试脚本返回根图计划、复核决定和最终回复。"""

    def __init__(
        self,
        tasks: list[TaskType],
        reviews: list[PlanReviewDecision] | None = None,
    ) -> None:
        self.tasks = tasks
        self.reviews = list(reviews or [])

    def with_structured_output(self, schema: type[BaseModel]) -> RunnableLambda:
        if schema is OrchestrationPlan:
            return RunnableLambda(
                lambda _messages: schema(
                    goal="完成复合旅行请求",
                    tasks=[
                        TaskSpec(
                            task_id=f"task_{index}",
                            task_type=task_type,
                            instruction=f"执行 {task_type.value} 子任务",
                        )
                        for index, task_type in enumerate(self.tasks, start=1)
                    ],
                )
            )

        def review(_messages: list[BaseMessage]) -> PlanReviewDecision:
            if self.reviews:
                return self.reviews.pop(0)
            return PlanReviewDecision(
                action="continue",
                reason="继续执行原计划",
                handoff_context="已完成前序任务，可继续执行下一项。",
            )

        return RunnableLambda(review)

    def with_config(self, **_kwargs: object) -> RunnableLambda:
        return RunnableLambda(
            lambda _messages: AIMessage(content="已完成本轮复合旅行请求。")
        )


class RecordingSubgraph:
    """记录子图调用顺序与内部 Task 输入，不执行真实模型。"""

    def __init__(
        self,
        name: str,
        calls: list[str],
        inputs: dict[str, str],
        result_text: str | None = None,
    ) -> None:
        self.name = name
        self.calls = calls
        self.inputs = inputs
        self.result_text = result_text

    async def ainvoke(self, payload: dict[str, object], **_kwargs: object) -> dict:
        self.calls.append(self.name)
        messages = payload["messages"]
        assert isinstance(messages, list)
        self.inputs[self.name] = str(messages[0].content)
        self.inputs[f"{self.name}_retrieval_query"] = str(payload["retrieval_query"])
        self.inputs[f"{self.name}_retrieval_user_input"] = str(
            payload["retrieval_user_input"]
        )
        self.inputs[f"{self.name}_retrieval_task_goal"] = str(
            payload["retrieval_task_goal"]
        )
        return {
            "assistant_message": self.result_text or f"{self.name} result：沙面",
            "candidate_itinerary": None,
            "current_itinerary": None,
        }


class InterruptingPlanningSubgraph:
    """在 Planning 执行中暂停，验证根图恢复时保留已完成 Task。"""

    async def ainvoke(self, _payload: dict[str, object], **_kwargs: object) -> dict:
        answer = interrupt(
            {
                "kind": "candidate_confirmation",
                "question": "是否确认当前候选方案？",
                "candidate_itinerary": "沙面候选行程",
            }
        )
        return {
            "assistant_message": f"用户回答：{answer}",
            "candidate_itinerary": None,
            "current_itinerary": "沙面已确认行程",
        }


def build_test_root(
    monkeypatch: pytest.MonkeyPatch,
    tasks: list[TaskType],
    reviews: list[PlanReviewDecision] | None = None,
    result_text: str | None = None,
) -> tuple[CompiledStateGraph, list[str], dict[str, str]]:
    """构建只替换业务子图、保留真实根图调度的测试图。"""
    calls: list[str] = []
    inputs: dict[str, str] = {}
    module = import_module("tourism_agent.graph.root")
    for name in ("planning", "explore", "research", "helper"):
        monkeypatch.setattr(
            module,
            f"build_{name}_graph",
            lambda *_args, _name=name, **_kwargs: RecordingSubgraph(
                _name, calls, inputs, result_text
            ),
        )
    graph = module.build_root_graph(
        ScriptedOrchestratorModel(tasks, reviews),
        RoutingContextRepository(),
    )
    return graph, calls, inputs


def test_root_executes_multi_task_plan_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """若根图仍是单路由，三个领域任务不会按计划全部执行。"""
    graph, calls, _inputs = build_test_root(
        monkeypatch,
        tasks=[TaskType.EXPLORE, TaskType.RESEARCH, TaskType.PLANNING],
    )

    result = asyncio.run(graph.ainvoke(ROOT_INPUT, ROOT_CONFIG))

    assert calls == ["explore", "research", "planning"]
    assert [item.task_type for item in result["task_results"]] == [
        TaskType.EXPLORE,
        TaskType.RESEARCH,
        TaskType.PLANNING,
    ]
    assert result["route"] is TaskType.PLANNING


def test_resume_continues_current_task_without_replaying_completed_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Planning 恢复时不得重放已经完成的 Explore Task。"""
    module = import_module("tourism_agent.graph.root")
    calls: list[str] = []
    inputs: dict[str, str] = {}
    monkeypatch.setattr(
        module,
        "build_explore_graph",
        lambda *_args, **_kwargs: RecordingSubgraph("explore", calls, inputs),
    )
    monkeypatch.setattr(
        module,
        "build_planning_graph",
        lambda *_args, **_kwargs: InterruptingPlanningSubgraph(),
    )
    for name in ("research", "helper"):
        monkeypatch.setattr(
            module,
            f"build_{name}_graph",
            lambda *_args, _name=name, **_kwargs: RecordingSubgraph(
                _name, calls, inputs
            ),
        )
    graph = module.build_root_graph(
        ScriptedOrchestratorModel(
            [TaskType.EXPLORE, TaskType.PLANNING],
            reviews=[
                PlanReviewDecision(
                    action="continue",
                    reason="继续修改行程",
                    handoff_context="Explore 已找到沙面，下一步需要确认行程调整。",
                ),
                PlanReviewDecision(action="finish", reason="目标已经完成"),
            ],
        ),
        RoutingContextRepository(),
    )

    first = asyncio.run(graph.ainvoke(ROOT_INPUT, ROOT_CONFIG))
    assert first["__interrupt__"][0].value["kind"] == "candidate_confirmation"
    assert first["route"] is TaskType.PLANNING
    assert isinstance(first["current_task"], TaskSpec)
    assert first["current_task"].task_id == "task_2"
    assert calls.count("explore") == 1

    completed = asyncio.run(graph.ainvoke(Command(resume="是"), ROOT_CONFIG))
    assert calls.count("explore") == 1
    assert completed["executed_task_count"] == 2
    assert [result.task_id for result in completed["task_results"]] == [
        "task_1",
        "task_2",
    ]
    assert len(completed["task_results"]) == 2
    assert len({result.task_id for result in completed["task_results"]}) == 2


def test_subgraph_receives_orchestrator_handoff_instead_of_raw_task_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """若根图仍透传用户消息，后续任务会缺少编排标签和前序结果。"""
    graph, _calls, inputs = build_test_root(
        monkeypatch,
        tasks=[TaskType.EXPLORE, TaskType.RESEARCH],
        reviews=[
            PlanReviewDecision(
                action="continue",
                reason="继续调查候选",
                handoff_context="沙面是当前候选，下一步需要核实开放时间。",
            ),
            PlanReviewDecision(action="finish", reason="调查完成"),
        ],
        result_text="RAW_TASK_RESULT_SENTINEL",
    )

    asyncio.run(graph.ainvoke(ROOT_INPUT, ROOT_CONFIG))
    research_input = inputs["research"]

    assert "【原始用户目标】" in research_input
    assert "【当前子任务】" in research_input
    assert "【Orchestrator整理的现有结果】" in research_input
    assert "沙面是当前候选，下一步需要核实开放时间。" in research_input
    assert "RAW_TASK_RESULT_SENTINEL" not in research_input
    research_query = inputs["research_retrieval_query"]
    assert research_query.startswith("【当前检索目标】")
    assert "执行 research 子任务" in research_query
    assert ROOT_INPUT["user_input"] in research_query
    assert "沙面是当前候选，下一步需要核实开放时间。" in research_query
    assert "RAW_TASK_RESULT_SENTINEL" not in research_query
    assert inputs["research_retrieval_user_input"] == ROOT_INPUT["user_input"]
    assert inputs["research_retrieval_task_goal"] == "执行 research 子任务"


def test_review_finish_skips_research_and_planning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """复核决定提前结束后，不应继续执行原计划中的剩余任务。"""
    graph, calls, _inputs = build_test_root(
        monkeypatch,
        tasks=[TaskType.EXPLORE, TaskType.RESEARCH, TaskType.PLANNING],
        reviews=[
            PlanReviewDecision(
                action="finish",
                reason="附近没有找到满足要求的地点",
            )
        ],
    )

    result = asyncio.run(graph.ainvoke(ROOT_INPUT, ROOT_CONFIG))

    assert calls == ["explore"]
    assert result["executed_task_count"] == 1


def test_review_replaces_only_remaining_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """替换复核决定只覆盖待办队列，不影响已经记录的任务结果。"""
    reviews = [
        PlanReviewDecision(
            action="replace_remaining",
            reason="需要调查明确候选",
            replacement_tasks=[
                TaskSpec(
                    task_id="task_4",
                    task_type="research",
                    instruction="深入调查沙面",
                )
            ],
            handoff_context="沙面是需要继续调查的候选。",
        ),
        PlanReviewDecision(action="finish", reason="调查已经完成"),
    ]
    graph, calls, _inputs = build_test_root(
        monkeypatch,
        tasks=[TaskType.EXPLORE, TaskType.PLANNING],
        reviews=reviews,
    )

    result = asyncio.run(graph.ainvoke(ROOT_INPUT, ROOT_CONFIG))

    assert [item.task_id for item in result["task_results"]] == [
        "task_1",
        "task_4",
    ]
    assert calls == ["explore", "research"]
    assert result["task_results"][1].task_type is TaskType.RESEARCH


def test_root_finalizes_after_five_completed_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """即使复核持续补充任务，单轮也只能完成五项任务。"""
    reviews = [
        PlanReviewDecision(
            action="replace_remaining",
            reason="继续安排下一项任务",
            replacement_tasks=[
                TaskSpec(
                    task_id=f"task_{index + 1}",
                    task_type="helper",
                    instruction=f"执行第 {index + 1} 项任务",
                )
            ],
            handoff_context="已完成当前步骤，继续处理下一项。",
        )
        for index in range(1, 6)
    ]
    graph, calls, _inputs = build_test_root(
        monkeypatch,
        tasks=[TaskType.HELPER],
        reviews=reviews,
    )

    result = asyncio.run(graph.ainvoke(ROOT_INPUT, ROOT_CONFIG))

    assert result["executed_task_count"] == 5
    assert len(result["task_results"]) == 5
    assert len(calls) == 5


def test_root_logs_orchestrator_lifecycle_without_full_task_result(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """编排日志应覆盖关键生命周期，同时只记录任务结果的截断摘要。"""
    long_result = "LONG_TASK_RESULT_START_" + "详细研究结果" * 100
    graph, _calls, _inputs = build_test_root(
        monkeypatch,
        tasks=[TaskType.RESEARCH],
        reviews=[PlanReviewDecision(action="finish", reason="研究已完成")],
        result_text=long_result,
    )

    with caplog.at_level(logging.INFO, logger="tourism_agent"):
        asyncio.run(graph.ainvoke(ROOT_INPUT, ROOT_CONFIG))

    messages = [record.getMessage() for record in caplog.records]
    plan_logs = [message for message in messages if "Orchestrator初始计划" in message]
    assert plan_logs
    assert "task_count=1" in plan_logs[0]
    assert "task_1:research" in plan_logs[0]
    review_logs = [message for message in messages if "Orchestrator复核决定" in message]
    assert review_logs
    assert "action=finish" in review_logs[0]
    assert "pending_count=0" in review_logs[0]
    assert "replacement_count=0" in review_logs[0]
    task_result_logs = [
        message for message in messages if "Orchestrator任务结果" in message
    ]
    assert task_result_logs
    assert "task_id=task_1" in task_result_logs[0]
    assert "task_type=research" in task_result_logs[0]
    assert "status=success" in task_result_logs[0]
    assert "LONG_TASK_RESULT_START_" in task_result_logs[0]
    assert long_result not in task_result_logs[0]
    assert all(long_result not in message for message in messages)
    finalize_logs = [message for message in messages if "Orchestrator执行结束" in message]
    assert finalize_logs
    assert "executed_task_count=1" in finalize_logs[0]
