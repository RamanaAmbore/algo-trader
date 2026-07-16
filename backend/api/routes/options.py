"""
`/api/options/*` — options analytics for the /admin/options dashboard.

Computes Greeks, payoff curves, risk metrics (max profit / max loss /
breakeven / POP), theoretical-vs-market discrepancy, and historical
candles for any single-leg option position. Three input modes:

  - `live`         — read qty/avg/LTP from a real broker position
  - `sim`          — read from the SimDriver's `_positions_rows`
  - `hypothetical` — operator-specified symbol + qty; LTP fetched from
                     broker for theoretical analysis before they take
                     the trade.

Underlying spot, current LTP, and historical candles are fetched via
`get_market_data_broker()` so they honor the `connections.price_account`
setting in /admin/settings — operators centralize "which Kite handle do
we hammer for shared market data" in one place.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import threading as _threading
import time
from collections import OrderedDict as _OrderedDict
from datetime import date, datetime, timedelta
from typing import Any, Optional

import msgspec
from litestar import Controller, Request, get, post
from litestar.exceptions import HTTPException

from backend.api.algo.derivatives import (
    DEFAULT_IV,
    DEFAULT_RISK_FREE,
    black_scholes,
    days_to_expiry,
    expected_value,
    find_breakevens,
    futures_symbol_for_expiry,
    greeks,
    implied_vol,
    is_mcx_underlying,
    lookup_future_for_option,
    lookup_mcx_front_month_future,
    lookup_mcx_future_for_expiry,
    multileg_extremes,
    multileg_greeks,
    multileg_payoff_curve,
    multileg_intermediate_curves,
    intermediate_curves,
    multileg_pop,
    option_quote_key,
    parse_tradingsymbol,
    payoff_curve,
    risk_metrics,
    risk_reward_ratio,
    underlying_ltp_key,
)
from backend.api.auth_guard import admin_guard, auth_or_demo_guard
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


def _ohlcv_trace_enabled() -> bool:
    """Settings-gated INFO instrumentation for the historical endpoint.

    Defaults to False on every environment. Operator flips
    `debug.ohlcv_trace` in `/admin/settings` to surface per-(symbol,
    exchange, range) telemetry when chasing a BEL-style intermittent
    "no data available" race. Matches the gate in ohlcv_store so a
    single flag turns on the full read-path trace.
    """
    try:
        from backend.shared.helpers import settings as _settings
        return _settings.get_bool("debug.ohlcv_trace", False)
    except Exception:
        return False


_VALID_MODES = ("live", "sim", "hypothetical")

# ── Strategy-analytics short-circuit cache ────────────────────────────
# When the frontend re-polls /strategy-analytics every 5s with the SAME
# legs + spot + underlying, skip the full broker quote + BS IV calibration
# path and return the cached response in <20ms.
#
# Cache shape: hex_key → (monotonic_ts, StrategyResponse)
# Eviction: LRU with capacity 64. TTL 5s (matches the frontend poll cadence).
# Key: blake2b-16 over (sorted_legs_tuple, spot, mode) — same shape used by
#   backend/api/cache.py for per-key locking (using time.monotonic, not
#   time.time, so clock-skew / NTP jumps don't create false expiries).
#
# Thread-safety: same pattern as _HIST_CACHE — threading.Lock guards the
# check-then-mutate pair; cache reads/writes happen in the async frame so
# CPython GIL keeps the dict mutation atomic without the lock, but the Lock
# ensures the check+mutate is atomic as a unit.

_STRATEGY_ANALYTICS_CACHE: "_OrderedDict[str, tuple[float, Any]]" = _OrderedDict()
_STRATEGY_ANALYTICS_CACHE_LOCK = _threading.Lock()
_STRATEGY_ANALYTICS_CACHE_TTL   = 5     # seconds — matches frontend poll cadence
_STRATEGY_ANALYTICS_CACHE_SIZE  = 64    # LRU cap


# ── Phase 4 — Leg-curve cache (spot-INDEPENDENT pieces) ───────────────
# Splits the strategy-analytics work into two buckets:
#
#   SPOT-INDEPENDENT (cached 5 min, keyed on legs+shape only):
#     • expiry_curve_normalized  — expiry_value per x_ratio in [1-span, 1+span]
#     • intermediate_curves_norm — per-slice {label, elapsed_pct, days_left,
#                                   values} where values[i] indexed on same grid
#     • max_profit, max_loss, rr_ratio  — derived from expiry curve alone
#
#   SPOT-DEPENDENT (always recomputed, ~25 ms with NumPy):
#     • today_value per point (uses current sigma calibrated against live LTP)
#     • EV, POP (integrate cached expiry curve × current lognormal PDF)
#     • aggregate_greeks (single-point, current spot)
#     • spot, iv_proxy, net_cost, leg_details (per-request LTP resolution)
#
# Normalized form — x_ratio = x_abs / S is stored rather than absolute spot
# so the cached values remain valid across spot moves.  At response-build
# time: spot_i = x_ratio_i * S_current.
#
# Cache shape:  hex_key → (last_access_monotonic, payload_dict)
# LRU: same OrderedDict pattern as _STRATEGY_ANALYTICS_CACHE.
# TTL: 5 minutes sliding (refreshed on access, not on write).  Long enough
#      that a 5s frontend re-poll always hits; short enough to pick up σ /
#      leg changes from operator edits without operator-visible staleness.
# Capacity: 64 entries (matches _STRATEGY_ANALYTICS_CACHE_SIZE).
# Thread safety: same threading.Lock pattern — check + mutate atomic.
#
# Interaction with Phase 2 cache:
#   strategy_analytics() checks _STRATEGY_ANALYTICS_CACHE first (TTL 5s,
#   identical request = 100% skip).  On Phase 2 miss (spot changed), the
#   impl calls _leg_curve_cache_get() — if hit, skip ~60 ms of NumPy
#   curve compute and only run today-curve + EV + POP (~25 ms).  On
#   leg-curve miss too, cold compute runs and populates both caches.

_LEG_CURVE_CACHE: "_OrderedDict[str, tuple[float, dict]]" = _OrderedDict()
_LEG_CURVE_CACHE_LOCK = _threading.Lock()
_LEG_CURVE_CACHE_TTL  = 300   # 5 minutes sliding window
_LEG_CURVE_CACHE_SIZE = 64    # LRU cap


def _leg_curve_cache_key(
    resolved_legs: list[dict],
    span_pct: float,
    span_sigmas: float,
    points: int,
    time_slices: int,
) -> str:
    """Compute a blake2b-16 hex key over the leg-geometry + shape params.

    Uses the same blake2b primitive as _strategy_cache_key — one hash
    implementation for both caches (SSOT).  The payload encodes the resolved
    leg geometry (strike, opt_type, qty, entry_price, T_years, sigma,
    scale_ratio, kind) rather than the raw StrategyRequest fields, because
    the impl has already resolved LTP → sigma by the time this is called.
    span_pct here is the *resolved* span (after σ×√T auto-derivation) so
    the key is stable across re-polls where span_sigmas is identical.
    """
    # Normalise leg geometry — sort by (kind, strike, qty) for stable key
    # regardless of leg submission order.
    canonical_legs = sorted(
        [
            {
                "kind":        l.get("kind") or "opt",
                "strike":      round(float(l.get("strike") or 0), 4),
                "opt_type":    l.get("opt_type") or "",
                "qty":         int(l.get("qty") or 0),
                "entry_price": round(float(l.get("entry_price") or 0), 6),
                "T_years":     round(float(l.get("T_years") or 0), 8),
                "sigma":       round(float(l.get("sigma") or 0), 8),
                "scale_ratio": round(float(l.get("scale_ratio") or 1.0), 8),
            }
            for l in resolved_legs
        ],
        key=lambda d: (d["kind"], d["strike"], d["qty"]),
    )
    payload = json.dumps(
        {
            "legs":        canonical_legs,
            "span_pct":    round(float(span_pct), 8),
            "span_sigmas": round(float(span_sigmas), 6),
            "points":      int(points),
            "time_slices": int(time_slices),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.blake2b(payload, digest_size=16).hexdigest()


def _leg_curve_cache_get(key: str) -> dict | None:
    """Return the cached leg-curve payload if present and within TTL, else None.

    TTL is *sliding* — every successful hit refreshes `last_access`
    (move_to_end also promotes the entry for LRU purposes).  This mirrors
    the access pattern of the Phase 2 cache but uses a 5-minute window
    instead of 5 seconds.
    """
    with _LEG_CURVE_CACHE_LOCK:
        entry = _LEG_CURVE_CACHE.get(key)
        if entry is None:
            return None
        ts, payload = entry
        if time.monotonic() - ts >= _LEG_CURVE_CACHE_TTL:
            _LEG_CURVE_CACHE.pop(key, None)
            return None
        # Refresh last-access timestamp for sliding TTL.
        _LEG_CURVE_CACHE[key] = (time.monotonic(), payload)
        _LEG_CURVE_CACHE.move_to_end(key)
        return payload


def _leg_curve_cache_put(key: str, payload: dict) -> None:
    """Store a leg-curve payload.  Evicts oldest (LRU) when over capacity."""
    with _LEG_CURVE_CACHE_LOCK:
        _LEG_CURVE_CACHE[key] = (time.monotonic(), payload)
        _LEG_CURVE_CACHE.move_to_end(key)
        while len(_LEG_CURVE_CACHE) > _LEG_CURVE_CACHE_SIZE:
            _LEG_CURVE_CACHE.popitem(last=False)


def _strategy_cache_key(data: "StrategyRequest") -> str:
    """Compute a blake2b-16 hex key from the request fields that fully
    determine the response.  The key covers:
      - canonical sorted leg tuples (symbol, qty, avg_cost, ltp, iv, expiry)
      - spot override (None → broker resolves)
      - span_pct, span_sigmas, points, time_slices

    Legs are sorted by (symbol, qty) so leg order doesn't create spurious
    cache misses for the same basket.
    """
    sorted_legs = sorted(
        [
            (
                (leg.symbol or "").upper().strip(),
                int(leg.qty),
                round(float(leg.avg_cost), 6) if leg.avg_cost is not None else None,
                round(float(leg.ltp), 6)      if leg.ltp      is not None else None,
                round(float(leg.iv),  6)       if leg.iv       is not None else None,
                leg.expiry,
            )
            for leg in data.legs
        ],
        key=lambda t: (t[0], t[1]),
    )
    payload = json.dumps(
        {
            "legs":        sorted_legs,
            "spot":        round(float(data.spot), 6) if data.spot is not None else None,
            "span_pct":    round(float(data.span_pct), 6) if data.span_pct is not None else None,
            "span_sigmas": round(float(data.span_sigmas), 6),
            "points":      int(data.points),
            "time_slices": int(data.time_slices),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.blake2b(payload, digest_size=16).hexdigest()


def _strategy_cache_get(key: str) -> Any | None:
    """Return cached StrategyResponse if present and unexpired (TTL=5s),
    else None. Uses time.monotonic() — immune to clock skew / NTP jumps."""
    with _STRATEGY_ANALYTICS_CACHE_LOCK:
        entry = _STRATEGY_ANALYTICS_CACHE.get(key)
        if entry is None:
            return None
        ts, value = entry
        if time.monotonic() - ts >= _STRATEGY_ANALYTICS_CACHE_TTL:
            _STRATEGY_ANALYTICS_CACHE.pop(key, None)
            return None
        _STRATEGY_ANALYTICS_CACHE.move_to_end(key)
        return value


def _strategy_cache_put(key: str, value: Any) -> None:
    """Store a StrategyResponse under key.  Evicts oldest when over capacity."""
    with _STRATEGY_ANALYTICS_CACHE_LOCK:
        _STRATEGY_ANALYTICS_CACHE[key] = (time.monotonic(), value)
        _STRATEGY_ANALYTICS_CACHE.move_to_end(key)
        while len(_STRATEGY_ANALYTICS_CACHE) > _STRATEGY_ANALYTICS_CACHE_SIZE:
            _STRATEGY_ANALYTICS_CACHE.popitem(last=False)


# ── Historical OHLCV in-process cache ─────────────────────────────────
# Keyed by (symbol, exchange_hint, days, interval) → (expires_at_unix, value).
# Mirrors the (key → (expires_at, value)) shape used by backend/api/cache.py.
# Threading.Lock (not asyncio.Lock) because the handler offloads broker
# calls to asyncio.to_thread — the cache reads/writes happen in the async
# frame which is fine with a sync lock on CPython (GIL protects the dict
# mutation; the Lock guards the check-then-set pair).
_HIST_CACHE: "_OrderedDict[tuple, tuple[float, object]]" = _OrderedDict()
_HIST_CACHE_LOCK = _threading.Lock()

_HIST_CACHE_TTL_OK    = 60    # seconds — fresh bars
_HIST_CACHE_TTL_EMPTY = 2     # seconds — empty bars; short TTL so a transient
                              # cold-call (instruments cache not yet warm for the
                              # day, or a single rate-limit blip) doesn't poison
                              # every subsequent reload with "No data available"
                              # for 10 s. 2 s is long enough to coalesce a tight
                              # double-click + slow on the few cases where the
                              # symbol genuinely has no historical bars.
                              # Operator-caught BEL flicker: lowered from 10 → 2.
_HIST_CACHE_MAX_SIZE  = 200   # LRU cap — prevent unbounded growth from chain-picker

# Per-symbol counter for first-cold-empty events. Increments when the
# historical handler returns bars=[] AND the response came back from the
# fallback broker_loop (i.e. ohlcv_store missed too). Operator-visible
# under /api/admin/health.ohlcv_first_cold_empty. If a symbol's counter
# is non-zero AND grows across reloads, the BEL race is still happening
# for that symbol — operator can investigate (instruments map staleness,
# broker rate-limit, missing exchange listing).
# Bucketed by IST date so the counter resets at midnight. Capped at 200
# distinct symbols to prevent unbounded growth.
_FIRST_COLD_EMPTY: dict[tuple[str, str], int] = {}     # (date_iso, symbol) → count
_FIRST_COLD_EMPTY_LOCK = _threading.Lock()
_FIRST_COLD_EMPTY_MAX  = 200


def _record_first_cold_empty(symbol: str) -> None:
    """Increment the per-symbol counter for today (IST). Best-effort —
    swallows any error so the historical handler is never blocked."""
    try:
        from zoneinfo import ZoneInfo
        today_ist = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
    except Exception:
        today_ist = datetime.now().date().isoformat()
    key = (today_ist, symbol.upper().strip())
    with _FIRST_COLD_EMPTY_LOCK:
        # Drop any stale-date entries to keep the dict bounded.
        if len(_FIRST_COLD_EMPTY) >= _FIRST_COLD_EMPTY_MAX:
            stale = [k for k in _FIRST_COLD_EMPTY if k[0] != today_ist]
            for k in stale:
                _FIRST_COLD_EMPTY.pop(k, None)
        _FIRST_COLD_EMPTY[key] = _FIRST_COLD_EMPTY.get(key, 0) + 1


def get_first_cold_empty_counts() -> dict[str, int]:
    """Return a snapshot of today's per-symbol empty-response counter.
    Exposed via /api/admin/health for operator monitoring."""
    try:
        from zoneinfo import ZoneInfo
        today_ist = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
    except Exception:
        today_ist = datetime.now().date().isoformat()
    with _FIRST_COLD_EMPTY_LOCK:
        return {sym: cnt for (d, sym), cnt in _FIRST_COLD_EMPTY.items() if d == today_ist}

# ── Chart self-heal constants ─────────────────────────────────────────
# When ohlcv_store returns fewer bars than _SELF_HEAL_COVERAGE_THRESHOLD
# fraction of the requested days, the handler retries with bypass_cache=True
# to force a full broker fetch and heal the persistent tiers.
#
# This runs regardless of runtime_state.get_mode() ("off" / "soft" / "hard")
# so the self-heal fires in the default-off state without operator action.
#
# The log is rate-limited via the shared _self_heal_log_once helper
# (one INFO per symbol per 60 s) so a hot chart page does not flood the log.

_SELF_HEAL_COVERAGE_THRESHOLD: float = 0.70   # below this fraction → force broker fetch

# Shared throttled logger — imported from canonical SSOT in helpers/.
# Both quote.py (sparkline self-heal) and options.py use the SAME symbol
# so the per-(sym, exch) 60-second throttle table is shared process-wide.
from backend.api.helpers.self_heal_log import _self_heal_log_once

# Process-wide token-resolution cache for the historical-bars endpoint.
# Key: (broker_account, exchange). Value: dict[tradingsymbol_upper, int_token].
#
# Why: the historical endpoint used to call `kite.instruments(EX)` afresh
# per request, walking up to 5 exchanges per broker per symbol. With a
# typical option-chain pull (~20 strikes opened simultaneously), this
# blew past Kite's instruments-endpoint rate limit (the operator saw the
# 17:56:03 storm of "ZG0790 rate-limited" warnings followed by all
# brokers falling through and the chart panel reporting "not found" for
# symbols that DO exist in the MCX dump).
#
# The dump itself is stable for 24 h (Kite refreshes nightly), so we
# cache the resolved {tradingsymbol → token} dict per (account, exchange)
# for 6 h. First miss does the network fetch and populates the cache
# with EVERY symbol in the exchange dump (~15k for MCX, ~75k for NFO);
# all subsequent O(1) lookups are token-only and never re-hit Kite.
#
# 6 h covers operator's full session including the open/close summary
# window without straddling a Kite contract refresh. A separate startup
# warm task could push this to "warm at boot" later if needed.
_INSTRUMENTS_CACHE: dict[tuple[str, str], tuple[float, dict[str, int]]] = {}
_INSTRUMENTS_LOCK  = _threading.Lock()
_INSTRUMENTS_TTL   = 21600   # 6 h

# ── MCX futures price cache (Phase 3) ─────────────────────────────────
# Per-month futures price cache for MCX per-leg scale_ratio computation.
# Key:   (underlying: str, year: int, month: int)
# Value: (expires_at_monotonic: float, fut_symbol: str, price: float)
# TTL:   60s — long enough to survive multiple 5-s re-polls; short enough
#        to pick up intraday price moves that materially affect σ calibration.
#
# Without this cache, every /strategy-analytics POST (even if the strategy-
# analytics body cache misses due to a spot change) triggers N broker.quote()
# calls for each distinct MCX contract month in the basket. A 4-leg JUN/JUL
# spread fired twice = 4 unnecessary round-trips.
#
# Convention: single module-level dict (mirrors _TICK_INDEX, _INSTRUMENTS_CACHE).
# Lock is the same threading.Lock pattern used by the other caches here.
_MCX_FUT_CACHE: dict[tuple[str, int, int], tuple[float, str, float]] = {}
_MCX_FUT_CACHE_LOCK = _threading.Lock()
_MCX_FUT_CACHE_TTL  = 60   # seconds


def _mcx_fut_cache_get(underlying: str, year: int,
                        month: int) -> tuple[str, float] | None:
    """Return (fut_symbol, price) if a non-expired entry exists, else None."""
    key = (underlying.upper(), year, month)
    with _MCX_FUT_CACHE_LOCK:
        entry = _MCX_FUT_CACHE.get(key)
        if entry is None:
            return None
        expires_at, fut_sym, price = entry
        if time.monotonic() >= expires_at:
            _MCX_FUT_CACHE.pop(key, None)
            return None
        return (fut_sym, price)


def _mcx_fut_cache_put(underlying: str, year: int, month: int,
                        fut_sym: str, price: float) -> None:
    """Store a fresh broker-sourced (fut_sym, price) under (underlying, year, month)."""
    key = (underlying.upper(), year, month)
    with _MCX_FUT_CACHE_LOCK:
        _MCX_FUT_CACHE[key] = (time.monotonic() + _MCX_FUT_CACHE_TTL, fut_sym, price)


def _instruments_cache_get(account: str, exchange: str) -> dict[str, int] | None:
    """Return cached {tradingsymbol → token} for the (account, exchange)
    pair if present and unexpired, else None. Empty dicts cache too —
    a known-empty result still saves the network walk."""
    with _INSTRUMENTS_LOCK:
        entry = _INSTRUMENTS_CACHE.get((account, exchange))
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() >= expires_at:
            _INSTRUMENTS_CACHE.pop((account, exchange), None)
            return None
        return value


def _instruments_cache_put(account: str, exchange: str,
                            token_map: dict[str, int]) -> None:
    with _INSTRUMENTS_LOCK:
        _INSTRUMENTS_CACHE[(account, exchange)] = (
            time.monotonic() + _INSTRUMENTS_TTL, token_map,
        )


def _hist_cache_get(key: tuple) -> object | None:
    """Return cached value for *key* if present and unexpired, else None.
    Moves the key to the end (most-recently-used position) on a hit."""
    with _HIST_CACHE_LOCK:
        entry = _HIST_CACHE.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() >= expires_at:
            _HIST_CACHE.pop(key, None)
            return None
        _HIST_CACHE.move_to_end(key)
        return value


def _hist_cache_put(key: tuple, value: object, ttl_seconds: float) -> None:
    """Store *value* under *key* with the given TTL.

    Evicts the oldest entry when the cache exceeds _HIST_CACHE_MAX_SIZE.
    Also does an opportunistic sweep of expired entries so the dict stays
    small even when _MAX_SIZE is never hit (e.g. many unique keys each
    requested only once).
    """
    now = time.monotonic()
    with _HIST_CACHE_LOCK:
        _HIST_CACHE[key] = (now + ttl_seconds, value)
        _HIST_CACHE.move_to_end(key)
        # Evict oldest until under cap. The LRU cap is the only
        # memory-bound that matters — an O(N) stale-entry sweep on
        # every write held the lock for ~200 µs per call against a
        # 200-entry cache, blocking concurrent reads under load.
        # Expired entries get filtered at read time (`_HIST_CACHE_get`
        # checks the per-entry expiry stamp), so this background
        # sweep was pure write-side overhead with no correctness
        # benefit.
        while len(_HIST_CACHE) > _HIST_CACHE_MAX_SIZE:
            _HIST_CACHE.popitem(last=False)


# ── Schemas ───────────────────────────────────────────────────────────

class OptionGreeks(msgspec.Struct):
    delta: float
    gamma: float
    theta: float        # per day
    vega:  float        # per 1 % IV
    rho:   float        # per 1 % rate


class OptionRisk(msgspec.Struct):
    max_profit: float | None      # None = unlimited
    max_loss:   float | None
    breakeven:  float
    pop:        float             # 0..1
    long_short: str               # 'long' / 'short' / 'flat'
    # Expected value of the position at expiry (₹), integrated against
    # the lognormal pdf of the underlying using the calibrated σ. POP
    # tells you "how often you win"; EV tells you "weighted by win/loss
    # magnitudes, what the trade is worth on average".
    ev:           float
    # ev / |entry_cost| as a percentage — return-on-cost. Null when
    # entry_cost is zero (operator hasn't taken the trade yet).
    ev_pct:       float | None
    # Risk:reward = max_profit / |max_loss|. None for unbounded legs
    # (long calls, short puts) where the ratio isn't meaningful.
    rr_ratio:     float | None


class PayoffPoint(msgspec.Struct):
    spot:         float
    today_value:  float
    expiry_value: float


class IntermediateCurve(msgspec.Struct):
    """One time-slice curve between Today (full DTE) and Expiry (T=0).
    `values` is parallel-indexed against the same spot grid as `payoff`,
    so the frontend pairs `intermediate_curves[k].values[i]` with
    `payoff[i].spot`. `label` is a compact display string ("T-3d", "T-12h")
    suitable for the chart legend."""
    label:       str
    elapsed_pct: float       # 0..1 fraction of remaining time elapsed
    days_left:   float       # decimal days remaining at this slice
    values:      list[float]


class SpotResponse(msgspec.Struct):
    """Lightweight spot lookup — used by the chain picker to anchor
    its ATM highlight + auto-scroll on whichever underlying the
    operator picked, regardless of whether it matches the page's
    primary strategy underlying."""
    underlying:            str
    spot:                  float
    spot_source:           str            # 'sim' | 'live' | 'close' | 'depth' | 'futures' | 'fallback'
    spot_prev_close:       float | None
    spot_anchor_contract:  str | None = None  # e.g. CRUDEOIL25JUNFUT when source='futures'


class ChainQuoteRow(msgspec.Struct):
    """One strike's CE + PE top-of-book bid / ask. Any side may be null
    when the broker quote came back empty for that contract or the
    depth book was uncovered."""
    k:        float
    ce_bid:   float | None
    ce_ask:   float | None
    pe_bid:   float | None
    pe_ask:   float | None


class ChainQuotesResponse(msgspec.Struct):
    """Per-strike CE / PE bid / ask map for the chain picker — one
    round-trip populates the inline quote cells next to every Buy /
    Sell / (i) button on both sides of the strike grid."""
    underlying:  str
    expiry:      str
    rows:        list[ChainQuoteRow]


class ChainSnapshotLeg(msgspec.Struct):
    """One side (CE or PE) of one strike — LTP + IV + per-share Greeks.
    All fields nullable so the LLM can see exactly what data was
    available and what was missing (rather than getting silent zeros)."""
    ltp:    float | None
    bid:    float | None
    ask:    float | None
    iv:     float | None          # calibrated implied vol (decimal, e.g. 0.18 = 18%)
    delta:  float | None
    gamma:  float | None
    theta:  float | None          # per-day
    vega:   float | None          # per 1% IV change
    rho:    float | None          # per 1% rate change


class ChainSnapshotRow(msgspec.Struct):
    """One strike with both sides + an `atm_distance` (signed:
    negative for strikes below spot, positive for above)."""
    k:            float
    atm_distance: float
    ce:           ChainSnapshotLeg
    pe:           ChainSnapshotLeg


class ChainSnapshotResponse(msgspec.Struct):
    """One-round-trip option chain with Greeks. Designed for the MCP
    get_options_chain_snapshot tool — saves the LLM from making
    `atm_window * 2 + 1` per-strike get_option_analytics calls when
    planning a multi-leg structure."""
    underlying:       str
    expiry:           str
    spot:             float
    spot_source:      str
    spot_prev_close:  float | None
    days_to_expiry:   float
    risk_free_rate:   float            # used to compute IV / Greeks
    atm_strike:       float | None     # nearest strike to spot
    rows:             list[ChainSnapshotRow]


class OptionAnalyticsResponse(msgspec.Struct):
    # Identification
    mode:          str
    symbol:        str
    underlying:    str
    opt_type:      str            # CE / PE
    strike:        float
    expiry:        str            # ISO date
    days_to_expiry: float

    # Position
    account:       str | None
    qty:           int
    avg_cost:      float          # entry premium per share

    # Pricing block
    spot:          float
    ltp:           float
    iv:            float
    theoretical:   float          # BS at current spot/IV/DTE
    discrepancy:   float          # ltp - theoretical
    discrepancy_pct: float        # %

    # Greeks (per share + position-scaled)
    greeks_per_share: OptionGreeks
    greeks_position:  OptionGreeks

    # Risk + payoff curve
    risk:                OptionRisk
    payoff:              list[PayoffPoint]
    # Intermediate-DTE Black-Scholes curves between Today and Expiry
    # — empty when caller passes time_slices=0 (default). Each entry's
    # `values` is parallel to `payoff` (same spot grid). NO default —
    # msgspec.Struct rejects required fields after optional ones, and
    # the route handler always supplies this list explicitly.
    intermediate_curves: list[IntermediateCurve]

    # Provenance — lets the UI flag stale data with a yellow chip.
    # ltp_source ∈ {'override','sim','live','close','depth','avg_cost'}
    # spot_source ∈ {'override','sim','live','close','depth'}
    # iv_source  ∈ {'override','calibrated','default'}
    ltp_source:   str
    spot_source:  str
    iv_source:    str

    # Payoff x-axis range used. `span_pct` is the actual decimal fraction
    # applied (e.g. 0.06 = ±6 %); `span_sigmas` is the σ-multiple it was
    # derived from (e.g. 2.5 means the chart spans ±2.5σ at expiry). UI
    # shows the σ form in the chart footnote.
    span_pct:     float
    span_sigmas:  float

    # Yesterday's close on the underlying — lets the UI color the SPOT
    # value green/red depending on whether it's traded above or below
    # the prior close. Null when the broker didn't supply ohlc.close
    # (operator override, sim, fallback path).
    spot_prev_close: float | None = None

    # When source='futures' on an MCX commodity, the bare tradingsymbol
    # of the futures contract used as the spot anchor (e.g.
    # 'CRUDEOIL26SEPFUT'). None for index/equity paths and non-futures
    # sources. Lets the UI chip-tip show "Anchored on CRUDEOIL26SEPFUT"
    # so the operator knows which contract the IV was calibrated against.
    spot_anchor_contract: str | None = None


class HistoricalBar(msgspec.Struct):
    ts:     str
    open:   float
    high:   float
    low:    float
    close:  float
    volume: int


class HistoricalResponse(msgspec.Struct):
    symbol:           str
    instrument_token: int | None
    interval:         str
    bars:             list[HistoricalBar]
    # partial=True signals "empty result is likely transient — instruments
    # cache cold, broker rate-limited, or token resolution missed". The
    # frontend treats partial-empty as "keep loading state up + retry soon"
    # instead of immediately rendering "No data available." Operator-caught
    # BEL race: cold first call could return bars=[] with the broker still
    # warming, and the second call (after instruments map populated) had
    # data — the UI was flashing "No data available" between the two.
    partial:          bool = False


# Multi-leg strategy schemas. Each leg can come from any source — live
# broker, simulator, or operator imagination. The route resolves missing
# LTPs by hitting the broker; sim legs supply ltp inline so no broker
# round-trip is needed for them.
class StrategyLeg(msgspec.Struct):
    symbol:    str
    qty:       int                       # signed: + long, − short
    avg_cost:  float | None = None       # per-share entry premium; defaults to ltp
    ltp:       float | None = None       # current premium; fetched from broker if absent
    iv:        float | None = None       # IV override; calibrated from ltp otherwise
    # ISO-date expiry override (YYYY-MM-DD). The backend symbol parser
    # assumes NSE F&O's last-Thursday-of-month rule — fine for NIFTY,
    # BANKNIFTY, RELIANCE, etc., but wrong for MCX commodities (GOLDM
    # expires on the 5th day of the contract month, not the last
    # Thursday). The frontend already has the authoritative expiry in
    # its instruments cache (Kite's own `expiry` field per contract);
    # passing it here lets the backend skip the symbol-parser inference
    # for that leg.
    expiry:    str | None   = None


class StrategyRequest(msgspec.Struct):
    legs:        list[StrategyLeg]
    spot:        float | None = None     # spot override; sim or broker otherwise
    # span_pct=None auto-derives the chart range from σ × √T (using the
    # qty-weighted IV proxy across legs and the shared expiry). Pass an
    # explicit value to override. Clamped to [1%, 50%].
    span_pct:    float | None = None
    # σ-multiple used when span_pct is None. Default 3.0 → tick labels
    # at ±0.5σ, ±1σ, ±1.5σ, ±2σ, ±2.5σ, ±3σ on each side; covers
    # ~99.7 % of the lognormal mass at expiry.
    span_sigmas: float = 3.0
    points:      int   = 51
    # Number of intermediate-DTE Black-Scholes curves to draw between
    # the Today and Expiry lines. Default 0 keeps the response compact;
    # the /admin/options page passes 2 for two-slice (T-33%, T-67%)
    # rendering. Capped at 5.
    time_slices: int   = 0


class LegDetail(msgspec.Struct):
    symbol:       str
    opt_type:     str
    strike:       float
    qty:          int
    avg_cost:     float
    ltp:          float
    iv:           float
    theoretical:  float
    discrepancy:  float
    greeks:       OptionGreeks
    # Provenance per leg — UI flags any leg whose LTP came from a fallback
    # (close / avg_cost) so the operator knows which numbers to trust.
    ltp_source:   str = "live"
    iv_source:    str = "calibrated"


class StrategyRisk(msgspec.Struct):
    max_profit:  float                   # numerical max — only as wide as the curve
    max_loss:    float
    breakevens:  list[float]
    pop:         float                   # 0..1
    # EV: probability-weighted expiry value (lognormal pdf over the
    # curve's spot grid). For credit strategies this is typically slightly
    # positive; for paid-premium debit strategies it depends on whether
    # the breakevens sit inside or outside the lognormal mass.
    ev:          float
    ev_pct:      float | None            # ev / |net_cost| — null when net_cost == 0
    rr_ratio:    float | None            # max_profit / |max_loss| — null when unbounded


class StrategyResponse(msgspec.Struct):
    underlying:        str
    expiry:            str
    days_to_expiry:    float
    spot:              float
    net_cost:          float             # signed: + paid, − collected
    net_qty:           int               # ∑ signed qty (just for the header)
    iv_proxy:          float             # qty-weighted IV used by POP
    aggregate_greeks:    OptionGreeks
    risk:                StrategyRisk
    payoff:              list[PayoffPoint]
    intermediate_curves: list[IntermediateCurve]
    legs:                list[LegDetail]
    # Yesterday's close on the underlying — used by the UI to color
    # the SPOT value green/red depending on the day's direction. Null
    # when the broker didn't supply ohlc.close on the resolving leg.
    spot_prev_close:   float | None = None
    # Same provenance as single-leg /analytics — UI shows ±2.5σ in the
    # chart footnote when span_sigmas is non-zero.
    span_pct:          float = 0.10
    span_sigmas:       float = 0.0
    # True when the basket spans two or more distinct option expiry dates
    # (calendar spread / diagonal). Lets the frontend show a footnote that
    # the X-axis uses front-month as the spot reference, not the per-leg month.
    multi_expiry:      bool  = False
    # Resolved futures tradingsymbol used as the spot anchor when
    # source='futures' (MCX commodities). Null for index/equity paths.
    spot_anchor_contract: str | None = None
    # Provenance of the resolved spot — matches the single-leg
    # /analytics response. Lets the UI distinguish a real broker
    # reading ('live' / 'close' / 'depth' / 'futures' / 'cached') from
    # a synthetic fallback ('fallback' = median-strike anchor) and
    # suppress the spot marker entirely on the chart when the value
    # would be misleading. Earlier responses omitted this field, so
    # the frontend's `strategy.spot_source === 'fallback'` check at
    # the OptionsPayoff callsite was always false — a stale 8129
    # median-strike or stale-cached value would still display as a
    # real spot. Default 'live' so older clients that don't read the
    # field don't trip on a missing-default error.
    spot_source: str = 'live'


# ── Resolvers ─────────────────────────────────────────────────────────

def _resolve_position_sim(symbol: str) -> tuple[int, str, float]:
    """Return (qty, account, avg_cost) from the active SimDriver for symbol."""
    from backend.api.algo.sim.driver import get_driver
    drv = get_driver()
    for r in drv._positions_rows:
        if str(r.get("tradingsymbol", "")).upper() == symbol.upper():
            return (
                int(r.get("quantity") or 0),
                str(r.get("account") or "—"),
                float(r.get("average_price") or 0),
            )
    raise HTTPException(status_code=404, detail=f"sim has no position '{symbol}'")


def _resolve_position_live(symbol: str, account: Optional[str]) -> tuple[int, str, float]:
    """Return (qty, account, avg_cost) from a live broker position lookup."""
    if not account:
        raise HTTPException(status_code=400, detail="live mode requires `account`.")
    from backend.brokers.registry import get_broker
    try:
        positions = get_broker(account).positions() or {}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"broker positions fetch failed: {e}")
    nets = positions.get("net") or positions.get("day") or []
    for p in nets:
        if str(p.get("tradingsymbol", "")).upper() == symbol.upper():
            return (
                int(p.get("quantity") or 0),
                account,
                float(p.get("average_price") or 0),
            )
    raise HTTPException(status_code=404,
                        detail=f"account {account!r} has no position '{symbol}'")


def _resolve_position(mode: str, symbol: str, qty: Optional[int],
                     account: Optional[str], avg_cost: Optional[float]
                     ) -> tuple[int, str | None, float]:
    """
    Resolve (qty, account, avg_cost) for the requested mode. Hypothetical
    mode lets the operator pre-trade-analyze any symbol with a default qty
    of 1 lot (or the lot size if we can derive it).
    """
    if mode == "hypothetical":
        # Default qty = 1 share long; operator can override via query.
        return (int(qty) if qty is not None else 1,
                account, float(avg_cost) if avg_cost is not None else 0.0)

    if mode == "sim":
        return _resolve_position_sim(symbol)
    return _resolve_position_live(symbol, account)


def _resolve_span_pct(*, sigma: float, T_years: float,
                      span_pct: Optional[float],
                      span_sigmas: float = 3.0) -> float:
    """
    Pick the payoff-curve x-axis span. When the operator passed an
    explicit `span_pct` override, use that. Otherwise derive from the
    underlying's standard deviation at expiry:

        span_pct = span_sigmas × σ × √T_years

    σ × √T is the annualized vol scaled to the option's time-to-expiry
    (so a 7-DTE 15% IV option spans ±~5% at 2.5σ; a 60-DTE same-IV
    option spans ±~15%). Keeps the chart "tight enough to show the
    interesting region" without manual span tuning per contract.

    Clamped to [2%, 50%] so degenerate inputs (σ=0, T=0, or absurdly
    long-dated contracts) still produce a readable chart.
    """
    if span_pct is not None and span_pct > 0:
        return max(0.01, min(float(span_pct), 0.5))
    if sigma > 0 and T_years > 0:
        derived = float(span_sigmas) * sigma * math.sqrt(T_years)
        return max(0.02, min(derived, 0.5))
    # σ=0 / T=0 — fall back to a reasonable default so the operator
    # doesn't see a zero-width chart.
    return 0.10


def _finite_or_null(x: float) -> "float | None":
    """Return None for ±inf so msgspec doesn't choke; UI renders '∞'."""
    return None if x == float("inf") or x == float("-inf") else x


