// Isolated headless browser with every /api request mocked. No broker calls.
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { createServer } from 'node:net'
import path from 'node:path'

const base = 'http://127.0.0.1:5175'
const artifacts = path.resolve('.test-artifacts')
mkdirSync(artifacts, { recursive: true })
const executable = ['C:/Program Files/Google/Chrome/Application/chrome.exe', 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'].find(existsSync)
assert.ok(executable, 'A Chromium browser is required for this smoke test.')
const portServer = createServer()
await new Promise(resolve => portServer.listen(0, '127.0.0.1', resolve))
const port = portServer.address().port
await new Promise(resolve => portServer.close(resolve))
const browser = spawn(executable, ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check', '--disable-background-networking', '--window-size=1440,1100', `--remote-debugging-port=${port}`, '--remote-debugging-address=127.0.0.1', `--user-data-dir=${path.join(artifacts, `profile-${Date.now()}`)}`, 'about:blank'], { windowsHide: true, stdio: 'ignore' })
const pause = ms => new Promise(resolve => setTimeout(resolve, ms))
let socket, sequence = 0, pendingSignal, pendingPreview, pendingAlgo, scannerRequests = 0
const calls = new Map()
const errors = []
const strategySource = readFileSync('backend/strategy_templates/connection_check.py', 'utf8')
const parameters = [
  { key: 'min_price', label: 'Minimum price', type: 'number', default: 1, min: 1, max: 100000 },
  { key: 'entry_buffer', label: 'Entry buffer %', type: 'number', default: 1, min: .6, max: 5 },
  { key: 'stop_percent', label: 'Stop loss %', type: 'number', default: 2, min: 1, max: 10 },
  { key: 'target_percent', label: 'Target %', type: 'number', default: 4, min: 1, max: 20 },
  { key: 'min_volume', label: 'Minimum daily volume', type: 'integer', default: 0, min: 0, max: 1000000000, advanced: true },
]
const strategies = []
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const id = ++sequence
  calls.set(id, { resolve, reject })
  socket.send(JSON.stringify({ id, method, params }))
})
const evaluate = async expression => {
  const result = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true })
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text)
  return result.result.value
}
const waitFor = async (check, label) => {
  for (let i = 0; i < 100; i++) { if (await check()) return; await pause(75) }
  throw new Error(`Timed out: ${label}`)
}
const visibleText = () => evaluate("document.querySelector('.dashboard-panel:not([hidden])')?.innerText || ''")
const navigate = name => evaluate(`Array.from(document.querySelectorAll('.nav-item')).find(button => button.textContent.trim() === ${JSON.stringify(name)}).click()`)
const fulfill = (requestId, value) => send('Fetch.fulfillRequest', { requestId, responseCode: 200, responseHeaders: [{ name: 'Content-Type', value: 'application/json' }], body: Buffer.from(JSON.stringify(value)).toString('base64') })

