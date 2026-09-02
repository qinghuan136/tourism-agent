# Orchestrator Handoff Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让现有 Orchestrator Reviewer 整理面向下一 Task 的 `handoff_context`，并以该上下文构造子图执行消息和自动 RAG 查询，同时完整保留原始 `TaskResult`。

**Architecture:** Reviewer 在既有结构化复核调用中额外生成自由文本 handoff；根图确定性保存并传给下一 Task。根图构造独立 `retrieval_query`，四个子图仍自行执行数据库召回，不新增 Agent、LLM 调用或持久化表。

**Tech Stack:** Python、Pydantic v2、LangGraph、LangChain Messages、pytest

**Spec:** `docs/superpowers/specs/2026-09-01-orchestrator-handoff-context-design.md`

## Global Constraints

- 完整 `task_results` 始终保留为本轮事实来源，不覆盖、不写入 handoff。
- 不新增 Formatter Agent、LLM 调用、数据库表或结构化子图输出。
- `handoff_context` 和 `retrieval_query` 仅存在于 Graph State，不写 Conversation/RAG Chunk。
- 四个子图继续自行加载业务 Context 和执行自动历史召回。
- 不改变 interrupt/resume、idempotency、thread 锁和 CurrentItinerary 写入边界。
- 当前工作区包含用户已有未提交修改，本计划不自动创建 Git commit。

---

### Task 1: Reviewer 生成并传递 Handoff Context

**Files:**
- Modify: `src/tourism_agent/models/orchestration.py`
- Modify: `src/tourism_agent/graph/state.py`
- Modify: `src/tourism_agent/graph/nodes/orchestrator.py`
- Modify: `src/tourism_agent/graph/root.py`
- Modify: `tests/models/test_orchestration.py`
- Modify: `tests/graph/test_orchestrator_nodes.py`
- Modify: `tests/graph/test_root_orchestrator.py`

**Interfaces:**
- Produces: `PlanReviewDecision.handoff_context: str`、`RootState.handoff_context`、更新后的 `build_task_message(state)`。
- Preserves: `RootState.task_results` 中全部原始 `TaskResult`。

- [ ] **Step 1: 写模型失败测试**

新增测试覆盖：

```python
def test_continue_requires_handoff_context() -> None:
    with pytest.raises(ValueError, match="交接上下文"):
        PlanReviewDecision(action="continue", reason="继续")


def test_finish_rejects_handoff_context() -> None:
    with pytest.raises(ValueError, match="结束"):
        PlanReviewDecision(
            action="finish",
            reason="完成",
            handoff_context="不应存在",
        )
```

并补充合法 `continue`、合法 `replace_remaining` 和合法 `finish` 用例。

- [ ] **Step 2: 运行模型测试确认 RED**

Run: `.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\models\test_orchestration.py`

Expected: FAIL，当前模型不存在 `handoff_context` 或未执行跨字段校验。

- [ ] **Step 3: 实现模型和 RootState 字段**

在 `PlanReviewDecision` 增加 `handoff_context: str = ""`，扩展现有 `model_validator`：

```python
if self.action is ReviewAction.FINISH and self.handoff_context.strip():
    raise ValueError("结束任务计划时不能提供交接上下文")
if self.action is not ReviewAction.FINISH and not self.handoff_context.strip():
    raise ValueError("继续执行任务时必须提供交接上下文")
```

在 `RootState` 增加 `handoff_context: NotRequired[str]`。

- [ ] **Step 4: 写 Reviewer 和根图失败测试**

扩展测试 Fake，使 Reviewer 返回不同的 handoff。断言：

```python
assert result["review_decision"].handoff_context == "沙面需要继续核实开放时间"
```

根图两 Task 测试断言第二个子图输入包含：

```text
【Orchestrator整理的现有结果】
沙面需要继续核实开放时间
```

并断言它不包含第一个 `TaskResult.result` 中专门设置的原文哨兵。

- [ ] **Step 5: 运行节点和根图测试确认 RED**

Run: `.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\graph\test_orchestrator_nodes.py tests\graph\test_root_orchestrator.py`

Expected: FAIL，当前 Reviewer 不返回 handoff，根图仍传最近 TaskResult 原文。

- [ ] **Step 6: 实现 Handoff 生成和传递**

- 更新 Reviewer Prompt，要求依据全部已完成结果为下一实际 Task 整理交接文本，不得生成新事实。
- `create_plan_node()` 初始化 `handoff_context=""`。
- `apply_review_decision()` 在 continue/replace 时覆盖为 Reviewer handoff，在 finish 时清空。
- `build_task_message()` 使用 `handoff_context`，不再读取最近 TaskResult。
- 保持 `record_task_result()` 的原始结果追加逻辑不变。

