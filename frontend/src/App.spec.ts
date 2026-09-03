import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'

import App from './App.vue'
import { router } from './router'
import { useLocalSessionStore } from './stores/local-session'

afterEach(() => {
  vi.unstubAllGlobals()
})

it('renders the trip workspace with the current itinerary sidebar', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/health')) {
        return new Response(JSON.stringify({ status: 'ok' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      if (url.includes('/conversations?')) {
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
            ],
            next_before_id: null,
            has_more: false,
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        )
      }
      return new Response(
        JSON.stringify({
          trip_id: '22222222-2222-4222-8222-222222222222',
          conversations: {
            items: [
              {
                id: 101,
                role: 'user',
                content: '帮我规划 **广州** 三日游',
                created_at: '2026-09-02T10:20:30+08:00',
                exchange_id: null,
              },
              {
                id: 102,
                role: 'assistant',
                content: '**自然景观**\n\n- 公园\n- 博物馆\n\n<script>window.unsafe = true</script>',
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
    }),
  )

  const pinia = createPinia()
  setActivePinia(pinia)
  useLocalSessionStore().saveSession(
    '00000000-0000-4000-8000-000000000001',
    '22222222-2222-4222-8222-222222222222',
  )
  await router.push('/trips/22222222-2222-4222-8222-222222222222')
  await router.isReady()

  const wrapper = mount(App, {
    global: {
      plugins: [pinia, router],
    },
  })
  await vi.waitFor(() => {
    expect(wrapper.get('[data-testid="service-status"]').text()).toContain('服务正常')
  })

  expect(wrapper.text()).toContain('旅程助手')
  expect(wrapper.text()).toContain('旅行工作区')
  expect(wrapper.text()).toContain('对话')
  expect(wrapper.text()).toContain('行程')
  expect(wrapper.text()).toContain('22222222…2222')
  expect(wrapper.get('[data-testid="current-itinerary-panel"]').isVisible()).toBe(true)
  await vi.waitFor(() => {
    expect(wrapper.text()).toContain('帮我规划 **广州** 三日游')
  })
  expect(wrapper.text()).toContain('自然景观')
  expect(wrapper.findAll('.conversation-message--assistant strong')).toHaveLength(1)
  expect(wrapper.findAll('.conversation-message--assistant li')).toHaveLength(2)
  expect(wrapper.findAll('.conversation-message--assistant script')).toHaveLength(0)
  expect(wrapper.findAll('.conversation-message--user strong')).toHaveLength(0)
  expect(wrapper.findAll('.conversation-scroll-area .message-composer')).toHaveLength(0)
  expect(wrapper.findAll('.conversation-content > .message-composer')).toHaveLength(1)

  const loadEarlierButton = wrapper
    .findAll('button')
    .find((button) => button.text().includes('加载更早消息'))
  expect(loadEarlierButton).toBeDefined()
  await loadEarlierButton!.trigger('click')
  await vi.waitFor(() => {
    expect(wrapper.text()).toContain('我更喜欢自然景观。')
  })
  expect(wrapper.text().indexOf('我更喜欢自然景观。')).toBeLessThan(
    wrapper.text().indexOf('帮我规划 **广州** 三日游'),
  )

  expect(wrapper.get('[data-testid="current-itinerary-panel"]').text()).toContain('第 1 天：沙面')
  expect(wrapper.findAll('[role="tab"]')).toHaveLength(0)
})
