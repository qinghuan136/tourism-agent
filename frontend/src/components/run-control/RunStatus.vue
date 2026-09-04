<script setup lang="ts">
import type { RunIssue } from '@/api/errors'

defineProps<{
  feedback: string | null
  issue: RunIssue | null
  canCancel: boolean
  isCancelling: boolean
}>()

const emit = defineEmits<{
  retrySame: []
  retryNew: []
  restoreCandidate: []
  cancel: []
  leaveTrip: []
}>()
</script>

<template>
  <section
    v-if="feedback || issue || canCancel"
    class="run-status"
    data-testid="run-status"
    aria-live="polite"
  >
    <p
      v-if="feedback"
      class="run-status__feedback"
      role="status"
    >
      {{ feedback }}
    </p>
    <template v-if="issue">
      <p
        class="run-status__issue"
        role="alert"
      >
        {{ issue.message }}
      </p>
      <div class="run-status__actions">
        <button
          v-if="issue.actions.includes('retry-same')"
          type="button"
          aria-label="使用原请求重试"
          @click="emit('retrySame')"
        >
          使用原请求重试
        </button>
        <button
          v-if="issue.actions.includes('retry-new')"
          type="button"
          aria-label="使用新请求重试"
          @click="emit('retryNew')"
        >
          使用新请求重试
        </button>
        <button
          v-if="issue.actions.includes('restore-candidate')"
          type="button"
          aria-label="返回候选确认"
          @click="emit('restoreCandidate')"
        >
          返回候选确认
        </button>
        <button
          v-if="issue.actions.includes('leave-trip')"
          class="run-status__leave"
          type="button"
          aria-label="返回旅行选择"
          @click="emit('leaveTrip')"
        >
          返回旅行选择
        </button>
        <button
          v-if="issue.actions.includes('cancel')"
          class="run-status__cancel"
          type="button"
          :disabled="isCancelling"
          aria-label="取消当前任务"
          @click="emit('cancel')"
        >
          {{ isCancelling ? '正在取消…' : '取消当前任务' }}
        </button>
      </div>
    </template>
    <button
      v-else-if="canCancel"
      class="run-status__cancel"
      type="button"
      :disabled="isCancelling"
      aria-label="取消当前任务"
      @click="emit('cancel')"
    >
      {{ isCancelling ? '正在取消…' : '取消当前任务' }}
    </button>
  </section>
</template>
