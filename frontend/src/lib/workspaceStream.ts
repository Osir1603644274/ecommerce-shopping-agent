import { api, ApiError, isSessionError } from './api'
import { parseWorkspace, type Workspace } from './workspace'

// Fetch (not EventSource) preserves the existing CSRF header contract.
export async function readWorkspaceEvents(runId: string, csrf: string, signal: AbortSignal,
  onSnapshot: (workspace: Workspace) => void, onOpen: () => void = () => {}) {
  const response = await fetch(`/api/commerce-demo/workspace/control/events?runId=${encodeURIComponent(runId)}`, {
    credentials:'same-origin', cache:'no-store', signal:AbortSignal.any([signal,AbortSignal.timeout(60_000)]), headers:{Accept:'text/event-stream','X-CSRF-Token':csrf},
  })
  if (!response.ok) throw new ApiError('进度订阅暂不可用',response.status,null,response.status===403?'csrf validation failed':null)
  if (!response.body || !response.headers.get('content-type')?.includes('text/event-stream'))
    throw new ApiError('服务暂不支持进度订阅',415)
  onOpen()
  const reader=response.body.getReader(), decoder=new TextDecoder()
  let buffer='', settled=false, last:Workspace|null=null
  try {
    while (!signal.aborted) {
      const chunk=await reader.read()
      if(chunk.done) break
      buffer+=decoder.decode(chunk.value,{stream:true})
      if(buffer.length>4_000_000) throw new Error('进度数据过大')
      let boundary=/\r?\n\r?\n/.exec(buffer)
      while(boundary?.index!=null) {
        const block=buffer.slice(0,boundary.index)
        buffer=buffer.slice(boundary.index+boundary[0].length)
        const data=block.split(/\r?\n/).filter(line=>line.startsWith('data:')).map(line=>line.slice(5).replace(/^ /,'')).join('\n')
        if(data) {
          const event=JSON.parse(data)
          if(event.type==='snapshot') {
            const workspace=parseWorkspace(event.workspace)
            if(workspace.run?.id!==runId) throw new ApiError('运行身份已变化',409)
            last=workspace
            onSnapshot(workspace)
          } else if(event.type==='answer_delta') {
            if(event.runId !== runId || !Number.isSafeInteger(event.sequence) || event.sequence < 0 ||
              typeof event.text !== 'string' || typeof event.replace !== 'boolean' || typeof event.requestId !== 'string')
              throw new ApiError('回答增量身份不正确',409)
            if(last && last.run?.requestId === event.requestId && (event.replace || event.sequence > (last.answerStream?.sequence ?? -1))) {
              const text = event.replace ? event.text : (last.answerStream?.text ?? '') + event.text
              last = parseWorkspace({...last, answerStream:{runId,requestId:event.requestId,text,sequence:event.sequence,status:event.status}})
              onSnapshot(last)
            }
          } else if(event.type==='settled') {
            if(!last?.run || ['running','pausing'].includes(last.run.status)) throw new Error('订阅结束但尚未收到明确状态')
            settled=true
          }
          else if(event.type==='reconnect') {
            if(!last) throw new Error('订阅没有提供状态')
            return 'reconnect'
          }
          else if(event.type==='error') throw new ApiError('进度连接已中断，任务不会因此取消',event.status??503,null,event.status===403?'csrf validation failed':null)
        }
        boundary=/\r?\n\r?\n/.exec(buffer)
      }
      if(settled) return
    }
    if(!signal.aborted && !settled) throw new Error('进度连接结束，重新订阅原任务')
  } finally { await reader.cancel().catch(()=>{}); reader.releaseLock() }
}

const wait=(ms:number,signal:AbortSignal)=>new Promise<void>(resolve=>{
  if(signal.aborted){resolve();return}
  const finish=()=>{clearTimeout(timer);signal.removeEventListener('abort',finish);resolve()}
  const timer=setTimeout(finish,ms);signal.addEventListener('abort',finish,{once:true})
})

/** Reconnect reads only: never replay /run, /continue or a commerce write. */
export async function watchWorkspace(runId:string,csrf:string,signal:AbortSignal,
  onSnapshot:(workspace:Workspace)=>void,onNotice:(text:string)=>void) {
  let failures=0, fallback=false
  while(!signal.aborted) {
    try {
      if(fallback) {
        const value=parseWorkspace(await api('/workspace/control',{signal,headers:{'X-CSRF-Token':csrf}}))
        if(value.run?.id!==runId) throw new ApiError('运行身份已变化',409)
        onSnapshot(value)
        if(!['running','pausing'].includes(value.run.status)) return
        await wait(1500,signal)
      } else {
        const end=await readWorkspaceEvents(runId,csrf,signal,onSnapshot,()=>onNotice(''))
        if(end==='reconnect') continue
        return
      }
    } catch(error) {
      if(signal.aborted) return
      if(isSessionError(error) || error instanceof ApiError && error.status===409) throw error
      failures++
      if(error instanceof ApiError && [404,405,415].includes(error.status)) fallback=true
      onNotice(fallback?'流式进度暂不可用，正在自动回查原任务。':'进度连接中断，正在重新连接；任务未因此取消。')
      await wait(Math.min(8000,1000*2**Math.min(failures-1,3)),signal)
    }
  }
}
