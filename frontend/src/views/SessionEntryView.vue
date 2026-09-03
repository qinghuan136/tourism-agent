<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { isUuid, useLocalSessionStore } from '@/stores/local-session'

const router = useRouter()
const route = useRoute()
const session = useLocalSessionStore()

const userId = ref(session.userId ?? '')
const tripId = ref(session.tripId ?? '')
const errorMessage = ref<string | null>(null)
const recentTrips = computed(() =>
  userId.value.trim().toLowerCase() === session.userId?.toLowerCase() ? session.recentTrips : [],
)

function submit(): void {
  const normalizedUserId = userId.value.trim()
  const normalizedTripId = tripId.value.trim()
  if (!isUuid(normalizedUserId) || !isUuid(normalizedTripId)) {
    errorMessage.value = '请输入有效的 user_id 和 trip_id（UUID 格式）'
    return
  }
  session.saveSession(normalizedUserId, normalizedTripId)
  const redirect = route.query.redirect
  const destination = typeof redirect === 'string' && redirect.startsWith('/')
    ? redirect
    : `/trips/${normalizedTripId}`
  void router.push(destination)
}

function chooseRecentTrip(selectedTripId: string): void {
  tripId.value = selectedTripId
  errorMessage.value = null
}
</script>

<template>
  <main class="session-entry">
    <section
      class="session-entry__panel"
      aria-labelledby="session-entry-title"
    >
      <p class="session-entry__eyebrow">
        TRIP WORKSPACE
      </p>
      <h1 id="session-entry-title">
        进入旅行工作区
      </h1>
      <p class="session-entry__description">
        当前阶段使用本地保存的用户与旅行标识，不会发起登录请求。
      </p>

      <form @submit.prevent="submit">
        <label for="user-id">user_id</label>
        <input
          id="user-id"
          v-model="userId"
          autocomplete="off"
          placeholder="请输入用户 UUID"
          @input="errorMessage = null"
        >

        <label for="trip-id">trip_id</label>
        <input
          id="trip-id"
          v-model="tripId"
          autocomplete="off"
          placeholder="请输入旅行 UUID"
          @input="errorMessage = null"
        >

        <p
          v-if="errorMessage"
          class="session-entry__error"
          role="alert"
        >
          {{ errorMessage }}
        </p>
        <button type="submit">
          进入工作区
        </button>
      </form>

      <section
        v-if="recentTrips.length"
        class="session-entry__recent"
        aria-labelledby="recent-trips-title"
      >
        <h2 id="recent-trips-title">
          最近打开的旅行
        </h2>
        <ul>
          <li
            v-for="recentTrip in recentTrips"
            :key="recentTrip.tripId"
          >
            <button
              type="button"
              @click="chooseRecentTrip(recentTrip.tripId)"
            >
              {{ recentTrip.tripId }}
            </button>
          </li>
        </ul>
      </section>
    </section>
  </main>
</template>