try {
  let targets
  await waitFor(async () => {
    try { targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json(); return targets.some(target => target.type === 'page') } catch { return false }
  }, 'browser startup')
  socket = new WebSocket(targets.find(target => target.type === 'page').webSocketDebuggerUrl)
  await new Promise((resolve, reject) => { socket.addEventListener('open', resolve, { once: true }); socket.addEventListener('error', reject, { once: true }) })
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data)
    if (message.id) {
      const call = calls.get(message.id)
      calls.delete(message.id)
      if (message.error) call?.reject(new Error(message.error.message)); else call?.resolve(message.result)
    } else if (message.method === 'Runtime.exceptionThrown') {
      errors.push(message.params.exceptionDetails.exception?.description || message.params.exceptionDetails.text)
    } else if (message.method === 'Fetch.requestPaused') {
      const { requestId, request } = message.params
      const pathname = new URL(request.url).pathname
      let response = {}
      if (pathname === '/api/auth/status') response = { authenticated: true }
      else if (pathname === '/api/user/profile') response = { user_id: 'DEMO', user_name: 'Preview Account', products: ['CNC'], exchanges: ['NSE'] }
      else if (pathname === '/api/account/summary') response = { available_cash: 25000, day_pnl: 0, open_positions: 0, holding_count: 0, warnings: [], monitors: [], as_of: new Date().toISOString(), status: { mode: 'paper', worker_online: false, persistent: false } }
      else if (pathname === '/api/auto-trade/status') response = { mode: 'paper', worker_online: false, persistent: false }
      else if (pathname === '/api/auto-trade/events' || pathname === '/api/auto-trade/activity') response = { events: [], monitors: [] }
      else if (pathname === '/api/scanner-data') {
        scannerRequests += 1
        if (scannerRequests > 1) { pendingAlgo = requestId; return }
        response = { stocks: [], metadata: {} }
      }
      else if (pathname === '/api/strategies' && request.method === 'POST') {
        const upload = JSON.parse(request.postData)
        assert.equal(upload.filename, 'connection_check.py')
        assert.ok(upload.source.includes('def evaluate(candles, params):'))
        const strategy = { id: 'a'.repeat(32), name: 'Kite connection check', filename: upload.filename, source: upload.source, parameters }
        strategies.push(strategy)
        response = { strategy }
      } else if (pathname === '/api/strategies') response = { strategies }
      else if (pathname === '/api/signals') { pendingSignal = { requestId, payload: JSON.parse(request.postData) }; return }
      else if (pathname.endsWith('/scan')) { pendingPreview = requestId; return }
      fulfill(requestId, response).catch(error => errors.push(error.message))
    }
  })
  await send('Page.enable')
  await send('Runtime.enable')
  await send('Fetch.enable', { patterns: [{ urlPattern: '*/api/*', requestStage: 'Request' }] })
  await send('Page.navigate', { url: base })
  await waitFor(async () => (await visibleText()).includes('Available cash'), 'dashboard load')

  await navigate('Auto Trade')
  await waitFor(async () => await evaluate("document.querySelector('[role=combobox]')?.textContent.includes('Select strategy')"), 'empty strategy selector')

  await navigate('Strategies')
  await waitFor(async () => (await visibleText()).includes('No strategies yet'), 'empty library')
  await evaluate(`(() => { const transfer = new DataTransfer(); transfer.items.add(new File([${JSON.stringify(strategySource)}], 'connection_check.py', {type:'text/x-python'})); const input=document.querySelector('input[type=file]'); input.files=transfer.files; input.dispatchEvent(new Event('change',{bubbles:true})); })()`)
  await waitFor(async () => (await visibleText()).includes('Kite connection check'), 'strategy upload')

  await navigate('Signals')
  await evaluate("document.querySelector('button[aria-label=\"Increase Short SMA\"]').click()")
  assert.equal(await evaluate("document.querySelector('input[aria-label=\"Short SMA\"]').value"), '7')
  await evaluate("document.querySelector('.generate-button').click()")
  await waitFor(() => pendingSignal, 'signal request')
  assert.equal(pendingSignal.payload.short_sma, 7)
  await navigate('User')
  await waitFor(async () => await evaluate("document.querySelector('.background-jobs')?.textContent.includes('Signals running')"), 'background progress')
  await fulfill(pendingSignal.requestId, { results: [], total_signals: 0, scanned: 100, skipped: 0, short_sma: 7, long_sma: 30, lookback_days: 30, as_of: '2026-11-18' })
  await waitFor(async () => await evaluate("document.querySelector('.toast-stack')?.textContent.includes('Signals ready')"), 'background completion')
  await navigate('Signals')
  assert.equal(await evaluate("document.querySelector('input[aria-label=\"Short SMA\"]').value"), '7')
  assert.ok((await visibleText()).includes('No crossovers found'))
  console.log('PASS: signal request finishes across tabs; input and results are retained')

  await navigate('Auto Trade')
  await waitFor(async () => await evaluate("document.querySelector('[role=combobox]')?.textContent.includes('Kite connection check')"), 'strategy fetched into Auto Trade')
  await evaluate("document.querySelector('button[aria-label=\"Increase Risk / trade\"]').click()")
  await evaluate("document.querySelector('.trade-action-row .secondary-button').click()")
  await waitFor(() => pendingPreview, 'strategy preview request')
  await navigate('Algo Signals')
  await fulfill(pendingPreview, { preview_id: 'b'.repeat(32), matches: 1, scanned: 100, evaluationErrors: 0, asOf: '2026-11-18', candidates: [{ symbol: 'TEST', companyName: 'Preview company', entry_price: 100, entry_limit_price: 100.5, stop_loss: 98, stop_limit_price: 97.5, target_price: 104 }] })
  await waitFor(async () => await evaluate("document.querySelector('.toast-stack')?.textContent.includes('Scan complete')"), 'preview completion')
  await navigate('Auto Trade')
  assert.ok((await visibleText()).includes('TEST'))
  assert.equal(await evaluate("document.querySelector('input[aria-label=\"Risk / trade\"]').value"), '300')
  await evaluate("document.querySelector('[role=combobox]').click()")
  assert.equal(await evaluate("document.querySelector('[role=combobox]').getAttribute('aria-expanded')"), 'true')
  assert.equal(await evaluate("getComputedStyle(document.querySelector('.modern-select-menu')).position"), 'absolute')
  const dark = await send('Page.captureScreenshot', { format: 'png' })
  writeFileSync(path.join(artifacts, 'controls-dark.png'), Buffer.from(dark.data, 'base64'))
  await evaluate("document.querySelector('.topbar .theme-toggle').click()")
  await pause(150)
  assert.equal(await evaluate('document.documentElement.dataset.theme'), 'light')
  const light = await send('Page.captureScreenshot', { format: 'png' })
  writeFileSync(path.join(artifacts, 'controls-light.png'), Buffer.from(light.data, 'base64'))
  await navigate('Algo Signals')
  await waitFor(async () => (await visibleText()).includes('No stocks are available'), 'initial algo scan')
  await evaluate("document.querySelector('.algo-run-button').click()")
  await waitFor(() => pendingAlgo, 'manual algo request')
  await navigate('Strategies')
  await fulfill(pendingAlgo, { stocks: [], metadata: {} })
  await waitFor(async () => await evaluate("document.querySelector('.toast-stack')?.textContent.includes('Algo scan complete')"), 'background algo completion')
  await navigate('Algo Signals')
  assert.ok((await visibleText()).includes('No stocks are available'))
  assert.deepEqual(errors, [])
  console.log('PASS: strategy upload, refreshed selection, background strategy/Algo scans, risk controls, and both themes')
  console.log('Screenshots: .test-artifacts/controls-dark.png and controls-light.png')
} finally {
  if (socket?.readyState === WebSocket.OPEN) { await send('Browser.close').catch(() => {}); socket.close() }
  browser.kill()
}
