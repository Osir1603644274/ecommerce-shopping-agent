import { test, expect } from '@playwright/test'
import type { Route } from '@playwright/test'
import type { Workspace } from '../src/lib/workspace'

const product = {id:123,title:'二手手机测试款',brand:'TEST',priceMinor:150000,currency:'CNY',available:10,purchasable:true}
const initial: Workspace = {messages:[],cards:[],selection:null,checkout:null,csrfToken:'guest-csrf'}
const json = (r:Route, body:unknown, status=200) => r.fulfill({status,contentType:'application/json',body:JSON.stringify(body)})

test('guest chats, selects a product, and navigates without losing context', async ({page}) => {
  let state = structuredClone(initial)
  let writes = 0
  await page.route('**/api/commerce-demo/**',async route => {
    const path = new URL(route.request().url()).pathname.replace('/api/commerce-demo','')
    if(path==='/me') return json(route,{detail:'authentication required'},401)
    if(path==='/capability') return json(route,{enabled:true,paymentSimulationEnabled:false})
    if(path==='/workspace') return json(route,state)
    if(path==='/workspace/run') {
      const body=route.request().postDataJSON()
      expect(Object.keys(body).sort()).toEqual(['message','mode','requestId'])
      expect(body.mode).toBe('continuous')
      state={...state,cards:[product],messages:[{role:'user',content:body.message,requestId:body.requestId},
        {role:'assistant',content:'这部手机可以考虑。',requestId:body.requestId,cards:[product]}]}
      return json(route,state)
    }
    if(path==='/workspace/selection') {state={...state,selection:{product,quantity:1}}; return json(route,state)}
    if(path.includes('confirm')) writes++
    return json(route,{},404)
  })
  await page.goto('/')
  await expect(page.getByRole('heading',{name:'今天想找什么？'})).toBeVisible()
  await page.getByLabel('告诉我你想找什么').fill('想找二手手机')
  await page.getByRole('button',{name:'发送消息'}).click()
  await expect(page.locator('[data-product-id="123"]')).toHaveCount(1)
  await page.getByRole('button',{name:'查看商品',exact:true}).click()
  await expect(page.getByRole('button',{name:'预览订单'})).toBeEnabled()
  await page.evaluate(() => window.scrollTo(0,0))
  await page.screenshot({path:'test-results/unified-mock-guide.png',fullPage:true})
  await page.getByRole('button',{name:'关闭商品与交易'}).click()
  await page.getByRole('button',{name:'我的收藏',exact:true}).click()
  await expect(page.getByRole('button',{name:'登录查看收藏'})).toBeVisible()
  await page.getByRole('button',{name:'AI 导购',exact:true}).click()
  await page.getByRole('button',{name:'打开商品与交易'}).click()
  await expect(page.getByRole('button',{name:'预览订单'})).toBeVisible()
  await page.reload()
  await expect(page.getByText('这部手机可以考虑。',{exact:true})).toBeVisible()
  await page.getByRole('button',{name:'打开商品与交易'}).click()
  await page.getByRole('button',{name:'预览订单'}).click()
  await expect(page.getByRole('dialog',{name:'登录账户'})).toBeVisible()
  expect(writes).toBe(0)
  expect(await page.evaluate(()=>[localStorage.length,sessionStorage.length])).toEqual([0,0])
})

test('unknown transaction shows reconcile instead of allowing another confirmation',async({page})=>{
  const state:Workspace={...initial,checkout:{pending:true,proposal:{action:'create_order',confirmationId:'cfm-abcdefghijkl',expiresAt:'2099-01-01T00:00:00Z',preview:{title:'二手手机测试款',quantity:1,payableMinor:150000}},outcome:{status:'unknown'}}}
  let confirms=0,reads=0
  await page.route('**/api/commerce-demo/**',async r=>{
    const p=new URL(r.request().url()).pathname
    if(p.endsWith('/me'))return json(r,{authenticated:true,username:'alice',csrfToken:'alice-csrf'})
    if(p.endsWith('/capability'))return json(r,{enabled:true})
    if(p.endsWith('/favorites'))return json(r,{products:[]})
    if(p.endsWith('/reconcile')){reads++;return json(r,state)}
    if(p.endsWith('/confirm'))confirms++
    return json(r,state)
  })
  await page.goto('/')
  await page.getByRole('button',{name:'打开商品与交易'}).click()
  await expect(page.getByRole('button',{name:'确认下单',exact:true})).toBeDisabled()
  await page.getByRole('button',{name:'关闭商品与交易'}).click()
  await page.getByLabel('告诉我你想找什么').fill('再买一个')
  await expect(page.getByRole('button',{name:'发送消息'})).toBeDisabled()
  await page.getByRole('button',{name:'打开商品与交易'}).click()
  await page.getByRole('button',{name:'回查结果'}).click()
  expect(confirms).toBe(0);expect(reads).toBe(1)
})