def _resolve_iv_for_analytics(
    iv: Optional[float],
    ltp_val: float,
    ltp_src: str,
    S: float,
    K: float,
    T_yrs: float,
    opt_type: str,
) -> tuple[float, str]:
    """Resolve the implied-vol (sigma) to use for analytics, plus the source label.

    Priority: explicit override > calibrated from LTP > default IV.
    Falls back to 'default' when calibration round-trips on an estimated LTP.
    """
    if iv is not None and iv > 0:
        return float(iv), "override"
    calibrated = implied_vol(ltp_val, S, K, T_yrs, DEFAULT_RISK_FREE, opt_type)
    # When the calibrated value equals DEFAULT_IV (bisection failed / degenerate
    # inputs), or when the LTP itself came from a fallback source, treat as default
    # so the UI can flag lower confidence.
    if calibrated == DEFAULT_IV or ltp_src in ("close", "depth", "avg_cost", "estimated"):
        return calibrated, "default" if calibrated == DEFAULT_IV else "calibrated"
    return calibrated, "calibrated"


def _depth_mid(depth: dict) -> "Optional[float]":
    """Return bid+ask midpoint from a Kite depth dict, or None when unavailable."""
    buy  = (depth.get("buy")  or [{}])[0]
    sell = (depth.get("sell") or [{}])[0]
    bid  = buy.get("price")
    ask  = sell.get("price")
    if bid and ask and bid > 0 and ask > 0:
        return (float(bid) + float(ask)) / 2.0
    return None


