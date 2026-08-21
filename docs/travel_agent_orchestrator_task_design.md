# Travel Agent Orchestrator-Task 架构设计

## 1. 总体目标

将原本只负责单次意图路由的 `IntentAgent` 升级为 **Orchestrator**。

Orchestrator 不直接完成具体旅行任务，而是负责：

- 理解当前用户输入
- 判断是否包含多个连续任务
- 动态拆分并编排子图调用顺序
- 为每个子图生成明确的 Task 输入
- 读取上一 Task 的结果，提炼对下一 Task 有用的信息
- 判断任务是否已经完成、是否需要继续执行下一 Task
- 必要时根据中间结果重新规划后续任务

整体流程：

```text
User Input
    ↓
Orchestrator
    ↓
生成 Task 1
    ↓
Subgraph / Task Component
    ↓
TaskResult
    ↓
Orchestrator
    ↓
结合：
- 原始用户输入
- 已完成任务
- 当前 TaskResult
- 业务状态

决定：
- 是否结束
- 是否请求用户补充
- 下一 Task 是什么
- 下一 Task 输入是什么
    ↓
Task 2
    ↓
...
    ↓
Finalize
```

---

## 2. 子图定位

现有业务子图都视为 **Task Component**，彼此平级，例如：

```text
Root / Orchestrator
├── ExploreGraph
├── ResearchGraph
├── PlanningGraph
├── PreTripGraph
├── InTripGraph
└── PostTripGraph
```

每个子图只负责完成一个明确任务：

```text
TaskInput
    ↓
Subgraph
    ↓
TaskResult
```

子图之间不直接依赖、也不需要知道其他子图的存在。

例如：

- `ExploreGraph`：开放式探索、寻找候选目的地/POI
- `ResearchGraph`：围绕指定地点或主题做深度调研
- `PlanningGraph`：生成或修改旅行计划

---

## 3. Orchestrator 的职责

Orchestrator 同时承担三类职责。

### 3.1 Planner

根据用户请求决定需要执行哪些任务。

例如：

```text
用户：
“帮我找几个北京小众古建筑，
挑两个合适的直接放进第二天行程。”

↓

可能的任务链：

Explore
→ Planning
```

任务链不要求一次性完全确定。

Orchestrator 可以执行一个 Task 后，根据结果动态决定下一步，实现：

```text
Plan
→ Act
→ Observe
→ Replan
```

例如：

```text
用户：
“研究一下西贡，如果适合我就放进行程。”

Research
↓
结果：不推荐
↓
Orchestrator 判断条件不成立
↓
不再调用 Planning
```

---

### 3.2 Router

Orchestrator 根据当前目标选择需要调用的子图。

允许的 Task 类型应由程序限制，例如：

```python
TaskType = Literal[
    "explore",
    "research",
    "planning",
    "pre_trip",
    "in_trip",
    "post_trip",
]
```

LLM 负责决策，但真正可执行的 Task 类型由系统约束。

---

### 3.3 Context Manager

Orchestrator 负责在 Task 之间进行**语义级上下文搬运**。

原则不是：

```text
Task A 全量输出
→ 原样复制
→ Task B
```

而是：

```text
Task A Result
↓
Orchestrator 理解
↓
筛选与重组
↓
只保留 Task B 真正需要的信息
↓
生成 Task B Input
```

例如 Explore 返回：

```text
找到了智化寺、白塔寺、法源寺等候选。
其中智化寺和法源寺最符合用户偏好的“小众、历史文化”特征。
智化寺 POI ID 为 xxx，法源寺 POI ID 为 yyy。
```

Orchestrator 为 Planning 生成：

```text
目标：
将 Explore 阶段筛选出的地点加入第二天行程。

有效上下文：
- 智化寺，POI ID=xxx
- 法源寺，POI ID=yyy
- 两者最符合用户“小众、历史文化”的偏好

要求：
合理安排到第二天，并尽量保持其他已确认计划不变。
```

搜索过程、被淘汰候选、无关网页内容等不继续传递。

---

## 4. TaskInput

每个 Task 应收到一个明确、独立的任务描述。

第一版可以保持简单：

```python
class TaskSpec(BaseModel):
    task_type: TaskType
    instruction: str
```

其中：

- `task_type`：决定调用哪个子图
- `instruction`：Orchestrator 为该 Task 生成的自然语言任务输入

