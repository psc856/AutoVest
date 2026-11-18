"""Offline regression tests. All broker and Redis calls are replaced with fakes."""
import asyncio
import json
import math
import tempfile
import uuid
from contextlib import contextmanager
import unittest
from datetime import datetime
from unittest.mock import Mock, AsyncMock, patch
from pathlib import Path
from zoneinfo import ZoneInfo

from starlette.requests import Request
from fastapi import HTTPException
from pydantic import ValidationError
from backend import main, trade_worker
from backend.guardrails import RedisLease, LeaseBusy, LeaseLost, position_size
from backend.strategy_runtime import evaluate_strategy, inspect_strategy, StrategyValidationError


def strategy(body):
    return 'STRATEGY = {"name": "Regression strategy", "parameters": []}\ndef evaluate(candles, params):\n' + '\n'.join('    ' + line for line in body.splitlines())


def request():
    return Request({"type": "http", "method": "POST", "path": "/api/auto-trade/execute", "headers": [(b"origin", b"http://localhost:5173")], "client": ("127.0.0.1", 1234)})


@contextmanager
def workspace_files():
    # Windows 3.14's TemporaryDirectory ACL blocks restricted sandbox users.
    root = Path(__file__).resolve().parent
    directory = root / ('.test-' + uuid.uuid4().hex)
    assert directory.parent == root
    directory.mkdir()
    try:
        yield directory
    finally:
        for file in directory.iterdir():
            file.unlink()
        directory.rmdir()


class InterpreterTests(unittest.TestCase):
    def test_unsafe_code_is_rejected(self):
        for body in ('import os\nreturn {}', 'return candles.__class__', 'while True:\n    return {}', 'return open("secret")', 'return {**params}'):
            with self.subTest(body=body), self.assertRaises(StrategyValidationError):
                inspect_strategy(strategy(body))

    def test_short_circuit_skips_invalid_access(self):
        result = evaluate_strategy(strategy('return {"signal": False and candles[999]["close"] > 0}'), [{"close": 10}], {})
        self.assertFalse(result['signal'])

    def test_memory_amplification_is_rejected_before_allocation(self):
        for expression in ('"%999999999s" % "x"', '999999999 * "x"', '[1] * 999999999'):
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                evaluate_strategy(strategy(f'return {expression}'), [{"close": 10}], {})

    def test_exponent_overflow_and_complex_values_are_rejected(self):
        with self.assertRaises(ValueError):
            evaluate_strategy(strategy('return 10 ** 999'), [{"close": 10}], {})
        with self.assertRaises(StrategyValidationError):
            inspect_strategy(strategy('return 1j'))

    def test_nested_aliases_have_a_budget(self):
        body = 'a = [1]\n' + '\n'.join('a = [a, a]' for _ in range(20)) + '\nreturn a'
        with self.assertRaises(ValueError):
            evaluate_strategy(strategy(body), [{"close": 10}], {})

    def test_example_generates_a_signal(self):
        from pathlib import Path
        source = Path('backend/strategy_templates/daily_volume_breakout.py').read_text()
        candles = [{"open": 90, "high": 100, "low": 88, "close": 95, "volume": 100} for _ in range(25)]
        candles.append({"open": 99, "high": 110, "low": 99, "close": 108, "volume": 500})
        metadata = inspect_strategy(source)
        result = evaluate_strategy(source, candles, {p['key']: p['default'] for p in metadata['parameters']})
        self.assertTrue(result['signal'])
        self.assertLess(result['stop_loss'], result['entry_price'])

    def test_connection_check_template_evaluates_kite_candles_and_filters(self):
        source = Path('backend/strategy_templates/connection_check.py').read_text()
        metadata = inspect_strategy(source)
        params = {item['key']: item['default'] for item in metadata['parameters']}
        candles = [{'open': 98, 'high': 105, 'low': 95, 'close': 100, 'volume': 1000} for _ in range(30)]
        outcome = evaluate_strategy(source, candles, params)
        self.assertTrue(outcome['signal'])
        self.assertLess(outcome['stop_loss'], outcome['entry_price'])
        self.assertGreater(outcome['target_price'], outcome['entry_price'])
        self.assertFalse(evaluate_strategy(source, candles, {**params, 'min_price': 500})['signal'])
        self.assertFalse(evaluate_strategy(source, candles[:10], params)['signal'])


