# Orchestrator Handoff Context 设计

## 目标

保留每个子图产生的原始 `TaskResult` 作为本轮执行事实，由现有 Orchestrator Reviewer 在每个 Task 完成后整理面向下一 Task 的 `handoff_context`。下一子图不再直接消费最近一个原始结果，而是消费原始用户目标、当前任务指令和经过整理的交接上下文。

## 核心边界

- `task_results` 保留全部原始结果，继续提供给 Reviewer 和 Finalizer，不压缩、不覆盖。
- `handoff_context` 是可重新生成的派生工作上下文，不是长期记忆，也不写入 Conversation 或 RAG Chunk。
- 不新增 Formatter Agent 或额外 LLM 节点；现有 Reviewer 在同一次结构化调用中同时返回复核动作和交接上下文。
- `handoff_context` 使用自由文本，不为候选地点、证据、约束等内容设计固定字段。
- 根图只保存并传递交接上下文，不从自然语言中再次解析业务字段。

## 数据契约

`PlanReviewDecision` 增加：

```python
handoff_context: str = ""
```

约束如下：

- `continue` 和 `replace_remaining` 必须返回非空 `handoff_context`；即使没有可供下游使用的结果，也应明确写出“暂无有效结果”及下一 Task 需要自行确认的内容。
- `finish` 必须返回空字符串，因为不存在下一 Task。
- 现有 `replacement_tasks` 跨字段规则保持不变。

`RootState` 增加覆盖型字段：

```python
handoff_context: NotRequired[str]
```

创建新计划时将其初始化为空字符串；每次应用 Reviewer 决策时覆盖旧值。

## 运行流程

```text
子图正常完成
→ 原始结果追加到 task_results
→ Reviewer 读取用户目标、全部 task_results 和剩余任务
→ Reviewer 返回 action / replacement_tasks / handoff_context
→ 确定性代码应用复核动作并保存 handoff_context
→ 根图取出下一 Task
→ 子图收到用户原始目标、当前 Task、handoff_context
```

Reviewer 只能依据已完成 `TaskResult` 整理以下信息：

- 已确认的有效结果；
- 与下一 Task 有关的候选、结论、约束和证据线索；
- 尚未确认、需要下一 Task 继续处理的问题。

Reviewer 不得补充未被执行结果支持的新事实，也不得把内部 task ID、JSON 或调度过程写入交接文本。

## 子图执行输入

内部任务消息调整为：

```text
【原始用户目标】
{user_input}

【当前子任务】
{current_task.instruction}

【Orchestrator整理的现有结果】
{handoff_context 或“当前没有已完成任务的有效结果”}
```

不再把最近一个 `TaskResult.result` 原文放入内部 HumanMessage。完整原始结果仍保留在根图 State 中。

## RAG 查询

根图根据自己持有的编排信息构造专门的查询文本：

```text
【当前检索目标】
{current_task.instruction}

【用户总体目标】
{user_input}

【Orchestrator整理的现有结果】
{handoff_context，可为空}
```

根图在调用子图时通过独立的 `retrieval_query` 字段传入。四个子图仍在各自 `load_context` 节点调用 `load_related_history()`，继续负责 user/trip 过滤、近期 Conversation Exchange 排除和召回结果注入。
根图正常执行路径始终提供该字段；为兼容子图独立调用和调试，State 将其声明为可选，缺失时才回退到当前执行消息。

`messages[0]` 与 `retrieval_query` 的职责不同：

- `messages[0]` 告诉 Agent 当前需要完成什么；
- `retrieval_query` 告诉 RAG 应从历史 Conversation 中寻找什么。

Agent 主动调用 `search_conversation_history` Tool 时仍自行填写查询字符串，本设计只改变进入子图时的自动召回。

## 中断与持久化

- `handoff_context` 和 `retrieval_query` 都属于当前根图运行的临时 State。
- Planning 在 interrupt 前已经选定当前 Task；恢复时沿原 checkpoint 继续，不重新生成 handoff。
- 正常请求完成后继续由 API 清理进程内 checkpoint。
- 两个字段均不写数据库，不生成新的 Conversation 或 RAG Chunk。

## 测试要求

- 校验不同 ReviewAction 与 `handoff_context` 的跨字段关系。
- 证明 Reviewer 根据全部原始结果生成 handoff，而原始 `task_results` 未被覆盖。
- 证明第二个子图收到 handoff，不再收到最近 TaskResult 原文。
- 证明 `continue`、`replace_remaining` 和 `finish` 分支正确更新或清理 handoff。
- 证明四个子图自动召回使用 `retrieval_query`，而不是执行消息全文。
- 运行 interrupt/resume、API Conversation/RAG Chunk 和全量回归，确认内部 handoff 不进入长期数据。

## 非目标

- 不对四个子图输出进行结构化改造。
- 不增加额外总结模型、Formatter Agent 或查询改写模型。
- 不修改 Conversation Chunk、Embedding、向量维度和 PostgreSQL 表结构。
- 不改变主动历史召回 Tool 的接口。
