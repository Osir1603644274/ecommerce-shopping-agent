export type Campaign = { id: string; title: string; status: string; salePriceMinor: number; availableStock: number; startsAt?: string; endsAt?: string }

// Java's LocalDateTime campaign fields are stored in UTC. Never interpret
// timezone-less values as the browser's local time.
export function campaignInstant(value?: string): number {
  if (!value) return NaN
  return Date.parse(/[zZ]$|[+-]\d\d:\d\d$/.test(value) ? value : value + 'Z')
}
export function campaignState(campaign: Campaign, now = Date.now()) {
  if (campaign.status !== 'ACTIVE') return { available: false, label: ({ DRAFT: '未开始', ENDED: '已结束', CLOSED: '已关闭' } as Record<string,string>)[campaign.status] || '不可参加' }
  const start = campaignInstant(campaign.startsAt), end = campaignInstant(campaign.endsAt)
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return { available: false, label: '活动时间待核验' }
  if (now < start) return { available: false, label: '未开始' }
  if (now >= end) return { available: false, label: '已结束' }
  if (campaign.availableStock <= 0) return { available: false, label: '已抢完' }
  return { available: true, label: '进行中' }
}
