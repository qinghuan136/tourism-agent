/** 消息接口的稳定错误码，避免 Store 依赖后端原始文案。 */
export type MessageRequestErrorCode =
  | 'network'
  | 'trip-not-found'
  | 'trip-running'
  | 'run-cancelled'
  | 'idempotency-conflict'
  | 'candidate-confirmation-invalid'
  | 'server-failed'
  | 'invalid-request'
  | 'protocol'

export type RunRecoveryAction =
  | 'retry-same'
  | 'retry-new'
  | 'cancel'
  | 'restore-candidate'
  | 'leave-trip'

export interface RunIssue {
  code: MessageRequestErrorCode
  message: string
  actions: RunRecoveryAction[]
}

export class MessageRequestError extends Error {
  constructor(
    public readonly code: MessageRequestErrorCode,
    message: string,
  ) {
    super(message)
    this.name = 'MessageRequestError'
  }
}

/** 将非流式 HTTP 错误响应映射为可预测的前端错误。 */
export function toMessageRequestError(status: number, body: unknown): MessageRequestError {
  const detail = readDetail(body)
  if (status === 404) {
    return new MessageRequestError('trip-not-found', '未找到当前用户对应的旅行，请返回旅行选择')
  }
  if (status === 409 && detail.includes('当前旅行正在处理中')) {
    return new MessageRequestError('trip-running', '当前旅行正在处理中，请先取消当前任务')
  }
  if (status === 409 && detail.includes('当前运行已取消')) {
    return new MessageRequestError('run-cancelled', '当前运行已取消，请使用新请求重试')
  }
  if (status === 409 && detail.includes('idempotency_id')) {
    return new MessageRequestError('idempotency-conflict', '请求标识与内容冲突，请使用新请求重试')
  }
  if (status === 422 && detail.includes('候选方案确认只接受')) {
    return new MessageRequestError(
      'candidate-confirmation-invalid',
      '候选方案确认无效，请重新选择是否采用',
    )
  }
  if (status >= 500) {
    return new MessageRequestError('server-failed', '消息处理失败，请使用新请求重试')
  }
  return new MessageRequestError('invalid-request', '请求参数无效，请检查后重试')
}

/** fetch 未取得响应时，原请求是否已经送达未知，只能保留原键重试。 */
export function toNetworkRequestError(): MessageRequestError {
  return new MessageRequestError('network', '网络连接已中断，请使用原请求重试或取消当前任务')
}

/** 协议或流读取中断与网络异常采用同一恢复策略。 */
export function toProtocolRequestError(message: string): MessageRequestError {
  return new MessageRequestError('protocol', message)
}

export function toRunIssue(error: unknown): RunIssue {
  if (!(error instanceof MessageRequestError)) {
    return {
      code: 'network',
      message: '网络连接已中断，请使用原请求重试或取消当前任务',
      actions: ['retry-same', 'cancel'],
    }
  }

  const actionsByCode: Record<MessageRequestErrorCode, RunRecoveryAction[]> = {
    network: ['retry-same', 'cancel'],
    protocol: ['retry-same', 'cancel'],
    'trip-not-found': ['leave-trip'],
    'trip-running': ['cancel'],
    'run-cancelled': ['retry-new', 'leave-trip'],
    'idempotency-conflict': ['retry-new'],
    'candidate-confirmation-invalid': ['restore-candidate'],
    'server-failed': ['retry-new'],
    'invalid-request': [],
  }
  return { code: error.code, message: error.message, actions: actionsByCode[error.code] }
}

function readDetail(body: unknown): string {
  if (typeof body === 'object' && body !== null && 'detail' in body) {
    const detail = body.detail
    if (typeof detail === 'string') return detail
  }
  return ''
}
