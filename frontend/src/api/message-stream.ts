import { runtimeConfig } from '@/config/runtime'
import { toMessageRequestError, toNetworkRequestError, toProtocolRequestError } from '@/api/errors'

export interface StreamMessageRequest {
  userId: string
  tripId: string
  idempotencyId: string
  message: string
}

export interface MessageResult {
  route: 'planning' | 'explore' | 'research' | 'helper'
  message: string
  candidateItinerary: string | null
  currentItinerary: string | null
}

export type TaskResultStatus = 'success' | 'partial' | 'failed'

export interface StreamedTaskResult {
  taskId: string
  module: MessageResult['route']
  status: TaskResultStatus
  result: string
}

interface StreamEventBase {
  sequence: number
  idempotencyId: string
  timestamp: string
}

export type MessageStreamEvent =
  | (StreamEventBase & { type: 'run.started'; message: string })
  | (StreamEventBase & {
      type: 'task.started' | 'task.completed'
      taskId: string
      module: MessageResult['route']
      message: string
    })
  | (StreamEventBase & StreamedTaskResult & { type: 'task.result' })
  | (StreamEventBase & {
      type: 'operation.started' | 'operation.completed' | 'operation.failed'
      operationId: string
      tool: string
      message: string
    })
  | (StreamEventBase & { type: 'token.delta'; text: string })
  | (StreamEventBase & {
      type: 'interaction.required'
      kind: 'ask_user' | 'candidate_confirmation'
      question: string
      allowedAnswers: ['是', '否'] | null
      candidateItinerary: string | null
    })
  | (StreamEventBase & { type: 'result'; result: MessageResult })
  | (StreamEventBase & { type: 'error'; code: string; message: string; retryable: boolean })
  | (StreamEventBase & { type: 'run.cancelled'; message: string })
  | (StreamEventBase & { type: 'run.completed'; status: StreamRunStatus })

export type StreamRunStatus = 'completed' | 'waiting_user' | 'cancelled' | 'failed'

export type StreamMessageOutcome =
  | { kind: 'stream'; status: StreamRunStatus }
  | { kind: 'processing' }
  | { kind: 'replay'; result: MessageResult }

export interface SendStreamedMessageOptions {
  signal?: AbortSignal
  onEvent?: (event: MessageStreamEvent) => void
}

/** 发送一条消息，并将服务端 SSE 事件转换为稳定的前端类型。 */
export async function sendStreamedMessage(
  request: StreamMessageRequest,
  options: SendStreamedMessageOptions = {},
): Promise<StreamMessageOutcome> {
  let response: Response
  try {
    response = await fetch(`${runtimeConfig.apiBaseUrl}/messages/stream`, {
      method: 'POST',
      headers: {
        Accept: 'text/event-stream, application/json',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        user_id: request.userId,
        trip_id: request.tripId,
        idempotency_id: request.idempotencyId,
        message: request.message,
      }),
      signal: options.signal,
    })
  } catch {
    throw toNetworkRequestError()
  }

  const contentType = response.headers.get('content-type') ?? ''
  if (!contentType.includes('text/event-stream')) {
    return parseNonStreamResponse(response, request.idempotencyId)
  }
  if (!response.ok || !response.body) {
    if (!response.ok) throw toMessageRequestError(response.status, null)
    throw toProtocolRequestError('流式响应不可用，请使用原请求重试或取消当前任务')
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let completionStatus: StreamRunStatus | null = null

  while (true) {
    const { done, value } = await reader.read()
    buffer += decoder.decode(value, { stream: !done })
    const frames = splitSseFrames(buffer)
    buffer = frames.remainder

    for (const frame of frames.complete) {
      const event = parseSseFrame(frame, request.idempotencyId)
      if (!event) continue
      options.onEvent?.(event)
      if (event.type === 'run.completed') completionStatus = event.status
    }

    if (done) break
  }

  if (completionStatus === null) {
    throw toProtocolRequestError('流式响应在结束前断开，请使用原请求重试或取消当前任务')
  }
  return { kind: 'stream', status: completionStatus }
}

async function parseNonStreamResponse(
  response: Response,
  idempotencyId: string,
): Promise<StreamMessageOutcome> {
  const body: unknown = await readJson(response)
  if (response.status === 202 && isProcessingResponse(body, idempotencyId)) {
    return { kind: 'processing' }
  }
  if (response.ok && isMessageResultResponse(body)) {
    return { kind: 'replay', result: toMessageResult(body) }
  }
  throw toMessageRequestError(response.status, body)
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json()
  } catch {
    return null
  }
}

function splitSseFrames(buffer: string): { complete: string[]; remainder: string } {
  const normalized = buffer.replaceAll('\r\n', '\n')
  const complete = normalized.split('\n\n')
  return { complete: complete.slice(0, -1), remainder: complete.at(-1) ?? '' }
}

