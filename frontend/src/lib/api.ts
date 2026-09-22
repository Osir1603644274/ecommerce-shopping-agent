import { startObservation } from './backendObserver'
import type { ObservationCause } from './backendObserver'
export class ApiError extends Error {
  readonly status: number
  readonly retryAfter: number | null
  readonly code: string | null
  constructor(
    message: string,
    status = 0,
    retryAfter: number | null = null,
    code: string | null = null,
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.retryAfter = retryAfter
    this.code = code
  }
}
export function retrySeconds(value: string | null): number | null {
  if (!value) return null
  if (/^\d+$/.test(value)) return Number(value)
  const time = Date.parse(value)
  return Number.isFinite(time) ? Math.max(0, Math.ceil((time - Date.now()) / 1000)) : null
}
const errors: Record<string, string> = {
  'authentication required': '请先登录后查看自己的订单',
  'authentication expired': '登录已过期，请重新登录',
  'authentication rejected': '身份验证未通过，请检查账号或重新登录',
  'Java authority unavailable': '订单服务暂时不可用，请稍后重试',
  'commerce demo disabled': '账户服务尚未启用，请检查本机服务配置',
  'csrf validation failed': '会话已更新，请重新登录后再操作',
  'same-origin request required': '请求来源校验未通过，请通过同源页面访问',
}
export async function api(path: string, options: RequestInit & { observation?: ObservationCause; timeoutMs?: number } = {}): Promise<unknown> {
  const { observation, timeoutMs = 12000, ...fetchOptions } = options
  const observed = startObservation(path, options.method ?? 'GET', observation)
  let observedStatus = 0, ticket: string | null = null
  const timeout = new AbortController()
  const timer = setTimeout(() => timeout.abort(), Math.max(1000, Math.min(90000, timeoutMs)))
  const signal = options.signal ? AbortSignal.any([options.signal, timeout.signal]) : timeout.signal
  try {
    const response = await fetch(`/api/commerce-demo${path}`, {
      ...fetchOptions,
      signal,
      credentials: 'same-origin',
      cache: 'no-store',
      headers: {
        Accept: 'application/json',
        ...(observed ? { 'X-Backend-Observe': '1' } : {}),
        ...(options.body ? { 'Content-Type': 'application/json' } : {}),
        ...options.headers,
      },
    })
    observedStatus = response.status
    ticket = response.headers.get('X-Backend-Trace-Ticket')
    const body: unknown = await response.json().catch(() => null)
    if (!response.ok) {
      const retry = retrySeconds(response.headers.get('Retry-After'))
      const detail = body && typeof body === 'object' && 'detail' in body ? body.detail : null
      let message = typeof detail === 'string' ? (errors[detail] ?? detail) : '请求失败，请稍后重试'
      if (response.status === 429)
        message = retry === null ? '操作太频繁，请稍后重试' : `操作太频繁，请在 ${retry} 秒后重试`
      if (response.status === 422 && typeof detail !== 'string') message = '输入格式不正确，请检查填写内容'
      if (response.status >= 500) message = '服务暂时不可用，请检查后端连接后重试'
      throw new ApiError(
        message,
        response.status,
        retry,
        typeof detail === 'string' ? detail : null,
      )
    }
    if (body === null) throw new ApiError('服务返回了无效的数据', 502)
    return body
  } catch (error) {
    if (options.signal?.aborted) throw error
    if (error instanceof ApiError) throw error
    throw new ApiError(
      timeout.signal.aborted ? '请求超时，请稍后重试' : '无法连接服务，请检查网络或本机后端',
    )
  } finally {
    clearTimeout(timer)
    observed?.(observedStatus, ticket)
  }
}
export const errorMessage = (error: unknown): string =>
  error instanceof Error ? error.message : '发生了未知错误，请重试'
export const isSessionError = (error: unknown): boolean =>
  error instanceof ApiError &&
  (error.status === 401 || (error.status === 403 && error.code === 'csrf validation failed'))
