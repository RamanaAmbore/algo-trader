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
_DEFAULT_BELLWETHERS: dict[str, list[str]] = {
    "NSE": ["NSE:NIFTY 50", "NSE:NIFTY BANK"],
    "NFO": ["NSE:NIFTY 50", "NSE:NIFTY BANK"],
    "BSE": ["BSE:SENSEX"],
    "BFO": ["BSE:SENSEX"],
    "CDS": ["NSE:NIFTY 50"],
    # MCX has no shared index — pick the most actively traded futures.
    # The contract month suffix changes monthly; we ask for several so
    # at least one is active. Operator override is the right place to
    # pin a specific live contract if these miss.
    "MCX": [
        "MCX:CRUDEOIL26JUNFUT", "MCX:CRUDEOIL26JULFUT",
        "MCX:GOLD26JUNFUT",     "MCX:GOLD26AUGFUT",
        "MCX:SILVER26JULFUT",
    ],
}


_PROBE_CACHE: dict[str, tuple[float, Optional[bool]]] = {}
_PROBE_LOCK = threading.Lock()
_CACHE_TTL_SEC = 60.0
_STALE_THRESHOLD_MIN = 15  # last_trade_time older than this ⇒ stale


def _parse_overrides() -> dict[str, list[str]]:
    """Pull `market.bellwether_symbols` from settings — CSV of
    EXCHANGE:SYMBOL entries — merge into the default map. Operator can
    keep the MCX contract month current here without code changes."""
    try:
        from backend.shared.helpers.settings import get_string
        raw = get_string("market.bellwether_symbols", "") or ""
    except Exception:
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
    return out


def _candidates(exchange: str) -> list[str]:
    overrides = _parse_overrides()
    if exchange in overrides:
        return overrides[exchange]
    return list(_DEFAULT_BELLWETHERS.get(exchange, []))


def _ist_now() -> datetime:
    from backend.shared.helpers.date_time_utils import timestamp_indian
    return timestamp_indian()


def probe_market_active(exchange: str, kite: Any = None,
                        max_age_min: int = _STALE_THRESHOLD_MIN
                        ) -> Optional[bool]:
    """Return True if Kite quote for a bellwether on `exchange` shows
    a `last_trade_time` within the last `max_age_min` minutes.
    Returns False when probe ran and no candidate had a recent trade.
    Returns None when probe couldn't run (no Kite handle, no candidate,
    or any exception) — caller should fall back to the calendar
    verdict in that case.

    Cached for `_CACHE_TTL_SEC` per exchange so the agent engine's
    per-tick gate evaluation doesn't hammer Kite. Cache key is the
    exchange code; the value persists until TTL or a service restart.
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

    candidates = _candidates(exchange)
    if not candidates:
        return None

    # Lazy-resolve a Kite handle from Connections() if the caller
    # didn't pass one. We use any available Kite account — quote
    # access is shared across the operator's accounts.
    if kite is None:
        try:
            from backend.shared.helpers.connections import Connections
            for c in (Connections().conn or {}).values():
                if hasattr(c, "get_kite_conn"):
                    try:
                        kite = c.get_kite_conn()
                        break
                    except Exception:
                        continue
        except Exception:
            pass
    if kite is None:
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
