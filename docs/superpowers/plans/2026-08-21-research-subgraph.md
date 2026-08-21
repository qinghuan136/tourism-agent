# Research 子图实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地从根图路由、结构化研究规划、ReAct 调查到独立报告综合的 Research 最小闭环。

**Architecture:** Research 子图进入后自行加载只读快照，并从同一个基础模型派生 Planner、Researcher 与 Synthesis 三个 Runnable。只有 Researcher 绑定公共只读 Tools、`ask_user` 和 `revise_research_plan`；子图默认继承根图 Checkpointer，以支持 interrupt/resume。

**Tech Stack:** Python 3.12、LangGraph 1.x、LangChain 1.x、Pydantic 2.x、pytest、ruff

**Spec:** `docs/research-subgraph-design.md`

## Global Constraints

- Research 不写 TripContext、CurrentItinerary 或报告数据库。
- ResearchPlan 只包含 `goal`、`questions`、`source_strategy`、`success_criteria`。
- `ask_user` 和 `revise_research_plan` 必须独占一轮 Tool 调用。
- 成功重规划最多两次；达到上限后把原因作为 ToolMessage 交还 Agent。
- 新增及修改的注释、Docstring 和异常信息使用中文。
- 保留当前工作区已有修改，不执行提交、重置或无关重构。

---

### Task 1: State 与只读上下文

**Files:**
- Create: `src/tourism_agent/graph/subgraphs/research/state.py`
- Create: `src/tourism_agent/services/research_context.py`
- Create: `tests/graph/test_research_graph.py`

**Interfaces:**
- Produces: `ResearchPlan`、`ResearchState`、`ResearchContextBuilder.build(trip_id, before_message_id)`。

- [ ] 写失败测试，验证最近 8 条 Conversation、完整 TripContext 和 CurrentItinerary 被加载。
- [ ] 运行 `pytest tests/graph/test_research_graph.py -q`，确认因 Research 模块不存在而失败。
- [ ] 实现最小 State 与 Context Builder。
- [ ] 重跑测试并确认通过。

### Task 2: 私有 Tools 与 Research 图

**Files:**
- Create: `src/tourism_agent/graph/subgraphs/research/tools.py`
- Create: `src/tourism_agent/graph/subgraphs/research/graph.py`
- Modify: `tests/graph/test_research_graph.py`

**Interfaces:**
- Consumes: `ResearchPlan`、`ResearchState`、公共只读 `BaseTool` 列表。
- Produces: `create_research_tools(query_tools)`、`build_research_graph(model, repository, query_tools)`。

- [ ] 写失败测试，验证三个模型阶段、Research Tool 白名单、ReAct 查询与最终综合。
- [ ] 写失败测试，验证独占调用拒绝、`ask_user` 恢复和重规划上限。
- [ ] 逐组运行测试，确认分别因缺少实现而失败。
- [ ] 实现 Prompt 投影、结构化规划节点、Researcher、ToolNode、重规划路由和 Synthesis 节点。
- [ ] 重跑 `tests/graph/test_research_graph.py` 并确认通过。

### Task 3: 根图确定性接入

**Files:**
- Modify: `src/tourism_agent/models/contracts.py`
- Modify: `src/tourism_agent/graph/nodes/intent.py`
- Modify: `src/tourism_agent/graph/root.py`
- Modify: `tests/graph/test_intent_node.py`
- Modify: `tests/graph/test_root_routing_context.py`

**Interfaces:**
- Produces: `RouteTarget.RESEARCH`，根图 `research` 节点以及 `ResearchState.assistant_message -> RootState.response` 映射。

- [ ] 写失败测试，验证深度调查语义进入 Research，且历史与当前消息边界不变。
- [ ] 运行目标测试，确认因路由未注册而失败。
- [ ] 增加结构化路由枚举、Prompt 规则和根图子图映射。
- [ ] 重跑目标测试并确认通过。

### Task 4: 回归验证

**Files:**
- Modify only if a verified regression requires a scoped fix.

- [ ] 运行 `pytest tests/graph/test_research_graph.py tests/graph/test_intent_node.py tests/graph/test_root_routing_context.py -q -p no:cacheprovider`。
- [ ] 运行 `pytest -q -p no:cacheprovider --ignore=tests/integration`。
- [ ] 运行 `ruff check src tests`。
- [ ] 检查 `git diff --check` 和实际变更范围，不提交 Git commit。
