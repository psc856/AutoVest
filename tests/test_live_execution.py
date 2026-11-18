"""Live-order contracts exercised offline with broker/storage fakes."""
import hashlib
import json
import os
import struct
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet
from fastapi import HTTPException
from starlette.requests import Request

from backend import main, trade_worker
from backend.live_config import configuration_errors, deployment_fingerprint, heartbeat_key, validate_live_settings
from backend.order_state import save_monitor, archive_monitor, StateConflict
from backend.tick_parser import parse_ticks
from backend.vendor.kite_rest import KiteConnect


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 18, 10, 0, tzinfo=ZoneInfo('Asia/Kolkata'))


class LiveConfigurationTests(unittest.TestCase):
    def valid(self):
        return {'APP_ENV': 'production', 'AUTO_TRADE_MODE': 'live', 'KV_REST_API_URL': 'https://example.upstash.io',
                'KV_REST_API_TOKEN': 'write-test', 'KITE_TOKEN_ENCRYPTION_KEY': Fernet.generate_key().decode(),
                'APP_DOMAIN': 'app.example.com', 'FRONTEND_ORIGIN': 'https://app.example.com'}

    def test_live_mode_requires_real_storage_and_encryption(self):
        with self.assertRaises(RuntimeError):
            validate_live_settings({'AUTO_TRADE_MODE': 'live'})
        self.assertEqual(configuration_errors(self.valid()), [])

    def test_production_refuses_http_origins_and_invalid_risk_values(self):
        environment = {**self.valid(), 'FRONTEND_ORIGIN': 'http://example.com', 'TRADE_MAX_DAILY_LOSS': 'nan'}
        self.assertEqual(len(configuration_errors(environment)), 3)

    def test_production_refuses_domain_origin_mismatch(self):
        errors = configuration_errors({**self.valid(), 'FRONTEND_ORIGIN': 'https://other.example.com'})
        self.assertIn('FRONTEND_ORIGIN hostname must match APP_DOMAIN for the single-origin deployment.', errors)

    def test_other_deployments_cannot_satisfy_worker_health(self):
        first = self.valid()
        self.assertNotEqual(deployment_fingerprint(first), deployment_fingerprint({**first, 'AUTO_TRADE_MODE': 'paper'}))
        self.assertNotEqual(deployment_fingerprint(first), deployment_fingerprint({**first, 'DEPLOYMENT_ID': 'different'}))
        with patch.object(main, '_remote_session_store_enabled', return_value=True), patch.object(main, '_redis_request', return_value=None) as storage:
            self.assertFalse(main._trade_worker_online())
            storage.assert_called_once_with('GET', heartbeat_key())

    def test_production_origin_check_does_not_trust_local_websites(self):
        with patch.object(main, '_ALLOW_LOCAL_ORIGINS', False), patch.object(main, 'allowed_origins', ['https://app.example.com']):
            self.assertFalse(main._origin_allowed('http://localhost:5175'))
            self.assertTrue(main._origin_allowed('https://app.example.com'))


