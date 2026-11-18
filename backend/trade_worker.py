"""Persistent, restart-safe monitor for AutoInvest entry and protective GTT orders."""

import json
import logging
import time
import uuid
import signal
import os
from contextvars import ContextVar
from threading import Event
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from datetime import datetime
from zoneinfo import ZoneInfo

from .vendor.kite_rest import KiteConnect

from .main import _read_saved_session, _redis_request, _redis_eval, _worker_session_key
from .guardrails import RedisLease, LeaseBusy, finite_number
from .guardrails import LeaseLost
from .live_config import validate_live_settings, heartbeat_key
from .order_state import save_monitor, archive_monitor, StateConflict

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("autoinvest.trade-worker")
_lease = ContextVar('worker_lease', default=None)
_shutdown = Event()


class BrokerStateChanged(RuntimeError):
    """The broker object no longer matches the persisted order intent."""


def _price_to_tick(value: float, tick_size: float, *, upward: bool) -> float:
    tick = Decimal(str(tick_size))
    price = Decimal(str(value))
    if not tick.is_finite() or tick <= 0 or not price.is_finite() or price <= 0:
        raise ValueError("Price and tick size must be finite and positive")
    units = (price / tick).to_integral_value(rounding=ROUND_CEILING if upward else ROUND_FLOOR)
    return float(units * tick)


def _event(account_id: str, title: str, message: str, level: str = "info", details: dict | None = None) -> None:
    item = {
        "id": uuid.uuid4().hex,
        "at": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(timespec="seconds"),
        "title": title[:100], "message": message[:300], "level": level, "details": details or {},
    }
    key = f"autoinvest:events:{account_id}"
    try:
        _redis_request("LPUSH", key, json.dumps(item))
        _redis_request("LTRIM", key)
    except Exception:
        log.error("Notification persistence failed for an order event")


def _load_monitor(monitor_id: str) -> dict | None:
    value = _redis_request("GET", f"autoinvest:monitor:{monitor_id}")
    return json.loads(value) if value else None


def _save_monitor(monitor: dict) -> None:
    active_lease = _lease.get()
    if active_lease:
        active_lease.renew()
    save_monitor(_redis_eval, monitor)


def _kite_for(monitor: dict) -> KiteConnect:
    refreshed_session = _redis_request("GET", _worker_session_key(monitor["account_id"], monitor["api_key"]))
    saved = _read_saved_session(refreshed_session or monitor.get("session_id"))
    if not saved:
        raise RuntimeError("Session expired or was signed out")
    if saved["api_key"] != monitor["api_key"]:
        raise RuntimeError("Worker API key mismatch")
    client = KiteConnect(api_key=saved["api_key"])
    client.set_access_token(saved["access_token"])
    try:
        if client.profile().get("user_id") != monitor["account_id"]:
            raise RuntimeError("Worker session account mismatch")
    except Exception:
        client.reqsession.close()
        raise
    return client


def _gtt(client: KiteConnect, trigger_id: str, book=None) -> dict:
    response = book.get(str(trigger_id)) if book is not None else None
    if response is None:
        response = client.get_gtt(int(trigger_id))
    result = response.get('data', response) if isinstance(response, dict) else None
    if not isinstance(result, dict) or not isinstance(result.get('orders'), list) or result.get('status') not in {'active', 'triggered', 'cancelled', 'expired', 'rejected', 'deleted', 'disabled'}:
        raise RuntimeError('Unverifiable broker GTT response')
    return result


def _entry_order(gtt: dict) -> tuple[str | None, dict]:
    failure = {}
    for item in gtt.get("orders", []):
        result = item.get("result") or {}
        nested = result.get("order_result") or {}
        order_id = result.get("order_id") or nested.get("order_id")
        if order_id:
            return str(order_id), nested or result
        if nested or result:
            failure = nested or result
    return None, failure


