import { test, expect } from '@playwright/test'
import type { Workspace } from '../src/lib/workspace'

test('one composer submits clarification; stopped run can be replaced without an extra form', async ({page}) => {
  let state: Workspace = {conversationId:'a'.repeat(32),messages:[{role:'user',requestId:'initial',content:'有没有5000以下的？'}],cards:[],selection:null,checkout:null,
    run:{id:'r',requestId:'initial',mode:'continuous',status:'clarification',revision:1,nodes:[],notice:'你想找手机还是电脑？'}}
  const writes:string[]=[]
  await page.route('**/api/**',async r=>{
    const path=new URL(r.request().url()).pathname
    let body:unknown=state
    if(path.endsWith('/me'))body={authenticated:true,username:'fixture',csrfToken:'csrf'}
    else if(path.endsWith('/favorites'))body={products:[]}
    else if(path.endsWith('/capability'))body={enabled:true}
    else if(path.endsWith('/conversations'))body={conversations:[],nextOffset:null}
    else if(path.endsWith('/control/events'))return r.fulfill({contentType:'text/event-stream',body:'event: snapshot\ndata: '+JSON.stringify({type:'snapshot',workspace:state})+'\n\nevent: settled\ndata: {"type":"settled"}\n\n'})
    else if(path.endsWith('/control/continue')){
      writes.push('continue');const input=r.request().postDataJSON();expect(input.answer).toBe('手机，6000以下')
      state={...state,messages:[...state.messages,{role:'assistant',requestId:'question',content:state.run!.notice!},{role:'user',requestId:input.requestId,content:input.answer}],
        run:{...state.run!,status:'running',revision:2}};body=state
    } else if(path.endsWith('/control/pause')){
      writes.push('pause');state={...state,run:{...state.run!,status:'paused',revision:3},answerStream:{runId:'r',requestId:'initial',text:'目前找到这些手机。',sequence:1,status:'paused'}};body=state
    } else if(path.endsWith('/control/end')){
      writes.push('end');state={...state,messages:[...state.messages,{role:'assistant',requestId:'initial',content:'目前找到这些手机。\n\n已停止生成'}],answerStream:null,run:{...state.run!,status:'ended',revision:4}};body=state
    } else if(path.endsWith('/run')){
      writes.push('run');expect(r.request().postDataJSON().message).toBe('改成3000以内')
      state={...state,messages:[...state.messages,{role:'user',requestId:r.request().postDataJSON().requestId,content:'改成3000以内'}],run:null};body=state
    } else if(r.request().method()!=='GET')throw new Error('Unexpected write: '+path)
    await r.fulfill({contentType:'application/json',body:JSON.stringify(body)})
  })
  await page.goto('/')
  await expect(page.locator('textarea')).toHaveCount(1)
  await expect(page.getByText('你想找手机还是电脑？',{exact:true})).toBeVisible()
  await page.screenshot({path:'test-results/clarification-one-composer.png',fullPage:true})
  const input=page.getByLabel('告诉我你想找什么')
  await input.fill('手机，6000以下');await input.press('Enter')
  await expect(page.getByRole('button',{name:'停止输出'})).toBeVisible()
  await input.fill('改成3000以内')
  await page.getByRole('button',{name:'停止输出'}).click()
  await expect(page.getByRole('button',{name:'发送消息'})).toBeEnabled()
  await input.press('Enter')
  await expect.poll(()=>writes).toEqual(['continue','pause','end','run'])
  await expect(input).toHaveValue('')
  await expect(page.getByText('目前找到这些手机。',{exact:false})).toBeVisible()
  await expect(page.locator('textarea')).toHaveCount(1)
  await page.setViewportSize({width:390,height:844})
  await expect(input).toBeVisible()
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true)
  await page.screenshot({path:'test-results/stop-send-mobile.png',fullPage:true})
})

for (const observer of [false,true]) test(`expired campaign disabled; rejection survives refresh (observer=${observer})`,async({page})=>{
  let posts=0,gets=0
  const campaigns=[{id:'2',title:'已经过期的活动',status:'ACTIVE',salePriceMinor:9900,availableStock:50,startsAt:'2026-09-01T15:02:58',endsAt:'2026-09-01T15:33:58'},
    {id:'3',title:'可点击的测试活动',status:'ACTIVE',salePriceMinor:9900,availableStock:2,startsAt:'2020-01-01T00:00:00',endsAt:'2099-01-01T00:00:00'}]
  await page.route('**/api/**',async r=>{
    const path=new URL(r.request().url()).pathname
    let body:unknown={messages:[],cards:[],selection:null,checkout:null}
    if(path.endsWith('/me'))body={authenticated:true,username:'fixture',csrfToken:'csrf'}
    else if(path.endsWith('/favorites'))body={products:[]}
    else if(path.endsWith('/conversations'))body={conversations:[],nextOffset:null}
    else if(path.endsWith('/benefits')){gets++;body={campaigns,coupons:[],purchases:[]}}
    else if(path.endsWith('/purchase')){posts++;return r.fulfill({status:422,contentType:'application/json',body:JSON.stringify({detail:'秒杀活动已经结束'})})}
    else if(r.request().method()!=='GET')throw new Error('Unexpected write: '+path)
    await r.fulfill({contentType:'application/json',body:JSON.stringify(body)})
  })
  await page.goto('/')
  if(observer){await page.getByRole('button',{name:'后端执行详情',exact:true}).click();await page.getByRole('checkbox',{name:'记录 Java 后端执行详情'}).check();await page.getByRole('button',{name:'关闭后端执行详情'}).click()}
  await page.getByRole('button',{name:'优惠与抢购',exact:true}).click()
  const expired=page.locator('article').filter({has:page.getByText('已经过期的活动',{exact:true})})
  await expect(expired.getByRole('button')).toBeDisabled()
  await expect(expired).toContainText('已结束')
  await page.locator('article').filter({has:page.getByText('可点击的测试活动',{exact:true})}).getByRole('button').click()
  await expect(page.getByRole('region',{name:'确认抢购'})).toBeInViewport()
  expect(posts).toBe(0)
  await page.getByRole('button',{name:'确认参加抢购',exact:true}).click()
  await expect(page.getByRole('alert')).toContainText('秒杀活动已经结束')
  await expect.poll(()=>gets).toBeGreaterThan(1)
  await page.getByRole('button',{name:'刷新我的权益与抢购'}).click()
  await expect.poll(()=>gets).toBeGreaterThan(2)
  await expect(page.getByRole('alert')).toContainText('秒杀活动已经结束')
  expect(posts).toBe(1)
  await page.screenshot({path:`test-results/benefits-error-${observer}.png`,fullPage:true})
})

