import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import {
  ArrowRight,
  Bookmark,
  Check,
  ArrowDown,
  ArrowUp,
  PanelLeft,
  Square,
  SquarePen,
  Copy,
  LogOut,
  MessageCircle,
  ReceiptText,
  RefreshCw,
  ShoppingBag,
  Smartphone,
  Sparkles,
} from 'lucide-react'
import { AuthDialog, Detail, OrderCard, restoreSession } from './App'
import { api, ApiError, errorMessage, isSessionError } from './lib/api'
import { money, statuses } from './lib/orders'
import type { Order, Session, StatusFilter } from './lib/orders'
import { useOrders } from './hooks/useOrders'
import { loadWorkspace, parseWorkspace } from './lib/workspace'
import { watchWorkspace } from './lib/workspaceStream'
import type { ProductCard, Workspace } from './lib/workspace'
import './Shop.css'
import './ChatShell.css'
import { ShopDrawer } from './ShopDrawer'
import { ChatHistoryRail, type ArchivedChat } from './ChatHistoryRail'
import { MessageBody } from './MessageBody'
import { OrderAfterSales } from './OrderAfterSales'
import { Flow, RunPanel, isActive } from './RunPanel'
import { ConversationHistory } from './ConversationHistory'
import { BackendObserver } from './BackendObserver'
import { Benefits } from './Benefits'
import { RecommendationMode } from './RecommendationMode'

type Tab = 'guide' | 'favorites' | 'orders' | 'benefits'
const names = { guide: 'AI 导购', favorites: '我的收藏', orders: '我的订单', benefits: '优惠与抢购' }

function Orders({
  session,
  onLogin,
  onExpired,
  onPay,
  onAfterSale,
}: {
  session: Session | null
  onLogin: () => void
  onExpired: () => void
  onPay: (id: string) => void
  onAfterSale: (path: string, body: object) => void
}) {
  const [status, setStatus] = useState<StatusFilter>('')
  const [detail, setDetail] = useState<Order | null>(null)
  const orders = useOrders(
    session?.username ?? null,
    false,
    status,
    session?.csrfToken ?? null,
    onExpired,
  )
  if (!session)
    return (
      <div className="shop-empty">
        <ReceiptText size={36} />
        <h2>每一笔选择，都在这里</h2>
        <p>登录后查看自己的订单与支付状态。</p>
        <button className="button primary" onClick={onLogin}>
          登录查看订单
        </button>
      </div>
    )
  return (
    <section className="shop-orders">
      <div className="shop-section-heading">
        <div>
          <div className="eyebrow">YOUR ORDERS</div>
          <h1>我的订单</h1>
        </div>
        <button className="button secondary" onClick={orders.refresh} disabled={orders.loading}>
          <RefreshCw size={16} />
          刷新
        </button>
      </div>
      <div className="shop-filter">
        <label htmlFor="order-status">订单状态</label>
        <select
          id="order-status"
          value={status}
          onChange={(e) => {
            setStatus(e.target.value as StatusFilter)
            setDetail(null)
          }}
        >
          <option value="">全部订单</option>
          {Object.entries(statuses).map(([key, name]) => (
            <option key={key} value={key}>
              {name}
            </option>
          ))}
        </select>
      </div>
      {orders.error && (
        <p role="alert" className="shop-error">
          {orders.error}
        </p>
      )}
      {orders.orders.map((order) => (
        <div key={order.id}>
          <OrderCard order={order} onDetail={setDetail} />
          {order.status === 'PENDING_PAYMENT' && (
            <button className="button primary pay-from-order" onClick={() => onPay(order.id)}>
              去支付 <ArrowRight size={16} />
            </button>
          )}
        </div>
      ))}
      {orders.loaded && !orders.orders.length && (
        <div className="shop-empty">
          <ShoppingBag size={36} />
          <h2>这里还没有订单</h2>
          <p>从导购里选一件喜欢的商品吧。</p>
        </div>
      )}
      {orders.loading && <p role="status">正在读取订单…</p>}
      {(orders.hasMore || orders.error) && (
        <button className="button secondary" disabled={orders.loading} onClick={orders.loadMore}>
          {orders.error ? '重试' : '加载更多订单'}
        </button>
      )}
      {detail && (
        <Detail
          selected={detail}
          demo={false}
          csrf={session.csrfToken}
          onClose={() => setDetail(null)}
          onExpired={onExpired}
          afterSales={<OrderAfterSales id={detail.id} csrf={session.csrfToken} onPreview={onAfterSale} />}
        />
      )}
    </section>
  )
}