def _verify_gtt(monitor, gtt, *, exit_order=False):
    condition, orders = gtt.get('condition') or {}, gtt.get('orders') or []
    tick = float(monitor.get('tick_size', .05))
    if exit_order:
        values = [_price_to_tick(monitor['stop_loss'], tick, upward=False), _price_to_tick(monitor['target'], tick, upward=True)]
        prices = [_price_to_tick(monitor['stop_loss'] * .995, tick, upward=False), values[1]]
        quantity, side = int(monitor['filled_quantity']), 'SELL'
    else:
        values = [_price_to_tick(monitor['entry'], tick, upward=True)]
        prices = [float(monitor.get('entry_limit') or _price_to_tick(monitor['entry'] * 1.005, tick, upward=True))]
        quantity, side = int(monitor['quantity']), 'BUY'
    actual = condition.get('trigger_values') or []
    if (condition.get('exchange') != 'NSE' or condition.get('tradingsymbol') != monitor['symbol']
        or len(actual) != len(values) or len(orders) != len(values)
        or any(abs(finite_number(a) - b) > .000001 for a, b in zip(actual, values))
        or any(o.get('transaction_type') != side or o.get('product') != 'CNC' or o.get('order_type') != 'LIMIT'
               or int(o.get('quantity', 0)) != quantity or abs(finite_number(o.get('price', 0)) - p) > .000001
               for o, p in zip(orders, prices))):
        raise BrokerStateChanged('Broker GTT differs from the saved intent')


def _verify_fill(monitor, order, side):
    expected = int(monitor['quantity'] if side == 'BUY' else monitor['filled_quantity'])
    if (order.get('tradingsymbol') != monitor['symbol'] or order.get('exchange') != 'NSE'
        or order.get('product') != 'CNC' or order.get('transaction_type') != side
        or int(order.get('quantity', 0)) != expected or int(order.get('filled_quantity', 0) or 0) > expected):
        raise BrokerStateChanged('Broker order differs from the saved intent')


def _order_now(client: KiteConnect, order_id: str) -> dict:
    history = client.order_history(order_id)
    if not history:
        raise RuntimeError('Broker order history is unavailable')
    order = history[-1]
    if not isinstance(order, dict) or not isinstance(order.get('status'), str):
        raise RuntimeError('Invalid broker order status')
    filled = int(order.get('filled_quantity', 0) or 0)
    if filled < 0 or (order['status'] == 'COMPLETE' and filled == 0):
        raise RuntimeError('Invalid broker fill quantity')
    return order


def _remove_monitor(monitor: dict) -> None:
    active_lease = _lease.get()
    if active_lease:
        active_lease.renew()
    archive_monitor(_redis_eval, monitor, monitor.get('close_reason', 'Monitoring complete'))


def _record_fill_day(monitor, order):
    timestamp = order.get('exchange_update_timestamp') or order.get('order_timestamp')
    try:
        when = timestamp if isinstance(timestamp, datetime) else datetime.fromisoformat(str(timestamp))
        if when.tzinfo is None:
            when = when.replace(tzinfo=ZoneInfo('Asia/Kolkata'))
        fill_day = when.astimezone(ZoneInfo('Asia/Kolkata')).date().isoformat()
    except (TypeError, ValueError):
        fill_day = monitor.get('fill_day') or datetime.now(ZoneInfo('Asia/Kolkata')).date().isoformat()
    _redis_request('SET', f"autoinvest:bought:{monitor['account_id']}:{fill_day}:{monitor['symbol']}", monitor['id'], ttl=45 * 86400, nx=True)
    monitor['fill_day'] = fill_day


def _defer_reconciliation(monitor, waiting_stage):
    attempts = int(monitor.get('reconcile_attempts', 0)) + 1
    monitor['reconcile_attempts'] = attempts
    monitor['next_check_at'] = time.time() + min(300, 10 * 2 ** min(attempts, 5))
    monitor['stage'] = waiting_stage if attempts < 6 else 'MANUAL_REVIEW'
    if attempts >= 6:
        monitor['resume_stage'] = waiting_stage
    _save_monitor(monitor)


