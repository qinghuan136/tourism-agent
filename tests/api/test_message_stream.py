"""验证流式消息接口返回面向前端的 SSE 事件。"""

import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessageChunk

from tests.api.test_messages import (
    IDEMPOTENCY_ID_1,
    ApiFakeConversationChunkService,
    ApiFakeIdempotencyRepository,
    ApiFakeRepository,
    message_payload,
    override_api_dependencies,
)
from tourism_agent.models.context import ConversationMessage, ConversationRole
from tourism_agent.models.idempotency import IdempotencyStatus
from tourism_agent.models.orchestration import TaskSpec, TaskType


@dataclass
class _StreamSnapshot:
    """模拟流接口运行前后读取到的最小 checkpoint 快照。"""

    values: dict[str, object]
    interrupts: tuple[object, ...] = ()


class _FakeCheckpointer:
    """记录正常运行结束后的 checkpoint 清理行为。"""

    async def adelete_thread(self, _thread_id: str) -> None:
        return None


class StreamingFakeGraph:
    """提供固定 Graph 事件与最终 State，避免流接口测试依赖真实模型。"""

    def __init__(self) -> None:
        self.checkpointer = _FakeCheckpointer()
        self._final_state = _StreamSnapshot(
            values={
                "route": TaskType.PLANNING,
                "response": "这是最终旅行建议。",
                "orchestration_goal": "完成当前旅行规划请求",
            }
        )
        self._has_finished = False

    async def aget_state(self, _config: dict[str, object]) -> _StreamSnapshot:
        if self._has_finished:
            return self._final_state
        return _StreamSnapshot(values={})

    async def astream_events(
        self,
        graph_input: dict[str, object],
        _config: dict[str, object],
        *,
        version: str,
    ) -> Any:
        assert version == "v2"
        assert graph_input["user_input"] == "帮我规划北京三日游"
        yield {
            "event": "on_chain_start",
            "name": "planning",
            "run_id": "planning-run-1",
            "metadata": {"langgraph_node": "run_planning"},
            "data": {
                "input": {
                    "current_task": TaskSpec(
                        task_id="task_1",
                        task_type=TaskType.PLANNING,
                        instruction="生成旅行规划",
                    )
                }
            },
        }
        yield {
            "event": "on_tool_start",
            "name": "web_search",
            "run_id": "tool-1",
            "data": {"input": {"query": "不应向前端暴露"}},
        }
        yield {
            "event": "on_tool_end",
            "name": "web_search",
            "run_id": "tool-1",
            "data": {"output": "不应向前端暴露"},
        }
        yield {
            "event": "on_chat_model_stream",
            "tags": ["orchestrator", "finalize", "public_output"],
            "data": {"chunk": AIMessageChunk(content="这是")},
        }
        yield {
            "event": "on_chat_model_stream",
            "tags": ["planning", "react"],
            "data": {"chunk": AIMessageChunk(content="内部回答")},
        }
        yield {
            "event": "on_chain_end",
            "name": "planning",
            "run_id": "planning-run-1",
            "metadata": {"langgraph_node": "run_planning"},
            "data": {
                "output": {
                    "latest_task_result": {
                        "task_id": "task_1",
                        "task_type": "planning",
                        "status": "success",
                        "result": "已完成旅行规划子任务。",
                    }
                }
            },
        }
        self._has_finished = True


class InterruptStreamingFakeGraph(StreamingFakeGraph):
    """模拟候选行程确认 interrupt。"""

    def __init__(self) -> None:
        super().__init__()
        self._final_state = _StreamSnapshot(
            values={
                "route": TaskType.PLANNING,
                "current_task": TaskSpec(
                    task_id="task_1",
                    task_type=TaskType.PLANNING,
                    instruction="生成并确认候选行程",
                ),
            },
            interrupts=(
                SimpleNamespace(
                    value={
                        "kind": "candidate_confirmation",
                        "question": "是否确认采用这份行程？请选择：是或否。",
                        "candidate_itinerary": "第一天游览西湖。",
                    }
                ),
            ),
        )

    async def astream_events(
        self,
        graph_input: dict[str, object],
        _config: dict[str, object],
        *,
        version: str,
    ) -> Any:
        assert version == "v2"
        assert graph_input["user_input"] == "帮我规划杭州一日游"
        yield {
            "event": "on_chain_start",
            "name": "planning",
            "run_id": "planning-run-1",
            "metadata": {"langgraph_node": "planning"},
            "data": {
                "input": {
                    "current_task": TaskSpec(
                        task_id="task_1",
                        task_type=TaskType.PLANNING,
                        instruction="生成并确认候选行程",
                    )
                }
            },
        }
        yield {
            "event": "on_chain_end",
            "name": "planning",
            "run_id": "planning-run-1",
            "metadata": {"langgraph_node": "planning"},
            "data": {},
        }
        self._has_finished = True


class UserWriteFailingRepository(ApiFakeRepository):
    """模拟流建立后、根图开始前写入用户消息失败。"""

    async def append_conversation(
        self,
        trip_id: UUID,
        role: ConversationRole,
        content: str,
    ) -> ConversationMessage:
        if role is ConversationRole.USER:
            raise RuntimeError("模拟用户消息持久化失败")
        return await super().append_conversation(trip_id, role, content)