def _ltp_from_quote(q: dict) -> tuple[Optional[float], str]:
    """
    Pick the best price out of a Kite quote dict. Order:
      1. last_price (live, freshest)
      2. ohlc.close (previous-day close — stale but real)
      3. depth mid (bid+ask)/2 if both present
    Returns `(price, source)` where source ∈ {'live','close','depth','none'}.
    """
    if not q:
        return (None, "none")
    lp = q.get("last_price")
    if lp not in (None, 0, 0.0):
        return (float(lp), "live")
    ohlc  = q.get("ohlc") or {}
    close = ohlc.get("close")
    if close not in (None, 0, 0.0):
        return (float(close), "close")
    mid = _depth_mid(q.get("depth") or {})
    if mid is not None:
        return (mid, "depth")
    return (None, "none")


def _prev_close_from_quote(q: dict) -> float | None:
    """Yesterday's close from a Kite quote dict. Used by callers that
    want a sign cue ("today's spot vs yesterday's close") for the
    operator. Returns None when the broker didn't supply ohlc.close."""
    if not q:
        return None
    close = (q.get("ohlc") or {}).get("close")
    if close in (None, 0, 0.0):
        return None
    try:
        return float(close)
    except (TypeError, ValueError):
        return None


def _mcx_fut_candidates(items: list, underlying: str) -> list:
    """Filter instruments list to MCX FUT contracts for the given underlying."""
    target_u = underlying.upper()
    return sorted(
        [
            inst for inst in items
            if (inst.e == "MCX"
                and inst.t == "FUT"
                and (inst.u or "").upper() == target_u
                and inst.x)
        ],
        key=lambda i: i.x or "",
    )


async def _lookup_mcx_future(underlying: str,
                             expiry_hint: date) -> Optional[str]:
    """Return the MCX FUT tradingsymbol for *underlying* whose expiry
    falls in the same month/year as *expiry_hint*, falling back to the
    nearest available future when no same-month contract is found.

    Uses the shared instruments cache (24h TTL, warmed at startup) so
    the returned tradingsymbol matches Kite's actual format (e.g.
    ``CRUDEOIL26MAY19FUT``) rather than the constructed approximation
    that ``futures_symbol_for_expiry`` produces."""
    from backend.api.cache import get_or_fetch
    from backend.api.routes.instruments import _fetch_instruments, _TTL_SECONDS
    try:
        resp = await get_or_fetch("instruments", _fetch_instruments,
                                  ttl_seconds=_TTL_SECONDS)
        items = resp.items if resp else []
    except Exception:
        items = []
    if not items:
        return None
    candidates = _mcx_fut_candidates(items, underlying)
    if not candidates:
        return None
    # Prefer same-month/year; else earliest expiry (near-month fallback).
    same_month = _lookup_mcx_same_month(candidates, expiry_hint)
    return same_month if same_month is not None else candidates[0].s


def _lookup_mcx_same_month(candidates: list, expiry_hint: date) -> Optional[str]:
    """Iterate instrument candidates and return the tradingsymbol of the first
    whose expiry year+month matches expiry_hint. Returns None when no match
    is found (caller falls back to candidates[0])."""
    for inst in candidates:
        try:
            exp = date.fromisoformat(inst.x[:10])
            if exp.year == expiry_hint.year and exp.month == expiry_hint.month:
                return inst.s
        except (TypeError, ValueError):
            pass
    return None


# `_lookup_mcx_near_month_future` moved to
# `derivatives.lookup_mcx_front_month_future` so paper / sim / chart
# code can reuse the same "today's liquid contract" rule. Kept as a
# breadcrumb for anyone grep'ing the old name.


