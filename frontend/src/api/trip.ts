import { runtimeConfig } from '@/config/runtime'
import type {
  ConversationMessage,
  ConversationPage,
  ConversationRole,
  TripBootstrap,
} from '@/types/trip'

const bootstrapMessageLimit = 30

type TripApiErrorKind = 'not-found' | 'failed'

export class TripApiError extends Error {
  constructor(public readonly kind: TripApiErrorKind, message: string) {
    super(message)
    this.name = 'TripApiError'
  }
}

export async function loadTripBootstrap(
  tripId: string,
  userId: string,
): Promise<TripBootstrap> {
  const search = new URLSearchParams({
    user_id: userId,
    message_limit: String(bootstrapMessageLimit),
  })
  const response = await fetch(
    `${runtimeConfig.apiBaseUrl}/trips/${encodeURIComponent(tripId)}/bootstrap?${search}`,
    { headers: { Accept: 'application/json' } },
  )

  if (response.status === 404) {
    throw new TripApiError('not-found', '未找到当前用户对应的旅行')
  }
  if (!response.ok) {
    throw new TripApiError('failed', `行程加载失败，HTTP 状态码：${response.status}`)
  }

  const body: unknown = await response.json()
  if (!isTripBootstrapResponse(body)) {
    throw new TripApiError('failed', '行程加载响应格式无效')
  }

  return {
    tripId: body.trip_id,
    conversations: toConversationPage(body.conversations),
    currentItinerary: body.current_itinerary,
  }
}

export async function loadEarlierConversations(
  tripId: string,
  userId: string,
  beforeId: number,
): Promise<ConversationPage> {
  const search = new URLSearchParams({
    user_id: userId,
    before_id: String(beforeId),
    limit: String(bootstrapMessageLimit),
  })
  const response = await fetch(
    `${runtimeConfig.apiBaseUrl}/trips/${encodeURIComponent(tripId)}/conversations?${search}`,
    { headers: { Accept: 'application/json' } },
  )

  if (response.status === 404) {
    throw new TripApiError('not-found', '未找到当前用户对应的旅行')
  }
  if (!response.ok) {
    throw new TripApiError('failed', `历史对话加载失败，HTTP 状态码：${response.status}`)
  }

  const body: unknown = await response.json()
  if (!isConversationPageResponse(body)) {
    throw new TripApiError('failed', '历史对话加载响应格式无效')
  }
  return toConversationPage(body)
}

/** 取消当前 Trip 的运行或等待用户输入的 checkpoint。 */
export async function cancelTripRun(tripId: string, userId: string): Promise<boolean> {
  const response = await fetch(
    `${runtimeConfig.apiBaseUrl}/trips/${encodeURIComponent(tripId)}/cancel`,
    {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ user_id: userId }),
    },
  )

  if (response.status === 404) {
    throw new TripApiError('not-found', '未找到当前用户对应的旅行')
  }
  if (!response.ok) {
    throw new TripApiError('failed', `取消当前任务失败，HTTP 状态码：${response.status}`)
  }

  const body: unknown = await response.json()
  if (!isCancelRunResponse(body)) {
    throw new TripApiError('failed', '取消当前任务响应格式无效')
  }
  return body.cancelled
}

interface TripBootstrapResponse {
  trip_id: string
  conversations: ConversationPageResponse
  current_itinerary: string | null
}

interface ConversationPageResponse {
  items: ConversationMessageResponse[]
  next_before_id: number | null
  has_more: boolean
}

interface ConversationMessageResponse {
  id: number
  role: ConversationRole
  content: string
  created_at: string
  exchange_id: string | null
}

interface CancelRunResponse {
  cancelled: boolean
}

function toConversationMessage(response: ConversationMessageResponse): ConversationMessage {
  return {
    id: response.id,
    role: response.role,
    content: response.content,
    createdAt: response.created_at,
    exchangeId: response.exchange_id,
  }
}

function toConversationPage(response: ConversationPageResponse): ConversationPage {
  return {
    items: response.items.map(toConversationMessage),
    nextBeforeId: response.next_before_id,
    hasMore: response.has_more,
  }
}

function isTripBootstrapResponse(value: unknown): value is TripBootstrapResponse {
  if (!isRecord(value) || typeof value.trip_id !== 'string') return false
  if (typeof value.current_itinerary !== 'string' && value.current_itinerary !== null) {
    return false
  }
  return isConversationPageResponse(value.conversations)
}

function isConversationPageResponse(value: unknown): value is ConversationPageResponse {
  if (!isRecord(value) || !Array.isArray(value.items)) return false
  if (typeof value.has_more !== 'boolean') return false
  if (typeof value.next_before_id !== 'number' && value.next_before_id !== null) return false
  return value.items.every(isConversationMessageResponse)
}

function isConversationMessageResponse(value: unknown): value is ConversationMessageResponse {
  return (
    isRecord(value) &&
    typeof value.id === 'number' &&
    (value.role === 'user' || value.role === 'assistant') &&
    typeof value.content === 'string' &&
    typeof value.created_at === 'string' &&
    (typeof value.exchange_id === 'string' || value.exchange_id === null)
  )
}

function isCancelRunResponse(value: unknown): value is CancelRunResponse {
  return isRecord(value) && typeof value.cancelled === 'boolean'
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}
