import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { router } from '@/router'
import { useLocalSessionStore } from '@/stores/local-session'

import SessionEntryView from './SessionEntryView.vue'

const userId = '11111111-1111-4111-8111-111111111111'
const tripId = '22222222-2222-4222-8222-222222222222'

describe('session entry view', () => {
  beforeEach(async () => {
    localStorage.clear()
    setActivePinia(createPinia())
    await router.push('/')
  })

  it('saves both UUIDs and opens the selected trip', async () => {
    const wrapper = mount(SessionEntryView, { global: { plugins: [router] } })

    await wrapper.get('#user-id').setValue(userId)
    await wrapper.get('#trip-id').setValue(tripId)
    await wrapper.get('form').trigger('submit')

    await vi.waitFor(() => {
      expect(useLocalSessionStore().userId).toBe(userId)
      expect(router.currentRoute.value.fullPath).toBe(`/trips/${tripId}`)
    })
  })

  it('redirects a workspace route to the entry when no user is saved', async () => {
    await router.push(`/trips/${tripId}`)

    expect(router.currentRoute.value.name).toBe('session-entry')
    expect(router.currentRoute.value.query.redirect).toBe(`/trips/${tripId}`)
  })
})
