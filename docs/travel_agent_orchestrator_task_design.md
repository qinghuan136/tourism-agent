# Travel Agent Orchestrator-Task 架构设计

## 1. 目标与边界

根图由 Orchestrator 根据一次用户请求生成顺序 Task 计划，并按需要调用多个业务子图。
Orchestrator 负责理解目标、生成初始计划、调度子图、复核中间结果和整理最终回复，
但不直接查询旅行信息，也不直接修改业务数据。

本设计采用：

```text
初始计划
→ 顺序执行一个 Task
→ 观察 TaskResult
→ 保留、替换或终止剩余计划
→ 继续执行或结束
```

它是受约束的 `Plan → Execute → Observe → Replan`，不是生成计划后机械执行，也不是允许
Orchestrator 无限制地自由调用模块。

当前阶段明确不实现：

- 并行 Task 和通用 DAG；
- Task 数据库和独立 Artifact Store；
- Orchestrator 直接调用业务 Tools；
- 向用户展示或确认内部执行计划；
- 为内部 Task 单独持久化 Conversation 或 RAG Chunk；
- RAG 语义增强模型和 `memory_summary`；
- 新增业务子图或重构现有子图内部 ReAct 流程。

## 2. 组件职责

### 2.1 Orchestrator

Orchestrator 只承担四项职责：

1. 根据当前用户输入和少量近期 Conversation 生成初始计划；
2. 为当前 Task 选择已注册的业务子图；
3. 根据 TaskResult 复核用户目标是否完成，并调整尚未执行的 Task；
4. 综合本轮有效结果生成最终对话回复。

Orchestrator 使用受 Pydantic Schema 约束的模型输出。LLM 负责语义决策，程序负责校验任务数量、
任务类型和实际图跳转。

### 2.2 Task Component

现有子图继续彼此平级，并作为 Task Component：

```text
Orchestrator
├── ExploreGraph
├── ResearchGraph
├── PlanningGraph
└── HelperGraph
```

- Explore：开放式发现候选地点、活动和旅行方向；
- Research：围绕明确对象进行深入研究；
- Planning：生成或修改旅行计划，并沿用候选行程确认流程；
- Helper：处理简单问答、局部只读查询和无法归入其他模块的请求。

子图之间不能直接调用，也不需要知道其他子图的存在。每个子图仍按自己的需要加载近期
Conversation、相关历史、TripContext 和 CurrentItinerary。

### 2.3 Tool

Tool 仍是子图内部的原子能力。Orchestrator 不绑定 `web_search`、地点查询、天气查询、
TripContext 写入或行程写入等 Tools。

## 3. 总体运行流程

```text
START
  ↓
load_orchestrator_context
  ↓
create_plan
  ↓
prepare_next_task ←──────────────────────┐
  ↓                                      │
dispatch_task                            │
  ├── explore                            │
  ├── research                           │
  ├── planning                           │
  └── helper                             │
         ↓                               │
record_task_result                       │
         ↓                               │
review_plan                              │
  ├── continue ──────────────────────────┤
  ├── replace_remaining ─────────────────┘
  └── finish
         ↓
finalize
  ↓
END
```

节点分工：

- `load_orchestrator_context`：只加载制定计划所需的少量近期 Conversation；
- `create_plan`：通过结构化模型输出生成初始顺序计划；
- `prepare_next_task`：由程序从待执行列表中取出下一个 Task；
- `dispatch_task`：由程序根据 `task_type` 跳转到已注册子图；
- 子图节点：执行当前 Task，并返回对 Orchestrator 有效的结果；
- `record_task_result`：由程序记录结果并增加已执行数量；
- `review_plan`：通过结构化模型输出决定继续、替换剩余计划或结束；
- `finalize`：综合有效结果生成用户可见回复。

`dispatch_task` 使用确定性条件路由或 `Command`，不能让 LLM 返回任意 Python 节点名。

## 4. 初始计划

第一版只支持顺序任务：

```python
class TaskType(StrEnum):
    EXPLORE = "explore"
    RESEARCH = "research"
    PLANNING = "planning"
    HELPER = "helper"


class TaskSpec(BaseModel):
    """Orchestrator 交给子图的单个任务。"""

    task_id: str
    task_type: TaskType
    instruction: str


class OrchestrationPlan(BaseModel):
    """当前用户请求对应的初始执行计划。"""

    goal: str
    tasks: list[TaskSpec]
    notes: str = ""
```

`tasks` 最少包含一个任务，最多包含五个任务。`instruction` 使用自然语言，可以引用前序 Task
未来产生的结果，例如“调查 Explore 筛选出的最佳候选”，不要求初始计划提前知道具体地点。

简单请求只生成一个 Task。例如天气查询或轻量问答可直接生成 Helper Task，不要求为了体现
Orchestrator 而强行拆分任务。

## 5. TaskResult

子图正常结束后，根图将其对话输出转换为统一 TaskResult：

