import { describe, expect, it } from 'vitest'
import { parseWorkspace } from './workspace'

const card = {id:123,title:'phone',brand:'brand',currency:'CNY',priceMinor:100,available:2,purchasable:true}
const state = {messages:[{role:'assistant',content:'hello',requestId:'id',cards:[card]}],cards:[card],selection:{product:card,quantity:1},checkout:null}
const proposal = {action:'create_order',confirmationId:'cfm-abcdefghijkl',expiresAt:'2099-01-01T00:00:00Z',preview:{title:'phone',quantity:1,payableMinor:100}}

describe('workspace boundary', () => {
  it('preserves large Java product ids without numeric rounding', () => {
    const id = '6806929769710081737'
    expect(parseWorkspace({...state, cards:[{...card,id}]}).cards[0].id).toBe(id)
    expect(() => parseWorkspace({...state, cards:[{...card,id:Number(id)}]})).toThrow()
  })
  it('accepts hydrated products and a confirmed receipt', () => {
    expect(parseWorkspace(state).selection?.product.id).toBe(123)
    expect(parseWorkspace({...state,checkout:{proposal,pending:false,outcome:{result:{id:'order-1'}}}}).checkout?.outcome?.result?.id).toBe('order-1')
  })
  it.each([
    {...state,messages:[{role:'system',content:'wrong',requestId:'id'}]},
    {...state,cards:[{...card,id:'not-an-id'}]},
    {...state,cards:[{...card,priceMinor:-1}]},
    {...state,selection:{product:card,quantity:0}},
    {...state,checkout:{proposal:{...proposal,action:'delete_order'},pending:false,outcome:null}},
    {...state,checkout:{proposal:{...proposal,preview:{}},pending:false,outcome:null}},
    {...state,checkout:{proposal,pending:'false',outcome:null}},
    {...state,checkout:{proposal,pending:false,outcome:{result:{}}}},
    {...state,checkout:{proposal,pending:false,outcome:{result:{id:''}}}},
  ])('rejects malformed state rather than showing fake success', value => {
    expect(() => parseWorkspace(value)).toThrow('购物空间数据格式不正确')
  })
  it('validates payment amount from the owned order', () => {
    const payment = {...proposal,action:'create_payment',preview:{order:{id:'order-1',payableMinor:100,items:[{titleSnapshot:'phone',quantity:1}]}}}
    expect(parseWorkspace({...state,checkout:{proposal:payment,pending:false,outcome:null}}).checkout).not.toBeNull()
  })
  it('validates execution records and rejects unsafe evidence links', () => {
    const run = {id:'run',requestId:'req',revision:1,mode:'continuous',status:'paused',nodes:[{label:'executor',detail:{count:3}}]}
    expect(parseWorkspace({...state,run}).run?.status).toBe('paused')
    expect(() => parseWorkspace({...state,run:{...run,nodes:{}}})).toThrow()
    expect(() => parseWorkspace({...state,run:{...run,nodes:[{label:'executor',source:{snippet:{bad:true}}}]}})).toThrow()
    expect(() => parseWorkspace({...state,run:{...run,nodes:[{label:'executor',startedAt:{bad:true}}]}})).toThrow()
    expect(() => parseWorkspace({...state,run:{...run,revision:-1}})).toThrow()
    const evidence = {id:'e',model:'vivo:y35',text:'5000mAh',url:'https://example.org/spec'}
    expect(parseWorkspace({...state,cards:[{...card,evidence:[evidence]}]}).cards[0].evidence).toHaveLength(1)
    for(const url of ['javascript:alert(1)','https://user:secret@example.org/spec']) {
      expect(() => parseWorkspace({...state,cards:[{...card,evidence:[{...evidence,url}]}]})).toThrow()
    }
  })
})
