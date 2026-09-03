import { defineStore } from 'pinia'
import { ref } from 'vue'

import { checkHealth } from '@/api/health'

export type ServiceStatus = 'checking' | 'available' | 'unavailable'

export const useServiceStatusStore = defineStore('service-status', () => {
  const status = ref<ServiceStatus>('checking')

  async function check(): Promise<void> {
    status.value = 'checking'
    try {
      await checkHealth()
      status.value = 'available'
    } catch {
      // 顶栏只暴露用户可行动的服务状态，不向界面泄露底层网络异常。
      status.value = 'unavailable'
    }
  }

  return { status, check }
})
