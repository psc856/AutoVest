# Auto Trade test deployment on Oracle Cloud

For real orders, use [Deploy real Kite order execution](PRODUCTION_DEPLOYMENT.md) and `compose.production.yaml`. This older guide describes the simulation profile; it does not enable the real-order worker.

This repository now has a Docker API and persistent order-monitor worker. The worker checks Kite entry GTTs, waits for entry fills, and then creates an OCO GTT for the stop-loss and target. The default mode is **paper**; it sends no orders to Kite. Keep it in paper mode while checking scans, account isolation, sizing, worker recovery, and notifications.

## Safety and strategy format

- Strategy files are account-specific and encrypted in Upstash Redis. Account identity comes from Kite's authenticated profile, not a browser-supplied user ID.
- The `.py` file format is intentionally restricted. It allows a literal `STRATEGY` dictionary and an `evaluate(candles, params)` function with assignments, `if`, and `return`. It does not allow imports, loops, attributes, file access, network access, or arbitrary Python execution. This keeps one user's uploaded logic away from other users' Kite sessions.
- The strategy may use `sma`, `highest`, `lowest`, `crossed_above`, `crossed_below`, `volume_multiple`, `abs`, `len`, `sum`, `min`, and `max`. Use the downloadable example or `backend/strategy_templates/daily_volume_breakout.py` as a starting point.
- Starting risk controls are editable: ₹250 estimated risk per trade, ₹750 daily loss guard, 3 open positions, and ₹10,000 maximum value per position. Quantity uses the entry limit and stop limit (including a 0.5% allowance); fees and gap risk are not bounded. Manual positions count toward the position cap, but their open risk cannot be inferred reliably from Kite's positions response.
- Live mode is rejected unless the API can see the worker heartbeat. After a buy GTT triggers, the worker checks its fill and creates the OCO exit GTT. Notifications flag order rejections and unprotected positions. This is not a guarantee against losses or broker/network failures; monitor Kite's order book and holdings.
- Run one API instance and one trade-worker instance for the initial deployment. API execution and worker processing share an account lease in Redis, renewed before broker writes. Websocket connection counts are local to an API process; additional API replicas require a shared subscription service.

## Deploy the API and worker on an OCI VM

Oracle currently lists an Always Free Ampere A1 allowance equivalent to 2 OCPUs and 12 GB RAM, subject to account capacity and usage rules. Oracle can reclaim instances it considers idle, so use this only for development and paper trading, not as the sole host for unattended live trading. See [OCI Always Free resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm) and [idle instance rules](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm).

1. Create an Ubuntu or Oracle Linux Ampere A1 VM in your OCI home region. Install Docker Engine and the Docker Compose plugin.
2. Configure a DNS name such as `api.your-domain.com` to point to the VM's public IP. In the OCI network security list/NSG and VM firewall, allow inbound TCP 80 and 443; restrict SSH (22) to your IP. Caddy obtains and renews HTTPS certificates for the DNS name.
3. Clone this repository on the VM. Copy `.env.oracle.example` to `.env.oracle` and set the real Upstash REST URL, **write** token, existing stable `KITE_TOKEN_ENCRYPTION_KEY`, `API_DOMAIN`, and exact Vercel frontend origin. Do not put these values in GitHub.
4. Start the services from the repository root:

   ```bash
   docker compose --env-file .env.oracle up -d --build
   docker compose --env-file .env.oracle logs -f api caddy
   ```

5. Confirm `https://api.your-domain.com/api/health` and `/documentation` work. Auto Trade should show simulation. The real-order worker remains stopped in this profile; it refuses to run in paper mode.
6. In the Vercel **frontend** project, change `VITE_API_URL` to `https://api.your-domain.com` and redeploy the frontend. Keep `FRONTEND_ORIGIN` in `.env.oracle` equal to the production frontend origin. Sign in again because the API host changed.
7. Leave `AUTO_TRADE_MODE=paper` while validating strategy uploads, scan previews, risk calculations, notifications, restarts, and Redis persistence. The code only accepts `paper`, `live`, or disabled modes; setting `live` is an explicit server-side switch. Do not enable it until you have personally checked the behavior and Kite order/GTT settings.

## Move to AWS later

The Docker image and Compose services are provider-neutral. On AWS, use an always-on VM/container for the API and worker, attach a stable HTTPS domain, and copy the existing Redis and encryption environment variables into the secret manager. Update the frontend `VITE_API_URL` and backend `FRONTEND_ORIGIN` only when the hostnames change. Keep the same Fernet key to read the encrypted sessions and strategies already stored in Redis.


## Guardrails and recovery

- `paper` mode never places broker orders. Live entry requires an online worker, a fresh preview, a recent traded price, and explicit confirmation. Strategies evaluate completed daily candles.
- New live entries check holdings, open orders, existing managed setups, pending cash commitments, available cash, daily position P&L, position caps, and configured risk. Active external buy GTTs block Auto Trade because their risk is not managed by this app.
- Duplicate entry reservations apply per account, symbol, and submission day, across strategies. Reservations remain in place if a broker response is uncertain. An entry GTT may trigger on a later trading day; this is not a fill-day deduplication guarantee.
- Use **Overview ? Pause new entries** to block new submissions. This does not cancel any existing GTT or market order.
- Missing records, unknown submissions, failed protection, and broker errors block new entries. The worker reconciles uncertain submissions without blindly resubmitting them. Automatic protective re-arming is suspended after a failed or cancelled exit so it cannot accidentally sell unrelated holdings.
- Resolve affected orders and holdings directly in Kite, then select **Activity ? Recheck with Kite**. Monitoring only resumes after a matching protective GTT and quantity are verified, or closes after no exposure, active GTT, or pending order remains. There is a five-minute minimum observation period before closing an uncertain setup.
- Keep Redis credentials and the stable encryption key backed up securely. Changing the key without migrating existing ciphertext makes saved sessions and strategies unreadable. Broker access tokens still expire; sign in again manually to refresh worker credentials.
- API and worker containers run without added capabilities, with read-only filesystems, resource limits, bounded temporary storage, and rotating logs. Caddy receives the API domain only, not broker-storage secrets.
