# 前后端接口契约

本文档以当前 FastAPI 实现为准，面向前端联调。服务默认地址为
`http://127.0.0.1:8000`。除 `POST /messages/stream` 的响应使用
`text/event-stream` 外，其他接口均使用 `application/json`。

当前未提供登录鉴权、用户/旅行创建与查询列表接口；前端需要在进入对话页前已经持有合法的
`user_id` 与 `trip_id`。UUID 字段均采用标准 UUID 字符串。

## 交互约定

### 消息与幂等键

- 每一次**新的用户意图**都要由前端生成新的 `idempotency_id`（UUID）。
- 因网络超时等原因重试同一请求时，必须复用原来的 `idempotency_id`，且
  `user_id`、`trip_id`、`message` 必须完全一致；后端会重放已完成请求的原始响应。
- 同一个幂等键仍在执行时，接口返回 `202` 和 `status: processing`。当前没有按幂等键查询
  执行结果的接口，前端应保留原请求的等待状态，或由用户取消后发起新消息。
- 消息会去除首尾空白，去除后不得为空，最大长度为 4000 个字符。

### Agent 提问与候选行程确认

`POST /messages` 与 `POST /messages/stream` 的最终 `MessageResponse.message` 永远是可直接显示的
对话文本；完整行程仅使用 `candidate_itinerary` 和 `current_itinerary` 展示，不应重复拼入普通对话
气泡。

- 当 Agent 主动询问补充信息时，前端将用户回答作为新的 `/messages` 请求发送即可。
- 当响应中 `candidate_itinerary` 非空时，当前请求正在等待用户确认候选行程；下一条消息必须
  是精确的 `是` 或 `否`，否则接口返回 `422`。该回复也必须使用新的 `idempotency_id`。
- `current_itinerary` 是当前已确认的行程全文；未确认或尚无行程时为 `null`。
- 单个 `trip_id` 同一时刻只允许一个图运行。若要放弃执行中任务或等待中的提问，调用取消接口；
  取消不会回滚此前已经发生的外部副作用。

