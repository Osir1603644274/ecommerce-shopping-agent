import { test, expect } from '@playwright/test'
for (const mobile of [false,true]) test(`clickable product explanation ${mobile?'mobile':'desktop'}`, async ({page}) => {
  if(mobile) await page.setViewportSize({width:390,height:844})
  const event=(id:string,parentId:string|null,name:string,kind:string,snippet:string)=>({id,parentId,name,kind,outcome:kind==='sql'?'executed':'returned',durationMs:2,affectedRows:1,
    source:{file:'backend/src/main/java/com/example/locallife/product/'+name.split('.')[0]+'.java',line:16,symbol:name,snippet},input:{id:'1133890'},output:{count:1}})
  const events=[
    {id:'http',parentId:null,kind:'http',name:'GET /api/products/:id/offer',outcome:'HTTP 200',durationMs:5,affectedRows:null},
    event('controller','http','ProductOfferController.get','method','16: var p=products.findById(id);\n17: var offer=offers.find(id);'),
    event('sql','controller','ProductMapper.findById [SELECT]','sql','16: @Select("SELECT * FROM product WHERE id = #{id}")\n17: Product findById(Long id);'),
    event('offer','controller','LocalOfferService.find','method','16: return jdbc.query("SELECT price_minor FROM product_local_offer WHERE product_id=?", mapper, id);'),
  ]
  await page.route('**/api/**',async r=>{
    const path=new URL(r.request().url()).pathname
    let body:unknown={}; let headers={}
    if(path.endsWith('/me'))body={authenticated:true,username:'fixture',csrfToken:'test'}
    else if(path.endsWith('/workspace'))body={messages:[],cards:[],selection:null,checkout:null,csrfToken:'test'}
    else if(path.endsWith('/benefits')) {body={coupons:[],campaigns:[],purchases:[]};headers={'X-Backend-Trace-Ticket':'fixture'}}
    else if(path.endsWith('/backend-traces/fixture'))body={scope:'界面测试夹具，并非线上记录',calls:[{method:'GET',path:'/api/products/:id/offer',status:200,traceId:'fixture',detail:{events,droppedEvents:0}}]}
    else if(path.endsWith('/favorites'))body={products:[]}
    await r.fulfill({contentType:'application/json',headers,body:JSON.stringify(body)})
  })
  await page.goto('/')
  await page.getByRole('checkbox',{name:'记录 Java 后端执行详情'}).check()
  await page.getByRole('button',{name:'优惠与抢购',exact:true}).click()
  await page.locator('.backend-request-list button').filter({hasText:'已收到回复'}).first().click()
  await expect(page.getByRole('heading',{name:'核对商品价格与库存'})).toBeVisible()
  const graph=page.locator('.learning-mermaid svg')
  await expect(graph.getByRole('button')).toHaveCount(3)
  await expect(graph.locator('path[marker-end]')).toHaveCount(2)
  await expect(page.getByText('GET /api/products/:id/offer',{exact:true})).not.toBeVisible()
  await expect(page.getByText(/事件 ID|上级调用|次 SQL 调用|开始时间/)).toHaveCount(0)
  await graph.getByRole('button',{name:/读取商品资料/}).click()
  await page.getByText('查看这一步的代码和参数',{exact:true}).click()
  await expect(page.getByRole('region',{name:'后台步骤说明'}).getByText(/@Select/)).toBeVisible()
  await expect(page.getByText('"id": "1133890"',{exact:false})).toBeVisible()
  await graph.getByRole('button',{name:/查找商品报价/}).click()
  await page.getByText('查看这一步的代码和参数',{exact:true}).click()
  await expect(page.getByRole('region',{name:'后台步骤说明'}).getByText(/product_local_offer/)).toBeVisible()
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true)
  await page.screenshot({path:`test-results/explainer-${mobile?'mobile':'desktop'}.png`,fullPage:true})
})
