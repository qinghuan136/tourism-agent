<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import MarkdownContent from '@/components/content/MarkdownContent.vue'
import type {
  ConversationRunStatus,
  HistoryStatus,
  LiveConversationMessage,
  TripWorkspaceStatus,
} from '@/stores/trip-workspace'
import type { StreamedTaskResult } from '@/api/message-stream'
import type { TripBootstrap } from '@/types/trip'

const props = defineProps<{
  status: TripWorkspaceStatus
  historyStatus: HistoryStatus
  bootstrap: TripBootstrap | null
  runStatus: ConversationRunStatus
  runFeedback: string | null
  runError: string | null
  candidateItinerary: string | null
  liveMessages: LiveConversationMessage[]
  taskResults: StreamedTaskResult[]
  isCancelling: boolean
}>()

const emit = defineEmits<{
  retry: []
  loadEarlier: []
  sendMessage: [message: string]
  confirmCandidate: [accepted: boolean]
  cancelRun: []
}>()

const message = ref('')
const isTemporaryOutputClosed = ref(false)
const canSendMessage = computed(
  () =>
    props.status === 'ready' &&
    props.runStatus !== 'running' &&
    props.candidateItinerary === null,
)
const canCancelRun = computed(
  () =>
    props.status === 'ready' &&
    (props.runStatus === 'running' || props.runStatus === 'waiting_user'),
)
const taskModuleLabels: Record<StreamedTaskResult['module'], string> = {
  planning: '规划结果',
  explore: '探索结果',
  research: '调研结果',
  helper: '助手结果',
}
const hasTemporaryOutput = computed(
  () => props.candidateItinerary !== null || props.taskResults.length > 0,
)
const isTemporaryOutputVisible = computed(
  () => hasTemporaryOutput.value && !isTemporaryOutputClosed.value,
)

watch(
  [() => props.candidateItinerary, () => props.taskResults.length],
  ([candidateItinerary, taskResultCount], [previousCandidateItinerary, previousTaskResultCount]) => {
    if (
      (candidateItinerary && candidateItinerary !== previousCandidateItinerary) ||
      taskResultCount > previousTaskResultCount
    ) {
      isTemporaryOutputClosed.value = false
    }
  },
)

function submitMessage(): void {
  const content = message.value.trim()
  if (!content || !canSendMessage.value) return
  emit('sendMessage', content)
  message.value = ''
}

function closeTemporaryOutput(): void {
  isTemporaryOutputClosed.value = true
}
</script>

