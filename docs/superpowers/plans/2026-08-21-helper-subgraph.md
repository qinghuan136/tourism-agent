# Helper 子图实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现可由根图路由、支持直接聊天和公共只读查询、并能主动提问的 Helper ReAct 子图。

**Architecture:** Helper 使用与现有业务子图一致的最小 State，自行加载近期 Conversation、完整 TripContext 和 CurrentItinerary。一个 Helper Agent 根据需要直接回答或进入只读 Tool ReAct 循环；根图负责确定性路由和显式输入输出映射。

**Tech Stack:** Python 3.12、LangGraph、LangChain、Pydantic、pytest。

**Spec:** `docs/helper-subgraph-design.md`

## Global Constraints

- Helper 只能读取业务上下文，不得修改 TripContext、CurrentItinerary 或其他长期数据。
- 第一版只复用六个公共查询 Tools，并增加 Helper 私有 `ask_user`。
- `ask_user` 必须独占一轮 Tool 调用，混合调用整批拒绝。
- 不实现通用网页自动操作、订单、支付、路线 Tool、Orchestrator 协议或新数据库表。
- 子图不配置独立 Checkpointer，interrupt 继承根图的内存 Checkpointer。
- 新增模块说明、Docstring、注释和异常信息使用中文。
- 当前工作树包含本功能依赖的未提交修改；本计划不执行自动 Git commit。

---

### Task 1: Helper 状态与上下文快照

**Files:**
- Create: `src/tourism_agent/graph/subgraphs/helper/state.py`
- Create: `src/tourism_agent/services/helper_context.py`
- Create: `tests/graph/test_helper_graph.py`

**Interfaces:**
- Consumes: `PlanningRepository.get_recent_conversation/get_trip_context/get_current_itinerary`
- Produces: `HelperState`、`HelperContextBuilder.build(trip_id, before_message_id)`

- [x] 写出验证最近 8 条 Conversation、完整 TripContext 和 CurrentItinerary 被加载的失败测试。
- [x] 运行聚焦测试，确认因 Helper 模块尚不存在而失败。
- [x] 实现只包含业务作用域、消息 reducer、只读快照和 `assistant_message` 的 HelperState。
- [x] 实现 HelperContextSnapshot 与 HelperContextBuilder，不增加写入方法。
- [x] 运行上下文测试并确认通过。

### Task 2: Helper ReAct、只读 Tools 与主动提问

**Files:**
- Create: `src/tourism_agent/graph/subgraphs/helper/tools.py`
- Create: `src/tourism_agent/graph/subgraphs/helper/graph.py`
- Modify: `tests/graph/test_helper_graph.py`

**Interfaces:**
- Consumes: 六个公共只读查询 `BaseTool`
- Produces: `create_helper_tools(query_tools)`、`build_helper_graph(model, repository, query_tools)`

- [x] 写出直接聊天、上下文分区、只读 Tool 白名单和 Tool Observation 回流的失败测试。
- [x] 写出 `ask_user` interrupt/resume 与混合调用整批拒绝的失败测试。
- [x] 运行 Helper 聚焦测试并确认因图和 Tools 尚不存在而失败。
- [x] 实现 Helper Prompt、模型消息构造、Agent 节点、ToolNode、独占调用检查和 finalize 节点。
- [x] 保证 Tool 参数 `ValueError` 作为 ToolMessage 返回，其他外部异常继续抛出。
- [x] 运行 Helper 测试并确认通过。

### Task 3: 根路由与公共契约接入

**Files:**
- Modify: `src/tourism_agent/models/contracts.py`
- Modify: `src/tourism_agent/graph/nodes/intent.py`
- Modify: `src/tourism_agent/graph/root.py`
- Modify: `tests/models/test_contracts.py`
- Modify: `tests/graph/test_intent_node.py`
- Modify: `tests/graph/test_root_routing_context.py`

**Interfaces:**
- Consumes: `build_helper_graph(model, repository, query_tools)`
- Produces: `RouteTarget.HELPER`、`RootState.response` 和 API 公共路由值 `helper`

- [x] 先把契约测试加入 `helper`，并新增问候、局部查询和 Helper 兜底边界的路由测试。
- [x] 新增根图实际进入 Helper 并映射 `assistant_message` 的失败测试。
- [x] 运行聚焦测试，确认缺少 `RouteTarget.HELPER` 和根图节点导致失败。
- [x] 扩展 Intent Prompt，明确 Helper 与其他三个业务模块的边界。
- [x] 构建 Helper 子图并在根图增加 `run_helper` 的显式输入输出映射和确定性边。
- [x] 运行契约、理解节点、根图和 Helper 聚焦测试并确认通过。

### Task 4: 完整回归与文档一致性

**Files:**
- Verify: `docs/helper-subgraph-design.md`
- Verify: all changed source and test files

- [x] 检查实现只绑定设计文档允许的六个公共查询 Tools 和 `ask_user`。
- [x] 运行 `pytest -q -p no:cacheprovider --ignore=tests/integration`。
- [x] 运行 `ruff check src tests`。
- [x] 运行 `git diff --check`。
- [x] 运行包含真实模型集成测试的完整 pytest，并如实记录外部服务结果。
