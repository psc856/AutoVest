import { useEffect, useMemo, useRef, useState } from 'react'
import { Activity, AlertTriangle, ArrowRight, Check, ChevronDown, Code2, FileCode2, LoaderCircle, Play, Plus, RefreshCw, ShieldCheck, Trash2, Upload, X } from 'lucide-react'
import { NumberField, SelectField } from './FormControls'
import { InfoTip } from './Notifications'
import connectionCheckSource from '../backend/strategy_templates/connection_check.py?raw'

export function StrategyLibraryPanel({ request, notify }) {
  const [strategies, setStrategies] = useState([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const fileRef = useRef(null)

  const refresh = async () => {
    setLoading(true); setError('')
    try { setStrategies((await request('/api/strategies')).strategies || []) }
    catch (e) { setError(e.message); notify?.('Could not load strategies', e.message, 'error') }
    finally { setLoading(false) }
  }
  useEffect(() => { refresh() }, [])

  const upload = async file => {
    if (!file) return
    if (!file.name.toLowerCase().endsWith('.py')) { setError('Choose a Python .py file.'); return }
    if (file.size > 65536) { setError('Choose a strategy file smaller than 64 KB.'); return }
    setBusy(true); setError('')
    try {
      const source = await file.text()
      const data = await request('/api/strategies', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ filename: file.name, source }) })
      setStrategies(current => [data.strategy, ...current])
      notify?.('Strategy added', `${data.strategy.name} is saved to your Kite account.`, 'success')
    } catch (e) { setError(e.message); notify?.('Strategy upload failed', e.message, 'error') }
    finally { setBusy(false); if (fileRef.current) fileRef.current.value = '' }
  }

  const remove = async strategy => {
    if (!window.confirm(`Delete “${strategy.name}”? This cannot be undone.`)) return
    try {
      await request(`/api/strategies/${strategy.id}`, { method: 'DELETE' })
      setStrategies(current => current.filter(item => item.id !== strategy.id))
      notify?.('Strategy deleted', `${strategy.name} was removed.`, 'success')
    } catch (e) { notify?.('Could not delete strategy', e.message, 'error') }
  }

  const downloadExample = () => {
    const link = document.createElement('a')
    link.href = URL.createObjectURL(new Blob([connectionCheckSource], { type: 'text/x-python' }))
    link.download = 'connection_check.py'; link.click(); window.setTimeout(() => URL.revokeObjectURL(link.href), 1000)
  }

  return <div className="trading-page">
    <section className="strategy-welcome glass-card"><div className="strategy-welcome-icon"><Code2 size={22} /></div><div><span className="trade-kicker">STRATEGIES</span><h2>Your logic library</h2></div><button className="secondary-button" onClick={downloadExample}><FileCode2 size={15} /> Download sample</button></section>
    {error && <div className="trade-error" role="alert"><AlertTriangle size={17} /><span>{error}</span></div>}
    <section className="logic-library glass-card"><header className="trade-section-head"><div><span className="trade-kicker">YOUR STRATEGIES</span><h2>Saved logic <small>{strategies.length}</small></h2></div><div className="trade-head-actions"><button className="secondary-button" onClick={refresh} disabled={loading}><RefreshCw size={14} className={loading ? 'spin' : ''} /> Refresh</button><button className="primary-button trade-primary" onClick={() => fileRef.current?.click()} disabled={busy}>{busy ? <LoaderCircle size={15} className="spin" /> : <Upload size={15} />} Upload Python</button><input ref={fileRef} type="file" accept=".py,text/x-python" hidden onChange={e => upload(e.target.files?.[0])} /></div></header>
      {loading ? <div className="trade-loading"><LoaderCircle size={18} className="spin" /> Loading your strategies…</div> : strategies.length ? <div className="strategy-grid">{strategies.map(strategy => <article className="strategy-card" key={strategy.id}><div className="strategy-card-top"><span className="strategy-file-icon"><FileCode2 size={17} /></span><button className="icon-button danger-icon" aria-label={`Delete ${strategy.name}`} onClick={() => remove(strategy)}><Trash2 size={15} /></button></div><h3>{strategy.name}</h3><p>{strategy.description || 'No description added.'}</p><div className="strategy-card-meta"><span>{strategy.parameters?.length || 0} inputs</span><span>{strategy.filename}</span></div><details className="strategy-source"><summary><Code2 size={13} /> View strategy file <ChevronDown size={13} /></summary><pre>{strategy.source}</pre></details></article>)}</div> : <div className="trade-empty"><div className="trade-empty-icon"><Code2 size={20} /></div><strong>No strategies yet</strong><button className="primary-button trade-primary" onClick={() => fileRef.current?.click()}><Plus size={15} /> Add your first logic</button></div>}
    </section>
  </div>
}

