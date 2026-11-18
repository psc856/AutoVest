# Deploy AutoVest from GitHub to AWS EC2

This deployment runs the React frontend, FastAPI API, order worker, and Caddy HTTPS proxy on one EC2 instance. Caddy serves the frontend and routes `/api`, `/ws`, `/documentation`, `/redoc`, and `/openapi.json` to FastAPI through the private Docker network.

Upstash remains the encrypted persistent store, so sessions, strategies, notifications, and managed-order records survive an EC2 stop/start cycle. Stopping EC2 stops the website, API, worker, reconciliation, expiry handling, and notifications. AutoVest does not prevent a manual EC2 shutdown: inspect Kite and take responsibility for every remaining order and position before stopping the instance.

## 1. Confirm the GitHub repository

The deployment source is:

```text
https://github.com/psc856/AutoVest
```

Before every push, verify that real runtime secrets remain ignored:

```powershell
git check-ignore -v --no-index .env .env.production .token_store.json .token_store.key .strategy_store.json
git status --short
```

Only `.env.production.example` should be committed. Never commit `.env.production`, API credentials, Upstash tokens, the Fernet key, session files, or uploaded strategy storage.

Push a reviewed local change with:

```powershell
git add .
git diff --cached --check
git diff --cached --name-only
git commit -m "Describe the change"
git push origin main
```

## 2. Launch the EC2 instance

