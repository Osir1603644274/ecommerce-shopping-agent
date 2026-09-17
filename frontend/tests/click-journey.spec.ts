import {test,expect} from '@playwright/test'
const event=(name:string,extra:Record<string,unknown>={})=>({name,kind:'method',outcome:'returned',affectedRows:null,input:{},output:{},...extra})
const product={id:123,title:'二手手机测试款',brand:'TEST',priceMinor:69300,currency:'CNY',available:10,purchasable:true}
const calls=[
  {path:'/api/products/:id',method:'GET',status:200,detail:{droppedEvents:0,events:[event('ProductController.get',{input:{id:'123'},output:{type:'ResponseEntity'}}),event('ProductService.get'),event('商品缓存',{kind:'cache',outcome:'hit'})]}},
  {path:'/api/products/:id/offer',method:'GET',status:200,detail:{droppedEvents:0,events:[event('ProductOfferController.get'),event('ProductMapper.findById [SELECT]',{kind:'sql',outcome:'executed'}),event('LocalOfferService.find')]}},
  {path:'/api/identity/me',method:'GET',status:200,detail:{droppedEvents:0,events:[event('com.example.locallife.identity.IdentityController.me')]}},
  {path:'/api/orders/preview',method:'POST',status:200,detail:{droppedEvents:0,events:[
    event('OrderController.preview'),event('OrderService.preview',{input:{request:{itemId:'123',quantity:1}},source:{file:'fixture/OrderService.java',line:143,snippet:'143: public OrderPreviewResponse preview(CreateOrderRequest request, String userId) {\n144: // UI fixture only\n145: }'}}),
    event('CommerceCatalogReadService.requireItem'),event('CouponService.quote',{output:{discountMinor:0}}),event('InventoryService.getStock'),event('InventoryMapper.findStock [SELECT]',{kind:'sql',outcome:'executed'}),
  ]}},
]
for(const mobile of [false,true]) test(`one click explains four Java calls ${mobile?'mobile':'desktop'}`,async({page})=>{
  await page.setViewportSize(mobile?{width:390,height:844}:{width:1440,height:1000})
  let selections=0,previews=0,unexpectedWrites=0
  const state={csrfToken:'fixture',messages:[{role:'assistant',content:'测试商品',requestId:'fixture',cards:[product]}],cards:[product],selection:null as unknown,checkout:null}
  await page.route('**/api/**',async route=>{
    const path=new URL(route.request().url()).pathname
    let body:unknown={},headers={}
    if(path.endsWith('/me'))body={authenticated:true,username:'fixture',csrfToken:'fixture'}
    else if(path.endsWith('/capability'))body={enabled:true,paymentSimulationEnabled:false}
    else if(path.endsWith('/favorites'))body={products:[]}
    else if(path.endsWith('/backend-traces/preview'))body={scope:'测试夹具，不是真实交易证据',calls}
    else if(path.endsWith('/backend-traces/selection'))body={scope:'测试夹具',calls:calls.slice(0,2)}
    else if(path.endsWith('/selection')){selections++;state.selection={product,quantity:1};body=state;headers={'X-Backend-Trace-Ticket':'selection'}}
    else if(path.endsWith('/preview')){previews++;body=state;headers={'X-Backend-Trace-Ticket':'preview'}}
    else if(path.endsWith('/workspace'))body=state
    else if(route.request().method()==='POST')unexpectedWrites++
    await route.fulfill({contentType:'application/json',headers,body:JSON.stringify(body)})
  })
  await page.goto('/')
  await page.getByRole('button',{name:'后端执行详情',exact:true}).click()
  await page.getByRole('checkbox',{name:'记录 Java 后端执行详情'}).check()
  await page.getByRole('button',{name:'关闭后端执行详情'}).click()
  await page.getByRole('button',{name:'查看商品',exact:true}).click()
  await page.getByRole('button',{name:'关闭商品与交易'}).click()
  await page.getByRole('button',{name:'后端执行详情',exact:true}).click()
  await page.locator('.backend-request-list button').first().click()
  await expect(page.getByRole('navigation',{name:'这次点击涉及的后台环节'}).getByRole('button')).toHaveCount(2)
  await expect(page.locator('.learning-mermaid svg')).toHaveCount(1)
  await page.getByRole('button',{name:'关闭后端执行详情'}).click()
  await page.getByRole('button',{name:'打开商品与交易'}).click()
  await page.getByRole('button',{name:'预览订单',exact:true}).click()
  await page.getByRole('button',{name:'关闭商品与交易'}).click()
  await page.getByRole('button',{name:'后端执行详情',exact:true}).click()
  await page.locator('.backend-request-list button').first().click()
  const nav=page.getByRole('navigation',{name:'这次点击涉及的后台环节'})
  await expect(nav.getByRole('button')).toHaveCount(4)
  await expect(nav).toContainText('不是多次下单')
  await expect(nav).toContainText('旧记录若含商品查询，仍按原调用展示')
  await expect(page.locator('.backend-call')).toHaveCount(1)
  await expect(page.locator('.learning-mermaid svg')).toHaveCount(1)
  await nav.getByRole('button',{name:/核实本次交易/}).click()
  await expect(page.getByRole('region',{name:'后台步骤说明'})).toContainText('不是重新登录')
  await page.getByText('查看这一步的代码和参数',{exact:true}).click()
  await page.evaluate(()=>(document.activeElement as HTMLElement)?.blur())
  const reference=page.getByRole('region',{name:'工作区参考代码'})
  await expect(reference).toContainText('尚未确认它与本次运行版本一致')
  await expect(reference.locator('pre')).toContainText('authentication.getAuthorities()')
  await nav.getByRole('button',{name:/试算本单金额/}).click()
  const graph=page.locator('.learning-mermaid svg')
  await expect(graph.getByRole('button',{name:'试算金额并检查库存够不够'})).toBeVisible()
  await graph.getByRole('button',{name:'试算金额并检查库存够不够'}).click()
  const panel=page.getByRole('region',{name:'后台步骤说明'})
  await expect(panel).toContainText('商品 123，数量 1 件')
  await expect(panel).toContainText('不会创建订单或预占库存')
  await expect(page.getByText('执行业务处理',{exact:true})).toHaveCount(0)
  await expect(panel.getByText('试算金额并检查库存够不够',{exact:true})).toHaveCount(1)
  await expect(panel.locator('pre:visible')).toHaveCount(0)
  await page.getByText('查看这一步的代码和参数',{exact:true}).click()
  await expect(panel.locator('pre').first()).toContainText('OrderPreviewResponse preview')
  expect([selections,previews,unexpectedWrites]).toEqual([1,1,0])
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true)
  await page.getByText('查看这一步的代码和参数',{exact:true}).click()
  await page.locator('.backend-journey').scrollIntoViewIfNeeded()
  await page.locator('#backend-observer').screenshot({path:`test-results/click-journey-${mobile?'mobile':'desktop'}.png`})
})