test('mobile unified navigation fits without horizontal overflow',async({page})=>{
  await page.setViewportSize({width:390,height:844})
  await page.route('**/api/commerce-demo/**',async r=>{
    if(r.request().url().endsWith('/me'))return json(r,{},401)
    return json(r,initial)
  })
  await page.goto('/')
  await page.getByRole('button',{name:'切换侧栏'}).click()
  await expect(page.getByRole('button',{name:'AI 导购',exact:true})).toBeVisible()
  const buttons=page.getByRole('navigation',{name:'主导航'}).getByRole('button')
  const boxes=await Promise.all([0,1,2].map(i=>buttons.nth(i).boundingBox()))
  expect(new Set(boxes.map(b=>b?.x)).size).toBe(1)
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true)
  await page.screenshot({path:'test-results/unified-mobile.png',fullPage:true})
})

test('saved product opens a revalidated selection instead of asking for another search', async ({page}) => {
  let state:Workspace={...initial}
  let selections=0, previews=0, chats=0
  const updated={...product,priceMinor:160000}
  await page.route('**/api/commerce-demo/**',async r=>{
    const path=new URL(r.request().url()).pathname
    if(path.endsWith('/me')) return json(r,{authenticated:true,username:'alice',csrfToken:'alice-csrf'})
    if(path.endsWith('/capability')) return json(r,{enabled:true})
    if(path.endsWith('/favorites')) return json(r,{products:[product]})
    if(path.endsWith('/run')) chats++
    if(path.endsWith('/selection')) {
      selections++
      expect(r.request().postDataJSON()).toEqual({productId:123,quantity:1})
      state={...state,selection:{product:updated,quantity:1}}
    }
    if(path.endsWith('/preview')) {
      previews++
      state={...state,checkout:{pending:false,proposal:{action:'create_order',confirmationId:'cfm-abcdefghijkl',expiresAt:'2099-01-01T00:00:00Z',preview:{title:updated.title,quantity:1,payableMinor:160000}},outcome:{status:'rejected',message:'商品价格已变化，请重新预览并确认'}}}
    }
    return json(r,state)
  })
  await page.goto('/#favorites')
  await page.getByRole('button',{name:'查看商品',exact:true}).click()
  await expect(page.locator('.checkout-card')).toContainText('1,600.00')
  await page.getByRole('button',{name:'预览订单',exact:true}).click()
  await expect(page.getByText('商品价格已变化，请重新预览并确认')).toBeVisible()
  await expect(page.getByRole('button',{name:'确认下单',exact:true})).toBeDisabled()
  await expect(page.getByRole('button',{name:'重新预览订单',exact:true})).toBeEnabled()
  expect([selections,previews,chats]).toEqual([1,1,0])
})

test('chat rejects a changed account cookie and clears private history',async({page})=>{
  const privateState:Workspace={...initial,messages:[{role:'user',content:'alice private wish',requestId:'abcdefghijklmnop'}]}
  await page.route('**/api/commerce-demo/**',async r=>{
    const p=new URL(r.request().url()).pathname
    if(p.endsWith('/me'))return json(r,{authenticated:true,username:'alice',csrfToken:'alice-csrf'})
    if(p.endsWith('/capability'))return json(r,{enabled:true})
    if(p.endsWith('/favorites'))return json(r,{products:[]})
    if(p.endsWith('/run'))return json(r,{detail:'csrf validation failed'},403)
    return json(r,r.request().headers()['x-csrf-token']==='alice-csrf' ? privateState : initial)
  })
  await page.goto('/')
  await expect(page.getByText('alice private wish')).toBeVisible()
  await page.getByLabel('告诉我你想找什么').fill('continue')
  await page.getByRole('button',{name:'发送消息'}).click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await expect(page.getByText('alice private wish')).toHaveCount(0)
  await expect(page.locator('.account-name')).toHaveCount(0)
})

test('comparison markdown is readable and inert on mobile', async ({page}) => {
  await page.setViewportSize({width:390,height:844})
  const state:Workspace={...initial,messages:[{role:'assistant',requestId:'markdown-qa',
    content:'### 商品对比\n\n**价格已经核对，成色仍需确认。**\n\n|维度|手机一|手机二|\n|---|---|---|\n|价格|1882.18元|1797.80元|\n|电池健康|未知|未知|\n\n[点此支付](https://example.com/pay)\n\n<img src=x onerror=alert(1)>'}]}
  await page.route('**/api/commerce-demo/**',async r=>{
    if(r.request().url().endsWith('/me')) return json(r,{},401)
    return json(r,state)
  })
  await page.goto('/')
  await expect(page.locator('.message-copy table')).toBeVisible()
  await expect(page.locator('.message-copy strong')).toContainText('价格已经核对')
  await expect(page.locator('.message-copy a, .message-copy img')).toHaveCount(0)
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true)
  await page.screenshot({path:'test-results/unified-mobile-markdown.png',fullPage:true})
})
