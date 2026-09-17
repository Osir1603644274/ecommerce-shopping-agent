import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import {
  ArrowDown,
  ArrowRight,
  Check,
  CheckCircle2,
  ChevronRight,
  Clock3,
  Copy,
  CreditCard,
  Eye,
  EyeOff,
  FlaskConical,
  Headphones,
  Info,
  LoaderCircle,
  LogIn,
  LogOut,
  Package,
  ReceiptText,
  RefreshCw,
  Search,
  ShieldCheck,
  ShoppingBag,
  Smartphone,
  Sparkles,
  X,
} from 'lucide-react'
import { api, ApiError, errorMessage, isSessionError } from './lib/api'
import { dateLabel, money, parseOrder, parseSession, statuses } from './lib/orders'
import type { Order, OrderItem, Session, StatusFilter } from './lib/orders'
import { useOrders } from './hooks/useOrders'
import './App.css'

let restoreInFlight: Promise<Session> | null = null
export function restoreSession() {
  // /me rotates CSRF. Share StrictMode's overlapping bootstrap requests.
  if (!restoreInFlight)
    restoreInFlight = api('/me')
      .then(parseSession)
      .finally(() => {
        restoreInFlight = null
      })
  return restoreInFlight
}
function Modal({
  children,
  onClose,
  className,
  label,
  busy = false,
}: {
  children: ReactNode
  onClose: () => void
  className: string
  label: string
  busy?: boolean
}) {
  const ref = useRef<HTMLDialogElement>(null)
  const returnFocus = useRef<HTMLElement | null>(null)
  useEffect(() => {
    const dialog = ref.current
    if (!returnFocus.current && document.activeElement instanceof HTMLElement)
      returnFocus.current = document.activeElement
    dialog?.showModal()
    return () => {
      dialog?.close()
      if (returnFocus.current?.isConnected) returnFocus.current.focus()
    }
  }, [])
  return (
    <dialog
      ref={ref}
      className={className}
      aria-label={label}
      onCancel={(event) => {
        event.preventDefault()
        if (!busy) onClose()
      }}
    >
      <button
        className="icon-button dialog-close"
        aria-label="关闭窗口"
        onClick={onClose}
        disabled={busy}
      >
        <X size={20} />
      </button>
      {children}
    </dialog>
  )
}
function ProductIcon({ title }: { title: string }) {
  return (
    <span
      className={`product-icon ${title.includes('耳机') ? 'headphone' : ''}`}
      aria-hidden="true"
    >
      {title.includes('耳机') ? (
        <Headphones size={32} strokeWidth={1.35} />
      ) : (
        <Smartphone size={32} strokeWidth={1.35} />
      )}
    </span>
  )
}
function ItemRow({ item, currency }: { item: OrderItem; currency: string }) {
  return (
    <div className="item-row">
      <ProductIcon title={item.titleSnapshot} />
      <div className="item-content">
        <h3>{item.titleSnapshot}</h3>
        <p>
          商品 #{item.itemId} <span className="dot">·</span> 数量 × {item.quantity}
        </p>
      </div>
      <div className="item-price">
        <span>{money(item.subtotalMinor, currency)}</span>
        <small>单价 {money(item.unitPriceMinor, currency)}</small>
      </div>
    </div>
  )
}
export function OrderCard({ order, onDetail }: { order: Order; onDetail: (order: Order) => void }) {
  return (
    <article className="order-card" aria-label={`订单 ${order.orderNo}`}>
      <header className="order-card-header">
        <div className="order-meta">
          <span>{dateLabel(order.createdAt)}</span>
          <span className="order-number">订单号 {order.orderNo}</span>
        </div>
        <span className={`status-badge status-${order.status.toLowerCase()}`}>
          <i />
          {statuses[order.status]}
        </span>
      </header>
      <div className="order-items">
        {order.items.length ? (
          order.items.map((item) => <ItemRow key={item.id} item={item} currency={order.currency} />)
        ) : (
          <p className="muted empty-items">此订单暂无商品明细</p>
        )}
      </div>
      <footer className="order-card-footer">
        <p>
          共 {order.items.reduce((sum, item) => sum + item.quantity, 0)} 件商品
          <span className="total-label">
            {order.status === 'PENDING_PAYMENT' ? '应付' : '订单金额'}{' '}
            <strong>{money(order.payableMinor, order.currency)}</strong>
          </span>
        </p>
        <button className="button secondary small" onClick={() => onDetail(order)}>
          订单详情 <ChevronRight size={15} />
        </button>
      </footer>
    </article>
  )
}
export function Detail({
  selected,
  demo,
  csrf,
  onClose,
  onExpired,
  afterSales,
}: {
  selected: Order
  demo: boolean
  csrf: string | null
  onClose: () => void
  onExpired: () => void
  afterSales?: React.ReactNode
}) {
  const [order, setOrder] = useState<Order | null>(demo ? selected : null)
  const [error, setError] = useState('')
  const [revision, setRevision] = useState(0)
  const [copied, setCopied] = useState(false)
  useEffect(() => {
    if (demo) return
    const controller = new AbortController()
    setOrder(null)
    setError('')
    api(`/orders/${encodeURIComponent(selected.id)}`, {
      signal: controller.signal,
      headers: { 'X-CSRF-Token': csrf ?? '' },
    })
      .then(parseOrder)
      .then((value) => {
        if (value.id !== selected.id) throw new Error('服务返回的订单不匹配')
        if (!controller.signal.aborted) setOrder(value)
      })
      .catch((caught) => {
        if (controller.signal.aborted) return
        if (isSessionError(caught)) onExpired()
        else setError(errorMessage(caught))
      })
    return () => controller.abort()
  }, [selected.id, demo, csrf, onExpired, revision])
  useEffect(() => {
    if (copied) {
      const timer = setTimeout(() => setCopied(false), 2000)
      return () => clearTimeout(timer)
    }
  }, [copied])
  return (
    <Modal label="订单详情" className="detail-dialog" onClose={onClose}>
      <div className="eyebrow">ORDER DETAILS</div>
      <h2>订单详情</h2>
      {demo && (
        <p className="demo-label">
          <FlaskConical size={15} /> 示例数据，不是真实交易
        </p>
      )}
      {!order && !error && (
        <div className="loading-state" role="status">
          <LoaderCircle className="spin" /> 正在核对最新订单状态…
        </div>
      )}
      {error && (
        <div className="error-box" role="alert">
          <p>{error}</p>
          <button className="button secondary" onClick={() => setRevision((value) => value + 1)}>
            重试详情
          </button>
        </div>
      )}
      {order && (
        <>
          <div className={`detail-status status-${order.status.toLowerCase()}`}>
            <ReceiptText size={24} />
            <div>
              <strong>{statuses[order.status]}</strong>
              <p>以订单服务返回的状态为准</p>
            </div>
          </div>
          <div className="detail-reference">
            <span>{order.orderNo}</span>
            <button
              className="icon-button"
              aria-label="复制订单号"
              onClick={() => {
                void navigator.clipboard
                  .writeText(order.orderNo)
                  .then(() => setCopied(true))
                  .catch(() => setError('复制失败，请手动选择订单号复制'))
              }}
            >
              {copied ? <Check size={17} /> : <Copy size={17} />}
            </button>
            <span role="status" className="muted">
              {copied ? '已复制' : ''}
            </span>
          </div>
          <div className="detail-items">
            {order.items.map((item) => (
              <ItemRow key={item.id} item={item} currency={order.currency} />
            ))}
          </div>
          <dl className="price-breakdown">
            <div>
              <dt>商品总额</dt>
              <dd>{money(order.totalMinor, order.currency)}</dd>
            </div>
            <div>
              <dt>优惠抵扣</dt>
              <dd>−{money(order.discountMinor, order.currency)}</dd>
            </div>
            <div className="payable">
              <dt>订单应付金额</dt>
              <dd>{money(order.payableMinor, order.currency)}</dd>
            </div>
          </dl>
          <dl className="detail-dates">
            <div>
              <dt>创建时间</dt>
              <dd>{dateLabel(order.createdAt)}</dd>
            </div>
            <div>
              <dt>支付时间</dt>
              <dd>{dateLabel(order.paidAt)}</dd>
            </div>
            <div>
              <dt>支付截止时间</dt>
              <dd>{dateLabel(order.expiresAt)}</dd>
            </div>
          </dl>
          {afterSales || <p className="detail-note">
            <ShieldCheck size={17} />
            此页面只读，不会发起支付、取消订单或退款。
          </p>}
        </>
      )}
    </Modal>
  )
}
export function AuthDialog({
  onClose,
  onSession,
}: {
  onClose: () => void
  onSession: (value: Session) => void
}) {
  const [register, setRegister] = useState(false)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [visible, setVisible] = useState(false)
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
  const busy = useRef(false)
  const controller = useRef<AbortController | null>(null)
  useEffect(() => () => controller.current?.abort(), [])
  async function submit(event: FormEvent) {
    event.preventDefault()
    if (busy.current) return
    if (register && password !== confirm) {
      setError('两次输入的密码不一致')
      return
    }
    busy.current = true
    setPending(true)
    setError('')
    const request = new AbortController()
    controller.current = request
    try {
      const result = parseSession(
        await api(register ? '/register' : '/login', {
          method: 'POST',
          body: JSON.stringify({ username: username.trim(), password }),
          signal: request.signal,
        }),
      )
      if (!request.signal.aborted) {
        setPassword('')
        setConfirm('')
        onSession(result)
      }
    } catch (caught) {
      if (!request.signal.aborted) setError(errorMessage(caught))
    } finally {
      busy.current = false
      if (!request.signal.aborted) setPending(false)
    }
  }
  return (
    <Modal
      label={register ? '注册账户' : '登录账户'}
      className="auth-dialog"
      onClose={onClose}
      busy={pending}
    >
      <div className="auth-symbol">
        <ShoppingBag size={27} />
      </div>
      <div className="eyebrow">YOUR SHOPPING, TOGETHER</div>
      <h2>{register ? '开始记录每一次好选择' : '欢迎回来'}</h2>
      <p className="muted">
        {register ? '创建账户，查看属于你的订单。' : '登录后，继续查看你的真实订单。'}
      </p>
      <form onSubmit={(event) => void submit(event)}>
        <label htmlFor="username">用户名</label>
        <input
          id="username"
          name="username"
          value={username}
          onChange={(event) => setUsername(event.target.value)}
          required
          minLength={3}
          maxLength={64}
          pattern="[A-Za-z0-9._\-]+"
          autoComplete="username"
          placeholder="请输入用户名"
          disabled={pending}
        />
        <small className="field-hint">3–64 位字母、数字、点、下划线或短横线</small>
        <label htmlFor="password">密码</label>
        <div className="password-field">
          <input
            id="password"
            name="password"
            type={visible ? 'text' : 'password'}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            required
            minLength={8}
            maxLength={128}
            autoComplete={register ? 'new-password' : 'current-password'}
            placeholder="至少 8 位字符"
            disabled={pending}
          />
          <button
            type="button"
            className="icon-button"
            aria-label={visible ? '隐藏密码' : '显示密码'}
            onClick={() => setVisible((value) => !value)}
          >
            {visible ? <EyeOff size={18} /> : <Eye size={18} />}
          </button>
        </div>
        {register && (
          <>
            <label htmlFor="confirm">确认密码</label>
            <input
              id="confirm"
              type="password"
              value={confirm}
              onChange={(event) => setConfirm(event.target.value)}
              required
              autoComplete="new-password"
              disabled={pending}
            />
          </>
        )}
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <button className="button primary auth-submit" type="submit" disabled={pending}>
          {pending ? <LoaderCircle className="spin" size={18} /> : <ArrowRight size={18} />}
          {pending ? '正在验证…' : register ? '注册并登录' : '登录账户'}
        </button>
      </form>
      <p className="auth-switch">
        {register ? '已经有账户？' : '还没有账户？'}
        <button
          className="text-button"
          disabled={pending}
          onClick={() => {
            setRegister((value) => !value)
            setError('')
            setPassword('')
            setConfirm('')
          }}
        >
          {register ? '去登录' : '创建账户'}
        </button>
      </p>
      <p className="security-note">
        <ShieldCheck size={15} />
        使用 HttpOnly 会话，JWT 不会存入浏览器。
      </p>
    </Modal>
  )
}

