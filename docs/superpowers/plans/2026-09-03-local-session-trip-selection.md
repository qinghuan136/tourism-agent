# 本地用户会话与 Trip 选择 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不依赖后端登录或 Trip 列表接口的前提下，使用户能保存本地 `user_id`、选择 `trip_id` 并进入旅行工作区。

**Architecture:** 新增 Pinia `local-session` store 作为当前用户的唯一前端来源，并同步受版本保护的 `localStorage`。入口页只写入通过 UUID 校验的身份和旅行；Trip bootstrap 成功后才记录最近旅行。路由守卫阻止无用户会话直接进入对话页，工作区现有 API 与 SSE 请求统一读取 store 中的 `userId`。

**Tech Stack:** Vue 3、TypeScript、Pinia、Vue Router、Element Plus、Vitest、Vue Test Utils。

**Spec:** `docs/superpowers/specs/2026-09-03-local-session-trip-selection-design.md`

## Global Constraints

- 不增加后端接口，不伪造 Trip 列表、登录或权限校验。
- `localStorage` 只保存 UUID，不能作为认证或授权依据。
- UUID 格式不合法时不允许写入会话或导航；Trip 所属关系仍由既有 bootstrap 的 `404` 判定。
- 所有业务请求必须使用 `local-session.userId`，不再读取 `runtimeConfig.demoUserId`。
- 最近 Trip 只在 bootstrap 成功后记录，按当前用户过滤、去重并限制为 8 条。
- 保持 Vue 3 + TypeScript + Pinia + Vue Router + Element Plus；所有用户可见文本使用中文。
- 当前工作区存在未提交改动；每个任务结束仅运行验证与 `git status --short`，不自行提交。

---

### Task 1: 本地会话与最近 Trip Store

**Files:**
- Create: `frontend/src/stores/local-session.ts`
- Create: `frontend/src/stores/local-session.spec.ts`
- Modify: `frontend/src/main.ts`

**Interfaces:**
- Produces: `useLocalSessionStore()`，包含 `userId`、`tripId`、`recentTrips`、`restore()`、`saveSession(userId, tripId)`、`recordSuccessfulTrip(tripId)` 和 `clear()`。
- Consumes: 浏览器 `localStorage`；存储失败或损坏时使用空状态。
- Used by: 路由守卫、入口页和 Trip workspace。

- [ ] **Step 1: 写入 Store 的失败测试**

```ts
it('persists a valid session and keeps only the current user recent trips', () => {
  const store = useLocalSessionStore()
  store.saveSession('11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222')
  store.recordSuccessfulTrip('33333333-3333-4333-8333-333333333333')

  expect(store.userId).toBe('11111111-1111-4111-8111-111111111111')
  expect(store.recentTrips.map((trip) => trip.tripId)).toEqual([
    '33333333-3333-4333-8333-333333333333',
    '22222222-2222-4222-8222-222222222222',
  ])
})

it('restores an empty session when stored JSON has an invalid shape', () => {
  localStorage.setItem('tourism-agent.local-session.v1', '{bad json')
  const store = useLocalSessionStore()
  store.restore()

  expect(store.userId).toBeNull()
  expect(store.tripId).toBeNull()
})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pnpm test -- local-session.spec.ts`

Expected: FAIL，因为 `local-session` store 尚不存在。

- [ ] **Step 3: 实现最小可恢复 Store**

```ts
export interface RecentTrip {
  userId: string
  tripId: string
}

export const useLocalSessionStore = defineStore('local-session', () => {
  const userId = ref<string | null>(null)
  const tripId = ref<string | null>(null)
  const recentTrips = ref<RecentTrip[]>([])

  function saveSession(nextUserId: string, nextTripId: string): void { /* 写入内存和 localStorage */ }
  function recordSuccessfulTrip(nextTripId: string): void { /* 当前用户去重、截断至 8 项 */ }
  function restore(): void { /* 仅恢复通过 UUID 结构校验的内容 */ }
  function clear(): void { /* 清空当前会话，不删除其他用户最近记录 */ }

  return { userId, tripId, recentTrips, saveSession, recordSuccessfulTrip, restore, clear }
})
```

`main.ts` 创建 Pinia 实例后先调用 `useLocalSessionStore(pinia).restore()`，再将同一实例安装到应用，保证首次路由守卫可读取恢复后的状态。

- [ ] **Step 4: 运行 Store 测试确认通过**

