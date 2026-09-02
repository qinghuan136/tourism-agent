# Orchestrator Task Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将根图从单次意图路由升级为可生成初始计划、顺序执行多个业务子图、复核中间结果并整理最终回复的 Orchestrator。

**Architecture:** Orchestrator 使用结构化模型输出生成 1～5 个顺序 Task，并在每个 Task 后返回继续、替换剩余计划或结束的复核决策。实际节点跳转、任务计数和写行程边界由确定性代码控制；现有 Planning、Explore、Research、Helper 子图保持独立并自行加载业务 Context。

**Tech Stack:** Python、Pydantic、LangGraph StateGraph、LangChain Messages、pytest

**Spec:** `docs/travel_agent_orchestrator_task_design.md`

## Global Constraints

- 内部计划自动执行，不增加计划确认交互。
- 每次请求最多完成 5 个 Task，并且只顺序执行。
- Orchestrator 不绑定或调用领域 Tool。
- 子图之间不直接调用，所有跨模块执行都经过根图调度。
- Planning 继续独占候选行程确认和 CurrentItinerary 写入。
- TaskSpec、TaskResult 和内部子图调用消息不写入 Conversation，也不单独生成 RAG Chunk。
- 一次外部输入与一次用户可见输出继续组成一个 Exchange。
- 当前不实现并行 Task、通用 DAG、任务数据库、Artifact Store 或 RAG 语义增强。
- API `route` 保持兼容，表示最近完成或当前中断的 Task 类型。
- 不自动创建 Git commit，保留当前工作区已有未提交修改。

---

### Task 1: Orchestrator 数据契约与 RootState

**Files:**
- Create: `src/tourism_agent/models/orchestration.py`
- Modify: `src/tourism_agent/models/contracts.py`
- Modify: `src/tourism_agent/graph/state.py`
- Create: `tests/models/test_orchestration.py`
- Modify: `tests/models/test_contracts.py`

**Interfaces:**
- Consumes: Pydantic `BaseModel`、现有 `ConversationMessage` 和消息 API 契约。
- Produces: `TaskType`、`TaskSpec`、`OrchestrationPlan`、`TaskStatus`、`TaskResult`、`ReviewAction`、`PlanReviewDecision`、扩展后的 `RootState`。

- [ ] **Step 1: 写失败测试，固定任务数量、任务类型和复核动作**

```python
def test_orchestration_plan_accepts_one_to_five_registered_tasks() -> None:
    plan = OrchestrationPlan(
        goal="找到合适地点并加入行程",
        tasks=[
            TaskSpec(
                task_id="task_1",
                task_type="explore",
                instruction="寻找附近适合闲逛的地点",
            )
        ],
    )
    assert plan.tasks[0].task_type is TaskType.EXPLORE


def test_orchestration_plan_rejects_more_than_five_tasks() -> None:
    with pytest.raises(ValidationError):
        OrchestrationPlan(
            goal="过长计划",
            tasks=[
                TaskSpec(
                    task_id=f"task_{index}",
                    task_type="helper",
                    instruction=f"执行任务 {index}",
                )
                for index in range(6)
            ],
        )


def test_review_decision_rejects_unknown_action() -> None:
    with pytest.raises(ValidationError):
        PlanReviewDecision(action="retry_forever", reason="无效动作")
```

- [ ] **Step 2: 运行测试并确认契约尚不存在**

Run: `\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\models\test_orchestration.py`

Expected: FAIL，错误包含 `ModuleNotFoundError: tourism_agent.models.orchestration`。

- [ ] **Step 3: 实现最小 Pydantic 契约**

```python
OrchestrationText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class TaskType(StrEnum):
    PLANNING = "planning"
    EXPLORE = "explore"
    RESEARCH = "research"
    HELPER = "helper"


class TaskSpec(BaseModel):
    task_id: OrchestrationText
    task_type: TaskType
    instruction: OrchestrationText


class OrchestrationPlan(BaseModel):
    goal: OrchestrationText
    tasks: list[TaskSpec] = Field(min_length=1, max_length=5)
    notes: str = ""


class TaskStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class TaskResult(BaseModel):
    task_id: OrchestrationText
    task_type: TaskType
    status: TaskStatus
    result: OrchestrationText


class ReviewAction(StrEnum):
    CONTINUE = "continue"
    REPLACE_REMAINING = "replace_remaining"
    FINISH = "finish"


class PlanReviewDecision(BaseModel):
    action: ReviewAction
    reason: OrchestrationText
    replacement_tasks: list[TaskSpec] = Field(default_factory=list, max_length=5)
```

