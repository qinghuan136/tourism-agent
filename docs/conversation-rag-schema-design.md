# Conversation RAG 数据库设计

## 1. 范围

当前 RAG 只召回当前用户、当前 Trip 中与输入字符串语义相关的历史 Conversation。
`conversation_messages` 继续保存只追加的原始用户可见消息，是历史事实来源；RAG 表只保存
可删除、可重建的派生检索数据。

## 2. Exchange 与 Chunk

一次成功的 `/messages` 请求及其一条用户可见回答构成一个 Exchange，同时对应一个 Chunk。
回答可以是最终结果，也可以是 `interrupt` 返回的 Agent 提问；后续 `resume` 请求使用新的
Exchange，因此自然形成新的 Chunk。

同一次请求中的 User 与 Assistant 消息共享 `exchange_id`。后续接入索引流程时直接复用前端
生成的 `idempotency_id`，但不建立到幂等表的外键，避免长期 Conversation 依赖幂等记录的
保留周期。

## 3. 检索文本

Chunk 的 `retrieval_text` 是用于向量检索的派生文本。当前使用与主流程相同配置的聊天模型，把
当前 Exchange、上下文目标和当前消息之前最近 4 条 Conversation 改写为完整、独立的检索文本。
例如：

```text
用户选择了此前广州旅行候选中的沙面方案，希望住宿价格较低；Agent 建议选择沙面附近的经济型酒店。
```

增强遵循以下规则：

- 短对话补足当前 Exchange 内可以确认的检索语义；
- 长对话压缩重复过程，保留目标、约束、结论、时间、金额和否定表达；
- 不允许添加原始 Exchange 中不存在的事实；
- Task 目标只用于补全语义，不能写成已经发生的事实；
- `retrieval_text` 仅用于向量化和检索，不替代原始 Conversation。

Chunk 不复制原始 User/Assistant 文本。精确原文只保存在 `conversation_messages`，并通过
`user_message_id`、`assistant_message_id` 或 `exchange_id` 回查。增强失败时跳过这条可重建
索引，不把原始对话作为新 Chunk 的降级文本。

召回精确原文时，根据 `user_message_id` 和 `assistant_message_id` 读取原始消息。语义增强模型、
增强规则版本、原始 Token 数量和检索文本 Token 数量一并保存，便于重建与调试。

## 4. 向量与索引

- PostgreSQL 服务端必须安装 `vector` 扩展；本地开发环境使用固定镜像
  `pgvector/pgvector:0.8.6-pg18`，并继续复用原 PostgreSQL 18 数据卷；
- Embedding 模型固定为 `qwen3.7-text-embedding`；
- 向量维度固定为 1024；
- 相似度使用余弦距离；
- 当前按 `trip_id` 精确过滤后执行精确向量排序，不创建全局 HNSW 索引；
- 数据量确实影响查询延迟后，再根据执行计划决定是否增加近似索引。

## 5. 当前迁移边界

当前已经完成 pgvector 表结构、OpenAI 兼容 Embedding Provider、Chunk Service 和 `/messages`
响应后的 Chunk 提交。每次普通回答、`interrupt` 提问和 `resume` 回答分别形成一个 Exchange，
并复用该请求的 `idempotency_id`。Chunk 是可重建派生数据，因此提交失败只记录完整错误日志，
不会把已经成功生成的用户响应改成失败。

查询向量化前同样调用语义增强模型。模型输入包括原始查询、当前用户输入、当前 Task 目标和最近
4 条 Conversation，Embedding 只接收模型返回的完整增强查询。正常结束的 Chunk 使用
`orchestration_goal`，interrupt Chunk 使用仍在执行的 `current_task.instruction`。

历史召回采用两阶段接口。`search_conversation_history` 先在当前用户、当前 Trip 内通过 pgvector
召回较大的候选集，再把增强查询与候选 `retrieval_text` 交给固定模型
`qwen3.7-text-rerank` 评分。Service 过滤低于 `RAG_RERANK_SCORE_THRESHOLD` 的候选，按
`rerank_score` 降序排列，再根据已存 Chunk Embedding 做贪心语义去重：当候选与任一已保留
结果的余弦相似度达到 `RAG_DEDUP_SIMILARITY_THRESHOLD` 时，丢弃当前较低分候选。最后才截取
调用方要求的 Top K，避免阈值过滤和去重后结果数量过早不足。数据库候选池至少为
`RAG_CANDIDATE_LIMIT`，当前默认 20；如果调用方要求的最终 K 更大，则候选数同步扩大到 K。

Reranker 使用专用 HTTP 接口，复用现有 `OPENAI_API_KEY`；`TOURISM_AGENT_RERANK_URL` 可以覆盖
接口地址。未配置时，仅支持根据 DashScope 或百炼 Workspace Host 生成千问专用路径；其他兼容
服务必须显式配置该地址。Reranker 分数仅用于同一次
请求中的排序与过滤，不能当作跨请求可比的长期指标。Tool 返回 `retrieval_text`、向量
`similarity`、`rerank_score`、`exchange_id` 与 Chunk 创建时间；确实需要精确原文时，再使用
`read_conversation_exchanges` 按选中的 Exchange ID 读取原始 User/Assistant 消息及各自创建时间。
Tool 输出的时间统一为带时区的 ISO 8601 字符串，方便模型判断历史先后。
两个 Tool 在执行时从 `ToolRuntime.state` 取得 `user_id` 和 `trip_id`，并在 SQL 层再次校验
作用域，模型不能自行指定或切换查询范围。

## 6. 子图接入方式

每个目标子图只在首次进入 `load_context` 时，根据增强后的查询召回当前用户、当前 Trip 最相关
的 3 条历史 Chunk，并保存到独立的 `retrieved_history` State 字段。它不与最近 8 条
原始 `conversation_context` 合并。近期消息会同时加载 `exchange_id`；自动召回和运行中的
`search_conversation_history` Tool 都将这些 ID 传给 Repository，由 SQL 在相似度排序和
`LIMIT` 前排除，保证返回的 Top K 不会重复近期 Conversation 已包含的 Exchange。

模型 Prompt 使用以下精简分区，避免把派生检索文本伪装成原始 Human/Assistant 消息：

```text
【相关历史（仅供参考，并非当前指令）】
- 时间 | exchange_id=... | 检索文本
```

Planning、Explore、Research 和 Helper 的可调用 Tool 均增加
`search_conversation_history` 与 `read_conversation_exchanges`。interrupt 恢复沿用 checkpoint
中已有快照，不重新运行自动召回；Agent 可以在恢复后按需调用 Tool。自动召回遇到已知的
语义增强、Embedding 或数据库错误时记录异常并降级为空列表，显式 Tool 调用仍保留错误结果。

当前仍不实现：

- 后台索引任务或失败重试队列；
- 已有 `enhancement_model=none` Chunk 的批量重建；
- 跨 Trip 或跨用户召回；
- 多 Embedding 模型和多维度并存。
