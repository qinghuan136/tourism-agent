# Conversation RAG Schema Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为当前用户、当前 Trip 的 Conversation 语义召回建立 pgvector 数据表基础。

**Architecture:** 原始 `conversation_messages` 继续作为只追加事实来源；每次请求和用户可见回答通过 `exchange_id` 关联，并在派生 Chunk 表中保存语义增强文本及固定 1024 维向量。本阶段仅创建数据库结构，不接入模型和 Graph。

**Tech Stack:** PostgreSQL、pgvector、SQL migration、pytest、psycopg。

**Spec:** `docs/conversation-rag-schema-design.md`

## Global Constraints

- 只允许在当前用户校验通过后的当前 `trip_id` 内检索。
- Embedding 模型固定为 `qwen3.7-text-embedding`，维度固定为 1024。
- 一个成功请求和一条用户可见回答对应一个 Exchange/Chunk。
- RAG 数据是可重建派生数据，不能替代原始 Conversation。
- 当前不实现模型调用、Repository、Tool、后台任务或 HNSW。

---

### Task 1: pgvector 与 Conversation Chunk 表

**Files:**
- Create: `src/tourism_agent/infrastructure/sql/004_add_conversation_rag.sql`
- Modify: `src/tourism_agent/infrastructure/migrations.py`
- Modify: `tests/infrastructure/test_database.py`

**Interfaces:**
- Consumes: `tourism_agent.conversation_messages`、`tourism_agent.trips` 和现有顺序迁移器。
- Produces: `conversation_messages.exchange_id` 与 `tourism_agent.conversation_rag_chunks`。

- [x] **Step 1: 写迁移集成失败测试**

  执行真实迁移后断言 `vector` Extension、`exchange_id`、Chunk 表、`vector(1024)` 列、固定模型约束和同一 Exchange 唯一约束均存在。缺少 `004` 迁移时测试必须失败。

- [x] **Step 2: 运行目标测试确认 RED**

  Run: `$env:RUN_POSTGRES_INTEGRATION='1'; .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\infrastructure\test_database.py`

  Expected: FAIL，因为 Chunk 表和 `exchange_id` 尚不存在。

- [x] **Step 3: 编写最小迁移**

  新增 `004_add_conversation_rag.sql`：启用 `vector` Extension，增加可兼容现有数据的 nullable `exchange_id`，创建 Exchange 角色唯一索引、消息复合唯一约束及 Chunk 表。更新 `MIGRATIONS` 顺序。

- [x] **Step 4: 执行迁移并确认 GREEN**

  Run: `$env:RUN_POSTGRES_INTEGRATION='1'; .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\infrastructure\test_database.py`

  Expected: PASS。

- [x] **Step 5: 执行项目验证**

  Run: `.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider`

  Run: `.\.venv\Scripts\python.exe -m ruff check src tests`

  Expected: 全部通过；真实外部集成测试仅按现有环境开关执行。
