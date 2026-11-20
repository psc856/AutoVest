"""Persistent, one-shot opening protection. Broker writes stay in the worker."""

import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

try:
    from .guardrails import LeaseBusy, RedisLease
    from .live_config import heartbeat_key
    from .opening_stop import ACTIVE, CUTOFF, OPEN, fresh_quote, market_time, plan, price
    from .vendor.kite_rest import KiteConnect
except ImportError:
    from guardrails import LeaseBusy, RedisLease
    from live_config import heartbeat_key
    from opening_stop import ACTIVE, CUTOFF, OPEN, fresh_quote, market_time, plan, price
    from vendor.kite_rest import KiteConnect

log = logging.getLogger(__name__)
QUEUE = heartbeat_key().replace(':heartbeat', ':opening-queue')
HEARTBEAT = heartbeat_key().replace(':heartbeat', ':opening-heartbeat')
JOB_TTL = 86400
_ticks = {}


def job_key(account):
    return f'autoinvest:opening-stop:{account}'


def get_job(command, account):
    raw = command('GET', job_key(account))
    return json.loads(raw) if raw else None


def save_job(evaluate, job, lease):
    active = '1' if job['state'] in ACTIVE else '0'
    saved = evaluate(
        """if redis.call('GET', KEYS[3]) ~= ARGV[1] then return 0 end
           redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
           if ARGV[4] == '1' then redis.call('SADD', KEYS[2], ARGV[5])
           else redis.call('SREM', KEYS[2], ARGV[5]) end
           return 1""",
        [job_key(job['account']), QUEUE, lease.key],
        [lease.owner, json.dumps(job, allow_nan=False), str(JOB_TTL), active, job['account']],
    )
    if saved != 1:
        raise RuntimeError('Opening protection lease lost')


def tick_sizes(client):
    day = market_time().date()
    if day not in _ticks:
        values = {}
        for exchange in ('NSE', 'BSE'):
            for item in client.instruments(exchange):
                if item.get('instrument_type') == 'EQ':
                    values[f"{exchange}:{item['tradingsymbol']}"] = price(item['tick_size'])
        _ticks.clear()
        _ticks[day] = values
    return _ticks[day]


def reconcile(intent, orders):
    """An ambiguous write is never replayed. Require evidence in the order book."""
    action = intent['action']
    if 'order_id' in action:
        matches = [o for o in orders if str(o['order_id']) == str(action['order_id'])]
    else:
        ids = set(intent.get('ids', []))
        matches = [o for o in orders if str(o['order_id']) in ids or o.get('tag') == intent['tag']]
    verified = [
        o for o in matches
        if o.get('status') == 'TRIGGER PENDING'
        and o.get('exchange') == action['exchange'] and o.get('tradingsymbol') == action['tradingsymbol']
        and o.get('product') == 'MIS' and o.get('transaction_type') == 'SELL'
        and o.get('order_type') == action['order_type']
        and price(o['trigger_price']) == price(action['trigger_price'])
        and ('price' not in action or price(o['price']) == price(action['price']))
    ]
    return bool(verified) and sum(int(o['pending_quantity']) for o in verified) == action['quantity']


def send(client, intent):
    action = intent['action']
    params = dict(variety='regular', trigger_price=action['trigger_price'],
                  order_type=action['order_type'], validity='DAY')
    if action['order_type'] == 'SL-M':
        params['market_protection'] = -1
    if 'order_id' in action:
        if 'price' in action:
            params['price'] = action['price']
        # Leave quantity untouched on a modification.
        return [str(client.modify_order(order_id=action['order_id'], **params))]
    result = client.place_autoslice_order(
        exchange=action['exchange'], tradingsymbol=action['tradingsymbol'],
        transaction_type='SELL', product='MIS', quantity=action['quantity'],
        tag=intent['tag'], **params,
    )
    children = result.get('children') or []
    if not children and result.get('order_id'):
        return [str(result['order_id'])]
    return [str(child['order_id']) for child in children if child.get('order_id')]


class OrderBudget:
    def __init__(self):
        self.last_quote = float('-inf')

    def take(self, evaluate, lease, account, count):
        # Keep a broker-write budget across worker restarts and replicas.
        return evaluate(
            """if redis.call('GET', KEYS[2]) ~= ARGV[1] then return 0 end
               local clock = redis.call('TIME')
               local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
               redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - 60000)
               local minute = redis.call('ZCARD', KEYS[1])
               local second = redis.call('ZCOUNT', KEYS[1], now - 1100, '+inf')
               local count = math.min(tonumber(ARGV[2]), 8 - second, 190 - minute)
               for i = 1, count do redis.call('ZADD', KEYS[1], now, ARGV[3] .. ':' .. i) end
               redis.call('PEXPIRE', KEYS[1], 61000)
               return math.max(0, count)""",
            [f'autoinvest:opening-budget:{account}', lease.key],
            [lease.owner, str(count), uuid.uuid4().hex],
        )


def dispatch(client, intent, deadline, shutdown, snapshot_at):
    if (shutdown.is_set() or not OPEN <= market_time().time() < CUTOFF
            or time.time() >= deadline or time.monotonic() - snapshot_at > 2):
        return None, []
    broker = KiteConnect(api_key=client.api_key, access_token=client.access_token, timeout=3)
    try:
        attempted = market_time().isoformat(timespec='milliseconds')
        try:
            return attempted, send(broker, intent)
        except Exception as exc:
            log.warning('Opening stop request requires reconciliation (%s)', type(exc).__name__)
            return attempted, []
    finally:
        broker.reqsession.close()


