"""Atomic Lua tests against an isolated local Redis (enabled by TEST_REDIS_PORT).

No production Redis credentials are accepted. No FLUSH commands are used.
"""
import json
import os
import socket
import unittest
import uuid

from backend.order_state import create_intent, save_monitor, archive_monitor, StateConflict


class LocalRedis:
    def __init__(self):
        self.socket = socket.create_connection(('127.0.0.1', int(os.environ['TEST_REDIS_PORT'])), timeout=5)
        self.file = self.socket.makefile('rb')

    def close(self):
        self.file.close()
        self.socket.close()

    def command(self, *parts):
        chunks = [f'*{len(parts)}\r\n'.encode()]
        for part in parts:
            value = str(part).encode()
            chunks.extend([f'${len(value)}\r\n'.encode(), value, b'\r\n'])
        self.socket.sendall(b''.join(chunks))
        return self.read()

    def read(self):
        line = self.file.readline()
        kind, data = line[:1], line[1:-2]
        if kind == b'+':
            return data.decode()
        if kind == b'-':
            raise RuntimeError(data.decode())
        if kind == b':':
            return int(data)
        if kind == b'$':
            size = int(data)
            if size < 0:
                return None
            value = self.file.read(size)
            self.file.read(2)
            return value.decode()
        if kind == b'*':
            return [self.read() for _ in range(int(data))]
        raise RuntimeError('Invalid Redis response')

    def evaluate(self, script, keys, args):
        return self.command('EVAL', script, len(keys), *keys, *args)


@unittest.skipUnless(os.getenv('TEST_REDIS_PORT'), 'Set TEST_REDIS_PORT for isolated local Redis integration tests')
class RedisAtomicityTests(unittest.TestCase):
    def setUp(self):
        self.redis = LocalRedis()
        self.account = 'TEST' + uuid.uuid4().hex[:28]
        self.monitor = {'id': uuid.uuid4().hex, 'account_id': self.account, 'symbol': 'TEST', 'stage': 'SUBMITTING_ENTRY',
                        'quantity': 1, 'api_key': 'private-api', 'session_id': 'private-session'}
        self.duplicate = f'autoinvest:bought:{self.account}:2026-11-18:TEST'

    def tearDown(self):
        # Exact keys created by this test account only.
        self.redis.command('DEL', self.duplicate, f"autoinvest:monitor:{self.monitor['id']}",
            f'autoinvest:active:{self.account}', f'autoinvest:history:{self.account}',
            f"autoinvest:archive:{self.account}:{self.monitor['id']}")
        self.redis.command('SREM', 'autoinvest:accounts', self.account)
        self.redis.close()

    def test_intent_and_duplicate_guard_are_committed_together(self):
        self.assertTrue(create_intent(self.redis.evaluate, self.monitor, self.duplicate))
        duplicate = {**self.monitor, 'id': uuid.uuid4().hex}
        self.assertFalse(create_intent(self.redis.evaluate, duplicate, self.duplicate))
        self.assertIsNone(self.redis.command('GET', f"autoinvest:monitor:{duplicate['id']}"))
        self.assertEqual(self.redis.command('SCARD', f'autoinvest:active:{self.account}'), 1)

    def test_compare_and_swap_rejects_old_writes_and_archives_atomically(self):
        create_intent(self.redis.evaluate, self.monitor, self.duplicate)
        stale = dict(self.monitor)
        self.monitor['stage'] = 'ENTRY_PENDING'
        save_monitor(self.redis.evaluate, self.monitor)
        with self.assertRaises(StateConflict):
            save_monitor(self.redis.evaluate, stale)
        archive_monitor(self.redis.evaluate, self.monitor, 'Test complete')
        self.assertIsNone(self.redis.command('GET', f"autoinvest:monitor:{self.monitor['id']}"))
        self.assertEqual(self.redis.command('SCARD', f'autoinvest:active:{self.account}'), 0)
        archived = json.loads(self.redis.command('GET', f"autoinvest:archive:{self.account}:{self.monitor['id']}"))
        self.assertEqual(archived['reason'], 'Test complete')
        self.assertNotIn('api_key', archived)
        self.assertNotIn('session_id', archived)