class GuardrailTests(unittest.TestCase):
    def test_routes_are_registered_once(self):
        routes = [(route.path, tuple(sorted(getattr(route, 'methods', []) or ['WEBSOCKET']))) for route in main.app.routes]
        self.assertEqual(len(routes), len(set(routes)))
    def test_local_tokens_are_encrypted_and_survive_reload(self):
        with workspace_files() as directory, patch.object(main, 'TOKEN_FILE', Path(directory) / 'tokens.json'), patch.object(main, 'LOCAL_KEY_FILE', Path(directory) / 'tokens.key'), patch.object(main, '_TOKEN_ENCRYPTION_KEY', ''), patch.object(main, '_remote_session_store_enabled', return_value=False):
            main._write_saved_session('api-test', 'access-token-test')
            self.assertNotIn('access-token-test', main.TOKEN_FILE.read_text())
            self.assertEqual(main._read_saved_session()['access_token'], 'access-token-test')
    def test_sizing_respects_risk_and_cash(self):
        for risk, value, entry, stop in ((250, 10000, 101, 95), (2, 100, 500, 490), (100, 1000, 99.95, 99.9)):
            quantity, reserved = position_size(risk, value, entry, stop)
            self.assertLessEqual(reserved, risk)
            self.assertLessEqual(quantity * entry, value)

    def test_nonfinite_values_rejected(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.assertRaises(ValueError):
                position_size(value, 1000, 100, 90)
            with self.assertRaises(ValidationError):
                main.TradeRiskSettings(risk_per_trade=value)

    def test_budget_relationship_rejected(self):
        with self.assertRaises(ValidationError):
            main.TradeRiskSettings(risk_per_trade=1000, max_daily_loss=500)

    def test_lease_contention_fails_closed(self):
        with self.assertRaises(LeaseBusy):
            with RedisLease(Mock(return_value=None), Mock(), 'account'):
                self.fail('Must not enter while leased')

    def test_lost_lease_prohibits_broker_write(self):
        with RedisLease(Mock(return_value='OK'), Mock(return_value=0), 'account') as lease:
            with self.assertRaises(LeaseLost):
                lease.renew()

    def test_forged_and_malformed_cookies_are_rejected(self):
        for cookie in ('x.y', 'a' * 43 + '.' + '0' * 64, 'abc', '.signature', 'a' * 10000):
            self.assertIsNone(main._session_id_from_cookie(cookie))

    def test_tick_rounding_is_directional(self):
        self.assertEqual(main._price_to_tick(100.021, .05, upward=True), 100.05)
        self.assertEqual(main._price_to_tick(100.021, .05, upward=False), 100)


class TradeStateTests(unittest.TestCase):
    def test_uploaded_connection_strategy_roundtrip_to_sized_simulation(self):
        client = Mock()
        client.profile.return_value = {'user_id': 'ACCOUNT'}
        client.quote.return_value = {'NSE:TEST': {'last_price': 99}}
        candles = [{'date': '2026-01-01', 'open': 98, 'high': 105, 'low': 95, 'close': 100, 'volume': 1000} for _ in range(30)]
        source = Path('backend/strategy_templates/connection_check.py').read_text()
        snapshot = {'metadata': {'asOf': datetime.now(ZoneInfo('Asia/Kolkata')).date().isoformat()}, 'stocks': [{'symbol': 'TEST', 'companyName': 'Test company', 'instrument_token': 123, 'tick_size': .05, 'candles': candles}]}
        with workspace_files() as directory, patch.object(main, 'STRATEGY_FILE', directory / 'strategies.json'), patch.object(main, '_remote_session_store_enabled', return_value=False), patch.object(main, '_current_kite', return_value=client), patch.object(main, 'scanner_data', return_value=snapshot), patch.object(main, 'AUTO_TRADE_MODE', 'paper'), patch.object(main, '_emit_trade_event'), patch.dict(main._local_previews, {}, clear=True):
            uploaded = main.upload_strategy(main.StrategyUpload(filename='connection_check.py', source=source), request())['strategy']
            listed = main.list_strategies(request())['strategies']
            self.assertEqual(listed[0]['id'], uploaded['id'])
            preview = main.scan_strategy(uploaded['id'], main.StrategyScanRequest(), request())
            self.assertEqual(preview['matches'], 1)
            result = main.execute_auto_trade(main.TradeExecuteRequest(preview_id=preview['preview_id'], symbols=['TEST']), request())
            self.assertEqual(result['results'][0]['status'], 'paper')
            self.assertGreater(result['results'][0]['quantity'], 0)
        client.place_gtt.assert_not_called()

    def setUp(self):
        self.client = Mock()
        self.client.quote.return_value = {'NSE:TEST': {'last_price': 99}}
        self.row = {'symbol': 'TEST', 'entry_price': 100, 'entry_limit_price': 100.5, 'stop_loss': 95, 'stop_limit_price': 94.5, 'target_price': 110, 'tick_size': .05}
        self.preview = {'id': 'a' * 32, 'account_id': 'ACCOUNT', 'session_id': None, 'created_at': main.time.time(), 'asOf': datetime.now(ZoneInfo('Asia/Kolkata')).date().isoformat(), 'strategy': {'id': 'b' * 32}, 'candidates': [self.row]}
        self.payload = main.TradeExecuteRequest(preview_id='a' * 32, symbols=['TEST'])

    def test_paper_trade_actually_produces_a_plan_without_broker_writes(self):
        with patch.object(main, 'AUTO_TRADE_MODE', 'paper'), patch.object(main, '_remote_session_store_enabled', return_value=False), patch.dict(main._local_previews, {'a' * 32: self.preview}), patch.object(main, '_emit_trade_event'):
            result = main._execute_trade_locked(self.payload, request(), self.client, 'ACCOUNT', None)
        self.assertEqual(len(result['results']), 1)
        self.assertGreater(result['results'][0]['quantity'], 0)
        self.client.place_gtt.assert_not_called()

    def test_cross_account_preview_cannot_be_executed(self):
        with patch.object(main, 'AUTO_TRADE_MODE', 'paper'), patch.object(main, '_remote_session_store_enabled', return_value=False), patch.dict(main._local_previews, {'a' * 32: self.preview}):
            with self.assertRaises(HTTPException) as caught:
                main._execute_trade_locked(self.payload, request(), self.client, 'OTHER', None)
        self.assertEqual(caught.exception.status_code, 409)
        self.client.place_gtt.assert_not_called()

    def test_expired_preview_cannot_be_executed(self):
        self.preview['created_at'] -= 601
        with patch.object(main, 'AUTO_TRADE_MODE', 'paper'), patch.object(main, '_remote_session_store_enabled', return_value=False), patch.dict(main._local_previews, {'a' * 32: self.preview}):
            with self.assertRaises(HTTPException):
                main._execute_trade_locked(self.payload, request(), self.client, 'ACCOUNT', None)
        self.client.place_gtt.assert_not_called()

    def test_protection_timeout_persists_uncertain_state_before_call(self):
        monitor = {'id': 'm', 'account_id': 'ACCOUNT', 'symbol': 'TEST', 'stop_loss': 95, 'target': 110, 'tick_size': .05, 'stage': 'ENTRY_PENDING'}
        self.client.ltp.return_value = {'NSE:TEST': {'last_price': 100}}
        snapshots = []
        def broker(*args, **kwargs):
            self.assertEqual(snapshots[-1]['stage'], 'SUBMITTING_EXIT')
            raise TimeoutError()
        self.client.place_gtt.side_effect = broker
        with patch.object(trade_worker, '_save_monitor', side_effect=lambda m: snapshots.append(dict(m))), patch.object(trade_worker, '_event'), patch.object(trade_worker, '_record_fill_day'):
            trade_worker._finish_entry(self.client, monitor, {'filled_quantity': 2, 'status': 'COMPLETE'}, Mock())
        self.assertEqual(monitor['stage'], 'EXIT_UNKNOWN')
        self.assertEqual(self.client.place_gtt.call_count, 1)

    def test_uncertain_exit_is_reconciled_without_resubmission(self):
        monitor = {'id': 'm', 'account_id': 'ACCOUNT', 'symbol': 'TEST', 'stop_loss': 95, 'target': 110, 'stage': 'EXIT_UNKNOWN', 'exit_submitted_at': main.time.time(), 'last_notified_at': main.time.time()}
        self.client.get_gtts.return_value = []
        with patch.object(trade_worker, '_redis_request'), patch.object(trade_worker, '_load_monitor', return_value=monitor), patch.object(trade_worker, '_kite_for', return_value=self.client), patch.object(trade_worker, '_save_monitor'), patch.object(trade_worker, '_event'):
            trade_worker._process_monitor('m', Mock())
        self.client.place_gtt.assert_not_called()

    def test_existing_protection_failure_blocks_new_live_entries(self):
        now = datetime(2026, 11, 18, 10, 0, tzinfo=ZoneInfo('Asia/Kolkata'))
        self.preview['asOf'] = now.date().isoformat()
        self.client.positions.return_value = {'day': [], 'net': []}
        self.client.holdings.return_value = []
        self.client.orders.return_value = []
        self.client.margins.return_value = {'available': {'live_balance': 100000}}
        records = {
            'autoinvest:preview:ACCOUNT:' + 'a' * 32: json.dumps(self.preview),
            'autoinvest:active:ACCOUNT': ['monitor'],
            'autoinvest:monitor:monitor': json.dumps({'stage': 'EXIT_UNKNOWN', 'risk': 100}),
        }
        with patch.object(main, 'AUTO_TRADE_MODE', 'live'), patch.object(main, '_remote_session_store_enabled', return_value=True), patch.object(main, '_trade_worker_online', return_value=True), patch.object(main, '_redis_request', side_effect=lambda method, key, *args, **kwargs: records.get(key)), patch.object(main, 'datetime') as clock:
            clock.now.return_value = now
            with self.assertRaises(HTTPException) as caught:
                main._execute_trade_locked(self.payload, request(), self.client, 'ACCOUNT', Mock())
        self.assertEqual(caught.exception.status_code, 409)
        self.client.place_gtt.assert_not_called()


class HttpSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, path, body, origin='http://localhost:5173', content_length=None, method='POST', extra_headers=None):
        sent = []
        delivered = False
        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': body, 'more_body': False}
            await asyncio.Event().wait()
        async def send(message):
            sent.append(message)
        headers = [(b'host', b'localhost:8000'), (b'origin', origin.encode()), (b'content-type', b'application/json')]
        headers.extend(extra_headers or [])
        if content_length is not None:
            headers.append((b'content-length', str(content_length).encode()))
        scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'}, 'http_version': '1.1', 'method': method, 'scheme': 'http', 'path': path, 'raw_path': path.encode(), 'query_string': b'', 'headers': headers, 'client': ('127.0.0.1', 1234), 'server': ('localhost', 8000)}
        with patch.object(main, '_SESSION_REDIS_URL', ''), patch.object(main, '_SESSION_REDIS_TOKEN', ''), patch.object(main, '_remote_session_store_enabled', return_value=False):
            main._request_windows.clear()
            await asyncio.wait_for(main.app(scope, receive, send), timeout=5)
        status = next(m['status'] for m in sent if m['type'] == 'http.response.start')
        response = b''.join(m.get('body', b'') for m in sent if m['type'] == 'http.response.body')
        return status, response

    async def test_cross_origin_mutation_is_rejected(self):
        status, _ = await self.call('/api/auth/login', b'{}', origin='https://attacker.example')
        self.assertEqual(status, 403)

    async def test_vite_fallback_port_preflight_is_allowed(self):
        status, _ = await self.call('/api/auth/login', b'', origin='http://127.0.0.1:5175', method='OPTIONS',
                                    extra_headers=[(b'access-control-request-method', b'POST')])
        self.assertEqual(status, 200)
        self.assertTrue(main._origin_allowed('http://127.0.0.1:5175'))
        self.assertFalse(main._origin_allowed('http://attacker.example:5175'))

    async def test_streamed_body_limit(self):
        status, _ = await self.call('/api/auth/login', b'x' * 131073)
        self.assertEqual(status, 413)

    async def test_validation_response_never_echoes_credentials(self):
        secret = 'private-credential-do-not-echo'
        status, response = await self.call('/api/auth/login', json.dumps({'api_key': [], 'api_secret': secret}).encode())
        self.assertEqual(status, 422)
        self.assertNotIn(secret.encode(), response)

    async def test_websocket_disconnect_releases_subscription_slot(self):
        ws = Mock()
        ws.headers = {'origin': 'http://localhost:5173'}
        ws.client.host = '127.0.0.1'
        ws.accept = AsyncMock()
        ws.receive = AsyncMock(return_value={'type': 'websocket.disconnect'})
        ws.send_json = AsyncMock()
        ws.close = AsyncMock()
        with patch.object(main, '_current_kite', return_value=Mock()), patch.object(main, '_account_id', return_value='TESTACCOUNT'), patch.object(main, '_stream_ticks', new_callable=AsyncMock):
            await main.live_ticks(ws, tokens='123,456', mode='full')
        self.assertNotIn('TESTACCOUNT', main._live_connections)
        ws.close.assert_awaited()


if __name__ == '__main__':
    unittest.main()