如果后续确实需要，可以增加：

```python
task_id: str
```

用于记录执行链和调试。

---

## 5. TaskResult

Task 不应只返回裸 `str`。

至少需要：

```python
class TaskResult(BaseModel):
    status: Literal["success", "partial", "failed"]
    result: str
```

### status

用于让 Orchestrator 稳定判断任务执行状态：

- `success`：任务正常完成
- `partial`：得到部分结果，但存在缺失信息或部分外部能力失败
- `failed`：任务无法完成

避免让 Orchestrator仅靠自然语言猜测任务是否成功。

### result

`result` 使用自然语言描述 Task 的有效产出。

它的主要消费者是 Orchestrator，因此应：

- 包含下一步决策所需的重要信息
- 尽量保留关键标识，如 POI ID、地点名、日期等
- 避免输出无意义的执行过程
- 避免塞入大量原始 Tool 返回内容

---

## 6. 业务状态与 TaskResult 分离

TaskResult 只负责**任务间临时语义传递**，不能承担长期业务状态存储。

长期或权威业务事实仍由 DB / Shared State 管理，例如：

```text
UserProfile
TripContext
CurrentItinerary
Conversation History
用户明确确认的选择
```

因此：

```text
长期业务事实
→ PostgreSQL / Shared State

Task 中间结果
→ TaskResult.result

跨 Task 临时上下文
→ Orchestrator 从 Result 中提炼并生成下一 TaskInput
```

例如 Planning 不应该依赖上一 Task 用自然语言复制完整 `CurrentItinerary`，而应该直接读取当前权威 itinerary。

---

## 7. 用户补充与 interrupt

Task 执行过程中如果发现必须由用户决定或补充信息，可以通过现有 interrupt 机制暂停。

例如：

```text
Explore
↓
找到 5 个候选
↓
用户之前要求“先给我看看，我自己选”
↓
interrupt
↓
等待用户选择
```

恢复后仍继续当前 Task 或返回 Orchestrator。

如果用户已经明确授权：

```text
“你帮我选好之后直接放进行程”
```

则无需 interrupt：

```text
Explore
↓
TaskResult
↓
Orchestrator
↓
Planning
```

---

## 8. 执行护栏

为了防止 Orchestrator 无限调用子图，需要程序级限制。

建议至少包含：

```python
MAX_TASKS_PER_TURN = 5
```

并记录：

```text
已执行 Task 数量
已执行 Task 类型及结果摘要
```

达到上限时：

- 直接 finalize 当前已有结果，或
- 请求用户进一步确认

同时禁止 Orchestrator 动态创造未注册的 Task 类型。

---

## 9. 示例

用户：

```text
“帮我找一下广州最近适合我的展览，
挑一个最值得去的，放进周六行程。”
```

执行过程：

```text
User Input
    ↓
Orchestrator
    ↓
Task 1: Explore
instruction:
“寻找广州近期适合用户偏好的展览，
并筛选最值得关注的候选。”
    ↓
ExploreGraph
    ↓
TaskResult
status = success
result =
“找到三个较合适的展览，其中 A 最符合用户偏好……”
    ↓
Orchestrator
    ↓
发现用户目标尚未完成：
还需要修改行程
    ↓
Task 2: Planning
instruction:
“根据 Explore 结果，
将最匹配的展览 A 合理安排进周六现有行程，
尽量不破坏已确认安排。”
    ↓
PlanningGraph
    ↓
TaskResult
    ↓
Orchestrator
    ↓
目标完成
    ↓
Finalize
```

---

## 10. 当前架构原则

最终保持以下边界：

```text
Orchestrator
= Planner + Router + Context Manager

Subgraph
= Task Component

Tool
= 原子能力
```

数据边界：

```text
DB / Shared State
→ 长期、权威业务事实

TaskResult
→ 当前 Task 的结构化状态 + 自然语言结果

Orchestrator
→ 根据用户目标和 TaskResult
  动态裁剪、重组下一 Task 的输入
```

当前阶段优先保持实现简单，不引入 Artifact Store、复杂 DAG、Schema Registry 等基础设施。

如果后续实际出现字符串结果不稳定、关键 ID 丢失、Task 结果过长、并行任务依赖复杂等问题，再针对真实问题升级结构化 Artifact 或更复杂的工作流机制。
