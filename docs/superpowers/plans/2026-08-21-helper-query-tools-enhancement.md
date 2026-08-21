# Helper 查询 Tools 增强实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Helper 增加高德路线、距离比较和受限匿名 Playwright MCP 能力，同时增强地点详情。

**Architecture:** 高德 HTTP 与 Playwright MCP 通信都封装在 Provider 边界；公共 Tool 只暴露稳定参数和经过不可信标记的裁剪结果。Playwright 按 `thread_id` 懒加载隔离会话，动作串行且受 URL、次数、标签页和超时限制，只有 Helper 白名单可以绑定。

**Tech Stack:** Python 3.12、LangChain Tools、LangGraph ToolNode、httpx、高德 Web API、langchain-mcp-adapters、`@playwright/mcp@0.0.79`

**Spec:** `docs/helper-subgraph-design.md`

## Global Constraints

- 仅服务中国大陆路线和地点查询。
- Playwright 只能匿名访问公开 HTTP/HTTPS 网页。
- 禁止登录、验证码、Cookie 继承、敏感信息、上传下载、订单、预订和支付。
- 一个 Tool 调用轮次最多执行一个 Playwright 动作；`ask_user` 继续独占一轮。
- 新增或修改的注释、Docstring 和面向用户的异常信息使用中文。
- 不修改现有数据库模型，也不引入新的业务持久化表。

---

### Task 1: 增强高德地点详情

**Files:**
- Modify: `src/tourism_agent/providers/travel.py`
- Test: `tests/providers/test_travel.py`

**Interfaces:**
- Consumes: `AmapPlacesClient.get_place_details(place_id: str) -> str`
- Produces: 包含 `business,navi` 查询参数及导航入口、出口摘要的同一接口

- [ ] 写测试，断言地点详情请求 `show_fields=business,navi`，并裁剪输出导航入口和出口。
- [ ] 运行目标测试，确认因现有实现只请求 `business` 而失败。
- [ ] 修改 `AmapPlacesClient.get_place_details` 的请求和响应格式化。
- [ ] 运行地点 Provider 测试，确认通过。

### Task 2: 实现高德路线与距离 Provider/Tools

**Files:**
- Modify: `src/tourism_agent/providers/travel.py`
- Modify: `src/tourism_agent/graph/tools/travel_query.py`
- Test: `tests/providers/test_travel.py`
- Test: `tests/graph/test_planning_query_tools.py`

**Interfaces:**
- Produces: `AmapRouteClient.plan_route(origin, destination, mode, region, departure_time, preference) -> str`
- Produces: `AmapRouteClient.measure_travel_distance(origins, destination, mode, region) -> str`
- Produces: 公共 Tools `plan_route` 与 `measure_travel_distance`

- [ ] 写 Provider 失败测试，覆盖地点解析、驾车策略映射、公交城市参数、路线裁剪和距离模式映射。
- [ ] 运行 Provider 目标测试，确认缺少 `AmapRouteClient`。
- [ ] 实现地点解析、路线请求、距离请求和紧凑文本输出。
- [ ] 运行 Provider 目标测试，确认通过。
- [ ] 写 Tool 失败测试，断言两个 Tool 参数透传、外部数据标记和日志。
- [ ] 运行 Tool 目标测试，确认工具尚不存在。
- [ ] 扩展 `create_query_tools` 接收路线客户端并返回两个新 Tool。
- [ ] 运行公共 Tool 测试，确认通过。

### Task 3: 实现受限 Playwright MCP Client 和公共包装 Tools

**Files:**
- Create: `src/tourism_agent/providers/browser.py`
- Create: `src/tourism_agent/graph/tools/browser.py`
- Create: `tests/providers/test_browser.py`
- Create: `tests/graph/test_browser_tools.py`

**Interfaces:**
- Produces: `PlaywrightBrowserClient.invoke(thread_id, tool_name, arguments) -> str`
- Produces: `PlaywrightBrowserClient.close_thread(thread_id) -> None` 和 `close_all() -> None`
- Produces: `create_browser_tools(browser_client) -> list[BaseTool]`

- [ ] 写 Client 失败测试，覆盖显式打开URL的危险协议、本地或私有地址、动作上限、标签页上限和线程隔离。
- [ ] 运行 Client 测试，确认模块不存在。
- [ ] 实现 URL 校验、每线程会话、串行锁、白名单调用、资源限制和输出裁剪。
- [ ] 运行 Client 测试，确认通过。
- [ ] 写包装 Tool 失败测试，覆盖 `thread_id` 传递、导航校验和不可信数据标记。
- [ ] 运行 Tool 测试，确认模块或 Tools 尚不存在。
- [ ] 实现 10 个显式 Playwright Tool 包装及统一日志。
- [ ] 运行包装 Tool 测试，确认通过。

### Task 4: 接入应用生命周期与 Helper LangGraph

**Files:**
- Modify: `src/tourism_agent/providers/travel.py`
- Modify: `src/tourism_agent/api.py`
- Modify: `src/tourism_agent/graph/subgraphs/helper/tools.py`
- Modify: `src/tourism_agent/graph/subgraphs/helper/graph.py`
- Modify: `docs/helper-subgraph-design.md`
- Test: `tests/api/test_lifecycle.py`
- Test: `tests/api/test_messages.py`
- Test: `tests/graph/test_helper_graph.py`

**Interfaces:**
- Consumes: `TravelQueryClients.routes`、`TravelQueryClients.browser`
- Produces: Helper 绑定路线、距离和 Playwright 白名单；普通结束与取消时关闭对应线程浏览器会话

- [ ] 写 Helper 失败测试，断言新 Tool 白名单，并拒绝同轮多个 Playwright 动作或 Playwright 与其他 Tool 混用。
- [ ] 运行 Helper 目标测试，确认现有路由只约束 `ask_user`。
- [ ] 更新 Helper 白名单、Prompt 和 Tool 批次路由。
- [ ] 运行 Helper 测试，确认通过。
- [ ] 写生命周期失败测试，断言启动时注入路线和浏览器 Tools，退出时关闭全部会话，正常完成和取消时关闭线程会话。
- [ ] 运行生命周期/API 目标测试，确认缺少相关清理。
- [ ] 组装 Provider、公共 Tools 和 API 清理边界，并同步修正文档中实际 MCP 工具清单。
- [ ] 运行生命周期、API 和 Helper 测试，确认通过。

### Task 5: 全量验证

**Files:**
- Verify: `src/`
- Verify: `tests/`
- Verify: `docs/helper-subgraph-design.md`

- [ ] 运行 `pytest -q -p no:cacheprovider --ignore=tests/integration`。
- [ ] 运行 `ruff check src tests`。
- [ ] 运行 `git diff --check` 并检查本轮文件差异。
- [ ] 可用 Node/npm 环境下加载 `@playwright/mcp@0.0.79` 工具列表，验证必需白名单存在；不执行真实网页交易操作。
