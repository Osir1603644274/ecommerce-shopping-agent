import { useEffect, useRef, useState } from 'react'
import { api, errorMessage } from './lib/api'
import type { Message } from './lib/workspace'

export interface ArchivedChat { id:string; title:string; messages:Message[] }
export function ChatHistoryRail({csrf,revision,current,onOpen}:{csrf:string;revision:string;current?:string;onOpen:(chat:ArchivedChat)=>void}) {
  const [rows,setRows]=useState<{id:string;title:string}[]>([])
  const [next,setNext]=useState<number|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(false)
  const [loading,setLoading]=useState(false)
  const alive=useRef(true)
  useEffect(()=>{alive.current=true;return()=>{alive.current=false}},[])
  useEffect(()=>{let live=true;setRows([]);setError('');if(!csrf)return;setLoading(true)
    void api('/workspace/conversations',{headers:{'X-CSRF-Token':csrf}}).then(value=>{
      if(!live)return;const data=value as {conversations:typeof rows;nextOffset:number|null};if(!Array.isArray(data.conversations))throw new Error('历史记录暂不可用');setRows(data.conversations);setNext(data.nextOffset)
    }).catch(e=>{if(live)setError(errorMessage(e))}).finally(()=>{if(live)setLoading(false)});return()=>{live=false}
  },[csrf,revision])
  async function open(id:string){if(busy)return;setBusy(true);setError('')
    try {const data=await api(`/workspace/conversations/${encodeURIComponent(id)}`,{headers:{'X-CSRF-Token':csrf}}) as ArchivedChat;if(alive.current&&data.id===id&&Array.isArray(data.messages))onOpen(data)}
    catch(e){setError(errorMessage(e))}finally{setBusy(false)}
  }
  async function more(){if(busy||next==null)return;setBusy(true)
    try{const data=await api(`/workspace/conversations?offset=${next}`,{headers:{'X-CSRF-Token':csrf}}) as {conversations:typeof rows;nextOffset:number|null};if(!Array.isArray(data.conversations))throw new Error('历史记录暂不可用');if(alive.current){setRows(old=>[...old,...data.conversations]);setNext(data.nextOffset)}}
    catch(e){setError(errorMessage(e))}finally{setBusy(false)}
  }
  return <nav className="chat-history-rail" aria-label="历史会话列表"><h2>最近对话</h2>
    {rows.map(row=><button key={row.id} title={row.title} aria-current={current===row.id?'page':undefined} disabled={busy} onClick={()=>void open(row.id)}>{row.title||'新对话'}</button>)}
    {(loading||busy)&&<p role="status">正在读取历史对话…</p>}
    {!rows.length&&!error&&!loading&&<p>对话会保存在这里</p>}
    {next!=null&&<button disabled={busy} onClick={()=>void more()}>查看更多</button>}
    {error&&<p role="alert">{error}</p>}
  </nav>
}
