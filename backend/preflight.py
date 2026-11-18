"""Deployment checks which never submit or cancel a broker order."""
import argparse
import sys
import time
import uuid

from .main import _redis_eval, _redis_request, AUTO_TRADE_MODE
from .live_config import configuration_errors, heartbeat_key


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--check-worker', action='store_true')
    parser.add_argument('--worker-health', action='store_true')
    args = parser.parse_args()
    problems = configuration_errors()
    if args.live and AUTO_TRADE_MODE != 'live':
        problems.append('AUTO_TRADE_MODE is not live.')
    if problems:
        for problem in problems:
            print('FAIL:', problem)
        return 1
    try:
        if not args.worker_health:
            key = 'autoinvest:preflight:' + uuid.uuid4().hex
            nonce = uuid.uuid4().hex
            result = _redis_eval("redis.call('SET',KEYS[1],ARGV[1],'EX',15); return redis.call('GET',KEYS[1])", [key], [nonce])
            if result != nonce:
                raise RuntimeError('Atomic persistent storage check failed')
            print('PASS: configuration, encryption key format, and atomic persistent storage')
        if args.check_worker or args.worker_health:
            heartbeat = _redis_request('GET', heartbeat_key())
            if not heartbeat or not 0 <= time.time() - float(heartbeat) < 90:
                print('FAIL: matching order worker is offline or stale')
                return 1
            print('PASS: matching order worker is healthy')
    except Exception as exc:
        print(f'FAIL: persistent storage/worker check failed ({type(exc).__name__})')
        return 1
    print('No broker orders were submitted or cancelled.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