## 接口总览

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/health` | 最小存活检查。 |
| `GET` | `/trips/{trip_id}/bootstrap` | 加载 Trip 页面的最近对话与当前已确认行程。 |
| `GET` | `/trips/{trip_id}/conversations` | 按消息 ID 游标加载更早的历史对话。 |
| `POST` | `/messages` | 发送用户消息、恢复被 Agent 打断的对话，或确认候选行程。 |
| `POST` | `/messages/stream` | 以 SSE 返回消息执行进度、最终回答 Token 与最终结果。 |
| `POST` | `/trips/{trip_id}/cancel` | 取消该旅行正在执行的任务，或清除等待用户回答的 checkpoint。 |

## `GET /health`

用于本地启动检查与部署健康探针，不检查数据库、模型或外部旅行服务的可用性。

成功响应 `200`：

```json
{"status": "ok"}
```

## `GET /trips/{trip_id}/bootstrap`

进入 Trip 页面时一次加载最近的 Conversation 和当前已确认行程。当前接口不读取 Candidate、
pending interaction 或 checkpoint 运行状态。

查询参数：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `user_id` | 是 | 当前用户 UUID，用于校验 Trip 归属。 |
| `message_limit` | 否 | 首屏消息数，默认 30，范围 1～100。 |

成功响应 `200`：

```json
{
  "trip_id": "22222222-2222-2222-2222-222222222222",
  "conversations": {
    "items": [
      {
        "id": 101,
        "role": "user",
        "content": "帮我规划广州三日游",
        "created_at": "2026-09-02T10:20:30+08:00",
        "exchange_id": "33333333-3333-3333-3333-333333333333"
      }
    ],
    "next_before_id": 101,
    "has_more": true
  },
  "current_itinerary": "广州三日已确认行程……"
}
```

`items` 按消息 ID 正序返回，可直接渲染。`has_more=true` 时，使用 `next_before_id` 调用历史
分页接口；尚无已确认行程时 `current_itinerary` 为 `null`。

## `GET /trips/{trip_id}/conversations`

用户向上滚动时，读取 `before_id` 之前的历史消息。接口不会返回游标消息本身，因此相邻页面
不会重复。

查询参数：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `user_id` | 是 | 当前用户 UUID，用于校验 Trip 归属。 |
| `before_id` | 否 | 只返回 ID 小于该值的消息；不传时读取最新一页。 |
| `limit` | 否 | 每页消息数，默认 30，范围 1～100。 |

成功响应 `200`：

```json
{
  "items": [
    {
      "id": 71,
      "role": "user",
      "content": "我更喜欢自然景观。",
      "created_at": "2026-08-28T09:10:00+08:00",
      "exchange_id": null
    }
  ],
  "next_before_id": null,
  "has_more": false
}
```

`exchange_id` 允许为 `null`，前端不能依赖它决定消息是否显示。两个读取接口在 Trip 不属于
当前用户时均返回 `404`：

```json
{"detail": "未找到当前用户对应的旅行"}
```

## `POST /messages`

发送一条用户消息。若当前 `trip_id` 存在等待中的 Agent 提问，后端自动将本次消息作为恢复输入；
前端无需传递额外的 `resume` 标志。

请求体：

```json
{
  "user_id": "11111111-1111-1111-1111-111111111111",
  "trip_id": "22222222-2222-2222-2222-222222222222",
  "idempotency_id": "33333333-3333-3333-3333-333333333333",
  "message": "帮我规划广州三日游"
}
```

成功响应 `200`：

```json
{
  "route": "planning",
  "message": "我已整理好一份候选方案，请确认是否采用。",
  "candidate_itinerary": "第 1 天：广州塔……",
  "current_itinerary": null
}
```

字段说明：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `route` | `planning` \| `explore` \| `research` \| `helper` | 本次请求最后执行的业务模块。 |
| `message` | string | 用户可见的普通文本或 Agent 提问。 |
| `candidate_itinerary` | string \| null | 待用户确认的完整候选行程；非空时下一次回复只能为 `是` 或 `否`。 |
| `current_itinerary` | string \| null | 当前已确认行程的完整文本。 |

处理中响应 `202`：表示相同幂等键的首次请求尚未结束。

```json
{
  "idempotency_id": "33333333-3333-3333-3333-333333333333",
  "status": "processing"
}
```

常见错误：

| 状态码 | `detail` | 含义与前端处理 |
| --- | --- | --- |
| `404` | `未找到当前用户对应的旅行` | `trip_id` 不属于该用户，回到旅行选择页。 |
| `409` | `当前旅行正在处理中，请先取消` | 同一旅行存在另一条不同请求，禁用重复发送或提示用户取消。 |
| `409` | `当前运行已取消` | 本次运行被取消，不重用此幂等键，用户可重新发送。 |
| `409` | `idempotency_id 已用于不同的请求内容` | 同一幂等键绑定了不同请求，前端生成新 UUID 后重试。 |
| `422` | `候选方案确认只接受“是”或“否”` | 当前处于候选行程确认，提供明确的确认/拒绝按钮。 |
| `422` | FastAPI 校验详情 | 请求字段缺失、UUID 非法或消息为空/超过 4000 字符。 |
| `500` | `消息处理失败，请稍后使用新的 idempotency_id 重试` | 后端执行失败，展示错误并在重试时生成新 UUID。 |

## `POST /messages/stream`

请求体与 `POST /messages` 完全相同，但成功时响应头为 `Content-Type: text/event-stream`。由于请求
需要 JSON Body，前端应使用 `fetch()` 读取 `ReadableStream`，而不是原生 `EventSource`。

每个 SSE 事件的 `data` 均额外包含：

| 字段 | 说明 |
| --- | --- |
| `sequence` | 当前连接内从 1 递增的事件序号。 |
| `idempotency_id` | 本次消息请求的 UUID。 |
| `timestamp` | 服务端生成事件的 UTC 时间。 |

事件类型与主要用途：

| `event` | 用途 |
| --- | --- |
| `run.started` | 请求已经通过基础校验，开始运行。 |
| `task.started` / `task.completed` | Planning、Explore、Research 或 Helper 模块开始/结束。 |
| `task.result` | 某个子图 Task 完成后立即返回标准结果；字段见下表。它必须先于对应的 `task.completed`，且不写入 Conversation。 |
| `operation.started` / `operation.completed` / `operation.failed` | 天气、地点、路线、网页或历史查询等安全进度。不会包含原始参数和结果。 |
| `token.delta` | 仅最终可见回答的文本增量。 |
| `interaction.required` | Agent 提问或候选行程确认；候选确认的 `allowed_answers` 固定为 `['是', '否']`。 |
| `result` | 权威 `MessageResponse`；前端必须用其 `message` 覆盖此前的 Token 临时文本。 |
| `error` | 流建立后的整体失败，不含内部异常细节。 |
| `run.cancelled` | 当前运行被取消。 |
| `run.completed` | 每条流的终止事件，`status` 为 `completed`、`waiting_user`、`cancelled` 或 `failed`。 |

`task.result` 的业务字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `task_id` | `string` | Orchestrator 为当前 Task 分配的标识。 |
| `module` | `planning \| explore \| research \| helper` | 执行该 Task 的子图。 |
| `status` | `success \| partial \| failed` | 标准 TaskResult 状态。 |
| `result` | `string` | 子图交给后续编排和用户查看的结果文本。 |

子图进入 interrupt 时任务尚未完成，因此不发送 `task.result` 和 `task.completed`；resume 后真正产生
TaskResult 时再依次发送。前端应把 TaskResult 作为当前运行的临时展示数据独立保存，不追加到
Conversation；同一次运行跨 resume 时保留，新独立请求开始时清空。

示例：

```text
event: operation.started
data: {"sequence":3,"idempotency_id":"33333333-3333-3333-3333-333333333333","timestamp":"2026-09-02T12:00:00+00:00","operation_id":"...","tool":"get_weather","message":"正在查询天气"}

