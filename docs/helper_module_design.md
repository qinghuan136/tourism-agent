# Helper 模块设计

## 1. 模块定位

`HelperGraph` 用于处理旅行过程中**不需要重新规划，也不值得进入深度 Research 的轻量查询、网页操作和便利任务**。

核心特点：

- 目标通常明确
- 单次任务粒度较小
- 以查询、读取、比较、辅助操作为主
- 尽量不修改旅行核心业务状态
- 通过 `ReAct + Tools` 自动组合能力完成任务

整体定位：

```text
Explore
= 帮用户发现“有什么值得去”

Research
= 帮用户把某件事“研究明白”

Planning
= 帮用户决定“旅行怎么安排”，并修改旅行计划

Helper
= 帮用户“查一下、算一下、看一下、操作一下”
```

---

## 2. 核心功能

### 2.1 轻量事实查询

处理明确、局部的信息查询，例如：

- 天气
- POI 基本信息
- 营业时间
- 地址、联系方式
- 景点票价
- 简单路线、距离、预计耗时
- 交通班次等公开信息
- 普通 Web Search 可以直接解决的问题

典型工具：

```text
web_search
get_weather
search_places
get_place_details
get_route
```

### 2.2 网页内容提取与解释

用于读取指定网页并回答用户问题，例如：

- “这个景区退改规则是什么意思？”
- “这个酒店页面包不包含早餐？”
- “帮我总结一下这个购票页面的信息。”
- “官网写的是几点停止入场？”

优先使用：

```text
Tavily Extract
```

如果页面依赖 JS、需要交互后才能看到内容，再使用：

```text
Playwright MCP
```

### 2.3 复杂网页内查询

当普通 Web Search 搜不到、搜不准，或者信息必须通过网页交互才能获得时，使用 Playwright。

典型场景：

- 输入日期后查询余票
- 输入出发地、目的地后查看班次
- 设置入住日期、人数后查询酒店价格
- 页面需要点击筛选、翻页、展开内容
- JS 动态页面无法直接 Extract

基本流程：

```text
navigate
↓
snapshot
↓
click / type / select
↓
snapshot
↓
读取结果
↓
必要时继续操作
↓
返回查询结果
```

当前阶段以**只读型网页操作**为主。

### 2.4 页面与链接定位

Helper 可以帮助用户找到：

- 官方网站
- 官方购票页面
- 酒店页面
- 交通平台页面
- 活动详情页
- 地图 / 导航页面

不额外实现前端 `open_url` Action。

最终直接在回答中返回 URL，由用户自行点击。

### 2.5 旅行信息计算与比较

处理简单、确定性较强的计算与比较，例如：

- A、B 两个景点哪个离酒店更近
- 地铁和打车分别需要多久
- 几个班次哪个最早到达
- 多个方案的价格、时间比较
- 路线距离、耗时排序
- 简单时间差、预算差计算

流程通常是：

```text
查询相关 Tool
↓
得到结构化信息
↓
Agent / 程序做简单计算与比较
↓
返回结论
```

不需要进入 Planning，除非用户进一步要求修改实际旅行安排。

### 2.6 当前旅行信息查询与解释

Helper 可以读取当前旅行上下文并回答：

- “我第二天安排了什么？”
- “我现在住哪个酒店？”
- “为什么这里留了两个小时？”
- “这个景点离我酒店远吗？”

原则：

```text
读取 / 查询 / 解释
→ Helper

修改 TripContext / Itinerary
→ Planning
```

---

## 3. 推荐架构

Helper 第一版直接采用：

```text
START
  ↓
Helper Agent
  ↓
ToolNode
  ↓
Helper Agent
  ↓
...
  ↓
Finalize
  ↓
END
```

即标准：

```text
ReAct + Tools
```

Agent 负责判断：

- 是否需要调用工具
- 调哪个工具
- 工具结果是否足够
- 是否需要继续查询
- 何时结束任务

