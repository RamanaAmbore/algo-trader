"""
Live market-activity probe.

Calendar APIs (NSE holiday-master, Kite holidays()) catch the bulk of
"is market open today" cases, but miss two real-world patterns:

  1. MCX runs an evening commodity session on equity holidays. NSE's
     COM-segment list flags the date as closed, even though MCX is
     actually trading 17:00-23:30 IST.

  2. NSE Muhurat (Diwali, occasional SEBI-announced Saturday F&O
     expiries) — neither side appears in the holiday-master listing
     AND the date falls on a weekend, so the weekday-default treats
     them as closed.

Both cases resolve cleanly if we ask the broker directly: "have you
seen a trade for a bellwether symbol on this exchange recently?"
If yes → exchange is trading right now (or just was, within the
configured staleness window). Cached for 60 seconds per exchange so
the probe doesn't hit Kite on every agent tick.

The probe is OPTIONAL — every caller passes `kite=None` cleanly when
no broker handle is available (boot, test fixtures, sim driver) and
the function returns None so callers fall back to the calendar
verdict. Never raises.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# Bellwether symbols per exchange — what we ask Kite about to learn
# "has this exchange been trading recently". Indices are the best
# choice when available because they tick continuously throughout the
# session; for MCX (no index of that shape), the active near-month
# contract for a highly-liquid commodity is the safest fallback.
# Operator can override per-exchange via the
# `market.bellwether_symbols` setting (CSV of `EXCHANGE:SYMBOL`).
# Static bellwethers for index-driven exchanges. These symbols don't
# roll over (NIFTY/SENSEX are perpetual indices) so a hardcoded list
# is safe and faster than instruments-dump discovery.
_DEFAULT_BELLWETHERS: dict[str, list[str]] = {
    "NSE": ["NSE:NIFTY 50", "NSE:NIFTY BANK"],
    "NFO": ["NSE:NIFTY 50", "NSE:NIFTY BANK"],
    "BSE": ["BSE:SENSEX"],
    "BFO": ["BSE:SENSEX"],
    "CDS": ["NSE:NIFTY 50"],
    # MCX falls into the dynamic path — no index, contract months
    # roll monthly. See _discover_mcx_bellwethers below.
}

# Commodities probed for MCX activity, in descending liquidity order.
# We pick the nearest unexpired futures contract for the first few
# matches from broker.instruments("MCX"). Crude oil + gold are the
# most reliably-active globally and intraday on Indian sessions.
_MCX_LIQUID_COMMODITIES = (
    "CRUDEOIL", "NATURALGAS",
    "GOLD", "GOLDM",
    "SILVER", "SILVERM", "SILVERMIC",
    "COPPER", "ZINC",
)


_PROBE_CACHE: dict[str, tuple[float, Optional[bool]]] = {}
_DYNAMIC_BELLWETHER_CACHE: dict[str, tuple[Any, list[str]]] = {}
_PROBE_LOCK = threading.Lock()
_CACHE_TTL_SEC = 60.0
_STALE_THRESHOLD_MIN = 15  # last_trade_time older than this ⇒ stale

# Cache for the parsed `market.bellwether_symbols` setting. _parse_overrides
# used to hit the DB on every probe_market_active call (per cache-miss
# exchange, once per 60s per exchange). The setting changes ~never; a 30s
# TTL absorbs the read traffic without making operator edits feel laggy.
_OVERRIDES_CACHE: tuple[float, dict[str, list[str]]] | None = None
_OVERRIDES_TTL_SEC = 30.0


def _parse_overrides() -> dict[str, list[str]]:
    """Pull `market.bellwether_symbols` from settings — CSV of
    EXCHANGE:SYMBOL entries — merge into the default map. Operator can
    keep the MCX contract month current here without code changes.

    Cached for 30 s so a probe_market_active call doesn't hit the DB
    every cache cycle."""
    global _OVERRIDES_CACHE
    now_ts = time.monotonic()
    cached = _OVERRIDES_CACHE
    if cached and (now_ts - cached[0]) < _OVERRIDES_TTL_SEC:
        return cached[1]
    try:
        from backend.shared.helpers.settings import get_string
        raw = get_string("market.bellwether_symbols", "") or ""
    except Exception:
        _OVERRIDES_CACHE = (now_ts, {})
        return {}
    out: dict[str, list[str]] = {}
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok or ":" not in tok:
            continue
        ex, sym = tok.split(":", 1)
        ex = ex.strip().upper()
        sym = f"{ex}:{sym.strip()}"
        out.setdefault(ex, []).append(sym)
    _OVERRIDES_CACHE = (now_ts, out)
    return out


def _parse_expiry(exp: Any) -> "date | None":
    """Return a date from an expiry field (date object or ISO string), or None."""
    from datetime import date as _date
    if exp is None:
        return None
    if hasattr(exp, "date") and hasattr(exp, "isoformat") and "T" in str(exp):
        return exp.date()
    if isinstance(exp, _date):
        return exp
    try:
        from datetime import datetime as _dt
        return _dt.fromisoformat(str(exp)[:10]).date()
    except Exception:
        return None


def _group_active_futures(
    instruments: list[dict], today: Any,
) -> dict[str, list[tuple]]:
    """Group active (unexpired) MCX FUT instruments by underlying name → [(expiry, ts)]."""
    by_name: dict[str, list[tuple]] = {}
    for i in instruments or []:
        if (i.get("instrument_type") or "").upper() != "FUT":
            continue
        exp_date = _parse_expiry(i.get("expiry"))
        if exp_date is None or exp_date < today:
            continue
        name = (i.get("name") or "").upper()
        if not name:
            continue
        by_name.setdefault(name, []).append((exp_date, i.get("tradingsymbol") or ""))
    return by_name


def _discover_mcx_bellwethers(broker: Any) -> list[str]:
    """Pull the live MCX instruments dump and pick the nearest
    unexpired futures contract for each liquid commodity.

    Cached for the current trading date — the instruments list
    doesn't change intraday, so one fetch per day is sufficient.
    Returns a short list of canonical `MCX:<TS>` symbols suitable
    for kite.quote() probes; empty when the broker has no Kite/Dhan
    handle or the instruments call fails.

    Independent of contract-month suffixes (`26JUN`, `26JUL`, …) so
    rolls are seamless: when June crude expires it drops out of the
    instruments list and the function picks July automatically."""
    from datetime import date as _date
    today = _date.today()
    cached = _DYNAMIC_BELLWETHER_CACHE.get("MCX")
    if cached and cached[0] == today:
        return cached[1]

    try:
        instruments = broker.instruments("MCX")
    except Exception as e:
        logger.debug(f"market_probe: MCX instruments fetch failed: {e}")
        return []

    by_name = _group_active_futures(instruments, today)

    out: list[str] = []
    for name in _MCX_LIQUID_COMMODITIES:
        contracts = by_name.get(name)
        if not contracts:
            continue
        contracts.sort()  # nearest expiry first
        ts = contracts[0][1]
        if ts:
            out.append(f"MCX:{ts}")
        if len(out) >= 4:  # 4 commodities is plenty of redundancy
            break

    if out:
        _DYNAMIC_BELLWETHER_CACHE["MCX"] = (today, out)
    return out


def _candidates(exchange: str, broker: Any = None) -> list[str]:
    """Resolve the bellwether symbol list for `exchange`. Operator
    override (settings `market.bellwether_symbols`) always wins;
    otherwise static defaults for index-driven exchanges, dynamic
    instruments-dump discovery for MCX."""
    overrides = _parse_overrides()
    if exchange in overrides:
        return overrides[exchange]
    static = _DEFAULT_BELLWETHERS.get(exchange)
    if static:
        return list(static)
    if exchange == "MCX" and broker is not None:
        return _discover_mcx_bellwethers(broker)
    return []


def _ist_now() -> datetime:
    from backend.shared.helpers.date_time_utils import timestamp_indian
    return timestamp_indian()


def _probe_cache_get(exchange: str, now_ts: float) -> Optional[bool]:
    """Return cached probe result if still fresh, else None."""
    with _PROBE_LOCK:
        cached = _PROBE_CACHE.get(exchange)
        if cached and (now_ts - cached[0]) < _CACHE_TTL_SEC:
            return cached[1]
    return None


def _probe_cache_put(exchange: str, now_ts: float, verdict: Optional[bool]) -> None:
    """Store a probe result in the cache."""
    with _PROBE_LOCK:
        _PROBE_CACHE[exchange] = (now_ts, verdict)


def _probe_via_broker_status(
    exchange: str, now_ts: float,
) -> Optional[bool]:
    """Step 1: iterate broker adapters and return the first bool verdict."""
    try:
        from backend.brokers.registry import all_brokers
        for broker_adapter in all_brokers():
            try:
                verdict = broker_adapter.market_status(exchange)
            except Exception as e:
                logger.debug(
                    f"market_probe: {broker_adapter.broker_id} "
                    f"market_status({exchange}) raised: {e}"
                )
                continue
            if isinstance(verdict, bool):
                logger.debug(
                    f"market_probe: {broker_adapter.broker_id} reports "
                    f"{exchange}={'open' if verdict else 'closed'}"
                )
                _probe_cache_put(exchange, now_ts, verdict)
                return verdict
    except Exception as e:
        logger.debug(f"market_probe: broker iteration failed: {e}")
    return None


def _resolve_kite_and_broker(kite: Any) -> tuple[Any, Any]:
    """Lazy-resolve a Kite handle + Broker adapter when caller passed kite=None."""
    broker = None
    if kite is not None:
        return kite, broker
    try:
        from backend.brokers.connections import Connections
        for acct, c in (Connections().conn or {}).items():
            if not hasattr(c, "get_kite_conn"):
                continue
            try:
                kite = c.get_kite_conn()
                try:
                    from backend.brokers.registry import get_broker
                    broker = get_broker(acct)
                except Exception:
                    broker = None
                break
            except Exception:
                continue
    except Exception:
        pass
    return kite, broker


def _probe_via_bellwether(
    kite: Any, candidates: list[str], max_age_min: int,
) -> bool:
    """Step 2: check last_trade_time freshness for bellwether symbols."""
    from backend.shared.helpers.date_time_utils import INDIAN_TIMEZONE
    cutoff = _ist_now() - timedelta(minutes=max_age_min)
    q = kite.quote(candidates)
    for sym, row in (q or {}).items():
        ltt = row.get("last_trade_time")
        if ltt is None:
            continue
        if isinstance(ltt, str):
            try:
                ltt = datetime.fromisoformat(ltt)
            except Exception:
                continue
        if ltt.tzinfo is None:
            ltt = ltt.replace(tzinfo=INDIAN_TIMEZONE)
        if ltt >= cutoff:
            return True
    return False


def probe_market_active(exchange: str, kite: Any = None,
                        max_age_min: int = _STALE_THRESHOLD_MIN
                        ) -> Optional[bool]:
    """Return True if the market for `exchange` is currently trading.

    Resolution order (first definitive answer wins):
      1. **Broker market-status API** — iterate every loaded `Broker`
         adapter and call `broker.market_status(exchange)`. Returns
         True/False when ANY broker reports a definitive verdict.
         Dhan exposes `get_market_status`; Groww varies by SDK
         version. Kite Connect has no equivalent and returns None
         from its adapter (falls through to step 2).
      2. **Bellwether-quote probe** — call `kite.quote()` on
         configured bellwether symbols (NIFTY 50, SENSEX, MCX crude
         futures, …) and check `last_trade_time` freshness.

    Returns None when neither path can answer (no broker handle, no
    bellwether candidates, or every call raised) — caller should
    fall back to the calendar verdict in that case.

    Cached for `_CACHE_TTL_SEC` per exchange so the agent engine's
    per-tick gate evaluation doesn't hammer the brokers.
    """
    exchange = (exchange or "").upper()
    if not exchange:
        return None

    now_ts = time.monotonic()
    cached = _probe_cache_get(exchange, now_ts)
    if cached is not None:
        return cached

    # Step 1: broker market-status API
    verdict = _probe_via_broker_status(exchange, now_ts)
    if verdict is not None:
        return verdict

    # Step 2: bellwether-quote probe (Kite fallback)
    kite, broker = _resolve_kite_and_broker(kite)
    if kite is None:
        return None

    candidates = _candidates(exchange, broker=broker)
    if not candidates:
        _probe_cache_put(exchange, now_ts, None)
        return None

    try:
        active = _probe_via_bellwether(kite, candidates, max_age_min)
    except Exception as e:
        logger.debug(f"market_probe: kite.quote({exchange}) failed: {e}")
        _probe_cache_put(exchange, now_ts, None)
        return None

    _probe_cache_put(exchange, now_ts, active)
    return active


def invalidate_cache(exchange: str | None = None) -> None:
    """Clear cached probe results. Pass an exchange to clear just one
    entry; omit to clear all. Used by tests and the simulator driver
    when it wants the gate re-evaluated immediately."""
    with _PROBE_LOCK:
        if exchange is None:
            _PROBE_CACHE.clear()
        else:
            _PROBE_CACHE.pop((exchange or "").upper(), None)
