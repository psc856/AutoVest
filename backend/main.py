"""Local FastAPI bridge for Kite Connect. Never returns access tokens to clients."""

import json
import csv
import io
import os
import re
import time
import asyncio
import math
import hashlib
import hmac
import logging
import secrets
import uuid
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from contextlib import contextmanager, asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Any
from zoneinfo import ZoneInfo
from urllib.parse import urlparse
from urllib.parse import quote

import requests
from cryptography.fernet import Fernet
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, model_validator
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
try:
    from .vendor.kite_rest import KiteConnect
    from .tick_parser import parse_ticks
    from .live_config import validate_live_settings, configuration_errors, risk_policy, heartbeat_key, deployment_fingerprint
    from .order_state import create_intent, save_monitor, archive_monitor, StateConflict
    from .guardrails import RedisLease, LeaseBusy, finite_number, position_size, atomic_json_write
    from .strategy_runtime import StrategyValidationError, evaluate_strategy, inspect_strategy
except ImportError:  # Vercel's backend root loads main.py as a top-level module.
    from vendor.kite_rest import KiteConnect
    from tick_parser import parse_ticks
    from live_config import validate_live_settings, configuration_errors, risk_policy, heartbeat_key, deployment_fingerprint
    from order_state import create_intent, save_monitor, archive_monitor, StateConflict
    from guardrails import RedisLease, LeaseBusy, finite_number, position_size, atomic_json_write
    from strategy_runtime import StrategyValidationError, evaluate_strategy, inspect_strategy

load_dotenv()
try:
    from . import opening_worker as opening
except ImportError:
    import opening_worker as opening
# Some managed local runtimes inject this unreachable proxy sentinel. Remove only
# that placeholder so Kite and Nifty requests can use a direct connection.
for _proxy_name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    _proxy_value = os.environ.get(_proxy_name, "")
    try:
        _proxy_url = urlparse(_proxy_value)
        if _proxy_url.hostname in {"127.0.0.1", "localhost", "::1"} and _proxy_url.port == 9:
            os.environ.pop(_proxy_name, None)
    except ValueError:
        pass

@asynccontextmanager
async def application_lifespan(application):
    validate_live_settings()
    if AUTO_TRADE_MODE == 'live' or os.getenv('APP_ENV') == 'production':
        check_key = 'autoinvest:startup:' + uuid.uuid4().hex
        check_value = secrets.token_hex(16)
        await run_in_threadpool(_redis_request, 'SET', check_key, check_value, 15)
        if await run_in_threadpool(_redis_request, 'GET', check_key) != check_value:
            raise RuntimeError('Persistent order storage could not be verified.')
    yield


app = FastAPI(
    title="Kite Console API",
    version="1.0.0",
    docs_url="/documentation",
    redoc_url="/redoc",
    lifespan=application_lifespan,
)
frontend_origin = os.getenv("FRONTEND_ORIGIN", "").strip().rstrip("/")
allowed_origins = []
if frontend_origin:
    allowed_origins.append(frontend_origin)
_LOCAL_DEV_ORIGIN_PATTERN = r"^http://(?:localhost|127\.0\.0\.1|\[::1\]):[0-9]{1,5}$"
_ALLOW_LOCAL_ORIGINS = os.getenv('APP_ENV', 'development').lower() != 'production'


def _origin_allowed(origin: str | None) -> bool:
    """Trust the configured deployment origin and loopback Vite development ports."""
    if origin in allowed_origins:
        return True
    if not _ALLOW_LOCAL_ORIGINS or not origin or not re.fullmatch(_LOCAL_DEV_ORIGIN_PATTERN, origin):
        return False
    try:
        port = urlparse(origin).port
        return port is not None and 1 <= port <= 65535
    except ValueError:
        return False

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_origin_regex=_LOCAL_DEV_ORIGIN_PATTERN if _ALLOW_LOCAL_ORIGINS else None,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type"],
)

TOKEN_FILE = Path(__file__).resolve().parent.parent / ".token_store.json"
LOCAL_KEY_FILE = Path(__file__).resolve().parent.parent / ".token_store.key"
STRATEGY_FILE = Path(__file__).resolve().parent.parent / ".strategy_store.json"
_SESSION_REDIS_URL = os.getenv("UPSTASH_REDIS_REST_URL") or os.getenv("KV_REST_API_URL") or ""
_SESSION_REDIS_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN") or os.getenv("KV_REST_API_TOKEN") or ""
_SESSION_REDIS_KEY_PREFIX = "kite-console:session:"
_SESSION_COOKIE = "kite_console_session"
_TOKEN_ENCRYPTION_KEY = os.getenv("KITE_TOKEN_ENCRYPTION_KEY", "")
AUTO_TRADE_MODE = os.getenv("AUTO_TRADE_MODE", "paper").strip().lower()
_SECURE_COOKIE = frontend_origin.startswith("https://")
_APP_DOMAIN = os.getenv('APP_DOMAIN', '').strip().lower()
_COOKIE_SAMESITE = "lax" if _APP_DOMAIN and urlparse(frontend_origin).hostname == _APP_DOMAIN else "none" if _SECURE_COOKIE else "lax"
_lock = RLock()
_kite: KiteConnect | None = None
_instruments: list[dict[str, Any]] | None = None
_scanner_cache: dict[str, Any] | None = None
_scanner_cache_at = 0.0
_scanner_cache_lock = RLock()
_scanner_cache_account: str | None = None
_strategy_lock = RLock()
_local_previews: dict[str, dict[str, Any]] = {}
_local_trade_events: dict[str, list[dict[str, Any]]] = {}


@contextmanager
def _scanner_slot():
    if not _scanner_cache_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A market-data refresh is already running. Please retry shortly.")
    try:
        yield
    finally:
        _scanner_cache_lock.release()


class LoginPayload(BaseModel):
    api_key: str = Field(min_length=1, max_length=128)
    api_secret: str = Field(min_length=1, max_length=256)
    request_token: str = Field(min_length=1, max_length=512)


class SignalRequest(BaseModel):
    short_sma: int = Field(default=6, ge=2, le=200)
    long_sma: int = Field(default=30, ge=3, le=300)
    lookback_days: int = Field(default=30, ge=1, le=365)
    max_stocks: int = Field(default=25, ge=1, le=100)

    @model_validator(mode="after")
    def validate_periods(self):
        if self.short_sma >= self.long_sma:
            raise ValueError("Short SMA must be smaller than Long SMA.")
        return self


class StrategyUpload(BaseModel):
    filename: str = Field(min_length=1, max_length=128)
    source: str = Field(min_length=1, max_length=65536)


class StrategyScanRequest(BaseModel):
    parameters: dict[str, Any] = Field(default_factory=dict)


