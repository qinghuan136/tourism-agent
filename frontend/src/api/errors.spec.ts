import { describe, expect, it } from 'vitest'

import { toMessageRequestError, toNetworkRequestError, toRunIssue } from './errors'

describe('消息请求错误映射', () => {
  it.each([
    [404, '未找到当前用户对应的旅行', 'trip-not-found', ['leave-trip']],
    [409, '当前旅行正在处理中，请先取消', 'trip-running', ['cancel']],
    [409, '当前运行已取消', 'run-cancelled', ['retry-new', 'leave-trip']],
    [409, 'idempotency_id 已用于不同的请求内容', 'idempotency-conflict', ['retry-new']],
    [422, '候选方案确认只接受“是”或“否”', 'candidate-confirmation-invalid', ['restore-candidate']],
    [500, '消息处理失败，请稍后使用新的 idempotency_id 重试', 'server-failed', ['retry-new']],
  ] as const)(
    '将 HTTP %s 的 %s 映射为可预测恢复操作',
    (status, detail, code, actions) => {
      const issue = toRunIssue(toMessageRequestError(status, { detail }))

      expect(issue.code).toBe(code)
      expect(issue.actions).toEqual(actions)
    },
  )

  it('将网络中断映射为使用原请求重试和取消', () => {
    expect(toRunIssue(toNetworkRequestError()).actions).toEqual(['retry-same', 'cancel'])
  })
})
