# SSE 流式消息接口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在保留现有非流式 `/messages` 的前提下，提供可安全展示进度、最终输出 Token、interrupt 与最终结果的 SSE 接口。

**Architecture:** 新接口 `POST /messages/stream` 复用现有幂等校验、会话锁、消息持久化与结果组装逻辑。Graph 的异步事件被转换为受控的 SSE 事件；只有带 `orchestrator/finalize/public_output` 标签的模型 Token 可对外输出。流连接断开不会取消后台运行，取消仍由既有取消接口显式完成。

**Tech Stack:** FastAPI StreamingResponse、LangGraph astream_events、asyncio、Pydantic、pytest。

**Spec:** `docs/sse-streaming-design.md`

## Global Constraints

- 保持 `/messages` 与既有幂等、取消、Conversation/RAG 写入语义兼容。
- SSE 只输出受控事件；不暴露 State、Prompt、思维链、RAG 查询、原始工具结果或异常栈。
- 普通完成、等待用户输入、主动取消、运行失败都必须以 `run.completed` 收尾。
- SSE 只流式输出最终编排器的可见回答 Token；最终 `result` 始终是权威结果。
- 新增注释、Docstring、错误信息使用中文；避免不必要的抽象和防御性代码。

---

### Task 1: 事件模型与事件转换器

**Files:**
- Create: `src/tourism_agent/services/sse_stream.py`
- Test: `tests/services/test_sse_stream.py`

**Interfaces:**
- Produces: `SseEvent(event: str, data: dict[str, Any])`、`encode_sse_event()`、`GraphEventTranslator`。
- Consumes: LangGraph `astream_events(version="v2")` 的事件字典。

- [ ] **Step 1: 写入失败测试**

```python
def test_finalizer_token_is_converted_to_token_delta() -> None:
    translator = GraphEventTranslator()
    event = {
        "event": "on_chat_model_stream",
        "tags": ["orchestrator", "finalize", "public_output"],
        "data": {"chunk": AIMessageChunk(content="你好")},
    }

    assert translator.translate(event) == [
        SseEvent(event="token.delta", data={"text": "你好"})
    ]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests/services/test_sse_stream.py -q -p no:cacheprovider`

Expected: FAIL，因为 `sse_stream` 模块尚不存在。

- [ ] **Step 3: 实现最小事件模型与转换器**

```python
@dataclass(frozen=True)
class SseEvent:
    event: str
    data: dict[str, Any]

def encode_sse_event(event: SseEvent) -> bytes:
    return f"event: {event.event}\ndata: {json.dumps(event.data, ensure_ascii=False)}\n\n".encode()
```

仅转换最终输出标签的 Token，映射工具起止事件为安全的 `operation.*`，并映射模块节点起止事件为 `task.*`。

- [ ] **Step 4: 运行事件转换器测试确认通过**

Run: `.\.venv\Scripts\python.exe -m pytest tests/services/test_sse_stream.py -q -p no:cacheprovider`

Expected: PASS。

### Task 2: 后台流运行与同会话锁

**Files:**
- Modify: `src/tourism_agent/services/run_control.py`
- Modify: `src/tourism_agent/services/sse_stream.py`
- Test: `tests/api/test_run_control.py`
- Test: `tests/services/test_sse_stream.py`

**Interfaces:**
- Produces: `ThreadRunCoordinator.start()`，在取得 `thread_id` 锁后启动并返回后台 Task。
- Consumes: `operation: Callable[[], Awaitable[T]]` 与可选 `before_cancellation` 回调。

- [ ] **Step 1: 写入失败测试**

```python
async def test_start_keeps_thread_locked_until_background_operation_finishes() -> None:
    coordinator = ThreadRunCoordinator()
    started = asyncio.Event()
    release = asyncio.Event()

    task = await coordinator.start("thread-1", operation_that_waits(started, release))
    await started.wait()
    with pytest.raises(ThreadBusyError):
        await coordinator.start("thread-1", no_op)
    release.set()
    await task
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests/api/test_run_control.py -q -p no:cacheprovider`

Expected: FAIL，因为 `start` 方法尚不存在。

- [ ] **Step 3: 实现后台启动能力并令 execute 复用它**

`start` 在响应开始前占用锁、登记可取消任务，并在任务结束时释放登记与锁。`execute` 调用 `start` 后等待任务，保持原 API 行为。

- [ ] **Step 4: 运行测试确认通过**

Run: `.\.venv\Scripts\python.exe -m pytest tests/api/test_run_control.py -q -p no:cacheprovider`

Expected: PASS。

### Task 3: SSE API、运行完成与幂等处理

**Files:**
- Modify: `src/tourism_agent/api.py`
- Modify: `src/tourism_agent/graph/nodes/orchestrator.py`
- Test: `tests/api/test_message_stream.py`

**Interfaces:**
- Produces: `POST /messages/stream`。
- Consumes: 与 `MessageRequest` 相同的 JSON 请求体；成功时返回 `text/event-stream`。

- [ ] **Step 1: 写入失败 API 测试**

```python
def test_message_stream_emits_finalizer_token_and_result(client) -> None:
    with client.stream("POST", "/messages/stream", json=request_body) as response:
        body = response.read().decode()

    assert response.headers["content-type"].startswith("text/event-stream")
    assert 'event: token.delta' in body
    assert 'event: result' in body
    assert 'event: run.completed' in body
```

测试还覆盖 interrupt 返回 `interaction.required` 与 `result`，以及重复处理中请求的普通 JSON `202` 响应。

- [ ] **Step 2: 运行测试确认失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests/api/test_message_stream.py -q -p no:cacheprovider`

Expected: FAIL，因为流接口尚不存在。

- [ ] **Step 3: 实现流接口与运行收尾**

在 HTTP 头发送前完成所属用户、幂等与 resume 输入校验。后台生产者消费 `graph.astream_events`，事件经 `GraphEventTranslator` 过滤后进入无界队列；图结束后从 checkpoint 获取状态，复用既有结果组装与持久化语义，依次发送 `interaction.required`（如有）、`result` 与 `run.completed`。取消和异常发送安全事件及对应幂等结果，不泄露内部信息。

将 `create_finalize_node()` 的模型标签扩展为 `public_output`，以提供唯一的 Token 输出白名单。

- [ ] **Step 4: 运行 API 测试确认通过**

Run: `.\.venv\Scripts\python.exe -m pytest tests/api/test_message_stream.py tests/api/test_messages.py tests/api/test_run_control.py -q -p no:cacheprovider`

Expected: PASS。

### Task 4: 接口契约与全量验证

**Files:**
- Modify: `docs/api-contract.md`
- Modify: `docs/sse-streaming-design.md`（只在实现与原设计存在必要差异时）

**Interfaces:**
- Produces: `/messages/stream` 的请求、响应头、事件种类与重复请求行为说明。

- [ ] **Step 1: 更新契约文档**

为 OpenAPI 片段和接口说明增加 `POST /messages/stream`，明确 Fetch ReadableStream 使用方式、事件终止语义以及重复请求可能返回 JSON `202`。

- [ ] **Step 2: 执行完整验证**

Run: `.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider`

Expected: PASS。

Run: `.\.venv\Scripts\python.exe -m ruff check src tests`

Expected: PASS。