# Per-underlying last-known-spot cache. 24 h TTL so a transient broker
# failure (rate limit, network blip, instruments cache cold) doesn't
# poison the payoff chart by falling through to the median-strike value
# of the operator's option book. Tuple shape:
#   (expires_at_monotonic, spot_px, source, prev_close, anchor_contract)
# Source carries 'cached' when read back so the UI knows the data is
# stale-but-real (vs 'fallback' which means synthetic strike anchor).
#
# Key shape: (underlying, anchor) — anchor is the resolving futures
# tradingsymbol when the spot came from a futures proxy (every MCX
# commodity goes through this path; NSE/NFO falls through when the
# spot ticker miss-fires). Indices / stocks with a working NSE spot
# ticker land in the cache with anchor=None, so they all dedupe to
# one entry per underlying.
#
# Why the compound key matters: CRUDEOIL has no spot — JUN options
# are anchored to CRUDEOIL26JUNFUT and JUL options to CRUDEOIL26JULFUT.
# The two futures trade at different prices (calendar spread). The
# earlier single-key shape (`underlying` only) let JUL queries
# overwrite the JUN entry, so a JUN-month broker failure would
# silently return the JUL price as the JUN spot — wrong month, wrong
# basis. Compound key keeps per-month entries isolated.
_LAST_KNOWN_SPOT: dict[tuple[str, Optional[str]],
                       tuple[float, float, str, Optional[float], Optional[str]]] = {}
_LAST_KNOWN_SPOT_LOCK = _threading.Lock()
_LAST_KNOWN_SPOT_TTL = 86400  # 24 h


def _spot_cache_get(underlying: str,
                     anchor: Optional[str] = None
                     ) -> tuple[float, str, Optional[float], Optional[str]] | None:
    """Return (spot, 'cached', prev_close, anchor) if a non-expired cache
    entry exists, else None. Always relabels the source as 'cached'
    so the UI distinguishes stale-but-real data from a fresh broker
    fetch.

    Lookup order:
      1. Exact (underlying, anchor) — the caller asked for a specific
         futures-anchored spot (e.g. JUN's spot for a JUN option).
      2. Exact (underlying, None) — the no-anchor / spot-ticker entry
         (indices, stocks). Useful when the caller has no anchor and
         hopes any cached spot is good enough.

    No fuzzy fallback across different anchors: a JUL-anchored entry
    is NEVER returned for a JUN-anchored query (the whole point of
    the per-month key shape). When neither key hits, we return None
    and the resolver falls through to its synthetic-strike fallback.
    """
    with _LAST_KNOWN_SPOT_LOCK:
        keys = [(underlying, anchor)]
        # Indices / stocks: the spot-ticker entry is keyed (underlying, None).
        # If the caller passed a futures anchor and we have no per-anchor
        # entry, fall back to the spot-ticker entry. For commodities the
        # spot-ticker entry never exists (no NSE spot for MCX), so this
        # second probe just no-ops.
        if anchor is not None:
            keys.append((underlying, None))
        for k in keys:
            entry = _LAST_KNOWN_SPOT.get(k)
            if entry is None:
                continue
            expires_at, px, _orig_src, prev_close, anc = entry
            if time.monotonic() >= expires_at:
                _LAST_KNOWN_SPOT.pop(k, None)
                continue
            return (px, "cached", prev_close, anc)
        return None


def _spot_cache_put(underlying: str, px: float, src: str,
                     prev_close: Optional[float], anchor: Optional[str]) -> None:
    """Write a fresh broker-sourced spot into the cache. Never called
    with 'fallback' source — only real broker data gets cached, so
    a cache read can never reconstruct the median-strike anchor.

    Keyed by (underlying, anchor) so JUN and JUL futures cache
    independently. Indices / stocks pass anchor=None and dedupe to
    a single entry per underlying."""
    if px <= 0 or src in ("fallback", "cached"):
        return
    with _LAST_KNOWN_SPOT_LOCK:
        _LAST_KNOWN_SPOT[(underlying, anchor)] = (
            time.monotonic() + _LAST_KNOWN_SPOT_TTL,
            float(px), src, prev_close, anchor,
        )


async def _close_from_db(underlying: str) -> Optional[float]:
    """Return the best available close price for `underlying` from the DB.

    Two tiers (in order):
      1. ``daily_book`` where kind='holdings', symbol=underlying, ltp > 0,
         latest ``captured_at`` — the operator's actual last in-session LTP,
         more authoritative than broker ohlc.close during the overnight window.
      2. ``ohlcv_daily`` close for the most recent row where exchange IN
         ('NSE', 'BSE') — the canonical prior-close once tier 1 is cold.

    Returns the float price, or None when neither tier has data.
    Performance target: one indexed SQL round-trip (<50 ms).
    Reuses the same ``async_session`` pattern as other helpers in this module's
    sibling routes (audit.py, auth.py, metrics.py). UX note: N/A (backend-only).
    """
    from sqlalchemy import text as _sa_text
    from backend.api.database import async_session

    try:
        async with async_session() as session:
            # Tier 1: daily_book.ltp for holdings (last in-session value).
            # The ix_daily_book_kind_acct_sym_captured index (kind, account,
            # symbol, captured_at) supports this ORDER BY DESC lookup.
            row = await session.execute(
                _sa_text(
                    "SELECT ltp FROM daily_book "
                    "WHERE kind = 'holdings' AND symbol = :sym AND ltp > 0 "
                    "ORDER BY captured_at DESC LIMIT 1"
                ),
                {"sym": underlying},
            )
            result = row.fetchone()
            if result is not None:
                val = float(result[0])
                if val > 0:
                    return val

            # Tier 2: ohlcv_daily close — prefer NSE over BSE.
            row2 = await session.execute(
                _sa_text(
                    "SELECT close FROM ohlcv_daily "
                    "WHERE symbol = :sym AND exchange IN ('NSE', 'BSE') "
                    "ORDER BY exchange ASC, date DESC LIMIT 1"
                ),
                {"sym": underlying},
            )
            result2 = row2.fetchone()
            if result2 is not None:
                val2 = float(result2[0])
                if val2 > 0:
                    return val2
    except Exception as exc:
        logger.warning("_close_from_db for %s failed: %s", underlying, exc)

    return None


async def _spot_last_resort(
    underlying: str,
    cache_anchor: "Optional[str]",
    fallback: "Optional[float]",
) -> tuple[float, str, "Optional[float]", "Optional[str]"]:
    """Last-resort spot chain: last-known-spot cache → DB close → caller fallback → 502."""
    cached = _spot_cache_get(underlying, cache_anchor)
    if cached is not None:
        return cached  # (px, "cached", prev_close, anchor)

    db_close = await _close_from_db(underlying)
    if db_close is not None:
        logger.info("options spot for %s resolved via close-db: %.4f", underlying, db_close)
        return (db_close, "close-db", db_close, None)

    if fallback is not None and fallback > 0:
        logger.warning(
            "strategy spot for %s fell through to source='fallback' "
            "(spot=%.2f, anchor=None). Live + futures lookups failed; "
            "UI will suppress the spot marker.",
            underlying, float(fallback),
        )
        return (float(fallback), "fallback", None, None)

    raise HTTPException(status_code=502,
                        detail=f"spot for {underlying} unavailable from any source")


async def _resolve_spot(underlying: str, override: Optional[float],
                        *, fallback: Optional[float] = None,
                        expiry_hint: Optional[date] = None,
                        option_symbol: Optional[str] = None
                        ) -> tuple[float, str, Optional[float], Optional[str]]:
    """Spot for the underlying. Returns `(spot, source, prev_close, anchor_contract)`
    so the UI can flag stale data and color the spot value against
    yesterday's close. Sources: 'override' | 'sim' | 'live' | 'close'
    | 'depth' | 'futures' | 'fallback'. `prev_close` is None when
    the broker didn't include ohlc.close on the resolving leg
    (overrides, sim, fallback). `anchor_contract` is the resolved
    futures tradingsymbol when source='futures' (MCX commodities),
    else None.

    Resolution order:
      1. Operator override
      2. Active sim driver state
      3. Spot ticker (NSE:NIFTY 50, NSE:RELIANCE, …) — but skipped for
         MCX commodities since they have no NSE spot
      4. Matching monthly **futures** contract (MCX:CRUDEOIL25MAYFUT,
         NFO:RELIANCE25MAYFUT, …). Required for commodities; serves as
         a real-data fallback for indices/stocks when the spot fails.
      5. Operator-supplied `fallback` (typically the median strike) —
         last-resort sanity anchor when every quote path fails.

    When even the fallback isn't supplied AND every quote path fails,
    raises 502 — without `expiry_hint` we have no way to pick a
    futures contract for commodities.

    Sub-steps delegated to options_helpers: _resolve_spot_from_sim,
    _resolve_spot_ticker, _resolve_commodity_spot.
    """
    from backend.api.routes.options_helpers import (
        _resolve_spot_from_sim,
        _resolve_spot_ticker,
        _resolve_commodity_spot,
    )

    # 1. Operator override.
    if override is not None and override > 0:
        return (float(override), "override", None, None)

    # 2. Active SimDriver.
    sim_px = await _resolve_spot_from_sim(underlying)
    if sim_px is not None:
        return (sim_px, "sim", None, None)

    from backend.brokers.registry import get_market_data_broker
    broker = get_market_data_broker()
    is_commodity = is_mcx_underlying(underlying)

    # 3. Spot ticker — skipped for MCX commodities (no NSE spot).
    if not is_commodity:
        ticker_result = await _resolve_spot_ticker(
            underlying, broker, _spot_cache_put, _ltp_from_quote, _prev_close_from_quote,
        )
        if ticker_result is not None:
            return ticker_result

    # 4. Futures (instruments-cache lookup + walk-forward).
    #    Also determines the cache anchor for the last-known-spot lookup.
    px, src, prev, resolved_sym, anchor = await _resolve_commodity_spot(
        underlying, expiry_hint, option_symbol,
        broker, _spot_cache_put, _ltp_from_quote, _prev_close_from_quote,
    )
    if px is not None:
        return (px, src, prev, anchor)

    # Last-known-spot cache / DB / caller fallback / error.
    cache_anchor = resolved_sym if is_commodity else None
    return await _spot_last_resort(underlying, cache_anchor, fallback)


# Option-quote-key helper moved to `derivatives.option_quote_key`.
# This route module imports it directly. Keep the comment as a
# breadcrumb for anyone searching the old name.


def _leg_expiry_iso(leg, parsed: dict) -> str:
    """Pick the most authoritative expiry for a strategy leg. The
    frontend ships Kite's actual `expiry` field from its instruments
    cache (per-contract), which is correct for every exchange. When the
    frontend hasn't supplied an override, fall back to the parsed
    symbol's expiry — the parser uses last-Thursday for NSE/NFO equity
    options and last-Friday for MCX commodity options (GOLDM, GOLD,
    SILVER, CRUDEOIL, etc.). MCX futures follow per-commodity rules that
    don't fit a single fallback (GOLDM ≈ 5th, CRUDEOIL ≈ 19th,
    NATURALGAS ≈ 25th, base metals ≈ last day), so the parser uses
    last-Thursday for them too — strategy callers ALWAYS pass
    leg.expiry from the Kite cache, so the fallback only matters for
    callers that don't (e.g. analytics endpoints invoked with a bare
    tradingsymbol)."""
    if leg.expiry:
        try:
            # Validate ISO format; raises on garbage so the parser
            # fallback kicks in.
            return date.fromisoformat(leg.expiry).isoformat()
        except (ValueError, TypeError):
            pass
    return parsed["expiry"].isoformat()


def _ltp_sim_lookup(symbol: str) -> Optional[tuple[float, str]]:
    """Return (price, 'sim') if a SimDriver row matches *symbol*, else None."""
    from backend.api.algo.sim.driver import get_driver
    for r in get_driver()._positions_rows:
        if str(r.get("tradingsymbol", "")).upper() == symbol.upper():
            lp = r.get("last_price")
            if lp not in (None, 0, 0.0):
                return (float(lp), "sim")
    return None


async def _ltp_broker_quote(symbol: str) -> Optional[tuple[float, str]]:
    """Fetch a live broker quote for *symbol*; return (price, src) or None."""
    from backend.brokers.registry import get_market_data_broker
    key = option_quote_key(symbol)
    try:
        resp = await asyncio.to_thread(get_market_data_broker().quote, [key]) or {}
    except Exception as e:
        logger.warning(f"options LTP quote() failed for {symbol}: {e}")
        resp = {}
    price, src = _ltp_from_quote(resp.get(key) or {})
    return (price, src) if price is not None else None


def _ltp_bs_estimate(estimate_inputs: dict) -> Optional[tuple[float, str]]:
    """Synthesise an LTP via Black-Scholes at DEFAULT_IV as the last resort.
    Returns (price, 'estimated') when inputs are valid, else None.
    """
    S = float(estimate_inputs.get("spot") or 0)
    K = float(estimate_inputs.get("strike") or 0)
    T = float(estimate_inputs.get("T_years") or 0)
    opt = str(estimate_inputs.get("opt_type") or "CE")
    if S > 0 and K > 0 and T > 0:
        est = black_scholes(S, K, T, DEFAULT_RISK_FREE, DEFAULT_IV, opt)
        if est > 0:
            return (est, "estimated")
    return None


async def _resolve_ltp(symbol: str, mode: str, account: Optional[str],
                 override: Optional[float],
                 avg_cost_hint: Optional[float] = None,
                 *,
                 estimate_inputs: Optional[dict] = None
                 ) -> tuple[float, str]:
    """
    LTP for an option contract with full fallback chain:
      override > sim-row > broker quote(last_price > close > depth-mid)
                  > avg_cost_hint > BS-estimated.
    Returns `(price, source)` so the UI can flag stale prices. Sources:
    'override' | 'sim' | 'live' | 'close' | 'depth' | 'avg_cost' |
    'estimated'.

    `estimate_inputs` (when provided) lets the resolver synthesise an
    estimated LTP via Black-Scholes at default IV when nothing else
    works. Shape: `{'spot': S, 'strike': K, 'T_years': T, 'opt_type': 'CE'}`.
    With this, the page never returns 502 — the payoff still draws
    against an estimated price, and the UI shows an 'estimated' chip.
    """
    # Treat 0 / negative explicit overrides as "no override" so a sim
    # leg or picker that copied last_price=0 falls through to broker
    # fallbacks instead of locking in an obviously wrong number.
    if override is not None and override > 0:
        return (float(override), "override")

    if mode == "sim":
        hit = _ltp_sim_lookup(symbol)
        if hit is not None:
            return hit
        # Sim mode but no row — operator may be requesting a contract
        # outside the sim. Fall through to broker fallbacks (handy when
        # the sim is paused but real-data analytics are still useful).

    broker_hit = await _ltp_broker_quote(symbol)
    if broker_hit is not None:
        return broker_hit

    if avg_cost_hint is not None and avg_cost_hint > 0:
        return (float(avg_cost_hint), "avg_cost")

    # Final fallback — synthesise an LTP via Black-Scholes at default IV.
    # The payoff curve still renders something the operator can read;
    # the UI shows 'estimated' so they know not to trust absolute P&L.
    if estimate_inputs:
        bs_hit = _ltp_bs_estimate(estimate_inputs)
        if bs_hit is not None:
            return bs_hit

    raise HTTPException(
        status_code=502,
        detail=(f"No LTP available for '{symbol}' from any source "
                f"(broker quote, sim, avg_cost). "
                f"Pass `ltp=<value>` to override.")
    )


