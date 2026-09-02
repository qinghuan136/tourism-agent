# Conversation RAG Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在四个业务子图首次进入时自动召回相关历史，并允许 ReAct Agent 通过公共 Tool 按需继续召回。

**Architecture:** 原始近期 Conversation 与语义召回结果分别保存为 `conversation_context` 和 `retrieved_history`。自动召回由子图 `load_context` 执行，公共 Tool 在运行时从 State 取得可信用户与 Trip 作用域；根图只负责注入共享 Service 和 Tools，不加载业务记忆。

**Tech Stack:** Python、LangGraph、LangChain Tools、PostgreSQL、pgvector、pytest

**Spec:** `docs/conversation-rag-schema-design.md`

## Global Constraints

- 自动召回默认 Top 3；主动 Tool 搜索继续默认 Top 5。
- 自动召回和主动 Tool 搜索都在 SQL 的 Top K 截断前排除近期 Conversation 已有的 Exchange。
- `retrieved_history` 是无 reducer 的只读启动快照。
- resume 不重新进入 `load_context`，沿用 checkpoint 中已有召回结果。
- 当前工作区含同一 RAG 功能的未提交改动，本计划不创建 worktree、不自动提交。

---

### Task 1: 公共召回上下文

**Files:**
- Create: `src/tourism_agent/graph/history.py`
- Modify: `src/tourism_agent/services/conversation_retrieval.py`
- Test: `tests/graph/test_conversation_history_context.py`
- Test: `tests/services/test_conversation_retrieval.py`

**Interfaces:**
- Consumes: `ConversationRetrievalService.search(user_id, trip_id, query, limit, exclude_exchange_ids)`
- Produces: `load_related_history(...) -> list[ConversationChunkMatch]`、`format_related_history(...) -> str`

- [ ] 写失败测试，验证自动召回传入 limit=3、格式包含时间与 Exchange ID、已知服务错误降级为空列表。
- [ ] 运行测试并确认因缺少接口或 limit 参数失败。
- [ ] 实现可选 limit 和公共加载/格式化函数。
- [ ] 运行对应测试并确认通过。

### Task 2: Runtime 作用域历史 Tools

**Files:**
- Modify: `src/tourism_agent/graph/tools/conversation_history.py`
- Modify: `src/tourism_agent/graph/subgraphs/{planning,explore,research,helper}/tools.py`
- Test: `tests/graph/test_conversation_history_tools.py`

**Interfaces:**
- Consumes: `ToolRuntime.state["user_id"]`、`ToolRuntime.state["trip_id"]`
- Produces: `create_conversation_history_tools(service) -> list[BaseTool]`

- [ ] 写失败测试，用真实 `ToolNode` 验证模型参数不含作用域、执行时使用 State 作用域。
- [ ] 运行测试并确认旧闭包签名或缺少 Runtime 注入导致失败。
- [ ] 改造两个 Tool，并把名称加入四个子图只读白名单。
- [ ] 运行对应测试并确认通过。

### Task 3: 四子图自动召回与 Prompt 注入

**Files:**
- Modify: `src/tourism_agent/graph/subgraphs/{planning,explore,research,helper}/state.py`
- Modify: `src/tourism_agent/graph/subgraphs/{planning,explore,research,helper}/graph.py`
- Modify: `src/tourism_agent/graph/root.py`
- Test: `tests/graph/test_{planning,explore,research,helper}_graph.py`

**Interfaces:**
- Consumes: 可选 `ConversationRetrievalService` 与 State 初始 HumanMessage
- Produces: `retrieved_history: list[ConversationChunkMatch]` 和带精简相关历史分区的模型上下文

- [ ] 分别写失败测试，验证四个子图使用当前输入、可信作用域和 Top 3，并把结果放入 Prompt。
- [ ] 运行测试并确认当前构图签名或 State 更新缺失导致失败。
- [ ] 为四个 State 增加独立字段，在首次 `load_context` 加载并格式化相关历史；根图向四个子图转发 Service。
- [ ] 运行四个子图测试并确认通过，同时验证 interrupt/resume 测试保持通过。

### Task 4: 应用生命周期接线与整体验证

**Files:**
- Modify: `src/tourism_agent/api.py`
- Modify: `tests/api/test_lifecycle.py`
- Modify: `docs/architecture.md`

**Interfaces:**
- Consumes: 共享 Repository 与 Embedding Provider
- Produces: 应用级 `ConversationRetrievalService`、公共历史 Tools、完成依赖注入的根图

- [ ] 写失败测试，验证生命周期向根图注入召回 Service 和两项公共历史 Tool。
- [ ] 运行测试并确认当前生命周期未接线。
- [ ] 增加缓存 Service、生命周期 Tool 组装和清理逻辑，并同步架构文档。
- [ ] 运行定向测试、完整 pytest 和 Ruff，核对工作区差异。
