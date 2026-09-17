import {test,expect} from '@playwright/test'
const state=(status='running',revision=1)=>({messages:[{role:'user',requestId:'owned-request',content:'找续航手机'}],cards:[],selection:null,checkout:null,
  run:{id:'owned-run',requestId:'owned-request',mode:'continuous',status,revision,nodes:[]}})
const frame=(workspace:unknown)=>`event: snapshot\ndata: ${JSON.stringify({type:'snapshot',workspace})}\n\n`
test('SSE progress retains explicit pause and continue with one question submission',async({page})=>{
  let current:ReturnType<typeof state>|null=null, subscriptions=0
  const writes:string[]=[]
  await page.route('**/api/**',async r=>{
    const path=new URL(r.request().url()).pathname
    let body:unknown={}
    if(path.endsWith('/me'))body={authenticated:true,username:'fixture',csrfToken:'csrf'}
    else if(path.endsWith('/capability'))body={enabled:true,paymentSimulationEnabled:false}
    else if(path.endsWith('/favorites'))body={products:[]}
    else if(path.endsWith('/control/events')) {
      subscriptions++
      expect(r.request().headers()['x-csrf-token']).toBe('csrf')
      expect(new URL(r.request().url()).searchParams.get('runId')).toBe('owned-run')
      if(current?.run.revision===3) {
        const complete={...state('completed',4),messages:[...state().messages,{role:'assistant',requestId:'owned-request',content:'已经完成资料核验，这是正式回答。'}]}
        return r.fulfill({contentType:'text/event-stream',body:frame(complete)+'event: settled\ndata: {"type":"settled"}\n\n'})
      }
      return r.fulfill({contentType:'text/event-stream',body:frame({...current,run:{...current!.run,nodes:[{kind:'tool',label:'search_products',outcome:'tool_succeeded',input:{query:'续航手机'}}]}})})
    } else if(path.endsWith('/run')){writes.push('run');current=state();body=current}
    else if(path.endsWith('/control/pause')){writes.push('pause');expect(r.request().postDataJSON()).toEqual({runId:'owned-run',revision:1});current=state('paused',2);body=current}
    else if(path.endsWith('/control/continue')){writes.push('continue');expect(r.request().postDataJSON()).toEqual({runId:'owned-run',revision:2});current=state('running',3);body=current}
    else if(path.endsWith('/workspace'))body=current??{messages:[],cards:[],selection:null,checkout:null}
    else if(path.endsWith('/conversations'))body={conversations:[],nextOffset:null}
    else if(r.request().method()!=='GET')throw new Error('Unexpected write '+path)
    await r.fulfill({contentType:'application/json',body:JSON.stringify(body)})
  })
  await page.goto('/')
  await page.getByLabel('告诉我你想找什么').fill('找续航手机')
  await page.getByRole('button',{name:'发送消息'}).click()
  await expect.poll(()=>subscriptions).toBeGreaterThan(0)
  await page.getByText(/实际执行过程/).click()
  await expect(page.getByRole('button',{name:/检索商品/})).toBeVisible()
  await expect(page.getByText('已经完成资料核验，这是正式回答。')).toHaveCount(0)
  await page.getByRole('button',{name:'停止输出',exact:true}).click()
  await expect(page.getByRole('button',{name:'从原检查点继续'})).toBeVisible()
  await page.getByRole('button',{name:'从原检查点继续'}).click()
  await expect(page.getByText('已经完成资料核验，这是正式回答。',{exact:true})).toBeVisible()
  await expect(page.getByRole('button',{name:'停止输出',exact:true})).toHaveCount(0)
  expect(writes).toEqual(['run','pause','continue'])
  await page.screenshot({path:'test-results/sse-completed.png',fullPage:true})
})

test('SSE session rejection hides private workspace and asks for login',async({page})=>{
  await page.route('**/api/**',async r=>{
    const p=new URL(r.request().url()).pathname
    if(p.endsWith('/control/events'))return r.fulfill({status:403,contentType:'application/json',body:'{"detail":"csrf validation failed"}'})
    const body=p.endsWith('/me')?{authenticated:true,username:'fixture',csrfToken:'csrf'}:p.endsWith('/workspace')?
      (r.request().headers()['x-csrf-token']==='csrf'?state():{messages:[],cards:[],selection:null,checkout:null,csrfToken:'guest'}):{products:[]}
    return r.fulfill({contentType:'application/json',body:JSON.stringify(body)})
  })
  await page.goto('/')
  await expect(page.getByRole('dialog')).toBeVisible()
  await expect(page.locator('.account-name')).toHaveCount(0)
  await expect(page.getByText('找续航手机',{exact:true})).toHaveCount(0)
})
