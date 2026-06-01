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

import threading
import time
from typing import Any

from backend.shared.brokers.base import Broker
from backend.shared.brokers.dhan import DhanBroker
from backend.shared.brokers.groww import GrowwBroker
from backend.shared.brokers.kite import KiteBroker
from backend.shared.helpers.connections import Connections
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

# ── Per-broker rate-limit cool-off ────────────────────────────────────
# When a broker call raises an exception containing "too many requests"
# (case-insensitive — Kite's KiteException carries this verbatim), the
# broker is marked rate-limited for _RATE_LIMIT_COOLOFF_SECONDS.
# Subsequent _try() calls skip that broker immediately, letting the
# PriceBroker fallback chain (or the error path) take over without
# waiting for Kite's own retry timer.
#
# broker_id key format: "{broker_id}/{account}" (matches log lines and
# PriceBroker._last_used so operators can correlate across surfaces).
_RATE_LIMIT_COOLOFF: dict[str, float] = {}   # broker_id → expires_at (unix)
_RATE_LIMIT_LOCK = threading.Lock()
_RATE_LIMIT_COOLOFF_SECONDS = 30             # tunable here; settings-backed later if needed


def _is_rate_limited(broker_id: str) -> bool:
    """Return True when `broker_id` is in active cool-off."""
    with _RATE_LIMIT_LOCK:
        expires = _RATE_LIMIT_COOLOFF.get(broker_id, 0.0)
        if expires == 0.0:
            return False
        if time.time() >= expires:
            _RATE_LIMIT_COOLOFF.pop(broker_id, None)
            return False
        return True


def _mark_rate_limited(broker_id: str) -> None:
    """Record a rate-limit hit; blocks this broker for _RATE_LIMIT_COOLOFF_SECONDS."""
    with _RATE_LIMIT_LOCK:
        _RATE_LIMIT_COOLOFF[broker_id] = time.time() + _RATE_LIMIT_COOLOFF_SECONDS


