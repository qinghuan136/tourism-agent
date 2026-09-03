"""把 LangGraph 运行事件转换为稳定且不泄露内部细节的 SSE 事件。"""

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import BaseMessage

from tourism_agent.models.orchestration import TaskResult


@dataclass(frozen=True)
class SseEvent:
    """表示尚未补充请求级公共字段的一条 SSE 业务事件。"""

    event: str
    data: dict[str, Any]


class SseEventSink:
    """维护单个 SSE 连接的事件缓冲，并在客户端离开后停止积压事件。"""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[SseEvent | None] = asyncio.Queue()
        self._attached = True
        self._closed = False

    def emit(self, event: SseEvent) -> None:
        """仅在客户端仍连接且流未结束时缓存事件。"""
        if self._attached and not self._closed:
            self._queue.put_nowait(event)

    async def next_event(self) -> SseEvent | None:
        """读取下一条事件或终止标记。"""
        return await self._queue.get()

    def close(self) -> None:
        """正常结束当前连接的事件生产。"""
        if self._attached and not self._closed:
            self._closed = True
            self._queue.put_nowait(None)

    def detach(self) -> int:
        """客户端断线后丢弃已缓冲事件，后台任务仍可继续业务收尾。"""
        self._attached = False
        self._closed = True
        discarded_count = 0
        while not self._queue.empty():
            self._queue.get_nowait()
            discarded_count += 1
        return discarded_count


TOOL_PROGRESS_MESSAGES = {
    "get_weather": "正在查询天气",
    "search_places": "正在搜索相关地点",
    "search_nearby_places": "正在搜索附近地点",
    "get_place_details": "正在核实地点详情",
    "plan_route": "正在查询出行路线",
    "measure_travel_distance": "正在比较路程和耗时",
    "web_search": "正在搜索网页信息",
    "extract_web_content": "正在读取网页内容",
    "map_web_site": "正在分析网站结构",
    "crawl_web_site": "正在抓取相关页面",
    "search_conversation_history": "正在检索相关历史对话",
    "read_conversation_exchanges": "正在读取历史对话原文",
    "update_trip_context": "正在保存旅行信息",
    "delete_trip_context_keys": "正在更新旅行信息",
    "submit_candidate_itinerary": "正在提交候选行程",
}

MODULE_PROGRESS_MESSAGES = {
    "orchestrator": "正在协调旅行任务",
    "planning": "正在规划行程",
    "explore": "正在探索旅行目的地",
    "research": "正在进行深度调研",
    "helper": "正在处理旅行助手请求",
}

GRAPH_NODE_MODULES = {
    "planning": "planning",
    "explore": "explore",
    "research": "research",
    "helper": "helper",
}


def encode_sse_event(event: SseEvent) -> bytes:
    """按 SSE 标准编码单条事件，data 始终保持为一行 JSON。"""
    payload = json.dumps(event.data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event.event}\ndata: {payload}\n\n".encode()


class GraphEventTranslator:
    """仅选择可公开的图运行事件，并转换为前端稳定的语义事件。"""

    def __init__(self) -> None:
        # on_chain_end 不再携带节点输入，因此用同一 run_id 关联开始和结束事件。
        self._task_ids_by_run_id: dict[str, str] = {}

    def translate(self, event: dict[str, Any]) -> list[SseEvent]:
        """将一条 LangChain/LangGraph v2 事件映射为有限条业务事件。"""
        event_type = str(event.get("event", ""))
        if event_type == "on_chat_model_stream":
            return self._translate_finalizer_token(event)
        if event_type in {"on_tool_start", "on_tool_end", "on_tool_error"}:
            return self._translate_tool_event(event)
        if event_type in {"on_chain_start", "on_chain_end"}:
            return self._translate_graph_node(event)
        return []

    def _translate_finalizer_token(self, event: dict[str, Any]) -> list[SseEvent]:
        """只有最终汇总节点的文本才属于用户可见输出。"""
        tags = set(event.get("tags", []))
        if not {"orchestrator", "finalize", "public_output"}.issubset(tags):
            return []
        chunk = event.get("data", {}).get("chunk")
        if not isinstance(chunk, BaseMessage):
            return []
        content = chunk.content
        if not isinstance(content, str) or not content:
            return []
        return [SseEvent(event="token.delta", data={"text": content})]

    def _translate_tool_event(self, event: dict[str, Any]) -> list[SseEvent]:
        """Tool 生命周期只公开名称、调用标识和预设中文说明。"""
        tool_name = str(event.get("name", ""))
        message = TOOL_PROGRESS_MESSAGES.get(tool_name)
        if message is None or tool_name == "ask_user":
            return []
        operation_id = str(event.get("run_id", ""))
        event_type = str(event["event"])
        if event_type == "on_tool_start":
            kind = "operation.started"
        elif event_type == "on_tool_end":
            kind = "operation.completed"
        else:
            kind = "operation.failed"
        return [
            SseEvent(
                event=kind,
                data={
                    "operation_id": operation_id,
                    "tool": tool_name,
                    "message": message,
                },
            )
        ]

    def _translate_graph_node(self, event: dict[str, Any]) -> list[SseEvent]:
        """只将根图模块节点转换为任务进度和标准结果。"""
        # 外层模块节点的 name 与模块名相同；子图内部事件虽携带相同的
        # langgraph_node 元数据，但 name 为 LangGraph 或内部节点名，应忽略。
        module = GRAPH_NODE_MODULES.get(str(event.get("name", "")))
        if module is None:
            return []
        run_id = str(event.get("run_id", ""))
        if event["event"] == "on_chain_start":
            task_id = self._task_id_from_input(event)
            if not run_id or task_id is None:
                return []
            self._task_ids_by_run_id[run_id] = task_id
        else:
            task_id = self._task_ids_by_run_id.pop(run_id, None)
            if task_id is None:
                return []
            task_result = self._task_result_from_output(event)
            # interrupt 会结束本次子图事件流，但不代表当前任务已经完成。
            # 只有子图产出了标准 TaskResult，才能成对发送结果和完成事件。
            if task_result is None:
                return []
            events = [
                SseEvent(
                    event="task.result",
                    data={
                        "task_id": task_result.task_id,
                        "module": task_result.task_type.value,
                        "status": task_result.status.value,
                        "result": task_result.result,
                    },
                )
            ]
            events.append(
                SseEvent(
                    event="task.completed",
                    data={
                        "task_id": task_id,
                        "module": module,
                        "message": MODULE_PROGRESS_MESSAGES[module],
                    },
                )
            )
            return events
        return [
            SseEvent(
                event="task.started",
                data={
                    "task_id": task_id,
                    "module": module,
                    "message": MODULE_PROGRESS_MESSAGES[module],
                },
            )
        ]

    @staticmethod
    def _task_id_from_input(event: dict[str, Any]) -> str | None:
        """从根图模块节点输入中读取 Orchestrator 当前任务标识。"""
        node_input = event.get("data", {}).get("input")
        if not isinstance(node_input, Mapping):
            return None
        current_task = node_input.get("current_task")
        if isinstance(current_task, Mapping):
            task_id = current_task.get("task_id")
        else:
            task_id = getattr(current_task, "task_id", None)
        return task_id if isinstance(task_id, str) and task_id else None

    @staticmethod
    def _task_result_from_output(event: dict[str, Any]) -> TaskResult | None:
        """读取根图模块节点刚刚形成的标准 TaskResult。"""
        output = event.get("data", {}).get("output")
        if not isinstance(output, Mapping):
            return None
        task_result = output.get("latest_task_result")
        if task_result is None:
            return None
        return TaskResult.model_validate(task_result)
