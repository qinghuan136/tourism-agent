# SSE 流式输出设计

## 1. 目标与边界

本设计为 Tourism Agent 增加面向前端的流式反馈，解决模型与外部查询耗时期间页面缺少反馈的问题。
SSE 只输出用户可见的最终回答 Token、经过整理的执行进度、需要用户行动的 interrupt 和最终权威
结果，不暴露 LangGraph 内部状态与推理数据。

本阶段保留现有 `POST /messages`，新增流式消息入口；不实现事件持久化、断线续传、多进程事件
订阅，也不改变 Conversation、TripContext、CurrentItinerary 和 RAG 的存储职责。

## 2. 总体流程

```text
Frontend
  │ POST /messages/stream
  ▼
FastAPI SSE Endpoint
  │
  ▼
Message Execution
  │
  ├── LangGraph 模型事件 ──→ 只保留 public_output Token
  ├── 根图模块节点事件 ────→ 转成 task.*
  ├── Tool 生命周期事件 ────→ 转成 operation.*
  ├── interrupt ────────────→ interaction.required
  └── 最终 State ───────────→ result + run.completed
```

前端不得直接消费 LangGraph 的原始 `messages`、`updates` 或 GraphState。后端负责把框架事件转换成
稳定的项目事件契约，避免前端与节点名、State 字段及第三方 Tool 返回格式耦合。

## 3. 传输与事件格式

流式接口使用 `text/event-stream`。由于请求需要 JSON Body，前端使用 `fetch()` 读取
`ReadableStream`，而不是依赖只能通过 URL 建立连接的原生 `EventSource`。

每个事件使用 SSE 的 `event` 和 `data` 字段：

```text
event: operation.started
data: {"sequence":3,"idempotency_id":"...","timestamp":"...","operation_id":"...","tool":"get_weather","message":"正在查询天气"}

```

公共字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `sequence` | integer | 本次连接内从 1 递增的事件序号。 |
| `idempotency_id` | UUID | 当前消息请求的业务运行标识。 |
| `timestamp` | datetime | 服务端产生事件的时间。 |

`sequence` 只用于前端保持顺序和排查问题，不承诺跨连接重放。
后续各事件示例为突出业务字段，会省略这三个公共字段。

## 4. 事件类型

### 4.1 `run.started`

表示请求已经完成基础校验并进入执行。

```json
{
  "sequence": 1,
  "idempotency_id": "33333333-3333-3333-3333-333333333333",
  "timestamp": "2026-09-02T12:00:00+08:00",
  "trip_id": "22222222-2222-2222-2222-222222222222",
  "message": "正在处理你的请求"
}
```

### 4.2 `task.started` / `task.completed`

表示根图调度的用户可理解模块开始、完成。编排器自身及子图内部节点不会单独生成事件，避免前端收到
重复进度。

```json
{
  "task_id": "task_1",
  "module": "research",
  "message": "正在进行深度调研"
}
```

`module` 限定为：

```text
planning | explore | research | helper
```

### 4.3 `task.result`

每个子图 Task 完成后立即输出标准 TaskResult。该事件用于前端维护“当前运行”的任务结果，不写入
Conversation，也不代替整轮最终的 `result` 事件。一次运行因 `ask_user` 或候选确认跨越多个 resume
连接时，前端保留此前已完成的 TaskResult；用户发起新的独立请求时再清空。

```json
{
  "task_id": "task_1",
  "module": "research",
  "status": "success",
  "result": "已核实目标地点的开放时间、交通与游览限制。"
}
```

只有子图真正返回 `latest_task_result` 时，后端才依次发送 `task.result` 和 `task.completed`。子图进入
interrupt、仍等待用户回答时，两种完成事件都不发送；resume 后完成该任务时再发送。

Research 可以发送“制定调研计划”“搜集和核实资料”“综合调研结论”等阶段提示，但不得发送完整
ResearchPlan、TaskSpec、HandoffContext 或中间报告。

### 4.4 `operation.started` / `operation.completed` / `operation.failed`

表示用户可感知的 Tool 操作。事件包含稳定 Tool 名、同一次调用的 `operation_id` 和后端生成的
简短中文说明。

```json
{
  "operation_id": "tool-call-123",
  "tool": "get_weather",
  "message": "正在查询广州天气"
}
```

`operation.failed` 表示单个 Tool 失败但 Agent 仍可能继续，不等同于整次请求失败。不得在事件中
返回 Tool 原始结果、网页正文、供应商响应或异常堆栈。

首批状态映射：

| Tool | 开始提示 |
| --- | --- |
| `get_weather` | 正在查询天气 |
| `search_places` | 正在搜索相关地点 |
| `search_nearby_places` | 正在搜索附近地点 |
| `get_place_details` | 正在核实地点详情 |
| `plan_route` | 正在查询出行路线 |
| `measure_travel_distance` | 正在比较路程和耗时 |
| `web_search` | 正在搜索网页信息 |
| `extract_web_content` | 正在读取网页内容 |
| `map_web_site` | 正在分析网站结构 |
| `crawl_web_site` | 正在抓取相关页面 |
| Conversation 召回 | 正在检索相关历史对话 |
| `update_trip_context` | 正在保存旅行信息 |

`ask_user` 不作为 operation 发送，它会转换为 `interaction.required`。

### 4.5 `token.delta`

只发送最终用户可见回答的文本增量：

```json
{"text": "根据查询结果，广州未来三天"}
```

当前只允许带有 `public_output` tag 的 Orchestrator Finalizer 输出 Token。以下模型调用全部属于
内部过程，不允许转发：

- Orchestrator planner / reviewer；
- Planning、Explore、Helper 的 ReAct 中间调用；
- Research planner / researcher / synthesis；
- RAG 查询增强和 Chunk 语义增强；
- Tool Call 参数增量和模型 reasoning。

