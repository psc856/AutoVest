import { useEffect, useState } from 'react'
import { RefreshCw, ShieldCheck } from 'lucide-react'

const inr = new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR' })
const ist = new Intl.DateTimeFormat('en-IN', {
  timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', second: '2-digit',
  fractionalSecondDigits: 3, hour12: false,
})
const active = job => ['waiting', 'running'].includes(job?.state)

export function OpeningStopPanel({ request }) {
  const [data, setData] = useState(null)
  const [rows, setRows] = useState([])
  const [showSnapshot, setShowSnapshot] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const running = active(data?.job)

  async function refresh() {
    setBusy(true)
    setError('')
    try {
      const result = await request('/api/opening-stop?snapshot=true')
      setData(result)
      setRows(result.rows)
      setShowSnapshot(!result.job || !active(result.job))
    } catch (err) { setError(err.message) }
    finally { setBusy(false) }
  }

  useEffect(() => { refresh() }, [request])
  useEffect(() => {
    if (!running) return
    let disposed = false
    let timer
    async function poll() {
      try {
        const result = await request('/api/opening-stop')
        if (disposed) return
        setData(result)
        setError('')
      } catch (err) {
        if (!disposed) setError(err.message)
      } finally {
        if (!disposed) timer = setTimeout(poll, 1500)
      }
    }
    timer = setTimeout(poll, 1000)
    return () => { disposed = true; clearTimeout(timer) }
  }, [request, running])

  async function change(path) {
    setBusy(true)
    setError('')
    try {
      const job = await request(path, { method: 'POST' })
      setData(current => ({ ...current, job }))
      setShowSnapshot(false)
    } catch (err) { setError(err.message) }
    finally { setBusy(false) }
  }

  const job = data?.job
  const shown = !showSnapshot && job ? job.rows : running && job.rows.length ? job.rows : rows
  const confirmed = shown.filter(row => row.covered === row.quantity).length
  return <section className="glass-card trade-preview" aria-label="Opening Stop Loss">
    <div className="trade-section-head">
      <div><h2>Intraday protection</h2><p>Regular day stops · 5% below average buy price · NSE and BSE equity MIS</p></div>
      <div className="trade-head-actions">
        <button className="secondary-button" type="button" disabled={busy || running} onClick={refresh} aria-label="Refresh Kite positions"><RefreshCw size={15} /> Refresh</button>
        {running
          ? <button className="secondary-button" type="button" disabled={busy} onClick={() => change('/api/opening-stop/stop')}>Stop run</button>
          : <button className="primary-button trade-primary" type="button" disabled={busy || !data?.available} onClick={() => change('/api/opening-stop')}><ShieldCheck size={15} /> Apply SL-M</button>}
      </div>
    </div>
    {error && <div className="trade-error" role="alert">{error}</div>}
    <div className="trade-notice" role="status">
      {(!showSnapshot && job?.message) || (data?.available ? 'Applies protection from 9:15 AM IST and stops when this run is confirmed.' : data ? 'Live worker required to apply stops.' : 'Checking Kite positions…')}
      {job && !data?.available && ' Live worker is offline.'}
    </div>
    <div className="preview-summary">
      <div><strong>{shown.length}</strong><small>Long positions</small></div>
      <div><strong>{confirmed}</strong><small>Protection confirmed</small></div>
    </div>
    <div className="preview-table-wrap">
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
    </div>
    {!shown.length && <div className="trade-empty compact"><strong>{busy ? 'Checking Kite…' : 'No filled long MIS equity positions'}</strong><p>Pending buys appear once Kite reports a fill.</p></div>}
  </section>
}
