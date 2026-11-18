import { useCallback, useEffect, useState } from 'react'
import { ArrowRight, Activity, ShieldCheck, Wallet, TrendingUp, Layers, RefreshCw, Pause, Play, AlertTriangle, Clock3 } from 'lucide-react'

const money = value => value == null ? 'Unavailable' : new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 2 }).format(value)
const time = value => value ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' }) : '—'
const needsAttention = monitor => !['ENTRY_PENDING', 'EXIT_PROTECTED'].includes(monitor.stage) || Boolean(monitor.last_error)

export function OverviewPanel({ request, notify, navigate }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [pausing, setPausing] = useState(false)
  const refresh = useCallback(async () => {
    setLoading(true); setError('')
    try { setData(await request('/api/account/summary')) }
    catch (e) { setError(e.message) }
    finally { setLoading(false) }
  }, [request])
  useEffect(() => { refresh() }, [refresh])
  const pause = async () => {
    setPausing(true)
    try {
      const next = await request('/api/auto-trade/pause', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ paused: !data.status.paused }) })
      setData(current => ({ ...current, status: { ...current.status, paused: next.paused } }))
      notify(next.paused ? 'New entries paused' : 'New entries resumed', 'Existing GTTs remain active. Review or cancel them in Kite.', 'info')
    } catch (e) { notify('Could not update entry controls', e.message, 'error') }
    finally { setPausing(false) }
  }
  const status = data?.status
  const attention = (data?.monitors || []).filter(needsAttention)
  const metrics = [
    { title: 'Available cash', value: money(data?.available_cash), icon: Wallet, hint: 'Kite live balance' },
    { title: 'Day P&L', value: money(data?.day_pnl), icon: TrendingUp, hint: 'Broker reported daily positions', tone: data?.day_pnl < 0 ? 'negative' : 'positive' },
    { title: 'Open positions', value: data?.open_positions ?? '—', icon: Layers, hint: `${data?.holding_count ?? '—'} holdings in your account` },
    { title: 'Managed setups', value: data?.monitors?.length ?? '—', icon: ShieldCheck, hint: `${attention.length} need review`, tone: attention.length ? 'negative' : '' },
  ]
  return <div className="workspace-page">
    <section className="workspace-hero glass-card"><div><span className="trade-kicker">KITE CONNECT</span><h2>Account overview</h2></div><button className="secondary-button" onClick={refresh} disabled={loading}><RefreshCw size={15} className={loading ? 'spin' : ''} /> Refresh</button></section>
    {error && <div className="trade-error" role="alert"><AlertTriangle size={17} />{error}</div>}
    {data?.warnings?.map(warning => <div className="workspace-warning" key={warning}><AlertTriangle size={15} />{warning}</div>)}
    <div className="workspace-metrics" aria-busy={loading}>{metrics.map(({ title, value, icon: Icon, hint, tone }) => <article className="metric-card glass-card" key={title}><div className="metric-label"><span>{title}</span><Icon size={17} /></div><strong className={tone}>{loading && !data ? 'Loading…' : value}</strong><small>{hint}</small></article>)}</div>
    <div className="workspace-columns"><section className="workspace-section glass-card"><div className="trade-section-head"><div><span className="trade-kicker">EXECUTION</span><h2>Trading controls</h2></div><ShieldCheck size={19} /></div><div className="health-grid"><div><span>Order mode</span><strong className="state-pill">{status?.mode === 'paper' ? 'Simulation' : status?.mode === 'live' ? 'Live orders' : 'Unavailable'}</strong></div><div><span>Order service</span><strong className={`state-pill ${status?.worker_online ? 'healthy' : 'caution'}`}>{status?.worker_online ? 'Online' : 'Offline'}</strong></div><div><span>New entries</span><strong>{status?.paused ? 'Paused' : status?.mode === 'live' && status.worker_online ? 'Active' : 'Unavailable'}</strong></div></div><button className="secondary-button pause-control" title="Pauses new entries. Existing orders remain active." onClick={pause} disabled={!status?.persistent || pausing}>{status?.paused ? <Play size={14} /> : <Pause size={14} />}{pausing ? 'Updating…' : status?.paused ? 'Resume entries' : 'Pause entries'}</button></section>
      <section className="workspace-section glass-card"><div className="trade-section-head"><div><span className="trade-kicker">WORKSPACE</span><h2>{attention.length ? 'Needs attention' : 'Quick access'}</h2></div><Activity size={19} /></div>{attention.length ? <><p className="workspace-caption">{attention.length} managed setup(s) need review.</p><button className="workspace-link" onClick={() => navigate('Activity')}>View activity <ArrowRight size={15} /></button></> : <div className="workspace-steps">{[['Strategies', '01', 'Strategy library', 'Saved logic'], ['Auto Trade', '02', 'Auto Trade', 'Scans and execution'], ['Activity', '03', 'Activity', 'Orders and events']].map(([tab, n, title, description]) => <button key={tab} onClick={() => navigate(tab)}><span>{n}</span><div><strong>{title}</strong><small>{description}</small></div><ArrowRight size={15} /></button>)}</div>}</section></div>
    {data?.as_of && <p className="workspace-caption snapshot-time"><Clock3 size={13} /> Snapshot from {time(data.as_of)}</p>}
  </div>
}