Run: `pnpm test -- local-session.spec.ts`

Expected: PASS，覆盖写入、损坏存储、用户过滤、去重和 8 项上限。

- [ ] **Step 5: 检查工作区状态**

Run: `git status --short`

Expected: 只出现已有改动与本任务新增的 Store 文件；不提交任何内容。

### Task 2: 入口页与路由守卫

**Files:**
- Create: `frontend/src/views/SessionEntryView.vue`
- Create: `frontend/src/views/SessionEntryView.spec.ts`
- Modify: `frontend/src/router/index.ts`
- Modify: `frontend/src/styles/main.scss`
- Modify: `frontend/src/components/shell/AppHeader.vue`
- Modify: `frontend/src/views/TripWorkspaceView.vue`

**Interfaces:**
- Consumes: `useLocalSessionStore().saveSession()` 与 `recentTrips`。
- Produces: 根路由入口表单；`/trips/:tripId` 的 `beforeEnter` 用户会话检查；工作区页的“切换旅行”导航。
- Used by: 用户从入口页切换到任意已知 Trip。

- [ ] **Step 1: 写入入口与守卫的失败测试**

```ts
it('redirects a workspace route without a saved user to the session entry', async () => {
  await router.push('/trips/22222222-2222-4222-8222-222222222222')
  expect(router.currentRoute.value.name).toBe('session-entry')
  expect(router.currentRoute.value.query.redirect).toBe('/trips/22222222-2222-4222-8222-222222222222')
})

it('saves entered UUIDs and routes to the selected trip', async () => {
  const wrapper = mount(SessionEntryView, { global: { plugins: [pinia, router] } })
  await wrapper.get('#user-id').setValue('11111111-1111-4111-8111-111111111111')
  await wrapper.get('#trip-id').setValue('22222222-2222-4222-8222-222222222222')
  await wrapper.get('form').trigger('submit')

  expect(router.currentRoute.value.fullPath).toBe('/trips/22222222-2222-4222-8222-222222222222')
})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pnpm test -- SessionEntryView.spec.ts`

Expected: FAIL，因为入口视图和路由名尚不存在。

- [ ] **Step 3: 实现入口表单、最近记录与守卫**

```ts
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i

function submit(): void {
  if (!isUuid(userId.value) || !isUuid(tripId.value)) return
  session.saveSession(userId.value, tripId.value)
  void router.push(String(route.query.redirect ?? `/trips/${tripId.value}`))
}

function chooseRecentTrip(selectedTripId: string): void {
  tripId.value = selectedTripId
}
```

路由表新增名称为 `session-entry` 的 `/`；Trip 路由使用 `beforeEnter`，当
`useLocalSessionStore().userId` 为空时返回：

```ts
{ name: 'session-entry', query: { redirect: to.fullPath } }
```

入口页复用现有白色工作区视觉语言，使用语义化 `form`、可见标签和表单内错误提示；最近记录只展示
当前输入用户对应的条目。顶栏和工作区错误状态加入实际可点击的“切换旅行”导航，不能保留无效控件。

- [ ] **Step 4: 运行入口与路由测试确认通过**

Run: `pnpm test -- SessionEntryView.spec.ts App.spec.ts`

Expected: PASS，覆盖表单导航、守卫重定向、最近 Trip 预填与切换入口。

- [ ] **Step 5: 检查工作区状态**

Run: `git status --short`

Expected: 只出现已有改动与本任务入口、路由和样式文件；不提交任何内容。

### Task 3: 将工作区请求切换到保存的用户

**Files:**
- Modify: `frontend/src/config/runtime.ts`
- Modify: `frontend/src/stores/trip-workspace.ts`
- Modify: `frontend/src/stores/trip-workspace.spec.ts`
- Modify: `frontend/src/api/trip.spec.ts`
- Modify: `frontend/src/api/message-stream.spec.ts`

**Interfaces:**
- Consumes: `useLocalSessionStore().userId`、`recordSuccessfulTrip(tripId)`。
- Produces: bootstrap、历史分页和 SSE 消息请求统一使用保存用户；成功 bootstrap 写入最近 Trip。
- Used by: 已有 Trip workspace 页面和所有后端请求。

- [ ] **Step 1: 写入工作区身份来源的失败测试**