Graph 本身无需设计复杂固定流程。

---

## 4. Tool 能力划分

```text
HelperGraph
│
├── Web
│   ├── web_search
│   └── extract_webpage
│
├── Browser
│   ├── browser_navigate
│   ├── browser_snapshot
│   ├── browser_click
│   ├── browser_type
│   └── browser_select
│
├── Places / Maps
│   ├── search_places
│   ├── get_place_details
│   └── get_route
│
├── Weather
│   └── get_weather
│
└── Utility
    ├── 时间计算
    ├── 数值比较
    └── 简单排序 / 汇总
```

工具设计原则：

> 对 LLM 暴露业务能力，而不是底层基础设施。

例如优先：

```text
get_route
inspect_webpage
```

而不是：

```text
http_get
parse_html
execute_sql
```

---

## 5. Web 与 Playwright 的使用原则

优先级：

```text
普通 Web Search / Extract
↓
信息不足
↓
Playwright 细致网页查询
```

只有满足以下情况之一时才优先使用 Playwright：

1. 页面依赖 JS 动态渲染
2. 必须填写日期、地点、人数等条件
3. 必须点击、筛选、翻页后才能得到结果
4. 普通搜索无法可靠获得目标信息

避免所有网页问题都启动 Browser，减少延迟和资源消耗。

---

## 6. 当前阶段的操作边界

允许：

```text
✅ 浏览公开网页
✅ 输入查询条件
✅ 点击筛选、日期、翻页
✅ 读取动态页面结果
✅ 查询票价、班次、公开余票
✅ 提取和解释网页内容
✅ 返回目标页面 URL
```

暂不实现：

```text
❌ 登录用户私人账号
❌ 验证码处理
❌ 提交真实订单
❌ 支付
❌ 退款 / 取消订单
❌ 具有明显外部副作用的表单提交
❌ 接管用户本地浏览器
```

如果未来需要真实预订、支付等能力，应单独进入 Booking / Order 模块。

---

## 7. State 建议

Helper State 可以保持很小：

```python
class HelperState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]

    task_input: str
    context: HelperContext

    task_result: TaskResult | None
```

其中：

- `messages`：当前 ReAct 的 AI / Tool 工作消息
- `task_input`：Orchestrator 下发的当前任务
- `context`：当前任务真正需要的旅行上下文
- `task_result`：最终返回 Orchestrator 的结果

不在 Helper State 中保存：

```text
task_queue
next_task
完整 UserProfile
完整 TripContext
完整网页原始数据
所有 Tool 历史结果
```

这些分别属于 Orchestrator、DB/Shared State 或 `messages`。

---

## 8. TaskResult

沿用统一任务协议：

```python
class TaskResult(BaseModel):
    status: Literal["success", "partial", "failed"]
    result: str
```

`result` 应包含：

- 用户真正需要的结论
- 必要的数字、地点、时间等关键事实
- 必要的 URL
- 如果部分信息获取失败，需要明确说明缺失部分

---

## 9. 与其他模块的边界

```text
“广州有什么值得去的展？”
→ Explore

“这个展到底值不值得去？”
→ Research

“把这个展安排到周六下午。”
→ Planning

“这个展几点结束？”
→ Helper

“帮我看看官网下周六还有没有票。”
→ Helper + Playwright

“这个页面的退改规则是什么意思？”
→ Helper

“A 和 B 哪个离酒店近？”
→ Helper

“把更近的那个加进行程。”
→ Orchestrator → Helper / Planning
```

---

## 10. 第一版优先实现

建议优先完成：

1. Web Search
2. Web 内容提取与总结
3. POI 详情查询
4. 天气查询
5. 简单路线查询
6. 信息计算与比较
7. Playwright 细致网页查询
8. 页面 / 购票入口 URL 定位

当前阶段不继续扩张功能范围。

后续遇到新的轻量旅行辅助需求时，优先通过**新增 Tool**扩展 Helper，而不是修改 Graph 主流程。