def _place_exit_gtt(client: KiteConnect, monitor: dict, quantity: int, ltp: float, lease) -> str:
    stop = float(monitor["stop_loss"])
    target = float(monitor["target"])
    tick = float(monitor.get("tick_size", 0.05))
    if not stop < ltp < target:
        raise ValueError("Live price is outside the stop-loss/target range; protective GTT needs manual review")
    # Persist intent BEFORE the broker call. A timeout or process crash must not
    # cause a second protective sell GTT to be blindly submitted on restart.
    monitor["stage"] = "SUBMITTING_EXIT"
    monitor["filled_quantity"] = quantity
    monitor["exit_submitted_at"] = time.time()
    _save_monitor(monitor)
    lease.renew()
    result = client.place_gtt(
        client.GTT_TYPE_OCO,
        monitor["symbol"],
        "NSE",
        [_price_to_tick(stop, tick, upward=False), _price_to_tick(target, tick, upward=True)],
        ltp,
        [
            {"transaction_type": "SELL", "quantity": quantity, "order_type": "LIMIT", "product": "CNC", "price": _price_to_tick(stop * 0.995, tick, upward=False)},
            {"transaction_type": "SELL", "quantity": quantity, "order_type": "LIMIT", "product": "CNC", "price": _price_to_tick(target, tick, upward=True)},
        ],
    )
    trigger_id = result.get("trigger_id") if isinstance(result, dict) else None
    if not trigger_id:
        raise RuntimeError("Kite did not confirm the protective GTT trigger ID")
    return str(trigger_id)


def _finish_entry(client: KiteConnect, monitor: dict, order: dict, lease) -> None:
    filled = int(order.get("filled_quantity", 0) or 0)
    if filled <= 0:
        if order.get("status") in {"CANCELLED", "REJECTED"}:
            monitor['close_reason'] = f"Entry order {order['status'].lower()} without fills"
            _event(monitor["account_id"], f"{monitor['symbol']} entry did not fill", order.get("status_message") or "The entry order ended without a fill.", "warning")
            _remove_monitor(monitor)
        return
    if order.get("status") not in {"COMPLETE", "CANCELLED", "REJECTED"}:
        if monitor.get("stage") != "CANCEL_ENTRY_REMAINDER":
            lease.renew()
            client.cancel_order("regular", order["order_id"])
            monitor["stage"] = "CANCEL_ENTRY_REMAINDER"
            monitor["entry_order_id"] = str(order["order_id"])
            _save_monitor(monitor)
        return

    if filled > int(monitor.get('quantity', filled)):
        raise RuntimeError('Broker fill exceeds reserved quantity')
    _record_fill_day(monitor, order)
    monitor.update({'stage': 'ENTRY_FILLED', 'filled_quantity': filled})
    _save_monitor(monitor)
    quote = client.ltp([f"NSE:{monitor['symbol']}"])
    ltp = float(quote[f"NSE:{monitor['symbol']}"]["last_price"])
    try:
        exit_id = _place_exit_gtt(client, monitor, filled, ltp, lease)
    except Exception as exc:
        monitor["stage"] = "EXIT_UNKNOWN" if monitor.get("stage") == "SUBMITTING_EXIT" else "UNPROTECTED_POSITION"
        monitor["filled_quantity"] = filled
        monitor["protection_error"] = type(exc).__name__
        _save_monitor(monitor)
        _event(monitor["account_id"], f"URGENT: {monitor['symbol']} needs protection", f"{filled} share(s) filled, but the stop-loss GTT failed: {monitor['protection_error']}. Check Kite immediately.", "error", {"symbol": monitor["symbol"]})
        return
    monitor["stage"] = "EXIT_PROTECTED"
    monitor["filled_quantity"] = filled
    monitor["exit_gtt_id"] = exit_id
    _save_monitor(monitor)
    _event(monitor["account_id"], f"{monitor['symbol']} position protected", f"{filled} filled CNC share(s) now have a stop-loss/target OCO GTT.", "success", {"symbol": monitor["symbol"], "trigger_id": exit_id})


