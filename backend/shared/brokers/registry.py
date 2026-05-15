"""
Broker registry — routes an account to its `Broker` adapter.

Canonical broker_id values are stored in `broker_accounts.broker_id`
(DB-backed, editable via /admin/brokers). The registry reads the value
from the `Connections` singleton's `_broker_id_map` cache (populated
during `rebuild_from_db`) so hot-path calls never hit the DB.

Legacy / YAML-seeded rows that carry `broker: "kite"` still work —
`_ADAPTERS` maps both `"kite"` (legacy) and `"zerodha_kite"` (canonical)
to the same `KiteBroker` adapter. Adding a new vendor: add an adapter
class + one entry in `_ADAPTERS`.
"""

from __future__ import annotations

from backend.shared.brokers.base import Broker
from backend.shared.brokers.kite import KiteBroker
from backend.shared.helpers.connections import Connections


# Broker id → adapter class. Both "zerodha_kite" (canonical, stored in
# broker_accounts.broker_id) and "kite" (legacy YAML value) map to
# KiteBroker so existing rows remain compatible after the column was added.
# Extend here when a new vendor adapter lands — e.g. "upstox": UpstoxBroker.
_ADAPTERS: dict[str, type[Broker]] = {
    "zerodha_kite": KiteBroker,
    "kite": KiteBroker,         # legacy alias — YAML-seeded rows use "kite"
}


def _broker_id_for(account: str) -> str:
    """Canonical broker_id for a given account.

    Resolution order:
    1. Connections._broker_id_map — populated from broker_accounts.broker_id
       during rebuild_from_db (DB-authoritative, no extra query needed).
    2. secrets.yaml kite_accounts[account].broker — legacy fallback for
       accounts that were seeded before the DB-backed broker_id existed.
    3. "zerodha_kite" — safe default (every account today is Kite).
    """
    conns = Connections()
    # Step 1 — DB-backed cache (populated by rebuild_from_db).
    id_map: dict[str, str] = getattr(conns, "_broker_id_map", {})
    if account in id_map:
        return id_map[account]
    # Step 2 — YAML fallback.
    try:
        from backend.shared.helpers.utils import secrets
        accts = secrets.get("kite_accounts") or {}
        yaml_val = (accts.get(account) or {}).get("broker")
        if yaml_val:
            return str(yaml_val)
    except Exception:
        pass
    # Step 3 — default.
    return "zerodha_kite"


def get_broker(account: str) -> Broker:
    """
    Return the `Broker` adapter for `account`. Under the hood this
    asks the `Connections` singleton for the per-account client and
    wraps it in the right adapter class. Calling this on a hot path
    is fine — no re-auth happens here, and adapter construction is a
    two-attribute object that reuses the cached connection.
    """
    conn = Connections().conn.get(account)
    if conn is None:
        raise KeyError(f"No broker client configured for account {account!r}")
    broker_id = _broker_id_for(account)
    adapter_cls = _ADAPTERS.get(broker_id)
    if adapter_cls is None:
        raise ValueError(
            f"Account {account!r} is tagged broker={broker_id!r} but no "
            f"adapter is registered. Add it under "
            f"backend/shared/brokers/{broker_id}.py and register in "
            f"_ADAPTERS in this file."
        )
    # KiteBroker expects a KiteConnection. Future adapters may expect a
    # different client type — that's wrapped in the same dict today
    # because every account is Kite, but when a second vendor lands the
    # `Connections` class should hold broker-specific clients keyed by
    # account and this line will pass the right type through.
    return adapter_cls(conn)


def all_brokers() -> list[Broker]:
    """Every configured broker adapter, one per account."""
    return [get_broker(acct) for acct in Connections().conn.keys()]


def get_price_broker() -> Broker:
    """
    Adapter for shared market-data fetches (underlying spots, historical
    candles, instrument lookups) where any account works because the data
    isn't account-scoped. Reads the `connections.price_account` setting:
    if set and valid, uses that; otherwise falls back to the first
    available account in `secrets.yaml`.

    Lets the operator centralize "which Kite handle do we hammer for
    chart data" in /admin/settings instead of having that decision baked
    into the calling code.
    """
    from backend.shared.helpers.settings import get_string

    accounts = list(Connections().conn.keys())
    if not accounts:
        raise KeyError("No broker accounts configured.")

    pinned = get_string("connections.price_account", "") or ""
    pinned = pinned.strip()
    if pinned and pinned in accounts:
        return get_broker(pinned)
    return get_broker(accounts[0])