- [ ] **Step 7: 运行 Task 1 测试**

Run: `.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\models\test_orchestration.py tests\graph\test_orchestrator_nodes.py tests\graph\test_root_orchestrator.py`

Expected: PASS。

---

### Task 2: 独立 Retrieval Query 与四子图自动召回

**Files:**
- Modify: `src/tourism_agent/graph/root.py`
- Modify: `src/tourism_agent/graph/subgraphs/planning/state.py`
- Modify: `src/tourism_agent/graph/subgraphs/explore/state.py`
- Modify: `src/tourism_agent/graph/subgraphs/research/state.py`
- Modify: `src/tourism_agent/graph/subgraphs/helper/state.py`
- Modify: `src/tourism_agent/graph/subgraphs/planning/graph.py`
- Modify: `src/tourism_agent/graph/subgraphs/explore/graph.py`
- Modify: `src/tourism_agent/graph/subgraphs/research/graph.py`
- Modify: `src/tourism_agent/graph/subgraphs/helper/graph.py`
- Modify: `tests/graph/test_root_orchestrator.py`
- Modify: `tests/graph/test_conversation_history_context.py`
- Modify: `docs/architecture.md`
- Modify: `docs/travel_agent_orchestrator_task_design.md`

**Interfaces:**
- Produces: `build_retrieval_query(state: RootState) -> str` 和四个子图 State 的可选 `retrieval_query`。
- Consumes: Task 1 的 `RootState.handoff_context`。

- [ ] **Step 1: 写根图查询构造失败测试**

新增测试断言：

```python
query = build_retrieval_query(state)
assert query.startswith("【当前检索目标】")
assert state["current_task"].instruction in query
assert state["user_input"] in query
assert state["handoff_context"] in query
assert "原始TaskResult哨兵" not in query
```

- [ ] **Step 2: 写四子图自动召回失败测试**

在共享历史上下文测试中，为四个子图输入不同的：

```python
"messages": [HumanMessage(content="执行消息哨兵")],
"retrieval_query": "专用检索查询哨兵",
```

断言 Retrieval Service 实际收到 `专用检索查询哨兵`，而不是 `执行消息哨兵`。

- [ ] **Step 3: 运行测试确认 RED**

Run: `.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\graph\test_root_orchestrator.py tests\graph\test_conversation_history_context.py`

Expected: FAIL，当前 State 无 `retrieval_query`，子图仍使用 `current_message.text`。

- [ ] **Step 4: 实现查询构造和 State 映射**

在根图新增：

```python
def build_retrieval_query(state: RootState) -> str:
    task = state["current_task"]
    assert task is not None
    handoff = state.get("handoff_context", "").strip()
    parts = [
        f"【当前检索目标】\n{task.instruction}",
        f"【用户总体目标】\n{state['user_input']}",
    ]
    if handoff:
        parts.append(f"【Orchestrator整理的现有结果】\n{handoff}")
    return "\n\n".join(parts)
```

四个 `run_*` payload 增加 `retrieval_query=build_retrieval_query(state)`；四个 State 增加可选
`retrieval_query`，以兼容子图独立调用；四个 `load_context` 优先使用该字段，缺失时回退到当前
执行消息，再调用 `load_related_history()`。

- [ ] **Step 5: 运行聚焦测试**

Run: `.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\graph\test_root_orchestrator.py tests\graph\test_conversation_history_context.py tests\graph\test_planning_graph.py tests\graph\test_explore_graph.py tests\graph\test_research_graph.py tests\graph\test_helper_graph.py`

Expected: PASS。

- [ ] **Step 6: 更新架构文档**

记录 `task_results`、`handoff_context`、执行消息和 `retrieval_query` 的职责边界，并说明内部交接内容不写 Conversation/RAG Chunk。

- [ ] **Step 7: 运行 API 和完整回归**

Run: `.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\api\test_messages.py`

Expected: PASS，内部 handoff 不进入外部 Conversation/Chunk。

Run: `$env:RUN_LLM_INTEGRATION='false'; .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider`

Expected: 默认测试全部通过，真实模型和外部服务测试按既有开关跳过。

Run: `.\.venv\Scripts\python.exe -m ruff check src tests`

Expected: `All checks passed!`

Run: `git diff --check`

Expected: 退出码为 0；Windows 下允许 LF/CRLF 转换警告，不允许空白错误。
