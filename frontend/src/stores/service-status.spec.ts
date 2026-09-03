import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useServiceStatusStore } from './service-status'

describe('service status store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('marks the API available after a successful health check', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ status: 'ok' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const store = useServiceStatusStore()
    expect(store.status).toBe('checking')

    await store.check()

    expect(fetchMock).toHaveBeenCalledWith('/api/health', {
      headers: { Accept: 'application/json' },
    })
    expect(store.status).toBe('available')
  })

  it.each([
    ['an HTTP error', vi.fn().mockResolvedValue(new Response(null, { status: 503 }))],
    ['a network error', vi.fn().mockRejectedValue(new TypeError('network failed'))],
  ])('marks the API unavailable after %s', async (_caseName, fetchMock) => {
    vi.stubGlobal('fetch', fetchMock)

    const store = useServiceStatusStore()
    await store.check()

    expect(store.status).toBe('unavailable')
  })
})
