import { useEffect, useRef, useState } from 'react'
import { Clock3, Layers, RefreshCw, Shield, ShieldCheck } from 'lucide-react'
import { InfoTip } from './Notifications'

const inr = new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR' })
const ist = new Intl.DateTimeFormat('en-IN', {
  timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', second: '2-digit',
  fractionalSecondDigits: 3, hour12: false,
})
const active = job => ['waiting', 'running'].includes(job?.state)
const marketClock = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Kolkata', weekday: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
})

export function openingWindow(now) {
  const parts = Object.fromEntries(marketClock.formatToParts(now).map(({ type, value }) => [type, value]))
  const minutes = Number(parts.hour) * 60 + Number(parts.minute)
  if (['Sat', 'Sun'].includes(parts.weekday) || minutes < 9 * 60 || minutes >= 15 * 60 + 20) return 'closed'
  return minutes < 9 * 60 + 15 ? 'pre-open' : 'regular'
}

export function OpeningStopPanel({ request, notify }) {
  const [data, setData] = useState(null)
  const [rows, setRows] = useState([])
  const [showSnapshot, setShowSnapshot] = useState(true)
  const [busy, setBusy] = useState(false)
  const [snapshotError, setSnapshotError] = useState('')
  const [actionError, setActionError] = useState('')
  const [windowState, setWindowState] = useState(() => openingWindow(new Date()))
  const wasRunning = useRef(false)
  const mutationVersion = useRef(0)
  const running = active(data?.job)
  const job = data?.job

  useEffect(() => {
    const update = () => setWindowState(openingWindow(new Date()))
    const timer = setInterval(update, 5000)
    window.addEventListener('focus', update)
    return () => { clearInterval(timer); window.removeEventListener('focus', update) }
  }, [])

  useEffect(() => {
    if (wasRunning.current && job?.state === 'complete') notify?.('Opening stop-loss run complete', job.message, 'success')
    if (wasRunning.current && job?.state === 'review') notify?.('Opening stops need review', job.message, 'warning')
    wasRunning.current = running
  }, [job?.state, job?.message, notify, running])

  async function refresh() {
    setBusy(true)
    setSnapshotError('')
    setActionError('')
    try {
      const result = await request('/api/opening-stop?snapshot=true')
      setData(result)
      setRows(result.rows)
      setShowSnapshot(!result.job || !active(result.job))
    } catch (err) { setSnapshotError(err.message) }
    finally { setBusy(false) }
  }

  useEffect(() => { refresh() }, [request])
  useEffect(() => {
    if (!running) return
    let disposed = false
    let timer
    async function poll() {
      const version = mutationVersion.current
      try {
        const result = await request('/api/opening-stop')
        if (disposed || version !== mutationVersion.current) return
        setData(result)
        setSnapshotError('')
      } catch (err) {
        if (!disposed && version === mutationVersion.current) setSnapshotError(err.message)
      } finally {
        if (!disposed) timer = setTimeout(poll, 1500)
      }
    }
    timer = setTimeout(poll, 1000)
    return () => { disposed = true; clearTimeout(timer) }
  }, [request, running])

  async function change(path) {
    setBusy(true)
    setActionError('')
    mutationVersion.current += 1
    try {
      const job = await request(path, { method: 'POST' })
      mutationVersion.current += 1
      setData(current => ({ ...current, job }))
      setShowSnapshot(false)
    } catch (err) { setActionError(err.message) }
    finally { setBusy(false) }
  }

  const shown = !showSnapshot && job ? job.rows : running && job.rows.length ? job.rows : rows
  const confirmed = shown.filter(row => row.covered === row.quantity).length
  const closed = windowState === 'closed'
  const blocked = closed ? 'Arming is available weekdays, 9:00 AM–3:20 PM IST.' : snapshotError ? 'Refresh Kite positions before arming.' : !data?.available ? 'The live order worker must be online.' : ''
  const status = running ? (job.state === 'waiting' ? 'Armed for 9:15 AM' : 'Applying protection')
    : !showSnapshot && job ? ({ complete: 'Run complete', review: 'Needs review', stopped: 'Run stopped' }[job.state] || 'Run status')
      : closed ? 'Arming closed' : windowState === 'pre-open' ? 'Pre-open' : 'Arming available'
  const statusTone = (!showSnapshot && ['review', 'stopped'].includes(job?.state)) || (!running && closed) ? 'caution' : 'healthy'
  return <section className="glass-card opening-card" aria-label="Opening Stop Loss">
    <div className="opening-header">
      <div className="opening-title">
        <span className="opening-emblem"><ShieldCheck size={23} /></span>
        <div><h2>Intraday protection <InfoTip text="Uses filled NSE and BSE equity MIS positions. Stops target 5% below average buy price. Requests can start at 9:15 AM IST." /></h2>
          <p>Regular day stops <span aria-hidden="true">·</span> NSE and BSE equity MIS</p>
        </div>
      </div>
      <div className="trade-head-actions">
        <button className="secondary-button" type="button" disabled={busy || running} onClick={refresh} aria-label="Refresh Kite positions"><RefreshCw size={15} /> Refresh</button>
        {running
          ? <button className="secondary-button" type="button" disabled={busy} onClick={() => change('/api/opening-stop/stop')}>Stop run</button>
          : <button className="primary-button trade-primary" type="button" disabled={busy || Boolean(blocked)} title={blocked || 'Apply regular stop-loss orders'} aria-describedby="opening-window" onClick={() => change('/api/opening-stop')}><ShieldCheck size={16} /> Apply SL-M</button>}
      </div>
    </div>
    <div className="opening-state" role="status" id="opening-window">
      <span className={`state-pill ${statusTone}`}><Clock3 size={14} /> {status}</span>
      <span>{!showSnapshot && job?.message ? job.message : closed ? 'Weekdays · 9:00 AM–3:20 PM IST' : '5% below average buy price'}</span>
      {data && !data.available && <span className="opening-worker-offline">Live worker offline</span>}
    </div>
    {actionError && <div className="trade-error" role="alert">{actionError}</div>}
    {snapshotError && <div className="trade-error" role="alert">{snapshotError}{data && ' Showing the last available snapshot.'}</div>}
    {!data && !snapshotError && <div className="opening-empty" role="status">Checking Kite positions…</div>}
    {data && <><div className="opening-metrics">
      <div><Layers size={17} /><span>Long positions</span><strong>{shown.length}</strong></div>
      <div><Shield size={17} /><span>Protection confirmed</span><strong>{confirmed}</strong><InfoTip text="Counts positions with full quantity confirmed at the requested stop trigger." /></div>
    </div>
    {!!shown.length && <div className="preview-table-wrap opening-table">
      <table className="preview-table">
        <thead><tr>{['Stock', 'Net qty', 'Avg. buy', 'Position cost', 'New trigger', 'Existing stops', 'Covered', 'Last send attempt (IST)', 'Confirmation observed (IST)', 'Status'].map(label => <th key={label} scope="col">{label}</th>)}</tr></thead>
        <tbody>{shown.map(row => <tr key={row.key}>
          <th scope="row">{row.key}</th><td>{row.quantity.toLocaleString('en-IN')}</td>
          <td>{inr.format(row.average_price)}</td><td>{inr.format(row.investment)}</td>
          <td>{row.trigger == null ? '—' : inr.format(row.trigger)}</td>
          <td>{row.stops.length ? row.stops.map(stop => <span key={stop.id} style={{ display: 'block' }}>{stop.type} · {stop.quantity ?? '—'} @ {stop.trigger ? inr.format(stop.trigger) : '—'} · {stop.status}</span>) : 'None'}</td>
          <td>{row.covered} / {row.quantity}</td>
          <td>{job?.attempts?.[row.key] ? ist.format(new Date(job.attempts[row.key])) : '—'}</td>
          <td>{job?.confirmations?.[row.key] ? ist.format(new Date(job.confirmations[row.key])) : '—'}</td>
          <td>{row.status}</td>
        </tr>)}</tbody>
      </table>
    </div>}
    {!shown.length && !snapshotError && <div className="opening-empty"><Layers size={22} /><strong>{busy ? 'Checking Kite…' : running ? 'Waiting for positions' : 'No filled intraday longs'}</strong></div>}</>}
  </section>
}