# ── Strategy-analytics helpers ──────────────────────────────────────────
# Extracted from OptionsController._strategy_analytics_impl (cyclomatic
# hotspot, cc=120 → target < 30). Each helper is a pure function operating
# on the fully-parsed inputs so unit tests can exercise them without a live
# broker mock. The impl is now a thin orchestrator that composes them.
# Any HTTPException raised inside a helper surfaces unchanged.


def _strategy_validate_and_parse(data: "StrategyRequest") -> dict:
    """Validate the request shape and pre-parse every leg symbol.

    Returns:
        parsed_by_sym — {UPPERCASE_SYM: parse_tradingsymbol(sym) | None}

    Raises HTTPException(400) if data.legs is empty.
    """
    if not data.legs:
        raise HTTPException(status_code=400, detail="legs is required")
    return {
        (leg.symbol or "").upper().strip(): parse_tradingsymbol((leg.symbol or "").upper().strip())
        for leg in data.legs
    }


def _strategy_collect_leg_metadata(
    data: "StrategyRequest",
    parsed_by_sym: dict,
) -> tuple[set[str], set[str], dict[str, str]]:
    """Validate each leg + collect roots/expiries + build need_quote map.

    Returns:
        (roots, expiries, need_quote)
          - roots: {parsed[root]} across all legs
          - expiries: {leg_expiry_iso} across all legs
          - need_quote: {option_quote_key(sym): sym} for legs whose LTP
            wasn't supplied by the caller (ltp<=0 → broker fetch)

    Raises HTTPException(400) for empty/unrecognised legs or mixed roots.
    """
    roots: set[str] = set()
    expiries: set[str] = set()
    need_quote: dict[str, str] = {}
    for leg in data.legs:
        sym = (leg.symbol or "").upper().strip()
        if not sym:
            raise HTTPException(status_code=400, detail="leg.symbol is required")
        parsed = parsed_by_sym.get(sym)
        if not parsed or parsed.get("kind") not in ("opt", "fut"):
            raise HTTPException(
                status_code=400,
                detail=f"'{sym}' isn't a recognised option or futures contract."
            )
        roots.add(parsed["root"])
        leg_expiry = _leg_expiry_iso(leg, parsed)
        expiries.add(leg_expiry)
        # SIM leg LTP fast path: only queue legs that need a broker quote.
        if leg.ltp is None or leg.ltp <= 0:
            need_quote[option_quote_key(sym)] = sym
    if len(roots) > 1:
        raise HTTPException(
            status_code=400,
            detail=f"All legs must share an underlying; got {sorted(roots)}"
        )
    return roots, expiries, need_quote


def _strategy_anchor_from_modal_expiry(
    leg_expiries: list[tuple[str, str, str]],
) -> tuple[Optional[str], Optional[date]]:
    """Counter-based modal-expiry picker with option-preferred anchor.

    leg_expiries: list of (expiry_iso, kind, symbol) tuples.
    Returns (anchor_symbol, expiry_hint) where expiry_hint uses
    date.fromisoformat (stdlib canonical).
    Tie-break: front-month (earliest expiry) wins when two months
    have equal leg counts.
    """
    from collections import Counter
    expiry_counts = Counter(e for (e, _k, _s) in leg_expiries)
    modal_expiry = min(
        expiry_counts.keys(),
        key=lambda e: (-expiry_counts[e], e),
    )
    modal_legs = [(k, s) for (e, k, s) in leg_expiries if e == modal_expiry]
    option_modal = next((s for (k, s) in modal_legs if k == "opt"), None)
    futures_modal = next((s for (k, s) in modal_legs if k == "fut"), None)
    anchor_symbol = option_modal or futures_modal
    try:
        expiry_hint: Optional[date] = date.fromisoformat(modal_expiry)
    except Exception:
        expiry_hint = None
    return anchor_symbol, expiry_hint


def _strategy_pick_spot_anchor(
    data: "StrategyRequest",
    parsed_by_sym: dict,
) -> tuple[Optional[str], Optional[date]]:
    """Pick the spot ANCHOR leg — modal-expiry, option-preferred, front-month
    tie-break. See the exhaustive rationale block in the caller.

    Returns:
        (anchor_symbol, expiry_hint)
        Falls back to legs[0] symbol if none of the legs parse (defensive).
    """
    leg_expiries: list[tuple[str, str, str]] = []
    for leg in data.legs:
        p = parsed_by_sym.get(leg.symbol.upper().strip())
        if not p:
            continue
        kind = p.get("kind") or ""
        expiry_iso = _leg_expiry_iso(leg, p)
        leg_expiries.append((expiry_iso, kind, leg.symbol.upper().strip()))
    anchor_symbol: Optional[str] = None
    expiry_hint: Optional[date] = None
    if leg_expiries:
        anchor_symbol, expiry_hint = _strategy_anchor_from_modal_expiry(leg_expiries)
    if anchor_symbol is None and data.legs:
        anchor_symbol = data.legs[0].symbol
    return anchor_symbol, expiry_hint


def _strategy_option_T_range(
    data: "StrategyRequest",
    parsed_by_sym: dict,
    close_time: tuple[int, int],
) -> tuple[float, float]:
    """Compute (eval_T, T_yrs_shared) — earliest and latest option leg T_years.
    Returns (0.0, 0.0) if the basket has no option legs (fut-only).
    """
    _option_T_list = [
        days_to_expiry(
            date.fromisoformat(_leg_expiry_iso(leg, parsed_by_sym.get(leg.symbol.upper().strip()))),
            close_time=close_time,
        ) / 365.0
        for leg in data.legs
        if parsed_by_sym.get(leg.symbol.upper().strip()) and
           parsed_by_sym.get(leg.symbol.upper().strip()).get("kind") == "opt"
    ]
    eval_T = min(_option_T_list) if _option_T_list else 0.0
    T_yrs_shared = max(_option_T_list) if _option_T_list else 0.0
    return eval_T, T_yrs_shared


def _mcx_collect_month_keys(
    data: "StrategyRequest",
    parsed_by_sym: dict,
    underlying: str,
) -> tuple[dict[tuple[int, int], tuple[str, float]], dict[tuple[int, int], Optional[str]]]:
    """Phase 1: Partition legs by expiry month into cached vs. needs-lookup buckets."""
    month_to_cached: dict[tuple[int, int], tuple[str, float]] = {}
    month_to_fut_sym: dict[tuple[int, int], Optional[str]] = {}
    for _leg in data.legs:
        _lsym = _leg.symbol.upper().strip()
        _lparsed = parsed_by_sym.get(_lsym)
        if not _lparsed:
            continue
        _leg_exp = date.fromisoformat(_leg_expiry_iso(_leg, _lparsed))
        _mk = (_leg_exp.year, _leg_exp.month)
        if _mk in month_to_cached or _mk in month_to_fut_sym:
            continue
        _cached = _mcx_fut_cache_get(underlying, _leg_exp.year, _leg_exp.month)
        if _cached is not None:
            month_to_cached[_mk] = _cached
        else:
            month_to_fut_sym[_mk] = None
    return month_to_cached, month_to_fut_sym


async def _mcx_resolve_fut_symbols(
    underlying: str,
    month_to_fut_sym: dict[tuple[int, int], Optional[str]],
) -> None:
    """Phase 2: Resolve future tradingsymbols for months not in cache (in-place)."""
    for _mk in list(month_to_fut_sym.keys()):
        _hint = date(_mk[0], _mk[1], 1)
        try:
            _fut_sym = await _lookup_mcx_future(underlying, _hint)
            month_to_fut_sym[_mk] = _fut_sym
        except Exception:
            pass


async def _mcx_batch_quote_futures(
    underlying: str,
    month_to_fut_sym: dict[tuple[int, int], Optional[str]],
    month_to_cached: dict[tuple[int, int], tuple[str, float]],
    price_broker,
) -> None:
    """Phase 3: Batch-quote MCX futures and populate month_to_cached (in-place)."""
    _fut_quote_keys = [f"MCX:{fs}" for fs in month_to_fut_sym.values() if fs]
    _fut_quote_resp: dict = {}
    if _fut_quote_keys:
        try:
            _fut_quote_resp = await asyncio.to_thread(price_broker.quote, _fut_quote_keys) or {}
        except Exception as _e:
            logger.warning(
                f"MCX per-leg futures batch quote failed: {_e}; "
                "falling back to scale_ratio=1 for all legs"
            )
    _mcx_populate_from_quote_resp(underlying, month_to_fut_sym, month_to_cached, _fut_quote_resp)


def _mcx_populate_from_quote_resp(
    underlying: str,
    month_to_fut_sym: dict,
    month_to_cached: dict,
    _fut_quote_resp: dict,
) -> None:
    """Iterate month_to_fut_sym, call _ltp_from_quote on each futures symbol,
    and populate month_to_cached in-place. Skips months with no resolved symbol."""
    for _mk, _fut_sym in month_to_fut_sym.items():
        if not _fut_sym:
            continue
        _qdict = _fut_quote_resp.get(f"MCX:{_fut_sym}") or {}
        _s_fresh, _ = _ltp_from_quote(_qdict)
        if _s_fresh and _s_fresh > 0:
            _mcx_fut_cache_put(underlying, _mk[0], _mk[1], _fut_sym, _s_fresh)
            month_to_cached[_mk] = (_fut_sym, _s_fresh)


async def _strategy_mcx_scale_ratios(
    data: "StrategyRequest",
    parsed_by_sym: dict,
    underlying: str,
    S: float,
    price_broker,
) -> dict[str, float]:
    """For MCX baskets, build per-leg scale_ratio = S_leg_current / S_near.
    Non-MCX + fallback → scale_ratio = 1.0 (caller reads via .get(sym, 1.0)).
    """
    _leg_scale_ratios: dict[str, float] = {}
    if not (S > 0):
        return _leg_scale_ratios

    month_to_cached, month_to_fut_sym = _mcx_collect_month_keys(
        data, parsed_by_sym, underlying)
    await _mcx_resolve_fut_symbols(underlying, month_to_fut_sym)
    await _mcx_batch_quote_futures(underlying, month_to_fut_sym, month_to_cached, price_broker)

    for _leg in data.legs:
        _lsym = _leg.symbol.upper().strip()
        _lparsed = parsed_by_sym.get(_lsym)
        if not _lparsed:
            _leg_scale_ratios[_lsym] = 1.0
            continue
        _leg_exp = date.fromisoformat(_leg_expiry_iso(_leg, _lparsed))
        _mk = (_leg_exp.year, _leg_exp.month)
        _month_data = month_to_cached.get(_mk)
        if _month_data:
            _fut_sym_leg, _s_leg = _month_data
            _leg_scale_ratios[_lsym] = _s_leg / S if _s_leg > 0 else 1.0
        else:
            _leg_scale_ratios[_lsym] = 1.0
    return _leg_scale_ratios


def _strategy_build_futures_leg(
    leg,
    sym: str,
    quote_resp: dict,
    S_leg: float,
    scale_ratio: float,
    qty: int,
) -> tuple[dict, dict]:
    """Resolve a futures leg → (resolved_leg dict, leg_detail dict).
    LTP chain: operator override → broker quote → avg_cost → S_leg estimate.
    """
    fut_ltp: Optional[float] = None
    fut_src: str = "none"
    if leg.ltp is not None and leg.ltp > 0:
        fut_ltp, fut_src = float(leg.ltp), "override"
    else:
        q = quote_resp.get(option_quote_key(sym)) or {}
        fut_ltp, fut_src = _ltp_from_quote(q)
    if fut_ltp is None and leg.avg_cost is not None and leg.avg_cost > 0:
        fut_ltp, fut_src = float(leg.avg_cost), "avg_cost"
    if fut_ltp is None or fut_ltp <= 0:
        fut_ltp, fut_src = float(S_leg), "estimated"
    fut_entry = float(leg.avg_cost) if leg.avg_cost is not None else fut_ltp
    resolved = {
        "kind":        "fut",
        "qty":         qty,
        "entry_price": fut_entry,
        "scale_ratio": scale_ratio,
    }
    fut_g = {"delta": 1.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}
    detail = {
        "symbol":      sym,
        "opt_type":    "FUT",
        "strike":      0.0,
        "qty":         qty,
        "avg_cost":    fut_entry,
        "ltp":         fut_ltp,
        "iv":          0.0,
        "theoretical": fut_ltp,
        "discrepancy": 0.0,
        "greeks":      fut_g,
        "ltp_source":  fut_src,
        "iv_source":   "n/a",
    }
    return resolved, detail


def _strategy_ltp_from_leg_or_quote(
    leg,
    sym: str,
    quote_resp: dict,
) -> tuple[Optional[float], str]:
    """First two steps of the option LTP chain: operator override → broker quote.
    Returns (ltp_val, ltp_source) where ltp_val may be None when both miss."""
    if leg.ltp is not None and leg.ltp > 0:
        return float(leg.ltp), "override"
    q = quote_resp.get(option_quote_key(sym)) or {}
    return _ltp_from_quote(q)


def _strategy_ltp_apply_fallbacks(
    sym: str,
    ltp_val: Optional[float],
    ltp_source: str,
    avg_cost: Optional[float],
    S_leg: float,
    parsed: dict,
    T_yrs: float,
) -> tuple[float, str]:
    """avg_cost → BS estimate fallbacks for the option LTP chain.
    Raises HTTPException(400) when all fallbacks are exhausted."""
    if ltp_val is None and avg_cost is not None and avg_cost > 0:
        ltp_val, ltp_source = float(avg_cost), "avg_cost"
    if ltp_val is None or ltp_val <= 0:
        est = black_scholes(S_leg, parsed["strike"], T_yrs,
                            DEFAULT_RISK_FREE, DEFAULT_IV,
                            parsed["opt_type"])
        if est > 0:
            ltp_val, ltp_source = est, "estimated"
    if ltp_val is None or ltp_val <= 0:
        raise HTTPException(
            status_code=400,
            detail=(f"Leg '{sym}' has no usable price. Pass `ltp` "
                    f"or `avg_cost` in the leg body (sim positions "
                    f"and illiquid contracts often need this).")
        )
    return ltp_val, ltp_source


