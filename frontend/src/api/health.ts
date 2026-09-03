import { runtimeConfig } from '@/config/runtime'

interface HealthResponse {
  status: 'ok'
}

export async function checkHealth(): Promise<void> {
  const response = await fetch(`${runtimeConfig.apiBaseUrl}/health`, {
    headers: { Accept: 'application/json' },
  })

  if (!response.ok) {
    throw new Error(`健康检查失败，HTTP 状态码：${response.status}`)
  }

  const body: unknown = await response.json()
  if (!isHealthResponse(body)) {
    throw new Error('健康检查响应格式无效')
  }
}

function isHealthResponse(value: unknown): value is HealthResponse {
  return (
    typeof value === 'object' &&
    value !== null &&
    'status' in value &&
    value.status === 'ok'
  )
}
