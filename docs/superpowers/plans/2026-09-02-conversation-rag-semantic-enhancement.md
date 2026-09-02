# Conversation RAG Semantic Enhancement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 使用当前聊天模型增强查询和 Exchange，并确保 pgvector 与 Chunk 只消费增强后的文本。

**Architecture:** 新增无状态 `SemanticEnhancementService`，由 Conversation Retrieval 和 Chunk Service
复用。根图与四个子图只传递增强所需的用户输入、Task 目标和近期 Conversation，不新增图节点，
不改变 Checkpoint 结构或数据库表。

**Tech Stack:** Python、Pydantic、LangChain chat model、LangGraph、PostgreSQL/pgvector、pytest

**Spec:** `docs/superpowers/specs/2026-09-02-conversation-rag-semantic-enhancement-design.md`

## Global Constraints

- 新增 Docstring、注释与异常信息使用中文。
- Embedding 模型固定为 `qwen3.7-text-embedding`，维度固定为 1024。
- 最近增强历史固定为当前消息之前 4 条。
- 原始对话不复制到新 Chunk；增强失败不写 Chunk。
- 当前脏工作区内执行，不自动创建提交。

---

### Task 1: 语义增强 Service

**Files:**
- Create: `src/tourism_agent/services/semantic_enhancement.py`
- Create: `tests/services/test_semantic_enhancement.py`

**Interfaces:**
- Produces: `SemanticEnhancementService.enhance_query(...) -> str`
- Produces: `SemanticEnhancementService.enhance_exchange(...) -> str`
- Produces: `model_name: str`

- [x] 写失败测试，证明两个方法向模型明确传入原始内容、目标和最近历史，并返回 Schema 中的完整 `retrieval_text`。
- [x] 运行测试，确认因 Service 尚不存在而失败。
- [x] 实现单字段 Pydantic 输出、两个中文 Prompt 和最近 4 条历史格式化。
- [x] 运行聚焦测试并确认通过。

### Task 2: Retrieval 与 Chunk 只消费增强文本

**Files:**
- Modify: `src/tourism_agent/services/conversation_retrieval.py`
- Modify: `src/tourism_agent/services/conversation_chunk.py`
- Modify: `src/tourism_agent/api.py`
- Modify: `tests/services/test_conversation_retrieval.py`
- Modify: `tests/services/test_conversation_chunk.py`

**Interfaces:**
- Consumes: Task 1 的 `SemanticEnhancementService`。
- Produces: `ConversationRetrievalService.search(..., current_user_input, task_goal, recent_conversation)`。
- Produces: `ConversationChunkService.submit(..., context_goal, recent_conversation)`。

- [x] 修改 Service 测试，断言 Embedding 输入和 Chunk `retrieval_text` 都等于增强结果，而非原文。
- [x] 运行测试，确认旧实现导致预期失败。
- [x] 注入增强 Service，保留既有 1024 维校验与可信数据库作用域。
- [x] 在 API 依赖中创建并复用当前模型配置的增强 Service。
- [x] 运行 Service 测试并确认通过。

### Task 3: Graph、Tool 与 API 上下文接入

**Files:**
- Modify: `src/tourism_agent/graph/root.py`
- Modify: `src/tourism_agent/graph/history.py`
- Modify: `src/tourism_agent/graph/tools/conversation_history.py`
- Modify: `src/tourism_agent/graph/subgraphs/*/state.py`
- Modify: `src/tourism_agent/graph/subgraphs/*/graph.py`
- Modify: `tests/graph/test_conversation_history_context.py`
- Modify: `tests/graph/test_conversation_history_tools.py`
- Modify: `tests/api/test_messages.py`
- Modify: `docs/conversation-rag-schema-design.md`
- Modify: `docs/architecture.md`

**Interfaces:**
- Consumes: Task 2 扩展后的 Retrieval/Chunk 方法。
- Produces: 自动召回、显式 Tool 和 Chunk 提交均包含用户输入、Task/总体目标和最近历史。

- [x] 写失败测试：自动召回和 Tool 传递当前用户输入、Task 目标与近期历史；正常结束 Chunk 使用总体目标，interrupt 使用当前 Task。
- [x] 运行聚焦测试，确认新上下文参数缺失导致失败。
- [x] 根图向四个子图映射 `retrieval_user_input` / `retrieval_task_goal`，子图加载和 Tool 传给 Retrieval Service。
- [x] API 在当前 UserMessage 之前加载 4 条历史，并按 interrupt/正常结束选择 Chunk 上下文目标。
- [x] 同步 RAG 与总体架构文档。
- [x] 运行相关测试、全量 pytest、Ruff 和 `git diff --check`。