`MessageResponse.route` 改用 `TaskType`。删除不再使用的 `IntentDecision`；旧 `RouteTarget` 的引用在后续任务统一迁移。

- [ ] **Step 4: 扩展 RootState**

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
    review_decision: NotRequired[PlanReviewDecision]
    executed_task_count: NotRequired[int]
    route: NotRequired[TaskType]
    response: NotRequired[str]
    candidate_itinerary: NotRequired[str | None]
    current_itinerary: NotRequired[str | None]
```

- [ ] **Step 5: 运行契约测试**

Run: `\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\models\test_orchestration.py tests\models\test_contracts.py`

Expected: PASS。

---

### Task 2: Orchestrator 计划、复核与最终回复节点

**Files:**
- Create: `src/tourism_agent/graph/nodes/orchestrator.py`
- Create: `tests/graph/test_orchestrator_nodes.py`

**Interfaces:**
- Consumes: `RootState`、`OrchestrationPlan`、`PlanReviewDecision`、`conversation_to_messages()`、`BaseChatModel`。
- Produces: `create_plan_node(model)`、`create_review_node(model)`、`create_finalize_node(model)`。

- [ ] **Step 1: 写失败测试，验证计划节点严格区分历史与当前消息**

```python
RECENT_CONVERSATION = [
    ConversationMessage(
        id=1,
        role=ConversationRole.USER,
        content="我刚才在比较沙面和二沙岛",
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
]


class StructuredOrchestratorFakeModel:
    def __init__(self) -> None:
        self.plan_messages: list[BaseMessage] = []

    def with_structured_output(self, schema: type[BaseModel]) -> RunnableLambda:
        def respond(messages: list[BaseMessage]) -> BaseModel:
            if schema is OrchestrationPlan:
                self.plan_messages = messages
                return schema(
                    goal="研究候选并加入行程",
                    tasks=[
                        TaskSpec(
                            task_id="task_1",
                            task_type="research",
                            instruction="研究刚才选中的候选",
                        ),
                        TaskSpec(
                            task_id="task_2",
                            task_type="planning",
                            instruction="将合适候选加入行程",
                        ),
                    ],
                )
            return PlanReviewDecision(
                action="replace_remaining",
                reason="Explore 已经给出明确候选",
                replacement_tasks=[
                    TaskSpec(
                        task_id="task_3",
                        task_type="research",
                        instruction="深入调查沙面",
                    )
                ],
            )

        return RunnableLambda(respond)

    def with_config(self, **_kwargs: object) -> RunnableLambda:
        return RunnableLambda(
            lambda _messages: AIMessage(
                content="已筛选并调研沙面，等待你确认行程调整。"
            )
        )


def test_plan_node_builds_labeled_context_and_initializes_execution_state() -> None:
    model = StructuredOrchestratorFakeModel()
    node = create_plan_node(model)

    result = asyncio.run(
        node(
            {
                "user_input": "就按刚才的地点深入研究后加入行程",
                "routing_context": RECENT_CONVERSATION,
            }
        )
    )

    assert all(
        str(message.content).startswith("【历史消息】")
        for message in model.plan_messages[1:-1]
    )
    assert str(model.plan_messages[-1].content).startswith("【当前消息】")
    assert result["orchestration_goal"] == "研究候选并加入行程"
    assert len(result["pending_tasks"]) == 2
    assert result["task_results"] == []
    assert result["executed_task_count"] == 0
```

- [ ] **Step 2: 写失败测试，验证复核节点和最终回复节点的独立 Prompt**

```python
def test_review_node_receives_results_and_returns_structured_decision() -> None:
    model = StructuredOrchestratorFakeModel()
    state = {
        "user_input": "寻找合适地点后加入行程",
        "orchestration_goal": "寻找合适地点后加入行程",
        "pending_tasks": [],
        "task_results": [
            TaskResult(
                task_id="task_1",
                task_type="explore",
                status="success",
                result="沙面最适合进一步调研。",
            )
        ],
    }
    result = asyncio.run(create_review_node(model)(state))
    assert result["review_decision"].action is ReviewAction.REPLACE_REMAINING
    assert result["review_decision"].replacement_tasks[0].task_type is TaskType.RESEARCH


def test_finalize_node_does_not_repeat_full_itinerary() -> None:
    model = StructuredOrchestratorFakeModel()
    state = {
        "user_input": "寻找合适地点后加入行程",
        "orchestration_goal": "寻找合适地点后加入行程",
        "executed_task_count": 2,
        "task_results": [
            TaskResult(
                task_id="task_2",
                task_type="planning",
                status="success",
                result="已生成沙面候选行程，完整行程由后端单独返回。",
            )
        ],
    }
    result = asyncio.run(create_finalize_node(model)(state))
    assert result == {"response": "已筛选并调研沙面，等待你确认行程调整。"}
    assert "第一天：" not in result["response"]
```

- [ ] **Step 3: 运行节点测试并确认失败**

Run: `\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\graph\test_orchestrator_nodes.py`

Expected: FAIL，错误包含 `ModuleNotFoundError: tourism_agent.graph.nodes.orchestrator`。

- [ ] **Step 4: 实现三个节点及其中文 Prompt**

```python
def create_plan_node(model: BaseChatModel) -> OrchestratorNode:
    planner = model.with_structured_output(OrchestrationPlan).with_config(
        tags=["orchestrator", "planner"]
    )

    async def create_plan(state: RootState) -> dict[str, object]:
        history = conversation_to_messages(
            state.get("routing_context", []),
            label="【历史消息】",
        )
        plan = await planner.ainvoke(
            [
                SystemMessage(content=ORCHESTRATOR_PLAN_PROMPT),
                *history,
                HumanMessage(content=f"【当前消息】\n{state['user_input']}"),
            ]
        )
        return {
            "orchestration_goal": plan.goal,
            "pending_tasks": plan.tasks,
            "task_results": [],
            "executed_task_count": 0,
        }

    return create_plan
```

`create_review_node` 将原始目标、已完成 TaskResult 和剩余 Task 格式化为 JSON 后交给
`PlanReviewDecision` 结构化模型。`create_finalize_node` 只接收原始目标和 TaskResult，Prompt 明确
禁止输出完整 CurrentItinerary，并要求包含最终选择、关键理由和是否修改行程。

- [ ] **Step 5: 运行节点测试**

Run: `\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\graph\test_orchestrator_nodes.py`

Expected: PASS。

---

### Task 3: 根图顺序 Task 调度与子图结果适配

**Files:**
- Modify: `src/tourism_agent/graph/root.py`
- Create: `tests/graph/test_root_orchestrator.py`
- Modify: `tests/graph/test_root_routing_context.py`

**Interfaces:**
- Consumes: Task 1 的 Orchestrator 模型、Task 2 的节点工厂、四个现有子图的 `assistant_message` 输出。
- Produces: `build_task_message(state)`、顺序调度循环、统一 `latest_task_result` 和兼容的 `route`。

- [ ] **Step 1: 写失败测试，验证一次请求顺序进入 Explore、Research、Planning**

```python
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
            return PlanReviewDecision(action="continue", reason="继续执行原计划")

        return RunnableLambda(review)

    def with_config(self, **_kwargs: object) -> RunnableLambda:
        return RunnableLambda(
            lambda _messages: AIMessage(content="已完成本轮复合旅行请求。")
        )


class RecordingSubgraph:
    def __init__(self, name: str, calls: list[str], inputs: dict[str, str]) -> None:
        self.name = name
        self.calls = calls
        self.inputs = inputs

    async def ainvoke(self, payload: dict[str, object], **_kwargs: object) -> dict:
        self.calls.append(self.name)
        self.inputs[self.name] = str(payload["messages"][0].content)
        return {
            "assistant_message": f"{self.name} result：沙面",
            "candidate_itinerary": None,
            "current_itinerary": None,
        }


def build_test_root(
    monkeypatch: pytest.MonkeyPatch,
    tasks: list[TaskType],
    reviews: list[PlanReviewDecision] | None = None,
) -> tuple[CompiledStateGraph, list[str], dict[str, str]]:
    calls: list[str] = []
    inputs: dict[str, str] = {}
    module = import_module("tourism_agent.graph.root")
    for name in ("planning", "explore", "research", "helper"):
        monkeypatch.setattr(
            module,
            f"build_{name}_graph",
            lambda *_args, _name=name, **_kwargs: RecordingSubgraph(
                _name, calls, inputs
            ),
        )
    graph = module.build_root_graph(
        ScriptedOrchestratorModel(tasks, reviews),
        RoutingContextRepository(),
    )
    return graph, calls, inputs


def test_root_executes_multi_task_plan_in_order(monkeypatch) -> None:
    graph, calls, _inputs = build_test_root(
        monkeypatch,
        tasks=[TaskType.EXPLORE, TaskType.RESEARCH, TaskType.PLANNING]
    )

    result = asyncio.run(graph.ainvoke(ROOT_INPUT, ROOT_CONFIG))

    assert calls == ["explore", "research", "planning"]
    assert [item.task_type for item in result["task_results"]] == [
        TaskType.EXPLORE,
        TaskType.RESEARCH,
        TaskType.PLANNING,
    ]
    assert result["route"] is TaskType.PLANNING
```

- [ ] **Step 2: 写失败测试，验证内部 Task 输入具有明确标签**

```python
def test_subgraph_receives_original_goal_current_task_and_latest_result(
    monkeypatch,
) -> None:
    graph, _calls, inputs = build_test_root(
        monkeypatch,
        tasks=[TaskType.EXPLORE, TaskType.RESEARCH],
    )
    asyncio.run(graph.ainvoke(ROOT_INPUT, ROOT_CONFIG))
    research_input = inputs["research"]

    assert "【原始用户目标】" in research_input
    assert "【当前子任务】" in research_input
    assert "【前序任务有效结果】" in research_input
    assert "沙面" in research_input
```

- [ ] **Step 3: 运行根图测试并确认旧图只能执行一个路由**

Run: `\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\graph\test_root_orchestrator.py`

Expected: FAIL，错误显示根图仍依赖 `IntentDecision` 或不存在 `task_results`。

- [ ] **Step 4: 用 Orchestrator 循环替换单次意图路由**

核心节点行为：

```python
def prepare_next_task(state: RootState) -> dict[str, object]:
    pending = state.get("pending_tasks", [])
    if not pending or state.get("executed_task_count", 0) >= MAX_TASKS_PER_TURN:
        return {"current_task": None}
    return {
        "current_task": pending[0],
        "pending_tasks": pending[1:],
        # 子图可能在返回前 interrupt，因此进入子图前就记录当前 Task 类型。
        "route": pending[0].task_type,
    }


def select_task(state: RootState) -> TaskType | Literal["finalize"]:
    task = state.get("current_task")
    return task.task_type if task is not None else "finalize"
```

四个现有 `run_*` 包装节点继续负责 RootState 与子图 State 的显式映射，但传入的 HumanMessage
改为：

```python
def build_task_message(state: RootState) -> str:
    task = state["current_task"]
    latest = state.get("task_results", [])[-1:]
    previous = latest[0].result if latest else "无"
    return (
        f"【原始用户目标】\n{state['user_input']}\n\n"
        f"【当前子任务】\n{task.instruction}\n\n"
        f"【前序任务有效结果】\n{previous}"
    )
```

子图正常结束后返回：

```python
return {
    "latest_task_result": TaskResult(
        task_id=task.task_id,
        task_type=task.task_type,
        status=TaskStatus.SUCCESS,
        result=result["assistant_message"],
    ),
    "candidate_itinerary": result.get("candidate_itinerary"),
    "current_itinerary": result.get("current_itinerary"),
}
```

- [ ] **Step 5: 实现确定性结果记录节点**

```python
def record_task_result(state: RootState) -> dict[str, object]:
    latest = state["latest_task_result"]
    return {
        "task_results": [*state.get("task_results", []), latest],
        "latest_task_result": None,
        "current_task": None,
        "executed_task_count": state.get("executed_task_count", 0) + 1,
    }
```

- [ ] **Step 6: 连接根图边**

```text
START → load_orchestrator_context → create_plan → prepare_next_task
prepare_next_task → planning / explore / research / helper / finalize
四个子图 → record_task_result → review_plan
```

根图继续使用现有 Checkpointer 注入方式，`thread_id` 仍只通过运行配置传递。

- [ ] **Step 7: 运行根图顺序执行测试**

Run: `\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\graph\test_root_orchestrator.py tests\graph\test_root_routing_context.py`

Expected: PASS。

---

### Task 4: 计划复核、提前结束、替换任务与执行上限

**Files:**
- Modify: `src/tourism_agent/graph/root.py`
- Modify: `tests/graph/test_root_orchestrator.py`

**Interfaces:**
- Consumes: `RootState.review_decision`、`ReviewAction`、`MAX_TASKS_PER_TURN = 5`。
- Produces: `apply_review_decision(state)`、`select_after_review(state)`。

- [ ] **Step 1: 写失败测试，验证无候选时提前结束**

```python
def test_review_finish_skips_research_and_planning(monkeypatch) -> None:
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
```

- [ ] **Step 2: 写失败测试，验证只替换尚未执行的任务**

```python
def test_review_replaces_only_remaining_tasks(monkeypatch) -> None:
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
```

- [ ] **Step 3: 写失败测试，验证最多完成五个 Task**

```python
def test_root_finalizes_after_five_completed_tasks(monkeypatch) -> None:
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
```

- [ ] **Step 4: 运行测试并确认复核循环尚未连接**

Run: `\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\graph\test_root_orchestrator.py -k "finish or replaces or five"`

Expected: FAIL，表现为继续执行旧任务、未替换任务或超过上限。

- [ ] **Step 5: 实现复核决策应用和确定性出口**

```python
def apply_review_decision(state: RootState) -> dict[str, object]:
    decision = state["review_decision"]
    if decision.action is ReviewAction.REPLACE_REMAINING:
        return {"pending_tasks": decision.replacement_tasks}
    if decision.action is ReviewAction.FINISH:
        return {"pending_tasks": []}
    return {}


def select_after_review(state: RootState) -> Literal["prepare_next_task", "finalize"]:
    if state["review_decision"].action is ReviewAction.FINISH:
        return "finalize"
    if state.get("executed_task_count", 0) >= MAX_TASKS_PER_TURN:
        return "finalize"
    if not state.get("pending_tasks"):
        return "finalize"
    return "prepare_next_task"
```

连接：

```text
record_task_result → review_plan → apply_review_decision
apply_review_decision → prepare_next_task / finalize
```

- [ ] **Step 6: 运行复核测试和完整根图测试**

Run: `\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\graph\test_root_orchestrator.py`

Expected: PASS。

---

### Task 5: interrupt/resume、API 契约与 RAG 切块边界回归

**Files:**
- Modify: `tests/graph/test_root_orchestrator.py`
- Modify: `tests/graph/test_planning_graph.py`
- Modify: `tests/api/test_messages.py`
- Modify: `src/tourism_agent/api.py`

**Interfaces:**
- Consumes: 现有 `Command(resume=...)`、`get_user_visible_message()`、`ConversationChunkService.submit()`、根图 `route` 兼容字段。
- Produces: 多 Task 下不重复执行已完成 Task 的恢复行为，以及仍然只提交外部 Exchange 的 API 行为。

- [ ] **Step 1: 更新根图 Fake Model，使其按 Schema 返回 Orchestrator 计划和复核决策**

```python
def with_structured_output(self, schema: type[Any]) -> RunnableLambda:
    if schema is OrchestrationPlan:
        return RunnableLambda(
            lambda _messages: schema(
                goal="完成当前测试请求",
                tasks=[
                    TaskSpec(
                        task_id="task_1",
                        task_type="planning",
                        instruction="完成当前旅行规划请求",
                    )
                ],
            )
        )
    if schema is PlanReviewDecision:
        return RunnableLambda(
            lambda _messages: schema(action="finish", reason="任务已经完成")
        )
    return self.research_structured_output(schema)
```

只更新通过 `build_root_graph()` 运行的测试 Fake；直接测试子图的 Fake 不增加 Orchestrator 行为。

- [ ] **Step 2: 写恢复测试，验证已完成 Explore 不会因 Planning interrupt/resume 重跑**

```python
class InterruptingPlanningSubgraph:
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


def test_resume_continues_current_task_without_replaying_completed_tasks(
    monkeypatch,
) -> None:
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
                PlanReviewDecision(action="continue", reason="继续修改行程"),
                PlanReviewDecision(action="finish", reason="目标已经完成"),
            ],
        ),
        RoutingContextRepository(),
    )

    first = asyncio.run(graph.ainvoke(ROOT_INPUT, ROOT_CONFIG))
    assert first["__interrupt__"][0].value["kind"] == "candidate_confirmation"
    assert calls.count("explore") == 1

    completed = asyncio.run(graph.ainvoke(Command(resume="是"), ROOT_CONFIG))
    assert calls.count("explore") == 1
    assert completed["executed_task_count"] == 2
