# Security and execution review

This review hardens the current implementation. It is not a security certification or proof that trading losses are bounded. Local development defaults to paper; the [production profile](PRODUCTION_DEPLOYMENT.md) explicitly enables real orders and refuses missing prerequisites.

## Concrete findings and changes

| Finding | Change |
| --- | --- |
| Concurrent requests could both pass account risk checks | API execution, reconciliation, pause changes, and worker processing use the same owner-checked Redis account lease. Ownership is renewed before broker writes. |
| Valid plans were unreachable after an indented `continue` | Corrected plan construction and added a regression that proves paper mode returns a sized plan without broker writes. |
| Protective GTT timeouts could cause duplicate sell submissions | Persist the exit intent before calling Kite. Reconcile one matching GTT; uncertain or ambiguous results block further entries and require review. |
| An automatic retry could sell unrelated aggregate holdings | Removed automatic re-arming after failed protection. Recheck requires broker verification; no broker order is placed or cancelled by that action. |
| Expired or cross-account previews could be replayed | Verify account, signed browser session, preview age, current snapshot day in live mode, and symbols. Keep account-wide daily duplicate reservations across strategies. |
| Risk sizing ignored actual limit prices, holdings, and pending cash | Size against tick-rounded entry/exit limits. Check finite broker values, available live balance, pending commitments, holdings, open orders, GTTs, position count, and daily P&L before live submissions. |
| Uploaded arithmetic could allocate large strings or nested graphs | Arithmetic is numeric only, exponentiation is bounded, nesting and data budgets apply, and imports/arbitrary calls remain prohibited. Boolean operators now short circuit as Python expects. |
| Upload quota could be exceeded concurrently | Enforce the 50-strategy quota atomically in Redis. Fetch strategy documents in one storage operation and cache parsed strategy ASTs with a bounded cache. |
| Invalid request responses could echo credentials | Return field names without rejected values; redact login credentials and generated access tokens from login errors. |
| Unbounded request bodies and repeated mutations | Apply a 128 KB streamed body limit, origin checks, signed-session validation, and Redis-backed mutation limits. Local fallbacks are restricted to loopback clients. |
| Local tokens were plaintext and state writes were interruptible | Encrypt local sessions and atomically replace state files; exclude local keys and temporary state files from Git and Docker. |
| Websocket sessions could survive a disconnected browser | Cancel upstream streaming on browser disconnect, limit connections per account/process, bound upstream frames/queues, and use an async TLS-verifying transport with a bounded Kite binary tick parser. |
| Duplicate reservations and order state could be partially written | Commit the duplicate guard, durable order intent, and account indexes in one Lua operation before contacting Kite. Compare revisions before every update or archival. |
| Repeated failures or unknown submissions could loop indefinitely | Exponential backoff and six-attempt reconciliation/error budgets stop at manual review. Never blindly resubmit an uncertain entry or exit. |
| Old worker heartbeats could enable another deployment | Bind health to execution protocol, deployment, mode, origin, and encryption-key fingerprint. Refuse live startup without persistent storage and valid settings. |
| Legacy SDK websocket dependencies had published advisories | Preserve the official MIT REST SDK with source hashes and remove the legacy Autobahn transport from production requirements. |
| A GTT manually changed in Kite could be treated as the original intent | Verify symbol, exchange, product, side, quantity, triggers, and limit prices against the saved intent; verify order identities before using fills. Mismatches stop automation for review. |
| Empty tabs and temporary alerts hid operational state | Overview now shows account data and entry controls. Activity shows monitor states, persistent recent events, filters, and broker verification actions. |

## Verification

The offline regression suite replaces broker and Redis calls with fakes. Two additional integration tests run actual Lua operations against an isolated loopback Redis when `TEST_REDIS_PORT` is set; CI provides that service. No test submits real orders or accesses live account data.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
npm ci
npm run build
npm audit --omit=dev
python scripts/audit_python.py
```

The tests cover interpreter attacks, oversized requests, origin checks, credential-safe validation, forged cookies, encrypted local persistence, lease ownership, risk sizing, preview ownership/expiry, blocked entries during protection failures, and uncertain exit recovery. Frontend dependencies are pinned to the versions in the committed lockfile.

## Boundaries requiring operational validation

- Live brokerage, Upstash Lua operations, OCI restarts, and Docker hardening require validation on the actual deployment. The offline suite cannot verify broker permissions, product rules, instrument circuits, holidays, holdings authorisation, or network failure timing.
- An entry fill and its protective GTT are separate broker operations. A worker outage, expired token, GTT rejection, price gap, or unfilled limit order can leave exposure. Never treat estimated risk as a guaranteed loss limit. See [Kite GTT documentation](https://kite.trade/docs/connect/v3/gtt/).
- Daily P&L guards depend on broker positions and do not include all fees, outside-account activity, or a guaranteed realised-loss ledger. Manual positions consume capacity but their stop risk cannot be reliably inferred. External active buy GTTs therefore block new Auto Trade entries.
- Duplicate guards reserve both submission and confirmed fill days for this application's entries. External orders and manual activity cannot be made transactional with these guards. Entry-GTT expiry requires the worker to cancel and verify; an offline worker cannot guarantee cancellation before a trigger.
- Uploaded strategies use a deliberately restricted interpreter, not arbitrary Python packages. Supporting imports or unrestricted Python requires a separate isolated runner and resource policy.
- Run one API and worker for this deployment. Redis leases coordinate account operations but do not make broker APIs transactional or supply broker-native idempotency. Websocket limits currently apply per API process.
- Local encryption protects token contents on disk; access to both the key file and ciphertext defeats that protection. Restrict OS permissions and protect Redis/write tokens. Local strategy source files are not encrypted.
- The latest scoped OSV check found no published advisories for the 30 installed application runtime packages/SDK versions checked; npm's production audit also returned zero findings. These results do not cover unknown vulnerabilities or certify application logic. The legacy Autobahn package was removed from active dependencies; source provenance for the preserved Kite REST SDK is in `backend/vendor/kite_rest`.
- Validate the real deployment and broker lifecycle before unattended use. Oracle Free Tier may reclaim idle machines; it is not an availability guarantee. The production guide documents the separate entry-fill/protection operations and recovery limits.