class TradeRiskSettings(BaseModel):
    risk_per_trade: float = Field(default=250, gt=0, le=100000, allow_inf_nan=False)
    max_daily_loss: float = Field(default=750, gt=0, le=1000000, allow_inf_nan=False)
    max_open_positions: int = Field(default=3, ge=1, le=50)
    max_position_value: float = Field(default=10000, gt=0, le=10000000, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_budget(self):
        if self.risk_per_trade > self.max_daily_loss:
            raise ValueError("Per-trade risk cannot exceed the daily loss guard.")
        return self


class TradeExecuteRequest(BaseModel):
    preview_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    symbols: list[str] = Field(min_length=1, max_length=50)
    risk: TradeRiskSettings = Field(default_factory=TradeRiskSettings)


class PauseRequest(BaseModel):
    paused: bool


def _safe_monitors(account_id):
    if not _remote_session_store_enabled():
        return []
    fields = ("id", "symbol", "quantity", "filled_quantity", "stage", "entry", "stop_loss", "target", "risk", "entry_gtt_id", "exit_gtt_id", "created_at", "last_error", 'last_verified_at', 'next_check_at')
    monitors = []
    for monitor_id in _redis_request("SMEMBERS", f"autoinvest:active:{account_id}") or []:
        encoded = _redis_request("GET", f"autoinvest:monitor:{monitor_id}")
        if encoded:
            monitor = json.loads(encoded)
            monitors.append({key: monitor.get(key) for key in fields})
        else:
            monitors.append({"id": monitor_id, "symbol": "Unknown", "stage": "RECORD_MISSING"})
    return sorted(monitors, key=lambda item: item.get("created_at") or 0, reverse=True)


@app.post("/api/auto-trade/pause")
def pause_entries(payload: PauseRequest, request: Request):
    _require_frontend_origin(request)
    client = _current_kite(request)
    if client is None:
        raise HTTPException(status_code=401, detail="Please log in first.")
    if not _remote_session_store_enabled():
        raise HTTPException(status_code=409, detail="Account pause controls require persistent Redis storage.")
    account = _account_id(client)
    try:
        with RedisLease(_redis_request, _redis_eval, f"autoinvest:account-lock:{account}"):
            if payload.paused:
                _redis_request("SET", f"autoinvest:paused:{account}", "1")
            else:
                _redis_request("DEL", f"autoinvest:paused:{account}")
    except LeaseBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _emit_trade_event(account, "New entries paused" if payload.paused else "New entries resumed", "Existing broker orders and protective GTTs remain active. Manage cancellations in Kite.", "warning" if payload.paused else "info")
    return {"paused": payload.paused}


@app.get("/api/account/summary")
def account_summary(request: Request):
    client = _current_kite(request)
    if client is None:
        raise HTTPException(status_code=401, detail="Please log in first.")
    account = _account_id(client)
    warnings = []
    snapshot = {"available_cash": None, "day_pnl": None, "holding_count": None, "open_positions": None}
    try:
        margins = client.margins("equity")
        snapshot["available_cash"] = finite_number(margins.get("available", {}).get("live_balance"))
    except Exception:
        warnings.append("Funds are unavailable. Refresh or verify your Kite session.")
    try:
        positions = client.positions()
        snapshot["day_pnl"] = sum(finite_number(p.get("pnl", 0) or 0) for p in positions.get("day", []))
        snapshot["open_positions"] = sum(1 for p in positions.get("net", []) if p.get("quantity"))
    except Exception:
        warnings.append("Positions and daily P&L could not be verified.")
    try:
        snapshot["holding_count"] = sum(1 for h in client.holdings() if int(h.get("quantity", 0) or 0) + int(h.get("t1_quantity", 0) or 0) > 0)
    except Exception:
        warnings.append("Holdings are unavailable.")
    return {**snapshot, "warnings": warnings, "as_of": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(),
            "monitors": _safe_monitors(account), "status": auto_trade_status(request)}


def _remote_session_store_enabled() -> bool:
    configured = bool(_SESSION_REDIS_URL or _SESSION_REDIS_TOKEN)
    if configured and not (_SESSION_REDIS_URL and _SESSION_REDIS_TOKEN):
        raise RuntimeError("Upstash Redis is only partially configured; set its REST URL and REST token.")
    if configured and not _TOKEN_ENCRYPTION_KEY:
        raise RuntimeError("Set KITE_TOKEN_ENCRYPTION_KEY in the backend environment to encrypt the saved Kite token.")
    if not configured and (os.getenv("VERCEL") or os.getenv("VERCEL_ENV")):
        raise RuntimeError("Connect Upstash Redis to the backend Vercel project and set KITE_TOKEN_ENCRYPTION_KEY.")
    return configured


def _redis_request(method: str, key: str, value: str | None = None, ttl: int | None = None, nx: bool = False) -> Any:
    url = _SESSION_REDIS_URL.rstrip("/")
    headers = {"Authorization": f"Bearer {_SESSION_REDIS_TOKEN}"}
    if method == "GET":
        response = requests.get(f"{url}/get/{quote(key, safe='')}", headers=headers, timeout=8)
    elif method == "SMEMBERS":
        response = requests.get(f"{url}/smembers/{quote(key, safe='')}", headers=headers, timeout=8)
    elif method == "SET":
        command = ["SET", key, value] + (["EX", ttl] if ttl else []) + (["NX"] if nx else [])
        response = requests.post(url, headers={**headers, "Content-Type": "application/json"}, json=command, timeout=8)
    elif method in {"SADD", "SREM"}:
        response = requests.post(url, headers={**headers, "Content-Type": "application/json"}, json=[method, key, value], timeout=8)
    elif method == "RPOP":
        response = requests.get(f"{url}/rpop/{quote(key, safe='')}", headers=headers, timeout=8)
    elif method == "LRANGE":
        response = requests.get(f"{url}/lrange/{quote(key, safe='')}/0/99", headers=headers, timeout=8)
    elif method == "LPUSH":
        response = requests.post(url, headers={**headers, "Content-Type": "application/json"}, json=["LPUSH", key, value], timeout=8)
    elif method == "LTRIM":
        response = requests.post(url, headers={**headers, "Content-Type": "application/json"}, json=["LTRIM", key, "0", "99"], timeout=8)
    elif method == "DEL":
        response = requests.post(url, headers={**headers, "Content-Type": "application/json"}, json=["DEL", key], timeout=8)
    else:
        raise ValueError("Unsupported Redis command")
    response.raise_for_status()
    result = response.json()
    if result.get("error"):
        raise RuntimeError("Upstash Redis rejected the session storage request.")
    return result.get("result")


def _redis_eval(script: str, keys: list[str], args: list[str]) -> Any:
    response = requests.post(_SESSION_REDIS_URL.rstrip("/"), headers={"Authorization": f"Bearer {_SESSION_REDIS_TOKEN}"},
                             json=["EVAL", script, len(keys), *keys, *args], timeout=8)
    response.raise_for_status()
    result = response.json()
    if result.get("error"):
        raise RuntimeError("Redis guardrail operation failed.")
    return result.get("result")


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, exc: RequestValidationError):
    # Pydantic normally echoes rejected inputs, which can include credentials.
    fields = [".".join(map(str, item["loc"][1:])) for item in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": "Check the supplied fields: " + ", ".join(fields[:8])})


_request_windows: dict[str, tuple[float, int]] = {}


@app.middleware("http")
async def protect_requests(request: Request, call_next):
    if not (_SESSION_REDIS_URL or _SESSION_REDIS_TOKEN) and (request.url.hostname not in {"localhost", "127.0.0.1", "::1", "testserver"} or not request.client or request.client.host not in {"localhost", "127.0.0.1", "::1", "testclient"}):
        return JSONResponse(status_code=503, content={"detail": "Hosted access requires encrypted Redis sessions. Local shared sessions are restricted to loopback."})
    if request.method in {"POST", "DELETE"}:
        if not _origin_allowed(request.headers.get("origin")):
            return JSONResponse(status_code=403, content={"detail": "Request origin is not allowed."})
        try:
            if int(request.headers.get("content-length", "0")) > 131072:
                return JSONResponse(status_code=413, content={"detail": "Request exceeds the 128 KB limit."})
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "Invalid request length."})
        # Limit streamed bodies too, before JSON parsing or credential validation.
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 131072:
                return JSONResponse(status_code=413, content={"detail": "Request exceeds the 128 KB limit."})
        request._body = bytes(body)
        bucket = "login" if request.url.path == "/api/auth/login" else "mutation"
        identity = _session_id_from_cookie(request.cookies.get(_SESSION_COOKIE)) or (request.client.host if request.client else "unknown")
        key = "autoinvest:rate:" + hashlib.sha256(f"{bucket}:{identity}".encode()).hexdigest()
        limit = 10 if bucket == "login" else 30
        try:
            if _remote_session_store_enabled():
                count = await run_in_threadpool(_redis_eval, "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],60) end; return n", [key], [])
            else:
                now = time.monotonic()
                for old_key, (started, _) in list(_request_windows.items()):
                    if now - started >= 60:
                        del _request_windows[old_key]
                started, count = _request_windows.get(key, (now, 0))
                count += 1
                _request_windows[key] = (started, count)
            if count > limit:
                return JSONResponse(status_code=429, content={"detail": "Too many requests. Please retry in a minute."}, headers={"Retry-After": "60"})
        except Exception:
            return JSONResponse(status_code=503, content={"detail": "Security storage is unavailable. Please retry shortly."})
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    return response


@app.exception_handler(Exception)
async def unexpected_failure(request: Request, exc: Exception):
    # Never serialize raw SDK/network errors, local paths, or credentials.
    return JSONResponse(status_code=503, content={"detail": "The operation could not be verified. Refresh account activity and check Kite before retrying a trade."}, headers={"Cache-Control": "no-store"})


def _read_saved_session(session_id: str | None = None) -> dict[str, str] | None:
    if _remote_session_store_enabled():
        if not session_id:
            return None
        encoded = _redis_request("GET", f"{_SESSION_REDIS_KEY_PREFIX}{session_id}")
        if not encoded:
            return None
        saved = json.loads(encoded)
        token = Fernet(_TOKEN_ENCRYPTION_KEY.encode()).decrypt(saved["encrypted_access_token"].encode()).decode()
        return {"api_key": saved["api_key"], "access_token": token}
    if not TOKEN_FILE.exists():
        return None
    saved = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    if "encrypted_session" in saved:
        return json.loads(_local_cipher().decrypt(saved["encrypted_session"].encode()))
    # Upgrade existing local plaintext sessions without invalidating the token.
    _write_saved_session(saved["api_key"], saved["access_token"])
    return saved


