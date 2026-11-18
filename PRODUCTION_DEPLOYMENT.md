# Deploy AutoInvest from GitHub to AWS Lightsail

This deployment runs the React frontend, FastAPI API, persistent order worker, and Caddy HTTPS proxy on one AWS Lightsail instance. The browser uses one origin, such as `https://invest.example.com`; Caddy serves React and routes `/api`, `/ws`, `/documentation`, `/redoc`, and `/openapi.json` to FastAPI.

Upstash remains the encrypted persistent store. Keeping order state outside the VM allows the API and worker to recover after an instance restart. The production profile never silently switches a failed live deployment to simulation.

## 1. Create a separate GitHub repository

In GitHub, create a new **private** repository. Leave it empty: do not add a README, license, or `.gitignore` from GitHub.

Before committing locally, confirm that secret files are ignored:

```powershell
git check-ignore -v --no-index .env .env.production .token_store.json .token_store.key
git status --short
```

The output must not show a real `.env`, token store, encryption key, or credentials as staged files. The example environment files contain placeholders and are safe to commit.

If Git asks for an author identity, configure it for this repository only:

```powershell
git config user.name "Your Name"
git config user.email "your-github-email@example.com"
```

Commit the current project:

```powershell
git add .
git diff --cached --name-only
git commit -m "Prepare AutoInvest for AWS deployment"
git branch -M main
```

Preserve the current GitHub remote under another name, then add the new repository as `origin`. Replace the URL with the new repository URL:

```powershell
git remote rename origin previous-origin
git remote add origin https://github.com/YOUR_GITHUB_USER/YOUR_NEW_REPOSITORY.git
git remote -v
git push -u origin main
```

If `origin` does not exist, omit the `git remote rename` command. Do not force-push or upload `.env.production`.

## 2. Create the Lightsail instance

