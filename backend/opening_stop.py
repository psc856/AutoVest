"""Plan regular protective exits from broker state, without placing orders."""

from datetime import datetime, time
from decimal import Decimal, ROUND_FLOOR
from zoneinfo import ZoneInfo

IST = ZoneInfo('Asia/Kolkata')
OPEN = time(9, 15)
CUTOFF = time(15, 20)
ACTIVE = {'waiting', 'running'}
TERMINAL_ORDERS = {'COMPLETE', 'CANCELLED', 'REJECTED'}


def market_time():
    return datetime.now(IST)


def stock_key(item):
    return f"{item['exchange']}:{item['tradingsymbol']}"


def is_intraday_equity(item):
    return item.get('product') == 'MIS' and item.get('exchange') in {'NSE', 'BSE'}


def price(value):
    number = Decimal(str(value))
    if not number.is_finite() or number <= 0:
        raise ValueError('Invalid broker price')
    return number


def round_down(value, tick):
    return (value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def plan(positions, orders, ticks, scope=None):
    """Return one action per symbol. An outstanding sell reserves its quantity."""
    rows, actions = [], []
    sells = {}
    for order in orders:
        if (is_intraday_equity(order) and order.get('transaction_type') == 'SELL'
                and order.get('status') not in TERMINAL_ORDERS):
            sells.setdefault(stock_key(order), []).append(order)
    seen = set()
    for position in positions:
        if not is_intraday_equity(position) or int(position['quantity']) <= 0:
            continue
        key = stock_key(position)
        if scope is not None and key not in scope:
            continue
        if key in seen:
            raise ValueError('Duplicate broker position')
        seen.add(key)
        quantity = int(position['quantity'])
        average = price(position['average_price'])
        existing = sells.get(key, [])
        stops = [order for order in existing if order.get('order_type') in {'SL', 'SL-M'}]
        row = {
            'key': key, 'quantity': quantity, 'average_price': float(average),
            'investment': float(average * quantity), 'trigger': None, 'covered': 0,
            'stops': [{'id': o['order_id'], 'type': o['order_type'], 'trigger': o.get('trigger_price'),
                       'quantity': o.get('pending_quantity'), 'status': o.get('status')} for o in stops],
            'status': 'Awaiting protection',
        }
        rows.append(row)
        if key not in ticks:
            row['status'] = 'Instrument tick size unavailable'
            continue
        tick = ticks[key]
        trigger = round_down(average * Decimal('0.95'), tick)
        row['trigger'] = float(trigger)
        if trigger <= 0:
            row['status'] = 'Trigger is below minimum tick'
            continue
        if any(o.get('status') in {'OPEN PENDING', 'MODIFY PENDING', 'MODIFY VALIDATION PENDING',
                                    'VALIDATION PENDING', 'PUT ORDER REQ RECEIVED', 'CANCEL PENDING'}
               for o in existing):
            row['status'] = 'Waiting for Kite order update'
            continue
        if any(o.get('status') != 'TRIGGER PENDING' or o.get('variety') != 'regular'
               or o.get('validity') != 'DAY' or o.get('parent_order_id') or int(o.get('filled_quantity') or 0)
               for o in stops) or len(existing) != len(stops):
            row['status'] = 'Review competing or already triggered sell in Kite'
            continue
        reserved = sum(int(o['pending_quantity']) for o in stops)
        if any(int(o['pending_quantity']) <= 0 for o in stops) or reserved > quantity:
            row['status'] = 'Review existing sell quantity in Kite'
            continue
        row['covered'] = sum(int(o['pending_quantity']) for o in stops if price(o['trigger_price']) == trigger)
        if row['covered'] == quantity:
            row['status'] = 'Protection confirmed'
            continue
        action = {'key': key, 'exchange': position['exchange'], 'tradingsymbol': position['tradingsymbol'],
                  'trigger_price': float(trigger)}
        change = next((o for o in stops if price(o['trigger_price']) != trigger), None)
        if change:
            action.update(order_id=change['order_id'], order_type=change['order_type'],
                          quantity=int(change['pending_quantity']))
            if change['order_type'] == 'SL':
                gap = price(change['trigger_price']) - price(change['price'])
                limit = round_down(trigger - gap, tick)
                if gap < 0 or limit <= 0:
                    row['status'] = 'Existing SL limit needs manual review'
                    continue
                action['price'] = float(limit)
        else:
            action.update(order_type='SL-M', quantity=quantity - reserved)
        actions.append(action)
    return rows, actions


def fresh_quote(quote, now, trigger):
    if not quote or not isinstance(quote.get('last_trade_time'), datetime):
        return False
    stamp = quote['last_trade_time']
    stamp = stamp.replace(tzinfo=IST) if stamp.tzinfo is None else stamp.astimezone(IST)
    return (stamp.date() == now.date() and stamp.time() >= OPEN
            and 0 <= (now - stamp).total_seconds() <= 10 and price(quote['last_price']) > price(trigger))
