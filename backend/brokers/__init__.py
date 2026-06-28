"""
Broker abstraction layer.

Every broker-specific client lives behind the `Broker` interface defined
in `base.py`. The rest of the codebase (routes, background tasks, agent
engine, actions, the simulator) asks for a `Broker` via the registry
and never imports broker-specific SDKs directly — so adding a second
broker (Upstox, Angel One, Fyers, Dhan…) is "implement Broker, register
it" and nothing else changes.

Public API:

    from backend.brokers import Broker, get_broker, all_brokers

    broker = get_broker("ZG0790")       # Broker for that account
    broker.ltp(["NSE:NIFTY 50"])        # broker-agnostic call
    for b in all_brokers():             # every configured broker
        b.holdings()

Adding a new broker:
  1. Create `backend/brokers/adapters/<name>.py` with a class that
     implements every method of `Broker` (see base.py) for that
     vendor's SDK.
  2. Register it in `registry.py` under the broker identifier you
     pick (e.g. "upstox", "angel_one"). Set broker_accounts.broker_id
     to that value for the new account via /admin/brokers; the registry
     reads from there (DB-authoritative) and routes to the adapter.

Canonical broker_id values:
  "zerodha_kite"  — Zerodha Kite Connect (the only adapter today)
  "kite"          — legacy alias for "zerodha_kite" (YAML-seeded rows)
"""

from backend.brokers.base     import Broker
from backend.brokers.registry import get_broker, all_brokers, get_price_broker

__all__ = ["Broker", "get_broker", "all_brokers", "get_price_broker"]