function parseSseFrame(frame: string, expectedIdempotencyId: string): MessageStreamEvent | null {
  const fields = frame.split('\n').reduce<Record<string, string[]>>((result, line) => {
    if (line.startsWith(':')) return result
    const separator = line.indexOf(':')
    if (separator < 0) return result
    const name = line.slice(0, separator)
    const value = line.slice(separator + 1).replace(/^ /, '')
    result[name] ??= []
    result[name].push(value)
    return result
  }, {})
  const eventType = fields.event?.at(-1)
  const encodedData = fields.data?.join('\n')
  if (!eventType || !encodedData) return null

  let data: unknown
  try {
    data = JSON.parse(encodedData)
  } catch {
    throw toProtocolRequestError('流式响应格式无效，请使用原请求重试或取消当前任务')
  }
  if (!isStreamEventBase(data) || data.idempotency_id !== expectedIdempotencyId) {
    throw toProtocolRequestError('流式响应与当前请求不匹配，请使用原请求重试或取消当前任务')
  }
  return toStreamEvent(eventType, data)
}

function toStreamEvent(type: string, data: StreamEventBaseResponse): MessageStreamEvent | null {
  const base: StreamEventBase = {
    sequence: data.sequence,
    idempotencyId: data.idempotency_id,
    timestamp: data.timestamp,
  }
  if (type === 'token.delta' && typeof data.text === 'string') return { ...base, type, text: data.text }
  if (type === 'run.started' && typeof data.message === 'string') return { ...base, type, message: data.message }
  if (
    (type === 'task.started' || type === 'task.completed') &&
    typeof data.task_id === 'string' &&
    isRoute(data.module) &&
    typeof data.message === 'string'
  ) {
    return { ...base, type, taskId: data.task_id, module: data.module, message: data.message }
  }
  if (
    type === 'task.result' &&
    typeof data.task_id === 'string' &&
    isRoute(data.module) &&
    isTaskResultStatus(data.status) &&
    typeof data.result === 'string'
  ) {
    return {
      ...base,
      type,
      taskId: data.task_id,
      module: data.module,
      status: data.status,
      result: data.result,
    }
  }
  if (
    (type === 'operation.started' || type === 'operation.completed' || type === 'operation.failed') &&
    typeof data.operation_id === 'string' &&
    typeof data.tool === 'string' &&
    typeof data.message === 'string'
  ) {
    return { ...base, type, operationId: data.operation_id, tool: data.tool, message: data.message }
  }
  if (
    type === 'interaction.required' &&
    (data.kind === 'ask_user' || data.kind === 'candidate_confirmation') &&
    typeof data.question === 'string' &&
    isAllowedAnswers(data.allowed_answers) &&
    isNullableString(data.candidate_itinerary)
  ) {
    return {
      ...base,
      type,
      kind: data.kind,
      question: data.question,
      allowedAnswers: data.allowed_answers,
      candidateItinerary: data.candidate_itinerary,
    }
  }
  if (type === 'result' && isMessageResultResponse(data)) {
    return { ...base, type, result: toMessageResult(data) }
  }
  if (
    type === 'error' &&
    typeof data.code === 'string' &&
    typeof data.message === 'string' &&
    typeof data.retryable === 'boolean'
  ) {
    return { ...base, type, code: data.code, message: data.message, retryable: data.retryable }
  }
  if (type === 'run.cancelled' && typeof data.message === 'string') {
    return { ...base, type, message: data.message }
  }
  if (type === 'run.completed' && isStreamRunStatus(data.status)) {
    return { ...base, type, status: data.status }
  }
  throw toProtocolRequestError('流式响应事件格式无效，请使用原请求重试或取消当前任务')
}

interface StreamEventBaseResponse extends Record<string, unknown> {
  sequence: number
  idempotency_id: string
  timestamp: string
}

interface MessageResultResponse extends Record<string, unknown> {
  route: MessageResult['route']
  message: string
  candidate_itinerary: string | null
  current_itinerary: string | null
}

function isStreamEventBase(value: unknown): value is StreamEventBaseResponse {
  return (
    isRecord(value) &&
    typeof value.sequence === 'number' &&
    typeof value.idempotency_id === 'string' &&
    typeof value.timestamp === 'string'
  )
}

function isMessageResultResponse(value: unknown): value is MessageResultResponse {
  return (
    isRecord(value) &&
    isRoute(value.route) &&
    typeof value.message === 'string' &&
    isNullableString(value.candidate_itinerary) &&
    isNullableString(value.current_itinerary)
  )
}

function isProcessingResponse(value: unknown, idempotencyId: string): boolean {
  return (
    isRecord(value) &&
    value.idempotency_id === idempotencyId &&
    value.status === 'processing'
  )
}

function toMessageResult(response: MessageResultResponse): MessageResult {
  return {
    route: response.route,
    message: response.message,
    candidateItinerary: response.candidate_itinerary,
    currentItinerary: response.current_itinerary,
  }
}

function isRoute(value: unknown): value is MessageResult['route'] {
  return value === 'planning' || value === 'explore' || value === 'research' || value === 'helper'
}

function isStreamRunStatus(value: unknown): value is StreamRunStatus {
  return value === 'completed' || value === 'waiting_user' || value === 'cancelled' || value === 'failed'
}

function isTaskResultStatus(value: unknown): value is TaskResultStatus {
  return value === 'success' || value === 'partial' || value === 'failed'
}

function isAllowedAnswers(value: unknown): value is ['是', '否'] | null {
  return value === null || (Array.isArray(value) && value[0] === '是' && value[1] === '否')
}

function isNullableString(value: unknown): value is string | null {
  return typeof value === 'string' || value === null
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}