for(const malformed of [false,true]) test(`missing or malformed trace is explicit ${malformed}`,async({page})=>{
  await page.route('**/api/**',async r=>{
    const p=new URL(r.request().url()).pathname
    const body=p.endsWith('/me')?{authenticated:true,username:'fixture',csrfToken:'fixture'}:p.endsWith('/backend-traces/missing')?{calls:[{path:'/api/products/:id',method:'GET',status:200,detail:null}],scope:'fixture'}:p.endsWith('/workspace')?{messages:[],cards:[],selection:null,checkout:null}:p.endsWith('/benefits')?{coupons:[],campaigns:[],purchases:[]}:{products:[]}
    await r.fulfill({contentType:'application/json',headers:p.endsWith('/benefits')?{'X-Backend-Trace-Ticket':'missing'}:{},body:JSON.stringify(malformed && p.endsWith('/backend-traces/missing') ? {} : body)})
  })
  await page.goto('/')
  await page.getByRole('button',{name:'后端执行详情',exact:true}).click()
  await page.getByRole('checkbox',{name:'记录 Java 后端执行详情'}).check()
  await page.getByRole('button',{name:'关闭后端执行详情'}).click()
  await page.getByRole('button',{name:'优惠与抢购',exact:true}).click()
  await page.getByRole('button',{name:'后端执行详情',exact:true}).click()
  await page.locator('.backend-request-list button').first().click()
  await expect(page.getByText(malformed ? /后端记录格式不完整/ : 'Java 未提供详情或记录已过期。')).toBeVisible()
  await expect(page.locator('.learning-mermaid svg')).toHaveCount(0)
})