event: result
data: {"sequence":8,"idempotency_id":"33333333-3333-3333-3333-333333333333","timestamp":"2026-09-02T12:00:02+00:00","route":"planning","message":"已整理旅行建议。","candidate_itinerary":null,"current_itinerary":null}

event: run.completed
data: {"sequence":9,"idempotency_id":"33333333-3333-3333-3333-333333333333","timestamp":"2026-09-02T12:00:02+00:00","status":"completed"}
```

流连接断开不会自动取消图运行；后端会丢弃该断线连接未消费的进度和 Token，用户明确放弃时仍调用
取消接口。相同幂等键正在运行时和已经完成时，
该接口与 `/messages` 一样返回普通 JSON（分别为 `202` 或缓存的终态响应），前端应先检查
`Content-Type` 再按 SSE 解析。

## `POST /trips/{trip_id}/cancel`

取消执行中任务，或清除当前等待用户回答/确认的 LangGraph checkpoint。接口成功返回并不代表一定有
任务被取消：`cancelled: false` 表示该旅行当时没有可取消的运行或 checkpoint。

请求体：

```json
{
  "user_id": "11111111-1111-1111-1111-111111111111"
}
```

成功响应 `200`：

```json
{"cancelled": true}
```

错误响应：

```json
{"detail": "未找到当前用户对应的旅行"}
```

## OpenAPI 3.1 契约

以下 YAML 与当前接口字段、状态码和状态约束保持一致，可作为前端 OpenAPI Generator、Swagger
或 Postman 的导入起点。运行服务后，也可访问 `/docs` 查看 FastAPI 自动生成的交互文档。

```yaml
openapi: 3.1.0
info:
  title: Tourism Agent API
  version: 0.1.0
  description: 旅行 Agent 当前对外 HTTP 接口。
servers:
  - url: http://127.0.0.1:8000
paths:
  /health:
    get:
      summary: 健康检查
      description: 仅表示 HTTP 服务存活，不探测数据库、模型和外部旅行服务。
      responses:
        '200':
          description: 服务存活
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/HealthResponse'
  /messages:
    post:
      summary: 发送或恢复一条对话消息
      description: |
        首次消息启动根图；存在 checkpoint 时自动恢复等待中的 Agent。
        candidate_itinerary 非空时，下一条 message 必须为“是”或“否”。
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/MessageRequest'
      responses:
        '200':
          description: 图运行完成，或已产生一个等待用户回答的 interrupt。
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/MessageResponse'
        '202':
          description: 相同幂等请求正在处理中。
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/IdempotencyProcessingResponse'
        '404':
          $ref: '#/components/responses/TripNotFound'
        '409':
          $ref: '#/components/responses/Conflict'
        '422':
          $ref: '#/components/responses/ValidationError'
        '500':
          $ref: '#/components/responses/InternalError'
  /messages/stream:
    post:
      summary: 流式发送或恢复一条对话消息
      description: |
        请求体与 /messages 相同。新请求成功时返回 text/event-stream；
        相同幂等键正在处理中返回 application/json 的 202，已经结束时重放缓存 JSON 响应。
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/MessageRequest'
      responses:
        '200':
          description: SSE 事件流，以 result 事件输出权威 MessageResponse，并以 run.completed 收尾。
          content:
            text/event-stream:
              schema:
                type: string
                description: SSE 文本流；事件规范见本文档的 POST /messages/stream 说明。
        '202':
          description: 相同幂等请求正在处理中，响应为普通 JSON，不是 SSE。
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/IdempotencyProcessingResponse'
        '404':
          $ref: '#/components/responses/TripNotFound'
        '409':
          $ref: '#/components/responses/Conflict'
        '422':
          $ref: '#/components/responses/ValidationError'
  /trips/{trip_id}/bootstrap:
    get:
      summary: 加载 Trip 页面首屏数据
      description: 返回最近 Conversation 与当前已确认行程，不读取 Candidate 或 checkpoint 状态。
      parameters:
        - $ref: '#/components/parameters/TripId'
        - $ref: '#/components/parameters/UserId'
        - name: message_limit
          in: query
          required: false
          schema:
            type: integer
            minimum: 1
            maximum: 100
            default: 30
      responses:
        '200':
          description: Trip 页面首屏数据。
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/TripBootstrapResponse'
        '404':
          $ref: '#/components/responses/TripNotFound'
        '422':
          $ref: '#/components/responses/ValidationError'
  /trips/{trip_id}/conversations:
    get:
      summary: 分页读取 Trip 历史对话
      description: 按消息 ID 游标读取更早消息，页面内部保持时间正序。
      parameters:
        - $ref: '#/components/parameters/TripId'
        - $ref: '#/components/parameters/UserId'
        - name: before_id
          in: query
          required: false
          schema:
            type: integer
            minimum: 1
        - name: limit
          in: query
          required: false
          schema:
            type: integer
            minimum: 1
            maximum: 100
            default: 30
      responses:
        '200':
          description: 一页按时间正序排列的原始 Conversation。
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ConversationPage'
        '404':
          $ref: '#/components/responses/TripNotFound'
        '422':
          $ref: '#/components/responses/ValidationError'
  /trips/{trip_id}/cancel:
    post:
      summary: 取消旅行的当前运行
      description: 取消执行中任务，或清除等待用户回答的 checkpoint。
      parameters:
        - $ref: '#/components/parameters/TripId'
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/CancelRunRequest'
      responses:
        '200':
          description: 已完成取消检查。
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/CancelRunResponse'
        '404':
          $ref: '#/components/responses/TripNotFound'
        '422':
          $ref: '#/components/responses/ValidationError'