class LiveOrderTests(unittest.TestCase):
    def setUp(self):
        self.now = Clock.now()
        self.client = Mock(api_key='test-key')
        self.client.GTT_TYPE_SINGLE = 'single'
        self.client.positions.return_value = {'day': [], 'net': []}
        self.client.holdings.return_value = []
        self.client.orders.return_value = []
        self.client.get_gtts.return_value = []
        self.client.margins.return_value = {'available': {'live_balance': 100000}}
        self.client.quote.return_value = {'NSE:TEST': {'last_price': 99, 'last_trade_time': self.now}}
        self.client.place_gtt.return_value = {'trigger_id': 123}
        self.preview = {'id': 'a' * 32, 'account_id': 'ACCOUNT', 'session_id': None, 'created_at': self.now.timestamp(),
            'asOf': '2026-11-18', 'strategy': {'id': 'b' * 32},
            'candidates': [{'symbol': 'TEST', 'entry_price': 100, 'entry_limit_price': 100.5, 'stop_loss': 95,
                            'stop_limit_price': 94.5, 'target_price': 110, 'tick_size': .05}]}
        self.payload = main.TradeExecuteRequest(preview_id='a' * 32, symbols=['TEST'])
        self.request = Request({'type': 'http', 'method': 'POST', 'headers': [(b'origin', b'http://localhost:5173')]})
        self.reservations = set()
        self.intents = []

    def redis(self, method, key, *args, **kwargs):
        if key.startswith('autoinvest:preview:'):
            return json.dumps(self.preview)
        if method == 'SMEMBERS':
            return []
        return None

    def evaluate(self, script, keys, args):
        if "redis.call('EXISTS', KEYS[1])" in script:
            if keys[0] in self.reservations:
                return 0
            self.reservations.add(keys[0])
            self.intents.append(json.loads(args[1]))
        return 1

    def run_order(self, risk=None):
        payload = self.payload if risk is None else main.TradeExecuteRequest(preview_id='a' * 32, symbols=['TEST'], risk=risk)
        with patch.object(main, 'AUTO_TRADE_MODE', 'live'), patch.object(main, '_remote_session_store_enabled', return_value=True), patch.object(main, '_trade_worker_online', return_value=True), patch.object(main, '_redis_request', side_effect=self.redis), patch.object(main, '_redis_eval', side_effect=self.evaluate), patch.object(main, '_emit_trade_event'), patch.object(main, 'datetime', Clock), patch.object(main, 'time') as timer:
            timer.time.return_value = self.now.timestamp()
            return main._execute_trade_locked(payload, self.request, self.client, 'ACCOUNT', Mock())

    def test_real_gtt_contract_and_repeat_request_do_not_duplicate_entry(self):
        result = self.run_order()
        self.assertEqual(result['mode'], 'live')
        self.assertEqual(result['results'][0]['status'], 'submitted')
        self.assertEqual(self.intents[0]['stage'], 'SUBMITTING_ENTRY')
        self.assertEqual(self.client.place_gtt.call_args.args[:4], ('single', 'TEST', 'NSE', [100.0]))
        self.assertEqual(self.client.place_gtt.call_args.args[5][0]['price'], 100.5)
        self.assertEqual(self.run_order()['results'][0]['status'], 'skipped')
        self.client.place_gtt.assert_called_once()

    def test_timeout_is_reserved_and_never_blindly_retried(self):
        self.client.place_gtt.side_effect = TimeoutError('uncertain broker response')
        self.assertEqual(self.run_order()['results'][0]['status'], 'checking')
        self.assertEqual(self.run_order()['results'][0]['status'], 'skipped')
        self.client.place_gtt.assert_called_once()

    def test_server_risk_ceiling_cannot_be_bypassed_from_ui(self):
        risk = main.TradeRiskSettings(risk_per_trade=1001, max_daily_loss=2000)
        with self.assertRaises(HTTPException) as caught:
            self.run_order(risk)
        self.assertEqual(caught.exception.status_code, 422)
        self.client.place_gtt.assert_not_called()

    def test_insufficient_cash_submits_nothing(self):
        self.client.margins.return_value = {'available': {'live_balance': 1}}
        result = self.run_order()
        self.assertEqual(result['results'][0]['status'], 'skipped')
        self.client.place_gtt.assert_not_called()