Open the [EC2 console](https://console.aws.amazon.com/ec2/) and select the Mumbai region (`ap-south-1`), or the AWS region you intend to register with Zerodha.

Select **Launch instance** and use:

| Setting | Value |
| --- | --- |
| Name | `autovest-production` |
| AMI | Ubuntu Server 24.04 LTS, **64-bit Arm** |
| Instance type | `t4g.small` (2 vCPU, 2 GiB RAM) |
| Purchase option | On-Demand; do not use Spot |
| Key pair | Create and securely download a `.pem` key |
| Root volume | 30 GiB `gp3`, encrypted |
| Metadata | IMDSv2 required |

Create or select a security group with only these inbound rules:

| Port | Source | Purpose |
| --- | --- | --- |
| TCP 22 | My IP | SSH administration |
| TCP 80 | `0.0.0.0/0` | HTTPS certificate issuance and redirect |
| TCP 443 | `0.0.0.0/0` | AutoVest HTTPS |

Add IPv6 HTTP/HTTPS rules only if the instance and DNS are intentionally configured for IPv6. Do not expose FastAPI port `8000`.

Launch the instance and wait until both EC2 status checks pass.

Stopping the instance ends EC2 compute billing, but the encrypted EBS volume and Elastic IPv4 address remain billable. For short manual trading sessions these fixed resources usually dominate the monthly cost; for 24/7 operation, compare the current EC2 total with the fixed-price 2 GiB Lightsail bundle before deciding.

## 3. Attach a stable Elastic IP

In **EC2 -> Network & Security -> Elastic IP addresses**:

1. Select **Allocate Elastic IP address**.
2. Select the new address and choose **Associate Elastic IP address**.
3. Associate it with the `autovest-production` instance and its primary private IP.

Keep this Elastic IP associated when the instance is stopped. It preserves DNS and Zerodha registration, although AWS continues charging for the public IPv4 address while stopped.

Create a DNS `A` record with your domain provider:

```text
invest.example.com  ->  EC2_ELASTIC_IPV4
```

Wait until the hostname resolves to the Elastic IP. Register the same public IP in the Zerodha developer console. GTT management has different IP rules from regular orders, but AutoVest can cancel the unfilled remainder of a partial regular order, so the registered egress IP is required for the complete workflow.

## 4. Connect to Ubuntu and install Docker

You can use **EC2 -> Instances -> Connect -> EC2 Instance Connect**, or connect from PowerShell with the downloaded key:

```powershell
ssh -i "C:\path\to\autovest.pem" ubuntu@EC2_ELASTIC_IPV4
```

On Ubuntu, install Docker, Compose, and Git:

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 git
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

Disconnect and reconnect once so the Docker group membership applies, then verify:

```bash
docker version
docker compose version
```

If `docker-compose-v2` is unavailable in the selected Ubuntu image, use Docker's official Ubuntu installation instructions to install Docker Engine and the Compose plugin.

## 5. Give EC2 read-only access to GitHub

Generate a repository-specific deploy key on EC2:

```bash
ssh-keygen -t ed25519 -C "autovest-ec2" -f "$HOME/.ssh/autovest_deploy" -N ""
cat "$HOME/.ssh/autovest_deploy.pub"
```

In GitHub, open **AutoVest -> Settings -> Deploy keys -> Add deploy key**. Paste the public key and leave **Allow write access** disabled.

Clone the repository:

```bash
GIT_SSH_COMMAND="ssh -i $HOME/.ssh/autovest_deploy -o IdentitiesOnly=yes" git clone git@github.com:psc856/AutoVest.git AutoVest
cd AutoVest
git config core.sshCommand "ssh -i $HOME/.ssh/autovest_deploy -o IdentitiesOnly=yes"
```

Accept GitHub's host key only after confirming it against GitHub's published SSH fingerprints.

## 6. Create the private production environment

On EC2:

```bash
cd "$HOME/AutoVest"
cp -n .env.production.example .env.production
chmod 600 .env.production
nano .env.production
```

Use your actual hostname and secret values:

```env
APP_ENV=production
DEPLOYMENT_ID=autovest-production

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

`APP_DOMAIN` is the hostname only. `FRONTEND_ORIGIN` is the exact HTTPS origin without a trailing slash. Reuse the existing Fernet key if the selected Upstash database already contains records encrypted by this application.

Keep a protected offline backup of the Fernet key and Upstash credentials. Do not place them in GitHub, EC2 user data, an AMI name, tags, or shell history shared with other users.

## 7. Build and test with order execution disabled

Build the backend and frontend images:

```bash
docker compose --env-file .env.production -f compose.production.yaml build --pull
```

Verify configuration and persistent Redis storage. This command does not contact Kite to submit or cancel an order:

```bash
docker compose --env-file .env.production -f compose.production.yaml run --rm --no-deps api python -m backend.preflight
```

Start the API and frontend without the order worker:

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

Log in using a fresh Kite request token. Verify Profile, Funds, Signals, Algo Signals, strategy upload, and scan results. Auto Trade should show that execution is unavailable while `AUTO_TRADE_MODE=disabled`.

If HTTPS is unavailable, verify DNS and security-group ports 80/443, then inspect Caddy:

```bash
docker compose --env-file .env.production -f compose.production.yaml logs --tail=100 caddy
```

## 8. Enable live execution

Edit `.env.production`:

```env
AUTO_TRADE_MODE=live
```

Run the live configuration and storage preflight. It does not place or cancel orders:

```bash
docker compose --env-file .env.production -f compose.production.yaml run --rm --no-deps api python -m backend.preflight --live
```

Start all services with the updated environment:

```bash
docker compose --env-file .env.production -f compose.production.yaml up -d --force-recreate api trade-worker caddy
docker compose --env-file .env.production -f compose.production.yaml exec api python -m backend.preflight --live --check-worker
docker compose --env-file .env.production -f compose.production.yaml ps
```

Verify:

```text
https://invest.example.com/api/health/ready
```

The UI should show **Live orders**. A strategy scan and final browser confirmation remain mandatory before AutoVest submits an entry GTT.

## 9. Use EC2 for a short trading session

EC2 continues running until you stop it. It does not automatically stop after 30 minutes.

For a short session:

1. In EC2, select `autovest-production` and choose **Instance state -> Start instance**.
2. Wait for both status checks to pass and allow Docker a short time to start the containers.
3. Open `https://invest.example.com` and confirm `/api/health/ready` is healthy.
4. Log in to Kite for the current trading day if the access token has expired.
5. Scan, review, and confirm the intended trade.
6. Keep EC2 running for the period in which you want AutoVest to monitor the setup.
7. Before stopping, inspect Kite orders, GTTs, holdings, positions, and AutoVest Activity.
8. Manually cancel, protect, close, or take responsibility for everything that will remain after shutdown.
9. In EC2, choose **Instance state -> Stop instance** and confirm. Do not select **Terminate**.

Use the normal EC2 stop action rather than **Force stop**. AWS asks the operating system to shut down, allowing Docker's stop grace periods to signal the worker and API.

When EC2 is stopped:

- The website, API, scanner, WebSocket, worker, reconciliation, notifications, and application-managed GTT expiry are offline.
- Kite-hosted orders and GTTs are not cancelled merely because EC2 stopped.
- Any fill, rejection, unfilled protective limit, or manual Kite change is your responsibility until EC2 and the worker run again.
- EC2 compute billing stops, but EBS storage and the Elastic IPv4 address continue to incur charges.

When EC2 starts again, the enabled Docker service and `restart: unless-stopped` policies should restart the existing containers. If the site does not return after the EC2 status checks pass, connect through SSH and run:

```bash
cd "$HOME/AutoVest"
docker compose --env-file .env.production -f compose.production.yaml up -d
docker compose --env-file .env.production -f compose.production.yaml ps
```

The worker will read persistent records from Upstash and attempt reconciliation. If you manually changed or closed orders while EC2 was offline, inspect Activity and use **Recheck with Kite** only after reviewing the broker state.

## 10. Monitor and update from GitHub

View recent logs:

```bash
docker compose --env-file .env.production -f compose.production.yaml logs --tail=200 api trade-worker caddy
```

Follow logs temporarily:

```bash
docker compose --env-file .env.production -f compose.production.yaml logs -f api trade-worker
```

`Ctrl+C` stops following logs; it does not stop the containers.

Deploy a tested GitHub update when no broker operation has an uncertain outcome:

```bash
cd "$HOME/AutoVest"
git pull --ff-only origin main
docker compose --env-file .env.production -f compose.production.yaml build --pull
docker compose --env-file .env.production -f compose.production.yaml up -d
docker compose --env-file .env.production -f compose.production.yaml exec api python -m backend.preflight --live --check-worker
```

Do not rebuild or restart during an uncertain submission, partial fill, missing protection, or pending exit.

## Execution limits

- New live entries require a fresh preview, recent traded price, a matching healthy worker, server risk policy, available cash, position capacity, and explicit confirmation.
- AutoVest commits the duplicate reservation and durable intent before contacting Kite. Unknown responses are reconciled without blind resubmission.
- Application expiry of an entry GTT requires the worker to remain online until cancellation is confirmed.
- A confirmed entry fill and its protective OCO placement are separate broker operations.
- Price gaps and unfilled limit orders can exceed the configured estimated risk.
- Repeated ambiguity or a broker object that differs from the saved intent stops automation for manual review.
- Upstash history is operational history, not a statutory accounting or tax ledger.

GitHub Actions runs backend tests, frontend regressions, Redis Lua integration tests, dependency audits, and both production Docker builds. Tests mock broker writes and never place real orders.
