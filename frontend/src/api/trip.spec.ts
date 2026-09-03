import { afterEach, describe, expect, it, vi } from 'vitest'

import * as tripApi from './trip'
import { loadEarlierConversations, loadTripBootstrap } from './trip'

const tripId = '22222222-2222-4222-8222-222222222222'
const userId = '00000000-0000-4000-8000-000000000001'

describe('loadTripBootstrap', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('requests and parses the first Trip page with its configured user', async () => {
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
                  exchange_id: '33333333-3333-3333-3333-333333333333',
                },
                {
                  id: 102,
                  role: 'assistant',
                  content: '你更偏好自然景观还是城市文化？',
                  created_at: '2026-09-02T10:20:35+08:00',
                  exchange_id: '33333333-3333-3333-3333-333333333333',
                },
              ],
              next_before_id: null,
              has_more: false,
            },
            current_itinerary: '第 1 天：沙面\n第 2 天：广州塔',
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    )

    const bootstrap = await loadTripBootstrap(tripId, userId)

    expect(fetch).toHaveBeenCalledWith(
      `/api/trips/${tripId}/bootstrap?user_id=${userId}&message_limit=30`,
      { headers: { Accept: 'application/json' } },
    )
    expect(bootstrap.conversations.items).toHaveLength(2)
    expect(bootstrap.conversations.items[1]).toMatchObject({
      role: 'assistant',
      content: '你更偏好自然景观还是城市文化？',
    })
    expect(bootstrap.currentItinerary).toBe('第 1 天：沙面\n第 2 天：广州塔')
  })

  it('maps a missing Trip to a stable not-found error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, { status: 404 })))

    await expect(loadTripBootstrap(tripId, userId)).rejects.toMatchObject({
      kind: 'not-found',
    })
  })
})

describe('loadEarlierConversations', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('requests the page before the supplied cursor and parses it in ascending order', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            items: [
              {
                id: 71,
                role: 'user',
                content: '我更喜欢自然景观。',
                created_at: '2026-08-28T09:10:00+08:00',
                exchange_id: null,
              },
              {
                id: 72,
                role: 'assistant',
                content: '我会优先安排自然景点。',
                created_at: '2026-08-28T09:10:05+08:00',
                exchange_id: null,
              },
            ],
            next_before_id: null,
            has_more: false,
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    )

    const page = await loadEarlierConversations(tripId, userId, 101)

    expect(fetch).toHaveBeenCalledWith(
      `/api/trips/${tripId}/conversations?user_id=${userId}&before_id=101&limit=30`,
      { headers: { Accept: 'application/json' } },
    )
    expect(page.items.map((item) => item.id)).toEqual([71, 72])
    expect(page.hasMore).toBe(false)
    expect(page.nextBeforeId).toBeNull()
  })
})

describe('cancelTripRun', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('posts the selected user and returns whether a current run was cancelled', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ cancelled: true }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )

    const cancelTripRun = (tripApi as unknown as {
      cancelTripRun?: (tripId: string, userId: string) => Promise<boolean>
    }).cancelTripRun

    expect(cancelTripRun).toBeTypeOf('function')
    expect(await cancelTripRun!(tripId, userId)).toBe(true)
    expect(fetch).toHaveBeenCalledWith(`/api/trips/${tripId}/cancel`, {
      method: 'POST',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: userId }),
    })
  })
})