def _process_monitor(monitor_id: str, lease, client=None, book=None, bootstrap_error=None) -> None:
    _redis_request("SET", heartbeat_key(), str(time.time()), ttl=90)
    monitor = _load_monitor(monitor_id)
    if not monitor:
        return
    if monitor.get('owner') not in {None, os.getenv('DEPLOYMENT_ID', 'default')}:
        return
    if monitor.get('next_check_at', 0) > time.time() or monitor['stage'] == 'MANUAL_REVIEW':
        return
    token = _lease.set(lease)
    failed = False
    try:
        lease.renew()
        if monitor['stage'] in {'PROTECTION_LOST', 'UNPROTECTED_POSITION'}:
            monitor['next_check_at'] = time.time() + 300
            _save_monitor(monitor)
            if time.time() - monitor.get('last_notified_at', 0) > 300:
                monitor['last_notified_at'] = time.time()
                _save_monitor(monitor)
                _event(monitor['account_id'], f"URGENT: {monitor['symbol']} protection requires review", 'New entries are blocked. Verify the position and protection in Kite, then use Recheck with Kite.', 'error')
            return
        if bootstrap_error:
            raise RuntimeError('Broker session could not be verified')
        client = client or _kite_for(monitor)
        if monitor['stage'] == 'ENTRY_FILLED':
            _finish_entry(client, monitor, {'filled_quantity': monitor['filled_quantity'], 'status': 'COMPLETE'}, lease)
            return
        if monitor["stage"] in {"SUBMITTING_EXIT", "EXIT_UNKNOWN"}:
            response = list(book.values()) if book is not None else client.get_gtts()
            triggers = response.get("data", []) if isinstance(response, dict) else response
            matches = []
            for trigger in triggers or []:
                condition = trigger.get("condition") or {}
                values = condition.get("trigger_values") or []
                orders = trigger.get("orders") or []
                created = trigger.get("created_at")
                try:
                    when = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
                    recent = abs(when.timestamp() - monitor["exit_submitted_at"]) < 120
                except (ValueError, KeyError, TypeError):
                    recent = False
                if (recent and condition.get("exchange") == "NSE" and condition.get("tradingsymbol") == monitor["symbol"]
                    and len(values) == 2 and abs(float(values[0]) - monitor["stop_loss"]) < 0.001
                    and abs(float(values[1]) - monitor["target"]) < 0.001 and len(orders) == 2
                    and all(o.get("transaction_type") == "SELL" and o.get("product") == "CNC"
                            and int(o.get("quantity", 0)) == monitor["filled_quantity"] for o in orders)):
                    matches.append(trigger)
            if len(matches) == 1:
                _verify_gtt(monitor, matches[0], exit_order=True)
                monitor["exit_gtt_id"] = str(matches[0]["id"])
                status = matches[0].get('status')
                monitor['stage'] = 'EXIT_PROTECTED' if status == 'active' else 'EXIT_PENDING' if status == 'triggered' else 'PROTECTION_LOST'
                monitor['last_verified_at'] = time.time()
                monitor['reconcile_attempts'] = 0
                monitor.pop("last_error", None)
                _save_monitor(monitor)
                _event(monitor["account_id"], f"{monitor['symbol']} protection reconciled", "Exactly one matching protective GTT was found in Kite. Monitoring resumed.", "success")
            else:
                _defer_reconciliation(monitor, 'EXIT_UNKNOWN')
                if time.time() - monitor.get('last_notified_at', 0) > 300:
                    monitor['last_notified_at'] = time.time()
                    _save_monitor(monitor)
                    _event(monitor['account_id'], f"URGENT: {monitor['symbol']} protection needs review", 'Protective order outcome is uncertain. Duplicate sell triggers are blocked; check Kite and use Recheck with Kite.', 'error')
            return
        if monitor["stage"] in {"SUBMITTING_ENTRY", "SUBMISSION_UNKNOWN"}:
            response = list(book.values()) if book is not None else client.get_gtts()
            gtts = response.get("data", response) if isinstance(response, dict) else response
            matches = []
            for trigger in gtts if isinstance(gtts, list) else []:
                condition = trigger.get("condition") or {}
                orders = trigger.get("orders") or []
                order = orders[0] if orders else {}
                created = trigger.get("created_at") or ""
                try:
                    created_at = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
                    created_ts = created_at.timestamp()
                except (TypeError, ValueError):
                    created_ts = 0
                if (
                    trigger.get("id")
                    and abs(created_ts - monitor.get("created_at", time.time())) < 120
                    and condition.get("exchange") == "NSE"
                    and condition.get("tradingsymbol") == monitor["symbol"]
                    and condition.get("trigger_values")
                    and abs(float(condition["trigger_values"][0]) - float(monitor["entry"])) < 0.02
                    and order.get("transaction_type") == "BUY"
                    and order.get("product") == "CNC"
                    and int(order.get("quantity", 0) or 0) == int(monitor["quantity"])
                    and created
                ):
                    matches.append(trigger)
            if len(matches) == 1:
                found = matches[0]
                _verify_gtt(monitor, found)
                monitor["entry_gtt_id"] = str(found.get("id"))
                monitor["stage"] = "ENTRY_PENDING"
                monitor['reconcile_attempts'] = 0
                _save_monitor(monitor)
                _event(monitor["account_id"], f"{monitor['symbol']} order reconciled", "Kite's GTT book confirms the entry trigger. The worker will monitor its fill.", "success")
            else:
                _defer_reconciliation(monitor, 'SUBMISSION_UNKNOWN')
                if time.time() - monitor.get('created_at', time.time()) > 300 and time.time() - monitor.get('last_notified_at', 0) > 300:
                    monitor['last_notified_at'] = time.time()
                    _save_monitor(monitor)
                    _event(monitor['account_id'], f"URGENT: {monitor['symbol']} order needs review", 'Could not uniquely match the entry to Kite. Further submissions are blocked until broker verification resolves it.', 'error')
            return

        if monitor["stage"] in {"ENTRY_PENDING", "CANCEL_ENTRY_REMAINDER", 'CANCEL_ENTRY_GTT', 'ENTRY_TRIGGERED'}:
            gtt = _gtt(client, monitor["entry_gtt_id"], book)
            _verify_gtt(monitor, gtt)
            monitor.pop("last_error", None)
            monitor.update({'last_verified_at': time.time(), 'next_check_at': time.time() + 5})
            _save_monitor(monitor)
            status = str(gtt.get("status", "")).lower()
            order_id, result = _entry_order(gtt)
            if order_id:
                order = _order_now(client, order_id)
                _verify_fill(monitor, order, 'BUY')
                order.setdefault('order_id', order_id)
                monitor['entry_order_id'] = order_id
                _save_monitor(monitor)
                _finish_entry(client, monitor, order, lease)
                return
            if status == 'active' and time.time() >= monitor.get('entry_expires_at', monitor.get('created_at', time.time()) + 1800):
                monitor['stage'] = 'CANCEL_ENTRY_GTT'
                _save_monitor(monitor)
                lease.renew()
                client.delete_gtt(int(monitor['entry_gtt_id']))
                _event(monitor['account_id'], f"{monitor['symbol']} entry expired", 'The untriggered entry GTT was cancelled at its expiry. Broker confirmation is pending.', 'info')
                return
            if status in {"cancelled", "expired", "rejected", "deleted", "disabled"}:
                monitor['close_reason'] = f'Entry GTT {status}'
                _event(monitor["account_id"], f"{monitor['symbol']} entry GTT ended", f"Kite reports entry GTT status: {status}.", "warning")
                _remove_monitor(monitor)
                return
            if status == "triggered":
                if result.get("status") == "failed":
                    monitor['close_reason'] = 'Triggered entry rejected without an order'
                    _event(monitor["account_id"], f"{monitor['symbol']} entry rejected", str(result.get("rejection_reason", "Kite rejected the triggered order.")), "error")
                    _remove_monitor(monitor)
                else:
                    _defer_reconciliation(monitor, 'ENTRY_TRIGGERED')
            return

        if monitor["stage"] in {'EXIT_PROTECTED', 'EXIT_PENDING', 'EXIT_TRIGGERED'}:
            gtt = _gtt(client, monitor["exit_gtt_id"], book)
            _verify_gtt(monitor, gtt, exit_order=True)
            monitor.pop("last_error", None)
            monitor.update({'last_verified_at': time.time(), 'next_check_at': time.time() + 30})
            _save_monitor(monitor)
            status = str(gtt.get("status", "")).lower()
            if status == "triggered":
                order_id, result = _entry_order(gtt)
                if order_id:
                    order = _order_now(client, order_id)
                    _verify_fill(monitor, order, 'SELL')
                    if order.get("status") in {"COMPLETE", "CANCELLED", "REJECTED"}:
                        remaining = max(0, int(monitor.get("filled_quantity", 0)) - int(order.get("filled_quantity", 0) or 0))
                        if remaining == 0:
                            monitor['close_reason'] = 'Protective exit fully filled'
                            _event(monitor["account_id"], f"{monitor['symbol']} exit completed", "Kite reports no remaining position after the protective exit.", "success")
                            _remove_monitor(monitor)
                        else:
                            monitor["stage"] = "PROTECTION_LOST"
                            monitor["filled_quantity"] = remaining
                            _save_monitor(monitor)
                            _event(monitor["account_id"], f"URGENT: {monitor['symbol']} still has an open position", f"The protective exit ended as {order.get('status')}; {remaining} share(s) remain. New entries are blocked; review protection in Kite immediately.", "error")
                    else:
                        if monitor['stage'] != 'EXIT_PENDING':
                            _event(monitor['account_id'], f"{monitor['symbol']} exit pending", 'The protective limit order has triggered but is not yet filled. Check Kite if the price has moved away.', 'warning')
                        monitor.update({'stage': 'EXIT_PENDING', 'next_check_at': time.time() + 5})
                        _save_monitor(monitor)
                elif result.get("status") == "failed":
                    monitor["stage"] = "PROTECTION_LOST"
                    _save_monitor(monitor)
                    _event(monitor["account_id"], f"URGENT: {monitor['symbol']} exit failed", str(result.get("rejection_reason", "Protective GTT exit was rejected.")), "error")
                else:
                    _defer_reconciliation(monitor, 'EXIT_TRIGGERED')
            elif status in {"cancelled", "expired", "rejected", "deleted", "disabled"}:
                monitor["stage"] = "PROTECTION_LOST"
                _save_monitor(monitor)
                _event(monitor["account_id"], f"URGENT: {monitor['symbol']} protection ended", f"Kite reports protective GTT status: {status}. Check the position immediately.", "error")
    except BrokerStateChanged:
        failed = True
        monitor.update({'resume_stage': monitor['stage'], 'stage': 'MANUAL_REVIEW', 'last_error': 'BrokerStateChanged'})
        _save_monitor(monitor)
        _event(monitor['account_id'], f"URGENT: {monitor['symbol']} broker state changed", 'The GTT or order differs from its saved intent. Automation stopped; check Kite before reconciling.', 'error')
    except (StateConflict, LeaseLost):
        failed = True
        log.warning('Order processing yielded after state or lease ownership changed')
    except Exception as exc:
        failed = True
        # Do not log exception strings from broker/network clients; they may contain
        # request details. Keep the monitor for automatic retry and notify the UI.
        monitor["last_error"] = bootstrap_error or type(exc).__name__
        monitor["last_error_at"] = time.time()
        monitor['retry_count'] = int(monitor.get('retry_count', 0)) + 1
        monitor['next_check_at'] = time.time() + min(300, 5 * 2 ** min(monitor['retry_count'], 6))
        if monitor['retry_count'] >= 6:
            monitor['resume_stage'] = monitor['stage']
            monitor['stage'] = 'MANUAL_REVIEW'
        try:
            _save_monitor(monitor)
        except (StateConflict, LeaseLost):
            return
        if time.time() - monitor.get("last_notified_at", 0) > 300:
            action = 'Automatic checks stopped after repeated failures. Check Kite and use Recheck with Kite.' if monitor['stage'] == 'MANUAL_REVIEW' else 'The worker will retry with backoff; check Kite if this continues.'
            _event(monitor["account_id"], f"{monitor['symbol']} order check needs attention", f"Could not verify order status ({type(exc).__name__}). {action}", "warning")
            monitor["last_notified_at"] = time.time()
            _save_monitor(monitor)
    finally:
        if not failed and (monitor.get('retry_count', 0) or monitor.get('last_error')):
            monitor['retry_count'] = 0
            monitor.pop('last_error', None)
            try:
                _save_monitor(monitor)
            except (StateConflict, LeaseLost):
                pass
        _lease.reset(token)