class StateAndWorkerTests(unittest.TestCase):
    def test_confirmed_entry_fill_places_one_oco_and_completed_exit_is_archived(self):
        monitor = {'id': 'm', 'account_id': 'ACCOUNT', 'symbol': 'TEST', 'stage': 'ENTRY_PENDING', 'entry_gtt_id': 1, 'entry': 100, 'entry_limit': 100.5, 'quantity': 2, 'stop_loss': 95, 'target': 110}
        client = Mock()
        client.GTT_TYPE_OCO = 'two-leg'
        client.get_gtt.return_value = {'status': 'triggered', 'condition': {'exchange': 'NSE', 'tradingsymbol': 'TEST', 'trigger_values': [100]},
            'orders': [{'transaction_type': 'BUY', 'product': 'CNC', 'order_type': 'LIMIT', 'quantity': 2, 'price': 100.5, 'result': {'order_id': 'buy1'}}]}
        fill = {'status': 'COMPLETE', 'tradingsymbol': 'TEST', 'exchange': 'NSE', 'product': 'CNC', 'transaction_type': 'BUY', 'quantity': 2, 'filled_quantity': 2}
        client.order_history.return_value = [fill]
        client.ltp.return_value = {'NSE:TEST': {'last_price': 101}}
        client.place_gtt.return_value = {'trigger_id': 2}
        with patch.object(trade_worker, '_redis_request'), patch.object(trade_worker, '_load_monitor', return_value=monitor), patch.object(trade_worker, '_save_monitor'), patch.object(trade_worker, '_record_fill_day'), patch.object(trade_worker, '_event'), patch.object(trade_worker, '_remove_monitor') as archive:
            trade_worker._process_monitor('m', Mock(), client)
            self.assertEqual(monitor['stage'], 'EXIT_PROTECTED')
            self.assertEqual(client.place_gtt.call_args.args[:4], ('two-leg', 'TEST', 'NSE', [95, 110]))
            self.assertTrue(all(o['quantity'] == 2 and o['transaction_type'] == 'SELL' for o in client.place_gtt.call_args.args[5]))
            monitor['next_check_at'] = 0
            client.get_gtt.return_value = {'status': 'triggered', 'condition': {'exchange': 'NSE', 'tradingsymbol': 'TEST', 'trigger_values': [95, 110]},
                'orders': [{**o, 'result': {'order_id': 'sell1'} if index == 0 else None} for index, o in enumerate(client.place_gtt.call_args.args[5])]}
            client.order_history.return_value = [{**fill, 'transaction_type': 'SELL'}]
            trade_worker._process_monitor('m', Mock(), client)
            archive.assert_called_once()
        client.place_gtt.assert_called_once()

    def test_modified_broker_gtt_stops_before_processing_or_submitting_orders(self):
        monitor = {'id': 'm', 'account_id': 'ACCOUNT', 'symbol': 'TEST', 'stage': 'ENTRY_PENDING', 'entry_gtt_id': 1, 'entry': 100, 'entry_limit': 100.5, 'quantity': 1}
        client = Mock()
        client.get_gtt.return_value = {'status': 'triggered', 'condition': {'exchange': 'NSE', 'tradingsymbol': 'OTHER', 'trigger_values': [100]},
                                     'orders': [{'transaction_type': 'BUY', 'product': 'CNC', 'order_type': 'LIMIT', 'quantity': 1, 'price': 100.5}]}
        with patch.object(trade_worker, '_redis_request'), patch.object(trade_worker, '_load_monitor', return_value=monitor), patch.object(trade_worker, '_save_monitor'), patch.object(trade_worker, '_event'):
            trade_worker._process_monitor('m', Mock(), client)
        self.assertEqual(monitor['stage'], 'MANUAL_REVIEW')
        client.order_history.assert_not_called()
        client.place_gtt.assert_not_called()

    def test_worker_never_touches_another_deployments_monitor(self):
        monitor = {'owner': 'other-deployment', 'stage': 'ENTRY_PENDING'}
        client = Mock()
        with patch.dict(os.environ, {'DEPLOYMENT_ID': 'this-deployment'}), patch.object(trade_worker, '_redis_request'), patch.object(trade_worker, '_load_monitor', return_value=monitor):
            trade_worker._process_monitor('m', Mock(), client)
        self.assertEqual(client.mock_calls, [])

    def test_successful_but_unmatched_broker_reads_have_a_retry_budget(self):
        monitor = {'id': 'm', 'account_id': 'ACCOUNT', 'symbol': 'TEST', 'stage': 'SUBMISSION_UNKNOWN', 'entry': 100, 'quantity': 1, 'created_at': 1}
        client = Mock()
        client.get_gtts.return_value = []
        with patch.object(trade_worker, '_redis_request'), patch.object(trade_worker, '_load_monitor', return_value=monitor), patch.object(trade_worker, '_save_monitor'), patch.object(trade_worker, '_event'):
            for _ in range(6):
                monitor['next_check_at'] = 0
                trade_worker._process_monitor('m', Mock(), client)
            self.assertEqual(monitor['stage'], 'MANUAL_REVIEW')
            trade_worker._process_monitor('m', Mock(), client)
        self.assertEqual(client.get_gtts.call_count, 6)
        client.place_gtt.assert_not_called()

    def test_stale_order_update_is_rejected(self):
        monitor = {'id': 'm', 'account_id': 'ACCOUNT', '_revision': 3}
        with self.assertRaises(StateConflict):
            save_monitor(Mock(return_value=0), monitor)
        self.assertEqual(monitor['_revision'], 3)

    def test_history_excludes_credentials_and_retains_individual_archive(self):
        evaluate = Mock(return_value=1)
        archive_monitor(evaluate, {'id': 'm', 'account_id': 'ACCOUNT', 'symbol': 'TEST', 'api_key': 'private-key', 'session_id': 'private-session'})
        script, keys, args = evaluate.call_args.args
        self.assertIn('autoinvest:archive:ACCOUNT:m', keys)
        self.assertNotIn('private-key', args[2])
        self.assertNotIn('private-session', args[2])

    def test_repeated_broker_errors_back_off_and_stop_at_manual_review(self):
        monitor = {'id': 'm', 'account_id': 'ACCOUNT', 'symbol': 'TEST', 'stage': 'ENTRY_PENDING', 'entry_gtt_id': 1}
        client = Mock()
        client.get_gtt.side_effect = RuntimeError('broker unavailable')
        with patch.object(trade_worker, '_redis_request'), patch.object(trade_worker, '_load_monitor', return_value=monitor), patch.object(trade_worker, '_save_monitor'), patch.object(trade_worker, '_event'):
            for _ in range(6):
                monitor['next_check_at'] = 0
                trade_worker._process_monitor('m', Mock(), client)
            self.assertEqual(monitor['stage'], 'MANUAL_REVIEW')
            self.assertGreater(monitor['next_check_at'], monitor['last_error_at'])
            trade_worker._process_monitor('m', Mock(), client)
        self.assertEqual(client.get_gtt.call_count, 6)

    def test_fill_day_is_reserved_even_if_entry_was_submitted_earlier(self):
        monitor = {'id': 'm', 'account_id': 'ACCOUNT', 'symbol': 'TEST'}
        with patch.object(trade_worker, '_redis_request') as storage:
            trade_worker._record_fill_day(monitor, {'exchange_update_timestamp': '2026-11-18 10:00:00'})
        self.assertIn('2026-11-18:TEST', storage.call_args.args[1])

    def test_expired_untriggered_entry_is_cancelled_then_verified_before_archival(self):
        monitor = {'id': 'm', 'account_id': 'ACCOUNT', 'symbol': 'TEST', 'stage': 'ENTRY_PENDING', 'entry_gtt_id': 1, 'entry_expires_at': 1, 'entry': 100, 'entry_limit': 100.5, 'quantity': 1}
        client = Mock()
        trigger = {'condition': {'exchange': 'NSE', 'tradingsymbol': 'TEST', 'trigger_values': [100]}, 'orders': [{'transaction_type': 'BUY', 'product': 'CNC', 'order_type': 'LIMIT', 'quantity': 1, 'price': 100.5}]}
        client.get_gtt.side_effect = [{**trigger, 'status': 'active'}, {**trigger, 'status': 'deleted'}]
        with patch.object(trade_worker, '_redis_request'), patch.object(trade_worker, '_load_monitor', return_value=monitor), patch.object(trade_worker, '_save_monitor'), patch.object(trade_worker, '_remove_monitor') as archive, patch.object(trade_worker, '_event'):
            trade_worker._process_monitor('m', Mock(), client)
            client.delete_gtt.assert_called_once_with(1)
            archive.assert_not_called()
            monitor['next_check_at'] = 0
            trade_worker._process_monitor('m', Mock(), client)
            archive.assert_called_once()


