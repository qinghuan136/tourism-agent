import { createRouter, createWebHistory } from 'vue-router'

import TripWorkspaceView from '@/views/TripWorkspaceView.vue'
import SessionEntryView from '@/views/SessionEntryView.vue'
import { useLocalSessionStore } from '@/stores/local-session'

export const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/',
      name: 'session-entry',
      component: SessionEntryView,
    },
    {
      path: '/trips/:tripId',
      name: 'trip-workspace',
      component: TripWorkspaceView,
      beforeEnter: (to) => {
        if (useLocalSessionStore().userId) return true
        return { name: 'session-entry', query: { redirect: to.fullPath } }
      },
    },
  ],
})
