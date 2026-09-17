import './ExecutionDetail.css'
import reviewedSource from './lib/reviewedSource.json'

export interface ExecutionMetadata {
  id?: string; parentId?: string | null; startedAt?: string | null; finishedAt?: string | null
  input?: unknown; output?: unknown; cause?: Record<string, unknown>
  source?: { file?: string; symbol?: string; function?: string; line?: number; sha256?: string; snippet?: string; notice?: string; scope?: string } | null
}
function JsonBlock({ title, value }: { title: string; value: unknown }) {
  return <div className="execution-json"><h4>{title}</h4>{value == null ||
    (typeof value === 'object' && !Object.keys(value).length) ? <p>未记录可公开字段</p> :
    <pre>{JSON.stringify(value, null, 2)}</pre>}</div>
}
export function ExecutionDetail({ event, symbol, dataFirst = false }: { event: ExecutionMetadata; symbol?: string; dataFirst?: boolean }) {
  const key = (symbol ?? event.source?.symbol ?? '').replace(/ \[[^\]]+\]$/, '').split('.').slice(-2).join('.')
  const reference = !event.source?.snippet ? (reviewedSource as Record<string, {file:string;line:number;snippet:string;sha256:string}>)[key] : undefined
  const data = <div className="execution-data"><JsonBlock title="本次传入的数据" value={event.input} />
    <JsonBlock title="本次返回的结果" value={event.output} /></div>
  return <div className="execution-inspector">
    {dataFirst && data}
    <h4>这一步涉及的代码</h4>
      {event.source && Object.keys(event.source).length > 0 ? <>
        <p><code>{event.source.file}{event.source.line ? `:${event.source.line}` : ''}</code></p>
        <p><code>{event.source.symbol ?? event.source.function}</code></p>
        {event.source.snippet && <pre>{event.source.snippet}</pre>}
        {!event.source.snippet && <p>这条记录尚未附上代码正文。</p>}
        <details className="execution-source"><summary>核对源码版本</summary>
          <p>{event.source.notice ?? event.source.scope ?? '本次构建的源码摘录，不代表每一行都执行过。'}</p>
          {event.source.sha256 && <code>{event.source.sha256}</code>}
        </details>
      </> : <><p>这条记录未附源码摘录，暂不能显示代码正文。</p>{symbol && <code>{symbol}</code>}</>}
    {reference && <section aria-label="工作区参考代码"><h4>已核对的工作区参考代码</h4>
      <p>运行记录没有附上正文，下面提供工作区代码辅助学习。尚未确认它与本次运行版本一致，也不表示每一行都执行过。</p>
      <code>{reference.file}:{reference.line}</code><pre>{reference.snippet}</pre>
      <details><summary>参考文件版本</summary><code>{reference.sha256}</code></details>
    </section>}
    {!dataFirst && data}
  </div>
}
