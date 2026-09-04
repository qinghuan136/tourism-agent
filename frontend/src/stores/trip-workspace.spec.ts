import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useTripWorkspaceStore } from './trip-workspace'
import { useLocalSessionStore } from './local-session'

const tripId = '22222222-2222-4222-8222-222222222222'
const userId = '11111111-1111-4111-8111-111111111111'

describe('trip workspace store', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    useLocalSessionStore().saveSession(userId, tripId)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('keeps the bootstrap response as the Trip workspace source of truth', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            trip_id: tripId,
            conversations: {
              items: [
                {
                  id: 101,
                  role: 'user',
                  content: '帮我规划广州三日游',
                  created_at: '2026-09-02T10:20:30+08:00',
                  exchange_id: null,
                },
              ],
              next_before_id: 101,
              has_more: true,
            },
            current_itinerary: '第 1 天：沙面',
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    )

    const session = useLocalSessionStore()
    const store = useTripWorkspaceStore()
    await store.load(tripId)

    expect(store.status).toBe('ready')
    expect(store.bootstrap?.tripId).toBe(tripId)
    expect(store.bootstrap?.conversations.hasMore).toBe(true)
    expect(store.bootstrap?.currentItinerary).toBe('第 1 天：沙面')
    expect(fetch).toHaveBeenCalledWith(
      `/api/trips/${tripId}/bootstrap?user_id=${userId}&message_limit=30`,
      { headers: { Accept: 'application/json' } },
    )
    expect(session.recentTrips[0]).toEqual({ userId, tripId })
  })

  it('exposes a missing Trip as a distinct renderable state', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, { status: 404 })))

    const store = useTripWorkspaceStore()
    await store.load(tripId)

    expect(store.status).toBe('not-found')
    expect(store.bootstrap).toBeNull()
  })

  it('prepends an older page in ID order and does not duplicate an existing message', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
        if (String(input).includes('/bootstrap?')) {
          return new Response(
            JSON.stringify({
              trip_id: tripId,
              conversations: {
                items: [
                  {
                    id: 101,
                    role: 'user',
                    content: '帮我规划广州三日游',
                    created_at: '2026-09-02T10:20:30+08:00',
                    exchange_id: null,
                  },
                  {
                    id: 102,
                    role: 'assistant',
                    content: '你更偏好自然景观还是城市文化？',
                    created_at: '2026-09-02T10:20:35+08:00',
                    exchange_id: null,
                  },
                ],
                next_before_id: 101,
                has_more: true,
              },
              current_itinerary: '第 1 天：沙面',
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } },
          )
        }
        return new Response(
          JSON.stringify({
            items: [
              {
                id: 99,
                role: 'user',
                content: '我更喜欢自然景观。',
                created_at: '2026-08-28T09:10:00+08:00',
                exchange_id: null,
              },
              {
                id: 100,
                role: 'assistant',
                content: '我会优先安排自然景点。',
                created_at: '2026-08-28T09:10:05+08:00',
                exchange_id: null,
              },
              {
                id: 101,
                role: 'user',
                content: '帮我规划广州三日游',
                created_at: '2026-09-02T10:20:30+08:00',
                exchange_id: null,
              },
            ],
            next_before_id: null,
            has_more: false,
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        )
      }),
    )

    const store = useTripWorkspaceStore()
    await store.load(tripId)
    await store.loadEarlier()

    expect(store.bootstrap?.conversations.items.map((item) => item.id)).toEqual([
      99, 100, 101, 102,
    ])
    expect(store.bootstrap?.conversations.hasMore).toBe(false)
    expect(store.bootstrap?.conversations.nextBeforeId).toBeNull()
    expect(store.bootstrap?.currentItinerary).toBe('第 1 天：沙面')
  })

  it('keeps a streamed answer and candidate itinerary in explicit waiting state', async () => {
    const idempotencyId = '33333333-3333-3333-3333-333333333333'
    vi.stubGlobal('crypto', { randomUUID: () => idempotencyId })
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValueOnce(
          new Response(
            JSON.stringify({
              trip_id: tripId,
              conversations: { items: [], next_before_id: null, has_more: false },
              current_itinerary: null,
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } },
          ),
        )
        .mockResolvedValueOnce(
          new Response(
            [
              `event: task.result\ndata: {"sequence":1,"idempotency_id":"${idempotencyId}","timestamp":"2026-09-02T12:00:00+00:00","task_id":"task_1","module":"explore","status":"success","result":"已筛选适合本次行程的地点。"}\n\n`,
              `event: interaction.required\ndata: {"sequence":2,"idempotency_id":"${idempotencyId}","timestamp":"2026-09-02T12:00:00+00:00","kind":"candidate_confirmation","question":"是否采用这份候选行程？","allowed_answers":["是","否"],"candidate_itinerary":"第 1 天：沙面"}\n\n`,
              `event: result\ndata: {"sequence":3,"idempotency_id":"${idempotencyId}","timestamp":"2026-09-02T12:00:01+00:00","route":"planning","message":"是否确认采用这份行程？","candidate_itinerary":"第 1 天：沙面","current_itinerary":null}\n\n`,
              `event: run.completed\ndata: {"sequence":4,"idempotency_id":"${idempotencyId}","timestamp":"2026-09-02T12:00:01+00:00","status":"waiting_user"}\n\n`,
            ].join(''),
            { headers: { 'Content-Type': 'text/event-stream' } },
          ),
        ),
    )

    const store = useTripWorkspaceStore()
    await store.load(tripId)
    await store.sendMessage('帮我规划广州三日游')

    expect(store.runStatus).toBe('waiting_user')
    expect(store.candidateItinerary).toBe('第 1 天：沙面')
    expect(store.taskResults).toEqual([
      {
        taskId: 'task_1',
        module: 'explore',
        status: 'success',
        result: '已筛选适合本次行程的地点。',
      },
    ])
    expect(store.liveMessages).toEqual([
      { role: 'user', content: '帮我规划广州三日游' },
      { role: 'assistant', content: '是否确认采用这份行程？' },
    ])
  })

  it('clears a waiting candidate after the user cancels the current run', async () => {
    const idempotencyId = '33333333-3333-3333-3333-333333333333'
    vi.stubGlobal('crypto', { randomUUID: () => idempotencyId })
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValueOnce(
          new Response(
            JSON.stringify({
              trip_id: tripId,
              conversations: { items: [], next_before_id: null, has_more: false },
              current_itinerary: null,
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } },
          ),
        )
        .mockResolvedValueOnce(
          new Response(
            [
              `event: interaction.required\ndata: {"sequence":1,"idempotency_id":"${idempotencyId}","timestamp":"2026-09-02T12:00:00+00:00","kind":"candidate_confirmation","question":"是否采用这份候选行程？","allowed_answers":["是","否"],"candidate_itinerary":"第 1 天：沙面"}\n\n`,
              `event: run.completed\ndata: {"sequence":2,"idempotency_id":"${idempotencyId}","timestamp":"2026-09-02T12:00:01+00:00","status":"waiting_user"}\n\n`,
            ].join(''),
            { headers: { 'Content-Type': 'text/event-stream' } },
          ),
        )
        .mockResolvedValueOnce(
          new Response(JSON.stringify({ cancelled: true }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }),
        ),
    )

    const store = useTripWorkspaceStore()
    await store.load(tripId)
    await store.sendMessage('帮我规划广州三日游')

    const cancelRun = (store as unknown as { cancelRun?: () => Promise<void> }).cancelRun
    expect(cancelRun).toBeTypeOf('function')
    await cancelRun!()

    expect(store.runStatus).toBe('cancelled')
    expect(store.candidateItinerary).toBeNull()
    expect(store.runFeedback).toBe('已取消当前任务')
  })

  it('reuses the original request after a network interruption without duplicating the message', async () => {
    const idempotencyId = '33333333-3333-3333-3333-333333333333'
    vi.stubGlobal('crypto', { randomUUID: () => idempotencyId })
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            trip_id: tripId,
            conversations: { items: [], next_before_id: null, has_more: false },
            current_itinerary: null,
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      )
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ idempotency_id: idempotencyId, status: 'processing' }),
          { status: 202, headers: { 'Content-Type': 'application/json' } },
        ),
      )
    vi.stubGlobal('fetch', fetchMock)

    const store = useTripWorkspaceStore()
    await store.load(tripId)
    await store.sendMessage('帮我规划广州三日游')

    expect(store.runStatus).toBe('interrupted')
    expect(store.runIssue?.actions).toEqual(['retry-same', 'cancel'])

    await store.retryPendingRequest('same')

    expect(fetchMock.mock.calls[1]?.[1]).toMatchObject({
      body: JSON.stringify({
        user_id: userId,
        trip_id: tripId,
        idempotency_id: idempotencyId,
        message: '帮我规划广州三日游',
      }),
    })
    expect(fetchMock.mock.calls[2]?.[1]).toMatchObject({
      body: JSON.stringify({
        user_id: userId,
        trip_id: tripId,
        idempotency_id: idempotencyId,
        message: '帮我规划广州三日游',
      }),
    })
    expect(store.liveMessages).toEqual([{ role: 'user', content: '帮我规划广州三日游' }])
    expect(store.runStatus).toBe('running')
  })

  it('generates a new idempotency key after a server failure without duplicating the message', async () => {
    const firstIdempotencyId = '33333333-3333-3333-3333-333333333333'
    const secondIdempotencyId = '44444444-4444-4444-8444-444444444444'
    vi.stubGlobal('crypto', {
      randomUUID: vi.fn().mockReturnValueOnce(firstIdempotencyId).mockReturnValueOnce(secondIdempotencyId),
    })
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            trip_id: tripId,
            conversations: { items: [], next_before_id: null, has_more: false },
            current_itinerary: null,
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: '消息处理失败，请稍后使用新的 idempotency_id 重试' }), {
          status: 500,
          headers: { 'Content-Type': 'application/json' },
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ idempotency_id: secondIdempotencyId, status: 'processing' }),
          { status: 202, headers: { 'Content-Type': 'application/json' } },
        ),
      )
    vi.stubGlobal('fetch', fetchMock)

    const store = useTripWorkspaceStore()
    await store.load(tripId)
    await store.sendMessage('帮我规划广州三日游')
    await store.retryPendingRequest('new')

    expect(store.runIssue).toBeNull()
    expect(fetchMock.mock.calls[2]?.[1]).toMatchObject({
      body: expect.stringContaining(secondIdempotencyId),
    })
    expect(store.liveMessages).toEqual([{ role: 'user', content: '帮我规划广州三日游' }])
  })

  it('restores candidate confirmation after the server rejects a confirmation reply', async () => {
    const firstIdempotencyId = '33333333-3333-3333-3333-333333333333'
    const confirmationIdempotencyId = '44444444-4444-4444-8444-444444444444'
    vi.stubGlobal('crypto', {
      randomUUID: vi.fn().mockReturnValueOnce(firstIdempotencyId).mockReturnValueOnce(confirmationIdempotencyId),
    })
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValueOnce(
          new Response(
            JSON.stringify({
              trip_id: tripId,
              conversations: { items: [], next_before_id: null, has_more: false },
              current_itinerary: null,
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } },
          ),
        )
        .mockResolvedValueOnce(
          new Response(
            [
              `event: interaction.required\ndata: {"sequence":1,"idempotency_id":"${firstIdempotencyId}","timestamp":"2026-09-02T12:00:00+00:00","kind":"candidate_confirmation","question":"是否采用这份候选行程？","allowed_answers":["是","否"],"candidate_itinerary":"第 1 天：沙面"}\n\n`,
              `event: run.completed\ndata: {"sequence":2,"idempotency_id":"${firstIdempotencyId}","timestamp":"2026-09-02T12:00:01+00:00","status":"waiting_user"}\n\n`,
            ].join(''),
            { headers: { 'Content-Type': 'text/event-stream' } },
          ),
        )
        .mockResolvedValueOnce(
          new Response(JSON.stringify({ detail: '候选方案确认只接受“是”或“否”' }), {
            status: 422,
            headers: { 'Content-Type': 'application/json' },
          }),
        ),
    )

    const store = useTripWorkspaceStore()
    await store.load(tripId)
    await store.sendMessage('帮我规划广州三日游')
    await store.confirmCandidate(true)

    expect(store.runStatus).toBe('waiting_user')
    expect(store.runIssue?.code).toBe('candidate-confirmation-invalid')
    expect(store.candidateItinerary).toBe('第 1 天：沙面')

    store.restoreCandidateConfirmation()

    expect(store.runIssue).toBeNull()
  })
})
