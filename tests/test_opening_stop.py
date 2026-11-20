from datetime import datetime
import time
from decimal import Decimal
from threading import Event
from unittest import TestCase
from unittest.mock import Mock, patch

from backend.opening_stop import IST, fresh_quote, plan
from backend.opening_worker import reconcile, run_cycle, send

NOW = datetime(2026, 11, 23, 9, 15, 0, 300000, tzinfo=IST)
TICKS = {'NSE:ABC': Decimal('0.05')}
POSITION = {'exchange': 'NSE', 'tradingsymbol': 'ABC', 'product': 'MIS',
            'quantity': 100000, 'average_price': 101.03}
STOP = {'exchange': 'NSE', 'tradingsymbol': 'ABC', 'product': 'MIS',
        'transaction_type': 'SELL', 'status': 'TRIGGER PENDING', 'variety': 'regular',
        'validity': 'DAY', 'order_id': '1', 'order_type': 'SL-M',
        'trigger_price': 95.95, 'pending_quantity': 40000, 'filled_quantity': 0}


class OpeningStopTests(TestCase):
    def test_targets_uncovered_net_quantity_and_uses_broker_slicing(self):
        rows, actions = plan([POSITION], [STOP], TICKS)
        self.assertEqual(rows[0]['covered'], 40000)
        self.assertEqual(rows[0]['investment'], 10103000)
        action, = actions
        client = Mock()
        client.place_autoslice_order.return_value = {
            'order_id': 'parent', 'children': [{'order_id': '2'}, {'error': {'message': 'Rejected'}}],
        }
        self.assertEqual(send(client, {'action': action, 'tag': 'osABC'}), ['2'])
        params = client.place_autoslice_order.call_args.kwargs
        self.assertEqual(params['quantity'], 60000)
        self.assertEqual(params['market_protection'], -1)
        self.assertEqual(params['order_type'], 'SL-M')

    def test_modifies_matching_stop_in_place_and_keeps_sl_limit_gap(self):
        stop = dict(STOP, order_type='SL', trigger_price=96, price=95.5)
        _, actions = plan([POSITION], [stop], TICKS)
        action, = actions
        self.assertEqual(action['price'], 95.45)
        client = Mock()
        send(client, {'action': action})
        self.assertNotIn('quantity', client.modify_order.call_args.kwargs)
        client.place_autoslice_order.assert_not_called()

    def test_competing_sell_or_oversized_stop_blocks_submission(self):
        for stop in (dict(STOP, status='OPEN', order_type='LIMIT'),
                     dict(STOP, pending_quantity=100001)):
            rows, actions = plan([POSITION], [stop], TICKS)
            self.assertIn('Review', rows[0]['status'])
            self.assertEqual(actions, [])

    def test_uncertain_write_requires_all_slices_to_be_confirmed(self):
        _, actions = plan([POSITION], [STOP], TICKS)
        intent = {'action': actions[0], 'tag': 'osABC', 'ids': ['2', '3']}
        first = dict(STOP, order_id='2', pending_quantity=35000)
        second = dict(STOP, order_id='3', pending_quantity=25000)
        self.assertFalse(reconcile(intent, [first]))
        self.assertFalse(reconcile(intent, [first, dict(second, status='OPEN')]))
        self.assertTrue(reconcile(intent, [first, second]))

    def test_no_write_before_open_and_confirmed_run_ends(self):
        client = Mock()
        client.positions.return_value = {'net': [POSITION]}
        client.orders.return_value = [dict(STOP, pending_quantity=100000)]
        job = {'state': 'waiting', 'pending': {}}
        with patch('backend.opening_worker.tick_sizes', return_value=TICKS), \
             patch('backend.opening_worker.save_job'), \
             patch('backend.opening_worker.market_time') as clock:
            clock.return_value = NOW.replace(hour=9, minute=14)
            run_cycle(client, job, Mock(), Mock(), Mock(), Event(), Mock())
            self.assertEqual(job['state'], 'waiting')
            clock.return_value = NOW
            run_cycle(client, job, Mock(), Mock(), Mock(), Event(), Mock())
            self.assertEqual(job['state'], 'complete')
        client.quote.assert_not_called()
        client.place_autoslice_order.assert_not_called()

    def test_old_trades_and_price_already_through_trigger_are_blocked(self):
        self.assertFalse(fresh_quote({'last_trade_time': NOW.replace(minute=14), 'last_price': 101}, NOW, 95))
        self.assertFalse(fresh_quote({'last_trade_time': NOW, 'last_price': 94}, NOW, 95))
        self.assertTrue(fresh_quote({'last_trade_time': NOW, 'last_price': 101}, NOW, 95))

    def test_pending_intent_survives_restart_and_is_never_resubmitted(self):
        _, actions = plan([POSITION], [], TICKS)
        action, = actions
        client = Mock()
        client.positions.return_value = {'net': [POSITION]}
        client.orders.return_value = []
        executor = Mock()
        job = {'state': 'running', 'scope': [action['key']], 'deadline': time.time() + 60,
               'pending': {action['key']: {'action': action, 'tag': 'osABC',
                                            'at': time.time() - 15, 'attempt_at': None}},
               'issues': {}}
        with patch('backend.opening_worker.tick_sizes', return_value=TICKS), \
             patch('backend.opening_worker.save_job'), \
             patch('backend.opening_worker.market_time', return_value=NOW):
            run_cycle(client, job, Mock(), Mock(), Mock(), Event(), executor)
        self.assertIn(action['key'], job['pending'])
        self.assertIn('Could not confirm', job['rows'][0]['status'])
        executor.submit.assert_not_called()
