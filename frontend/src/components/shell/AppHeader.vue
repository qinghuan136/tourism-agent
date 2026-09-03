<script setup lang="ts">
import ServiceStatus from '@/components/run-control/ServiceStatus.vue'
import type { ServiceStatus as ServiceStatusValue } from '@/stores/service-status'

defineProps<{
  serviceStatus: ServiceStatusValue
  tripLabel: string
}>()
</script>

<template>
  <header class="app-header">
    <div class="app-header__identity">
      <div
        class="brand-mark"
        aria-hidden="true"
      >
        <svg
          viewBox="0 0 40 40"
          role="img"
        >
          <path d="M20 3.5 35.2 12v16L20 36.5 4.8 28V12L20 3.5Z" />
          <path
            class="brand-mark__route"
            d="m13.2 26.8 4.1-12.1 9.5-3.2-4.1 12.1-9.5 3.2Z"
          />
          <circle
            class="brand-mark__center"
            cx="20"
            cy="19.2"
            r="2.35"
          />
        </svg>
      </div>
      <span class="app-header__brand">旅程助手</span>
      <span
        class="app-header__divider"
        aria-hidden="true"
      />
      <span class="app-header__workspace-name">旅行工作区</span>
    </div>

    <div class="app-header__meta">
      <ServiceStatus :status="serviceStatus" />
      <span class="app-header__trip">行程 {{ tripLabel }}</span>
      <RouterLink
        class="app-header__switch"
        to="/"
      >
        切换旅行
      </RouterLink>
    </div>
  </header>
</template>
