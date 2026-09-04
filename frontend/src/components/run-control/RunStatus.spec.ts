import { mount } from '@vue/test-utils'
import { expect, it } from 'vitest'

import RunStatus from './RunStatus.vue'

it('shows only the recovery actions allowed for a network interruption', async () => {
  const wrapper = mount(RunStatus, {
    props: {
      feedback: null,
      issue: {
        code: 'network',
        message: '网络连接已中断，请使用原请求重试或取消当前任务',
        actions: ['retry-same', 'cancel'],
      },
      canCancel: true,
      isCancelling: false,
    },
  })

  expect(wrapper.get('[data-testid="run-status"]').text()).toContain('网络连接已中断')
  expect(wrapper.findAll('button')).toHaveLength(2)

  await wrapper.get('button[aria-label="使用原请求重试"]').trigger('click')
  await wrapper.get('button[aria-label="取消当前任务"]').trigger('click')

  expect(wrapper.emitted('retrySame')).toHaveLength(1)
  expect(wrapper.emitted('cancel')).toHaveLength(1)
})

it('offers return to trip selection for a missing Trip', () => {
  const wrapper = mount(RunStatus, {
    props: {
      feedback: null,
      issue: {
        code: 'trip-not-found',
        message: '未找到当前用户对应的旅行，请返回旅行选择',
        actions: ['leave-trip'],
      },
      canCancel: false,
      isCancelling: false,
    },
  })

  expect(wrapper.get('button[aria-label="返回旅行选择"]')).toBeTruthy()
  expect(wrapper.findAll('button[aria-label="取消当前任务"]')).toHaveLength(0)
})