def _strategy_resolve_option_ltp(
    leg,
    sym: str,
    parsed: dict,
    quote_resp: dict,
    S_leg: float,
    T_yrs: float,
) -> tuple[float, str]:
    """Option-leg LTP chain: override → broker → avg_cost → BS estimate → fail.
    Raises HTTPException(400) if all fallbacks are exhausted.
    """
    ltp_val, ltp_source = _strategy_ltp_from_leg_or_quote(leg, sym, quote_resp)
    return _strategy_ltp_apply_fallbacks(
        sym, ltp_val, ltp_source, leg.avg_cost, S_leg, parsed, T_yrs
    )


def _strategy_calibrate_iv(
    leg,
    parsed: dict,
    S_leg: float,
    T_yrs: float,
    ltp_val: float,
    ltp_source: str,
) -> tuple[float, str]:
    """Return (sigma, iv_source). Operator override → calibrated → DEFAULT_IV.
    """
    if leg.iv is not None and leg.iv > 0:
        return float(leg.iv), "override"
    sig = implied_vol(ltp_val, S_leg, parsed["strike"], T_yrs,
                      DEFAULT_RISK_FREE, parsed["opt_type"])
    if sig == DEFAULT_IV or ltp_source not in ("override", "live", "sim"):
        iv_source = "default" if sig == DEFAULT_IV else "calibrated"
    else:
        iv_source = "calibrated"
    return sig, iv_source


def _strategy_build_option_leg(
    leg,
    sym: str,
    parsed: dict,
    quote_resp: dict,
    S_leg: float,
    T_yrs: float,
    scale_ratio: float,
    qty: int,
) -> tuple[dict, dict, float]:
    """Resolve an option leg → (resolved_leg dict, leg_detail dict, sigma).
    Sigma is returned so the caller can accumulate the qty-weighted mean
    across all option legs.
    """
    ltp_val, ltp_source = _strategy_resolve_option_ltp(
        leg, sym, parsed, quote_resp, S_leg, T_yrs,
    )
    sig, iv_source = _strategy_calibrate_iv(
        leg, parsed, S_leg, T_yrs, ltp_val, ltp_source,
    )
    entry = float(leg.avg_cost) if leg.avg_cost is not None else ltp_val
    theo = black_scholes(S_leg, parsed["strike"], T_yrs,
                         DEFAULT_RISK_FREE, sig, parsed["opt_type"])
    g_per = greeks(S_leg, parsed["strike"], T_yrs,
                   DEFAULT_RISK_FREE, sig, parsed["opt_type"])
    resolved = {
        "strike":      parsed["strike"],
        "opt_type":    parsed["opt_type"],
        "qty":         qty,
        "entry_price": entry,
        "T_years":     T_yrs,
        "sigma":       sig,
        "scale_ratio": scale_ratio,
    }
    detail = {
        "symbol":      sym,
        "opt_type":    parsed["opt_type"],
        "strike":      parsed["strike"],
        "qty":         qty,
        "avg_cost":    entry,
        "ltp":         ltp_val,
        "iv":          sig,
        "theoretical": theo,
        "discrepancy": ltp_val - theo,
        "greeks":      g_per,
        "ltp_source":  ltp_source,
        "iv_source":   iv_source,
    }
    return resolved, detail, sig


def _strategy_compute_curves(
    resolved_legs: list[dict],
    S: float,
    span_pct_resolved: float,
    pts: int,
    n_slices: int,
    eval_T: float,
    data_span_sigmas: float,
) -> tuple[list, list, float, float, Optional[float]]:
    """Compute payoff curve + slices + extremes + rr_ratio with cache short-circuit.

    Returns (curve, slices, max_profit, max_loss, rr_ratio). The `curve` is
    the fresh today_value curve; expiry_value is either freshly computed or
    rewritten from cache in place (see leg-curve-cache Phase-4 comment block).
    """
    _lc_key = _leg_curve_cache_key(
        resolved_legs, span_pct_resolved,
        data_span_sigmas, pts, n_slices,
    )
    _lc_hit = _leg_curve_cache_get(_lc_key)

    curve = multileg_payoff_curve(
        resolved_legs, S=S,
        span_pct=span_pct_resolved,
        points=pts,
        eval_T=eval_T,
    )

    if _lc_hit is not None:
        _ev_norm: list[float] = _lc_hit["expiry_values_norm"]
        _xr:      list[float] = _lc_hit["x_ratios"]
        for _i, _pt in enumerate(curve):
            if _i < len(_ev_norm):
                _pt["expiry_value"] = _ev_norm[_i]
                _pt["spot"]         = round(_xr[_i] * S, 4)
        slices = _lc_hit["slices_norm"]
        max_p = _lc_hit["max_profit"]
        max_l = _lc_hit["max_loss"]
        agg_rr = _lc_hit["rr_ratio"]
        logger.debug(
            "[leg-curve-cache] hit key=%s legs=%d pts=%d slices=%d",
            _lc_key[:8], len(resolved_legs), pts, n_slices,
        )
    else:
        slices = multileg_intermediate_curves(
            resolved_legs, S=S,
            span_pct=span_pct_resolved,
            points=pts,
            time_slices=n_slices,
        )
        max_p, max_l = multileg_extremes(curve)
        agg_rr = risk_reward_ratio(max_p, max_l)
        _x_ratios_store  = [round(pt["spot"] / S, 10) for pt in curve] if S > 0 else []
        _ev_norm_store   = [pt["expiry_value"] for pt in curve]
        _leg_curve_cache_put(_lc_key, {
            "expiry_values_norm": _ev_norm_store,
            "x_ratios":          _x_ratios_store,
            "slices_norm":       slices,
            "max_profit":        max_p,
            "max_loss":          max_l,
            "rr_ratio":          agg_rr,
        })
        logger.debug(
            "[leg-curve-cache] miss key=%s legs=%d pts=%d slices=%d",
            _lc_key[:8], len(resolved_legs), pts, n_slices,
        )
    return curve, slices, max_p, max_l, agg_rr


def _strategy_build_legs(
    data: "StrategyRequest",
    parsed_by_sym: dict,
    quote_resp: dict,
    S: float,
    _is_commodity: bool,
    _close_time: tuple,
    _leg_scale_ratios: dict,
) -> tuple[list[dict], list[dict], float, float]:
    """Iterate ``data.legs`` and dispatch each to the futures or options
    builder.  Returns ``(resolved_legs, leg_details, sigma_weight_num,
    sigma_weight_den)`` so the caller can derive the qty-weighted IV proxy.
    """
    resolved_legs: list[dict] = []
    leg_details: list[dict] = []
    sigma_weight_num = 0.0
    sigma_weight_den = 0.0
    for leg in data.legs:
        sym = leg.symbol.upper().strip()
        parsed = parsed_by_sym.get(sym)
        leg_expiry = date.fromisoformat(_leg_expiry_iso(leg, parsed))
        T_yrs = days_to_expiry(leg_expiry, close_time=_close_time) / 365.0
        qty = int(leg.qty or 0)
        if qty == 0:
            raise HTTPException(status_code=400, detail=f"leg '{sym}' has qty=0")
        scale_ratio = _leg_scale_ratios.get(sym, 1.0) if _is_commodity else 1.0
        S_leg = S * scale_ratio

        if parsed.get("kind") == "fut":
            fut_resolved, fut_detail = _strategy_build_futures_leg(
                leg, sym, quote_resp, S_leg, scale_ratio, qty,
            )
            resolved_legs.append(fut_resolved)
            leg_details.append(fut_detail)
            continue

        opt_resolved, opt_detail, sig = _strategy_build_option_leg(
            leg, sym, parsed, quote_resp, S_leg, T_yrs, scale_ratio, qty,
        )
        resolved_legs.append(opt_resolved)
        leg_details.append(opt_detail)
        sigma_weight_num += sig * abs(qty)
        sigma_weight_den += abs(qty)
    return resolved_legs, leg_details, sigma_weight_num, sigma_weight_den


def _strategy_option_expiry_set(
    data: "StrategyRequest",
    parsed_by_sym: dict,
) -> tuple[set, str, date]:
    """Return (option_expiries, shared_expiry_iso, shared_expiry).
    Prefers min(option leg expiries); falls back to min of all expiries
    when there are no option legs (futures-only basket)."""
    all_expiries = {
        _leg_expiry_iso(leg, parsed_by_sym.get(leg.symbol.upper().strip()))
        for leg in data.legs
    }
    option_expiries = {
        _leg_expiry_iso(leg, parsed_by_sym.get(leg.symbol.upper().strip()))
        for leg in data.legs
        if (p := parsed_by_sym.get(leg.symbol.upper().strip())) and p.get("kind") == "opt"
    }
    shared_expiry_iso = min(option_expiries) if option_expiries else min(all_expiries)
    shared_expiry = date.fromisoformat(shared_expiry_iso)
    return option_expiries, shared_expiry_iso, shared_expiry


def _build_leg_details_list(leg_details: list) -> list:
    """Convert leg_details dicts to LegDetail instances."""
    return [
        LegDetail(
            symbol=l["symbol"], opt_type=l["opt_type"], strike=l["strike"],
            qty=l["qty"], avg_cost=l["avg_cost"], ltp=l["ltp"], iv=l["iv"],
            theoretical=l["theoretical"], discrepancy=l["discrepancy"],
            greeks=OptionGreeks(**l["greeks"]),
            ltp_source=l["ltp_source"], iv_source=l["iv_source"],
        )
        for l in leg_details
    ]


def _strategy_aggregate(
    data: "StrategyRequest",
    resolved_legs: list[dict],
    leg_details: list[dict],
    parsed_by_sym: dict,
    expiries: set,
    underlying: str,
    S: float,
    sigma_weight_num: float,
    sigma_weight_den: float,
    T_yrs_shared: float,
    eval_T: float,
    _close_time: tuple,
    spot_prev_close: "float | None",
    span_pct_resolved: float,
    _spot_anchor: "str | None",
    _spot_src: str,
) -> "StrategyResponse":
    """Phase-4 aggregate analytics: Greeks, curves, EV, R:R, breakevens.

    Builds and returns the final ``StrategyResponse`` from the already-
    resolved leg list.  All inputs are pure values — no broker I/O.
    """
    sigma_proxy = sigma_weight_num / sigma_weight_den if sigma_weight_den else DEFAULT_IV
    pts = max(11, min(int(data.points or 51), 121))
    _n_slices = max(0, min(int(data.time_slices or 0), 5))

    curve, slices, max_p, max_l, agg_rr = _strategy_compute_curves(
        resolved_legs, S, span_pct_resolved, pts, _n_slices,
        eval_T, float(data.span_sigmas),
    )
    agg_greeks = multileg_greeks(resolved_legs, S=S)
    bes        = find_breakevens(curve)
    pop        = multileg_pop(curve, S=S, T_years=T_yrs_shared, sigma=sigma_proxy)
    net_cost   = sum(l["entry_price"] * l["qty"] for l in resolved_legs)
    agg_ev     = expected_value(curve, S=S, T_years=T_yrs_shared, sigma=sigma_proxy)
    agg_ev_pct = (round(agg_ev / abs(net_cost) * 100.0, 2)
                  if abs(net_cost) > 0 else None)

    # Prefer min(option expiries) — options drive "time to expiry".
    option_expiries, shared_expiry_iso, shared_expiry = _strategy_option_expiry_set(
        data, parsed_by_sym
    )
    return StrategyResponse(
        underlying=underlying,
        expiry=shared_expiry_iso,
        days_to_expiry=days_to_expiry(shared_expiry, close_time=_close_time),
        spot=S,
        net_cost=net_cost,
        net_qty=sum(int(l["qty"]) for l in resolved_legs),
        iv_proxy=sigma_proxy,
        aggregate_greeks=OptionGreeks(**agg_greeks),
        risk=StrategyRisk(
            max_profit=max_p, max_loss=max_l,
            breakevens=bes, pop=pop,
            ev=agg_ev, ev_pct=agg_ev_pct, rr_ratio=agg_rr,
        ),
        payoff=[PayoffPoint(**p) for p in curve],
        intermediate_curves=[IntermediateCurve(**s) for s in slices],
        legs=_build_leg_details_list(leg_details),
        spot_prev_close=spot_prev_close,
        span_pct=span_pct_resolved,
        span_sigmas=float(data.span_sigmas) if data.span_pct is None else 0.0,
        multi_expiry=len(option_expiries) > 1,
        spot_anchor_contract=_spot_anchor,
        spot_source=_spot_src,
    )


# ── chain_quotes helpers ──────────────────────────────────────────────


def _best_depth_price(book: list) -> "float | None":
    """Top-of-book price from a depth.buy / depth.sell list.
    Returns None when every level is empty/zero."""
    for level in (book or []):
        p = level.get("price")
        if p not in (None, 0, 0.0):
            return float(p)
    return None


def _chain_quotes_build_sym_map(
    inst_resp, und: str, exp: str
) -> dict[float, dict[str, str]]:
    """Build strike → {CE: sym, PE: sym} from the instruments response."""
    sym_by_strike: dict[float, dict[str, str]] = {}
    for inst in inst_resp.items:
        if (inst.u or "").upper() != und:
            continue
        if inst.x != exp:
            continue
        if inst.t not in ("CE", "PE"):
            continue
        if inst.k is None:
            continue
        sym_by_strike.setdefault(float(inst.k), {"CE": "", "PE": ""})[inst.t] = inst.s
    return sym_by_strike


async def _chain_quotes_batch_quote(
    sym_by_strike: dict[float, dict[str, str]],
    und: str,
    exp: str,
) -> tuple[dict, dict[str, tuple[float, str]]]:
    """Build quote keys, fire one broker.quote() call, return (quote_resp, key_meta)."""
    keys: list[str] = []
    key_meta: dict[str, tuple[float, str]] = {}
    for strike, sides in sym_by_strike.items():
        for side, sym in sides.items():
            if not sym:
                continue
            qk = option_quote_key(sym)
            keys.append(qk)
            key_meta[qk] = (strike, side)

    from backend.brokers.registry import get_market_data_broker
    quote_resp: dict = {}
    if keys:
        try:
            quote_resp = await asyncio.to_thread(get_market_data_broker().quote, keys) or {}
        except Exception as e:
            logger.warning(f"chain-quotes quote() failed for {und}/{exp}: {e}")
    return quote_resp, key_meta