后端可对 Token 做短时间或短字符批量合并，避免一 Token 一事件造成前端频繁渲染。

### 4.6 `interaction.required`

Graph interrupt 时通知前端切换到等待用户输入状态。

主动提问：

```json
{
  "kind": "ask_user",
  "question": "你的旅行预算大概是多少？",
  "allowed_answers": null,
  "candidate_itinerary": null
}
```

候选行程确认：

```json
{
  "kind": "candidate_confirmation",
  "question": "是否采用这份候选行程？",
  "allowed_answers": ["是", "否"],
  "candidate_itinerary": "完整候选行程……"
}
```

该事件之后仍发送 `result`，最后以 `run.completed(status=waiting_user)` 关闭本次连接。

### 4.7 `result`

整次流的权威业务结果，Payload 与现有 `MessageResponse` 保持一致：

```json
{
  "route": "helper",
  "message": "根据查询结果，广州未来三天可能有阵雨……",
  "candidate_itinerary": null,
  "current_itinerary": null
}
```

前端用 `token.delta` 构建临时文本，收到 `result` 后必须以 `result.message` 覆盖临时文本，不能
再次追加。Conversation 和 RAG Chunk 也只保存最终 `result.message`，不保存 Token、进度事件或
内部模型内容。

### 4.8 `error`

表示整次请求无法继续：

```json
{
  "code": "MODEL_ERROR",
  "message": "模型调用失败，请稍后重试",
  "retryable": true
}
```

流建立前的 Trip 归属、幂等冲突和参数错误继续使用 HTTP `404`、`409`、`422`。流建立后无法
修改 HTTP 状态，因此运行错误发送 `error`，随后发送 `run.completed(status=failed)`。

### 4.9 `run.cancelled`

当前连接对应的运行被取消时发送：

```json
{"message": "本次运行已取消"}
```

随后发送 `run.completed(status=cancelled)`。

### 4.10 `run.completed`

所有正常、等待、取消和失败路径都以该事件结束：

```json
{"status": "completed"}
```

`status` 限定为：

```text
completed | waiting_user | cancelled | failed
```

## 5. 典型事件时序

普通查询：

```text
run.started
→ task.started
→ operation.started
→ operation.completed
→ task.result
→ task.completed
→ token.delta × N
→ result
→ run.completed(completed)
```

Agent 提问或候选确认：

```text
run.started
→ task.started
→ interaction.required
→ result
→ run.completed(waiting_user)
```

Tool 失败但 Agent 恢复：

```text
run.started
→ operation.started
→ operation.failed
→ operation.started
→ operation.completed
→ task.result
→ task.completed
→ token.delta × N
→ result
→ run.completed(completed)
```

整体失败：

```text
run.started
→ operation.started
→ operation.failed
→ error
→ run.completed(failed)
```

## 6. 幂等、取消与持久化

- 流式请求继续使用前端生成的 `idempotency_id`。
- Trip 归属校验、幂等认领和同 Trip 运行互斥应在返回 SSE Header 前完成。
- Graph Producer 必须在整个执行期间持有 `thread_id` 运行锁。
- `/trips/{trip_id}/cancel` 取消 Graph Producer，而不是仅关闭 SSE 输出协程。
- SSE 网络连接断开不等同于业务取消；Graph Producer 继续运行并完成终态持久化，只有显式调用取消
  接口才停止运行。
- 连接断开后，后端立即丢弃该连接尚未消费的事件，并停止继续向其缓存 Token/进度，避免后台运行
  因不可达客户端持续占用内存。
- 正常结果、interrupt 和错误终态继续写入幂等记录。
- Assistant Conversation、CurrentItinerary、RAG Chunk 的提交时机维持现有规则。
- 首版不保存 SSE 中间事件；网络断开后不能重放已经错过的进度与 Token。
- 相同幂等请求仍在执行时返回现有 `202 processing` JSON；已经结束时直接重放缓存的最终 JSON，
  前端必须先检查 HTTP 状态和 `Content-Type`，只有 `text/event-stream` 才进入 SSE 解析。

## 7. 禁止输出的数据

- 完整 GraphState、State updates 和 checkpoint 数据；
- System Prompt、内部 HumanMessage、ToolMessage；
- 模型 reasoning、内部结构化 JSON 和 Tool Call 参数流；
- OrchestrationPlan、ReviewDecision、ResearchPlan、HandoffContext；
- RAG 改写文本、向量、召回原文和相似度；
- Tool 原始结果、网页正文和供应商响应；
- 数据库对象、连接信息、API Key、Base URL；
- Python 异常堆栈及内部文件路径。

## 8. 前端状态映射

前端至少维护：

```text
idle | running | waiting_user | completed | cancelled | failed
```

- `run.started`：进入 `running`，禁用发送按钮，显示取消按钮；
- `task.*`、`operation.*`：更新独立进度区域，不写入聊天 Conversation；
- `token.delta`：追加临时回答；
- `interaction.required`：准备提问或确认控件；
- `result`：覆盖最终消息并更新行程；
- `run.completed`：根据 status 收束 UI 并关闭流。

## 9. 验收标准

1. 最终回答能够逐段显示，且 `result.message` 与数据库 Assistant Conversation 一致。
2. 天气、地点、路线和网页查询能够显示开始与结束状态。
3. Orchestrator、Research 和 RAG 的内部输出不会进入 SSE。
4. interrupt 能通过 `interaction.required` 让前端进入正确交互模式。
5. Tool 可恢复失败不会错误终止整个流；整体失败以 `error + run.completed(failed)` 收束。
6. 取消后释放 Trip 运行锁，幂等记录与现有取消语义一致。
7. 现有非流式 `/messages` 行为保持兼容。
