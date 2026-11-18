# Kite Console

A glass-style React dashboard with switchable dark and light themes and a FastAPI bridge to Zerodha Kite Connect. Enter your API key, API secret, and request token. The backend exchanges the request token for an access token and never returns it to the frontend. Local development saves the session in an ignored file; the hosted application uses Upstash Redis with the access token encrypted at rest and a signed, browser-specific HttpOnly cookie.

The Signals tab loads the current Nifty 100 members from the [official NSE Indices constituent CSV](https://www.niftyindices.com/IndexConstituent/ind_nifty100list.csv), maps each symbol to Kite's NSE instrument list, and scans daily candles for bullish and bearish crossovers. It uses closed daily candles and ranks the newest crossovers first. A full scan checks the whole current index and can take around 35 seconds due to Kite historical-data request pacing; **Max stocks** controls how many matching signals are shown.

The Strategies tab accepts account-scoped `.py` strategy files in a restricted format. Auto Trade starts in **paper mode**, previews signal matches, estimates position sizes from editable risk caps, and requires a second explicit confirmation. Live GTT entry orders are available only when the persistent trade worker is online and the backend is explicitly configured for live mode. See [AUTO_TRADING_DEPLOYMENT.md](AUTO_TRADING_DEPLOYMENT.md) before using it.

## Requirements

- Node.js 22.12 or newer and npm
- Python 3.10 or newer
- A Zerodha account and Kite Connect app (API key and API secret)

## Run locally

Open two terminals in the project folder.

### 1. Start FastAPI

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
py -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

If PowerShell blocks virtual environment activation, run `.venv\Scripts\python.exe -m pip install -r requirements.txt`, then start the API with `.venv\Scripts\python.exe -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000`.

### 2. Start the frontend

```powershell
npm install
npm run dev
```

Open [http://localhost:5173](http://localhost:5173). The Vite server connects to FastAPI at `http://localhost:8000`. To use another backend URL, set `VITE_API_URL` in a local `.env` file before starting Vite.

## Connect your Kite account

1. Sign in to Kite Connect on the device where your Kite app is configured and copy the `request_token` value from the redirect URL.
2. Paste the short-lived request token into Kite Console on this device with your API key and API secret, then select **Connect to Kite**.
3. The backend exchanges the token and the dashboard opens to your User profile. It fetches your name, user ID, products, and exchanges through the backend profile endpoint.

Kite request tokens expire within minutes, so paste and submit one soon after generating it on the other device. The generated access token is valid for the trading day; after it expires, generate a fresh request token and repeat the manual paste. See the [Kite Connect login flow](https://kite.trade/docs/connect/v3/user/) for the official steps.

## Local session and customization

- Locally, FastAPI encrypts the API key and access token in `.token_store.json`, with a generated `.token_store.key` stored separately in the project root. Both files are excluded from Git and Docker. Existing plaintext session files are upgraded when read. Keep the key private; deleting it prevents old sessions from being decrypted. The API secret and request token are not saved. Hosted deployments use Upstash Redis and `KITE_TOKEN_ENCRYPTION_KEY`; each browser gets a separate signed HttpOnly session cookie. This isolates sessions by browser but does not create verified user accounts; users must connect their own Kite credentials.
- On backend startup, the saved token is restored so restarting Vite, refreshing the page, or changing frontend code does not require another Kite login. Use **Sign out** in the sidebar to clear the browser session. Pending live entry GTTs block logout, and outstanding position monitors retain their encrypted worker credentials until they expire. A session may still expire according to Kite's token policy.
- The browser sends the API key, API secret, and pasted request token to the local backend for the exchange. The access token is never returned to the browser.
- The API binds to `127.0.0.1` in the commands above and accepts browser requests from the local Vite development URL. Without Redis, server requests are restricted to loopback clients. Hosted deployments require Redis and account-scoped signed browser sessions; see the production deployment guide.
- Main design tokens and responsive styles live in `src/styles.css`; dashboard structure and tabs live in `src/App.jsx`.

## API routes

- `GET /api/health` — local API health
- `GET /api/auth/status` — reports whether a saved session is present
- `POST /api/auth/login` — exchanges the request token and persists the access token; returns only an authenticated flag
- `POST /api/auth/logout` — clears the local session
- `GET /api/user/profile` — fetches the current Kite profile
- `POST /api/signals` — reads current Nifty 100 constituents and returns SMA crossovers. JSON fields: `short_sma` (default 6), `long_sma` (default 30), `lookback_days` (default 30), and `max_stocks` (default 25).
- `GET /api/scanner-data` — caches Nifty 100 metadata and 150 calendar days of daily candles for the Algo Signals scanner (15-minute cache).
- `GET /api/scanner-ohlc?tokens=...` — returns an OHLC last-price snapshot for currently triggered stocks.
- `WS /ws/ticks?tokens=...&mode=full` — streams live Kite ticks for the currently triggered stocks only.
- `GET/POST/DELETE /api/strategies` — list, upload, and delete the current Kite account's restricted Python strategies.
- `POST /api/strategies/{id}/scan` — evaluate a strategy against cached Nifty 100 daily candles and create a short-lived trade preview.
- `GET /api/auto-trade/status` and `/api/auto-trade/events` — worker status and account-scoped order notifications.
- `POST /api/auto-trade/execute` — simulate in paper mode or submit explicitly confirmed entry GTTs in live mode.

The production Docker deployment compiles and serves React, runs FastAPI and the persistent trade monitor, and exposes them through one Caddy-managed HTTPS origin on AWS Lightsail.


## Security and account operations

Overview displays broker funds, daily position P&L, holdings, managed setup counts, and entry-pause controls. Activity lists managed order states and the latest 100 events. Use **Recheck with Kite** after resolving a flagged setup in Kite; this reads broker state and never places or cancels an order.

See [SECURITY_REVIEW.md](SECURITY_REVIEW.md) for the concrete fixes, remaining limitations, and offline regression tests. Frontend dependency versions are pinned; deploy with `npm ci` and `npm run build`.

## Sample strategy and workspace state

Use **Strategies → Download sample** to download `connection_check.py`, or use `backend/strategy_templates/connection_check.py` directly. Upload it in Strategies, choose it in Auto Trade, and select Scan. It returns broad matches from real Kite daily candles to verify the upload and evaluation pipeline. It is a connection check, not a validated trading strategy. Raising Minimum price or Minimum daily volume can demonstrate the no-match state. Initial selections respect the configured open-position limit.

Visited tabs stay mounted during sidebar navigation. Running requests continue, results and edits remain in their panels, and a completion notification appears even when another tab is open. A full page reload or sign-out still resets frontend workspace state.

Market data, profiles, funds, and scans use Kite. The compact execution badge identifies Simulation or Live orders. Real order execution uses encrypted persistent Redis storage, a separate healthy worker, and `AUTO_TRADE_MODE=live`. The [production deployment guide](PRODUCTION_DEPLOYMENT.md) provides the live Compose profile, configuration, and storage/worker preflight commands. Missing live prerequisites block execution; they never silently switch to simulation. The connection-check sample is for scan plumbing and is not a validated trading strategy.

Frontend regression tests: `npm run test:frontend`. The optional `node tests/browser-smoke.mjs` test requires Chrome or Edge and the local frontend at `http://127.0.0.1:5175`; it uses an isolated profile and mocks all broker API calls.