def _chain_quotes_bid_ask_from_q(q: dict) -> tuple["float | None", "float | None"]:
    """Return (bid, ask) from a Kite quote dict.
    Falls back to last_price for both sides when the depth array is empty
    (illiquid PE strikes outside the front 5 strikes often have no depth).
    last_price > 0 is the gate — a true no-quote still returns (None, None)."""
    depth = q.get("depth") or {}
    bid = _best_depth_price(depth.get("buy"))
    ask = _best_depth_price(depth.get("sell"))
    if bid is None or ask is None:
        lp = float(q.get("last_price") or 0.0)
        if lp > 0:
            if bid is None:
                bid = lp
            if ask is None:
                ask = lp
    return bid, ask


def _chain_quotes_build_book(
    sym_by_strike: dict[float, dict[str, str]],
    quote_resp: dict,
    key_meta: dict[str, tuple[float, str]],
) -> dict[float, dict[str, dict[str, "float | None"]]]:
    """Populate bid/ask per strike+side from the broker quote response."""
    book_by_strike: dict[float, dict[str, dict[str, "float | None"]]] = {
        k: {"CE": {"bid": None, "ask": None},
            "PE": {"bid": None, "ask": None}}
        for k in sym_by_strike
    }
    for qk, (strike, side) in key_meta.items():
        q = quote_resp.get(qk) or {}
        bid, ask = _chain_quotes_bid_ask_from_q(q)
        book_by_strike[strike][side]["bid"] = bid
        book_by_strike[strike][side]["ask"] = ask
    return book_by_strike


async def _strategy_fetch_bulk_quote(
    need_quote: dict,
    _price_broker,
) -> dict:
    """Fire a bulk broker.quote() for all symbols in need_quote.
    Returns an empty dict when need_quote is empty or the call fails."""
    quote_resp: dict = {}
    if need_quote:
        try:
            quote_resp = await asyncio.to_thread(
                _price_broker.quote, list(need_quote.keys()),
            ) or {}
        except Exception as e:
            logger.warning(f"Strategy quote() failed: {e}")
    return quote_resp


async def _strategy_resolve_spot_impl(
    data: "StrategyRequest",
    parsed_by_sym: dict,
    underlying: str,
) -> tuple[float, str, "Optional[float]", "Optional[str]"]:
    """Compute sorted_strikes + median, pick anchor, then delegate to _resolve_spot.
    Logs a warning when the source is 'fallback' or 'cached'.
    Returns (S, spot_src, spot_prev_close, spot_anchor)."""
    sorted_strikes = sorted({
        p["strike"]
        for l in data.legs
        if (p := parsed_by_sym.get((l.symbol or "").upper().strip())) and "strike" in p
    })
    median_strike = sorted_strikes[len(sorted_strikes) // 2] if sorted_strikes else None
    anchor_symbol, expiry_hint = _strategy_pick_spot_anchor(data, parsed_by_sym)
    S, _spot_src, spot_prev_close, _spot_anchor = await _resolve_spot(
        underlying, data.spot,
        fallback=median_strike,
        expiry_hint=expiry_hint,
        option_symbol=anchor_symbol,
    )
    if _spot_src in ("fallback", "cached"):
        logger.warning(
            f"strategy spot for {underlying} fell through to "
            f"source={_spot_src!r} (spot={S}, anchor={_spot_anchor}). "
            f"Live + futures lookups failed; UI will suppress the spot marker."
        )
    return S, _spot_src, spot_prev_close, _spot_anchor


def _chain_snapshot_parse_expiry(exp: str) -> date:
    """Parse exp as an ISO date string. Raises HTTPException(400) on bad format."""
    try:
        return date.fromisoformat(exp)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400,
                            detail=f"expiry must be ISO format YYYY-MM-DD, got {exp!r}")


async def _chain_snapshot_resolve_spot(
    und: str, expiry_d: "Optional[date]"
) -> tuple[float, str, "Optional[float]"]:
    """Resolve spot for the chain-snapshot endpoint.
    Returns (spot, src, prev_close). Raises 502 when spot cannot be resolved
    or is non-positive."""
    try:
        spot, src, prev, _anc2 = await _resolve_spot(und, None, expiry_hint=expiry_d)
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"chain-snapshot spot resolution failed: {e}")
        raise HTTPException(status_code=502,
                            detail=f"could not resolve spot for {und}")
    if not spot or spot <= 0:
        raise HTTPException(status_code=502,
                            detail=f"resolved spot is non-positive: {spot}")
    return spot, src, prev


def _analytics_validate_request(mode: str, request: Any, symbol: str) -> str:
    """Validate mode, demo guard, and symbol for the /analytics endpoint.
    Returns the uppercased+stripped symbol on success; raises HTTPException
    for invalid mode (400), demo+live (403), missing symbol (400), or
    non-option symbol (400)."""
    if mode not in _VALID_MODES:
        raise HTTPException(status_code=400,
                            detail=f"mode must be one of {_VALID_MODES}")
    if getattr(request.state, "is_demo", False) and mode == "live":
        raise HTTPException(status_code=403, detail="Demo: read-only.")
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    sym = symbol.upper().strip()
    parsed = parse_tradingsymbol(sym)
    if not parsed or parsed.get("kind") != "opt":
        raise HTTPException(
            status_code=400,
            detail=f"'{sym}' isn't a recognised option contract. "
                   f"Futures and equities aren't supported by this endpoint."
        )
    return sym


def _analytics_compute_metrics(
    S: float,
    parsed: dict,
    T_yrs: float,
    sigma: float,
    ltp_val: float,
    qty_resolved: int,
    avg_resolved: float,
    span_pct: "Optional[float]",
    span_sigmas: float,
    points: int,
    time_slices: int,
) -> tuple:
    """Compute BS price, greeks, risk metrics, payoff curves, and EV for one option.

    Returns a 13-tuple:
      (theo, disc, disc_pct, g_per, g_pos, entry, risk,
       span_pct_resolved, curve, slices, ev, ev_pct, rr)
    """
    theo = black_scholes(S, parsed["strike"], T_yrs,
                         DEFAULT_RISK_FREE, sigma, parsed["opt_type"])
    disc = ltp_val - theo
    disc_pct = (disc / theo * 100.0) if theo else 0.0

    g_per = greeks(S, parsed["strike"], T_yrs,
                   DEFAULT_RISK_FREE, sigma, parsed["opt_type"])
    g_pos = {k: v * qty_resolved for k, v in g_per.items()}

    entry = avg_resolved if avg_resolved > 0 else ltp_val
    risk = risk_metrics(
        S=S, K=parsed["strike"], T_years=T_yrs,
        r=DEFAULT_RISK_FREE, sigma=sigma,
        opt_type=parsed["opt_type"], qty=qty_resolved,
        entry_price=entry,
    )
    span_pct_resolved = _resolve_span_pct(
        sigma=sigma, T_years=T_yrs,
        span_pct=span_pct, span_sigmas=span_sigmas,
    )
    pts = max(11, min(points, 101))
    curve = payoff_curve(
        S=S, K=parsed["strike"], T_years=T_yrs,
        r=DEFAULT_RISK_FREE, sigma=sigma,
        opt_type=parsed["opt_type"], qty=qty_resolved,
        entry_price=entry, span_pct=span_pct_resolved, points=pts,
    )
    slices = intermediate_curves(
        S=S, K=parsed["strike"], T_years=T_yrs,
        r=DEFAULT_RISK_FREE, sigma=sigma,
        opt_type=parsed["opt_type"], qty=qty_resolved,
        entry_price=entry, span_pct=span_pct_resolved, points=pts,
        time_slices=max(0, min(time_slices, 5)),
    )
    ev = expected_value(curve, S=S, T_years=T_yrs, sigma=sigma)
    cost_basis = abs(entry * qty_resolved)
    ev_pct = round(ev / cost_basis * 100.0, 2) if cost_basis > 0 else None
    rr = risk_reward_ratio(_finite_or_null(risk["max_profit"]),
                           _finite_or_null(risk["max_loss"]))
    return (theo, disc, disc_pct, g_per, g_pos, entry, risk,
            span_pct_resolved, curve, slices, ev, ev_pct, rr)


# ── Controller ────────────────────────────────────────────────────────

