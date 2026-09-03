import { mount } from '@vue/test-utils'
import { expect, it } from 'vitest'

import TripWorkspaceLayout from './TripWorkspaceLayout.vue'

it('reserves the side panel for the current itinerary', () => {
  const wrapper = mount(TripWorkspaceLayout, {
    props: {
      status: 'ready',
      historyStatus: 'idle',
      bootstrap: {
        tripId: '22222222-2222-4222-8222-222222222222',
        conversations: { items: [], nextBeforeId: null, hasMore: false },
        currentItinerary: '第 1 天：广州塔',
      },
      runStatus: 'waiting_user',
      runFeedback: null,
      runError: null,
      candidateItinerary: '第 1 天：沙面',
      liveMessages: [],
      taskResults: [],
      isCancelling: false,
    },
  })

  const sidePanel = wrapper.get('[data-testid="current-itinerary-panel"]')
  expect(sidePanel.text()).toContain('第 1 天：广州塔')
  expect(wrapper.findAll('[role="tab"]')).toHaveLength(0)
})

it('shows candidate confirmation buttons in the conversation panel while waiting', async () => {
  const wrapper = mount(TripWorkspaceLayout, {
    props: {
      status: 'ready',
      historyStatus: 'idle',
      bootstrap: {
        tripId: '22222222-2222-4222-8222-222222222222',
        conversations: { items: [], nextBeforeId: null, hasMore: false },
        currentItinerary: null,
      },
      runStatus: 'waiting_user',
      runFeedback: '是否确认采用这份行程？',
      runError: null,
      candidateItinerary: '第 1 天：沙面',
      liveMessages: [{ role: 'assistant', content: '是否确认采用这份行程？' }],
      taskResults: [],
      isCancelling: false,
    },
  })

  const actions = wrapper.get('[data-testid="conversation-candidate-actions"]')
  expect(actions.text()).toContain('确认采用')
  expect(actions.text()).toContain('不采用')
  expect(wrapper.get('#message-input').attributes('disabled')).toBeDefined()
  expect(wrapper.findAll('.cancel-run-button')).toHaveLength(1)
  expect(wrapper.findAll('[data-testid="candidate-panel"] .candidate-actions')).toHaveLength(0)

  await actions.get('.candidate-actions__accept').trigger('click')
  await actions.get('.candidate-actions__decline').trigger('click')

  expect(wrapper.emitted('confirmCandidate')).toEqual([[true], [false]])
})

it('shows completed task results outside the conversation messages', () => {
  const wrapper = mount(TripWorkspaceLayout, {
    props: {
      status: 'ready',
      historyStatus: 'idle',
      bootstrap: {
        tripId: '22222222-2222-4222-8222-222222222222',
        conversations: { items: [], nextBeforeId: null, hasMore: false },
        currentItinerary: null,
      },
      runStatus: 'running',
      runFeedback: null,
      runError: null,
      candidateItinerary: null,
      liveMessages: [],
      taskResults: [
        {
          taskId: 'task_1',
          module: 'explore',
          status: 'success',
          result: '陈家祠适合安排在第一天下午。',
        },
      ],
      isCancelling: false,
    },
  })

  const taskResults = wrapper.get('[data-testid="temporary-output-panel"]')
  expect(taskResults.text()).toContain('探索结果')
  expect(taskResults.text()).toContain('陈家祠适合安排在第一天下午。')
  expect(wrapper.findAll('.conversation-message')).toHaveLength(0)
})

it('allows closing temporary content without removing candidate confirmation actions', async () => {
  const wrapper = mount(TripWorkspaceLayout, {
    props: {
      status: 'ready',
      historyStatus: 'idle',
      bootstrap: {
        tripId: '22222222-2222-4222-8222-222222222222',
        conversations: { items: [], nextBeforeId: null, hasMore: false },
        currentItinerary: null,
      },
      runStatus: 'waiting_user',
      runFeedback: '是否确认采用这份行程？',
      runError: null,
      candidateItinerary: '第 1 天：沙面',
      liveMessages: [],
      taskResults: [],
      isCancelling: false,
    },
  })

  const temporaryPanel = wrapper.get('[data-testid="temporary-output-panel"]')
  expect(temporaryPanel.text()).toContain('第 1 天：沙面')

  await temporaryPanel.get('button[aria-label="关闭本轮临时内容"]').trigger('click')

  expect(wrapper.findAll('[data-testid="temporary-output-panel"]')).toHaveLength(0)
  expect(wrapper.findAll('[data-testid="conversation-candidate-actions"]')).toHaveLength(1)
})
