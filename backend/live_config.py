"""Deployment checks and server-enforced limits for real order execution."""
import hashlib
import math
import os
from urllib.parse import urlparse

from cryptography.fernet import Fernet


def configuration_errors(environment=None):
    env = os.environ if environment is None else environment
    errors = []
    mode = env.get('AUTO_TRADE_MODE', 'paper').strip().lower()
    production = env.get('APP_ENV', 'development').strip().lower() == 'production'
    if mode not in {'paper', 'live', 'disabled'}:
        errors.append('AUTO_TRADE_MODE must be live, paper, or disabled.')
    if mode == 'live' or production:
        endpoint = env.get('UPSTASH_REDIS_REST_URL') or env.get('KV_REST_API_URL', '')
        parsed = urlparse(endpoint)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            errors.append('Set a valid HTTPS Redis REST URL.')
        if not (env.get('UPSTASH_REDIS_REST_TOKEN') or env.get('KV_REST_API_TOKEN')):
            errors.append('Set the Redis REST write token.')
        try:
            Fernet(env.get('KITE_TOKEN_ENCRYPTION_KEY', '').encode())
        except (ValueError, TypeError):
            errors.append('Set a valid, stable KITE_TOKEN_ENCRYPTION_KEY.')
    if production:
        try:
            origin = urlparse(env.get('FRONTEND_ORIGIN', ''))
            origin_hostname = origin.hostname
            invalid_origin = (origin.scheme != 'https' or not origin_hostname or origin.username or origin.password
                              or origin.path not in {'', '/'} or origin.query or origin.fragment or origin.port not in {None, 443})
        except ValueError:
            origin_hostname, invalid_origin = None, True
        if invalid_origin:
            errors.append('FRONTEND_ORIGIN must be the exact HTTPS frontend origin.')
        domain = env.get('APP_DOMAIN', '').strip().lower()
        try:
            parsed_domain = urlparse('//' + domain)
            invalid_domain = (not domain or parsed_domain.hostname != domain or parsed_domain.port is not None
                              or '/' in domain or '.' not in domain)
        except ValueError:
            invalid_domain = True
        if invalid_domain:
            errors.append('APP_DOMAIN must be a public hostname without a scheme or path.')
        elif origin_hostname != domain:
            errors.append('FRONTEND_ORIGIN hostname must match APP_DOMAIN for the single-origin deployment.')
    for name, default in POLICY_DEFAULTS.items():
        try:
            value = float(env.get(name, default))
            if not math.isfinite(value) or value <= 0 or value > 10000000:
                raise ValueError()
            if name == 'TRADE_MAX_POSITIONS' and (not value.is_integer() or value > 50):
                raise ValueError()
        except (ValueError, TypeError):
            errors.append(f'{name} must be a finite positive limit.')
    return errors


POLICY_DEFAULTS = {
    'TRADE_MAX_RISK_PER_TRADE': 1000,
    'TRADE_MAX_DAILY_LOSS': 3000,
    'TRADE_MAX_POSITION_VALUE': 25000,
    'TRADE_MAX_POSITIONS': 5,
    'TRADE_MAX_ACCOUNT_EXPOSURE': 100000,
}


def risk_policy(environment=None):
    env = os.environ if environment is None else environment
    return {name: float(env.get(name, default)) for name, default in POLICY_DEFAULTS.items()}


def validate_live_settings(environment=None):
    errors = configuration_errors(environment)
    if errors:
        raise RuntimeError(' '.join(errors))


def deployment_fingerprint(environment=None):
    env = os.environ if environment is None else environment
    # A worker with a different encryption key, host, or execution mode cannot
    # accidentally satisfy this API's readiness check. Never expose the key.
    values = ['execution-protocol-2', env.get('APP_ENV', 'development'), env.get('DEPLOYMENT_ID', 'default'),
              env.get('FRONTEND_ORIGIN', ''), env.get('AUTO_TRADE_MODE', 'paper'),
              env.get('KITE_TOKEN_ENCRYPTION_KEY', '')]
    return hashlib.sha256('|'.join(values).encode()).hexdigest()[:24]


def heartbeat_key():
    return f'autoinvest:worker:{deployment_fingerprint()}:heartbeat'