const initialRisk = { risk_per_trade: 250, max_daily_loss: 750, max_open_positions: 3, max_position_value: 10000 }
const riskFields = [
  { key: 'risk_per_trade', label: 'Risk / trade', prefix: '₹', min: 1, max: 100000, step: 50 },
  { key: 'max_daily_loss', label: 'Daily loss limit', prefix: '₹', min: 1, max: 1000000, step: 50 },
  { key: 'max_open_positions', label: 'Open positions', min: 1, max: 50, step: 1 },
  { key: 'max_position_value', label: 'Position value', prefix: '₹', min: 1, max: 10000000, step: 500 },
]
const policyKeys = { risk_per_trade: 'TRADE_MAX_RISK_PER_TRADE', max_daily_loss: 'TRADE_MAX_DAILY_LOSS', max_position_value: 'TRADE_MAX_POSITION_VALUE', max_open_positions: 'TRADE_MAX_POSITIONS' }

export function AutoTradePanel({ request, notify, active = true, onBusyChange }) {
  const [strategies, setStrategies] = useState([])
  const [strategyId, setStrategyId] = useState('')
  const [parameters, setParameters] = useState({})
  const [risk, setRisk] = useState(initialRisk)
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [preview, setPreview] = useState(null)
  const [selected, setSelected] = useState(new Set())
  const [busy, setBusy] = useState(false)
  const [confirm, setConfirm] = useState(false)
  const [tradeResults, setTradeResults] = useState([])
  const [error, setError] = useState('')
  const scanVersion = useRef(0)
  const tradeInFlight = useRef(false)
  const [engineStatus, setEngineStatus] = useState({ mode: 'unknown', worker_online: false })
  useEffect(() => { onBusyChange?.(busy) }, [busy, onBusyChange])
  const selectedStrategy = strategies.find(item => item.id === strategyId)
  const basicParameters = useMemo(() => selectedStrategy?.parameters?.filter(item => !item.advanced) || [], [selectedStrategy])
  const advancedParameters = useMemo(() => selectedStrategy?.parameters?.filter(item => item.advanced) || [], [selectedStrategy])

  useEffect(() => {
    if (!active) return
    let alive = true
    request('/api/strategies').then(data => {
      if (!alive) return
      const items = data.strategies || []; setStrategies(items)
      setStrategyId(current => items.some(item => item.id === current) ? current : items[0]?.id || '')
    }).catch(e => { if (alive) setError(e.message) })
    return () => { alive = false }
  }, [active])
  useEffect(() => {
    let alive = true
    const refresh = async () => { try { const value = await request('/api/auto-trade/status'); if (alive) setEngineStatus(value) } catch { if (alive) setEngineStatus({ mode: 'unknown', worker_online: false }) } }
    refresh(); const timer = setInterval(refresh, 20000)
    return () => { alive = false; clearInterval(timer) }
  }, [])
  useEffect(() => {
    scanVersion.current += 1
    setConfirm(false); setSelected(new Set())
    if (!selectedStrategy) { setParameters({}); setPreview(null); return }
    setParameters(Object.fromEntries(selectedStrategy.parameters.map(item => [item.key, item.default])))
    setPreview(null); setTradeResults([])
  }, [strategyId])

  const setParameter = (key, value) => {
    scanVersion.current += 1
    setParameters(current => ({ ...current, [key]: value }))
    setPreview(null); setSelected(new Set()); setConfirm(false)
  }
  const runScan = async () => {
    if (!strategyId) { setError('Upload or choose a strategy first.'); return null }
    setBusy(true); setError(''); setConfirm(false); setTradeResults([]); setPreview(null); setSelected(new Set())
    const version = ++scanVersion.current
    try {
      const data = await request(`/api/strategies/${strategyId}/scan`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ parameters }) })
      if (version !== scanVersion.current) return null
      const selectionLimit = Number.isInteger(risk.max_open_positions) ? risk.max_open_positions : initialRisk.max_open_positions
      setPreview(data); setSelected(new Set((data.candidates || []).slice(0, selectionLimit).map(item => item.symbol)))
      notify?.('Scan complete', `${data.matches} matches across ${data.scanned} stocks.`, data.matches ? 'success' : 'info')
      return data
    } catch (e) { setError(e.message); notify?.('Scan failed', e.message, 'error'); return null }
    finally { setBusy(false) }
  }
  const beginTrade = async () => {
    if (!['paper', 'live'].includes(engineStatus.mode)) { setError('Execution status is unavailable. Please retry shortly.'); return }
    if (engineStatus.mode === 'live' && (engineStatus.live_available === false || !engineStatus.worker_online || engineStatus.paused)) { setError(engineStatus.blocking_reasons?.join(' ') || (engineStatus.paused ? 'New entries are paused.' : 'The order service is offline.')); return }
    const data = preview && !preview.consumed ? preview : await runScan()
    if (data?.candidates?.length) setConfirm(true)
    else if (data) notify?.('No trades to place', 'The current strategy scan returned no matching stocks.', 'warning')
  }
  const execute = async () => {
    if (!preview || preview.consumed || !selected.size || tradeInFlight.current) return
    if (Object.values(risk).some(value => !Number.isFinite(value) || value <= 0) || !Number.isInteger(risk.max_open_positions) || risk.risk_per_trade > risk.max_daily_loss) {
      setError('Enter positive risk limits and a whole number of positions. Per-trade risk must fit the daily loss guard.')
      setConfirm(false); return
    }
    if (selected.size > risk.max_open_positions) { setError(`Select at most ${risk.max_open_positions} stocks for the current position limit.`); setConfirm(false); return }
    tradeInFlight.current = true
    setBusy(true); setError('')
    try {
      const result = await request('/api/auto-trade/execute', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ preview_id: preview.preview_id, symbols: [...selected], risk }) })
      setTradeResults(result.results || []); setConfirm(false)
      const needsReview = (result.results || []).some(row => ['checking', 'error'].includes(row.status))
      notify?.(result.mode === 'paper' ? 'Simulation complete' : needsReview ? 'Orders need review' : 'Orders processed', result.mode === 'paper' ? `${(result.results || []).filter(row => row.status === 'paper').length} positions calculated.` : `${(result.results || []).filter(row => row.status === 'submitted').length} entry GTT(s) submitted.`, needsReview ? 'warning' : result.mode === 'paper' ? 'info' : 'success')
    } catch (e) { setConfirm(false); setError(e.message); notify?.('Trade needs review', e.message, 'error') }
    finally { setBusy(false); tradeInFlight.current = false; setPreview(current => current ? { ...current, consumed: true } : null); setSelected(new Set()) }
  }
  const riskChange = key => value => {
    setRisk(current => ({ ...current, [key]: value }))
    if (key === 'max_open_positions' && Number.isInteger(value) && value > 0) setSelected(current => new Set([...current].slice(0, value)))
  }
  const paramField = item => <div className="trade-field" key={item.key}><span>{item.label}</span>{item.type === 'boolean' ? <input aria-label={item.label} type="checkbox" disabled={busy} checked={Boolean(parameters[item.key])} onChange={e => setParameter(item.key, e.target.checked)} /> : <NumberField label={item.label} disabled={busy} min={item.min} max={item.max} step={item.type === 'integer' ? 1 : 0.1} value={parameters[item.key] ?? item.default} onChange={value => setParameter(item.key, value)} />}</div>

  return <div className="trading-page auto-trade-page">
    <div className="execution-toolbar"><span className="execution-source"><Activity size={14} /> Kite Connect</span><span title={engineStatus.blocking_reasons?.join(' ')} className={`execution-mode ${engineStatus.mode === 'live' && engineStatus.live_available !== false ? 'live' : ''}`}><ShieldCheck size={12} />{engineStatus.mode === 'paper' ? 'Simulation' : engineStatus.mode === 'live' ? engineStatus.live_available !== false && engineStatus.worker_online ? 'Live orders' : 'Live orders blocked' : 'Execution unavailable'}</span></div>
    {engineStatus.paused && <div className="workspace-warning"><AlertTriangle size={15} /> New live entries are paused. Resume them from Overview when ready.</div>}
    {error && <div className="trade-error" role="alert"><AlertTriangle size={17} /><span>{error}</span><button aria-label="Dismiss" onClick={() => setError('')}><X size={14} /></button></div>}
    <div className="auto-trade-layout">
      <section className="trade-config glass-card"><header className="trade-section-head"><div><span className="trade-kicker">SETUP</span><h2>Trade configuration</h2></div><span className="field-counter">{(selectedStrategy?.parameters?.length || 0) + 4} fields</span></header>
        <div className="trade-field strategy-select"><span>Strategy</span><SelectField label="Strategy" disabled={busy} value={strategyId} onChange={setStrategyId} placeholder="Select strategy" options={strategies.map(item => ({ value: item.id, label: item.name }))} /></div>
        {selectedStrategy && <><div className="trade-field-grid">{basicParameters.map(paramField)}</div>{advancedParameters.length > 0 && <><button className="advanced-toggle" onClick={() => setShowAdvanced(value => !value)}><ChevronDown size={14} className={showAdvanced ? 'rotate' : ''} /> {showAdvanced ? 'Less' : 'More'} options</button>{showAdvanced && <div className="trade-field-grid advanced-fields">{advancedParameters.map(paramField)}</div>}</>}</>}
        <div className="trade-subhead"><div><span className="trade-kicker">RISK MANAGEMENT</span><div className="heading-with-tip"><h3>Position limits</h3><InfoTip text="These limits cap planned trade size and risk. The backend also checks available funds and account limits before submitting live entries." /></div></div><ShieldCheck size={17} /></div>
        <div className="trade-field-grid risk-fields">{riskFields.map(({ key, ...field }) => <div className="trade-field" key={key}><span>{field.label}</span><NumberField {...field} max={engineStatus.mode === 'live' ? Math.min(field.max, engineStatus.policy?.[policyKeys[key]] ?? field.max) : field.max} value={risk[key]} onChange={riskChange(key)} disabled={busy} /></div>)}</div>
        <div className="trade-action-row"><button className="secondary-button" onClick={runScan} disabled={busy || !strategyId}>{busy ? <LoaderCircle size={15} className="spin" /> : <Play size={14} />} Scan</button><button className="primary-button trade-primary" onClick={beginTrade} disabled={busy || !strategyId || !['paper', 'live'].includes(engineStatus.mode)}>{busy ? <LoaderCircle size={15} className="spin" /> : <ArrowRight size={15} />}{engineStatus.mode === 'paper' ? 'Simulate' : 'Trade'}</button></div>
      </section>
      <section className="trade-preview glass-card"><header className="trade-section-head"><div><span className="trade-kicker">SCAN RESULTS</span><h2>Trade preview</h2></div>{preview && <span className="preview-age">Valid for 10 min</span>}</header>
        {preview ? <><div className="preview-summary"><div><strong>{preview.matches}</strong><small>matches</small></div><div><strong>{selected.size}</strong><small>selected</small></div><div><strong>{preview.scanned}</strong><small>stocks scanned</small></div><div><strong>{preview.evaluationErrors}</strong><small>logic errors</small></div><span>as of {preview.asOf}</span></div>
          {preview.candidates?.length ? <div className="preview-table-wrap"><table className="preview-table"><thead><tr><th><input type="checkbox" checked={selected.size === Math.min(preview.candidates.length, risk.max_open_positions)} onChange={e => setSelected(e.target.checked ? new Set(preview.candidates.slice(0, risk.max_open_positions).map(row => row.symbol)) : new Set())} aria-label="Select candidates up to the position limit" /></th><th>Symbol</th><th>Entry trigger</th><th>Stop</th><th>Target</th><th title="Estimated from entry and exit limits; excludes fees and gap risk">Risk/share</th></tr></thead><tbody>{preview.candidates.map(row => <tr key={row.symbol}><td><input type="checkbox" checked={selected.has(row.symbol)} disabled={!selected.has(row.symbol) && selected.size >= risk.max_open_positions} onChange={e => setSelected(current => { const next = new Set(current); e.target.checked ? next.add(row.symbol) : next.delete(row.symbol); return next })} aria-label={`Select ${row.symbol}`} /></td><td><strong>{row.symbol}</strong><small>{row.companyName}</small></td><td>₹{row.entry_price.toFixed(2)}<small>limit ₹{row.entry_limit_price.toFixed(2)}</small></td><td className="risk-color">₹{row.stop_loss.toFixed(2)}</td><td className="gain-color">₹{row.target_price.toFixed(2)}</td><td>₹{(row.entry_limit_price - row.stop_limit_price).toFixed(2)}</td></tr>)}</tbody></table></div> : <div className="trade-empty compact"><strong>No matching stocks</strong><p>Change the strategy inputs and scan again.</p></div>}
          {tradeResults.length > 0 && <div className="trade-results">{tradeResults.map(row => <div key={row.symbol} className={`trade-result ${row.status}`}><strong>{row.symbol}</strong><span>{row.status === 'submitted' ? `Entry GTT ${row.entry_gtt_id} · ${row.quantity} shares` : row.message}</span></div>)}</div>}
        </> : <div className="trade-empty"><div className="trade-empty-icon">{busy ? <LoaderCircle size={19} className="spin" /> : <Play size={19} />}</div><strong>{busy ? 'Scanning Kite data…' : 'No scan yet'}</strong></div>}
      </section>
    </div>
    {confirm && <div className="trade-modal-backdrop" role="presentation"><section className="trade-confirm-modal" role="dialog" aria-modal="true" aria-labelledby="trade-confirm-title"><button className="icon-button modal-close" onClick={() => setConfirm(false)} aria-label="Close" disabled={busy}><X size={17} /></button><span className="trade-kicker">REVIEW</span><h2 id="trade-confirm-title">{engineStatus.mode === 'paper' ? 'Confirm simulation' : 'Confirm live orders'}</h2><p>{selected.size} selected stocks · ₹{risk.risk_per_trade.toLocaleString('en-IN')} estimated risk per trade · ₹{risk.max_daily_loss.toLocaleString('en-IN')} daily limit.</p><div className="confirm-symbols">{[...selected].map(symbol => <span key={symbol}>{symbol}</span>)}</div><div className="confirm-warning"><ShieldCheck size={15} />{engineStatus.mode === 'paper' ? 'Simulation only. No broker orders.' : 'Real Kite GTT orders. Gaps and unfilled exits can exceed estimated risk.'}</div><div className="modal-actions"><button className="secondary-button" onClick={() => setConfirm(false)} disabled={busy}>Back</button><button className="primary-button trade-primary" onClick={execute} disabled={busy || !selected.size}>{busy ? <LoaderCircle size={15} className="spin" /> : <Check size={15} />}{engineStatus.mode === 'paper' ? 'Simulate' : 'Place orders'}</button></div></section></div>}
  </div>
}