```python
class TaskStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class TaskResult(BaseModel):
    """子图提供给 Orchestrator 的有效结果。"""

    task_id: str
    task_type: TaskType
    status: TaskStatus
    result: str
```

当前实现中，Planning、Explore、Research 和 Helper 四个子图正常到达结束节点时，根图都只记录
`success`。`partial` 与 `failed` 仅为 TaskResult 契约预留，供后续在有明确状态传播设计时使用；当前
Reviewer 不会收到这两种状态。未处理异常继续向上抛出，方便当前开发阶段调试，不伪装成 `failed`
结果。

`result` 应包含下一步决策需要的有效信息，例如准确地点名、POI ID、时间、推荐结论、关键理由和
仍未核实的内容；不得包含完整 ReAct Messages、原始网页正文或无关 Tool 返回。

Research 的完整报告和 Planning 的完整行程仍通过各模块原有输出管理。TaskResult 只承担本轮
Orchestrator 决策需要的原始语义传递，不成为长期事实来源。所有已完成 TaskResult 在本轮 State
中只追加保存；下游子图不直接消费这些原文，而由 Reviewer 整理 `handoff_context`。

## 6. RootState

RootState 保存当前请求的临时编排状态：

```python
class RootState(TypedDict):
    user_id: UUID
    trip_id: UUID
    user_message_id: int
    user_input: str

    routing_context: NotRequired[list[ConversationMessage]]
    orchestration_goal: NotRequired[str]
    pending_tasks: NotRequired[list[TaskSpec]]
    current_task: NotRequired[TaskSpec | None]
    task_results: NotRequired[list[TaskResult]]
    latest_task_result: NotRequired[TaskResult | None]
    handoff_context: NotRequired[str]
    executed_task_count: NotRequired[int]

    route: NotRequired[TaskType]
    response: NotRequired[str]
    candidate_itinerary: NotRequired[str | None]
    current_itinerary: NotRequired[str | None]
```

执行始终是顺序的，因此这些列表字段默认整体覆盖，不添加并行聚合 reducer。节点不能原地修改
State，应返回明确的局部更新。

`latest_task_result` 只在子图结束与 `record_task_result` 之间短暂存在。兼容现有 API 的 `route`
字段继续保留，但其语义调整为“最近一次完成或当前中断的 Task 类型”，不再表示整轮请求只有一个
路由。

OrchestrationPlan、TaskSpec 和 TaskResult 只进入 GraphState/Checkpoint，不建立新的业务表。
正常结束后沿用当前 Checkpoint 清理策略。

## 7. Task 输入与上下文搬运

Orchestrator 为子图生成的输入必须明确区分：

```text
【原始用户目标】
帮我看看广州塔附近有没有适合闲逛的地方，如果有就加入行程。

【当前子任务】
深入调查沙面是否适合当前用户，并核实交通、开放情况和游览价值。

【Orchestrator整理的现有结果】
Explore 找到三个候选，其中沙面最符合休闲散步需求……
```

不能把全部 TaskResult 原样复制给每个后续子图。`review_plan` 应根据全部已完成结果和下一实际
Task，把真正需要的地点名、标识、结论、约束、证据与待核实项整理为自由文本
`handoff_context`。原始 TaskResult 继续完整保留，搜索过程和原始 Tool 输出不直接传递。

根图另行构造自动历史召回查询：

```text
【当前检索目标】
{current_task.instruction}

【用户总体目标】
{user_input}

【Orchestrator整理的现有结果】
{handoff_context，可为空}
```

执行消息告诉 Agent 当前要做什么，`retrieval_query` 告诉 RAG 应寻找什么。根图调用四个子图时
始终传入专用查询；为保留子图独立调试能力，直接调用子图且未提供该字段时，才回退到执行消息。

这些内部构造的 HumanMessage 只用于调用子图，不是用户真实消息，不得追加到长期 Conversation。

## 8. 计划复核

每个 Task 完成后，由 `review_plan` 返回：

```python
class ReviewAction(StrEnum):
    CONTINUE = "continue"
    REPLACE_REMAINING = "replace_remaining"
    FINISH = "finish"


class PlanReviewDecision(BaseModel):
    action: ReviewAction
    reason: str
    replacement_tasks: list[TaskSpec] = []
    handoff_context: str = ""
```

- `continue`：保留当前剩余计划；
- `replace_remaining`：只替换尚未执行的任务；
- `finish`：用户目标已完成，或中间结果表明无需继续。

`continue` 和 `replace_remaining` 必须提供非空交接上下文；`finish` 必须清空它。交接文本只能
整理已经存在的结果，不能生成新事实，也不替代原始 TaskResult。

已完成 TaskResult 只追加，Orchestrator 不能删除或改写。替换剩余计划后，程序仍校验合法任务
类型和总执行上限。

例如 Explore 没找到合适地点时，应直接 `finish`，不能继续执行无对象的 Research 和 Planning。
Research 不推荐第一候选但仍有备选时，可以用新的 Research Task 替换剩余计划。