def parse_sse_events(body: str) -> list[tuple[str, dict[str, object]]]:
    """解析测试响应中的 SSE 文本，不依赖前端实现。"""
    parsed: list[tuple[str, dict[str, object]]] = []
    for block in body.strip().split("\n\n"):
        event_name, data_line = block.split("\n", maxsplit=1)
        parsed.append(
            (event_name.removeprefix("event: "), json.loads(data_line[6:]))
        )
    return parsed


def test_message_stream_emits_safe_progress_token_and_final_result() -> None:
    """流接口缺失、泄露内部 Token 或未返回权威结果时，该测试都应失败。"""
    from importlib import import_module

    api = import_module("tourism_agent.api")
    repository = ApiFakeRepository()
    idempotency_repository = ApiFakeIdempotencyRepository()
    chunk_service = ApiFakeConversationChunkService()
    graph = StreamingFakeGraph()
    override_api_dependencies(
        api,
        graph,
        repository,
        idempotency_repository,
        chunk_service,
    )

    try:
        with TestClient(api.app).stream(
            "POST",
            "/messages/stream",
            json=message_payload("帮我规划北京三日游", IDEMPOTENCY_ID_1),
        ) as response:
            body = response.read().decode("utf-8")
    finally:
        api.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse_events(body)
    assert [event_name for event_name, _ in events] == [
        "run.started",
        "task.started",
        "operation.started",
        "operation.completed",
        "token.delta",
        "task.result",
        "task.completed",
        "result",
        "run.completed",
    ]
    assert events[1][1]["task_id"] == "task_1"
    assert events[5][1] == {
        "sequence": 6,
        "idempotency_id": str(IDEMPOTENCY_ID_1),
        "timestamp": events[5][1]["timestamp"],
        "task_id": "task_1",
        "module": "planning",
        "status": "success",
        "result": "已完成旅行规划子任务。",
    }
    assert events[6][1]["task_id"] == "task_1"
    assert events[4][1]["text"] == "这是"
    assert "内部回答" not in response.text
    assert "不应向前端暴露" not in response.text
    assert {
        key: events[-2][1][key]
        for key in ("route", "message", "candidate_itinerary", "current_itinerary")
    } == {
        "route": "planning",
        "message": "这是最终旅行建议。",
        "candidate_itinerary": None,
        "current_itinerary": None,
    }
    assert events[-1][1]["status"] == "completed"
    assert [message.content for message in repository.messages] == [
        "帮我规划北京三日游",
        "这是最终旅行建议。",
    ]
    assert len(chunk_service.submissions) == 1


def test_message_stream_emits_interrupt_before_waiting_result() -> None:
    """候选确认必须先通知交互控件，再用权威结果结束本次 SSE 连接。"""
    from importlib import import_module

    api = import_module("tourism_agent.api")
    repository = ApiFakeRepository()
    idempotency_repository = ApiFakeIdempotencyRepository()
    graph = InterruptStreamingFakeGraph()
    override_api_dependencies(api, graph, repository, idempotency_repository)

    try:
        response = TestClient(api.app).post(
            "/messages/stream",
            json=message_payload("帮我规划杭州一日游", IDEMPOTENCY_ID_1),
        )
    finally:
        api.app.dependency_overrides.clear()

    events = parse_sse_events(response.text)
    event_names = [event_name for event_name, _ in events]
    assert "task.result" not in event_names
    assert "task.completed" not in event_names
    assert event_names[-3:] == [
        "interaction.required",
        "result",
        "run.completed",
    ]
    assert events[-3][1]["allowed_answers"] == ["是", "否"]
    assert events[-3][1]["candidate_itinerary"] == "第一天游览西湖。"
    assert events[-2][1]["message"] == "是否确认采用这份行程？请选择：是或否。"
    assert events[-1][1]["status"] == "waiting_user"


def test_message_stream_openapi_declares_sse_and_json_replay_responses() -> None:
    """自动 OpenAPI 必须让生成客户端知道成功响应是 SSE 而非默认 JSON。"""
    from importlib import import_module

    api = import_module("tourism_agent.api")
    schema = api.app.openapi()
    responses = schema["paths"]["/messages/stream"]["post"]["responses"]

    assert "text/event-stream" in responses["200"]["content"]
    assert "application/json" in responses["202"]["content"]


def test_message_stream_closes_with_error_when_user_message_persistence_fails() -> None:
    """流建立后输入落库失败时，客户端不能永久等待 processing 状态。"""
    from importlib import import_module

    api = import_module("tourism_agent.api")
    repository = UserWriteFailingRepository()
    idempotency_repository = ApiFakeIdempotencyRepository()
    graph = StreamingFakeGraph()
    override_api_dependencies(api, graph, repository, idempotency_repository)

    try:
        response = TestClient(api.app, raise_server_exceptions=False).post(
            "/messages/stream",
            json=message_payload("帮我规划北京三日游", IDEMPOTENCY_ID_1),
        )
    finally:
        api.app.dependency_overrides.clear()

    events = parse_sse_events(response.text)
    assert [event_name for event_name, _ in events] == ["error", "run.completed"]
    assert events[0][1]["code"] == "MODEL_ERROR"
    assert events[-1][1]["status"] == "failed"
    assert idempotency_repository.records[IDEMPOTENCY_ID_1].status is IdempotencyStatus.FAILED