class SdkAndParserTests(unittest.TestCase):
    def test_official_sdk_session_checksum_contract(self):
        client = KiteConnect(api_key='key')
        with patch.object(client, '_post', return_value={'access_token': 'token', 'login_time': '2026-11-18 10:00:00'}) as post:
            client.generate_session('request', api_secret='secret')
        checksum = hashlib.sha256(b'keyrequestsecret').hexdigest()
        self.assertEqual(post.call_args.kwargs['params']['checksum'], checksum)

    def test_vendor_source_matches_provenance(self):
        root = Path('backend/vendor/kite_rest')
        manifest = json.loads((root / 'provenance.json').read_text())
        for item in manifest['files']:
            content = (root / item['file']).read_text(encoding='utf-8').replace('\r\n', '\n').rstrip() + '\n'
            self.assertEqual(hashlib.sha256(content.encode()).hexdigest(), item['vendored_sha256'])

    def test_ticks_are_bounded_and_only_subscribed_prices_are_returned(self):
        token = 25601
        packet = struct.pack('>HHII', 1, 8, token, 12345)
        self.assertEqual(parse_ticks(packet, {token}), [{'instrument_token': token, 'last_price': 123.45}])
        self.assertEqual(parse_ticks(packet, set()), [])
        for malformed in (struct.pack('>H', 101), packet[:-1], packet + b'extra'):
            with self.assertRaises(ValueError):
                parse_ticks(malformed, {token})


if __name__ == '__main__':
    unittest.main()
