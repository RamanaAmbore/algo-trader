"""
self_heal_log.py — Shared throttled logger for self-heal broker fetch events.

Provides a single canonical `_self_heal_log_once` that emits at most one
INFO log per (symbol, exchange) per _SELF_HEAL_LOG_INTERVAL_S seconds.
Imported by both quote.py (sparkline self-heal) and options.py (historical
bars self-heal) so both surfaces share one SSOT throttle table.

Thread-safety: a module-level Lock guards the timestamp dict. CPython GIL
makes the dict mutation safe but the Lock ensures the read-and-write is
atomic (no race on the `last < threshold → update → log` sequence).
"""

from __future__ import annotations

import threading
import time

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

_SELF_HEAL_LOG_INTERVAL_S: float = 60.0

# (symbol, exchange) → last log emit time (monotonic)
_SELF_HEAL_LOG_TS: dict[tuple[str, str], float] = {}
_SELF_HEAL_LOG_LOCK = threading.Lock()


def _self_heal_log_once(sym: str, exch: str, coverage: int, requested: int) -> None:
    """Emit one INFO log per (sym, exch) per _SELF_HEAL_LOG_INTERVAL_S.

    Parameters
    ----------
    sym:
        Trading symbol (upper-cased, normalised).
    exch:
        Exchange code (upper-cased).
    coverage:
        How many bars/closes were found in Tier 1+2 before the heal.
    requested:
        How many bars/closes were requested (days window).

    Thread-safe, best-effort — a log failure never propagates to callers.
    """
    key = (sym, exch)
    now = time.monotonic()
    with _SELF_HEAL_LOG_LOCK:
        last = _SELF_HEAL_LOG_TS.get(key, 0.0)
        if now - last < _SELF_HEAL_LOG_INTERVAL_S:
            return
        _SELF_HEAL_LOG_TS[key] = now
    try:
        logger.info(
            f"sparkline: self-heal fetch for {sym}/{exch} — "
            f"DB empty during closed hours (coverage {coverage}/{requested})"
        )
    except Exception:
        pass
