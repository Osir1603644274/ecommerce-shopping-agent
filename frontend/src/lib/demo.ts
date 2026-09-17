import type { Order, OrderPage, OrderStatus, StatusFilter } from './orders'

// Explicit, read-only sample mode. Never substituted for a failed real API request.
const products = [
  ['Apple iPhone 15 Pro · 原色钛金属', 499900, 'PRODUCT'],
  ['Sony WH-1000XM5 无线降噪耳机', 189900, 'PRODUCT'],
  ['Anker 65W 双口快充充电器', 14900, 'PRODUCT'],
  ['Apple iPad Air 11 英寸 · 深空灰', 399900, 'PRODUCT'],
] as const
const demoStatuses: OrderStatus[] = [
  'PENDING_PAYMENT',
  'PAID',
  'COMPLETED',
  'CANCELLED',
  'EXPIRED',
  'REFUNDED',
  'REFUNDING',
]
export const demoOrders: Order[] = Array.from({ length: 26 }, (_, index) => {
  const status = demoStatuses[index % demoStatuses.length]
  const id = `sample-order-${String(index + 1).padStart(3, '0')}`
  const [title, price, type] = products[index % products.length]
  const items: Order['items'] = [
    {
      id: index * 2 + 1,
      orderId: id,
      itemType: type,
      itemId: index + 1,
      titleSnapshot: title,
      unitPriceMinor: price,
      quantity: 1,
      subtotalMinor: price,
    },
  ]
  if (index === 0)
    items.push({
      id: 1001,
      orderId: id,
      itemType: 'PRODUCT',
      itemId: 1001,
      titleSnapshot: 'Anker 65W 双口快充充电器',
      unitPriceMinor: 14900,
      quantity: 2,
      subtotalMinor: 29800,
    })
  const totalMinor = items.reduce((total, item) => total + item.subtotalMinor, 0)
  return {
    id,
    orderNo: `DEMO-202609${String(8 - Math.floor(index / 4)).padStart(2, '0')}-${String(index + 1).padStart(4, '0')}`,
    status,
    currency: 'CNY',
    totalMinor,
    discountMinor: index === 0 ? 10000 : 0,
    payableMinor: totalMinor - (index === 0 ? 10000 : 0),
    createdAt: `2026-09-${String(8 - Math.floor(index / 4)).padStart(2, '0')}T10:30:00`,
    expiresAt: status === 'PENDING_PAYMENT' ? '2026-09-08T11:00:00' : null,
    paidAt: ['PAID', 'COMPLETED', 'REFUNDING', 'REFUNDED'].includes(status)
      ? '2026-09-08T10:35:00'
      : null,
    items,
  }
})
export function demoPage(status: StatusFilter, cursor?: string | null): OrderPage {
  const rows = demoOrders.filter((order) => !status || order.status === status)
  const start = Number(cursor ?? 0)
  const orders = rows.slice(start, start + 20)
  const hasMore = start + 20 < rows.length
  return { orders, nextCursor: hasMore ? String(start + 20) : null, hasMore }
}
