import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'

import { useLocalSessionStore } from './local-session'

const userId = '11111111-1111-4111-8111-111111111111'
const firstTripId = '22222222-2222-4222-8222-222222222222'
const secondTripId = '33333333-3333-4333-8333-333333333333'

describe('local session store', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
  })

  it('persists the selected user and moves a successfully opened trip to the top', () => {
    const store = useLocalSessionStore()

    store.saveSession(userId, firstTripId)
    store.recordSuccessfulTrip(secondTripId)
    store.recordSuccessfulTrip(firstTripId)

    expect(store.userId).toBe(userId)
    expect(store.tripId).toBe(firstTripId)
    expect(store.recentTrips).toEqual([
      { userId, tripId: firstTripId },
      { userId, tripId: secondTripId },
    ])
    expect(localStorage.getItem('tourism-agent.local-session.v1')).toContain(userId)
  })

  it('recovers to an empty session when saved values are malformed', () => {
    localStorage.setItem('tourism-agent.local-session.v1', '{invalid')
    const store = useLocalSessionStore()

    store.restore()

    expect(store.userId).toBeNull()
    expect(store.tripId).toBeNull()
    expect(store.recentTrips).toEqual([])
  })
})
