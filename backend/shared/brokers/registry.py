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

from typing import Any

from backend.shared.brokers.base import Broker
from backend.shared.brokers.dhan import DhanBroker
from backend.shared.brokers.kite import KiteBroker
from backend.shared.helpers.connections import Connections
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# Broker id → adapter class. Both "zerodha_kite" (canonical, stored in
# broker_accounts.broker_id) and "kite" (legacy YAML value) map to
# KiteBroker so existing rows remain compatible after the column was added.
# Extend here when a new vendor adapter lands — e.g. "upstox": UpstoxBroker.
_ADAPTERS: dict[str, type[Broker]] = {
    "zerodha_kite": KiteBroker,
    "kite":         KiteBroker,  # legacy alias — YAML-seeded rows use "kite"
    "dhan":         DhanBroker,
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


class PriceBroker(Broker):
    """
    Auto-failover wrapper for shared market-data fetches.

    Wraps a preference-ordered list of underlying `Broker` adapters and
    transparently falls over to the next one when the current one fails.
    `quote()` / `ltp()` / `historical_data()` / `instruments()` /
    `holidays()` all benefit — they're broker-agnostic operations (same
    response regardless of which account makes the call), so a Kite
    rate-limit / token-expiry / network blip doesn't have to abort a
    chart render or a market-data refresh as long as ANY other broker
    is reachable.

    Account-specific operations (holdings / positions / orders / trades
    / place_order / modify_order / cancel_order / margins /
    basket_order_margins / profile) raise NotImplementedError — those
    must target a known account via `get_broker(account)`.

    Industry analogue: PagerDuty's "service team failover," Cloudflare
    Load Balancer's origin pool failover. Same pattern, applied to
    broker-level market-data calls.
    """

    def __init__(self, brokers: list[Broker]):
        if not brokers:
            raise ValueError("PriceBroker requires at least one underlying broker")
        self._brokers = brokers
        self._last_used: str | None = None  # observability: which broker served the last call

    @property
    def account(self) -> str:
        # Returns the first broker's account so callers logging
        # broker.account get a stable value. Use last_served_by() to
        # see which broker actually served the most recent call.
        return self._brokers[0].account

    @property
    def broker_id(self) -> str:
        return "price-broker-fallback"

    def last_served_by(self) -> str | None:
        """Which underlying broker served the most recent market-data
        call. None until the first call lands."""
        return self._last_used

    def underlying_count(self) -> int:
        return len(self._brokers)

    def _try(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        """Walk every underlying broker in preference order; return the
        first non-exception result. If every broker fails, re-raise the
        last exception so the caller sees a real diagnostic."""
        last_exc: Exception | None = None
        for broker in self._brokers:
            try:
                result = getattr(broker, method_name)(*args, **kwargs)
                self._last_used = f"{broker.broker_id}/{broker.account}"
                return result
            except Exception as e:
                last_exc = e
                logger.warning(
                    f"PriceBroker fallback: {method_name} failed on "
                    f"{broker.broker_id}/{broker.account}: {str(e)[:160]}"
                )
                continue
        # Every broker failed — surface the LAST exception so the
        # operator's log shows a real broker error, not a generic
        # 'all brokers failed' wrapper.
        raise last_exc if last_exc else RuntimeError(
            f"PriceBroker: no brokers available for {method_name}"
        )

    # ── Market-data methods — fall over across brokers ────────────────

    def quote(self, symbols: list[str]) -> dict:
        return self._try("quote", symbols)

    def ltp(self, symbols: list[str]) -> dict:
        return self._try("ltp", symbols)

    def historical_data(self, instrument_token: int, from_date: Any,
                        to_date: Any, interval: str = "day") -> list[dict]:
        return self._try("historical_data", instrument_token, from_date,
                          to_date, interval)

    def instruments(self, exchange: str | None = None) -> list[dict]:
        return self._try("instruments", exchange)

    def holidays(self, exchange: str) -> set[str]:
        return self._try("holidays", exchange)

    # ── Account-specific methods — DO NOT fall over ───────────────────
    # These would return different data per broker (one account's
    # holdings ≠ another's), so fallback is semantically wrong. Raise
    # so the caller is forced to use `get_broker(account)` instead.

    def profile(self) -> dict:
        raise NotImplementedError(
            "PriceBroker.profile() is account-specific — use get_broker(account).profile() instead."
        )

    def holdings(self) -> list[dict]:
        raise NotImplementedError(
            "PriceBroker.holdings() is account-specific — use get_broker(account).holdings() instead."
        )

    def positions(self) -> dict:
        raise NotImplementedError(
            "PriceBroker.positions() is account-specific — use get_broker(account).positions() instead."
        )

    def margins(self, segment: str | None = None) -> dict:
        raise NotImplementedError(
            "PriceBroker.margins() is account-specific — use get_broker(account).margins() instead."
        )

    def orders(self) -> list[dict]:
        raise NotImplementedError(
            "PriceBroker.orders() is account-specific — use get_broker(account).orders() instead."
        )

    def trades(self) -> list[dict]:
        raise NotImplementedError(
            "PriceBroker.trades() is account-specific — use get_broker(account).trades() instead."
        )

    def basket_order_margins(self, orders: list[dict]) -> list[dict]:
        raise NotImplementedError(
            "PriceBroker.basket_order_margins() is account-specific — use get_broker(account)."
        )

    def place_order(self, **kwargs: Any) -> str:
        raise NotImplementedError(
            "PriceBroker.place_order() not supported — orders must target a specific account "
            "via get_broker(account).place_order()."
        )

    def modify_order(self, order_id: str, **kwargs: Any) -> str:
        raise NotImplementedError(
            "PriceBroker.modify_order() not supported — use get_broker(account).modify_order()."
        )

    def cancel_order(self, order_id: str, **kwargs: Any) -> str:
        raise NotImplementedError(
            "PriceBroker.cancel_order() not supported — use get_broker(account).cancel_order()."
        )


def _account_priority(account: str) -> int:
    """Per-account priority hint for PriceBroker fallback ordering.
    Lower = tried first. Sourced from broker_accounts.priority via the
    Connections cache; defaults to 100 when the cache hasn't been
    populated (boot timing) or the account isn't in the map."""
    conns = Connections()
    pri_map: dict[str, int] = getattr(conns, "_priority_map", {}) or {}
    return int(pri_map.get(account, 100))


def get_price_broker() -> Broker:
    """
    Auto-failover broker for shared market-data fetches (underlying
    spots, historical candles, instrument lookups). Returns a
    `PriceBroker` wrapper that tries each available broker in
    preference order, falling over to the next on failure.

    Preference order:
      1. `connections.price_account` setting (operator-pinned account)
         — when set, that broker is always tried first.
      2. Remaining brokers sorted by `broker_accounts.priority` ASC
         (lower = earlier). Set per-account via /admin/brokers so
         operators can tune "if Kite stutters, hit Dhan next" from
         the UI without code changes.
      3. Tie-breaker: insertion order in Connections().conn.

    Operator's pinned choice + priority sort win when healthy; on
    failure (rate-limit, token expiry, network blip, vendor outage)
    the call transparently rolls to the next broker without the
    caller seeing it. Use `PriceBroker.last_served_by()` for
    debug-time visibility into which broker actually answered.
    """
    from backend.shared.helpers.settings import get_string

    accounts = list(Connections().conn.keys())
    if not accounts:
        raise KeyError("No broker accounts configured.")

    pinned = (get_string("connections.price_account", "") or "").strip()

    # Build ordered list: pinned first (if valid), then everything
    # else sorted by priority ASC (with a stable secondary sort on
    # insertion order from Connections().conn).
    ordered: list[Broker] = []
    seen: set[str] = set()
    if pinned and pinned in accounts:
        ordered.append(get_broker(pinned))
        seen.add(pinned)
    remaining = [a for a in accounts if a not in seen]
    remaining.sort(key=lambda a: (_account_priority(a), accounts.index(a)))
    for acct in remaining:
        ordered.append(get_broker(acct))

    return PriceBroker(ordered)
