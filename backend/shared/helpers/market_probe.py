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

    # Group active futures by underlying name, sorted by expiry asc.
    by_name: dict[str, list[dict]] = {}
    for i in instruments or []:
        if (i.get("instrument_type") or "").upper() != "FUT":
            continue
        exp = i.get("expiry")
        if not exp:
            continue
        # Tolerate both date objects and ISO strings.
        if hasattr(exp, "date"):
            exp_date = exp.date() if hasattr(exp, "isoformat") and "T" in str(exp) else exp
        else:
            try:
                from datetime import datetime as _dt
                exp_date = _dt.fromisoformat(str(exp)[:10]).date()
            except Exception:
                continue
        if exp_date < today:
            continue
        name = (i.get("name") or "").upper()
        if not name:
            continue
        by_name.setdefault(name, []).append((exp_date, i.get("tradingsymbol") or ""))

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

    # Cache hit?
    now_ts = time.monotonic()
    with _PROBE_LOCK:
        cached = _PROBE_CACHE.get(exchange)
        if cached and (now_ts - cached[0]) < _CACHE_TTL_SEC:
            return cached[1]

    # ── Step 1: broker market-status API ──────────────────────────────
    # Iterate brokers, ask each for an authoritative answer. ANY
    # definitive True/False wins; None means the adapter doesn't
    # implement the method or the call failed — try the next broker.
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
                with _PROBE_LOCK:
                    _PROBE_CACHE[exchange] = (now_ts, verdict)
                return verdict
    except Exception as e:
        logger.debug(f"market_probe: broker iteration failed: {e}")

    # ── Step 2: bellwether-quote probe (Kite fallback) ────────────────
    # Lazy-resolve a Kite handle + a Broker adapter from Connections()
    # / the broker registry when the caller didn't pass them. Quote
    # access is shared across the operator's accounts; instruments
    # are exposed via the Broker ABC.
    broker = None
    if kite is None:
        try:
            from backend.brokers.connections import Connections
            for acct, c in (Connections().conn or {}).items():
                if hasattr(c, "get_kite_conn"):
                    try:
                        kite = c.get_kite_conn()
                        # Attach a Broker adapter for instruments-dump
                        # lookups (used by MCX dynamic discovery).
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
    if kite is None:
        return None

    candidates = _candidates(exchange, broker=broker)
    if not candidates:
        with _PROBE_LOCK:
            _PROBE_CACHE[exchange] = (now_ts, None)
        return None

    try:
        q = kite.quote(candidates)
    except Exception as e:
        logger.debug(f"market_probe: kite.quote({exchange}) failed: {e}")
        with _PROBE_LOCK:
            _PROBE_CACHE[exchange] = (now_ts, None)
        return None

    cutoff = _ist_now() - timedelta(minutes=max_age_min)
    active = False
    for sym, row in (q or {}).items():
        ltt = row.get("last_trade_time")
        if ltt is None:
            continue
        if isinstance(ltt, str):
            # Some SDK versions stringify timestamps. Best-effort parse.
            try:
                ltt = datetime.fromisoformat(ltt)
            except Exception:
                continue
        if ltt.tzinfo is None:
            # Kite returns naive IST timestamps; attach the IST tz so
            # the cutoff comparison is sound.
            from backend.shared.helpers.date_time_utils import INDIAN_TIMEZONE
            ltt = ltt.replace(tzinfo=INDIAN_TIMEZONE)
        if ltt >= cutoff:
            active = True
            break

    with _PROBE_LOCK:
        _PROBE_CACHE[exchange] = (now_ts, active)
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