In the [AWS Lightsail console](https://lightsail.aws.amazon.com/):

1. Select **Create instance**.
2. Choose a nearby region, such as Mumbai for an India-based account.
3. Choose **Linux/Unix**, **OS only**, and **Ubuntu 24.04 LTS**.
4. Select at least the 2 GB Linux bundle. The 1 GB bundle is too tight for Docker, API, worker, Caddy, and deployment builds.
5. Name it `autoinvest-production` and create it.
6. Open **Networking**, create a static IP, and attach it to the instance.
7. Keep HTTP `80` and HTTPS `443` open to the internet. Restrict SSH `22` to your current public IP.

The FastAPI port `8000` must not be exposed publicly. Caddy is the only public entry point.

Create an AWS billing budget slightly above the expected monthly bundle price so AWS emails you about unexpected charges.

## 3. Configure DNS and Zerodha

Use a domain you control and create an `A` record:

```text
invest.example.com  ->  LIGHTSAIL_STATIC_IPV4
```

Wait until the record resolves to the static IP. Caddy obtains and renews the HTTPS certificate automatically after ports 80 and 443 are reachable.

Register the same static public IP in the Zerodha developer console. GTT management has different IP requirements from regular order APIs, but this worker can cancel the unfilled remainder of a partial regular order, so the registered egress IP is required for the complete workflow.

## 4. Install Docker and Git

Select **Connect using SSH** in Lightsail and run:

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 git
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

Disconnect and reconnect once, then verify:

```bash
docker version
docker compose version
```

If the Ubuntu image does not provide `docker-compose-v2`, install Docker Engine and the Compose plugin using Docker's official Ubuntu installation guide.

## 5. Give the server read-only GitHub access

Generate a dedicated deploy key on the Lightsail instance:

```bash
ssh-keygen -t ed25519 -C "autoinvest-lightsail" -f "$HOME/.ssh/autoinvest_deploy" -N ""
cat "$HOME/.ssh/autoinvest_deploy.pub"
```

In the new GitHub repository, open **Settings -> Deploy keys -> Add deploy key**. Paste the public key and leave **Allow write access** disabled.

Clone the repository, replacing the GitHub values:

```bash
GIT_SSH_COMMAND="ssh -i $HOME/.ssh/autoinvest_deploy -o IdentitiesOnly=yes" git clone git@github.com:YOUR_GITHUB_USER/YOUR_NEW_REPOSITORY.git AutoInvest
cd AutoInvest
git config core.sshCommand "ssh -i $HOME/.ssh/autoinvest_deploy -o IdentitiesOnly=yes"
```

Accept GitHub's host fingerprint only after confirming it matches GitHub's published SSH fingerprints.

## 6. Create the production environment

Create a private environment file on Lightsail:

```bash
cp -n .env.production.example .env.production
chmod 600 .env.production
nano .env.production
```

Set the real values:

```env
APP_ENV=production
DEPLOYMENT_ID=autoinvest-production

APP_DOMAIN=invest.example.com
FRONTEND_ORIGIN=https://invest.example.com

KV_REST_API_URL=https://your-upstash-endpoint.upstash.io
KV_REST_API_TOKEN=your-upstash-write-token
KITE_TOKEN_ENCRYPTION_KEY=your-stable-fernet-key

AUTO_TRADE_MODE=disabled

TRADE_MAX_RISK_PER_TRADE=1000
TRADE_MAX_DAILY_LOSS=3000
TRADE_MAX_POSITION_VALUE=25000
TRADE_MAX_POSITIONS=5
TRADE_MAX_ACCOUNT_EXPOSURE=100000
```

`APP_DOMAIN` contains only the hostname. `FRONTEND_ORIGIN` contains the full HTTPS origin with no trailing slash. Reuse the existing stable encryption key if the Upstash database already contains sessions or strategies encrypted by this application.

Store a protected backup of the encryption key and Upstash credentials. Losing the encryption key makes the encrypted records unreadable.

## 7. Start without order execution

Build both production images:

```bash
docker compose --env-file .env.production -f compose.production.yaml build --pull
```

Check configuration and persistent storage without placing or cancelling broker orders:

```bash
docker compose --env-file .env.production -f compose.production.yaml run --rm --no-deps api python -m backend.preflight
```

Start only the web application while live execution is disabled:

```bash
docker compose --env-file .env.production -f compose.production.yaml up -d api caddy
docker compose --env-file .env.production -f compose.production.yaml ps
```

Open:

```text
https://invest.example.com
https://invest.example.com/documentation
https://invest.example.com/api/health
```

Log in with a fresh Kite request token. Verify Profile, Funds, Signals, Algo Signals, strategy upload, and scan results. Auto Trade should report that execution is unavailable while the server mode is disabled.

If HTTPS does not become available, check that DNS points to the attached static IP and Lightsail permits inbound TCP 80 and 443:

```bash
docker compose --env-file .env.production -f compose.production.yaml logs --tail=100 caddy
```

## 8. Enable the persistent live worker

After the read-only and scanner checks pass, edit `.env.production`:

```env
AUTO_TRADE_MODE=live
```

Run the live configuration and Redis preflight. It does not place or cancel broker orders:

```bash
docker compose --env-file .env.production -f compose.production.yaml run --rm --no-deps api python -m backend.preflight --live
```

Start the API and worker with the live configuration:

```bash
docker compose --env-file .env.production -f compose.production.yaml up -d --force-recreate api trade-worker caddy
docker compose --env-file .env.production -f compose.production.yaml exec api python -m backend.preflight --live --check-worker
docker compose --env-file .env.production -f compose.production.yaml ps
```

The readiness endpoint should return HTTP 200 with live orders enabled:

```text
https://invest.example.com/api/health/ready
```

The UI should show **Live orders**. A scan and final confirmation are still required before any entry is submitted. Start with one stock and small server-side limits. Confirm the resulting GTT, order status, holding, and protective OCO directly in Kite.

## 9. Monitor and update

View bounded service logs:

```bash
docker compose --env-file .env.production -f compose.production.yaml logs --tail=200 api trade-worker caddy
```

Follow logs temporarily:

```bash
docker compose --env-file .env.production -f compose.production.yaml logs -f api trade-worker
```

Stop following with `Ctrl+C`; this does not stop the containers.

Before deploying an update, check the Activity page and Kite. Do not restart the worker during an uncertain submission or while a setup requires manual review. Then deploy the committed GitHub version:

```bash
cd "$HOME/AutoInvest"
git pull --ff-only origin main
docker compose --env-file .env.production -f compose.production.yaml build --pull
docker compose --env-file .env.production -f compose.production.yaml up -d
docker compose --env-file .env.production -f compose.production.yaml exec api python -m backend.preflight --live --check-worker
```

Automatic restarts are enabled. Caddy certificate data remains in Docker volumes, and order/session state remains in Upstash.

## Execution and recovery limits

- New live entries require a fresh preview, recent traded price, healthy matching worker, server risk policy, available cash, position capacity, and explicit browser confirmation.
- The duplicate reservation and durable order intent are committed before the Kite request. Uncertain responses are reconciled without blind resubmission.
- Entry GTTs expire in the application after 30 minutes or at 15:15 India time. Cancellation requires the worker to be online.
- A confirmed fill is followed by a separate protective OCO GTT operation. Network failure, token expiry, price gaps, limit orders that do not fill, or a stopped worker can leave exposure.
- Repeated or ambiguous broker responses stop after a bounded retry budget and require manual review. A broker object that differs from the saved symbol, side, quantity, trigger, or limit price also stops automation.
- Use **Activity -> Recheck with Kite** only after inspecting Kite. Recheck reads and reconciles broker state; it does not guess or blindly resubmit an order.
- Upstash history is operational history, not a statutory accounting or tax ledger.

Run the repository tests and dependency checks before pushing a release. GitHub Actions repeats the backend, frontend, Redis Lua, dependency, and Docker image checks. Tests mock broker writes and never place real orders.
