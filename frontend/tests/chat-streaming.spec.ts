import {test,expect} from '@playwright/test'

test('answer content paints before EOF; draft stays editable; pause keeps text',async({page})=>{
  let run={id:'stream-test',requestId:'request-test',mode:'continuous',status:'running',revision:1,nodes:[]}
  let started=false,stopped=false
  const messages=[{role:'user',requestId:run.requestId,content:'推荐续航手机'}]
  const snapshot=()=>({conversationId:'a'.repeat(32),messages:started?messages:[],cards:[],selection:null,checkout:null,run:started?run:null,csrfToken:'csrf',
    ...(stopped?{answerStream:{runId:run.id,requestId:run.requestId,sequence:2,text:'已知电池容量，但没有续航实测。',status:'paused'}}:{})})
  await page.addInitScript(()=>{
    const original=window.fetch
    window.fetch=async(input,init)=>{
      if(String(input).includes('/control/events?'))return new Response(new ReadableStream({start(controller){
        (window as unknown as {emit:(data:unknown)=>void}).emit=data=>controller.enqueue(new TextEncoder().encode('data: '+JSON.stringify(data)+'\n\n'))
      }}),{headers:{'Content-Type':'text/event-stream'}})
      return original(input,init)
    }
  })
  await page.route('**/api/**',async route=>{
    const path=new URL(route.request().url()).pathname
    let body:unknown=snapshot()
    if(path.endsWith('/me'))body={authenticated:true,username:'fixture',csrfToken:'csrf'}
    else if(path.endsWith('/favorites'))body={products:[]}
    else if(path.endsWith('/capability'))body={enabled:true,paymentSimulationEnabled:false}
    else if(path.endsWith('/conversations'))body={conversations:[],nextOffset:null}
    else if(path.endsWith('/run')){started=true;body=snapshot()}
    else if(path.endsWith('/pause')){stopped=true;run={...run,status:'paused',revision:2};body=snapshot()}
    await route.fulfill({contentType:'application/json',body:JSON.stringify(body)})
  })
  await page.goto('/')
  const input=page.getByLabel('告诉我你想找什么')
  await input.fill('推荐续航手机');await input.press('Enter')
  await expect(page.getByRole('button',{name:'停止输出'})).toBeVisible()
  await expect.poll(()=>page.evaluate(()=>typeof (window as unknown as {emit:unknown}).emit)).toBe('function')
  const emit=async(data:unknown)=>page.evaluate(value=>(window as unknown as {emit:(v:unknown)=>void}).emit(value),data)
  await emit({type:'snapshot',workspace:snapshot()})
  await emit({type:'answer_delta',runId:run.id,requestId:run.requestId,sequence:1,replace:true,text:'已知电池容量',status:'generating'})
  await expect(page.locator('.chat-message.assistant')).toContainText('已知电池容量')
  await expect(page.getByRole('button',{name:'停止输出'})).toBeVisible()
  await input.fill('那拍照呢？');await expect(input).toHaveValue('那拍照呢？')
  await emit({type:'answer_delta',runId:run.id,requestId:run.requestId,sequence:2,replace:false,text:'，但没有续航实测。',status:'generating'})
  await expect(page.locator('.chat-message.assistant')).toContainText('已知电池容量，但没有续航实测。')
  await page.getByRole('button',{name:'停止输出'}).click()
  await expect(page.getByRole('button',{name:'从原检查点继续'})).toBeVisible()
  await expect(input).toHaveValue('那拍照呢？')
  await expect(page.locator('.chat-message.assistant')).toContainText('已知电池容量，但没有续航实测。')
  await page.screenshot({path:'test-results/chat-paused-desktop.png',fullPage:true})
  await page.setViewportSize({width:390,height:844})
  await expect(input).toBeVisible()
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true)
  await page.screenshot({path:'test-results/chat-paused-mobile.png',fullPage:true})
})