def run() -> None:
    validate_live_settings()
    from .main import _remote_session_store_enabled, AUTO_TRADE_MODE
    if AUTO_TRADE_MODE != 'live':
        raise RuntimeError('The real-order worker requires AUTO_TRADE_MODE=live.')
    if not _remote_session_store_enabled():
        raise RuntimeError('The order worker requires encrypted persistent Redis storage.')
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: _shutdown.set())
    logging.info('AutoInvest order worker started')
    leader_key = heartbeat_key().replace(':heartbeat', ':leader')
    while not _shutdown.is_set():
        try:
            with RedisLease(_redis_request, _redis_eval, leader_key) as leader:
                while not _shutdown.is_set():
                    next_delay = 30
                    leader.renew()
                    _redis_request('SET', heartbeat_key(), str(time.time()), ttl=90)
                    for account_id in _redis_request('SMEMBERS', 'autoinvest:accounts') or []:
                        if _shutdown.is_set():
                            break
                        clients = {}
                        try:
                            with RedisLease(_redis_request, _redis_eval, f'autoinvest:account-lock:{account_id}') as account_lease:
                                raw = _redis_eval("local ids=redis.call('SMEMBERS',KEYS[1]); local r={}; for _,id in ipairs(ids) do local v=redis.call('GET',ARGV[1]..id); if v then table.insert(r,v) end end; return r", [f'autoinvest:active:{account_id}'], ['autoinvest:monitor:']) or []
                                due = [json.loads(value) for value in raw]
                                due = [item for item in due if item.get('owner') in {None, os.getenv('DEPLOYMENT_ID', 'default')}]
                                if any(item['stage'] not in {'EXIT_PROTECTED', 'MANUAL_REVIEW', 'UNPROTECTED_POSITION', 'PROTECTION_LOST'} for item in due):
                                    next_delay = 5
                                due = [item for item in due if item.get('next_check_at', 0) <= time.time() and item['stage'] != 'MANUAL_REVIEW']
                                priority = {'ENTRY_FILLED': 0, 'SUBMITTING_EXIT': 1, 'EXIT_UNKNOWN': 1, 'EXIT_PENDING': 2, 'ENTRY_PENDING': 3}
                                due.sort(key=lambda item: (priority.get(item['stage'], 4), item.get('created_at', 0)))
                                for monitor in due:
                                    if _shutdown.is_set():
                                        break
                                    leader.renew()
                                    # The broker's GTT book is fetched once per
                                    # API key in an account cycle, not per stock.
                                    identity = monitor['api_key']
                                    if identity not in clients:
                                        client = None
                                        try:
                                            account_lease.renew()
                                            client = _kite_for(monitor)
                                            response = client.get_gtts()
                                            triggers = response.get('data', []) if isinstance(response, dict) else response
                                            if not isinstance(triggers, list):
                                                raise RuntimeError('Invalid broker GTT book')
                                            clients[identity] = (client, {str(item['id']): item for item in triggers}, None)
                                        except Exception as exc:
                                            if client is not None:
                                                client.reqsession.close()
                                            clients[identity] = (None, None, type(exc).__name__)
                                    client, book, failure = clients[identity]
                                    _process_monitor(monitor['id'], account_lease, client, book, failure)
                        except LeaseBusy:
                            continue
                        finally:
                            for client, _, _ in clients.values():
                                if client is not None:
                                    client.reqsession.close()
                    _shutdown.wait(next_delay)
        except LeaseBusy:
            # A second replica waits as standby; it cannot submit broker writes
            # while the elected worker owns the deployment lease.
            _shutdown.wait(15)
        except Exception as exc:
            logging.error('Worker storage/service failure: %s', type(exc).__name__)
            _shutdown.wait(15)
    logging.info('Order worker stopped')


if __name__ == "__main__":
    run()
