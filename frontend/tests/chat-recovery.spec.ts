import { test, expect } from '@playwright/test'
import type { Workspace } from '../src/lib/workspace'
const oldId='a'.repeat(32), freshId='b'.repeat(32)

for (const status of ['failed','interrupted','paused','clarification']) {
  test(`${status}: new conversation retires old run, preserves history, no fake resume`,async({page})=>{
    let state: Workspace={conversationId:oldId,messages:[{role:'user',requestId:'q',content:'苹果手机有吗？'}],
      cards:[],selection:null,checkout:null,run:{id:'r',requestId:'q',revision:1,status,mode:'continuous',nodes:[],canResume:false}}
    const writes:string[]=[]
    await page.route('**/api/**',async route=>{
      const path=new URL(route.request().url()).pathname
      let body:unknown=state
      if(path.endsWith('/me'))body={authenticated:true,username:'fixture',csrfToken:'csrf'}
      else if(path.endsWith('/favorites'))body={products:[]}
      else if(path.endsWith('/capability'))body={enabled:true}
      else if(path.endsWith('/control/end')){
        writes.push('end');expect(route.request().postDataJSON().runId).toBe('r')
        state={...state,run:{...state.run!,status:'ended',revision:2}};body=state
      } else if(path.endsWith('/conversations')){
        if(route.request().method()==='POST'){
          writes.push('new');expect(route.request().postDataJSON().expectedConversationId).toBe(oldId)
          state={...state,conversationId:freshId,messages:[],run:null};body=state
        } else body={conversations:[{id:oldId,title:'苹果手机有吗？'}],nextOffset:null}
      } else if(path.endsWith('/conversations/'+oldId))body={id:oldId,title:'苹果手机有吗？',messages:[{role:'user',requestId:'q',content:'苹果手机有吗？'}]}
      else if(route.request().method()!=='GET')throw new Error('Unexpected mutation '+path)
      await route.fulfill({contentType:'application/json',body:JSON.stringify(body)})
    })
    await page.goto('/')
    await expect(page.getByRole('button',{name:'从原检查点继续',exact:true})).toHaveCount(0)
    await expect(page.getByRole('button',{name:'新对话',exact:true})).toBeEnabled()
    await page.getByRole('button',{name:'新对话',exact:true}).click()
    await expect.poll(()=>writes).toEqual(['end','new'])
    await expect(page.getByRole('button',{name:'苹果手机有吗？',exact:true})).toBeVisible()
    await expect(page.getByLabel('告诉我你想找什么')).toBeEnabled()
    await page.screenshot({path:`test-results/new-conversation-${status}.png`,fullPage:true})
  })
}

for (const status of ['waiting', 'interrupted']) {
  test(`${status} step task: typed request retires the old task before sending`, async ({ page }) => {
    let state: Workspace = { conversationId: oldId, messages: [], cards: [], selection: null, checkout: null,
      run: { id: 'step-run', requestId: 'old-step', revision: 1, status, mode: 'step', nodes: [] } }
    const writes: string[] = []
    await page.route('**/api/**', async route => {
      const path = new URL(route.request().url()).pathname
      let body: unknown = state
      if (path.endsWith('/me')) body = { authenticated: true, username: 'fixture', csrfToken: 'csrf' }
      else if (path.endsWith('/favorites')) body = { products: [] }
      else if (path.endsWith('/capability')) body = { enabled: true }
      else if (path.endsWith('/conversations')) body = { conversations: [], nextOffset: null }
      else if (path.endsWith('/control/end')) {
        writes.push('end')
        state = { ...state, run: { ...state.run!, status: 'ended', revision: 2 } }
        body = state
      } else if (path.endsWith('/run')) {
        writes.push('run')
        expect(route.request().postDataJSON().message).toBe('换一个需求')
        state = { ...state, run: null, messages: [{ role: 'user', requestId: 'new', content: '换一个需求' }] }
        body = state
      } else if (route.request().method() !== 'GET') throw new Error('Unexpected mutation ' + path)
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) })
    })
    await page.goto('/')
    const input = page.getByLabel('告诉我你想找什么')
    await input.fill('换一个需求')
    await expect(page.getByRole('button', { name: '发送消息' })).toBeEnabled()
    await input.press('Enter')
    await expect.poll(() => writes).toEqual(['end', 'run'])
  })
}