def _local_cipher() -> Fernet:
    if _TOKEN_ENCRYPTION_KEY:
        return Fernet(_TOKEN_ENCRYPTION_KEY.encode())
    try:
        # Exclusive creation prevents two first logins from choosing different keys.
        fd = os.open(LOCAL_KEY_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return Fernet(LOCAL_KEY_FILE.read_bytes().strip())
    with os.fdopen(fd, "wb") as handle:
        key = Fernet.generate_key()
        handle.write(key)
        handle.flush()
        os.fsync(handle.fileno())
    return Fernet(key)


def _write_saved_session(api_key: str, access_token: str, session_id: str | None = None) -> None:
    if _remote_session_store_enabled():
        if not session_id:
            raise RuntimeError("A browser session is required to save the Kite session.")
        encrypted = Fernet(_TOKEN_ENCRYPTION_KEY.encode()).encrypt(access_token.encode()).decode()
        _redis_request("SET", f"{_SESSION_REDIS_KEY_PREFIX}{session_id}", json.dumps({"api_key": api_key, "encrypted_access_token": encrypted}), ttl=86400)
        return
    encrypted = _local_cipher().encrypt(json.dumps({"api_key": api_key, "access_token": access_token}).encode()).decode()
    atomic_json_write(TOKEN_FILE, {"encrypted_session": encrypted})


def _delete_saved_session(session_id: str | None = None) -> None:
    if _remote_session_store_enabled():
        if session_id:
            _redis_request("DEL", f"{_SESSION_REDIS_KEY_PREFIX}{session_id}")
    else:
        TOKEN_FILE.unlink(missing_ok=True)


def _load_saved_session() -> None:
    """Restore a persisted session without exposing its token to clients."""
    global _kite
    try:
        saved = _read_saved_session()
        if saved and saved.get("api_key") and saved.get("access_token"):
            client = KiteConnect(api_key=saved["api_key"])
            client.set_access_token(saved["access_token"])
            _kite = client
        else:
            _kite = None
    except Exception:
        _kite = None


if not (_SESSION_REDIS_URL or _SESSION_REDIS_TOKEN):
    _load_saved_session()


def _signed_session_cookie(session_id: str) -> str:
    signature = hmac.new(_TOKEN_ENCRYPTION_KEY.encode(), session_id.encode(), hashlib.sha256).hexdigest()
    return f"{session_id}.{signature}"


def _session_id_from_cookie(cookie: str | None) -> str | None:
    if not cookie or "." not in cookie:
        return None
    session_id, signature = cookie.rsplit(".", 1)
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", session_id) or not re.fullmatch(r"[a-f0-9]{64}", signature):
        return None
    expected = hmac.new(_TOKEN_ENCRYPTION_KEY.encode(), session_id.encode(), hashlib.sha256).hexdigest()
    return session_id if hmac.compare_digest(signature, expected) else None


def _current_kite(request: Request | WebSocket) -> KiteConnect | None:
    """Load the Kite session belonging to this browser, never a shared global cloud session."""
    if _SESSION_REDIS_URL or _SESSION_REDIS_TOKEN:
        session_id = _session_id_from_cookie(request.cookies.get(_SESSION_COOKIE))
        saved = _read_saved_session(session_id)
        if saved and saved.get("api_key") and saved.get("access_token"):
            client = KiteConnect(api_key=saved["api_key"])
            client.set_access_token(saved["access_token"])
            return client
        return None
    return _kite


def _worker_session_key(account_id: str, api_key: str) -> str:
    return f"autoinvest:worker-session:{account_id}:{hashlib.sha256(api_key.encode()).hexdigest()}"


def _rebind_trade_monitors(account_id: str, api_key: str, session_id: str) -> None:
    """Refresh worker credentials without racing an order-state write."""
    if _remote_session_store_enabled():
        _redis_request("SET", _worker_session_key(account_id, api_key), session_id, ttl=86400)


@app.get("/api/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.get('/api/health/ready')
def readiness():
    ready = not configuration_errors()
    if AUTO_TRADE_MODE == 'live':
        try:
            ready = ready and _trade_worker_online()
        except Exception:
            ready = False
    return JSONResponse(status_code=200 if ready else 503, content={'ready': ready, 'live_orders': ready and AUTO_TRADE_MODE == 'live'})


@app.get("/api/auth/status")
def auth_status(request: Request) -> dict[str, bool]:
    client = _current_kite(request)
    if client is None:
        return {"authenticated": False}
    try:
        client.profile()
        return {"authenticated": True}
    except Exception:
        return {"authenticated": False}


@app.post("/api/auth/login")
def login(payload: LoginPayload, request: Request, response: Response) -> dict[str, bool]:
    global _kite
    _require_frontend_origin(request)
    with _lock:
        access_token = None
        try:
            # Detect broken storage/encryption before consuming a one-use Kite token.
            remote_store = _remote_session_store_enabled()
            if remote_store:
                Fernet(_TOKEN_ENCRYPTION_KEY.encode())
            client = KiteConnect(api_key=payload.api_key.strip())
            session = client.generate_session(
                payload.request_token.strip(), api_secret=payload.api_secret.strip()
            )
            access_token = session.get("access_token")
            if not access_token:
                raise ValueError("Kite did not return an access token.")
            client.set_access_token(access_token)
            session_id = secrets.token_urlsafe(32) if remote_store else None
            _write_saved_session(payload.api_key.strip(), access_token, session_id)
            if remote_store:
                try:
                    _rebind_trade_monitors(_account_id(client), payload.api_key.strip(), session_id)
                except Exception:
                    # Login must still succeed; the worker will alert if an old
                    # session expires before its outstanding orders are resolved.
                    pass
                response.set_cookie(_SESSION_COOKIE, _signed_session_cookie(session_id), httponly=True, secure=_SECURE_COOKIE, samesite=_COOKIE_SAMESITE, max_age=86400, path="/")
            else:
                _kite = client
        except Exception as exc:
            # Show the SDK's useful failure reason while redacting every submitted credential.
            message = str(getattr(exc, "message", str(exc)) or exc)
            for secret_value in (payload.api_key.strip(), payload.api_secret.strip(), payload.request_token.strip(), access_token):
                if secret_value:
                    message = message.replace(secret_value, "[redacted]")
            message = re.sub(r"\s+", " ", message).strip()[:320]
            exception_name = type(exc).__name__
            raise HTTPException(
                status_code=400,
                detail=f"Kite login failed ({exception_name}): {message or 'Unknown Kite Connect error.'}",
            ) from exc
    return {"authenticated": True}


@app.get("/api/user/profile")
def user_profile(request: Request) -> dict[str, Any]:
    client = _current_kite(request)
    if client is None:
        raise HTTPException(status_code=401, detail="Please log in to Kite Connect first.")
    try:
        profile = client.profile()
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Could not load your profile. Your session may have expired.") from exc
    return {
        "user_name": profile.get("user_name", "—"),
        "user_id": profile.get("user_id", "—"),
        "products": profile.get("products", []),
        "exchanges": profile.get("exchanges", []),
        "email": profile.get("email", ""),
        "broker": profile.get("broker", "Zerodha"),
    }


@app.post("/api/auth/logout")
def logout(request: Request, response: Response) -> dict[str, bool]:
    """Forget the local token so the next visit returns to the login screen."""
    _require_frontend_origin(request)
    global _kite
    with _lock:
        try:
            session_id = _session_id_from_cookie(request.cookies.get(_SESSION_COOKIE))
            client = _current_kite(request)
            retain_for_worker = False
            if client and _remote_session_store_enabled():
                account_id = _account_id(client)
                opening_job = opening.get_job(_redis_request, account_id)
                if (opening_job and opening_job['session'] == session_id and opening_job['state'] in opening.ACTIVE
                        and time.time() < opening_job['deadline']):
                    raise HTTPException(status_code=409, detail="Stop the active Opening Stop Loss run before signing out.")
                active_ids = _redis_request("SMEMBERS", f"autoinvest:active:{account_id}") or []
                stages = []
                for monitor_id in active_ids:
                    encoded = _redis_request("GET", f"autoinvest:monitor:{monitor_id}")
                    if encoded:
                        monitor = json.loads(encoded)
                        stages.append(monitor.get("stage"))
                if any(stage in {"SUBMITTING_ENTRY", "SUBMISSION_UNKNOWN", "ENTRY_PENDING", "CANCEL_ENTRY_REMAINDER"} for stage in stages):
                    raise HTTPException(status_code=409, detail="A buy GTT is still pending. Cancel or resolve it in Kite before signing out, so a later fill is not left unmanaged.")
                retain_for_worker = bool(active_ids)
            if not retain_for_worker:
                _delete_saved_session(session_id)
            _kite = None
            response.delete_cookie(_SESSION_COOKIE, path="/", secure=_SECURE_COOKIE, httponly=True, samesite=_COOKIE_SAMESITE)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Could not clear the saved session.") from exc
    return {"authenticated": False, "trade_monitoring": retain_for_worker}


def _account_id(client: KiteConnect) -> str:
    try:
        account_id = str(client.profile().get("user_id", "")).strip()
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Could not verify the Kite account for strategy storage.") from exc
    if not account_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", account_id):
        raise HTTPException(status_code=401, detail="Kite did not return a valid account ID.")
    return account_id


def _strategy_index_key(account_id: str) -> str:
    return f"autoinvest:strategies:{account_id}"


def _strategy_document_key(account_id: str, strategy_id: str) -> str:
    return f"autoinvest:strategy:{account_id}:{strategy_id}"


def _require_frontend_origin(request: Request) -> None:
    if not _origin_allowed(request.headers.get("origin")):
        raise HTTPException(status_code=403, detail="Request origin is not allowed.")


def _get_account_strategy(account_id: str, strategy_id: str) -> dict[str, Any] | None:
    if _remote_session_store_enabled():
        encrypted = _redis_request("GET", _strategy_document_key(account_id, strategy_id))
        if not encrypted:
            return None
        return json.loads(Fernet(_TOKEN_ENCRYPTION_KEY.encode()).decrypt(encrypted.encode()).decode())
    with _strategy_lock:
        return _local_strategy_store().get(account_id, {}).get(strategy_id)


def _local_strategy_store() -> dict[str, dict[str, Any]]:
    if not STRATEGY_FILE.exists():
        return {}
    try:
        return json.loads(STRATEGY_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _read_account_strategies(account_id: str) -> list[dict[str, Any]]:
    if _remote_session_store_enabled():
        documents = _redis_eval("local ids=redis.call('SMEMBERS',KEYS[1]); local result={}; for _,id in ipairs(ids) do local doc=redis.call('GET',ARGV[1]..id); if doc then table.insert(result,doc) end end; return result", [_strategy_index_key(account_id)], [f"autoinvest:strategy:{account_id}:"]) or []
        strategies = []
        for encrypted in documents:
            if encrypted:
                strategies.append(json.loads(Fernet(_TOKEN_ENCRYPTION_KEY.encode()).decrypt(encrypted.encode()).decode()))
        return sorted(strategies, key=lambda item: item.get("created_at", ""), reverse=True)
    with _strategy_lock:
        return list(_local_strategy_store().get(account_id, {}).values())


@app.get("/api/strategies")
def list_strategies(request: Request) -> dict[str, Any]:
    client = _current_kite(request)
    if client is None:
        raise HTTPException(status_code=401, detail="Please log in to Kite Connect first.")
    return {"strategies": _read_account_strategies(_account_id(client))}


@app.post("/api/strategies")
def upload_strategy(payload: StrategyUpload, request: Request) -> dict[str, Any]:
    _require_frontend_origin(request)
    client = _current_kite(request)
    if client is None:
        raise HTTPException(status_code=401, detail="Please log in to Kite Connect first.")
    if not payload.filename.lower().endswith(".py") or Path(payload.filename).name != payload.filename:
        raise HTTPException(status_code=400, detail="Upload a .py strategy file using a plain filename.")
    try:
        metadata = inspect_strategy(payload.source)
    except StrategyValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    account_id = _account_id(client)
    strategy_id = uuid.uuid4().hex
    document = {
        "id": strategy_id,
        "filename": payload.filename,
        "name": metadata["name"].strip(),
        "description": metadata.get("description", "").strip(),
        "parameters": metadata.get("parameters", []),
        "source": payload.source,
        "created_at": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(timespec="seconds"),
    }
    with _strategy_lock:
        if _remote_session_store_enabled():
            encrypted = Fernet(_TOKEN_ENCRYPTION_KEY.encode()).encrypt(json.dumps(document).encode()).decode()
            stored = _redis_eval("if redis.call('SCARD',KEYS[1])>=50 then return 0 end; redis.call('SET',KEYS[2],ARGV[2]); redis.call('SADD',KEYS[1],ARGV[1]); return 1", [_strategy_index_key(account_id), _strategy_document_key(account_id, strategy_id)], [strategy_id, encrypted])
            if stored != 1:
                raise HTTPException(status_code=409, detail="This account has reached the limit of 50 strategies. Remove a strategy before uploading another.")
        else:
            store = _local_strategy_store()
            if len(store.get(account_id, {})) >= 50:
                raise HTTPException(status_code=409, detail="This account has reached the limit of 50 strategies.")
            store.setdefault(account_id, {})[strategy_id] = document
            atomic_json_write(STRATEGY_FILE, store)
    return {"strategy": document}


@app.delete("/api/strategies/{strategy_id}")
def delete_strategy(strategy_id: str, request: Request) -> dict[str, bool]:
    _require_frontend_origin(request)
    client = _current_kite(request)
    if client is None:
        raise HTTPException(status_code=401, detail="Please log in to Kite Connect first.")
    if not re.fullmatch(r"[a-f0-9]{32}", strategy_id):
        raise HTTPException(status_code=404, detail="Strategy not found.")
    account_id = _account_id(client)
    with _strategy_lock:
        if _remote_session_store_enabled():
            ids = _redis_request("SMEMBERS", _strategy_index_key(account_id)) or []
            if strategy_id not in ids:
                raise HTTPException(status_code=404, detail="Strategy not found.")
            _redis_request("DEL", _strategy_document_key(account_id, strategy_id))
            _redis_request("SREM", _strategy_index_key(account_id), strategy_id)
        else:
            store = _local_strategy_store()
            if strategy_id not in store.get(account_id, {}):
                raise HTTPException(status_code=404, detail="Strategy not found.")
            del store[account_id][strategy_id]
            atomic_json_write(STRATEGY_FILE, store)
    return {"deleted": True}


@app.post("/api/strategies/{strategy_id}/scan")
def scan_strategy(strategy_id: str, payload: StrategyScanRequest, request: Request) -> dict[str, Any]:
    _require_frontend_origin(request)
    client = _current_kite(request)
    if client is None:
        raise HTTPException(status_code=401, detail="Please log in to Kite Connect first.")
    if not re.fullmatch(r"[a-f0-9]{32}", strategy_id):
        raise HTTPException(status_code=404, detail="Strategy not found.")
    document = _get_account_strategy(_account_id(client), strategy_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Strategy not found.")

    declared = {item["key"]: item for item in document["parameters"]}
    if set(payload.parameters) - set(declared):
        raise HTTPException(status_code=422, detail="Scan parameters include keys that are not declared by this strategy.")
    parameters = {}
    for key, schema in declared.items():
        value = payload.parameters.get(key, schema["default"])
        if schema["type"] == "boolean":
            valid = isinstance(value, bool)
        elif schema["type"] == "integer":
            valid = isinstance(value, int) and not isinstance(value, bool) and schema["min"] <= value <= schema["max"]
        else:
            valid = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and schema["min"] <= value <= schema["max"]
        if not valid:
            raise HTTPException(status_code=422, detail=f"Invalid value for strategy parameter {key}.")
        parameters[key] = value

    data = scanner_data(request)
    candidates = []
    failures = 0
    for stock in data["stocks"]:
        try:
            closed_candles = [c for c in stock["candles"] if c["date"] < datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()]
            outcome = evaluate_strategy(document["source"], closed_candles, parameters)
            if not isinstance(outcome, dict) or not outcome.get("signal"):
                continue
            entry = float(outcome.get("entry_price", closed_candles[-1]["close"]))
            stop = float(outcome["stop_loss"])
            target = float(outcome["target_price"])
            if not all(math.isfinite(value) and value > 0 for value in (entry, stop, target)) or not stop < entry < target:
                raise ValueError("A signal needs positive prices ordered as stop loss < entry < target.")
            tick = float(stock.get("tick_size") or 0.05)
            entry = _price_to_tick(entry, tick, upward=True)
            stop = _price_to_tick(stop, tick, upward=False)
            target = _price_to_tick(target, tick, upward=True)
            entry_limit = _price_to_tick(entry * 1.005, tick, upward=True)
            stop_limit = _price_to_tick(stop * 0.995, tick, upward=False)
            if not 0 < stop_limit < stop < entry <= entry_limit < target:
                raise ValueError("Entry, stop loss, and target collapse to invalid levels at this instrument's tick size.")
            candidates.append({
                "symbol": stock["symbol"], "companyName": stock["companyName"],
                "instrument_token": stock["instrument_token"], "tick_size": tick, "entry_price": entry,
                "entry_limit_price": entry_limit, "stop_limit_price": stop_limit,
                "stop_loss": stop, "target_price": target,
                "reason": str(outcome.get("reason", "Strategy conditions matched"))[:240],
            })
        except Exception:
            failures += 1
    candidates.sort(key=lambda item: item["symbol"])
    preview_id = uuid.uuid4().hex
    account_id = _account_id(client)
    preview = {
        "id": preview_id, "account_id": account_id,
        "session_id": _session_id_from_cookie(request.cookies.get(_SESSION_COOKIE)),
        "strategy": {"id": document["id"], "name": document["name"]},
        "asOf": data["metadata"]["asOf"], "created_at": time.time(), "candidates": candidates,
    }
    if _remote_session_store_enabled():
        _redis_request("SET", f"autoinvest:preview:{account_id}:{preview_id}", json.dumps(preview), ttl=600)
    else:
        for old_id, old_preview in list(_local_previews.items()):
            if time.time() - old_preview.get("created_at", 0) >= 600:
                _local_previews.pop(old_id, None)
        if len(_local_previews) >= 100:
            _local_previews.pop(next(iter(_local_previews)))
        _local_previews[preview_id] = preview
    return {
        "preview_id": preview_id,
        "strategy": {"id": document["id"], "name": document["name"]},
        "scanned": len(data["stocks"]), "matches": len(candidates), "evaluationErrors": failures,
        "asOf": data["metadata"]["asOf"], "candidates": candidates,
    }


def _emit_trade_event(account_id: str, title: str, message: str, level: str = "info", details: dict[str, Any] | None = None) -> None:
    event = {"id": uuid.uuid4().hex, "at": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(timespec="seconds"), "title": title[:100], "message": message[:300], "level": level, "details": details or {}}
    if _remote_session_store_enabled():
        key = f"autoinvest:events:{account_id}"
        try:
            _redis_request("LPUSH", key, json.dumps(event))
            _redis_request("LTRIM", key)
        except Exception:
            pass  # An unavailable notification sink must never rewind broker state.
    else:
        _local_trade_events.setdefault(account_id, []).insert(0, event)
        del _local_trade_events[account_id][100:]


def _trade_worker_online() -> bool:
    if not _remote_session_store_enabled():
        return False
    heartbeat = _redis_request("GET", heartbeat_key())
    try:
        timestamp = float(heartbeat)
        return math.isfinite(timestamp) and 0 <= time.time() - timestamp < 90
    except (TypeError, ValueError):
        return False


def _opening_account(request):
    client = _current_kite(request)
    if client is None:
        raise HTTPException(status_code=401, detail="Please log in to Kite first.")
    return client, _account_id(client)


def _opening_status(job):
    if not job:
        return None
    status = {key: job[key] for key in ('state', 'rows', 'message', 'attempts')}
    status['confirmations'] = job.get('confirmations', {})
    if status['state'] in opening.ACTIVE and time.time() >= job['deadline']:
        status.update(state='review', message='Run expired. Check outstanding protection in Kite.')
    return status


def _opening_snapshot_step(label, operation):
    try:
        return operation()
    except Exception as exc:
        logging.getLogger(__name__).warning('Opening stop snapshot failed at %s (%s)', label, type(exc).__name__)
        if type(exc).__name__ == 'TokenException':
            raise HTTPException(status_code=401, detail='Your Kite session expired. Sign in again and refresh.') from exc
        if type(exc).__name__ == 'PermissionException':
            raise HTTPException(status_code=403, detail=f'Kite denied access to {label}. Check your Kite Connect permissions.') from exc
        raise HTTPException(status_code=502, detail=f'Could not load {label} from Kite. Refresh and try again.') from exc


def _opening_positions(client):
    positions = client.positions()['net']
    if not isinstance(positions, list):
        raise ValueError('Invalid positions response')
    return [p for p in positions if opening.is_intraday_equity(p) and int(p['quantity']) > 0]


@app.get('/api/opening-stop')
def opening_stop_status(request: Request, snapshot: bool = False):
    client, account = _opening_account(request)
    remote = _remote_session_store_enabled()
    job = opening.get_job(_redis_request, account) if remote else None
    online = bool(remote and AUTO_TRADE_MODE == 'live' and _redis_request('GET', opening.HEARTBEAT))
    result = {'job': _opening_status(job), 'available': online}
    if snapshot:
        positions = _opening_snapshot_step('intraday positions', lambda: _opening_positions(client))
        if not positions:
            result['rows'] = []
            return result
        orders = _opening_snapshot_step('the order book', client.orders)
        ticks = _opening_snapshot_step('instrument tick sizes', lambda: opening.tick_sizes(client, positions))
        result['rows'], _ = _opening_snapshot_step('stop-loss details', lambda: opening.plan(positions, orders, ticks))
    return result


@app.post('/api/opening-stop')
def start_opening_stop(request: Request):
    _require_frontend_origin(request)
    if AUTO_TRADE_MODE != 'live' or not _remote_session_store_enabled() or not _redis_request('GET', opening.HEARTBEAT):
        raise HTTPException(status_code=503, detail='Opening Stop Loss requires the live worker to be online.')
    _, account = _opening_account(request)
    session = _session_id_from_cookie(request.cookies.get(_SESSION_COOKIE))
    now = opening.market_time()
    if now.weekday() >= 5 or not now.replace(hour=9, minute=0, second=0, microsecond=0).time() <= now.time() < opening.CUTOFF:
        raise HTTPException(status_code=409, detail='Arm this on a weekday between 9:00 AM and 3:20 PM IST.')
    day = now.date().isoformat()
    try:
        with RedisLease(_redis_request, _redis_eval, f'autoinvest:account-lock:{account}') as lease:
            previous = opening.get_job(_redis_request, account)
            if previous and previous['date'] == day:
                if previous['state'] in opening.ACTIVE and time.time() < previous['deadline']:
                    return _opening_status(previous)
                if previous['pending']:
                    raise HTTPException(status_code=409, detail='A previous submission is unconfirmed. Verify orders and any slices in Kite before managing this position manually.')
            start = max(now.timestamp(), now.replace(hour=9, minute=15, second=0, microsecond=0).timestamp())
            cutoff = datetime.combine(now.date(), opening.CUTOFF, tzinfo=now.tzinfo).timestamp()
            job = {'account': account, 'session': session, 'date': day, 'queue': opening.QUEUE,
                   'deadline': min(start + 180, cutoff), 'scope': None, 'pending': {}, 'issues': {},
                   'rows': [], 'attempts': {}, 'confirmations': {},
                   'state': 'waiting' if now.time() < opening.OPEN else 'running',
                   'message': 'Armed for 9:15 AM IST.' if now.time() < opening.OPEN else 'Checking live positions and orders.'}
            opening.save_job(_redis_eval, job, lease)
            return _opening_status(job)
    except LeaseBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post('/api/opening-stop/stop')
def stop_opening_stop(request: Request):
    _require_frontend_origin(request)
    _, account = _opening_account(request)
    if not _remote_session_store_enabled():
        return None
    try:
        with RedisLease(_redis_request, _redis_eval, f'autoinvest:account-lock:{account}') as lease:
            job = opening.get_job(_redis_request, account)
            if job and job['state'] in opening.ACTIVE:
                if job['queue'] != opening.QUEUE:
                    raise HTTPException(status_code=409, detail='This run belongs to another deployment.')
                job.update(state='stopped', message='Run stopped. Existing broker orders remain in Kite.')
                opening.save_job(_redis_eval, job, lease)
            return _opening_status(job)
    except LeaseBusy as exc:
        raise HTTPException(status_code=409, detail='A broker check is in progress. Retry Stop in a moment.') from exc


@app.get("/api/auto-trade/events")
def auto_trade_events(request: Request) -> dict[str, Any]:
    client = _current_kite(request)
    if client is None:
        raise HTTPException(status_code=401, detail="Please log in to Kite Connect first.")
    account_id = _account_id(client)
    if _remote_session_store_enabled():
        items = _redis_request("LRANGE", f"autoinvest:events:{account_id}") or []
        return {"events": [json.loads(item) for item in items]}
    return {"events": _local_trade_events.get(account_id, [])}


@app.get("/api/auto-trade/status")
def auto_trade_status(request: Request) -> dict[str, Any]:
    client = _current_kite(request)
    if client is None:
        raise HTTPException(status_code=401, detail="Please log in to Kite Connect first.")
    online = _trade_worker_online()
    remote = _remote_session_store_enabled()
    paused = bool(_redis_request("GET", f"autoinvest:paused:{_account_id(client)}")) if remote else False
    blockers = configuration_errors()
    if AUTO_TRADE_MODE != 'live':
        blockers.append('Live execution is not enabled on this server.')
    if not remote:
        blockers.append('Persistent order storage is not configured.')
    if not online:
        blockers.append('The order worker is offline or belongs to another deployment.')
    if paused:
        blockers.append('New entries are paused for this account.')
    return {"mode": AUTO_TRADE_MODE if AUTO_TRADE_MODE in {"paper", "live"} else "disabled", "worker_online": online, "paused": paused, "persistent": remote,
            'live_available': not blockers, 'blocking_reasons': blockers, 'policy': risk_policy()}


@app.get("/api/auto-trade/activity")
def trade_activity(request: Request):
    client = _current_kite(request)
    if client is None:
        raise HTTPException(status_code=401, detail="Please log in first.")
    account = _account_id(client)
    history = _redis_request('LRANGE', f'autoinvest:history:{account}') if _remote_session_store_enabled() else []
    return {**auto_trade_events(request), "monitors": _safe_monitors(account), 'history': [json.loads(row) for row in history or []]}


@app.post("/api/auto-trade/monitors/{monitor_id}/reconcile")
def reconcile_monitor(monitor_id: str, request: Request):
    """Read broker state; never place or cancel orders to resolve uncertainty."""
    _require_frontend_origin(request)
    client = _current_kite(request)
    if client is None:
        raise HTTPException(status_code=401, detail="Please log in first.")
    account = _account_id(client)
    if not _remote_session_store_enabled() or not re.fullmatch(r"[a-f0-9]{32}", monitor_id):
        raise HTTPException(status_code=404, detail="Managed setup not found.")
    try:
        with RedisLease(_redis_request, _redis_eval, f"autoinvest:account-lock:{account}") as lease:
            if monitor_id not in (_redis_request("SMEMBERS", f"autoinvest:active:{account}") or []):
                raise HTTPException(status_code=404, detail="Managed setup not found.")
            raw = _redis_request("GET", f"autoinvest:monitor:{monitor_id}")
            if not raw:
                raise HTTPException(status_code=409, detail="The safety record is missing. Operator investigation is required.")
            monitor = json.loads(raw)
            if monitor.get("account_id") != account:
                raise HTTPException(status_code=404, detail="Managed setup not found.")
            result = client.get_gtts()
            triggers = result.get("data", []) if isinstance(result, dict) else result
            relevant = [t for t in triggers if (t.get("condition") or {}).get("tradingsymbol") == monitor["symbol"]]
            matches = []
            for trigger in relevant:
                condition, orders = trigger.get("condition") or {}, trigger.get("orders") or []
                values = condition.get("trigger_values") or []
                if (trigger.get("status") == "active" and condition.get("exchange") == "NSE" and len(values) == 2
                    and abs(float(values[0]) - float(monitor["stop_loss"])) < .001
                    and abs(float(values[1]) - float(monitor["target"])) < .001 and len(orders) == 2
                    and int(monitor.get("filled_quantity", 0)) > 0
                    and all(o.get("transaction_type") == "SELL" and o.get("product") == "CNC" and int(o.get("quantity", 0)) == monitor["filled_quantity"] for o in orders)):
                    matches.append(trigger)
            positions = client.positions()["net"]
            holdings = client.holdings()
            pending = [o for o in client.orders() if o.get("tradingsymbol") == monitor["symbol"] and o.get("status") not in {"COMPLETE", "CANCELLED", "REJECTED"}]
            entry_matches = []
            for trigger in relevant:
                condition, orders = trigger.get('condition') or {}, trigger.get('orders') or []
                values = condition.get('trigger_values') or []
                if (trigger.get('status') == 'active' and condition.get('exchange') == 'NSE' and len(values) == 1
                    and abs(float(values[0]) - float(monitor['entry'])) < .001 and len(orders) == 1
                    and orders[0].get('transaction_type') == 'BUY' and orders[0].get('product') == 'CNC'
                    and int(orders[0].get('quantity', 0)) == int(monitor['quantity'])):
                    entry_matches.append(trigger)
            owned = int(monitor.get("filled_quantity", 0))
            quantity_verified = owned > 0 and (any(p.get("tradingsymbol") == monitor["symbol"] and p.get("exchange") == "NSE" and p.get("product") == "CNC" and int(p.get("quantity", 0)) >= owned for p in positions)
                or any(h.get("tradingsymbol") == monitor["symbol"] and h.get("exchange") == "NSE" and int(h.get("quantity", 0) or 0) + int(h.get("t1_quantity", 0) or 0) >= owned for h in holdings))
            if len(entry_matches) == 1 and not pending and not any(p.get('tradingsymbol') == monitor['symbol'] and p.get('quantity') for p in positions) and not any(h.get('tradingsymbol') == monitor['symbol'] and (h.get('quantity') or h.get('t1_quantity')) for h in holdings):
                if time.time() >= monitor.get('entry_expires_at', monitor.get('created_at', 0) + 1800):
                    raise HTTPException(status_code=409, detail='The matching entry has expired. Cancel it in Kite, then recheck.')
                monitor.update({'entry_gtt_id': str(entry_matches[0]['id']), 'stage': 'ENTRY_PENDING', 'retry_count': 0, 'reconcile_attempts': 0,
                                'last_verified_at': time.time(), 'next_check_at': 0})
                monitor.pop('last_error', None)
                lease.renew()
                save_monitor(_redis_eval, monitor)
                message = 'Kite confirms the active entry GTT and no existing exposure. Monitoring resumed.'
            elif len(matches) == 1 and not pending and quantity_verified:
                monitor["exit_gtt_id"] = str(matches[0]["id"])
                monitor["stage"] = "EXIT_PROTECTED"
                monitor.pop("last_error", None)
                monitor['last_verified_at'] = time.time()
                monitor['retry_count'] = 0
                monitor['reconcile_attempts'] = 0
                monitor.pop('next_check_at', None)
                lease.renew()
                save_monitor(_redis_eval, monitor)
                message = "Kite confirms one matching protective OCO GTT. Monitoring resumed."
            else:
                has_position = any(p.get("tradingsymbol") == monitor["symbol"] and int(p.get("quantity", 0)) != 0 for p in positions)
                has_holding = any(h.get("tradingsymbol") == monitor["symbol"] and int(h.get("quantity", 0) or 0) + int(h.get("t1_quantity", 0) or 0) > 0 for h in holdings)
                active_trigger = any(t.get("status") == "active" for t in relevant)
                if has_position or has_holding or pending or active_trigger or time.time() - monitor.get("created_at", time.time()) < 300:
                    raise HTTPException(status_code=409, detail="Kite still shows exposure, a pending order/trigger, or ambiguous protection. Resolve it in Kite, then recheck. No broker orders were changed.")
                lease.renew()
                archive_monitor(_redis_eval, monitor, 'Broker verification confirmed no remaining exposure')
                message = "Kite confirms no position, holding, pending order, or active GTT for this symbol. Monitoring closed."
            _emit_trade_event(account, f"{monitor['symbol']} reconciled", message, "success")
            return {"message": message}
    except LeaseBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/auto-trade/execute")
def execute_auto_trade(payload: TradeExecuteRequest, request: Request) -> dict[str, Any]:
    """Place entry GTTs only after a fresh scan and explicit confirmation."""
    _require_frontend_origin(request)
    client = _current_kite(request)
    if client is None:
        raise HTTPException(status_code=401, detail="Please log in to Kite Connect first.")
    account_id = _account_id(client)
    if AUTO_TRADE_MODE != "live":
        with _lock:
            return _execute_trade_locked(payload, request, client, account_id, None)
    if not _remote_session_store_enabled():
        raise HTTPException(status_code=503, detail="Live trading requires encrypted Redis storage.")
    try:
        with RedisLease(_redis_request, _redis_eval, f"autoinvest:account-lock:{account_id}") as lease:
            return _execute_trade_locked(payload, request, client, account_id, lease)
    except LeaseBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _execute_trade_locked(payload, request, client, account_id, lease):
    session_id = _session_id_from_cookie(request.cookies.get(_SESSION_COOKIE))
    mode = AUTO_TRADE_MODE if AUTO_TRADE_MODE in {"paper", "live"} else "disabled"
    if mode == "disabled":
        raise HTTPException(status_code=503, detail="Auto Trade is disabled by the backend configuration.")
    if mode == "live" and not _trade_worker_online():
        raise HTTPException(status_code=503, detail="Trade worker is offline. No orders were submitted.")
    if mode == 'live':
        policy = risk_policy()
        limits = [('risk_per_trade', 'TRADE_MAX_RISK_PER_TRADE'), ('max_daily_loss', 'TRADE_MAX_DAILY_LOSS'),
                  ('max_position_value', 'TRADE_MAX_POSITION_VALUE'), ('max_open_positions', 'TRADE_MAX_POSITIONS')]
        if any(getattr(payload.risk, field) > policy[limit] for field, limit in limits):
            raise HTTPException(status_code=422, detail='Requested risk limits exceed this server’s live execution policy.')
    if mode == "live" and _redis_request("GET", f"autoinvest:paused:{account_id}"):
        raise HTTPException(status_code=409, detail="New entries are paused for this account. Existing protection continues.")
    if _remote_session_store_enabled():
        encoded = _redis_request("GET", f"autoinvest:preview:{account_id}:{payload.preview_id}")
        preview = json.loads(encoded) if encoded else None
    else:
        preview = _local_previews.get(payload.preview_id)
    if not preview or preview.get("account_id") != account_id or time.time() - preview.get("created_at", 0) > 600:
        raise HTTPException(status_code=409, detail="Scan preview expired. Scan again before placing trades.")
    if preview.get("session_id") != session_id:
        raise HTTPException(status_code=403, detail="This preview belongs to a different browser session.")
    if mode == "live" and preview.get("asOf") != datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat():
        raise HTTPException(status_code=409, detail="The scan uses an older market snapshot. Refresh the scanner and scan again.")
    candidates = {row["symbol"]: row for row in preview["candidates"]}
    requested_symbols = list(dict.fromkeys(payload.symbols))
    if not requested_symbols or set(requested_symbols) - set(candidates):
        raise HTTPException(status_code=422, detail="Choose only symbols shown in this scan preview.")
    if len(requested_symbols) > payload.risk.max_open_positions:
        raise HTTPException(status_code=409, detail="Selection exceeds the configured position cap.")

    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    if mode == "live" and (now.weekday() >= 5 or not (9 * 60 + 15 <= now.hour * 60 + now.minute < 15 * 60 + 15)):
        _emit_trade_event(account_id, "Market is closed", "No orders were submitted. Run Auto Trade during NSE market hours.", "warning")
        raise HTTPException(status_code=409, detail="Market is closed. No orders were submitted.")

    try:
        realized_pnl = 0.0
        reserved_risk = 0.0
        existing_symbols: set[str] = set()
        pending_symbols: set[str] = set()
        active_ids = []
        available_cash = float("inf")
        reserved_cash = 0.0
        account_exposure = 0.0
        if mode == "live":
            positions = client.positions()
            day_positions = positions['day']
            net_positions = positions['net']
            if not isinstance(day_positions, list) or not isinstance(net_positions, list):
                raise ValueError('Invalid positions response')
            realized_pnl = sum(finite_number(position.get("pnl", 0) or 0) for position in day_positions)
            holdings = client.holdings()
            margins = client.margins("equity")
            available_cash = max(0.0, finite_number(margins.get("available", {}).get("live_balance"), "Available cash"))
            open_orders = client.orders()
            if not isinstance(holdings, list) or not isinstance(open_orders, list):
                raise ValueError('Invalid portfolio response')
            for item in holdings:
                quantity = abs(int(item.get('quantity', 0) or 0)) + abs(int(item.get('t1_quantity', 0) or 0))
                if quantity:
                    account_exposure += quantity * finite_number(item.get('last_price'), 'Holding price')
            for item in net_positions:
                quantity = abs(int(item.get('quantity', 0) or 0))
                if quantity:
                    account_exposure += quantity * finite_number(item.get('last_price'), 'Position price')
            active_ids = _redis_request("SMEMBERS", f"autoinvest:active:{account_id}") or []
            existing_symbols = {position.get("tradingsymbol") for position in net_positions if int(position.get("quantity", 0) or 0) != 0}
            existing_symbols |= {item.get("tradingsymbol") for item in holdings if int(item.get("quantity", 0) or 0) + int(item.get("t1_quantity", 0) or 0) > 0}
            existing_symbols |= {item.get("tradingsymbol") for item in open_orders if item.get("status") not in {"COMPLETE", "CANCELLED", "REJECTED"}}
            for monitor_id in active_ids:
                saved_monitor = _redis_request("GET", f"autoinvest:monitor:{monitor_id}")
                if saved_monitor:
                    monitor = json.loads(saved_monitor)
                    if monitor.get("stage") not in {"ENTRY_PENDING", "EXIT_PROTECTED"} or monitor.get("last_error"):
                        raise HTTPException(status_code=409, detail="An existing trade needs review. New entries are blocked until its state is verified.")
                    if time.time() - monitor.get('last_verified_at', 0) > 120:
                        raise HTTPException(status_code=409, detail='An existing order has not been verified recently. New entries are blocked until the worker verifies it.')
                    reserved_risk += finite_number(monitor.get("risk", 0) or 0)
                    if monitor.get("stage") == "ENTRY_PENDING":
                        pending_symbols.add(monitor.get("symbol"))
                        reserved_cash += finite_number(monitor.get("entry_limit", monitor["entry"])) * int(monitor["quantity"])
                else:
                    raise HTTPException(status_code=409, detail="An active trade record is missing. New entries are blocked until it is reconciled.")
            gtts_response = client.get_gtts()
            gtts = gtts_response.get("data", []) if isinstance(gtts_response, dict) else gtts_response
            if not isinstance(gtts, list):
                raise ValueError('Invalid GTT response')
            for trigger in gtts or []:
                if trigger.get("status") == "active" and any(order.get("transaction_type") == "BUY" for order in trigger.get("orders", [])):
                    condition = trigger.get("condition") or {}
                    if condition.get("tradingsymbol") not in pending_symbols:
                        raise HTTPException(status_code=409, detail="An external buy GTT is active in Kite. Its cash and risk are unmanaged here; resolve it before adding Auto Trade entries.")
            if realized_pnl <= -payload.risk.max_daily_loss:
                raise HTTPException(status_code=409, detail="Daily loss cap reached. New trades are blocked.")
            if len(existing_symbols | pending_symbols | set(requested_symbols)) > payload.risk.max_open_positions:
                raise HTTPException(status_code=409, detail="Selected trades exceed the maximum open positions setting.")
        quote_data = client.quote([f"NSE:{symbol}" for symbol in requested_symbols])
    except HTTPException:
        raise
    except Exception as exc:
        _emit_trade_event(account_id, "Trade preflight failed", "Could not verify current positions and prices; no orders were submitted.", "error")
        raise HTTPException(status_code=502, detail="Could not verify account positions and live prices. No orders were submitted.") from exc

    planned_risk = 0.0
    plans = []
    skipped_results = []
    trade_day = now.date().isoformat()
    for symbol in requested_symbols:
        row = candidates[symbol]
        if mode == "live" and (symbol in existing_symbols or symbol in pending_symbols):
            skipped_results.append({"symbol": symbol, "status": "skipped", "message": "An existing position or active order already uses this symbol."})
            _emit_trade_event(account_id, f"{symbol} skipped", "The account already has a position or active Auto Trade order for this symbol.", "warning")
            continue
        entry, stop, target = float(row["entry_price"]), float(row["stop_loss"]), float(row["target_price"])
        quote_row = quote_data.get(f"NSE:{symbol}", {})
        ltp = finite_number(quote_row.get("last_price") or 0, "Live price")
        if mode == "live":
            try:
                last_trade = quote_row.get("last_trade_time")
                last_trade = last_trade if isinstance(last_trade, datetime) else datetime.fromisoformat(str(last_trade))
                if last_trade.tzinfo is None:
                    last_trade = last_trade.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
                age = (now - last_trade).total_seconds()
                if not 0 <= age <= 300:
                    raise ValueError("Stale quote")
            except (ValueError, TypeError):
                skipped_results.append({"symbol": symbol, "status": "skipped", "message": "A verified trade price from the last 5 minutes is required for live entry."})
                continue
        if ltp <= 0 or entry <= ltp:
            skipped_results.append({"symbol": symbol, "status": "skipped", "message": "Entry is no longer above the live price, or the live quote is unavailable."})
            _emit_trade_event(account_id, f"{symbol} skipped", "The breakout entry is no longer above the live price or a live quote was unavailable. Run a new scan.", "warning")
            continue
        max_entry = float(row.get("entry_limit_price") or _price_to_tick(entry * 1.005, row.get("tick_size", 0.05), upward=True))
        worst_stop_fill = float(row.get("stop_limit_price") or _price_to_tick(stop * 0.995, row.get("tick_size", 0.05), upward=False))
        quantity, order_risk = position_size(payload.risk.risk_per_trade, payload.risk.max_position_value, max_entry, worst_stop_fill)
        if quantity < 1:
            skipped_results.append({"symbol": symbol, "status": "skipped", "message": "Risk and position caps do not allow one share."})
            _emit_trade_event(account_id, f"{symbol} skipped", "Risk and position caps do not allow even one share at this stop distance.", "warning")
            continue
        if planned_risk + reserved_risk + order_risk + max(0.0, -realized_pnl) > payload.risk.max_daily_loss:
            skipped_results.append({"symbol": symbol, "status": "skipped", "message": "Insufficient daily risk budget after active setups and daily losses."})
            _emit_trade_event(account_id, f"{symbol} skipped", "The selected orders would exceed the remaining daily loss budget.", "warning")
            continue
        # Cash buffer covers charges; GTT does not reserve funds at the broker.
        cash_required = quantity * max_entry * 1.01
        if reserved_cash + cash_required > available_cash:
            skipped_results.append({"symbol": symbol, "status": "skipped", "message": "Insufficient available cash after pending orders and a 1% charge buffer."})
            _emit_trade_event(account_id, f"{symbol} skipped", "Insufficient available cash after pending entries and a 1% charge buffer.", "warning")
            continue
        if mode == 'live' and account_exposure + reserved_cash + cash_required > policy['TRADE_MAX_ACCOUNT_EXPOSURE']:
            skipped_results.append({'symbol': symbol, 'status': 'skipped', 'message': 'The account exposure limit would be exceeded.'})
            continue
        reserved_cash += cash_required
        planned_risk += order_risk
        plans.append((row, ltp, quantity, order_risk, trade_day, max_entry))

    if mode == "paper":
        results = [{"symbol": row["symbol"], "status": "paper", "quantity": quantity, "risk": round(order_risk, 2), "message": f"Simulated {quantity} share(s); estimated risk ₹{order_risk:.2f}. No Kite order was sent."} for row, _, quantity, order_risk, _, _ in plans]
        _emit_trade_event(account_id, "Paper trade simulation complete", f"Estimated {len(results)} position(s); no live broker orders were placed.", "info")
        return {"mode": "paper", "results": results + skipped_results, "risk_reserved": round(planned_risk, 2)}

    results = skipped_results
    for row, ltp, quantity, order_risk, trade_day, max_entry in plans:
        symbol = row["symbol"]
        lease.renew()
        duplicate_key = f"autoinvest:bought:{account_id}:{trade_day}:{symbol}"
        monitor = {"id": uuid.uuid4().hex, "account_id": account_id, "session_id": session_id,
                   "api_key": client.api_key, "strategy_id": preview["strategy"]["id"], "symbol": symbol, "quantity": quantity,
                   "entry": row["entry_price"], "stop_loss": row["stop_loss"], "target": row["target_price"],
                   "tick_size": row.get("tick_size", 0.05), "entry_limit": max_entry,
                   "risk": order_risk, "entry_gtt_id": None, "stage": "SUBMITTING_ENTRY", "created_at": time.time(), 'last_verified_at': time.time(),
                   'entry_expires_at': min(time.time() + 1800, now.replace(hour=15, minute=15, second=0, microsecond=0).timestamp()), 'deployment': deployment_fingerprint(),
                   'owner': os.getenv('DEPLOYMENT_ID', 'default')}
        broker_call_started = False
        try:
            lease.renew()
            if not create_intent(_redis_eval, monitor, duplicate_key):
                results.append({'symbol': symbol, 'status': 'skipped', 'message': 'This account already reserved or submitted an entry for this stock today.'})
                continue
            lease.renew()
            broker_call_started = True
            trigger = client.place_gtt(
                client.GTT_TYPE_SINGLE, symbol, "NSE", [_price_to_tick(row["entry_price"], row.get("tick_size", 0.05), upward=True)], ltp,
                [{"transaction_type": "BUY", "quantity": quantity, "order_type": "LIMIT", "product": "CNC", "price": max_entry}],
            ).get("trigger_id")
            if not trigger:
                raise RuntimeError("Kite did not confirm the entry GTT trigger ID.")
            monitor["entry_gtt_id"] = str(trigger)
            monitor["stage"] = "ENTRY_PENDING"
            lease.renew()
            save_monitor(_redis_eval, monitor)
            results.append({"symbol": symbol, "status": "submitted", "quantity": quantity, "entry_gtt_id": str(trigger), "risk": round(order_risk, 2)})
            _emit_trade_event(account_id, f"{symbol} entry GTT submitted", f"Buy GTT is waiting at ₹{row['entry_price']:.2f} for {quantity} CNC share(s). Protective OCO GTT will be added after fill confirmation.", "success", {"symbol": symbol, "trigger_id": str(trigger)})
        except Exception as exc:
            if not broker_call_started:
                # Storage calls can time out after committing. Keep reservations
                # and intents for reconciliation instead of deleting uncertain state.
                results.append({"symbol": symbol, "status": "error", "message": "Order preparation could not be verified. No broker request was made; check Activity before retrying."})
                _emit_trade_event(account_id, f"{symbol} order setup failed", "Could not save the order safety record; no broker order was submitted.", "error")
                continue
            # Keep both the reservation and intent when the broker outcome is
            # uncertain. The worker reconciles it against Kite's GTT book.
            # The durable SUBMITTING_ENTRY intent survives the request. Never
            # overwrite it from a stale lease after a worker may have advanced it.
            results.append({"symbol": symbol, "status": "checking", 'risk': round(order_risk, 2), 'quantity': quantity, "message": "Order outcome is being reconciled with Kite before retrying."})
            _emit_trade_event(account_id, f"{symbol} order status checking", f"Kite submission result is uncertain ({type(exc).__name__}); duplicate orders are blocked while the worker checks the GTT book.", "warning")
    return {"mode": "live", "results": results, "risk_reserved": round(sum(row.get('risk', 0) for row in results if row['status'] in {'submitted', 'checking'}), 2)}


NIFTY_100_CSV = "https://www.niftyindices.com/IndexConstituent/ind_nifty100list.csv"
NIFTY_50_CSV = "https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv"


def _nifty_constituents() -> list[dict[str, str]]:
    """Read the live constituent list published by NSE Indices."""
    try:
        response = requests.get(
            NIFTY_100_CSV,
            headers={"User-Agent": "Mozilla/5.0 (compatible; KiteConsole/1.0)"},
            timeout=20,
        )
        response.raise_for_status()
        reader = csv.DictReader(io.StringIO(response.content.decode("utf-8-sig")))
        rows = []
        for row in reader:
            normalized = {str(key or "").strip().lower(): str(value or "").strip() for key, value in row.items()}
            symbol = normalized.get("symbol")
            company = normalized.get("company name") or normalized.get("companyname") or normalized.get("name")
            if symbol:
                rows.append({"symbol": symbol, "company": company or symbol})
        if not rows:
            raise ValueError("The official constituent CSV contained no symbols.")
        return rows
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Could not load the Nifty 100 constituent CSV from Nifty Indices.") from exc


def _read_index_csv(url: str, display_name: str) -> list[dict[str, str]]:
    try:
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; KiteConsole/1.0)"}, timeout=20)
        response.raise_for_status()
        reader = csv.DictReader(io.StringIO(response.content.decode("utf-8-sig")))
        result = []
        for row in reader:
            normalized = {str(key or "").strip().lower(): str(value or "").strip() for key, value in row.items()}
            symbol = normalized.get("symbol")
            company = normalized.get("company name") or normalized.get("companyname") or normalized.get("name")
            if symbol:
                result.append({"symbol": symbol, "company": company or symbol})
        if not result:
            raise ValueError("No constituents found")
        return result
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not load the official {display_name} constituent CSV.") from exc


def _get_nse_instruments(client: KiteConnect) -> list[dict[str, Any]]:
    global _instruments
    if _instruments is None:
        try:
            _instruments = client.instruments("NSE")
        except Exception as exc:
            raise HTTPException(status_code=502, detail="Could not load NSE instruments from Kite Connect.") from exc
    return _instruments


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)).date()


def _price_to_tick(value: float, tick_size: float, *, upward: bool) -> float:
    """Round an NSE price to a valid instrument tick without binary-float drift."""
    tick = Decimal(str(tick_size))
    price = Decimal(str(value))
    if not tick.is_finite() or tick <= 0 or not price.is_finite() or price <= 0:
        raise ValueError("Price and instrument tick size must be finite and positive.")
    units = (price / tick).to_integral_value(rounding=ROUND_CEILING if upward else ROUND_FLOOR)
    return float(units * tick)


@app.get("/api/scanner-data")
def scanner_data(request: Request) -> dict[str, Any]:
    """Build and cache the daily candles and instrument metadata used by Algo Signals."""
    global _scanner_cache, _scanner_cache_at, _scanner_cache_account
    client = _current_kite(request)
    if client is None:
        raise HTTPException(status_code=401, detail="Please log in to Kite Connect first.")
    account_id = _account_id(client)
    with _scanner_slot():
        if _scanner_cache and _scanner_cache_account == account_id and _scanner_cache["metadata"]["asOf"] == datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat() and time.monotonic() - _scanner_cache_at < 900:
            return _scanner_cache

        nifty100 = _read_index_csv(NIFTY_100_CSV, "Nifty 100")
        nifty50_symbols = {item["symbol"] for item in _read_index_csv(NIFTY_50_CSV, "Nifty 50")}
        instruments = _get_nse_instruments(client)
        token_by_symbol = {
            item.get("tradingsymbol"): item
            for item in instruments
            if item.get("exchange") == "NSE" and item.get("tradingsymbol") and item.get("instrument_token")
        }
        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        start = today - timedelta(days=150)
        stocks: list[dict[str, Any]] = []
        missing: list[str] = []
        for member in nifty100:
            instrument = token_by_symbol.get(member["symbol"])
            if not instrument:
                missing.append(member["symbol"])
                continue
            try:
                history = client.historical_data(instrument["instrument_token"], start, today, "day", continuous=False, oi=False)
                candles = [{
                    "date": _as_date(candle["date"]).isoformat(),
                    "open": float(candle["open"]),
                    "high": float(candle["high"]),
                    "low": float(candle["low"]),
                    "close": float(candle["close"]),
                    "volume": float(candle["volume"]),
                } for candle in history]
                candles.sort(key=lambda candle: candle["date"])
                if any(not math.isfinite(c[field]) or c[field] < 0 for c in candles for field in ("open", "high", "low", "close", "volume")):
                    raise ValueError("Historical candles contain invalid values.")
                if len({c["date"] for c in candles}) != len(candles) or any(not 0 < c["low"] <= min(c["open"], c["close"]) <= max(c["open"], c["close"]) <= c["high"] for c in candles):
                    raise ValueError("Historical candle OHLC values or dates are inconsistent.")
                if not candles:
                    missing.append(member["symbol"])
                    continue
                stocks.append({
                    "symbol": member["symbol"],
                    "companyName": member["company"],
                    "instrument_token": int(instrument["instrument_token"]),
                    "tick_size": float(instrument.get("tick_size") or 0.05),
                    "nifty50": member["symbol"] in nifty50_symbols,
                    "nifty100": True,
                    "niftyFlags": (["Nifty 50"] if member["symbol"] in nifty50_symbols else []) + ["Nifty 100"],
                    "candles": candles,
                })
            except Exception:
                missing.append(member["symbol"])
            time.sleep(0.35)

        if not stocks:
            raise HTTPException(status_code=502, detail="Kite did not return daily candles for the Nifty 100 universe.")
        _scanner_cache = {
            "metadata": {
                "index": "Nifty 100",
                "source": NIFTY_100_CSV,
                "asOf": today.isoformat(),
                "cachedAt": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(timespec="seconds"),
                "stockCount": len(stocks),
                "missingSymbols": missing,
            },
            "stocks": stocks,
        }
        _scanner_cache_at = time.monotonic()
        _scanner_cache_account = account_id
        return _scanner_cache


@app.get("/api/scanner-ohlc")
def scanner_ohlc(request: Request, tokens: str = Query(min_length=1, max_length=5000)) -> dict[str, Any]:
    client = _current_kite(request)
    if client is None:
        raise HTTPException(status_code=401, detail="Please log in to Kite Connect first.")
    requested = {int(value) for value in tokens.split(",") if value.strip().isdigit()}
    instruments = _get_nse_instruments(client)
    symbols = [f"NSE:{item['tradingsymbol']}" for item in instruments if item.get("instrument_token") in requested]
    try:
        quotes = client.quote(symbols) if symbols else {}
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Could not load the Kite OHLC price snapshot.") from exc
    return {"prices": {str(value.get("instrument_token")): value.get("last_price") for value in quotes.values() if value.get("instrument_token") is not None and value.get("last_price") is not None}}


_live_connections: dict[str, int] = {}


async def _stream_ticks(websocket, client, token_list, mode):
    # TLS certificate/hostname verification uses the maintained websocket client.
    # Parse bounded Kite binary frames without the legacy SDK websocket transport.
    from websockets.asyncio.client import connect
    endpoint = "wss://ws.kite.trade?api_key=" + quote(client.api_key, safe="") + "&access_token=" + quote(client.access_token, safe="")
    for attempt in range(4):
        try:
            async with connect(endpoint, open_timeout=10, close_timeout=3, max_size=262144, max_queue=16, ping_interval=20) as broker:
                await broker.send(json.dumps({"a": "subscribe", "v": token_list}))
                await broker.send(json.dumps({"a": "mode", "v": [mode, token_list]}))
                await websocket.send_json({"type": "status", "status": "connected"})
                async for payload in broker:
                    if isinstance(payload, bytes) and len(payload) > 1:
                        ticks = parse_ticks(payload, set(token_list))
                        await websocket.send_json({"type": "ticks", "ticks": [{"instrument_token": item["instrument_token"], "last_price": item["last_price"]} for item in ticks if item.get("instrument_token") in token_list]})
                    elif isinstance(payload, str):
                        message = json.loads(payload)
                        if message.get("type") == "error":
                            await websocket.send_json({"type": "status", "status": "Kite rejected the subscription; check your session"})
                            return
        except asyncio.CancelledError:
            raise
        except Exception:
            # Exception text can contain the credential-bearing upstream URL.
            await websocket.send_json({"type": "status", "status": "reconnecting" if attempt < 3 else "disconnected; refresh your Kite session"})
        if attempt < 3:
            await asyncio.sleep(min(2 ** attempt, 8))


@app.websocket("/ws/ticks")
async def live_ticks(websocket: WebSocket, tokens: str = Query(min_length=1, max_length=1500), mode: str = "full") -> None:
    if not _origin_allowed(websocket.headers.get("origin")):
        await websocket.close(code=4403)
        return
    if not (_SESSION_REDIS_URL or _SESSION_REDIS_TOKEN) and (not websocket.client or websocket.client.host not in {"127.0.0.1", "::1", "localhost"}):
        await websocket.close(code=4403)
        return
    client = await run_in_threadpool(_current_kite, websocket)
    if client is None:
        await websocket.close(code=4401)
        return
    parts = tokens.split(",")
    if mode not in {"full", "ltp"} or len(parts) > 100 or not all(re.fullmatch(r"[0-9]{1,12}", value) for value in parts):
        await websocket.close(code=4400)
        return
    token_list = sorted({int(value) for value in parts})
    if any(value <= 0 for value in token_list):
        await websocket.close(code=4400)
        return
    account = await run_in_threadpool(_account_id, client)
    if _live_connections.get(account, 0) >= 2:
        await websocket.close(code=4429)
        return
    _live_connections[account] = _live_connections.get(account, 0) + 1
    receiver = None
    bridge = None
    try:
        await websocket.accept()
        await websocket.send_json({"type": "status", "status": "connecting"})
        receiver = asyncio.create_task(websocket.receive())
        bridge = asyncio.create_task(_stream_ticks(websocket, client, token_list, mode))
        await asyncio.wait({receiver, bridge}, return_when=asyncio.FIRST_COMPLETED)
    except (WebSocketDisconnect, RuntimeError, OSError):
        pass
    finally:
        tasks = [task for task in (receiver, bridge) if task]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        _live_connections[account] = max(0, _live_connections.get(account, 1) - 1)
        if not _live_connections[account]:
            _live_connections.pop(account, None)
        try:
            await websocket.close()
        except RuntimeError:
            pass


@app.post("/api/signals")
def generate_signals(payload: SignalRequest, request: Request) -> dict[str, Any]:
    """Scan the current Nifty 100 universe for the latest daily SMA crossovers."""
    client = _current_kite(request)
    if client is None:
        raise HTTPException(status_code=401, detail="Please log in to Kite Connect first.")

    constituents = _nifty_constituents()
    instruments = _get_nse_instruments(client)
    token_by_symbol = {
        item.get("tradingsymbol"): item.get("instrument_token")
        for item in instruments
        if item.get("exchange") == "NSE" and item.get("tradingsymbol") and item.get("instrument_token")
    }
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    from_date = today - timedelta(days=payload.lookback_days + payload.long_sma * 2 + 14)
    earliest_signal = today - timedelta(days=payload.lookback_days)
    signals: list[dict[str, Any]] = []
    scanned = 0
    skipped = 0

    for stock in constituents:
        token = token_by_symbol.get(stock["symbol"])
        if not token:
            skipped += 1
            continue
        try:
            candles = client.historical_data(token, from_date, today, "day", continuous=False, oi=False)
            # Ignore today's incomplete daily candle so signals are based on closed sessions.
            candles = [c for c in candles if _as_date(c["date"]) < today]
            scanned += 1
        except Exception:
            skipped += 1
            time.sleep(0.35)
            continue

        closes = [float(c["close"]) for c in candles]
        latest_signal = None
        for index in range(payload.long_sma, len(candles)):
            signal_date = _as_date(candles[index]["date"])
            if signal_date < earliest_signal:
                continue
            short_now = sum(closes[index - payload.short_sma + 1:index + 1]) / payload.short_sma
            long_now = sum(closes[index - payload.long_sma + 1:index + 1]) / payload.long_sma
            short_before = sum(closes[index - payload.short_sma:index]) / payload.short_sma
            long_before = sum(closes[index - payload.long_sma:index]) / payload.long_sma
            direction = None
            if short_before <= long_before and short_now > long_now:
                direction = "Bullish"
            elif short_before >= long_before and short_now < long_now:
                direction = "Bearish"
            if direction:
                latest_signal = {
                    "ticker": stock["symbol"],
                    "company": stock["company"],
                    "crossover_type": direction,
                    "crossover_date": signal_date.isoformat(),
                    "close": round(closes[index], 2),
                    "short_sma": round(short_now, 2),
                    "long_sma": round(long_now, 2),
                    "_date": signal_date,
                }
        if latest_signal:
            signals.append(latest_signal)
        # Kite API permits only a few historical requests per second.
        time.sleep(0.35)

    signals.sort(key=lambda item: (item["_date"], item["ticker"]), reverse=True)
    results = [{key: value for key, value in item.items() if key != "_date"} for item in signals[:payload.max_stocks]]
    return {
        "results": results,
        "total_signals": len(signals),
        "scanned": scanned,
        "skipped": skipped,
        "as_of": today.isoformat(),
        "short_sma": payload.short_sma,
        "long_sma": payload.long_sma,
        "lookback_days": payload.lookback_days,
    }
