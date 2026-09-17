import { isProductId } from './identity'
import type { ProductId } from './identity'
export const statuses = {
  PENDING_PAYMENT: '待支付',
  PAID: '已支付',
  COMPLETED: '已完成',
  CANCELLED: '已取消',
  EXPIRED: '已过期',
  REFUNDING: '退款中',
  REFUNDED: '已退款',
} as const
export type OrderStatus = keyof typeof statuses
export type StatusFilter = OrderStatus | ''
export interface OrderItem {
  id: number
  orderId: string
  itemType: string
  itemId: ProductId
  titleSnapshot: string
  unitPriceMinor: number
  quantity: number
  subtotalMinor: number
}
export interface Order {
  id: string
  orderNo: string
  status: OrderStatus
  currency: string
  totalMinor: number
  discountMinor: number
  payableMinor: number
  createdAt: string
  expiresAt: string | null
  paidAt: string | null
  items: OrderItem[]
}
export interface OrderPage {
  orders: Order[]
  nextCursor: string | null
  hasMore: boolean
}
export interface Session {
  authenticated: true
  username: string
  csrfToken: string
}

const record = (value: unknown): Record<string, unknown> => {
  if (!value || typeof value !== 'object' || Array.isArray(value))
    throw new Error('服务返回的数据格式不正确')
  return value as Record<string, unknown>
}
const string = (value: unknown): string => {
  if (typeof value !== 'string' || !value) throw new Error('服务返回的数据字段不完整')
  return value
}
const integer = (value: unknown): number => {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0)
    throw new Error('订单数值格式不正确')
  return value
}
const nullableString = (value: unknown): string | null => (value === null ? null : string(value))

export function parseOrder(value: unknown): Order {
  const o = record(value)
  if (!Array.isArray(o.items) || !Object.hasOwn(statuses, string(o.status)))
    throw new Error('订单状态或明细格式不正确')
  const id = string(o.id)
  return {
    id,
    orderNo: string(o.orderNo),
    status: o.status as OrderStatus,
    currency: string(o.currency),
    totalMinor: integer(o.totalMinor),
    discountMinor: integer(o.discountMinor),
    payableMinor: integer(o.payableMinor),
    createdAt: string(o.createdAt),
    expiresAt: nullableString(o.expiresAt),
    paidAt: nullableString(o.paidAt),
    items: o.items.map((value) => {
      const item = record(value)
      if (item.orderId !== id || integer(item.quantity) === 0 || !isProductId(item.itemId))
        throw new Error('订单明细归属或数量不正确')
      return {
        id: integer(item.id),
        orderId: id,
        itemType: string(item.itemType),
        itemId: item.itemId,
        titleSnapshot: string(item.titleSnapshot),
        unitPriceMinor: integer(item.unitPriceMinor),
        quantity: integer(item.quantity),
        subtotalMinor: integer(item.subtotalMinor),
      }
    }),
  }
}
export function parsePage(value: unknown): OrderPage {
  const page = record(value)
  if (!Array.isArray(page.orders) || typeof page.hasMore !== 'boolean')
    throw new Error('分页数据格式不正确')
  const nextCursor = nullableString(page.nextCursor)
  if (
    (page.hasMore && (!nextCursor || page.orders.length === 0)) ||
    (!page.hasMore && nextCursor !== null)
  ) {
    throw new Error('分页游标与结果不一致，请刷新后重试')
  }
  return { orders: page.orders.map(parseOrder), nextCursor, hasMore: page.hasMore }
}
export function parseSession(value: unknown): Session {
  const session = record(value)
  if (session.authenticated !== true) throw new Error('登录状态格式不正确')
  return {
    authenticated: true,
    username: string(session.username),
    csrfToken: string(session.csrfToken),
  }
}
export function mergeOrders(previous: Order[], incoming: Order[]): Order[] {
  const orders = new Map(previous.map((order) => [order.id, order]))
  incoming.forEach((order) => orders.set(order.id, order))
  return [...orders.values()]
}
export function money(minor: number, currency = 'CNY'): string {
  try {
    return new Intl.NumberFormat('zh-CN', { style: 'currency', currency }).format(minor / 100)
  } catch {
    return `${currency} ${(minor / 100).toFixed(2)}`
  }
}
// Java returns LocalDateTime with no timezone; display it without inventing a UTC offset.
export function dateLabel(value: string | null): string {
  if (!value) return '—'
  return value.replace('T', ' ').slice(0, 16)
}
export function pageQuery(status: StatusFilter, cursor?: string | null): string {
  const query = new URLSearchParams({ size: '20' })
  if (status) query.set('status', status)
  if (cursor) query.set('cursor', cursor)
  return query.toString()
}