test('new conversation safely pauses active output, then retires its run', async ({ page }) => {
  let state: Workspace = { conversationId: oldId, messages: [], cards: [], selection: null, checkout: null,
    run: { id: 'live-run', requestId: 'live', revision: 1, status: 'running', mode: 'continuous', nodes: [] } }
  const writes: string[] = []
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    let body: unknown = state
    if (path.endsWith('/me')) body = { authenticated: true, username: 'fixture', csrfToken: 'csrf' }
    else if (path.endsWith('/favorites')) body = { products: [] }
    else if (path.endsWith('/capability')) body = { enabled: true }
    else if (path.endsWith('/conversations')) {
      if (route.request().method() === 'POST') {
        writes.push('new')
        state = { ...state, conversationId: freshId, messages: [], run: null }
        body = state
      } else body = { conversations: [], nextOffset: null }
    } else if (path.endsWith('/control/pause')) {
      writes.push('pause')
      state = { ...state, run: { ...state.run!, status: 'paused', revision: 2 } }
      body = state
    } else if (path.endsWith('/control/end')) {
      writes.push('end')
      state = { ...state, run: { ...state.run!, status: 'ended', revision: 3 } }
      body = state
    } else if (path.endsWith('/control/events')) {
      return route.fulfill({ contentType: 'text/event-stream', body: 'event: settled\ndata: {"type":"settled"}\n\n' })
    } else if (route.request().method() !== 'GET') throw new Error('Unexpected mutation ' + path)
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) })
  })
  await page.goto('/')
  await expect(page.getByRole('button', { name: '新对话', exact: true })).toBeEnabled()
  await page.getByRole('button', { name: '新对话', exact: true }).click()
  await expect.poll(() => writes).toEqual(['pause', 'end', 'new'])
  await expect(page.getByLabel('告诉我你想找什么')).toBeEnabled()
})

test('server-confirmed checkpoint offers continuation',async({page})=>{
  const state:Workspace={conversationId:oldId,messages:[],cards:[],selection:null,checkout:null,
    run:{id:'r',requestId:'q',revision:1,status:'paused',mode:'continuous',nodes:[],canResume:true}}
  await page.route('**/api/**',async route=>{
    const path=new URL(route.request().url()).pathname
    const body=path.endsWith('/me')?{authenticated:true,username:'fixture',csrfToken:'csrf'}:
      path.endsWith('/favorites')?{products:[]}:path.endsWith('/conversations')?{conversations:[],nextOffset:null}:state
    await route.fulfill({contentType:'application/json',body:JSON.stringify(body)})
  })
  await page.goto('/')
  await expect(page.getByRole('button',{name:'从原检查点继续',exact:true})).toBeVisible()
})

test('history opens in the normal composer and selecting a saved card fetches current offer',async({page})=>{
  const card={id:'1',title:'历史苹果手机',brand:'Apple',priceMinor:10000,currency:'CNY',purchasable:true}
  const messages=[{role:'user' as const,requestId:'q',content:'苹果手机有吗？'},
    {role:'assistant' as const,requestId:'a',content:'可查看这件商品',cards:[card]}]
  let state:Workspace={conversationId:freshId,messages:[],cards:[],selection:null,checkout:null}
  const writes:string[]=[]
  await page.route('**/api/**',async route=>{
    const path=new URL(route.request().url()).pathname
    let body:unknown=state
    if(path.endsWith('/me'))body={authenticated:true,username:'fixture',csrfToken:'csrf'}
    else if(path.endsWith('/favorites'))body={products:[]}
    else if(path.endsWith('/conversations'))body={conversations:[{id:oldId,title:'苹果手机有吗？'}],nextOffset:null}
    else if(path.endsWith('/conversations/'+oldId))body={id:oldId,title:'苹果手机有吗？',messages}
    else if(path.endsWith('/activate')){
      writes.push('activate');state={...state,conversationId:oldId,messages,cards:[card]};body=state
    } else if(path.endsWith('/selection')){
      writes.push('selection');expect(route.request().postDataJSON().productId).toBe('1')
      state={...state,selection:{product:{...card,priceMinor:20000,available:2},quantity:1}};body=state
    } else if(path.endsWith('/run')){
      writes.push('run');expect(route.request().postDataJSON().message).toBe('续航怎么样？')
      state={...state,messages:[...state.messages,{role:'user',requestId:'new-q',content:'续航怎么样？'}]};body=state
    } else if(route.request().method()!=='GET')throw new Error('Unexpected write '+path)
    await route.fulfill({contentType:'application/json',body:JSON.stringify(body)})
  })
  await page.goto('/')
  await page.getByRole('button',{name:'苹果手机有吗？',exact:true}).click()
  const input=page.getByLabel('告诉我你想找什么')
  await expect(input).toBeEnabled()
  await page.getByRole('button',{name:'查看商品',exact:true}).click()
  await expect.poll(()=>writes).toEqual(['activate','selection'])
  await page.getByRole('button',{name:'关闭商品与交易',exact:true}).click()
  await input.fill('续航怎么样？');await input.press('Enter')
  await expect.poll(()=>writes).toEqual(['activate','selection','run'])
  await expect(page.getByText('续航怎么样？',{exact:true})).toBeVisible()
  await page.screenshot({path:'test-results/continued-history.png',fullPage:true})
})
