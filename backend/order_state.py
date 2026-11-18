"""Atomic order intents, optimistic state updates, and terminal audit records."""
import json
import time


class StateConflict(RuntimeError):
    pass


def create_intent(evaluate, monitor, duplicate_key, ttl=45 * 86400):
    monitor['_revision'] = 1
    monitor['updated_at'] = time.time()
    script = """
    if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
    if redis.call('EXISTS', KEYS[2]) == 1 then return -1 end
    redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[3])
    redis.call('SET', KEYS[2], ARGV[2])
    redis.call('SADD', KEYS[3], ARGV[1])
    redis.call('SADD', KEYS[4], ARGV[4])
    return 1
    """
    return evaluate(script, [duplicate_key, f"autoinvest:monitor:{monitor['id']}",
        f"autoinvest:active:{monitor['account_id']}", 'autoinvest:accounts'],
        [monitor['id'], json.dumps(monitor, allow_nan=False), str(ttl), monitor['account_id']]) == 1


def save_monitor(evaluate, monitor):
    previous = int(monitor.get('_revision', 0))
    next_value = {**monitor, '_revision': previous + 1, 'updated_at': time.time()}
    script = """
    local raw=redis.call('GET',KEYS[1]); if not raw then return 0 end
    local current=cjson.decode(raw)
    if tonumber(current._revision or 0) ~= tonumber(ARGV[1]) then return 0 end
    redis.call('SET',KEYS[1],ARGV[2]); return 1
    """
    if evaluate(script, [f"autoinvest:monitor:{monitor['id']}"], [str(previous), json.dumps(next_value, allow_nan=False)]) != 1:
        raise StateConflict('The order state changed. Reconciliation is required before retrying.')
    monitor.update(next_value)


def archive_monitor(evaluate, monitor, reason='Monitoring complete'):
    # Archive only operational data; session pointers and API keys are excluded.
    fields = ('id', 'account_id', 'symbol', 'quantity', 'filled_quantity', 'entry',
              'stop_loss', 'target', 'risk', 'entry_gtt_id', 'entry_order_id',
              'exit_gtt_id', 'created_at', 'updated_at', 'stage')
    record = {key: monitor.get(key) for key in fields}
    record.update({'closed_at': time.time(), 'reason': reason, 'status': 'CLOSED'})
    script = """
    local raw=redis.call('GET',KEYS[1]); if not raw then return 0 end
    local current=cjson.decode(raw)
    if tonumber(current._revision or 0) ~= tonumber(ARGV[1]) then return 0 end
    redis.call('SET',KEYS[4],ARGV[3]); redis.call('LPUSH',KEYS[3],ARGV[3]); redis.call('LTRIM',KEYS[3],0,999)
    redis.call('SREM',KEYS[2],ARGV[2]); redis.call('DEL',KEYS[1])
    if redis.call('SCARD',KEYS[2]) == 0 then redis.call('SREM',KEYS[5],ARGV[4]) end
    return 1
    """
    if evaluate(script, [f"autoinvest:monitor:{monitor['id']}", f"autoinvest:active:{monitor['account_id']}",
                         f"autoinvest:history:{monitor['account_id']}", f"autoinvest:archive:{monitor['account_id']}:{monitor['id']}", 'autoinvest:accounts'],
                [str(monitor.get('_revision', 0)), monitor['id'], json.dumps(record, allow_nan=False), monitor['account_id']]) != 1:
        raise StateConflict('Order state changed before archival.')
