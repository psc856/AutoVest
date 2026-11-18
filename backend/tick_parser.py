"""Bounded LTP decoding from Kite's documented binary market-data protocol."""
import struct


def parse_ticks(payload, subscribed):
    if len(payload) < 2:
        return []
    count = struct.unpack_from('>H', payload)[0]
    if count > 100 or len(payload) > 262144:
        raise ValueError('Tick frame exceeds subscription limits')
    offset, ticks = 2, []
    for _ in range(count):
        if offset + 2 > len(payload):
            raise ValueError('Truncated tick length')
        length = struct.unpack_from('>H', payload, offset)[0]
        offset += 2
        if length not in {8, 28, 32, 44, 184} or offset + length > len(payload):
            raise ValueError('Invalid tick packet')
        token, price = struct.unpack_from('>II', payload, offset)
        if token in subscribed:
            segment = token & 0xff
            divisor = 10000000 if segment == 3 else 10000 if segment == 6 else 100
            ticks.append({'instrument_token': token, 'last_price': price / divisor})
        offset += length
    if offset != len(payload):
        raise ValueError('Unexpected tick frame content')
    return ticks
