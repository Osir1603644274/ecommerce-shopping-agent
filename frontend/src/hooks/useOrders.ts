import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { api, isSessionError, errorMessage } from '../lib/api'
import { demoPage } from '../lib/demo'
import { mergeOrders, pageQuery, parsePage } from '../lib/orders'
import type { Order, StatusFilter } from '../lib/orders'

export function useOrders(
  identity: string | null,
  demo: boolean,
  status: StatusFilter,
  csrf: string | null,
  onExpired: () => void,
) {
  const [orders, setOrders] = useState<Order[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [hasMore, setHasMore] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null)
  const [revision, setRevision] = useState(0)
  const cursor = useRef<string | null>(null)
  const generation = useRef(0)
  const active = useRef<AbortController | null>(null)
  const busy = useRef(false)
  const current = useRef({ identity, demo, status })
  useLayoutEffect(() => {
    current.current = { identity, demo, status }
  }, [identity, demo, status])

  const request = useCallback(
    async (append: boolean, gen: number) => {
      if (busy.current || (!identity && !demo)) return
      busy.current = true
      const controller = new AbortController()
      active.current = controller
      const requestedCursor = append ? cursor.current : null
      const valid = () =>
        gen === generation.current &&
        !controller.signal.aborted &&
        current.current.identity === identity &&
        current.current.demo === demo &&
        current.current.status === status
      setLoading(true)
      setError('')
      try {
        const page = demo
          ? demoPage(status, requestedCursor)
          : parsePage(
              await api(`/orders/page?${pageQuery(status, requestedCursor)}`, {
                observation: { label: append ? '加载下一页订单' : '加载或刷新订单列表', trigger: append ? 'user' : 'effect', code: 'frontend/src/hooks/useOrders.ts → request' },
                signal: controller.signal,
                headers: { 'X-CSRF-Token': csrf ?? '' },
              }),
            )
        if (!valid()) return
        if (page.hasMore && page.nextCursor === requestedCursor)
          throw new Error('服务返回了重复游标，请刷新订单后重试')
        setOrders((previous) => (append ? mergeOrders(previous, page.orders) : page.orders))
        cursor.current = page.nextCursor
        setHasMore(page.hasMore)
        setLoaded(true)
        setUpdatedAt(new Date())
      } catch (caught) {
        if (!valid()) return
        if (isSessionError(caught)) onExpired()
        else setError(errorMessage(caught))
      } finally {
        if (valid()) {
          busy.current = false
          setLoading(false)
        }
      }
    },
    [identity, demo, status, csrf, onExpired],
  )

  useEffect(() => {
    const gen = ++generation.current
    active.current?.abort()
    busy.current = false
    cursor.current = null
    setOrders([])
    setHasMore(false)
    setLoaded(false)
    setError('')
    setLoading(false)
    setUpdatedAt(null)
    void request(false, gen)
    return () => {
      active.current?.abort()
      generation.current++
    }
  }, [request, revision])

  return {
    orders,
    loading,
    error,
    hasMore,
    loaded,
    updatedAt,
    refresh: () => setRevision((value) => value + 1),
    loadMore: () => void request(loaded, generation.current),
  }
}
