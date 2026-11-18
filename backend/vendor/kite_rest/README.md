# Kite REST SDK provenance

This directory contains the REST client, exception types, version metadata, and MIT license from the official `kiteconnect` 5.2.2 Python distribution.

Upstream: https://github.com/zerodha/pykiteconnect

The sole functional SDK change is `import kiteconnect.exceptions as ex` → `from . import exceptions as ex`, which isolates the REST SDK from the package initializer and its legacy Autobahn transport. No order, session-exchange, or request-signing implementation has been replaced. Original and vendored source hashes are recorded in `provenance.json` using normalized UTF-8/LF content.

The production dependency set includes the REST SDK's Requests, python-dateutil, and six dependencies. It does not install kiteconnect's legacy Autobahn/Twisted transport. Live ticks use the maintained `websockets` transport and the bounded parser in `backend/tick_parser.py`.

On SDK upgrades, review the upstream diff, re-apply the isolated import, update the version/license/provenance, run the SDK contract and trading-state tests, and rerun the application runtime dependency advisory audit. Vendoring moves this update responsibility into the repository; it is not a substitute for ongoing advisory review.
