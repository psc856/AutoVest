import assert from 'node:assert/strict'
import { after, before, test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

let server, app, trading, controls
const originalFetch = globalThis.fetch
const originalWindow = globalThis.window

before(async () => {
  globalThis.window = { location: { origin: 'http://127.0.0.1:5175' }, setTimeout, clearTimeout, setInterval, clearInterval }
  server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
  app = await server.ssrLoadModule('/src/App.jsx')
  trading = await server.ssrLoadModule('/src/TradingPanels.jsx')
  controls = await server.ssrLoadModule('/src/FormControls.jsx')
})

after(async () => {
  globalThis.fetch = originalFetch
  globalThis.window = originalWindow
  await server?.close()
})

test('Strategies renders without referencing Auto Trade state', () => {
  const html = renderToStaticMarkup(createElement(trading.StrategyLibraryPanel, { request: async () => ({ strategies: [] }) }))
  assert.match(html, /Saved logic/)
  assert.match(html, /Loading your strategies/)
})

test('Auto Trade renders concise status and custom controls without the guide banner', () => {
  const html = renderToStaticMarkup(createElement(trading.AutoTradePanel, { request: async () => ({ strategies: [] }) }))
  assert.match(html, /role="combobox"/)
  assert.match(html, /Increase Risk \/ trade/)
  assert.doesNotMatch(html, /Paper mode|Test strategy signals and sizing safely|Editable defaults/)
})

test('number steppers clamp boundaries and preserve decimal precision', () => {
  assert.equal(controls.stepNumber(6, 1, 2, 200, 1), 7)
  assert.equal(controls.stepNumber(2, 1, 2, 200, -1), 2)
  assert.equal(controls.stepNumber(200, 1, 2, 200, 1), 200)
  assert.equal(controls.stepNumber(1.2, .1, 0, 10, 1), 1.3)
})

test('unvisited tabs are lazy and active tabs have a stable retained wrapper', () => {
  const hidden = renderToStaticMarkup(createElement(controls.RetainedPanel, { active: false }, createElement('span', null, 'Lazy child')))
  assert.equal(hidden, '')
  const active = renderToStaticMarkup(createElement(controls.RetainedPanel, { active: true, name: 'Signals' }, createElement('span', null, 'Live child')))
  assert.match(active, /class="dashboard-panel"/)
  assert.match(active, /Live child/)
})

test('Algo Signals initial render displays its loading state and scan control', () => {
  const html = renderToStaticMarkup(createElement(app.AlgoSignalsPanel))
  assert.match(html, /Loading scanner cache/)
  assert.match(html, /Run scanner/)
  assert.match(html, /aria-busy="true"/)
})

test('zero matches displays a completed scan explanation and count', () => {
  const html = renderToStaticMarkup(createElement(app.AlgoScanFeedback, { scanned: 100 }))
  assert.match(html, /No breakout retest setups match/)
  assert.match(html, /100 stocks scanned/)
  assert.match(html, /role="status"/)
})

test('an empty cache is distinguished from stocks that do not match', () => {
  const html = renderToStaticMarkup(createElement(app.AlgoScanFeedback, { scanned: 0 }))
  assert.match(html, /No stocks are available in the scanner cache/)
})

test('slow requests show continued progress and failed scans offer retry', () => {
  const pending = renderToStaticMarkup(createElement(app.AlgoScanFeedback, { busy: true, elapsed: 20 }))
  assert.match(pending, /Loading scanner cache/)
  assert.match(pending, /Still loading market data \(20s\)/)
  const failed = renderToStaticMarkup(createElement(app.AlgoScanFeedback, { error: 'Kite unavailable' }))
  assert.match(failed, /Scan could not finish/)
  assert.match(failed, /Retry scanner/)
})

const stock = (symbol = 'TEST', latestClose = 100.5) => ({
  symbol, companyName: 'Test company', instrument_token: '12345',
  candles: Array.from({ length: 28 }, (_, index) => ({
    date: new Date(Date.UTC(2026, 0, index + 1)).toISOString(),
    open: '95', high: '100', low: '90', close: '98', volume: '100',
    ...(index === 25 ? { open: '100', close: '106', high: '110', low: '90', volume: '350' } : {}),
    ...(index === 26 ? { open: '108', close: '110', high: '120', low: '102' } : {}),
    ...(index === 27 ? { open: '104', close: String(latestClose), high: '108', low: '99' } : {}),
  })).reverse(),
})

test('scanner normalizes data, detects a valid setup, and ranks retest proximity', () => {
  const scan = app.scanBreakoutRetests([stock('FAR', 100.8), stock('NEAR', 100.1)])
  assert.equal(scan.stocks.length, 2)
  assert.equal(scan.eligibleCount, 2)
  assert.deepEqual(scan.results.map(row => row.symbol), ['NEAR', 'FAR'])
  assert.equal(scan.results[0].bIndex, 25)
  assert.equal(scan.results[0].daysSince, 2)
  assert.equal(scan.results[0].volMultiple, 3.5)
  assert.equal(scan.results[0].breakoutDate, '2026-01-26')
  assert.equal(scan.results[0].candles[0].date, '2026-01-01')
})

test('nonmatching rules, missing history, and invalid dates produce safe outcomes', () => {
  const noMatch = stock('NONE', 110)
  noMatch.candles.push({ date: 'bad date' }, null)
  assert.equal(app.scanBreakoutRetests([noMatch]).results.length, 0)
  const insufficient = app.scanBreakoutRetests([{ symbol: 'SHORT', candles: [] }])
  assert.equal(insufficient.stocks.length, 1)
  assert.equal(insufficient.eligibleCount, 0)
  assert.equal(insufficient.results.length, 0)
  assert.throws(() => app.scanBreakoutRetests(undefined), /stocks list/)
})

test('overlapping initial loads share one request and manual reruns fetch again', async () => {
  let calls = 0, resolveFetch
  globalThis.fetch = () => { calls += 1; return new Promise(resolve => { resolveFetch = resolve }) }
  const first = app.loadScannerSnapshot()
  const second = app.loadScannerSnapshot()
  assert.equal(first, second)
  assert.equal(calls, 1)
  resolveFetch({ ok: true, json: async () => ({ stocks: [] }) })
  await first
  globalThis.fetch = async () => { calls += 1; return { ok: true, json: async () => ({ stocks: [] }) } }
  await app.loadScannerSnapshot()
  assert.equal(calls, 2)
})

test('API errors remain visible and a failed request can be retried', async () => {
  globalThis.fetch = async () => ({ ok: false, json: async () => ({ detail: 'Kite historical data unavailable' }) })
  await assert.rejects(app.loadScannerSnapshot(), /Kite historical data unavailable/)
  globalThis.fetch = async () => ({ ok: true, json: async () => ({ stocks: [] }) })
  assert.deepEqual((await app.loadScannerSnapshot()).stocks, [])
})