```ts
it('uses the saved user and records the trip only after bootstrap succeeds', async () => {
  const session = useLocalSessionStore()
  session.saveSession('11111111-1111-4111-8111-111111111111', tripId)
  const workspace = useTripWorkspaceStore()

  await workspace.load(tripId)

  expect(fetch).toHaveBeenCalledWith(
    `/api/trips/${tripId}/bootstrap?user_id=11111111-1111-4111-8111-111111111111&message_limit=30`,
    { headers: { Accept: 'application/json' } },
  )
  expect(session.recentTrips[0]).toEqual({ userId: '11111111-1111-4111-8111-111111111111', tripId })
})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pnpm test -- trip-workspace.spec.ts`

Expected: FAIL，因为现有工作区仍读取 `runtimeConfig.demoUserId`。

- [ ] **Step 3: 替换身份来源并记录成功访问**

```ts
const session = useLocalSessionStore()

async function load(tripId: string): Promise<void> {
  const userId = session.userId
  if (!userId) {
    status.value = 'failed'
    return
  }
  bootstrap.value = await loadTripBootstrap(tripId, userId)
  session.recordSuccessfulTrip(tripId)
}
```

对历史分页和 `sendStreamedMessage()` 使用相同的 `session.userId`；若会话在页面仍打开时被清除，返回
“请先选择用户和旅行”的用户可操作错误，绝不回退到 demo 用户。`runtime.ts` 保留 API 基础地址，只移除
业务使用的 `demoUserId`。

- [ ] **Step 4: 运行相关测试确认通过**

Run: `pnpm test -- trip-workspace.spec.ts trip.spec.ts message-stream.spec.ts`

Expected: PASS，既有 API 契约不变，唯一变化是请求用户来自本地会话。

- [ ] **Step 5: 检查工作区状态**

Run: `git status --short`

Expected: 只出现已有改动与本任务预期前端文件；不提交任何内容。

### Task 4: 集成验收与回归验证

**Files:**
- Modify: `frontend/src/App.spec.ts`（如集成场景需适配入口守卫）

**Interfaces:**
- Consumes: 前三项完成的 Store、入口页、路由和工作区。
- Produces: 用户从本地身份入口进入工作区、发送请求、返回切换入口的可验收路径。

- [ ] **Step 1: 写入入口到工作区的失败集成测试**

```ts
it('enters the selected workspace with a saved local user', async () => {
  await router.push('/')
  const wrapper = mount(App, { global: { plugins: [pinia, router] } })
  await wrapper.get('#user-id').setValue('11111111-1111-4111-8111-111111111111')
  await wrapper.get('#trip-id').setValue('22222222-2222-4222-8222-222222222222')
  await wrapper.get('form').trigger('submit')

  await vi.waitFor(() => expect(wrapper.text()).toContain('旅行工作区'))
})
```

- [ ] **Step 2: 运行集成测试确认失败**

Run: `pnpm test -- App.spec.ts`

Expected: FAIL，直到测试夹具和入口流程均接入。

- [ ] **Step 3: 适配集成夹具与可访问状态**

更新现有 `App.spec.ts` 的 Pinia 初始化与 `fetch` 夹具，使其在入口提交后验证 bootstrap 请求中的
`user_id`、最近记录，以及后端 `404` 状态下的“切换旅行”链接。不要为测试新增后端模拟接口。

- [ ] **Step 4: 运行最终前端验证**

Run: `pnpm typecheck; pnpm lint; pnpm test; pnpm build`

Expected: 四条命令全部通过。

- [ ] **Step 5: 浏览器验收与工作区检查**

Run: 使用 Browser 打开本地前端；Browser 不可用时记录原因后使用 Playwright。

Expected: 在桌面和约 390px 宽度下，验证 UUID 错误提示、保存后导航、最近 Trip 预填、工作区对话加载以及“切换旅行”入口；无控制台错误、无水平溢出。随后运行 `git diff --check` 与 `git status --short`，不提交任何内容。

## Self-Review

- Spec coverage：Task 1 实现本地存储、损坏回退、最近记录和用户过滤；Task 2 实现入口、守卫与切换入口；Task 3 替换所有既有用户来源并仅在 bootstrap 成功后记录；Task 4 覆盖端到端交互和验证。
- Placeholder scan：本计划未保留未定义占位项、延后实现或没有具体测试的步骤。
- Type consistency：`useLocalSessionStore`、`userId`、`tripId`、`recentTrips`、`saveSession`、`recordSuccessfulTrip` 在所有任务中保持一致；路由名固定为 `session-entry`。
