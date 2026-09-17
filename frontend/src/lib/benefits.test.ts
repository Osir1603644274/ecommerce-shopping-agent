import { describe, expect, it, vi, afterEach } from 'vitest'
import { campaignInstant, campaignState } from './benefits'
import { api } from './api'
const campaign={id:'2',title:'test',status:'ACTIVE',salePriceMinor:9900,availableStock:50,startsAt:'2026-09-01T15:02:58',endsAt:'2026-09-01T15:33:58'}
afterEach(()=>vi.unstubAllGlobals())
describe('campaign eligibility',()=>{
  it('uses UTC and rejects expired ACTIVE campaigns',()=>{
    expect(campaignInstant(campaign.startsAt)).toBe(Date.parse('2026-09-01T15:02:58Z'))
    expect(campaignState(campaign,Date.parse('2026-09-16T00:00:00Z')).label).toBe('已结束')
    expect(campaignState(campaign,Date.parse(campaign.endsAt+'Z')).available).toBe(false)
    expect(campaignState(campaign,Date.parse(campaign.startsAt+'Z')).available).toBe(true)
    expect(campaignState({...campaign,endsAt:undefined}).available).toBe(false)
  })
  it('preserves business 422 details',async()=>{
    vi.stubGlobal('fetch',vi.fn().mockResolvedValue(new Response(JSON.stringify({detail:'秒杀活动已经结束'}),{status:422})))
    await expect(api('/workspace/benefits/2/purchase')).rejects.toThrow('秒杀活动已经结束')
  })
})