## 9. 行程修改与用户确认

内部执行计划不向用户展示，也不需要用户提前确认。但是“内部计划自动执行”不等于“行程修改
自动生效”。

即使原始请求包含“如果合适就加入行程”，Planning 仍应先形成包含具体地点和调整影响的候选
行程，并沿用当前 `submit_candidate_itinerary → interrupt → 用户确认` 流程。用户确认后才写入
CurrentItinerary。

Planning 仍是唯一能够提交候选行程并触发行程写入的模块，Orchestrator 不能绕过该边界。

## 10. interrupt、resume 与取消

子图发生 interrupt 时，当前 Task、待执行计划和已完成结果已经存在于根图 Checkpoint：

```text
Orchestrator 正在执行 Research
→ Research 调用 ask_user
→ API 返回 interrupt
→ 用户 resume
→ 从 Research 中断点恢复
→ Research 返回 TaskResult
→ 根图继续 review_plan
```

resume 不重新执行 `create_plan`，也不重复已经完成的 Task。只有子图真正结束并返回 TaskResult 后，
`executed_task_count` 才增加。

取消继续沿用项目现有语义：停止后续执行并清理临时 Checkpoint，不回滚已经产生的数据库或外部
副作用。

## 11. Conversation 与 RAG 边界

Orchestrator 不改变现有切块规则：

```text
一次外部用户输入
+ 一次对用户可见的 Agent 输出
= 一个 Exchange / RAG Chunk
```

内部计划、TaskSpec、TaskResult、`handoff_context`、`retrieval_query`、子图调用消息、ReAct
Messages 和 Tool 结果都不是 Conversation，不得单独生成 Chunk。

一次请求内部即使依次执行 Explore、Research 和 Planning，正常结束时仍只保存原始 UserMessage
和 Orchestrator 最终 AssistantMessage。发生 interrupt 时，继续按现有规则把“当前外部输入 +
对用户可见的 interrupt 问题”作为一个 Exchange；resume 输入与下一次可见输出形成下一个
Exchange。

当前尚未实现语义增强，因此第一版要求 `finalize` 回复至少简要包含：

- 本轮完成了什么；
- 最终选择或结论；
- 最关键的选择理由；
- 是否修改了行程。

未来引入语义增强时，可以在 finalize 阶段生成不写入 Conversation 的 `memory_summary`，把原始
用户消息、最终回复和已确认的有效任务结论共同交给增强模型，生成
`conversation_rag_chunks.retrieval_text`。精确原文仍通过 Exchange 关联的原始 Conversation
读取。不得把被淘汰候选、原始 Tool 内容和未经确认的网页结论写入长期检索文本。

## 12. 最终响应

`finalize` 使用独立系统 Prompt，综合原始目标和 TaskResult 生成用户可见回复。它不得重复输出
完整 CurrentItinerary；完整行程继续由 API 的 `current_itinerary` 字段独立返回。Planning 处于
候选确认 interrupt 时，API 继续独立返回 `candidate_itinerary`。

示例：

```json
{
  "message": "我筛选并调研了沙面；在你确认后，已经将其安排到第二天下午。",
  "current_itinerary": "……完整行程……"
}
```

## 13. 执行护栏

第一版只保留必要护栏：

- 每次请求最多完成五个 Task；
- 每次只执行一个 Task；
- Task 类型只能来自 `TaskType`；
- Orchestrator 不绑定业务 Tool；
- 达到任务上限后基于已完成的 TaskResult 进入 finalize 并停止执行；
- 子图异常暂时向上抛出，方便调试；
- interrupt/resume 不重复计数；
- 不增加通用重试、补偿、并行依赖和任务持久化框架。

需要记录的核心日志包括：初始计划、当前 Task、TaskResult、复核决策、剩余 Task 和最终执行数量。

## 14. 典型示例

用户请求：

```text
帮我看看广州塔附近有没有适合闲逛的地方，如果有就加入我的行程。
```

执行过程：

```text
create_plan
  1. Explore：寻找并筛选候选
  2. Research：深入核实最佳候选
  3. Planning：把确认合适的地点加入行程

Explore
  → 找到沙面、二沙岛、海心沙，建议优先调查沙面

review_plan
  → 把 Research instruction 改写为“深入调查沙面……”

Research
  → 沙面适合休闲散步，交通与现有路线兼容，建议加入

review_plan
  → 把 Planning instruction 改写为“将沙面合理加入第二天下午……”

Planning
  → 生成候选行程
  → interrupt 等待用户确认
  → 确认后写入 CurrentItinerary

review_plan
  → 用户目标已经完成

finalize
  → 返回简短说明，API 独立返回完整 CurrentItinerary
```

最终边界保持：

```text
Orchestrator = Planner + Router + Context Manager + Finalizer
Subgraph     = Task Component
Tool         = 原子能力
DB           = 长期权威事实
GraphState   = 当前请求的临时编排状态
Conversation = 用户可见、只追加的外部对话
```