class OptionsController(Controller):
    path   = "/api/options"
    # auth_or_demo: demo visitors on prod browse the analytics
    # surface read-only. The endpoints don't touch the broker
    # write path, so leakage risk is per-row data only — and
    # everything in a strategy response is computed (Greeks /
    # payoff curves), not account-tagged.
    guards = [auth_or_demo_guard]

    @get("/analytics")
    async def analytics(self, request: Request,
                        mode: str = "live", symbol: str = "",
                        account: Optional[str] = None,
                        qty: Optional[int] = None,
                        avg_cost: Optional[float] = None,
                        spot: Optional[float] = None,
                        ltp: Optional[float] = None,
                        iv: Optional[float] = None,
                        span_pct: Optional[float] = None,
                        span_sigmas: float = 3.0,
                        points: int = 51,
                        time_slices: int = 0) -> OptionAnalyticsResponse:
        """
        Full analytics bundle for one option position. Single round-trip
        — Greeks, theoretical price, discrepancy, risk metrics, payoff
        curve all computed in-process. The frontend renders this as the
        side panel + payoff chart on /admin/options.
        """
        sym    = _analytics_validate_request(mode, request, symbol)
        parsed = parse_tradingsymbol(sym)

        qty_resolved, acct_resolved, avg_resolved = _resolve_position(
            mode, sym, qty, account, avg_cost)
        # Spot first, with the strike as a synthetic-spot fallback so
        # the page never 502s when broker market-data is down. The
        # response carries spot_source='fallback' in that case.
        # Pass `expiry_hint` so the resolver can fall through to the
        # matching monthly futures contract for commodities (MCX
        # underlyings have no NSE spot ticker — index lookup misses
        # silently and we'd otherwise anchor on the strike).
        S, spot_src, spot_prev_close, _spot_anchor_single = await _resolve_spot(
            parsed["root"], spot,
            fallback=parsed["strike"],
            expiry_hint=parsed["expiry"],
            option_symbol=sym)
        _close_time = (23, 30) if is_mcx_underlying(parsed["root"]) else (15, 30)
        T_yrs = days_to_expiry(parsed["expiry"], close_time=_close_time) / 365.0
        # Pass avg_cost AND estimated-BS inputs as last-resort fallbacks
        # so a stale broker quote on an illiquid contract still produces
        # a usable payoff curve. ltp_source='estimated' tells the UI it
        # came from BS at default IV against the resolved spot.
        ltp_val, ltp_src = await _resolve_ltp(
            sym, mode, acct_resolved or account, ltp,
            avg_cost_hint=avg_resolved if avg_resolved > 0 else avg_cost,
            estimate_inputs={
                "spot":      S,
                "strike":    parsed["strike"],
                "T_years":   T_yrs,
                "opt_type":  parsed["opt_type"],
            },
        )
        # IV: explicit override > calibrate from current LTP > default
        # implied_vol returns DEFAULT_IV (0.15) on bracket failure or
        # near-intrinsic / degenerate inputs; treat that as the
        # "default" source so the UI can flag it. Estimated-LTP
        # fallback also forces 'default' since the calibration
        # would just be a self-referential round-trip.
        sigma, iv_src = _resolve_iv_for_analytics(
            iv, ltp_val, ltp_src,
            S, parsed["strike"], T_yrs, parsed["opt_type"],
        )

        (theo, disc, disc_pct, g_per, g_pos, entry, risk,
         span_pct_resolved, curve, slices, ev, ev_pct, rr) = _analytics_compute_metrics(
            S, parsed, T_yrs, sigma, ltp_val, qty_resolved, avg_resolved,
            span_pct, span_sigmas, int(points), int(time_slices),
        )

        return OptionAnalyticsResponse(
            mode=mode,
            symbol=sym,
            underlying=parsed["root"],
            opt_type=parsed["opt_type"],
            strike=parsed["strike"],
            expiry=parsed["expiry"].isoformat(),
            days_to_expiry=days_to_expiry(parsed["expiry"], close_time=_close_time),
            account=acct_resolved,
            qty=qty_resolved,
            avg_cost=entry,
            spot=S, ltp=ltp_val, iv=sigma,
            theoretical=theo,
            discrepancy=disc,
            discrepancy_pct=disc_pct,
            greeks_per_share=OptionGreeks(**g_per),
            greeks_position=OptionGreeks(**g_pos),
            risk=OptionRisk(
                max_profit=_finite_or_null(risk["max_profit"]),
                max_loss=_finite_or_null(risk["max_loss"]),
                breakeven=risk["breakeven"],
                pop=risk["pop"],
                long_short=risk["long_short"],
                ev=ev,
                ev_pct=ev_pct,
                rr_ratio=rr,
            ),
            payoff=[PayoffPoint(**p) for p in curve],
            intermediate_curves=[IntermediateCurve(**s) for s in slices],
            ltp_source=ltp_src,
            spot_source=spot_src,
            iv_source=iv_src,
            span_pct=span_pct_resolved,
            span_sigmas=float(span_sigmas) if span_pct is None else 0.0,
            spot_prev_close=spot_prev_close,
            spot_anchor_contract=_spot_anchor_single,
        )

    @get("/spot")
    async def spot(self, underlying: str = "",
                   expiry: Optional[str] = None) -> SpotResponse:
        """Lightweight spot for `underlying`, optionally hinted by an
        expiry date (used to pick the matching monthly futures
        contract for MCX commodities). Returns the value + provenance
        + yesterday's close so the UI can anchor the chain picker's
        ATM highlight on any underlying the operator switches to,
        not just the page's primary one.

        Reuses `_resolve_spot()` so the resolution order matches
        every other surface (sim → spot ticker → futures fallback).
        """
        und = (underlying or "").upper().strip()
        if not und:
            raise HTTPException(status_code=400,
                                detail="underlying is required")
        expiry_d: Optional[date] = None
        if expiry:
            try:
                expiry_d = date.fromisoformat(expiry)
            except (TypeError, ValueError):
                pass
        # No fallback — let the resolver 502 if there's no real data.
        # The chain picker handles the failure by leaving chainSpot
        # null (suppresses the ATM highlight + spot pill); we don't
        # want to anchor the UI on a synthetic median-strike value
        # here.
        px, src, prev, _anc = await _resolve_spot(und, None, expiry_hint=expiry_d)
        return SpotResponse(
            underlying=und,
            spot=px,
            spot_source=src,
            spot_prev_close=prev,
            spot_anchor_contract=_anc,
        )

    @get("/chain-quotes")
    async def chain_quotes(self, underlying: str = "",
                           expiry: str = "") -> ChainQuotesResponse:
        """Per-strike CE + PE LTP for a given (underlying, expiry).
        Resolves all CE/PE contracts via the cached instruments dump
        (already warmed by the daily refresh task), then makes ONE
        broker `quote()` call covering both sides of every strike. The
        chain picker on /admin/options renders the LTPs inline next to
        each Buy / Sell / (i) button so the operator can size legs
        without leaving the chain view."""
        und = (underlying or "").upper().strip()
        exp = (expiry or "").strip()
        if not und:
            raise HTTPException(status_code=400,
                                detail="underlying is required")
        if not exp:
            raise HTTPException(status_code=400,
                                detail="expiry is required")

        from backend.api.cache import get_or_fetch
        from backend.api.routes.instruments import _fetch_instruments
        try:
            inst_resp = await get_or_fetch(
                "instruments", _fetch_instruments, ttl_seconds=86400)
        except Exception as e:
            logger.warning(f"chain-quotes instruments fetch failed: {e}")
            return ChainQuotesResponse(underlying=und, expiry=exp, rows=[])

        # Map (strike, side) → tradingsymbol for the matching contracts.
        sym_by_strike = _chain_quotes_build_sym_map(inst_resp, und, exp)
        if not sym_by_strike:
            return ChainQuotesResponse(underlying=und, expiry=exp, rows=[])

        # Build quote keys, fire one broker.quote() call.
        quote_resp, key_meta = await _chain_quotes_batch_quote(sym_by_strike, und, exp)

        # Populate bid/ask per strike+side.
        book_by_strike = _chain_quotes_build_book(sym_by_strike, quote_resp, key_meta)

        rows = [
            ChainQuoteRow(
                k=strike,
                ce_bid=sides["CE"]["bid"],
                ce_ask=sides["CE"]["ask"],
                pe_bid=sides["PE"]["bid"],
                pe_ask=sides["PE"]["ask"],
            )
            for strike, sides in sorted(book_by_strike.items())
        ]
        return ChainQuotesResponse(underlying=und, expiry=exp, rows=rows)

    @get("/chain-snapshot")
    async def chain_snapshot(
        self,
        underlying: str = "",
        expiry: str = "",
        atm_window: int = 10,
    ) -> ChainSnapshotResponse:
        """Full option-chain snapshot — LTP + IV + per-share Greeks for
        every strike within ±`atm_window` of the at-the-money strike.
        One broker.quote() round-trip; Greeks computed in-process.

        Designed for the MCP `get_options_chain_snapshot` tool so the
        LLM can plan a multi-leg structure (iron condor, butterfly,
        diagonal) in one tool call instead of `atm_window * 2 + 1`
        per-leg `get_option_analytics` round-trips. A 5-strike call
        butterfly previously needed 5 calls + 5 round-trips; now it's
        one.

        IV calibration: `implied_vol(market_price, S, K, T, r, opt_type)`
        bisection-solves between 0.0001 and 5.0. Falls back to
        DEFAULT_IV (15%) when the LTP can't bracket a valid σ — that
        path still returns Greeks but they reflect the fallback IV
        rather than market-implied. LLMs should weight Greeks lighter
        when `iv` field is null (LTP unavailable).
        """
        und = (underlying or "").upper().strip()
        exp = (expiry or "").strip()
        atm_window = max(1, min(int(atm_window or 10), 30))
        if not und:
            raise HTTPException(status_code=400, detail="underlying is required")
        if not exp:
            raise HTTPException(status_code=400, detail="expiry is required")

        # 1. Resolve spot (no fallback — we need it for Greeks).
        expiry_d = _chain_snapshot_parse_expiry(exp)
        spot, src, prev = await _chain_snapshot_resolve_spot(und, expiry_d)

        from backend.api.routes.options_helpers import (
            _chain_snapshot_instruments,
            _chain_snapshot_batch_quote,
            _chain_snapshot_compute_rows,
        )

        # 2+3. Instruments fetch + ATM window.
        sym_by_strike, atm_strike, window_strikes = await _chain_snapshot_instruments(
            und, exp, spot, atm_window,
        )
        if not sym_by_strike:
            return ChainSnapshotResponse(
                underlying=und, expiry=exp, spot=spot, spot_source=src,
                spot_prev_close=prev,
                days_to_expiry=days_to_expiry(expiry_d) if expiry_d else 0.0,
                risk_free_rate=DEFAULT_RISK_FREE,
                atm_strike=None, rows=[],
            )

        # 4. Batch broker quote.
        quote_resp, _key_meta = await _chain_snapshot_batch_quote(
            und, exp, sym_by_strike, window_strikes,
        )

        # 5. Time-to-expiry.
        T_yrs = days_to_expiry(expiry_d) / 365.0 if expiry_d else 0.0

        # 6. Per-strike IV + greeks.
        rows = _chain_snapshot_compute_rows(
            sym_by_strike, window_strikes, quote_resp, spot, T_yrs,
            ChainSnapshotLeg, ChainSnapshotRow,
        )

        return ChainSnapshotResponse(
            underlying=und, expiry=exp, spot=spot, spot_source=src,
            spot_prev_close=prev,
            days_to_expiry=days_to_expiry(expiry_d) if expiry_d else 0.0,
            risk_free_rate=DEFAULT_RISK_FREE,
            atm_strike=atm_strike, rows=rows,
        )

    @get("/historical")
    async def historical(self, symbol: str = "", days: int = 30,
                         interval: str = "day",
                         exchange: str = "") -> HistoricalResponse:
        # Default exchange "" (not "NFO") so when the caller doesn't
        # pass an explicit hint the loop walks every supported arm
        # (NFO → BFO → NSE → BSE → MCX → CDS). MCX commodities (GOLD,
        # SILVER, CRUDEOIL) + CDS currencies (USDINR) need this — they
        # never resolve on the NFO-only fast path and the operator saw
        # the chart hang on "Loading..." until the 25s frontend timeout.
        """
        Daily / hourly / minute candles from Kite. `interval` ∈ {day,
        60minute, 30minute, 15minute, 5minute, minute}. Underlyings get
        their NSE spot history; options + futures use NFO.

        The instrument-token lookup hits the broker's instruments dump
        for the relevant exchange — that response is large but already
        cached by the InstrumentsController (TTL 24h via `get_or_fetch`),
        so a warm cache makes this endpoint cheap.
        """
        if not symbol:
            raise HTTPException(status_code=400, detail="symbol is required")
        sym  = symbol.upper().strip()
        # Cap raised from 90 to 365 — frontend ChartWorkspace exposes
        # 6M (180d) and 1Y (365d) range buttons that were SILENTLY
        # truncated to 90 days, leaving the operator's "1Y" view
        # showing only the last quarter. The three-tier ohlcv_store
        # (memory → DB → broker) handles longer ranges cheaply:
        # cold cycles hit the broker once, warm reads serve from
        # /opt/ramboq DB. Kite historical_data accepts 2000-day
        # ranges for daily bars so 365 is well within the SDK limit.
        days = max(1, min(int(days), 365))

        valid_intervals = ("day", "60minute", "30minute", "15minute", "5minute", "minute")
        if interval not in valid_intervals:
            raise HTTPException(status_code=400,
                                detail=f"interval must be one of {valid_intervals}")

        # ── Cache lookup ───────────────────────────────────────────────
        cache_key = (sym, (exchange or "NFO").upper(), days, interval)
        _cached = _hist_cache_get(cache_key)
        if _cached is not None:
            return _cached

        from backend.api.routes.options_helpers import (
            _historical_ohlcv_store,
            _historical_intraday_store,
            _historical_closed_guard,
            _historical_broker_loop,
        )

        # ── Tier-1/2/3 store for daily bars (interval="day") ─────────
        # Immutable once the day closes — serve from DB/memory cache
        # instead of re-hitting the broker on every cold open or deploy.
        # Use yesterday as the upper-bound (today's bar not yet final).
        if interval == "day":
            result = await _historical_ohlcv_store(
                sym, exchange, days,
                _hist_cache_put, _ohlcv_trace_enabled,
                _self_heal_log_once, _SELF_HEAL_COVERAGE_THRESHOLD,
                _HIST_CACHE_TTL_OK, _HIST_CACHE_TTL_EMPTY,
                HistoricalBar, HistoricalResponse,
            )
            if result is not None:
                return result

        # ── Tier-1/2/3 store for intraday bars (5/15/30/60-minute) ───
        # Per-day store calls dedup via the store's fetch lock so
        # concurrent operators on the same chart share one broker
        # round-trip per date.
        if interval in ("5minute", "15minute", "30minute", "60minute"):
            result = await _historical_intraday_store(
                sym, exchange, days, interval,
                _hist_cache_put, _self_heal_log_once,
                _HIST_CACHE_TTL_OK,
                HistoricalBar, HistoricalResponse,
            )
            if result is not None:
                return result

        # ── Closed-hours guard — prevent live broker historical_data calls ──
        # For intraday intervals when the store returned nothing and markets
        # are closed, return an empty response rather than wasting quota.
        guard_result = _historical_closed_guard(
            sym, interval, _hist_cache_put, cache_key,
            _HIST_CACHE_TTL_EMPTY, HistoricalResponse,
        )
        if guard_result is not None:
            return guard_result

        # ── Account-fallback broker loop ──────────────────────────────
        # get_historical_brokers() returns the prioritised list of eligible
        # accounts (historical_data_enabled=True, not in rate-limit cool-off).
        return await _historical_broker_loop(
            sym, exchange, days, interval,
            _hist_cache_put, _ohlcv_trace_enabled, _record_first_cold_empty,
            cache_key, _HIST_CACHE_TTL_OK, _HIST_CACHE_TTL_EMPTY,
            HistoricalBar, HistoricalResponse,
            _instruments_cache_get, _instruments_cache_put,
        )

    # ── Multi-leg strategy analytics (POST) ────────────────────────────

    @post("/strategy-analytics")
    async def strategy_analytics(self, data: "StrategyRequest",
                                  bypass_cache: bool = False) -> "StrategyResponse":
        """
        Aggregate analytics for a multi-leg single-underlying strategy
        (vertical spread, iron condor, butterfly, strangle, etc.).
        Accepts a list of legs; v1 requires every leg to share the same
        underlying and same expiry.

        Per-leg `ltp` and `avg_cost` are optional — if provided (e.g.
        legs sourced from the simulator), they're used directly; if
        missing, the broker is hit for the current LTP and `avg_cost`
        falls back to the LTP (treats the leg as "what if I open this
        right now").

        Query param `bypass_cache=true` forces a full recompute even if a
        fresh cached response exists (e.g. after operator changes an override
        field that isn't structurally different from the previous request).
        """
        # ── Cache short-circuit ───────────────────────────────────────
        # Identical re-polls (frontend polls every 5s) skip the broker quote
        # + BS IV calibration path and return the cached response in <20ms.
        # bypass_cache=true signals the operator changed something and wants
        # a fresh compute even if the input hash is identical.
        cache_key = _strategy_cache_key(data)
        if not bypass_cache:
            cached = _strategy_cache_get(cache_key)
            if cached is not None:
                return cached

        try:
            result = await self._strategy_analytics_impl(data)
        except HTTPException:
            raise
        except Exception:
            # Log the full traceback so 500s in this endpoint are
            # debuggable — Litestar's default 500 handler swallows the
            # exception text. Re-raise as a 500 with a generic message
            # so the operator at least sees something actionable.
            logger.exception("Strategy analytics failed (legs=%s)", data.legs)
            raise HTTPException(status_code=500,
                detail="Strategy analytics failed; see server logs.")
        _strategy_cache_put(cache_key, result)
        return result

    async def _strategy_analytics_impl(self, data: "StrategyRequest") -> "StrategyResponse":
        """Orchestrator — thin composition of module-level `_strategy_*`
        helpers. Original 620-LOC monolith decomposed 2026-07-03 (cyclomatic
        hotspot cc=120 → target < 30). All helpers are pure (no self),
        exception semantics preserved (HTTPException 400 for validation).
        """
        # ── 1. Parse + validate leg metadata ──────────────────────────
        parsed_by_sym = _strategy_validate_and_parse(data)
        roots, expiries, need_quote = _strategy_collect_leg_metadata(data, parsed_by_sym)
        underlying = next(iter(roots))

        from backend.brokers.registry import get_market_data_broker
        _price_broker = get_market_data_broker()

        # Bulk quote — richer than ltp(); includes ohlc.close for off-hours.
        quote_resp = await _strategy_fetch_bulk_quote(need_quote, _price_broker)

        # ── 2. Resolve spot ───────────────────────────────────────────
        S, _spot_src, spot_prev_close, _spot_anchor = await _strategy_resolve_spot_impl(
            data, parsed_by_sym, underlying,
        )

        # ── 3. Build resolved-leg list with σ calibrated per leg ──────
        _is_commodity = is_mcx_underlying(underlying)
        _close_time = (23, 30) if _is_commodity else (15, 30)
        eval_T, T_yrs_shared = _strategy_option_T_range(data, parsed_by_sym, _close_time)

        _leg_scale_ratios: dict[str, float] = {}
        if _is_commodity:
            _leg_scale_ratios = await _strategy_mcx_scale_ratios(
                data, parsed_by_sym, underlying, S, _price_broker,
            )

        resolved_legs, leg_details, sigma_weight_num, sigma_weight_den = (
            _strategy_build_legs(
                data, parsed_by_sym, quote_resp, S,
                _is_commodity, _close_time, _leg_scale_ratios,
            )
        )

        # ── 4. Aggregate analytics ────────────────────────────────────
        sigma_proxy = sigma_weight_num / sigma_weight_den if sigma_weight_den else DEFAULT_IV
        span_pct_resolved = _resolve_span_pct(
            sigma=sigma_proxy, T_years=T_yrs_shared,
            span_pct=data.span_pct, span_sigmas=data.span_sigmas,
        )
        return _strategy_aggregate(
            data, resolved_legs, leg_details, parsed_by_sym, expiries,
            underlying, S, sigma_weight_num, sigma_weight_den, T_yrs_shared, eval_T,
            _close_time, spot_prev_close, span_pct_resolved, _spot_anchor, _spot_src,
        )
