import { useState } from 'react'
import { api, errorMessage } from './lib/api'

type DemoCase = { caseId: string; label: string; historyCount: number; historyTitles: string[] }
export function RecommendationMode({value,onChange,csrf,disabled}:{value:string;onChange:(value:string)=>void;csrf:string;disabled:boolean}) {
  const [cases,setCases]=useState<DemoCase[]>([])
  const [error,setError]=useState('')
  const [loading,setLoading]=useState(false)
  async function load(){
    setLoading(true);setError('')
    try {
      const result=await api('/workspace/recommendation/cases',{headers:{'X-CSRF-Token':csrf}}) as {cases:DemoCase[]}
      if(!Array.isArray(result.cases)||!result.cases.every(c=>typeof c.caseId==='string'&&typeof c.label==='string'&&Array.isArray(c.historyTitles))) throw new Error('推荐样例格式错误')
      setCases(result.cases)
    } catch(e){setError(errorMessage(e))} finally{setLoading(false)}
  }
  const selected=cases.find(c=>c.caseId===value)
  return <details className="recommendation-mode" style={{margin:'8px 20px',padding:'10px 14px',background:'#fff8f1',borderRadius:12}}>
    <summary>{selected?`推荐实验室 · ${selected.label} · ${selected.historyCount} 件历史商品`:'推荐实验室 · 真实电商评价历史'}</summary>
    <p>在当前导购对话中体验个性化推荐。使用公开美妆商品及匿名样例历史，不代表你的真实偏好。</p>
    {!cases.length&&<button type="button" disabled={disabled||loading||!csrf} onClick={()=>void load()}>{loading?'加载样例…':'加载公开历史样例'}</button>}
    {!!cases.length&&<label>对话模式 <select aria-label="推荐演示身份" value={value} disabled={disabled} onChange={e=>{onChange(e.target.value);const details=e.currentTarget.closest('details');if(details)details.open=false}}>
      <option value="">普通导购</option>{cases.map(c=><option key={c.caseId} value={c.caseId}>{c.label} · {c.historyCount} 件历史商品</option>)}
    </select></label>}
    {selected&&<><p>历史示例：{selected.historyTitles.join('；')||'无历史，将使用热门回退。'}</p><p>在下方输入：推荐 / 搜索护手霜 / 喜欢第一个 / 不要第二个 / 比较第一个和第三个 / 撤销。价格、库存未知，不支持交易。</p></>}
    {error&&<p role="alert">{error}</p>}
  </details>
}
