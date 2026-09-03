"""验证 SSE 事件只暴露前端可消费的安全信息。"""

import asyncio
import json

from langchain_core.messages import AIMessageChunk

from tourism_agent.models.orchestration import TaskResult, TaskSpec
from tourism_agent.services.sse_stream import (
    GraphEventTranslator,
    SseEvent,
    SseEventSink,
    encode_sse_event,
)


def test_finalizer_token_is_converted_to_token_delta() -> None:
    """最终汇总节点的文本增量应进入前端可见 Token 流。"""
    translator = GraphEventTranslator()

    events = translator.translate(
        {
            "event": "on_chat_model_stream",
            "tags": ["orchestrator", "finalize", "public_output"],
            "data": {"chunk": AIMessageChunk(content="旅行建议")},
        }
    )

    assert events == [SseEvent(event="token.delta", data={"text": "旅行建议"})]


def test_internal_model_token_is_not_exposed() -> None:
    """规划、检索等内部模型文本不得泄露到 SSE。"""
    translator = GraphEventTranslator()

    events = translator.translate(
        {
            "event": "on_chat_model_stream",
            "tags": ["planning", "react"],
            "data": {"chunk": AIMessageChunk(content="内部提示词内容")},
        }
    )

    assert events == []


def test_tool_lifecycle_is_mapped_without_raw_arguments_or_result() -> None:
    """网页工具事件应只展示稳定名称和中文进度，不能携带原始查询参数。"""
    translator = GraphEventTranslator()

    events = translator.translate(
        {
            "event": "on_tool_start",
            "name": "web_search",
            "run_id": "tool-run-1",
            "data": {"input": {"query": "不应暴露的内部检索语句"}},
        }
    )

    assert events == [
        SseEvent(
            event="operation.started",
            data={
                "operation_id": "tool-run-1",
                "tool": "web_search",
                "message": "正在搜索网页信息",
            },
        )
    ]


def test_only_outer_module_node_is_mapped_to_task_progress() -> None:
    """编排内部节点和子图内部节点不能制造重复的前端任务进度。"""
    translator = GraphEventTranslator()

    assert translator.translate(
        {
            "event": "on_chain_start",
            "name": "create_plan",
            "metadata": {"langgraph_node": "create_plan"},
            "data": {},
        }
    ) == []

    assert translator.translate(
        {
            "event": "on_chain_start",
            "name": "planning",
            "run_id": "planning-run-1",
            "metadata": {"langgraph_node": "planning"},
            "data": {
                "input": {
                    "current_task": TaskSpec(
                        task_id="task_2",
                        task_type="planning",
                        instruction="把选中的景点加入行程",
                    )
                }
            },
        }
    ) == [
        SseEvent(
            event="task.started",
            data={
                "task_id": "task_2",
                "module": "planning",
                "message": "正在规划行程",
            },
        )
    ]
    assert translator.translate(
        {
            "event": "on_chain_end",
            "name": "planning",
            "run_id": "planning-run-1",
            "metadata": {"langgraph_node": "planning"},
            "data": {
                "output": {
                    "latest_task_result": TaskResult(
                        task_id="task_2",
                        task_type="planning",
                        status="success",
                        result="已形成第一天行程调整建议。",
                    )
                }
            },
        }
    ) == [
        SseEvent(
            event="task.result",
            data={
                "task_id": "task_2",
                "module": "planning",
                "status": "success",
                "result": "已形成第一天行程调整建议。",
            },
        ),
        SseEvent(
            event="task.completed",
            data={
                "task_id": "task_2",
                "module": "planning",
                "message": "正在规划行程",
            },
        )
    ]


def test_interrupted_module_does_not_emit_result_or_completed() -> None:
    """子图进入 interrupt 时任务尚未完成，不能提前发送完成事件。"""
    translator = GraphEventTranslator()
    start_event = {
        "event": "on_chain_start",
        "name": "planning",
        "run_id": "planning-interrupt-run",
        "metadata": {"langgraph_node": "planning"},
        "data": {
            "input": {
                "current_task": TaskSpec(
                    task_id="task_interrupt",
                    task_type="planning",
                    instruction="向用户确认候选行程",
                )
            }
        },
    }

    assert translator.translate(start_event)[0].event == "task.started"
    assert translator.translate(
        {
            "event": "on_chain_end",
            "name": "planning",
            "run_id": "planning-interrupt-run",
            "metadata": {"langgraph_node": "planning"},
            "data": {},
        }
    ) == []

    # resume 会形成新的根模块运行；真正返回 TaskResult 后才发送完成事件。
    resumed_start = dict(start_event, run_id="planning-resumed-run")
    assert translator.translate(resumed_start)[0].event == "task.started"
    resumed_events = translator.translate(
        {
            "event": "on_chain_end",
            "name": "planning",
            "run_id": "planning-resumed-run",
            "metadata": {"langgraph_node": "planning"},
            "data": {
                "output": {
                    "latest_task_result": TaskResult(
                        task_id="task_interrupt",
                        task_type="planning",
                        status="success",
                        result="用户确认后已完成规划任务。",
                    )
                }
            },
        }
    )
    assert [event.event for event in resumed_events] == [
        "task.result",
        "task.completed",
    ]


def test_encode_sse_event_uses_standard_event_and_json_data_lines() -> None:
    """编码结果必须能被浏览器的 SSE 解析器按事件读取。"""
    encoded = encode_sse_event(
        SseEvent(event="run.started", data={"message": "正在处理你的请求"})
    ).decode("utf-8")

    assert encoded.startswith("event: run.started\n")
    assert json.loads(encoded.split("data: ", maxsplit=1)[1]) == {
        "message": "正在处理你的请求"
    }
    assert encoded.endswith("\n\n")


def test_detached_sse_sink_discards_buffered_and_later_events() -> None:
    """浏览器断线后，后台运行不得继续在内存中累积不可消费的 SSE 事件。"""

    async def scenario() -> None:
        sink = SseEventSink()
        sink.emit(SseEvent(event="token.delta", data={"text": "已缓存"}))
        assert sink.detach() == 1
        sink.emit(SseEvent(event="token.delta", data={"text": "不应缓存"}))

        assert sink.detach() == 0

    asyncio.run(scenario())
