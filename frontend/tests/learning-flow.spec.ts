import { test, expect } from '@playwright/test'
const event=(name:string, id:string, parentId:string|null=null, extra:Record<string,unknown>={})=>({name,id,parentId,kind:name.includes('Mapper')?'sql':'method',outcome:name.includes('Mapper')?'executed':'returned',affectedRows:1,
  input:{},output:{},source:{file:'backend/src/main/java/example.java',line:15,snippet:'15: service.create(request, user, key);'},...extra})
const events=[
  event('CartOrderController.create','entry',null,{input:{request:{items:[{itemId:'1710698',quantity:1,expectedUnitPriceMinor:69300}],expectedPayableMinor:69300}},output:{type:'ResponseEntity'}}),
  event('CartOrderService.create','service','entry'),
  event('数据库事务','tx','service',{kind:'transaction',outcome:'committed'}),
  event('OrderMapper.findByIdempotencyKey [SELECT]','duplicate','service',{affectedRows:0}),
  event('ProductMapper.findById [SELECT]','product','service'),
  event('LocalOfferService.find','quote','service'),
  event('InventoryService.getStock','stock-read','service'),
  event('CouponService.consume','coupon','service',{output:0}),
  event('OrderMapper.insertOrder [INSERT]','order','service'),
  event('InventoryService.reserve','reserve','service',{input:{itemId:'1710698',quantity:1}}),
  event('InventoryMapper.findStock [SELECT]','stock','reserve'),
  event('InventoryMapper.reserve [UPDATE]','update','reserve'),
  event('InventoryMapper.insertReservation [INSERT]','reservation','reserve'),
  event('OrderMapper.insertItem [INSERT]','item','service'),
  event('OutboxService.append','outbox','service'),
  event('OrderService.get','read','service',{output:{status:'PENDING_PAYMENT'}}),
]
for(const mobile of [false,true]) test(`Mermaid cart lesson ${mobile?'mobile':'desktop'}`,async({page})=>{
  await page.setViewportSize(mobile?{width:390,height:844}:{width:1440,height:1000})
  const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message))
  await page.route('**/api/**',async r=>{
    const path=new URL(r.request().url()).pathname
    let body:unknown={};let headers={}
    if(path.endsWith('/me'))body={authenticated:true,username:'fixture',csrfToken:'test'}
    else if(path.endsWith('/workspace'))body={messages:[],cards:[],selection:null,checkout:null,csrfToken:'test'}
    else if(path.endsWith('/benefits')){body={coupons:[],campaigns:[],purchases:[]};headers={'X-Backend-Trace-Ticket':'fixture'}}
    else if(path.endsWith('/backend-traces/fixture'))body={scope:'界面夹具，不是用户订单',calls:[{method:'POST',path:'/api/orders/cart',status:201,traceId:'fixture',detail:{events,droppedEvents:0}}]}
    else if(path.endsWith('/favorites'))body={products:[]}
    await r.fulfill({contentType:'application/json',headers,body:JSON.stringify(body)})
  })
  await page.goto('/')
  await page.getByRole('checkbox',{name:'记录 Java 后端执行详情'}).check()
  await page.getByRole('button',{name:'优惠与抢购',exact:true}).click()
  await page.locator('.backend-request-list button').filter({hasText:'已收到回复'}).first().click()
  const svg=page.locator('.learning-mermaid svg')
  await expect(svg).toBeVisible()
  await expect(svg.getByRole('button')).toHaveCount(8)
  const detail=page.getByRole('region',{name:'后台步骤说明'})
  await expect(detail).toContainText('1710698')
  await expect(detail).toContainText('693.00 元')
  await expect(detail.locator('pre:visible')).toHaveCount(0)
  await svg.getByRole('button',{name:'保存订单并预占库存'}).click()
  await detail.getByRole('button',{name:'把可用库存转成预占库存'}).click()
  await expect(detail).toContainText('条件更新命中')
  await detail.getByText('查看这一步的代码和参数',{exact:true}).click()
  await expect(detail.locator('pre').first()).toContainText('15: service.create')
  await svg.getByRole('button',{name:'登记待发送的订单事件'}).focus()
  await page.keyboard.press('Enter')
  await expect(detail).toContainText('不是 Kafka 消费完成')
  await svg.getByRole('button',{name:'确认数据库事务结果'}).click()
  await expect(detail).toContainText('不代表消息已消费或已经支付')
  expect(errors).toEqual([])
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true)
  const node=svg.getByRole('button').first()
  expect((await node.boundingBox())!.width).toBeGreaterThan(130)
  await svg.getByRole('button',{name:'接收并检查下单请求'}).click()
  await page.locator('.execution-learning').screenshot({path:`test-results/mermaid-lesson-${mobile?'mobile':'desktop'}.png`})
})
