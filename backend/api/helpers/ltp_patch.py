"""Shared scaffold for the LTP-override patch logic used by both
`backend/api/routes/positions.py` and `.../holdings.py`.

Both routes use the KiteTicker live tick map to override stale REST
last_price values (Kite's /positions + /holdings APIs can lag the WS
feed by minutes on less-liquid contracts at session open). Both fall
back to the last-known-good cache when the ticker has no fresh
sample. Both write a `last_price_stale` flag for downstream UI.

The difference is per-row policy:

  • positions.py — patch whenever the ticker has a fresh sample that
    differs from the broker by > 0.005, OR when the broker shipped a
    zero and the cache has a recent good value. Followed by an
    additive pnl patch using `Δ_pnl = (new_LTP − old_LTP) × qty`.

  • holdings.py — only patch rows whose broker LTP is zero or
    missing (never overwrite a valid non-zero broker value). No
    additive pnl patch (holdings pnl recompute is naive).

This module owns:
  • The ticker pull + last-known-good fallback iteration.
  • The patched-index bookkeeping (patched_idx + patched_old_ltp +
    stale_idx).
  • The `last_price_stale` column write.

Each route provides:
  • A `policy(current_ltp, tick_ltp) -> Decision` callback that
    decides whether to patch from the live ticker. The scaffold
    handles the LKG-cache fallback automatically when the policy
    returns `consider_cache=True`.
  • Its own day-change + pnl recompute step, performed AFTER this
    helper returns the bookkeeping result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

import pandas as pd

import time as _time

from backend.brokers.broker_apis import get_last_good_ltp, record_good_ltp
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Throttled log helper — emit at most once per (sym, source) per 60s.
# Detects when the LTP patch scaffold falls back to close_price territory
# because the ticker has no local token registration for the symbol.
# ---------------------------------------------------------------------------
_LTP_GAP_LOG_TTL_S = 60.0
_ltp_gap_last: dict[tuple[str, str], float] = {}  # (sym, source) → monotonic


def _log_ltp_gap_throttled(sym: str, source: str, close: float) -> None:
    key = (sym, source)
    now = _time.monotonic()
    if now - _ltp_gap_last.get(key, 0.0) > _LTP_GAP_LOG_TTL_S:
        _ltp_gap_last[key] = now
        logger.warning(
            "[LTP-GAP] sym=%s source=%s close=%.4f reason=no_local_token",
            sym,
            source,
            close,
        )


@dataclass(frozen=True)
class Decision:
    """A per-row patch decision returned by the policy callback.

    Two outcomes a policy can request:
      • `new_ltp != None`        — write this value (live tick path)
      • `consider_cache == True` — no live tick to use, try the LKG
                                   cache; the scaffold writes the
                                   cache value with from_cache=True.
      • both None / False        — leave row unchanged.
    """
    new_ltp: Optional[float] = None
    consider_cache: bool = False


@dataclass
class PatchResult:
    """Bookkeeping output from `apply_ltp_patch`. Caller uses
    `patched_idx` (+ `patched_old_ltp` if it wants the pnl additive
    delta) for its own day-change / pnl recompute step."""
    patched_idx: list = field(default_factory=list)
    patched_old_ltp: dict = field(default_factory=dict)
    stale_idx: list = field(default_factory=list)

    @property
    def any_patched(self) -> bool:
        return bool(self.patched_idx)


PolicyFn = Callable[[float, Optional[float]], Decision]


def _detect_qty_col(raw: pd.DataFrame) -> Optional[str]:
    """Detect the qty column once — positions use `quantity`, holdings
    use `opening_quantity`. Rows with qty==0 are fully-closed intraday
    positions (Kite includes them in `net` when realised != 0). LTP for
    a flat position is meaningless — patching would rewrite `last_price`
    to a misleading value that doesn't correspond to any held position
    and could poison downstream displays that assume LTP moves the row.
    """
    if 'quantity' in raw.columns:
        return 'quantity'
    if 'opening_quantity' in raw.columns:
        return 'opening_quantity'
    return None


def _row_is_flat(raw: pd.DataFrame, idx, qty_col: Optional[str]) -> bool:
    """True iff qty=0 (fully-closed intraday row). Missing / bad values
    treat as 0.
    """
    if qty_col is None:
        return False
    try:
        _qv = raw.at[idx, qty_col]
        _qty_row = float(_qv) if pd.notna(_qv) else 0.0
    except (TypeError, ValueError):
        _qty_row = 0.0
    return _qty_row == 0.0


def _read_current_ltp(raw: pd.DataFrame, idx) -> float:
    try:
        return float(raw.at[idx, 'last_price']) \
            if pd.notna(raw.at[idx, 'last_price']) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _maybe_log_ltp_gap(raw: pd.DataFrame, idx, sym_s: str, current: float) -> None:
    """Ticker has no sample and broker value equals close_price — that's
    the exact condition that causes LTP/close flicker on the frontend.
    """
    if 'close_price' not in raw.columns:
        return
    try:
        _close_val = raw.at[idx, 'close_price']
        _close_f = float(_close_val) if pd.notna(_close_val) else None
    except (TypeError, ValueError):
        _close_f = None
    if _close_f is not None and _close_f > 0 and abs(current - _close_f) < 0.005:
        _log_ltp_gap_throttled(sym_s, "policy_no_ticker", _close_f)


def _apply_row_decision(
    raw: pd.DataFrame, idx, sym_s: str, current: float, decision: Decision,
    result: PatchResult,
) -> None:
    """Apply a single Decision to `raw` in place + update bookkeeping."""
    if decision.new_ltp is not None:
        raw.at[idx, 'last_price'] = float(decision.new_ltp)
        result.patched_idx.append(idx)
        result.patched_old_ltp[idx] = current
        return

    if not decision.consider_cache:
        return

    cached = get_last_good_ltp(sym_s)
    if cached is not None and cached > 0:
        raw.at[idx, 'last_price'] = float(cached)
        result.patched_idx.append(idx)
        result.patched_old_ltp[idx] = current
        result.stale_idx.append(idx)


def apply_ltp_patch(raw: pd.DataFrame, policy: PolicyFn) -> Optional[PatchResult]:
    """Iterate `raw` rows; ask `policy` whether to patch each one;
    apply the patches in place; return a PatchResult so the caller
    can run its own day-change / pnl recompute on the patched rows.

    Returns None when the ticker / dataframe is unusable (empty,
    missing tradingsymbol column, ticker unavailable). The caller
    should early-return on None.

    Always records every fresh tick to the LKG cache (keeps the
    cache warm regardless of policy outcome). When the policy
    declines a live patch but sets `consider_cache=True`, falls
    back to `get_last_good_ltp(sym)` and writes that value with
    `last_price_stale=True`.
    """
    if raw is None or raw.empty or 'tradingsymbol' not in raw.columns:
        return None
    try:
        from backend.brokers.kite_ticker import get_ticker as _get_ticker
        _ticker = _get_ticker()
    except Exception:
        return None

    result = PatchResult()
    _qty_col = _detect_qty_col(raw)

    for idx in raw.index:
        sym = raw.at[idx, 'tradingsymbol']
        if not sym:
            continue
        sym_s = str(sym)

        if _row_is_flat(raw, idx, _qty_col):
            continue

        current = _read_current_ltp(raw, idx)

        tick_ltp = _ticker.get_ltp_by_sym(sym_s)
        # Keep LKG cache warm independent of policy outcome.
        if tick_ltp is not None and tick_ltp > 0:
            record_good_ltp(sym_s, tick_ltp)
        else:
            _maybe_log_ltp_gap(raw, idx, sym_s, current)

        decision = policy(current, tick_ltp)
        _apply_row_decision(raw, idx, sym_s, current, decision, result)

    if result.stale_idx:
        if 'last_price_stale' not in raw.columns:
            raw['last_price_stale'] = False
        for idx in result.stale_idx:
            raw.at[idx, 'last_price_stale'] = True

    return result


# ---------------------------------------------------------------------------
# Built-in policies — the two used by positions.py + holdings.py today.
# Custom routes can pass their own PolicyFn instead.
# ---------------------------------------------------------------------------


def positions_policy(current: float, tick_ltp: Optional[float]) -> Decision:
    """positions.py policy — patch whenever a fresh tick differs from
    the broker by > 0.005. When the ticker has no sample AND the
    broker LTP is zero, fall back to the LKG cache.
    """
    if tick_ltp is not None and tick_ltp > 0:
        if abs(tick_ltp - current) <= 0.005:
            return Decision()  # no-op (drift is within epsilon)
        return Decision(new_ltp=float(tick_ltp))
    if current <= 0:
        return Decision(consider_cache=True)
    return Decision()


def holdings_policy(current: float, tick_ltp: Optional[float]) -> Decision:
    """holdings.py policy — only patch when the broker LTP is zero
    or missing. Never overwrites a valid non-zero broker value. Tries
    LKG cache when the ticker also has nothing.
    """
    if current > 0:
        return Decision()  # broker value is valid, leave it
    if tick_ltp is not None and tick_ltp > 0:
        return Decision(new_ltp=float(tick_ltp))
    return Decision(consider_cache=True)
