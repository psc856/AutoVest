"""Small, independently testable guards shared by API and order worker."""
import math
import secrets
import json
import os
import tempfile
from pathlib import Path


def atomic_json_write(path: Path, value) -> None:
    """Create private replacement files before atomically replacing stored state."""
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def finite_number(value, label="Broker value") -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number.")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number.")
    return result


def position_size(risk, value_cap, entry_limit, stop_limit) -> tuple[int, float]:
    risk, value_cap, entry_limit, stop_limit = [finite_number(v) for v in (risk, value_cap, entry_limit, stop_limit)]
    if not 0 < stop_limit < entry_limit or min(risk, value_cap) <= 0:
        raise ValueError("Invalid trade sizing inputs.")
    per_share = entry_limit - stop_limit
    quantity = max(0, min(int(risk // per_share), int(value_cap // entry_limit)))
    return quantity, per_share * quantity


class LeaseBusy(RuntimeError):
    pass


class LeaseLost(RuntimeError):
    pass


class RedisLease:
    """Owner-checked leases; broker writes require a renewed lease immediately beforehand."""
    def __init__(self, command, evaluate, key, ttl=120):
        self.command, self.evaluate, self.key, self.ttl = command, evaluate, key, ttl
        self.owner = secrets.token_hex(24)

    def __enter__(self):
        if self.command("SET", self.key, self.owner, ttl=self.ttl, nx=True) != "OK":
            raise LeaseBusy("Another operation is managing this account. Please retry shortly.")
        return self

    def renew(self):
        result = self.evaluate("if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('EXPIRE', KEYS[1], ARGV[2]) else return 0 end", [self.key], [self.owner, str(self.ttl)])
        if result != 1:
            raise LeaseLost("Account operation lease expired. No further broker requests are allowed.")

    def __exit__(self, *args):
        try:
            self.evaluate("if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end", [self.key], [self.owner])
        except Exception:
            pass  # Expiry is a fallback; never delete another owner's lease.
