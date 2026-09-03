<script setup lang="ts">
import { computed } from 'vue'

import type { ServiceStatus } from '@/stores/service-status'

const props = defineProps<{
  status: ServiceStatus
}>()

const label = computed(() => {
  if (props.status === 'available') return '服务正常'
  if (props.status === 'unavailable') return '服务不可用'
  return '正在检查'
})
</script>

<template>
  <span
    class="service-status"
    :class="`service-status--${status}`"
    data-testid="service-status"
    role="status"
    aria-live="polite"
  >
    <span
      class="service-status__dot"
      aria-hidden="true"
    />
    {{ label }}
  </span>
</template>
