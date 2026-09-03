import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

const sessionStorageKey = 'tourism-agent.local-session.v1'
const recentTripsStorageKey = 'tourism-agent.recent-trips.v1'
const maxRecentTrips = 8
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i

export interface RecentTrip {
  userId: string
  tripId: string
}

interface StoredSession {
  userId: string
  tripId: string
}

export const useLocalSessionStore = defineStore('local-session', () => {
  const userId = ref<string | null>(null)
  const tripId = ref<string | null>(null)
  const allRecentTrips = ref<RecentTrip[]>([])
  const recentTrips = computed(() =>
    userId.value ? allRecentTrips.value.filter((trip) => trip.userId === userId.value) : [],
  )

  function restore(): void {
    const storedSession = readStorage(sessionStorageKey)
    const storedTrips = readStorage(recentTripsStorageKey)
    const parsedSession = isStoredSession(storedSession) ? storedSession : null

    userId.value = parsedSession?.userId ?? null
    tripId.value = parsedSession?.tripId ?? null
    allRecentTrips.value = Array.isArray(storedTrips)
      ? storedTrips.filter(isRecentTrip).slice(0, maxRecentTrips)
      : []
  }

  function saveSession(nextUserId: string, nextTripId: string): boolean {
    if (!isUuid(nextUserId) || !isUuid(nextTripId)) return false

    userId.value = nextUserId
    tripId.value = nextTripId
    writeStorage(sessionStorageKey, { userId: nextUserId, tripId: nextTripId })
    return true
  }

  function recordSuccessfulTrip(nextTripId: string): void {
    const currentUserId = userId.value
    if (!currentUserId || !isUuid(nextTripId)) return

    const nextTrip: RecentTrip = { userId: currentUserId, tripId: nextTripId }
    allRecentTrips.value = [
      nextTrip,
      ...allRecentTrips.value.filter(
        (trip) => trip.userId !== currentUserId || trip.tripId !== nextTripId,
      ),
    ].slice(0, maxRecentTrips)
    writeStorage(recentTripsStorageKey, allRecentTrips.value)
  }

  function clear(): void {
    userId.value = null
    tripId.value = null
    removeStorage(sessionStorageKey)
  }

  return { userId, tripId, recentTrips, restore, saveSession, recordSuccessfulTrip, clear }
})

export function isUuid(value: string): boolean {
  return uuidPattern.test(value)
}

function isStoredSession(value: unknown): value is StoredSession {
  return (
    isRecord(value) &&
    typeof value.userId === 'string' &&
    typeof value.tripId === 'string' &&
    isUuid(value.userId) &&
    isUuid(value.tripId)
  )
}

function isRecentTrip(value: unknown): value is RecentTrip {
  return (
    isRecord(value) &&
    typeof value.userId === 'string' &&
    typeof value.tripId === 'string' &&
    isUuid(value.userId) &&
    isUuid(value.tripId)
  )
}

function readStorage(key: string): unknown {
  try {
    const value = localStorage.getItem(key)
    return value ? JSON.parse(value) : null
  } catch {
    return null
  }
}

function writeStorage(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value))
  } catch {
    // 本地存储不可用时仅保留当前页面会话，不阻断用户进入工作区。
  }
}

function removeStorage(key: string): void {
  try {
    localStorage.removeItem(key)
  } catch {
    // 私有模式等环境可能禁用存储，内存状态已清空即可。
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}
