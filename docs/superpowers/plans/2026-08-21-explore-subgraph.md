# Explore 子图实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现可由根图路由、只读探索旅行信息并支持主动提问的 Explore ReAct 子图。

**Architecture:** Explore 使用独立的最小 State 和上下文加载器；模型只绑定公共只读查询 Tools 与 Explore 私有 `ask_user`。根图负责显式输入输出映射，并把旧 `inspiration` 路由替换为 `explore`。

**Tech Stack:** Python 3.12、LangGraph、LangChain、Pydantic、pytest。

**Spec:** `docs/explore-subgraph-design.md`

## Global Constraints

- Explore 不得写入 TripContext、CurrentItinerary 或其他业务数据。
- `ask_user` 必须独占一轮 Tool 调用，公共查询 Tools 可以并发。
- 子图不配置独立 Checkpointer，interrupt 继承根图的内存 Checkpointer。
- 新增模块说明、Docstring、注释和异常信息使用中文。

---

### Task 1: Explore 状态与上下文快照

**Files:**
- Create: `src/tourism_agent/graph/subgraphs/explore/state.py`
- Create: `src/tourism_agent/services/explore_context.py`
- Test: `tests/graph/test_explore_graph.py`

**Interfaces:**
- Consumes: `PlanningRepository.get_recent_conversation/get_trip_context/get_current_itinerary`
- Produces: `ExploreState`、`ExploreContextBuilder.build(trip_id, before_message_id)`

- [x] 写出验证最近 8 条 Conversation、完整 TripContext 和 CurrentItinerary 被加载的失败测试。
- [x] 运行测试，确认因 Explore 模块尚不存在而失败。
- [x] 实现最小 State 与上下文快照构建器。
- [x] 运行聚焦测试并确认通过。

### Task 2: Explore ReAct、只读 Tools 与 interrupt

**Files:**
- Create: `src/tourism_agent/graph/subgraphs/explore/tools.py`
- Create: `src/tourism_agent/graph/subgraphs/explore/graph.py`
- Test: `tests/graph/test_explore_graph.py`

**Interfaces:**
- Consumes: 六个公共只读查询 `BaseTool`
- Produces: `create_explore_tools(query_tools)`、`build_explore_graph(model, repository, query_tools)`

- [x] 写出查询 Tool 循环、完整最终回答、`ask_user` interrupt/resume 和混合调用整批拒绝的失败测试。
- [x] 运行测试，确认分别因缺少图、Tool 或路由行为而失败。
- [x] 实现 Prompt、Agent 节点、ToolNode、独占调用校验和 finalize 节点。
- [x] 运行 Explore 测试并确认通过。

### Task 3: 根路由与应用组装

**Files:**
- Modify: `src/tourism_agent/models/contracts.py`
- Modify: `src/tourism_agent/graph/nodes/intent.py`
- Modify: `src/tourism_agent/graph/root.py`
- Modify: `src/tourism_agent/api.py`
- Modify: `tests/graph/test_intent_node.py`
- Modify: `tests/graph/test_root_routing_context.py`
- Modify: `tests/api/test_messages.py`
- Modify: `tests/models/test_contracts.py`

**Interfaces:**
- Consumes: `build_explore_graph(...)`
- Produces: API 公共路由值 `explore` 与 `RootState.response`

- [x] 先把既有测试期望改为 `explore`，并新增根图实际进入 Explore 的失败测试。
- [x] 运行聚焦测试，确认旧 `inspiration` 路由造成失败。
- [x] 用 `RouteTarget.EXPLORE` 替换旧路由并接入真实 Explore 子图；应用向根图传入完整公共查询 Tool 集合。
- [x] 运行根图、API 和契约聚焦测试并确认通过。
- [x] 运行全部非外部集成测试、Ruff 与 `git diff --check`。