def run_cycle(client, job, lease, evaluate, budget, shutdown, executor):
    now = market_time()
    orders = client.orders()
    snapshot_at = time.monotonic()
    positions = client.positions()['net']
    ticks = tick_sizes(client)
    rows, actions = plan(positions, orders, ticks, job.get('scope'))
    job['rows'] = rows
    if now.time() < OPEN:
        if not job.get('preview_at') or time.time() - job['preview_at'] >= 2:
            job['preview_at'] = time.time()
            save_job(evaluate, job, lease)
        return
    job['state'] = 'running'
    if job.get('scope') is None and rows:
        job['scope'] = [row['key'] for row in rows]
    pending = job['pending']
    for key, intent in list(pending.items()):
        if reconcile(intent, orders):
            del pending[key]
            job.setdefault('confirmations', {})[key] = market_time().isoformat(timespec='milliseconds')
            continue
        if time.time() - intent['at'] >= 12:
            job['issues'][key] = 'Could not confirm full protection. Check all orders and slices in Kite.'
        for row in rows:
            if row['key'] == key:
                row['status'] = job['issues'].get(key, 'Awaiting broker confirmation')
                row['last_attempt_at'] = intent['attempt_at']
    if job.get('scope') and not pending and all(r['covered'] == r['quantity'] for r in rows):
        job.update(state='complete', message='Protection confirmed for the remaining positions in this run.')
        save_job(evaluate, job, lease)
        return
    if time.time() >= job['deadline'] or now.time() >= CUTOFF:
        job.update(state='review', message='Run ended. Review any unprotected quantity or uncertain orders in Kite.')
        save_job(evaluate, job, lease)
        return
    candidates = [a for a in actions if a['key'] not in pending and a['key'] not in job['issues']]
    if candidates and time.monotonic() - budget.last_quote >= 1.05:
        budget.last_quote = time.monotonic()
        offset = job.get('quote_offset', 0) % len(candidates)
        page = (candidates[offset:] + candidates[:offset])[:50]
        job['quote_offset'] = offset + len(page)
        quotes = client.quote([a['key'] for a in page])
        eligible = []
        for action in page:
            if not fresh_quote(quotes.get(action['key']), market_time(), action['trigger_price']):
                next(r for r in rows if r['key'] == action['key'])['status'] = 'Waiting for a fresh price above the trigger'
                continue
            eligible.append(action)
        if eligible:
            lease.renew()
            count = budget.take(evaluate, lease, job['account'], len(eligible))
            batch = {
                action['key']: {'action': action, 'tag': 'os' + uuid.uuid4().hex[:18],
                                'at': time.time(), 'attempt_at': None}
                for action in eligible[:count]
            }
            if batch:
                pending.update(batch)
                save_job(evaluate, job, lease)
                futures = {key: executor.submit(dispatch, client, intent, job['deadline'], shutdown, snapshot_at)
                           for key, intent in batch.items()}
                for key, future in futures.items():
                    attempted, ids = future.result()
                    if attempted is None:
                        del pending[key]
                    else:
                        pending[key].update(attempt_at=attempted, ids=ids)
                        job['attempts'][key] = attempted
                        job.setdefault('confirmations', {}).pop(key, None)
    save_job(evaluate, job, lease)


def run(shutdown):
    from .main import _read_saved_session, _redis_eval, _redis_request

    clients = {}
    budgets = {}
    executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix='opening-order')
    next_heartbeat = 0
    try:
        while not shutdown.is_set():
            now = market_time()
            try:
                if time.monotonic() >= next_heartbeat:
                    _redis_request('SET', HEARTBEAT, str(time.time()), ttl=10)
                    next_heartbeat = time.monotonic() + 4
                if now.weekday() >= 5 or not now.replace(hour=9, minute=0).time() <= now.time() < CUTOFF:
                    shutdown.wait(2)
                    continue
                accounts = _redis_request('SMEMBERS', QUEUE) or []
                for account in list(clients):
                    if account not in accounts:
                        clients.pop(account)[1].reqsession.close()
                        budgets.pop(account, None)
                for account in accounts:
                    if shutdown.is_set():
                        break
                    try:
                        with RedisLease(_redis_request, _redis_eval, f'autoinvest:account-lock:{account}') as lease:
                            job = get_job(_redis_request, account)
                            if not job or job.get('queue') != QUEUE or job['state'] not in ACTIVE:
                                _redis_request('SREM', QUEUE, account)
                                continue
                            if job['date'] != now.date().isoformat() or time.time() >= job['deadline']:
                                job.update(state='review', message='Run expired. Check outstanding protection in Kite.')
                                save_job(_redis_eval, job, lease)
                                continue
                            saved = _read_saved_session(job['session'])
                            if not saved:
                                job.update(state='review', message='Kite session ended. Check protection in Kite.')
                                save_job(_redis_eval, job, lease)
                                continue
                            cached = clients.get(account)
                            if cached is None or cached[0] != (saved['api_key'], saved['access_token']):
                                client = KiteConnect(api_key=saved['api_key'], timeout=3)
                                client.set_access_token(saved['access_token'])
                                if cached:
                                    cached[1].reqsession.close()
                                clients[account] = ((saved['api_key'], saved['access_token']), client)
                            run_cycle(clients[account][1], job, lease, _redis_eval,
                                      budgets.setdefault(account, OrderBudget()), shutdown, executor)
                    except LeaseBusy:
                        continue
                    except Exception as exc:
                        log.warning('Opening protection cycle failed (%s)', type(exc).__name__)
                        shutdown.wait(1)
                near_open = now.hour == 9 and now.minute in {14, 15}
                shutdown.wait(0.35 if near_open or accounts and now.time() >= OPEN else 2)
            except Exception as exc:
                log.warning('Opening protection storage unavailable (%s)', type(exc).__name__)
                shutdown.wait(2)
    finally:
        executor.shutdown()
        for _, client in clients.values():
            client.reqsession.close()