components:
  schemas:
    UUID:
      type: string
      format: uuid
    HealthResponse:
      type: object
      required: [status]
      properties:
        status:
          type: string
          const: ok
    MessageRequest:
      type: object
      required: [user_id, trip_id, idempotency_id, message]
      properties:
        user_id:
          $ref: '#/components/schemas/UUID'
        trip_id:
          $ref: '#/components/schemas/UUID'
        idempotency_id:
          $ref: '#/components/schemas/UUID'
          description: 前端为每个新的用户意图生成的 UUID。
        message:
          type: string
          minLength: 1
          maxLength: 4000
          description: 首尾空白会被后端移除。
    MessageResponse:
      type: object
      required: [route, message, candidate_itinerary, current_itinerary]
      properties:
        route:
          type: string
          enum: [planning, explore, research, helper]
        message:
          type: string
          minLength: 1
        candidate_itinerary:
          type: [string, 'null']
          minLength: 1
        current_itinerary:
          type: [string, 'null']
          minLength: 1
    IdempotencyProcessingResponse:
      type: object
      required: [idempotency_id, status]
      properties:
        idempotency_id:
          $ref: '#/components/schemas/UUID'
        status:
          type: string
          const: processing
    CancelRunRequest:
      type: object
      required: [user_id]
      properties:
        user_id:
          $ref: '#/components/schemas/UUID'
    CancelRunResponse:
      type: object
      required: [cancelled]
      properties:
        cancelled:
          type: boolean
    ConversationMessage:
      type: object
      required: [id, role, content, created_at, exchange_id]
      properties:
        id:
          type: integer
        role:
          type: string
          enum: [user, assistant]
        content:
          type: string
          minLength: 1
        created_at:
          type: string
          format: date-time
        exchange_id:
          type: [string, 'null']
          format: uuid
    ConversationPage:
      type: object
      required: [items, next_before_id, has_more]
      properties:
        items:
          type: array
          items:
            $ref: '#/components/schemas/ConversationMessage'
        next_before_id:
          type: [integer, 'null']
        has_more:
          type: boolean
    TripBootstrapResponse:
      type: object
      required: [trip_id, conversations, current_itinerary]
      properties:
        trip_id:
          $ref: '#/components/schemas/UUID'
        conversations:
          $ref: '#/components/schemas/ConversationPage'
        current_itinerary:
          type: [string, 'null']
          minLength: 1
    ErrorResponse:
      type: object
      required: [detail]
      properties:
        detail:
          description: 业务错误为字符串；字段校验失败时为 FastAPI 的错误详情数组。
  parameters:
    TripId:
      name: trip_id
      in: path
      required: true
      schema:
        type: string
        format: uuid
    UserId:
      name: user_id
      in: query
      required: true
      schema:
        type: string
        format: uuid
  responses:
    TripNotFound:
      description: 用户作用域下不存在该旅行。
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/ErrorResponse'
    Conflict:
      description: 当前旅行并发、已取消或幂等键被错误复用。
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/ErrorResponse'
    ValidationError:
      description: 请求格式不合法，或候选行程确认值不是“是”或“否”。
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/ErrorResponse'
    InternalError:
      description: 消息处理失败。
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/ErrorResponse'
```