```

- [ ] **Step 3: 写 API/RAG 边界测试**

```python
def test_multi_task_request_persists_only_external_exchange() -> None:
    payload = message_payload("寻找广州塔附近适合闲逛的地点，调研后加入行程")
    response = client.post("/messages", json=payload)

    assert response.status_code == 200
    assert [message.role for message in repository.messages] == [
        ConversationRole.USER,
        ConversationRole.ASSISTANT,
    ]
    assert len(chunk_service.submissions) == 1
    assert "【当前子任务】" not in repository.messages[1].content
    assert chunk_service.submissions[0]["user_message"].content == payload["message"]
```

- [ ] **Step 4: 运行回归测试并确认失败位置**

Run: `\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\graph\test_planning_graph.py tests\api\test_messages.py`

Expected: 首次运行至少因旧 `IntentDecision` Fake 或根图仍是单路由结构失败。

- [ ] **Step 5: 保持 API 持久化边界，只调整 route 日志语义**

`api.py` 继续只持久化 `request.message` 和 `get_user_visible_message(result)`。不向 API 传递
TaskSpec、TaskResult 或内部子图 HumanMessage。日志文字从“路由”调整为“最近 Task”，
`MessageResponse.route` 继续读取 `result["route"]`。

- [ ] **Step 6: 运行 Planning 和 API 测试**

Run: `\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\graph\test_planning_graph.py tests\api\test_messages.py`

Expected: PASS。

---

### Task 6: 删除旧 Intent 节点并完成整体回归

**Files:**
- Delete: `src/tourism_agent/graph/nodes/intent.py`
- Delete: `tests/graph/test_intent_node.py`
- Modify: `tests/integration/test_real_model.py`
- Modify: `docs/architecture.md`
- Modify: `docs/root-graph-guide.md`
- Modify: `docs/travel_agent_orchestrator_task_design.md`

**Interfaces:**
- Consumes: 完成后的 `create_plan_node()` 和根图 Orchestrator。
- Produces: 不再包含旧 `IntentDecision`、`RouteTarget` 或单次路由文案的代码与测试。

- [ ] **Step 1: 更新真实模型 Smoke Test**

```python
def test_real_model_returns_structured_orchestration_plan() -> None:
    node = create_plan_node(create_chat_model(settings))

    result = asyncio.run(node({"user_input": "帮我规划北京三日游"}))

    assert result["pending_tasks"]
    assert result["pending_tasks"][0].task_type is TaskType.PLANNING
```

- [ ] **Step 2: 删除旧节点和旧测试，扫描残余引用**

Run: `rg -n "IntentDecision|RouteTarget|create_intent_node|understand_intent|select_route" src tests`

Expected: 没有旧根图实现引用。

- [ ] **Step 3: 运行 Orchestrator 相关测试**

Run: `\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\models\test_orchestration.py tests\graph\test_orchestrator_nodes.py tests\graph\test_root_orchestrator.py tests\graph\test_root_routing_context.py tests\graph\test_planning_graph.py tests\api\test_messages.py`

Expected: PASS。

- [ ] **Step 4: 运行完整测试**

Run: `$env:RUN_LLM_INTEGRATION='false'; \.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider`

Expected: 所有默认测试通过，只有需要 PostgreSQL、真实模型或真实外部服务的测试按既有开关跳过。

- [ ] **Step 5: 运行静态检查和差异检查**

Run: `\.venv\Scripts\python.exe -m ruff check src tests`

Expected: `All checks passed!`

Run: `git diff --check`

Expected: 退出码为 0；Windows 下允许显示 LF/CRLF 转换警告，但不能存在空白错误。
