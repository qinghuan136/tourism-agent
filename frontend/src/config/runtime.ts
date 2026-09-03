function normalizeApiBaseUrl(value: string | undefined): string {
  const baseUrl = value?.trim() || '/api'
  return baseUrl === '/' ? '' : baseUrl.replace(/\/$/, '')
}

export const runtimeConfig = Object.freeze({
  apiBaseUrl: normalizeApiBaseUrl(import.meta.env.VITE_API_BASE_URL),
})
