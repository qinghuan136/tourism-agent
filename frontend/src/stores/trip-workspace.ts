import { defineStore } from 'pinia'
import { ref } from 'vue'

import {
  cancelTripRun,
  loadEarlierConversations,
  loadTripBootstrap,
  TripApiError,
} from '@/api/trip'
import {
  sendStreamedMessage,
  type MessageResult,
  type MessageStreamEvent,
  type StreamedTaskResult,
  type StreamRunStatus,
} from '@/api/message-stream'
import { toRunIssue, type RunIssue } from '@/api/errors'
import { useLocalSessionStore } from '@/stores/local-session'
import type { ConversationMessage, TripBootstrap } from '@/types/trip'

export type TripWorkspaceStatus = 'idle' | 'loading' | 'ready' | 'not-found' | 'failed'
export type HistoryStatus = 'idle' | 'loading' | 'failed'
export type ConversationRunStatus =
  | 'idle'
  | 'running'
  | 'interrupted'
  | 'blocked'
  | StreamRunStatus

export interface LiveConversationMessage {
  role: 'user' | 'assistant'
  content: string
}

interface PendingMessageRequest {
  userId: string
  tripId: string
  idempotencyId: string
  message: string
}

export type PendingRetryMode = 'same' | 'new'

export const useTripWorkspaceStore = defineStore('trip-workspace', () => {
  const bootstrap = ref<TripBootstrap | null>(null)
  const status = ref<TripWorkspaceStatus>('idle')
  const historyStatus = ref<HistoryStatus>('idle')
  const runStatus = ref<ConversationRunStatus>('idle')
  const runFeedback = ref<string | null>(null)
  const runIssue = ref<RunIssue | null>(null)
  const pendingRequest = ref<PendingMessageRequest | null>(null)
  const candidateItinerary = ref<string | null>(null)
  const liveMessages = ref<LiveConversationMessage[]>([])
  const taskResults = ref<StreamedTaskResult[]>([])
  const isCancelling = ref(false)
  const session = useLocalSessionStore()

  async function load(tripId: string): Promise<void> {
    status.value = 'loading'
    bootstrap.value = null
    historyStatus.value = 'idle'
    resetRunState()
    const userId = session.userId
    if (!userId) {
      status.value = 'failed'
      return
    }

    try {
      bootstrap.value = await loadTripBootstrap(tripId, userId)
      session.recordSuccessfulTrip(tripId)
      status.value = 'ready'
    } catch (error) {
      status.value = error instanceof TripApiError && error.kind === 'not-found' ? 'not-found' : 'failed'
    }
  }

  async function loadEarlier(): Promise<void> {
    const current = bootstrap.value
    const page = current?.conversations
    if (!current || !page?.hasMore || page.nextBeforeId === null) return
    const userId = session.userId
    if (!userId) {
      historyStatus.value = 'failed'
      return
    }

    historyStatus.value = 'loading'
    try {
      const olderPage = await loadEarlierConversations(
        current.tripId,
        userId,
        page.nextBeforeId,
      )
      if (bootstrap.value?.tripId !== current.tripId) return

      bootstrap.value = {
        ...current,
        conversations: {
          items: mergeConversationItems(olderPage.items, current.conversations.items),
          nextBeforeId: olderPage.nextBeforeId,
          hasMore: olderPage.hasMore,
        },
      }
      historyStatus.value = 'idle'
    } catch {
      // 分页失败时必须保留已加载的对话，用户可通过同一入口重试。
      historyStatus.value = 'failed'
    }
  }

  async function sendMessage(message: string): Promise<void> {
    const current = bootstrap.value
    const normalizedMessage = message.trim()
    if (
      !current ||
      status.value !== 'ready' ||
      runStatus.value === 'running' ||
      runStatus.value === 'interrupted' ||
      runStatus.value === 'blocked' ||
      isCancelling.value ||
      runIssue.value
    ) {
      return
    }
    const userId = session.userId
    if (!userId) {
      runStatus.value = 'failed'
      runIssue.value = localRunIssue('请先选择用户和旅行')
      return
    }
    if (!normalizedMessage || normalizedMessage.length > 4000) {
      runStatus.value = 'failed'
      runIssue.value = localRunIssue('请输入 1 到 4000 个字符的消息')
      return
    }
    if (candidateItinerary.value) return

    if (runStatus.value !== 'waiting_user') taskResults.value = []
    await startPendingRequest({
      userId,
      tripId: current.tripId,
      idempotencyId: crypto.randomUUID(),
      message: normalizedMessage,
    })
  }

  async function confirmCandidate(accepted: boolean): Promise<void> {
    const current = bootstrap.value
    if (
      !current ||
      !candidateItinerary.value ||
      runStatus.value !== 'waiting_user' ||
      isCancelling.value
    ) {
      return
    }
    const userId = session.userId
    if (!userId) {
      runStatus.value = 'failed'
      runIssue.value = localRunIssue('请先选择用户和旅行')
      return
    }
    await startPendingRequest({
      userId,
      tripId: current.tripId,
      idempotencyId: crypto.randomUUID(),
      message: accepted ? '是' : '否',
    })
  }

  async function cancelRun(): Promise<void> {
    const current = bootstrap.value
    if (
      !current ||
      status.value !== 'ready' ||
      isCancelling.value ||
      (runStatus.value !== 'running' &&
        runStatus.value !== 'waiting_user' &&
        runStatus.value !== 'interrupted' &&
        runStatus.value !== 'blocked')
    ) {
      return
    }
    const userId = session.userId
    if (!userId) {
      runIssue.value = localRunIssue('请先选择用户和旅行')
      return
    }

    isCancelling.value = true
    runIssue.value = null
    try {
      const cancelled = await cancelTripRun(current.tripId, userId)
      candidateItinerary.value = null
      pendingRequest.value = null
      runStatus.value = 'cancelled'
      runFeedback.value = cancelled ? '已取消当前任务' : '当前没有可取消的任务'
    } catch (error) {
      runIssue.value = toRunIssue(error)
    } finally {
      isCancelling.value = false
    }
  }

  async function startPendingRequest(request: PendingMessageRequest): Promise<void> {
    pendingRequest.value = request
    await submitPendingRequest(request, true)
  }

  async function retryPendingRequest(mode: PendingRetryMode): Promise<void> {
    const request = pendingRequest.value
    if (!request || isCancelling.value) return
    const nextRequest =
      mode === 'same'
        ? request
        : { ...request, idempotencyId: crypto.randomUUID() }
    pendingRequest.value = nextRequest
    await submitPendingRequest(nextRequest, false)
  }

  function restoreCandidateConfirmation(): void {
    if (!candidateItinerary.value || runIssue.value?.code !== 'candidate-confirmation-invalid') return
    runIssue.value = null
    runFeedback.value = null
    runStatus.value = 'waiting_user'
  }

  function leaveTrip(): void {
    resetRunState()
    bootstrap.value = null
    status.value = 'idle'
  }

  async function submitPendingRequest(
    request: PendingMessageRequest,
    appendUserMessage: boolean,
  ): Promise<void> {
    runStatus.value = 'running'
    runFeedback.value = '正在发送请求'
    runIssue.value = null
    if (appendUserMessage) {
      liveMessages.value = [...liveMessages.value, { role: 'user', content: request.message }]
    }

    try {
      const outcome = await sendStreamedMessage(
        {
          userId: request.userId,
          tripId: request.tripId,
          idempotencyId: request.idempotencyId,
          message: request.message,
        },
        { onEvent: consumeStreamEvent },
      )
      if (outcome.kind === 'processing') {
        runStatus.value = 'running'
        runFeedback.value = '请求仍在处理中，请保持页面打开或取消当前任务'
        return
      }
      if (outcome.kind === 'replay') {
        applyResult(outcome.result)
        runStatus.value = 'completed'
        runFeedback.value = null
        pendingRequest.value = null
        return
      }
      if (runStatus.value === 'running') {
        runStatus.value = outcome.status
      }
      if (outcome.status !== 'failed') runFeedback.value = null
      if (outcome.status !== 'failed') pendingRequest.value = null
    } catch (error) {
      applyRunIssue(toRunIssue(error))
      runFeedback.value = null
    }
  }

  function applyRunIssue(issue: RunIssue): void {
    runIssue.value = issue
    if (issue.code === 'network' || issue.code === 'protocol') {
      runStatus.value = 'interrupted'
      return
    }
    if (issue.code === 'trip-running') {
      pendingRequest.value = null
      runStatus.value = 'blocked'
      return
    }
    if (issue.code === 'run-cancelled') {
      runStatus.value = 'cancelled'
      return
    }
    if (issue.code === 'candidate-confirmation-invalid') {
      pendingRequest.value = null
      runStatus.value = 'waiting_user'
      return
    }
    if (issue.code === 'trip-not-found' || issue.code === 'invalid-request') {
      pendingRequest.value = null
    }
    runStatus.value = 'failed'
  }

  function consumeStreamEvent(event: MessageStreamEvent): void {
    if (event.type === 'task.result') {
      taskResults.value = [
        ...taskResults.value,
        {
          taskId: event.taskId,
          module: event.module,
          status: event.status,
          result: event.result,
        },
      ]
      return
    }
    if (event.type === 'token.delta') {
      appendAssistantText(event.text)
      return
    }
    if (event.type === 'result') {
      applyResult(event.result)
      return
    }
    if (event.type === 'interaction.required') {
      candidateItinerary.value = event.candidateItinerary
      runFeedback.value = event.question
      return
    }
    if (event.type === 'error') {
      applyRunIssue({ code: 'server-failed', message: event.message, actions: ['retry-new'] })
      runFeedback.value = null
      return
    }
    if (event.type === 'run.cancelled') {
      runFeedback.value = event.message
      return
    }
    if (event.type === 'run.completed') {
      runStatus.value = event.status
      return
    }
    runFeedback.value = event.message
  }

  function appendAssistantText(text: string): void {
    const lastMessage = liveMessages.value.at(-1)
    if (lastMessage?.role === 'assistant') {
      lastMessage.content += text
      return
    }
    liveMessages.value = [...liveMessages.value, { role: 'assistant', content: text }]
  }

  function applyResult(result: MessageResult): void {
    const lastMessage = liveMessages.value.at(-1)
    if (lastMessage?.role === 'assistant') {
      lastMessage.content = result.message
    } else {
      liveMessages.value = [...liveMessages.value, { role: 'assistant', content: result.message }]
    }
    candidateItinerary.value = result.candidateItinerary
    if (bootstrap.value) {
      bootstrap.value = { ...bootstrap.value, currentItinerary: result.currentItinerary }
    }
  }

  function resetRunState(): void {
    runStatus.value = 'idle'
    runFeedback.value = null
    runIssue.value = null
    pendingRequest.value = null
    candidateItinerary.value = null
    liveMessages.value = []
    taskResults.value = []
    isCancelling.value = false
  }

  return {
    bootstrap,
    status,
    historyStatus,
    runStatus,
    runFeedback,
    runIssue,
    candidateItinerary,
    liveMessages,
    taskResults,
    isCancelling,
    load,
    loadEarlier,
    sendMessage,
    confirmCandidate,
    cancelRun,
    retryPendingRequest,
    restoreCandidateConfirmation,
    leaveTrip,
  }
})

function mergeConversationItems(
  olderItems: ConversationMessage[],
  existingItems: ConversationMessage[],
): ConversationMessage[] {
  const uniqueById = new Map<number, ConversationMessage>()
  for (const message of olderItems) uniqueById.set(message.id, message)
  for (const message of existingItems) uniqueById.set(message.id, message)
  return [...uniqueById.values()].sort((first, second) => first.id - second.id)
}

function localRunIssue(message: string): RunIssue {
  return { code: 'invalid-request', message, actions: [] }
}
