import { test, expect } from '@playwright/test'
import type { Workspace } from '../src/lib/workspace'

const state:Workspace={messages:[],cards:[],selection:null,checkout:null,csrfToken:'test-csrf',run:{
  id:'catalog-run-test',requestId:'catalog-step-test',revision:2,mode:'step',status:'waiting',nextStage:'检索两个商品来源',
  nodes:[{id:'prepare-test',label:'整理当前需求',outcome:'completed',
    detail:{workflow:'catalog_workspace_v1',phase:'prepare',action:'search',executionMode:'fixed_catalog_workflow',purpose:'模型解释需求，程序保存状态。'},
    input:{'用户原话':'无糖可乐 888ml','更新前条件':[]},
    output:{'完整需求':'无糖可乐 888ml','当前条件':[{'属性':'容量','要求':'888ml','强度':'必须'}],'下一步':'检索两个来源'},
    source:{file:'agent/app/catalog_conversation.py',function:'transition',line:180}}]}}

for (const mobile of [false,true]) test(`catalog step exposes real parameters and honest architecture ${mobile?'mobile':'desktop'}`,async({page})=>{
  await page.setViewportSize(mobile?{width:390,height:844}:{width:1440,height:1100})
  await page.route('**/api/commerce-demo/**',async route=>{
    const path=new URL(route.request().url()).pathname
    if(path.endsWith('/me'))return route.fulfill({status:401,json:{detail:'authentication required'}})
    if(path.endsWith('/capability'))return route.fulfill({json:{enabled:true}})
    return route.fulfill({json:state})
  })
  await page.goto('/')
  await expect(page.getByRole('region',{name:'普通商品执行架构'})).toBeVisible()
  await expect(page.locator('.catalog-architecture svg')).toBeVisible()
  await page.locator('.catalog-architecture').screenshot({path:`D:/agent-datasets/catalog-multiturn-repair-20260914-v1/catalog-diagram-${mobile?'mobile':'desktop'}.png`})
  await expect(page.getByText('尚无工具返回后再由模型自由规划的 ReAct 循环。',{exact:false})).toBeVisible()
  await page.getByRole('button',{name:/整理当前需求/}).click()
  const detail=page.getByRole('region',{name:'节点详情'})
  await expect(detail).toContainText('无糖可乐 888ml')
  await expect(detail).toContainText('容量')
  await expect(detail.getByText('未记录可公开字段')).toHaveCount(0)
  await expect(detail.locator('.flow-technical pre')).not.toBeVisible()
  await detail.getByText('技术详情',{exact:true}).click()
  await expect(detail.locator('.flow-technical pre')).toContainText('catalog_workspace_v1')
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true)
  await page.screenshot({path:`D:/agent-datasets/catalog-multiturn-repair-20260914-v1/catalog-step-${mobile?'mobile':'desktop'}.png`,fullPage:true})
})
