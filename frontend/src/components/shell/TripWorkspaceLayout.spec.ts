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
      runIssue: null,
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
      runIssue: null,
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
  expect(wrapper.findAll('button[aria-label="取消当前任务"]')).toHaveLength(1)
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
      runIssue: null,
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

it('emphasizes the running request feedback', () => {
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
      runFeedback: '正在处理你的请求，请稍候…',
      runIssue: null,
      candidateItinerary: null,
      liveMessages: [],
      taskResults: [],
      isCancelling: false,
    },
  })

  expect(wrapper.get('[data-testid="run-status"]').text()).toContain('正在处理你的请求，请稍候…')
})

it('allows collapsing temporary content without removing candidate confirmation actions', async () => {
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
      runIssue: null,
      candidateItinerary: '第 1 天：沙面',
      liveMessages: [],
      taskResults: [],
      isCancelling: false,
    },
  })

  const temporaryPanel = wrapper.get('[data-testid="temporary-output-panel"]')
  expect(temporaryPanel.text()).toContain('第 1 天：沙面')
  expect(temporaryPanel.findAll('[data-testid="temporary-output-content"]')).toHaveLength(1)

  await temporaryPanel.get('button[aria-label="收起本轮临时内容"]').trigger('click')

  expect(wrapper.findAll('[data-testid="temporary-output-panel"]')).toHaveLength(1)
  expect(wrapper.findAll('[data-testid="temporary-output-content"]')).toHaveLength(0)
  expect(temporaryPanel.findAll('button[aria-label="展开本轮临时内容"]')).toHaveLength(1)
  expect(wrapper.findAll('[data-testid="conversation-candidate-actions"]')).toHaveLength(1)

  await wrapper.setProps({
    taskResults: [
      {
        taskId: 'task_1',
        module: 'explore',
        status: 'success',
        result: '陈家祠适合安排在第一天下午。',
      },
    ],
  })

  expect(wrapper.get('[data-testid="temporary-output-content"]').text()).toContain('探索结果')
})

it('forwards same-request retry from the run control', async () => {
  const wrapper = mount(TripWorkspaceLayout, {
    props: {
      status: 'ready',
      historyStatus: 'idle',
      bootstrap: {
        tripId: '22222222-2222-4222-8222-222222222222',
        conversations: { items: [], nextBeforeId: null, hasMore: false },
        currentItinerary: null,
      },
      runStatus: 'interrupted',
      runFeedback: null,
      runIssue: {
        code: 'network',
        message: '网络连接已中断，请使用原请求重试或取消当前任务',
        actions: ['retry-same', 'cancel'],
      },
      candidateItinerary: null,
      liveMessages: [],
      taskResults: [],
      isCancelling: false,
    },
  })

  await wrapper.get('button[aria-label="使用原请求重试"]').trigger('click')

  expect(wrapper.emitted('retrySame')).toHaveLength(1)
})