<template>
  <main class="trip-workspace">
    <section
      class="trip-workspace__conversation"
      aria-labelledby="conversation-title"
    >
      <div class="section-heading">
        <div>
          <p class="section-heading__eyebrow">
            TRIP CONVERSATION
          </p>
          <h1 id="conversation-title">
            对话
          </h1>
        </div>
        <span
          class="section-heading__rule"
          aria-hidden="true"
        />
      </div>
      <div class="conversation-content">
        <section
          v-if="isTemporaryOutputVisible"
          class="temporary-output"
          data-testid="temporary-output-panel"
          aria-label="本轮临时内容"
        >
          <div class="temporary-output__header">
            <p>本轮临时内容</p>
            <button
              type="button"
              aria-label="关闭本轮临时内容"
              @click="closeTemporaryOutput"
            >
              关闭
            </button>
          </div>
          <div
            v-if="candidateItinerary"
            class="temporary-output__candidate"
          >
            <p class="temporary-output__label">
              候选行程
            </p>
            <MarkdownContent
              class="current-itinerary"
              :content="candidateItinerary"
            />
          </div>
          <section
            v-if="taskResults.length > 0"
            class="task-results"
            aria-label="本轮子任务结果"
          >
            <details
              v-for="(taskResult, index) in taskResults"
              :key="`${taskResult.taskId}-${index}`"
              class="task-result"
              open
            >
              <summary class="task-result__summary">
                <span>{{ taskModuleLabels[taskResult.module] }}</span>
                <span class="task-result__status">
                  {{ taskResult.status === 'success' ? '已完成' : taskResult.status === 'partial' ? '部分完成' : '失败' }}
                </span>
              </summary>
              <MarkdownContent
                class="task-result__content"
                :content="taskResult.result"
              />
            </details>
          </section>
        </section>
        <div class="conversation-scroll-area">
          <p
            v-if="status === 'loading' || status === 'idle'"
            class="workspace-feedback"
            role="status"
          >
            正在加载对话…
          </p>
          <div
            v-else-if="status === 'not-found' || status === 'failed'"
            class="workspace-feedback workspace-feedback--error"
            role="alert"
          >
            <p>{{ status === 'not-found' ? '未找到该旅行' : '加载旅行失败' }}</p>
            <button
              class="retry-button"
              type="button"
              @click="emit('retry')"
            >
              重新加载
            </button>
          </div>
          <template v-else>
            <p
              v-if="bootstrap?.conversations.items.length === 0 && liveMessages.length === 0 && taskResults.length === 0"
              class="workspace-feedback"
            >
              暂时还没有对话记录
            </p>
            <div
              v-if="bootstrap?.conversations.hasMore"
              class="history-control"
            >
              <button
                class="history-button"
                type="button"
                :disabled="historyStatus === 'loading'"
                @click="emit('loadEarlier')"
              >
                {{
                  historyStatus === 'loading'
                    ? '正在加载更早消息…'
                    : historyStatus === 'failed'
                      ? '重新加载更早消息'
                      : '加载更早消息'
                }}
              </button>
              <p
                v-if="historyStatus === 'failed'"
                class="history-control__error"
                role="status"
              >
                加载更早消息失败，请重试
              </p>
            </div>
            <ol
              class="conversation-list"
              aria-label="对话记录"
            >
              <li
                v-for="conversationMessage in bootstrap?.conversations.items"
                :key="conversationMessage.id"
                class="conversation-message"
                :class="`conversation-message--${conversationMessage.role}`"
              >
                <article class="conversation-message__bubble">
                  <MarkdownContent
                    v-if="conversationMessage.role === 'assistant'"
                    :content="conversationMessage.content"
                  />
                  <template v-else>
                    {{ conversationMessage.content }}
                  </template>
                </article>
              </li>
              <li
                v-for="(liveMessage, index) in liveMessages"
                :key="`live-${index}`"
                class="conversation-message"
                :class="`conversation-message--${liveMessage.role}`"
              >
                <article class="conversation-message__bubble">
                  <MarkdownContent
                    v-if="liveMessage.role === 'assistant'"
                    :content="liveMessage.content"
                  />
                  <template v-else>
                    {{ liveMessage.content }}
                  </template>
                </article>
              </li>
            </ol>
            <div
              v-if="(runFeedback || runError) && !(candidateItinerary && runStatus === 'waiting_user')"
              class="run-feedback"
            >
              <p
                v-if="runFeedback"
                role="status"
              >
                {{ runFeedback }}
              </p>
              <p
                v-if="runError"
                role="alert"
              >
                {{ runError }}
              </p>
            </div>
          </template>
        </div>
        <form
          v-if="status === 'ready'"
          class="message-composer"
          @submit.prevent="submitMessage"
        >
          <div
            v-if="candidateItinerary && runStatus === 'waiting_user'"
            class="candidate-actions candidate-actions--conversation"
            data-testid="conversation-candidate-actions"
          >
            <button
              class="candidate-actions__accept"
              type="button"
              :disabled="isCancelling"
              @click="emit('confirmCandidate', true)"
            >
              确认采用
            </button>
            <button
              class="candidate-actions__decline"
              type="button"
              :disabled="isCancelling"
              @click="emit('confirmCandidate', false)"
            >
              不采用
            </button>
          </div>
          <label for="message-input">向旅行助手发送消息</label>
          <div class="message-composer__controls">
            <textarea
              id="message-input"
              v-model="message"
              :disabled="!canSendMessage"
              maxlength="4000"
              placeholder="例如：帮我规划广州三日游"
              rows="3"
            />
            <button
              type="submit"
              :disabled="!canSendMessage || !message.trim()"
            >
              {{ runStatus === 'running' ? '处理中…' : '发送' }}
            </button>
          </div>
          <p
            v-if="candidateItinerary"
            class="message-composer__hint"
          >
            等待确认期间不能输入新消息；可确认、拒绝或取消当前任务。
          </p>
          <button
            v-if="canCancelRun"
            class="cancel-run-button"
            type="button"
            :disabled="isCancelling"
            @click="emit('cancelRun')"
          >
            {{ isCancelling ? '正在取消…' : '取消当前任务' }}
          </button>
        </form>
      </div>
    </section>

    <aside
      class="trip-workspace__itinerary"
      aria-labelledby="itinerary-title"
    >
      <div class="section-heading section-heading--itinerary">
        <div>
          <p class="section-heading__eyebrow">
            ITINERARY
          </p>
          <h2 id="itinerary-title">
            行程
          </h2>
        </div>
      </div>

      <div
        class="itinerary-panel itinerary-panel--current"
        data-testid="current-itinerary-panel"
      >
        <p
          v-if="status === 'loading' || status === 'idle'"
          class="workspace-feedback"
          role="status"
        >
          正在加载行程…
        </p>
        <MarkdownContent
          v-else-if="status === 'ready' && bootstrap?.currentItinerary"
          class="current-itinerary"
          :content="bootstrap.currentItinerary"
        />
        <p
          v-else-if="status === 'ready'"
          class="workspace-feedback"
        >
          暂无已确认行程
        </p>
      </div>
    </aside>
  </main>
</template>
