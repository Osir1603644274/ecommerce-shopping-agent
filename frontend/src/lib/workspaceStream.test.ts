import {afterEach,describe,expect,it,vi} from 'vitest'
import {readWorkspaceEvents,watchWorkspace} from './workspaceStream'
const snapshot=(status='running',revision=1)=>({messages:[],cards:[],selection:null,checkout:null,run:{id:'owned',requestId:'r',mode:'continuous',status,revision,nodes:[]}})
const sse=(workspace:unknown)=>`event: snapshot\r\ndata: ${JSON.stringify({type:'snapshot',workspace})}\r\n\r\n`
afterEach(()=>{vi.unstubAllGlobals();vi.useRealTimers()})
describe('SSE is a subscription, never a second execution',()=>{
  it('renders real content deltas before EOF and deduplicates replayed sequences',async()=>{
    let pipe!:ReadableStreamDefaultController<Uint8Array>
    vi.stubGlobal('fetch',vi.fn(async()=>new Response(new ReadableStream({start(c){pipe=c}}),{headers:{'content-type':'text/event-stream'}})))
    const seen:import('./workspace').Workspace[]=[]
    const pending=readWorkspaceEvents('owned','csrf',new AbortController().signal,w=>seen.push(w))
    await vi.waitFor(()=>expect(pipe).toBeDefined())
    const frame=(sequence:number,text:string,replace=false)=>`data: ${JSON.stringify({type:'answer_delta',runId:'owned',requestId:'r',sequence,text,replace,status:'generating'})}\n\n`
    pipe.enqueue(new TextEncoder().encode(sse(snapshot())+frame(1,'先看',true)))
    await vi.waitFor(()=>expect(seen.at(-1)?.answerStream?.text).toBe('先看'))
    pipe.enqueue(new TextEncoder().encode(frame(2,'电池。')+frame(2,'电池。')))
    await vi.waitFor(()=>expect(seen.at(-1)?.answerStream?.text).toBe('先看电池。'))
    expect(seen.filter(s=>s.answerStream?.sequence===2)).toHaveLength(1)
    pipe.enqueue(new TextEncoder().encode(sse(snapshot('completed',2))+'data: {"type":"settled"}\n\n'))
    await pending
  })
  it('reads UTF8 across arbitrary byte boundaries and ignores heartbeats',async()=>{
    const value={...snapshot('completed',2),messages:[{role:'assistant',requestId:'r',content:'续航资料已核对'}]}
    const bytes=new TextEncoder().encode(': heartbeat\r\n\r\n'+sse(snapshot())+sse(value)+'event: settled\r\ndata: {"type":"settled"}\r\n\r\n')
    const fetcher=vi.fn(async()=>new Response(new ReadableStream({start(controller){for(const b of bytes)controller.enqueue(Uint8Array.of(b));controller.close()}}),{headers:{'content-type':'text/event-stream'}}))
    vi.stubGlobal('fetch',fetcher)
    const seen:unknown[]=[]
    await readWorkspaceEvents('owned','csrf',new AbortController().signal,w=>seen.push(w))
    expect(seen).toEqual([snapshot(),value])
    expect(fetcher.mock.calls).toHaveLength(1)
    const options=(fetcher.mock.calls as unknown[][])[0][1] as RequestInit
    expect(options.headers).toMatchObject({'X-CSRF-Token':'csrf'})
    expect(options.method).toBeUndefined()
  })
  it('publishes progress before the answer exists, not after buffering EOF',async()=>{
    let pipe!:ReadableStreamDefaultController<Uint8Array>
    const fetcher=vi.fn(async()=>new Response(new ReadableStream({start(c){pipe=c}}),{headers:{'content-type':'text/event-stream'}}))
    vi.stubGlobal('fetch',fetcher)
    const seen:unknown[]=[]
    const pending=readWorkspaceEvents('owned','csrf',new AbortController().signal,w=>seen.push(w))
    await vi.waitFor(()=>expect(pipe).toBeDefined())
    pipe.enqueue(new TextEncoder().encode(sse(snapshot())))
    await vi.waitFor(()=>expect(seen).toEqual([snapshot()]))
    pipe.enqueue(new TextEncoder().encode(sse(snapshot('paused',2))+'data: {"type":"settled"}\n\n'))
    await pending
    expect(seen).toHaveLength(2)
  })
  it('rejects another run and a premature settled marker',async()=>{
    vi.stubGlobal('fetch',vi.fn(async()=>new Response(sse({...snapshot(),run:{...snapshot().run,id:'other'}}),{headers:{'content-type':'text/event-stream'}})))
    await expect(readWorkspaceEvents('owned','csrf',new AbortController().signal,()=>{})).rejects.toMatchObject({status:409})
    vi.stubGlobal('fetch',vi.fn(async()=>new Response('data: {"type":"settled"}\n\n',{headers:{'content-type':'text/event-stream'}})))
    await expect(readWorkspaceEvents('owned','csrf',new AbortController().signal,()=>{})).rejects.toThrow('尚未收到明确状态')
  })
  it('on disconnect retries only GET subscription, never run or continue',async()=>{
    vi.useFakeTimers()
    let count=0
    const fetcher=vi.fn(async()=>new Response(++count===1?sse(snapshot()):sse(snapshot('completed',2))+'data: {"type":"settled"}\n\n',{headers:{'content-type':'text/event-stream'}}))
    vi.stubGlobal('fetch',fetcher)
    const pending=watchWorkspace('owned','csrf',new AbortController().signal,()=>{},()=>{})
    await vi.advanceTimersByTimeAsync(1100);await pending
    expect(fetcher).toHaveBeenCalledTimes(2)
    expect(fetcher.mock.calls.every(args=>String((args as unknown[])[0]).includes('/control/events?runId=owned'))).toBe(true)
  })
  it('falls back to read-only control when old backend has no SSE',async()=>{
    vi.useFakeTimers()
    const fetcher=vi.fn(async(url: string)=>url.includes('/events?')?new Response('{}',{status:404}):Response.json(snapshot('completed',2)))
    vi.stubGlobal('fetch',fetcher)
    const pending=watchWorkspace('owned','csrf',new AbortController().signal,()=>{},()=>{})
    await vi.advanceTimersByTimeAsync(1100);await pending
    expect(fetcher.mock.calls.map(args=>args[0])).toEqual(['/api/commerce-demo/workspace/control/events?runId=owned','/api/commerce-demo/workspace/control'])
  })
  it('authentication failure closes instead of silently retrying',async()=>{
    const fetcher=vi.fn(async()=>new Response('{}',{status:403}));vi.stubGlobal('fetch',fetcher)
    await expect(watchWorkspace('owned','csrf',new AbortController().signal,()=>{},()=>{})).rejects.toMatchObject({status:403})
    expect(fetcher).toHaveBeenCalledTimes(1)
  })
  it('planned connection rotation reconnects quietly',async()=>{
    let count=0
    const fetcher=vi.fn(async()=>new Response(++count===1?sse(snapshot())+'data: {"type":"reconnect"}\n\n':sse(snapshot('completed',2))+'data: {"type":"settled"}\n\n',{headers:{'content-type':'text/event-stream'}}))
    vi.stubGlobal('fetch',fetcher)
    const notice=vi.fn()
    await watchWorkspace('owned','csrf',new AbortController().signal,()=>{},notice)
    expect(fetcher).toHaveBeenCalledTimes(2)
    expect(notice.mock.calls.every(args=>args[0]==='')).toBe(true)
  })
})
