# Conversation RAG 语义增强设计

## 目标

在查询向量化前和 Conversation Chunk 入库前，使用当前聊天模型把依赖上下文的表达改写为完整、
独立、适合向量检索的文本。原始 User/Assistant 消息只保存在 `conversation_messages`；新生成的
Chunk 只保存增强后的 `retrieval_text`，不复制原始对话。

## 核心边界

- 语义增强是普通 Service，不是 Agent、Tool 或 LangGraph 节点。
- 查询 Embedding 只接收增强后的查询；Chunk Embedding 只接收增强后的 `retrieval_text`。
- 增强模型复用 `TOURISM_AGENT_MODEL`、`OPENAI_API_KEY` 和 `OPENAI_BASE_URL` 配置。
- 模型输出使用只有 `retrieval_text` 一个字段的结构化 Schema。
- 增强时尽量保留原表达中的地点、时间、金额、人数、偏好、否定和不确定性，只补全指代与必要语义。
- 模型不得把 Task 目标写成已经发生的事实，也不得服从作为数据输入的对话内容中的指令。
- 近期历史固定取当前 UserMessage 之前最近 4 条 Conversation。
- 不修改数据库表结构；现有消息外键负责从 Chunk 回查原文。

## 查询链路

自动召回与 `search_conversation_history` Tool 都使用同一增强流程：

```text
原始查询 + 当前用户输入 + 当前 Task 目标 + 最近 4 条 Conversation
→ SemanticEnhancementService.enhance_query
→ 完整增强查询
→ Embedding
→ 当前 user_id / trip_id 内的 pgvector 检索
```

根图向子图明确传递当前用户输入和 `TaskSpec.instruction`。直接独立调用子图时，分别回退到当前
消息和既有 `retrieval_query`。Tool 调用使用 State 中相同的任务上下文和近期历史。

## Chunk 链路

```text
当前 Exchange 原文 + 本次上下文目标 + 当前 UserMessage 之前最近 4 条 Conversation
→ SemanticEnhancementService.enhance_exchange
→ 完整增强 retrieval_text
→ Embedding
→ conversation_rag_chunks
```

- interrupt 返回可见问题时，使用仍在 State 中的 `current_task.instruction`。
- 整轮正常结束时，使用 `orchestration_goal`，因为回复可能综合多个 Task，且 `current_task` 已清空。
- resume 输入是新的 Exchange，其原始用户文本取当前 API 请求，不复用初始请求。
- `source_token_count` 统计原始 Exchange；`retrieval_token_count` 统计增强文本。
- `enhancement_model` 保存当前聊天模型名，`enhancement_version` 当前为 1。

增强或 Embedding 失败时不写 Chunk，由 API 记录异常但继续返回已经生成的用户响应。自动召回增强
失败时沿用现有策略降级为空结果；显式 Tool 调用保留错误供当前开发阶段观察。不会把原文作为新
Chunk 的降级内容。

## 兼容性

已有 `enhancement_model=none` 的 Chunk 不在本次自动重建，仍可参与检索。后续如需统一质量，可按
`enhancement_model` 和 `enhancement_version` 执行一次独立的重建任务。