function Card({
  product,
  tabular = false,
  selected,
  saved,
  busy,
  onSelect,
  onSave,
}: {
  product: ProductCard
  tabular?: boolean
  selected: boolean
  saved: boolean
  busy: boolean
  onSelect: () => void
  onSave: () => void
}) {
  if (tabular) return (
    <tr className={selected ? 'chosen' : ''} data-product-id={product.id} data-purchasable={!!product.purchasable}>
      <td className="recommendation-product"><small>{product.brand || '商品'}</small><h3>{product.title}</h3>
        {product.evidence && <details className="product-evidence">
          <summary>推荐依据 · {product.evidence.length ? `${product.evidence.length} 条型号资料` : '资料待补充'}</summary>
          <p>{product.evidenceNotice}</p>
          {product.evidence.map(e => <div className="evidence-item" key={e.id}>
            <small>{e.field?.endsWith('_test') ? '实测记录' : '规格资料'} · {e.model}</small>
            <p>{e.text}</p>
            {e.conditions != null && <p>测试条件：{typeof e.conditions === 'string' ? e.conditions : JSON.stringify(e.conditions)}</p>}
            {e.software && <p>软件版本：{e.software}</p>}
            <small>适用版本：{e.region || '未记录'}</small>{' '}
            {/^(https?):\/\//.test(e.url) && <a href={e.url} target="_blank" rel="noopener noreferrer">查看资料来源 ↗</a>}
          </div>)}
        </details>}
      </td>
      <td className="recommendation-price"><div className="shop-product-price">{product.priceMinor == null ? '价格待确认' : money(product.priceMinor, product.currency)}</div>
        {['local_simulated', 'synthetic'].includes(product.priceKind || '') && <small className="price-provenance">模拟参考价 · 非真实报价</small>}</td>
      <td className="recommendation-actions">        <div className="shop-product-actions">
          <button className="button secondary small" disabled={busy} onClick={onSelect}>
            {selected ? <Check size={15} /> : null}
            {selected ? '已选择' : '查看商品'}
          </button>
          <button
            className={`icon-button ${saved ? 'saved' : ''}`}
            disabled={busy}
            aria-label={saved ? `取消收藏 ${product.title}` : `收藏 ${product.title}`}
            aria-pressed={saved}
            onClick={onSave}
          >
            <Bookmark size={18} />
          </button>
        </div>
</td>
    </tr>
  )
  return (
    <article className={`shop-product ${selected ? 'chosen' : ''}`} data-product-id={product.id} data-purchasable={!!product.purchasable}>
      <div className="shop-product-art" aria-hidden="true">
        {product.category?.includes('手机') ? <Smartphone size={74} strokeWidth={1} /> : <ShoppingBag size={74} strokeWidth={1} />}
        <span>{product.brand || 'GOOD FIND'}</span>
      </div>
      <div className="shop-product-copy">
        <small>{product.brand || '精选商品'}</small>
        <h3>{product.title}</h3>
        <div className="shop-product-price">{product.priceMinor == null ? '价格待确认' : money(product.priceMinor, product.currency)}</div>
        {['local_simulated', 'synthetic'].includes(product.priceKind || '') && <small className="price-provenance">模拟参考价 · 非真实报价</small>}
        <div className="shop-product-actions">
          <button className="button secondary small" disabled={busy} onClick={onSelect}>
            {selected ? <Check size={15} /> : null}
            {selected ? '已选择' : '查看商品'}
          </button>
          <button
            className={`icon-button ${saved ? 'saved' : ''}`}
            disabled={busy}
            aria-label={saved ? `取消收藏 ${product.title}` : `收藏 ${product.title}`}
            aria-pressed={saved}
            onClick={onSave}
          >
            <Bookmark size={18} />
          </button>
        </div>
        {product.evidence && <details className="product-evidence">
          <summary>推荐依据 · {product.evidence.length ? `${product.evidence.length} 条型号资料` : '资料待补充'}</summary>
          <p>{product.evidenceNotice}</p>
          {product.evidence.map(e => <div className="evidence-item" key={e.id}>
            <small>{e.field?.endsWith('_test') ? '实测记录' : '规格资料'} · {e.model}</small>
            <p>{e.text}</p>
            {e.conditions != null && <p>测试条件：{typeof e.conditions === 'string' ? e.conditions : JSON.stringify(e.conditions)}</p>}
            {e.software && <p>软件版本：{e.software}</p>}
            <small>适用版本：{e.region || '未记录'}</small>{' '}
            {/^(https?):\/\//.test(e.url) && <a href={e.url} target="_blank" rel="noopener noreferrer">查看资料来源 ↗</a>}
          </div>)}
        </details>}
      </div>
    </article>
  )
}

export default function Shop() {
  const [tab, setTab] = useState<Tab>(() =>
    ['guide', 'favorites', 'orders', 'benefits'].includes(location.hash.slice(1))
      ? (location.hash.slice(1) as Tab)
      : 'guide',
  )
  const [session, setSession] = useState<Session | null>(null)
  const [restoring, setRestoring] = useState(true),
    [login, setLogin] = useState(false)
  const [workspace, setWorkspace] = useState<Workspace | null>(null),
    [guestCsrf, setGuestCsrf] = useState('')
  const [favorites, setFavorites] = useState<ProductCard[]>([])
  const [message, setMessage] = useState(''),
    [error, setError] = useState('')
  const [busy, setBusy] = useState(false),
    [simulation, setSimulation] = useState(false)
  const [sidebarOpen,setSidebarOpen]=useState(false),[transactionOpen,setTransactionOpen]=useState(false),[observerOpen,setObserverOpen]=useState(false)
  const [archive,setArchive]=useState<ArchivedChat|null>(null),[copied,setCopied]=useState(''),[away,setAway]=useState(false)
  const [sending,setSending]=useState<{text:string;id:string}|null>(null)
  const followBottom=useRef(true)
  const composer=useRef<HTMLTextAreaElement>(null)
  const submitting=useRef(false)
  const [stepMode, setStepMode] = useState(false)
  const [recommendationCase, setRecommendationCase] = useState('')
  const [showHistory, setShowHistory] = useState(false)
  const [workspaceRetry, setWorkspaceRetry] = useState(0)
  const [streamNotice, setStreamNotice] = useState('')
  const activeRun = isActive(workspace?.run)
  const receivingRun = !!workspace?.run && ['running', 'pausing'].includes(workspace.run.status)
  const canChat = !receivingRun && (!activeRun || workspace?.run?.mode === 'continuous' || workspace?.run?.status === 'clarification')
  const partial=workspace?.answerStream
  const draft=partial?.runId===workspace?.run?.id && !workspace?.messages.some(m=>m.role==='assistant'&&m.requestId===partial?.requestId) ? partial?.text || '' : ''
  const streaming=receivingRun || !!draft
  const [quantity, setQuantity] = useState(1),
    [paid, setPaid] = useState(false)
  const generation = useRef(0),
    busyRef = useRef(false),
    stream = useRef<AbortController | null>(null)
  const end = useRef<HTMLDivElement>(null)
  const csrf = session?.csrfToken ?? guestCsrf
  const expired = useCallback(() => {
    setShowHistory(false)
    setArchive(null)
    setSending(null)
    generation.current++
    stream.current?.abort()
    setSession(null)
    setWorkspace(null)
    setFavorites([])
    setGuestCsrf('')
    setError('会话已更新，请重新登录。')
    setLogin(true)
  }, [])
  const navigate = (next: Tab) => {
    setTab(next)
    setSidebarOpen(false)
    setArchive(null)
    history.replaceState(null, '', '#' + next)
  }
  useEffect(() => {
    let live = true
    restoreSession()
      .then((value) => {
        if (live) setSession(value)
      })
      .catch((caught) => {
        if (live && !(caught instanceof ApiError && caught.status === 401))
          setError(errorMessage(caught))
      })
      .finally(() => {
        if (live) setRestoring(false)
      })
    void api('/capability')
      .then((value) => {
        if (live)
          setSimulation(!!(value as { paymentSimulationEnabled: boolean }).paymentSimulationEnabled)
      })
      .catch(() => {})
    return () => {
      live = false
      stream.current?.abort()
    }
  }, [])
  useEffect(() => {
    if (restoring) return
    setShowHistory(false)
    const gen = ++generation.current
    setWorkspace(null)
    setFavorites([])
    setArchive(null)
    setPaid(false)
    loadWorkspace(session?.csrfToken ?? '')
      .then(async (value) => {
        if (gen !== generation.current) return
        setWorkspace(value)
        setGuestCsrf(value.csrfToken ?? '')
        setQuantity(value.selection?.quantity ?? 1)
        if (session) {
          const saved = await api('/workspace/favorites', { headers: { 'X-CSRF-Token': session.csrfToken } })
          if (gen === generation.current) setFavorites((saved as { products: ProductCard[] }).products)
        }
      })
      .catch((caught) => {
        if (gen !== generation.current) return
        if (isSessionError(caught)) expired()
        else setError(errorMessage(caught))
      })
    return () => {
      generation.current++
      stream.current?.abort()
    }
  }, [session, restoring, workspaceRetry, expired])
  useLayoutEffect(() => {
    const history = end.current?.parentElement
    if (!history) return
    if(followBottom.current) history.scrollTop = history.scrollHeight
  }, [workspace?.messages.length, draft])
  useEffect(() => {
    const node=end.current?.parentElement
    if(!node)return
    const observer=new ResizeObserver(()=>{if(followBottom.current)node.scrollTop=node.scrollHeight})
    observer.observe(node)
    return()=>observer.disconnect()
  },[tab,archive,!!workspace])
  useEffect(()=>{const node=composer.current;if(node){node.style.height='auto';node.style.height=Math.min(node.scrollHeight,180)+'px'}},[message])
  useEffect(() => {
    if (!workspace?.run || !receivingRun || !csrf) { setStreamNotice(''); return }
    const gen = generation.current
    const controller = new AbortController()
    stream.current = controller
    void watchWorkspace(workspace.run.id, csrf, controller.signal, value => {
      if (controller.signal.aborted || gen !== generation.current) return
      setWorkspace(old => old?.run && value.run && old.run.id === value.run.id && old.run.revision > value.run.revision ? old : value)
    }, text => { if (!controller.signal.aborted && gen === generation.current) setStreamNotice(text) })
      .catch(caught => {
        if (controller.signal.aborted || gen !== generation.current) return
        if (isSessionError(caught)) expired()
        else setStreamNotice(errorMessage(caught))
      })
    return () => { controller.abort(); if (stream.current === controller) stream.current = null }
  }, [workspace?.run?.id, receivingRun, csrf, expired])

  async function action(path: string, body?: unknown, method = 'POST'): Promise<Workspace | null> {
    if (busyRef.current) return null
    busyRef.current = true
    setBusy(true)
    setError('')
    const gen = generation.current
    try {
      const value = parseWorkspace(
        await api('/workspace' + path, {
          observation: { label: ({ '/run': '发送导购需求', '/preview': '预览订单', '/confirm': '确认交易',
            '/reconcile': '回查原交易', '/selection': '选择商品', '/payment-preview': '预览支付',
            '/cancel-preview': '预览取消', '/refund-preview': '预览退款', '/conversations': '新建对话',
            '/control/pause': '请求安全暂停', '/control/continue': '继续原任务', '/control/end': '结束本轮',
            '/control/step': '执行下一步' } as Record<string,string>)[path] ?? '购物空间操作',
            trigger: 'user', code: 'frontend/src/Shop.tsx → action' },
          method,
          headers: { 'X-CSRF-Token': csrf },
          ...(body ? { body: JSON.stringify(body) } : {}),
        }),
      )
      if (gen !== generation.current) return null
      setWorkspace(value)
      if(['/selection','/preview','/payment-preview','/refund-preview','/cancel-preview','/confirm','/reconcile'].includes(path)) setTransactionOpen(true)
      setPaid(false)
      return value
    } catch (caught) {
      if (gen === generation.current) {
        if (isSessionError(caught)) {
          // Restore only this failed submission, before invalidating its owner.
          // A late response from a different generation must never restore text.
          if (path === '/run' && body && typeof (body as {message?: unknown}).message === 'string') {
            setMessage(current => current || (body as {message:string}).message)
          }
          expired()
        }
        else {
          setError(errorMessage(caught))
          if (path === '/confirm' || path === '/reconcile') {
            try {
              const current = await loadWorkspace(csrf)
              if (gen === generation.current) setWorkspace(current)
            } catch { /* Keep the same confirmation card for a later read-only retry. */ }
          }
        }
      }
      return null
    } finally {
      busyRef.current = false
      setBusy(false)
    }
  }
  async function newConversation() {
    if (submitting.current || busyRef.current || !workspace?.conversationId || receivingRun || workspace.checkout?.pending) return
    submitting.current = true
    const gen = generation.current
    try {
      const current = workspace.run
      // End only the stalled conversational run. This never cancels a trade.
      const ended = activeRun && current
        ? await action('/control/end', { runId: current.id, revision: current.revision }) : workspace
      if (!ended || gen !== generation.current) return
      const fresh = await action('/conversations', { expectedConversationId: ended.conversationId })
      if (fresh && gen === generation.current) {
        stream.current?.abort()
        setMessage(''); setSending(null); setQuantity(1)
        navigate('guide'); composer.current?.focus()
      }
    } finally { submitting.current = false }
  }
  async function openConversation(chat: ArchivedChat) {
    setTab('guide'); setSidebarOpen(false)
    if (chat.id === workspace?.conversationId) { setArchive(null); return }
    if (busyRef.current || submitting.current || !workspace) return
    // A running task or uncertain transaction can still be inspected, but must
    // not silently disappear when selecting another conversation.
    if (receivingRun || workspace.checkout?.pending) { setArchive(chat); return }
    submitting.current = true
    const gen = generation.current
    try {
      const current = workspace.run
      const ended = activeRun && current ? await action('/control/end', { runId: current.id, revision: current.revision }) : workspace
      if (!ended || gen !== generation.current) return
      const restored = await action(`/conversations/${encodeURIComponent(chat.id)}/activate`, { expectedConversationId: ended.conversationId })
      if (restored && gen === generation.current) {
        setArchive(null); setMessage(''); setSending(null); setQuantity(1)
        setTransactionOpen(false); followBottom.current = true
      }
    } finally { submitting.current = false }
  }
  async function ask(text = message) {
    if (!text.trim() || submitting.current || busyRef.current || !workspace || !csrf || workspace.checkout?.pending || !canChat) return
    submitting.current = true
    setMessage('')
    followBottom.current=true;setAway(false)
    const requestId=crypto.randomUUID()
    setSending({text,id:requestId})
    const gen=generation.current
    stream.current?.abort()
    let result: Workspace | null = null
    try {
      const currentRun = workspace.run
      if (recommendationCase) {
        result = await action('/recommendation/run', {message:text,requestId,caseId:recommendationCase,
          expectedRevision:workspace.recommendationDemo?.revision ?? 0,expectedConversationId:workspace.conversationId})
      } else if (currentRun?.status === 'clarification') {
        result = await action('/control/continue', { runId: currentRun.id, revision: currentRun.revision, answer: text, requestId })
      } else {
        const ended = activeRun && currentRun ? await action('/control/end', { runId: currentRun.id, revision: currentRun.revision }) : workspace
        if (ended && gen === generation.current) result = await action('/run', { message: text, requestId, mode: stepMode ? 'step' : 'continuous' })
      }
    } finally { submitting.current = false }
    if(gen!==generation.current)return
    setSending(null)
    if (!result) setMessage(current => current || text)
  }
  async function favorite(product: ProductCard) {
    if (!session) {
      setLogin(true)
      return
    }
    if (busyRef.current) return
    const exists = favorites.some((p) => p.id === product.id),
      gen = generation.current
    busyRef.current = true
    setBusy(true)
    setError('')
    try {
      await api(`/workspace/favorites/${product.id}`, {
        observation: { label: exists ? '取消收藏' : '收藏商品', trigger: 'user', code: 'frontend/src/Shop.tsx → favorite' },
        method: exists ? 'DELETE' : 'PUT',
        headers: { 'X-CSRF-Token': csrf },
      })
      if (gen === generation.current)
        setFavorites((old) => (exists ? old.filter((p) => p.id !== product.id) : [product, ...old]))
    } catch (caught) {
      if (gen === generation.current) {
        if (isSessionError(caught)) expired()
        else setError(errorMessage(caught))
      }
    } finally {
      busyRef.current = false
      setBusy(false)
    }
  }
  async function logout() {
    if (!session || busyRef.current) return
    const token = session.csrfToken
    generation.current++
    stream.current?.abort()
    setWorkspace(null)
    setFavorites([])
    setGuestCsrf('')
    busyRef.current = true
    setBusy(true)
    try {
      await api('/logout', { method: 'POST', headers: { 'X-CSRF-Token': token } })
      setSession(null)
      setError('')
    } catch {
      setError('退出未确认，已隐藏私人内容。请重试退出。')
    } finally {
      busyRef.current = false
      setBusy(false)
    }
  }
  async function pay(id: string) {
    navigate('guide')
    await action('/payment-preview', { orderId: id })
  }
  async function simulate(id: string) {
    if (busyRef.current) return
    busyRef.current = true
    setBusy(true)
    setError('')
    const gen = generation.current
    try {
      const path = workspace?.checkout?.proposal.action === 'create_refund'
        ? `/workspace/refunds/${encodeURIComponent(id)}/simulate-success`
        : `/payments/${encodeURIComponent(id)}/simulate-success`
      await api(path, {
        method: 'POST',
        headers: { 'X-CSRF-Token': csrf },
      })
      if (gen === generation.current) {
        setPaid(true)
        navigate('orders')
      }
    } catch (caught) {
      if (gen === generation.current) setError(errorMessage(caught))
    } finally {
      busyRef.current = false
      setBusy(false)
    }
  }
  const checkout = workspace?.checkout,
    result = checkout?.outcome?.result
  const selection = workspace?.selection
  const proposal = checkout?.proposal,
    preview = proposal?.preview
  const renderCard = (product: ProductCard, tabular = false) => (
    <Card
      key={product.id}
      tabular={tabular}
      product={product}
      saved={favorites.some((p) => p.id === product.id)}
      selected={selection?.product.id === product.id}
      busy={busy || activeRun}
      onSave={() => void favorite(product)}
      onSelect={() => {
        navigate('guide')
        setQuantity(1)
        void action('/selection', { productId: product.id, quantity: 1 })
      }}
    />
  )
  return (
    <div className={`app-shell unified-shop chat-shell ${sidebarOpen?'sidebar-open':''} ${tab==='guide'?'chat-route':''}`}>
      <a className="skip-link" href="#shop-main">
        跳到主要内容
      </a>
      <aside className="sidebar">
        <button className="icon-button sidebar-close" aria-label="关闭侧栏" onClick={()=>setSidebarOpen(false)}><PanelLeft size={20}/></button>
        <a className="brand" href="#guide" onClick={() => navigate('guide')}>
          <span className="brand-mark">
            <ShoppingBag size={24} />
          </span>
          <span>
            拾物<span className="brand-subtitle">FIND YOUR EVERYDAY</span>
          </span>
        </a>
        <button className="nav-item new-chat" disabled={busy || !workspace?.conversationId || receivingRun || !!checkout?.pending}
          onClick={()=>{void newConversation()}}><SquarePen size={19}/>新对话</button>
        <nav aria-label="主导航">
          {(['guide', 'favorites', 'orders', 'benefits'] as Tab[]).map((item) => (
            <button
              key={item}
              className={`nav-item ${tab === item ? 'active' : ''}`}
              aria-current={tab === item ? 'page' : undefined}
              onClick={() => navigate(item)}
            >
              {item === 'guide' ? (
                <Sparkles size={19} />
              ) : item === 'favorites' ? (
                <Bookmark size={19} />
              ) : (
                <ReceiptText size={19} />
              )}{' '}
              {names[item]}
              {tab === item && <span className="nav-indicator" />}
            </button>
          ))}
        </nav>
        <ChatHistoryRail key={csrf} csrf={csrf} revision={`${workspace?.conversationId}:${workspace?.messages.length}`} current={archive?.id||workspace?.conversationId}
          onOpen={chat=>{void openConversation(chat)}}/>
        <div className="sidebar-bottom"><small>拾物 · 你的购物助手</small><button className="text-button" onClick={()=>setShowHistory(true)}>浏览全部记录</button></div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <div className="breadcrumb">
            <button className="icon-button" aria-label="切换侧栏" aria-expanded={sidebarOpen} onClick={()=>setSidebarOpen(v=>!v)}><PanelLeft size={20}/></button>
            <span>{archive?.title || (tab==='guide'?'拾物 AI':names[tab])}</span>
          </div>
          <div className="account-actions">
            {(selection||checkout)&&<button className="icon-button" aria-label="打开商品与交易" onClick={()=>setTransactionOpen(true)}><ShoppingBag size={20}/></button>}
            <button className="button secondary small" onClick={() => setObserverOpen(true)}>后端执行详情</button>
            {session ? (
              <>
                <span className="account-name">
                  <span className="avatar">{session.username[0]?.toUpperCase()}</span>
                  {session.username}
                </span>
                <button
                  className="icon-button"
                  disabled={busy || activeRun}
                  aria-label="退出登录"
                  onClick={() => void logout()}
                >
                  <LogOut size={18} />
                </button>
              </>
            ) : (
              <button
                className="button secondary small"
                disabled={restoring || busy || activeRun}
                onClick={() => setLogin(true)}
              >
                {restoring ? '检查会话…' : '登录 / 注册'}
              </button>
            )}
          </div>
        </header>
        <main id="shop-main">
          {error && (
            <div role="alert" className="shop-error">
              {error}
              {!workspace && <button className="text-button" onClick={() => { setError(''); setWorkspaceRetry(value => value + 1) }}>重试连接</button>}
              <button className="text-button" onClick={() => setError('')}>
                收起
              </button>
            </div>
          )}
          {tab === 'guide' && archive && <section className="archive-chat"><button className="button secondary" onClick={()=>setArchive(null)}>返回当前对话</button><p className="archive-notice">当前任务仍在执行或交易结果待确认，暂时只查看历史。处理完成后可继续这段对话。</p><button disabled={busy || receivingRun || !!checkout?.pending} onClick={()=>void openConversation(archive)}>继续这段对话</button>{!archive.messages.length && <p role="status">这段对话没有已保存的消息。</p>}{archive.messages.map(m=><article className={`chat-message ${m.role}`} key={m.requestId+m.role}><MessageBody text={m.content}/></article>)}</section>}
          {tab === 'guide' && !archive && (
            <>
              <div className={`guide-grid ${selection || checkout ? 'has-checkout' : ''}`}>
                <section className="chat-panel" aria-label="导购对话">
                  <header>
                    <MessageCircle size={18} />
                    <strong>拾物 AI</strong>
                    <span>为你认真挑选</span>
                    <button className="conversation-button" disabled={busy || !workspace?.conversationId || receivingRun || !!checkout?.pending}
                      onClick={() => void newConversation()}>新建对话</button>
                    <button className="conversation-button" disabled={!workspace || busy} onClick={() => setShowHistory(true)}>历史对话</button>
                  </header>
                  <RecommendationMode value={recommendationCase} onChange={value=>{setRecommendationCase(value);setStepMode(false)}} csrf={csrf||''} disabled={busy||activeRun||!!checkout?.pending}/>
                  <div className="chat-history" onScroll={e=>{const node=e.currentTarget;followBottom.current=node.scrollHeight-node.scrollTop-node.clientHeight<80;setAway(!followBottom.current)}}>
                    {!workspace?.messages.length && !sending && (
                      <div className="chat-welcome">
                        <span className="welcome-star">
                          <Sparkles size={30} />
                        </span>
                        <h2>今天想找什么？</h2>
                        <p>
                          预算、品牌、使用场景，
                          <br />
                          告诉我你最在意的那一点。
                        </p>
                        <div className="prompt-chips">
                          {['推荐一部 2000 元左右的二手手机', '想找一部适合拍照的二手手机'].map(
                            (text) => (
                              <button
                                disabled={busy || !workspace || activeRun}
                                key={text}
                                onClick={() => void ask(text)}
                              >
                                {text}
                                <ArrowRight size={14} />
                              </button>
                            ),
                          )}
                        </div>
                      </div>
                    )}
                    {workspace?.messages.map((m, i) => (
                      <div key={m.requestId + m.role + i} className={`chat-message ${m.role} ${m.source==='public_recommendation_demo'?'recommendation-demo':''}`}>
                        <small>{m.role === 'user' ? '你' : '拾物 AI'}</small>
                        {m.role === 'assistant' && !!m.cards?.length ? <>
                          <MessageBody text={m.content}/>
                          <div className="recommendation-table-wrap">
                            <table className="recommendation-table" aria-label="本轮商品推荐">
                              <thead><tr><th scope="col">商品与资料</th><th scope="col">价格</th><th scope="col">操作</th></tr></thead>
                              <tbody>{m.cards.map(product => renderCard(product, true))}</tbody>
                            </table>
                          </div>
                          <p className="recommendation-note">按本轮推荐顺序展示；价格、规格或库存缺失时，仍需进一步核验。</p>
                        </> : m.role === 'assistant' ? <MessageBody text={m.content} /> : <div className="message-copy">{m.content}</div>}
                        {!!m.flow?.length && <details className="message-flow"><summary>查看本轮执行记录</summary><Flow nodes={m.flow} /></details>}
                        {m.role==='assistant'&&<button className="message-copy-button icon-button" aria-label="复制回答" onClick={()=>{void navigator.clipboard.writeText(m.content).then(()=>setCopied(m.requestId)).catch(()=>setError('复制失败，请选择文字复制'))}}>{copied===m.requestId?<Check size={16}/>:<Copy size={16}/>}</button>}
                      </div>
                    ))}
                    {sending && !workspace?.messages.some(m=>m.requestId===sending.id) && <div className="chat-message user"><div className="message-copy">{sending.text}</div></div>}
                    {(streaming || sending) && (
                      <div className={`chat-message assistant ${receivingRun&&draft?'answer-generating':''}`}>
                        <small>拾物 AI</small>
                        <div className="message-copy">
                          {draft ? <MessageBody text={draft}/> : (
                            <span role="status">
                              <span className="thinking-dot"/> {partial?.status==='generating'?'正在组织回答…':workspace?.run?.nextStage || '正在思考…'}
                            </span>
                          )}
                        </div>
                      </div>
                    )}
                    {streamNotice && <p role="status" className="flow-empty">{streamNotice}</p>}
                    {workspace?.run?.status === 'clarification' && !workspace.messages.some(m => m.role === 'assistant' && m.content === workspace.run?.notice) && <article className="chat-message assistant"><MessageBody text={workspace.run.notice || '请补充你的需求。'} /></article>}
                    {workspace?.run && (activeRun || stepMode) && <RunPanel run={workspace.run} busy={busy} composerControls onCommand={(op, answer) => {
                      void action('/control/' + op, { runId: workspace.run!.id, revision: workspace.run!.revision, ...(answer ? { answer } : {}) })
                    }} />}
                    <div ref={end} />
                  </div>
                  {away&&<button className="jump-bottom icon-button" aria-label="回到最新消息" onClick={()=>{followBottom.current=true;end.current?.parentElement?.scrollTo({top:end.current.parentElement.scrollHeight,behavior:'smooth'});setAway(false)}}><ArrowDown size={18}/></button>}
                  <form
                    className="chat-composer"
                    onSubmit={(e) => {
                      e.preventDefault()
                      void ask()
                    }}
                  >
                    {!workspace && (
                      <div role="status" className="workspace-recovery">
                        <span>{restoring ? '正在恢复登录状态…' : error || '正在连接购物空间…'}</span>
                        {!restoring && error && <button type="button" className="text-button" onClick={() => { setError(''); setWorkspaceRetry(value => value + 1) }}>重新连接</button>}
                        {!restoring && !session && <button type="button" className="text-button" onClick={() => setLogin(true)}>登录恢复对话</button>}
                      </div>
                    )}
                    <label className="sr-only" htmlFor="shopping-message">
                      告诉我你想找什么
                    </label>
                    <textarea
                      ref={composer}
                      id="shopping-message"
                      placeholder={workspace?.run?.status === 'clarification' ? '在这里回答，也可以补充或修改需求…' : '想找什么？也可以继续问我…'}
                      maxLength={2000}
                      value={message}
                      onChange={(e) => setMessage(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key !== 'Enter' || e.shiftKey || e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229) return
                        e.preventDefault()
                        if (!e.repeat && !busy && canChat && workspace && !checkout?.pending && message.trim()) {
                          e.currentTarget.form?.requestSubmit()
                        }
                      }}
                      rows={1}
                    />
                    <div>
                      <label className="debug-toggle"><input type="checkbox" checked={stepMode} disabled={busy || activeRun || !!recommendationCase}
                        onChange={e => setStepMode(e.target.checked)} />单步调试</label>
                      <small>{receivingRun ? '可以先写下一条，停止后发送' : 'Enter 发送 · Shift+Enter 换行'}</small>
                      {receivingRun ? <button type="button" className="send-button stop-button" aria-label="停止输出" disabled={busy||workspace?.run?.status==='pausing'} onClick={()=>void action('/control/pause',{runId:workspace!.run!.id,revision:workspace!.run!.revision})}><Square size={16} fill="currentColor"/></button> : <button
                        className="send-button"
                        aria-label="发送消息"
                        disabled={busy || !canChat || !workspace || !!checkout?.pending || !message.trim()}
                      >
                        <ArrowUp size={20} />
                      </button>}
                    </div>
                  </form>
                </section>
                {transactionOpen && (selection || checkout) && <ShopDrawer title="商品与交易" onClose={()=>setTransactionOpen(false)}><aside className="shopping-panel" aria-label="商品与交易">
                  {selection && !checkout && (
                    <section className="checkout-card">
                      <div className="eyebrow">YOUR SELECTION</div>
                      <h3>{selection.product.title}</h3>
                      <p className="checkout-price">
                        {selection.product.priceMinor == null
                          ? '价格待确认'
                          : money(selection.product.priceMinor)}
                      </p>
                      <label className="quantity-label">
                        数量
                        <input
                          type="number"
                          min={1}
                          max={20}
                          value={quantity}
                          onChange={(e) =>
                            setQuantity(Math.max(1, Math.min(20, Math.trunc(Number(e.target.value)) || 1)))
                          }
                        />
                      </label>
                      <button
                        className="button primary"
                        disabled={busy || !selection.product.purchasable}
                        onClick={() => {
                          if (!session) setLogin(true)
                          else
                            void action('/preview', { productId: selection.product.id, quantity })
                        }}
                      >
                        预览订单 <ArrowRight size={16} />
                      </button>
                      {!selection.product.purchasable && <p>暂不可下单，可以先收藏或继续挑选。</p>}
                    </section>
                  )}
                  {checkout && proposal && preview && (
                    <section className="checkout-card" aria-label="交易确认卡">
                      <div className="eyebrow">
                        {proposal.action === 'create_order' ? 'CONFIRM YOUR ORDER' : 'PAYMENT'}
                      </div>
                      <h2>{{ create_order: '确认这次选择', create_payment: '确认支付', cancel_order: '确认取消订单', create_refund: '确认退款明细与金额' }[proposal.action]}</h2>
                      <h3>
                        {preview.title ||
                          preview.order?.items?.map((i) => i.titleSnapshot).join('、') ||
                          '订单支付'}
                      </h3>
                      {preview.quantity && <p>数量 × {preview.quantity}</p>}
                      {preview.refundItems?.map(item => <p key={item.itemId}>{item.title} × {item.quantity} · {money(item.amountMinor)}</p>)}
                      {preview.reason && <p>原因：{preview.reason}</p>}
                      <p className="checkout-price">
                        {money(preview.payableMinor ?? preview.order?.payableMinor ?? 0)}
                      </p>
                      {!result && (
                        <>
                          <p className="muted">
                            {checkout.pending
                              ? '结果尚未确认，请先回查，勿重复下单。'
                              : checkout.outcome?.message || '请核对商品与金额，确认后才会提交。'}
                          </p>
                          {checkout.pending && checkout.outcome?.message && <p>{checkout.outcome.message}</p>}
                          <button
                            className="button primary"
                            disabled={busy || checkout.pending || checkout.outcome?.status === 'rejected'}
                            onClick={() =>
                              void action('/confirm', { confirmationId: proposal.confirmationId })
                            }
                          >
                            {{ create_order: '确认下单', create_payment: '确认发起支付', cancel_order: '确认取消订单', create_refund: '确认申请退款' }[proposal.action]}
                          </button>
                          <button
                            className="button secondary"
                            disabled={busy}
                            onClick={() =>
                              void action('/reconcile', { confirmationId: proposal.confirmationId })
                            }
                          >
                            回查结果
                          </button>
                          {checkout.outcome?.status === 'rejected' && selection && (
                            <button className="button secondary" disabled={busy}
                              onClick={() => void action('/preview', { productId: selection.product.id, quantity })}>
                              重新预览订单
                            </button>
                          )}
                        </>
                      )}
                      {result && (
                        <>
                          <p className="receipt-success">
                            <Check size={17} />
                            {{ create_order: '订单已创建', create_payment: '支付单已创建', cancel_order: '订单已取消', create_refund: '退款申请已创建' }[proposal.action]}
                          </p>
                          <p>订单号：{result.orderNo || result.orderId || result.id}</p>
                          {proposal.action === 'create_order' && result.id && (
                            <button
                              className="button primary"
                              disabled={busy}
                              onClick={() => void pay(result.id!)}
                            >
                              去支付 <ArrowRight size={16} />
                            </button>
                          )}
                          {['create_payment', 'create_refund'].includes(proposal.action) && result.id && (
                            <button
                              className="button primary"
                              disabled={busy || !simulation || paid}
                              onClick={() => void simulate(result.id!)}
                            >
                              {paid ? '结果已更新' : proposal.action === 'create_refund' ? '本地模拟退款成功' : '本地模拟支付成功'}
                            </button>
                          )}
                          <button className="button secondary" onClick={() => navigate('orders')}>
                            查看我的订单
                          </button>
                        </>
                      )}
                      {proposal.action === 'create_payment' && (
                        <p className="local-payment-note">仅本地模拟，不扣款、不接真实支付。</p>
                      )}
                    </section>
                  )}
                </aside></ShopDrawer>}
              </div>
            </>
          )}
          {tab === 'favorites' && (
            <section>
              <div className="shop-section-heading">
                <div>
                  <div className="eyebrow">SAVED FOR LATER</div>
                  <h1>先收藏，再心动</h1>
                  <p>喜欢的好物，留着慢慢挑。</p>
                </div>
              </div>
              {!session ? (
                <div className="shop-empty">
                  <Bookmark size={36} />
                  <h2>把喜欢的商品留下来</h2>
                  <button className="button primary" onClick={() => setLogin(true)}>
                    登录查看收藏
                  </button>
                </div>
              ) : favorites.length ? (
                <div className="favorites-grid">{favorites.map((p) => renderCard(p))}</div>
              ) : (
                <div className="shop-empty">
                  <Bookmark size={36} />
                  <h2>还没有收藏</h2>
                  <p>商品卡片上的书签，可以替你记住心动。</p>
                  <button className="button secondary" onClick={() => navigate('guide')}>
                    去发现好物
                  </button>
                </div>
              )}
            </section>
          )}
          {tab === 'orders' && (
            <Orders
              key={session?.csrfToken ?? 'guest'}
              session={session}
              onLogin={() => setLogin(true)}
              onExpired={expired}
              onPay={(id) => void pay(id)}
              onAfterSale={(path, body) => { navigate('guide'); void action(path, body) }}
            />
          )}
          {tab === 'benefits' && <Benefits key={session?.csrfToken ?? 'guest'} csrf={csrf} loggedIn={!!session} onLogin={() => setLogin(true)} />}
          <ShopDrawer title="后端执行详情" open={observerOpen} onClose={()=>setObserverOpen(false)}><BackendObserver csrf={csrf} loggedIn={!!session} /></ShopDrawer>
          <footer className="page-footer">
            <span>拾物 · 好选择，从聊聊开始</span>
            <span>本地交易体验 · 不提供真实支付</span>
          </footer>
        </main>
      </div>
      {showHistory && <ConversationHistory key={session?.username ?? 'guest'} csrf={csrf} onClose={() => setShowHistory(false)} onContinue={chat => { setShowHistory(false); void openConversation(chat) }} />}
      {login && (
        <AuthDialog
          onClose={() => setLogin(false)}
          onSession={(value) => {
            setSession(value)
            setRestoring(false)
            setLogin(false)
            setError('')
          }}
        />
      )}
    </div>
  )
}