export function ActivityPanel({ request, notify }) {
  const [events, setEvents] = useState([])
  const [monitors, setMonitors] = useState([])
  const [history, setHistory] = useState([])
  const [filter, setFilter] = useState('all')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [rechecking, setRechecking] = useState('')
  const recheck = async item => {
    setRechecking(item.id)
    try {
      const result = await request(`/api/auto-trade/monitors/${item.id}/reconcile`, { method: 'POST' })
      notify?.('Kite state verified', result.message, 'success')
      const data = await request('/api/auto-trade/activity')
      setEvents(data.events || []); setMonitors(data.monitors || []); setHistory(data.history || [])
    } catch (e) { notify?.('Setup still needs review', e.message, 'warning'); setError(e.message) }
    finally { setRechecking('') }
  }
  useEffect(() => {
    let active = true; let timer
    const poll = async () => {
      try {
        const data = await request('/api/auto-trade/activity')
        if (active) { setEvents(data.events || []); setMonitors(data.monitors || []); setHistory(data.history || []); setError('') }
      } catch (e) { if (active) setError(e.message) }
      finally { if (active) { setLoading(false); timer = setTimeout(poll, 15000) } }
    }
    poll(); return () => { active = false; clearTimeout(timer) }
  }, [request])
  const visible = events.filter(event => filter === 'all' || ['error', 'warning'].includes(event.level))
  return <div className="workspace-page">
    {error && <div className="trade-error" role="alert"><AlertTriangle size={17} />{error}</div>}
    <section className="workspace-section glass-card"><div className="trade-section-head"><div><span className="trade-kicker">ORDER LIFECYCLE</span><h2>Managed setups <small>{monitors.length}</small></h2></div><span className="preview-age">Refreshes every 15s</span></div>
      {monitors.length ? <div className="preview-table-wrap"><table className="preview-table lifecycle-table"><thead><tr><th>Symbol</th><th>State</th><th>Quantity</th><th>Entry</th><th>Stop / target</th><th>Estimated risk</th><th>Created</th><th>Review</th></tr></thead><tbody>{monitors.map(item => <tr key={item.id}><td><strong>{item.symbol}</strong></td><td><span className={`state-pill ${needsAttention(item) ? 'caution' : 'healthy'}`}>{item.stage.replaceAll('_', ' ')}</span>{item.last_error && <small>Verification failed: {item.last_error}</small>}</td><td>{item.filled_quantity ?? item.quantity ?? '—'}</td><td>{money(item.entry)}</td><td>{money(item.stop_loss)} / {money(item.target)}</td><td>{money(item.risk)}</td><td>{time(item.created_at)}</td><td>{needsAttention(item) && <button className="secondary-button" disabled={Boolean(rechecking)} onClick={() => recheck(item)}><RefreshCw size={12} className={rechecking === item.id ? 'spin' : ''} /> Recheck with Kite</button>}</td></tr>)}</tbody></table></div> : <div className="trade-empty compact"><ShieldCheck size={22} /><strong>{loading ? 'Loading execution state…' : 'No managed live setups'}</strong><p>Submitted orders appear here.</p></div>}
    </section>
    {history.length > 0 && <section className="workspace-section glass-card"><div className="trade-section-head"><div><span className="trade-kicker">AUDIT TRAIL</span><h2>Order history</h2></div><span className="preview-age">{history.length} recent records</span></div><div className="preview-table-wrap"><table className="preview-table lifecycle-table"><thead><tr><th>Symbol</th><th>Quantity</th><th>Entry GTT</th><th>Exit GTT</th><th>Closed</th><th>Outcome</th></tr></thead><tbody>{history.map(item => <tr key={item.id}><td><strong>{item.symbol}</strong></td><td>{item.filled_quantity ?? item.quantity ?? "—"}</td><td>{item.entry_gtt_id || "—"}</td><td>{item.exit_gtt_id || "—"}</td><td>{time(item.closed_at)}</td><td>{item.reason}</td></tr>)}</tbody></table></div></section>}
    <section className="workspace-section glass-card"><div className="trade-section-head"><div><span className="trade-kicker">ACCOUNT EVENTS</span><h2>Activity stream</h2></div><div className="activity-filters" aria-label="Filter activity">{[['all', 'All events'], ['attention', 'Needs attention']].map(([value, label]) => <button className={filter === value ? 'active' : ''} key={value} onClick={() => setFilter(value)}>{label}</button>)}</div></div><div className="event-stream">{visible.length ? visible.map(event => <article className={`event-row ${event.level}`} key={event.id}><span className="event-dot" /><div><strong>{event.title}</strong><p>{event.message}</p><time>{time(event.at)}</time></div><span className="event-level">{event.level}</span></article>) : <div className="trade-empty compact"><Activity size={22} /><strong>{loading ? 'Loading activity…' : 'No events in this view'}</strong><p>Your latest 100 account events appear here, including scans, submissions, and worker alerts.</p></div>}</div></section>
  </div>
}