function App() {
  const [demo, setDemo] = useState(
    () => new URLSearchParams(window.location.search).get('demo') === '1',
  )
  const [session, setSession] = useState<Session | null>(null)
  const [authVersion, setAuthVersion] = useState(0)
  const [restoring, setRestoring] = useState(!demo)
  const [notice, setNotice] = useState('')
  const [authOpen, setAuthOpen] = useState(false)
  const [loggingOut, setLoggingOut] = useState(false)
  const [status, setStatus] = useState<StatusFilter>('')
  const [search, setSearch] = useState('')
  const [selected, setSelected] = useState<Order | null>(null)
  const [restoreRevision, setRestoreRevision] = useState(0)
  const authGeneration = useRef(0)
  const logoutBusy = useRef(false)
  const expired = useCallback(() => {
    authGeneration.current++
    setSession(null)
    setAuthVersion((value) => value + 1)
    setSelected(null)
    setStatus('')
    setSearch('')
    setNotice('登录已过期或会话已在其他页面更新，请重新登录后查看订单。')
  }, [])
  useEffect(() => {
    const gen = ++authGeneration.current
    if (demo) {
      setRestoring(false)
      return
    }
    let live = true
    setRestoring(true)
    restoreSession()
      .then((value) => {
        if (live && gen === authGeneration.current) {
          setSession(value)
          setAuthVersion((version) => version + 1)
          setNotice('')
        }
      })
      .catch((caught) => {
        if (
          live &&
          gen === authGeneration.current &&
          !(caught instanceof ApiError && caught.status === 401)
        )
          setNotice(errorMessage(caught))
      })
      .finally(() => {
        if (live && gen === authGeneration.current) setRestoring(false)
      })
    return () => {
      live = false
    }
  }, [demo, restoreRevision])
  const orders = useOrders(
    session ? `${session.username}:${authVersion}` : null,
    demo,
    status,
    session?.csrfToken ?? null,
    expired,
  )
  const canView = demo || !!session
  const visibleOrders = orders.orders.filter((order) => {
    const term = search.trim().toLowerCase()
    return (
      !term ||
      order.orderNo.toLowerCase().includes(term) ||
      order.items.some((item) => item.titleSnapshot.toLowerCase().includes(term))
    )
  })
  function changeStatus(value: StatusFilter) {
    setStatus(value)
    setSearch('')
    setSelected(null)
  }
  function toggleDemo(value: boolean) {
    if (logoutBusy.current) return
    authGeneration.current++
    setSession(null)
    setAuthVersion((version) => version + 1)
    setDemo(value)
    setStatus('')
    setSearch('')
    setSelected(null)
    setNotice('')
    setAuthOpen(false)
    const url = new URL(window.location.href)
    if (value) url.searchParams.set('demo', '1')
    else url.searchParams.delete('demo')
    window.history.replaceState(null, '', url)
  }
  async function logout() {
    if (!session || logoutBusy.current) return
    logoutBusy.current = true
    setLoggingOut(true)
    const csrf = session.csrfToken
    authGeneration.current++
    setSession(null)
    setSelected(null)
    setStatus('')
    setSearch('')
    setAuthVersion((value) => value + 1)
    setNotice('')
    try {
      await api('/logout', { method: 'POST', headers: { 'X-CSRF-Token': csrf } })
    } catch (caught) {
      if (!(caught instanceof ApiError && caught.status === 401))
        setNotice('已隐藏本页订单，但服务端退出未确认。请重试登录后退出，或等待会话过期。')
    } finally {
      authGeneration.current++
      setSession(null)
      setSelected(null)
      setAuthVersion((value) => value + 1)
      logoutBusy.current = false
      setLoggingOut(false)
    }
  }
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        跳到订单内容
      </a>
      <aside className="sidebar">
        <a className="brand" href="/">
          <span className="brand-mark">
            <ShoppingBag size={24} />
          </span>
          <span>
            拾物<span className="brand-subtitle">COMMERCE STUDIO</span>
          </span>
        </a>
        <div className="nav-label">我的购物空间</div>
        <nav aria-label="主导航">
          <a className="nav-item" href="/commerce-demo">
            <Sparkles size={19} />
            发现好物
            <ArrowRight size={15} />
          </a>
          <a className="nav-item active" href="#main-content" aria-current="page">
            <ReceiptText size={19} />
            我的订单
            <span className="nav-indicator" />
          </a>
        </nav>
        <div className="sidebar-bottom">
          <div className="trust-symbol">
            <ShieldCheck size={23} />
          </div>
          <strong>每一笔，都有据可查</strong>
          <p>
            订单信息来自交易服务
            <br />
            仅展示当前账户的数据
          </p>
          <div className="local-label">
            <i />
            本机演示环境
          </div>
        </div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <div className="breadcrumb">
            个人中心 <ChevronRight size={14} />
            <span>我的订单</span>
          </div>
          <div className="account-actions">
            {demo ? (
              <>
                <span className="demo-label">
                  <FlaskConical size={14} />
                  示例模式
                </span>
                <button
                  className="text-button"
                  disabled={loggingOut}
                  onClick={() => toggleDemo(false)}
                >
                  返回真实账户
                </button>
              </>
            ) : session ? (
              <>
                <span className="account-name">
                  <span className="avatar">{session.username.slice(0, 1).toUpperCase()}</span>
                  {session.username}
                </span>
                <button
                  className="icon-button"
                  aria-label="退出登录"
                  onClick={() => void logout()}
                  disabled={loggingOut}
                >
                  <LogOut size={18} />
                </button>
              </>
            ) : (
              <button
                className="button secondary small"
                onClick={() => setAuthOpen(true)}
                disabled={restoring || loggingOut}
              >
                <LogIn size={16} />
                {restoring ? '检查会话…' : loggingOut ? '退出中…' : '登录 / 注册'}
              </button>
            )}
          </div>
        </header>
        <main id="main-content">
          <section className="page-heading">
            <div>
              <div className="eyebrow">YOUR ORDERS</div>
              <h1>
                我的订单<span className="heading-dot">.</span>
              </h1>
              <p>从心动到收下，每一笔选择都在这里。</p>
            </div>
            <a className="button primary" href="/commerce-demo">
              继续逛逛 <ArrowRight size={17} />
            </a>
          </section>
          {demo && (
            <div className="demo-banner" role="status">
              <FlaskConical size={19} />
              <div>
                <strong>你正在浏览示例订单</strong>
                <span>这些数据仅用于体验界面，不关联账户，也不会产生交易。</span>
              </div>
              <button
                className="text-button"
                disabled={loggingOut}
                onClick={() => toggleDemo(false)}
              >
                连接真实账户 <ArrowRight size={15} />
              </button>
            </div>
          )}
          {!demo && notice && (
            <div className="notice-banner" role="alert">
              <Info size={19} />
              <p>{notice}</p>
              {!session && (
                <button
                  className="text-button"
                  onClick={() => setRestoreRevision((value) => value + 1)}
                  disabled={restoring || loggingOut}
                >
                  重试连接
                </button>
              )}
            </div>
          )}
          {canView ? (
            <>
              <section className="stats-grid" aria-label="已加载订单概览">
                <div className="stat-card">
                  <span className="stat-icon">
                    <Package size={22} />
                  </span>
                  <div>
                    <span>当前筛选已加载</span>
                    <strong>
                      {orders.orders.length}
                      <small> 笔订单</small>
                    </strong>
                  </div>
                </div>
                <div className="stat-card">
                  <span className="stat-icon amber">
                    <Clock3 size={22} />
                  </span>
                  <div>
                    <span>其中待支付</span>
                    <strong>
                      {orders.orders.filter((order) => order.status === 'PENDING_PAYMENT').length}
                      <small> 笔</small>
                    </strong>
                  </div>
                </div>
                <div className="stat-card">
                  <span className="stat-icon green">
                    <CheckCircle2 size={22} />
                  </span>
                  <div>
                    <span>其中已完成</span>
                    <strong>
                      {orders.orders.filter((order) => order.status === 'COMPLETED').length}
                      <small> 笔</small>
                    </strong>
                  </div>
                </div>
              </section>
              <section className="orders-panel" aria-label="订单列表">
                <div className="filter-tabs" role="group" aria-label="订单状态筛选">
                  {(
                    [['', '全部订单'], ...Object.entries(statuses)] as [StatusFilter, string][]
                  ).map(([value, label]) => (
                    <button
                      key={value}
                      className={status === value ? 'selected' : ''}
                      aria-pressed={status === value}
                      onClick={() => changeStatus(value)}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                <div className="toolbar">
                  <label className="search-field">
                    <Search size={18} />
                    <input
                      aria-label="搜索已加载订单"
                      placeholder="在已加载订单中搜索商品或订单号"
                      value={search}
                      onChange={(event) => setSearch(event.target.value)}
                    />
                    {search && (
                      <button
                        className="icon-button"
                        aria-label="清空搜索"
                        onClick={() => setSearch('')}
                      >
                        <X size={16} />
                      </button>
                    )}
                  </label>
                  <button
                    className="button secondary small"
                    onClick={orders.refresh}
                    disabled={orders.loading}
                  >
                    <RefreshCw size={16} className={orders.loading ? 'spin' : ''} />
                    刷新订单
                  </button>
                </div>
                {orders.error && (
                  <div className="error-box" role="alert">
                    <Info size={18} />
                    <p>{orders.error}</p>
                    <button
                      className="text-button"
                      onClick={orders.loadMore}
                      disabled={orders.loading}
                    >
                      重试请求
                    </button>
                  </div>
                )}
                {!orders.loaded && orders.loading && (
                  <div className="skeleton-list" role="status" aria-label="正在加载订单">
                    {[1, 2, 3].map((value) => (
                      <div className="skeleton-card" key={value}>
                        <div />
                        <div />
                        <div />
                      </div>
                    ))}
                  </div>
                )}
                <div className="orders-list">
                  {visibleOrders.map((order) => (
                    <OrderCard key={order.id} order={order} onDetail={setSelected} />
                  ))}
                </div>
                {orders.loaded && visibleOrders.length === 0 && (
                  <div className="empty-state">
                    <span className="empty-symbol">
                      <Package size={35} strokeWidth={1.3} />
                    </span>
                    <h2>
                      {search
                        ? '已加载订单中没有匹配结果'
                        : status
                          ? '暂时没有这类订单'
                          : '你的第一笔好选择，还在路上'}
                    </h2>
                    <p>
                      {search
                        ? '试试其他关键词，或加载更多订单后再搜索。'
                        : status
                          ? '可以切换状态，查看其他订单。'
                          : '去发现喜欢的商品，完成下单后就能在这里查看。'}
                    </p>
                    {search ? (
                      <button className="button secondary" onClick={() => setSearch('')}>
                        清空搜索
                      </button>
                    ) : status ? (
                      <button className="button secondary" onClick={() => changeStatus('')}>
                        查看全部订单
                      </button>
                    ) : (
                      <a className="button primary" href="/commerce-demo">
                        去发现好物 <ArrowRight size={16} />
                      </a>
                    )}
                  </div>
                )}
                {orders.loaded && (
                  <div className="pagination-footer">
                    <div className="pagination-info">
                      <span>
                        {search ? `匹配 ${visibleOrders.length} 笔 · ` : ''}已加载{' '}
                        {orders.orders.length} 笔
                      </span>
                      <small>
                        {orders.updatedAt &&
                          `更新于 ${orders.updatedAt.toLocaleTimeString('zh-CN', { hour12: false })}`}
                      </small>
                    </div>
                    {orders.hasMore ? (
                      <button
                        className="button secondary"
                        onClick={orders.loadMore}
                        disabled={orders.loading}
                      >
                        {orders.loading ? (
                          <LoaderCircle size={16} className="spin" />
                        ) : (
                          <ArrowDown size={16} />
                        )}
                        {orders.loading ? '加载中…' : '加载更多订单'}
                      </button>
                    ) : (
                      <span className="end-label">
                        <Check size={15} />
                        已显示当前筛选的全部订单
                      </span>
                    )}
                  </div>
                )}
              </section>
              <p className="list-note">
                <Info size={14} />
                按创建时间从新到旧排列 · 金额与状态以交易服务为准 · 概览仅统计当前已加载订单
              </p>
            </>
          ) : (
            <section className="signin-gate">
              <div className="gate-art" aria-hidden="true">
                <div className="gate-orbit" />
                <div className="gate-receipt">
                  <ReceiptText size={58} strokeWidth={1} />
                  <span />
                  <span />
                </div>
                <span className="floating-check">
                  <ShieldCheck size={25} />
                </span>
              </div>
              <div className="eyebrow">A LITTLE SPACE FOR YOUR FINDS</div>
              <h2>{restoring ? '正在连接你的购物空间' : '你的好选择，值得好好记录'}</h2>
              <p>
                登录查看订单状态、商品明细与金额。
                <br />
                每个账户，都有自己的购物记录。
              </p>
              <div className="gate-actions">
                <button
                  className="button primary"
                  disabled={restoring || loggingOut}
                  onClick={() => setAuthOpen(true)}
                >
                  {restoring ? <LoaderCircle size={17} className="spin" /> : <LogIn size={17} />}
                  登录查看订单
                </button>
                <button
                  className="button secondary"
                  disabled={loggingOut}
                  onClick={() => toggleDemo(true)}
                >
                  <FlaskConical size={17} />
                  先看看示例
                </button>
              </div>
              <div className="gate-benefits">
                <span>
                  <Package size={16} />
                  多商品明细
                </span>
                <span>
                  <CreditCard size={16} />
                  金额清晰可查
                </span>
                <span>
                  <ShieldCheck size={16} />
                  账户独立隔离
                </span>
              </div>
            </section>
          )}
          <footer className="page-footer">
            <span>拾物 COMMERCE STUDIO</span>
            <span>本地交易演示 · 不提供真实支付</span>
          </footer>
        </main>
      </div>
      {authOpen && (
        <AuthDialog
          onClose={() => setAuthOpen(false)}
          onSession={(value) => {
            authGeneration.current++
            setSession(value)
            setAuthVersion((version) => version + 1)
            setRestoring(false)
            setNotice('')
            setStatus('')
            setSearch('')
            setAuthOpen(false)
          }}
        />
      )}
      {selected && canView && (
        <Detail
          key={`${demo}:${session?.username}:${authVersion}:${selected.id}`}
          selected={selected}
          demo={demo}
          csrf={session?.csrfToken ?? null}
          onClose={() => setSelected(null)}
          onExpired={expired}
        />
      )}
    </div>
  )
}
export default App