# Broker id → adapter class. Both "zerodha_kite" (canonical, stored in
# broker_accounts.broker_id) and "kite" (legacy YAML value) map to
# KiteBroker so existing rows remain compatible after the column was added.
# Extend here when a new vendor adapter lands — e.g. "upstox": UpstoxBroker.
_ADAPTERS: dict[str, type[Broker]] = {
    "zerodha_kite": KiteBroker,
    "kite":         KiteBroker,  # legacy alias — YAML-seeded rows use "kite"
    "dhan":         DhanBroker,
    "groww":        GrowwBroker,
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
        last exception so the caller sees a real diagnostic.

        Rate-limited brokers are skipped immediately (no network call)
        until their cool-off expires. When a broker returns a
        "Too many requests" error it is marked rate-limited for
        _RATE_LIMIT_COOLOFF_SECONDS so the next call falls through to the
        next broker without amplifying the rate-limit storm.
        """
        last_exc: Exception | None = None
        for broker in self._brokers:
            broker_key = f"{broker.broker_id}/{broker.account}"
            if _is_rate_limited(broker_key):
                # Skip immediately — no network call, no log spam.
                last_exc = RuntimeError(
                    f"{broker_key} rate-limited (cool-off active)"
                )
                continue
            try:
                result = getattr(broker, method_name)(*args, **kwargs)
                self._last_used = broker_key
                return result
            except Exception as e:
                last_exc = e
                if "too many requests" in str(e).lower():
                    _mark_rate_limited(broker_key)
                    logger.warning(
                        f"PriceBroker: {broker_key} rate-limited, "
                        f"cooling off {_RATE_LIMIT_COOLOFF_SECONDS}s"
                    )
                else:
                    logger.warning(
                        f"PriceBroker fallback: {method_name} failed on "
                        f"{broker_key}: {str(e)[:160]}"
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


def _is_hist_enabled(account: str) -> bool:
    """Return True when `account` has historical_data_enabled=True.
    Reads the Connections singleton cache populated by rebuild_from_db;
    defaults to True for YAML-seeded accounts (conservative — include all
    when the map hasn't been populated yet)."""
    conns = Connections()
    hist_map: dict[str, bool] = getattr(conns, "_hist_enabled_map", {})
    # If the account is in the map, honour it. If not, default True so
    # a freshly-loaded YAML account is never silently excluded.
    return bool(hist_map.get(account, True))


def get_historical_brokers() -> list[Broker]:
    """
    Return the prioritised list of Kite (and Kite-compatible) broker
    adapters eligible for /api/options/historical OHLCV calls.

    Ordering:
      1. The configured `connections.price_account` (when set, enabled,
         and not in active rate-limit cool-off) — tried first.
      2. Remaining accounts with historical_data_enabled=True, sorted by
         broker_accounts.priority ASC (lower = earlier), with
         insertion-order tie-breaking.
      3. Accounts currently in rate-limit cool-off are EXCLUDED so the
         caller doesn't waste a network round-trip on a known-throttled
         account.  The cool-off expires after _RATE_LIMIT_COOLOFF_SECONDS
         (30 s by default); the account re-enters the list on the next
         call automatically.

    Returns an empty list when every eligible account is in cool-off or
    no account has historical_data_enabled=True.  The historical handler
    treats an empty list as "return graceful empty bars immediately."
    """
    from backend.shared.helpers.settings import get_string

    accounts = list(Connections().conn.keys())
    if not accounts:
        return []

    pinned = (get_string("connections.price_account", "") or "").strip()

    ordered: list[Broker] = []
    seen: set[str] = set()

    # Pinned account first (if it exists, is eligible, and not rate-limited).
    if pinned and pinned in accounts and _is_hist_enabled(pinned):
        broker_key = f"{_broker_id_for(pinned)}/{pinned}"
        if not _is_rate_limited(broker_key):
            ordered.append(get_broker(pinned))
            seen.add(pinned)

    # Remaining eligible accounts, sorted by priority then insertion order.
    remaining = [
        a for a in accounts
        if a not in seen and _is_hist_enabled(a)
    ]
    remaining.sort(key=lambda a: (_account_priority(a), accounts.index(a)))
    for acct in remaining:
        broker_key = f"{_broker_id_for(acct)}/{acct}"
        if not _is_rate_limited(broker_key):
            ordered.append(get_broker(acct))

    return ordered


def get_sparkline_broker() -> Broker:
    """
    Sister of `get_price_broker()` — picks a Kite account distinct from
    the chart-historical pinned account so the two read workloads don't
    fight over the same 3 req/sec historical_data budget.

    Selection order:
      1. `connections.sparkline_account` setting (operator pin) — if set
         and the account is loaded + historical_data_enabled.
      2. The FIRST eligible Kite account that is NOT
         `connections.price_account` (the chart-historical pin).
      3. Fallback to `get_price_broker()` when only one Kite account is
         loaded — single-broker setups behave exactly as before.

    Returns a PriceBroker (auto-failover wrapper) so a single account in
    cool-off doesn't break sparklines. Just rotates the *primary* pick.
    """
    from backend.shared.helpers.settings import get_string

    accounts = list(Connections().conn.keys())
    if not accounts:
        raise KeyError("No broker accounts configured.")

    kite_accounts = [a for a in accounts if _broker_id_for(a) == "zerodha_kite"
                                       and _is_hist_enabled(a)]
    chart_pinned = (get_string("connections.price_account", "") or "").strip()
    spark_pinned = (get_string("connections.sparkline_account", "") or "").strip()

    ordered: list[Broker] = []
    seen: set[str] = set()

    # 1. Explicit sparkline pin wins when set + valid.
    if spark_pinned and spark_pinned in accounts and spark_pinned in kite_accounts:
        ordered.append(get_broker(spark_pinned))
        seen.add(spark_pinned)
    else:
        # 2. First Kite account that's NOT the chart-historical pin.
        for a in kite_accounts:
            if a != chart_pinned:
                ordered.append(get_broker(a))
                seen.add(a)
                break

    # 3. Pad with everything else (sorted by priority) so failover still
    # works if the primary pick stutters. Mirrors get_price_broker shape.
    remaining = [a for a in accounts if a not in seen]
    remaining.sort(key=lambda a: (_account_priority(a), accounts.index(a)))
    for acct in remaining:
        ordered.append(get_broker(acct))

    # If we couldn't find a distinct Kite account (only one Kite loaded),
    # fall back to the standard price-broker chain — behaviour-preserving.
    if not ordered:
        return get_price_broker()
    return PriceBroker(ordered)


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
