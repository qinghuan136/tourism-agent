# 本地用户会话与 Trip 选择设计

## 目标

在后端尚未提供登录和 Trip 列表接口的阶段，为前端提供一个可用入口：用户填写 `user_id` 和
`trip_id`，浏览器持久化当前身份与最近成功访问的旅行，随后所有现有请求自动携带当前
`user_id`。

## 范围与非目标

- 范围：本地 UUID 输入与校验、浏览器持久化、最近 Trip 选择、入口路由、对话页切换入口。
- 不实现：登录凭证、用户资料、Trip 创建、Trip 删除、服务端 Trip 列表，或任何权限绕过。
- `localStorage` 仅保存普通 UUID，不能被解释为真实认证或安全边界。

## 页面与路由

根路由 `/` 显示“进入旅行工作区”页：

1. 必填 `user_id` 和 `trip_id`，均以 UUID 格式校验。
2. 提交后保存当前会话并跳转 `/trips/:tripId`。
3. 当当前用户有已记录的最近 Trip 时，以列表展示其 UUID；选择条目只会预填对应
   `trip_id`，仍由用户点击提交进入。
4. `user_id` 修改后不展示旧用户的最近 Trip。

`/trips/:tripId` 通过路由守卫要求当前用户会话存在；不存在时带上目标地址跳转 `/`。对话页
加载收到后端 `404` 时保留现有错误提示，并提供“切换旅行”返回入口页。

## 状态与持久化

新增 Pinia `local-session` store，作为前端唯一的当前用户来源：

```ts
interface LocalSession {
  userId: string
  tripId: string | null
}

interface RecentTrip {
  userId: string
  tripId: string
}
```

- 存储键固定且带版本前缀，例如 `tourism-agent.local-session.v1` 和
  `tourism-agent.recent-trips.v1`。
- 只有用户主动提交有效 UUID 时写入当前会话。
- 只有 Trip 工作区的 bootstrap 成功加载时，才将该 Trip 写入最近记录，避免保存无权限或
  不存在的旅行。
- 最近记录按最近成功访问顺序去重，最多保留 8 项；只呈现当前 `user_id` 的记录。
- 本地存储不可用或内容损坏时回退为空状态，并允许用户重新输入，不阻断页面。

## 请求与数据流

```text
入口表单 → local-session store → localStorage
                           ↓
                  /trips/:tripId 路由
                           ↓
Trip workspace / SSE API / 历史分页
             使用 local-session.userId
                           ↓
bootstrap 成功 → recent trips 写入 localStorage
```

移除 `runtimeConfig.demoUserId` 作为业务请求的身份来源；现有的 bootstrap、历史分页和
`POST /messages/stream` 均从 `local-session.userId` 获取 `user_id`。开发环境变量可保留
`VITE_DEMO_TRIP_ID` 作为入口表单的初始示例值，但不会覆盖用户已保存会话。

## 交互与错误处理

- UUID 格式错误直接在表单内提示，不发送请求。
- 未保存当前用户时，路由守卫将对话页重定向至入口，防止请求发出空或默认身份。
- 保存身份或选择 Trip 不检查其所属关系；归属以既有 bootstrap 的服务端 `404` 为准。
- 切换用户或旅行不会取消已有运行；若当前页面存在运行中请求，切换入口仅发生在用户主动
  选择后。前端不伪造取消成功状态。

## 测试与验收

1. Store 测试：有效输入持久化、损坏存储回退、最近 Trip 去重与按用户过滤。
2. Router 测试：无用户会话时进入对话页会跳转入口；提交后进入指定 Trip。
3. Workspace 测试：bootstrap 成功时记录当前 Trip，所有 API 调用使用保存的用户。
4. UI 测试：表单验证、最近 Trip 预填、后端 404 后可返回切换入口。
5. 执行 `pnpm typecheck`、`pnpm lint`、`pnpm test` 与 `pnpm build`；浏览器可用时验证桌面和移动端入口到对话页的核心路径。