test('old empty archive is explicit, real messages remain readable after reload',async({page})=>{
  const old='c'.repeat(32), empty='d'.repeat(32)
  const messages=[{role:'user' as const,requestId:'x',content:'我要一部手机'},{role:'assistant' as const,requestId:'x',content:'预算是多少？'}]
  let state:Workspace={messages:[],cards:[],selection:null,checkout:null,conversationId:'e'.repeat(32)}
  await page.route('**/api/**',async r=>{
    const path=new URL(r.request().url()).pathname
    let body:unknown=state
    if(path.endsWith('/me'))body={authenticated:true,username:'fixture',csrfToken:'csrf'}
    else if(path.endsWith('/favorites'))body={products:[]}
    else if(path.endsWith('/conversations'))body={conversations:[{id:old,title:'先前的对话'},{id:empty,title:'旧版空记录'}],nextOffset:null}
    else if(path.endsWith('/conversations/'+old))body={id:old,title:'先前的对话',messages}
    else if(path.endsWith('/conversations/'+empty))body={id:empty,title:'旧版空记录',messages:[]}
    else if(path.endsWith('/activate')){
      expect(r.request().postDataJSON().expectedConversationId).toBe(state.conversationId)
      const id=path.split('/').at(-2)!
      state={...state,conversationId:id,messages:id===old?messages:[]};body=state
    } else if(r.request().method()!=='GET')throw new Error('Unexpected mutation '+path)
    await r.fulfill({contentType:'application/json',body:JSON.stringify(body)})
  })
  await page.goto('/')
  await page.getByRole('button',{name:'旧版空记录'}).click()
  await expect(page.getByLabel('告诉我你想找什么')).toBeEnabled()
  await expect(page.getByText('我要一部手机',{exact:true})).toHaveCount(0)
  await page.getByRole('button',{name:'先前的对话'}).click()
  await expect(page.getByText('我要一部手机',{exact:true})).toBeVisible()
  await page.reload();await page.getByRole('button',{name:'先前的对话'}).click()
  await expect(page.getByText('预算是多少？',{exact:true})).toBeVisible()
  await page.screenshot({path:'test-results/history-readable.png',fullPage:true})
})

test('accepted purchase is visibly pending then read back without submitting again',async({page})=>{
  let posts=0, readsAfterSubmit=0
  await page.route('**/api/**',async r=>{
    const path=new URL(r.request().url()).pathname
    let body:unknown={messages:[],cards:[],selection:null,checkout:null}
    if(path.endsWith('/me'))body={authenticated:true,username:'fixture',csrfToken:'csrf'}
    else if(path.endsWith('/favorites'))body={products:[]}
    else if(path.endsWith('/conversations'))body={conversations:[],nextOffset:null}
    else if(path.endsWith('/benefits')){
      if(posts)readsAfterSubmit++
      body={campaigns:[{id:'3',title:'回执测试',status:'ACTIVE',salePriceMinor:9900,availableStock:2,startsAt:'2020-01-01T00:00:00',endsAt:'2099-01-01T00:00:00'}],coupons:[],
        purchases:readsAfterSubmit>=2?[{id:'accepted-one',campaignId:'3',status:'CREATED',amountMinor:9900}]:[]}
    } else if(path.endsWith('/purchase')){posts++;body={orderId:'accepted-one',status:'QUEUED'}}
    else if(r.request().method()!=='GET')throw new Error('Unexpected write: '+path)
    await r.fulfill({contentType:'application/json',body:JSON.stringify(body)})
  })
  await page.goto('/')
  await page.getByRole('button',{name:'优惠与抢购',exact:true}).click()
  await page.getByRole('button',{name:'参加抢购',exact:true}).click()
  await page.getByRole('button',{name:'确认参加抢购',exact:true}).click()
  await expect(page.getByText(/受理单 accepted-one 正在等待落单/)).toBeVisible()
  await expect(page.locator('article').filter({has:page.getByText('订单 accepted-one',{exact:true})})).toContainText('已创建',{timeout:10000})
  await expect(page.getByText(/受理单 accepted-one 正在等待落单/)).toHaveCount(0)
  expect(posts).toBe(1)
})
