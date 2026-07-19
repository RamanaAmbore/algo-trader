"""
Derivatives helpers — Indian F&O symbol parser + Black-Scholes pricer +
implied-volatility calibrator. Used by the simulator to drive coherent
option/future re-pricing off a single underlying spot move (so a "−3%
NIFTY" tick re-prices every NIFTY call/put/future at once instead of
moving each contract in isolation).

Conventions:
  - Risk-free rate `r` defaults to 7 % (Indian 91-day T-bill, close enough
    for a sim that runs in minutes).
  - Day count: 365.
  - Vega/theta deliberately ignored — sim runs are minutes, not days, so
    the time-value bleed is a rounding error against the spot delta.
  - IV is locked at sim start by inverting BS against each option's current
    LTP. Subsequent ticks re-price with that cached σ. A scripted scenario
    can override per-position via `iv: 0.18` on the position dict; otherwise
    the calibrator runs on whatever LTP comes from the seed.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np

# F&O contracts are Indian instruments — Kite expiry dates are calendar
# days in IST. Computing DTE against the server's local clock (which is
# UTC on most VPS hosts) drops a day across the IST→UTC offset and gives
# the operator a stale "DTE" the moment the IST day flips. Anchor every
# DTE comparison to Asia/Kolkata so the rollover matches the operator's
# trading day, not the server's.
_IST = ZoneInfo("Asia/Kolkata")


DEFAULT_RISK_FREE   = 0.07     # 7% annualized
DEFAULT_DTE_DAYS    = 7        # fallback when neither row.expiry nor symbol parse yields a date
DEFAULT_IV          = 0.15     # 15% annualized — fallback when calibration fails
SECONDS_PER_YEAR    = 365 * 24 * 60 * 60


# ── Symbol parser ─────────────────────────────────────────────────────

# Monthly options:  NIFTY25APR22000CE, BANKNIFTY25APR48000PE, RELIANCE25APR2800CE
_OPT_MONTHLY = re.compile(r"^([A-Z]+?)(\d{2})([A-Z]{3})(\d+(?:\.\d+)?)(CE|PE)$")
# Weekly options (Kite uses single-digit month + 2-digit day): NIFTY25424CE = 24-Apr-25
# Format: NIFTY YY M DD STRIKE CE/PE  where M is 1-9 / O / N / D
_OPT_WEEKLY = re.compile(
    r"^([A-Z]+?)(\d{2})([1-9OND])(\d{2})(\d+(?:\.\d+)?)(CE|PE)$"
)
# Monthly futures: NIFTY25APRFUT
_FUT_MONTHLY = re.compile(r"^([A-Z]+?)(\d{2})([A-Z]{3})FUT$")

_MONTH_BY_CODE_LONG = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_MONTH_BY_CODE_SHORT = {
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
    "O": 10, "N": 11, "D": 12,
}


def parse_tradingsymbol(symbol: str) -> Optional[dict]:
    """
    Parse a Kite-style F&O tradingsymbol. Returns a dict with:
        kind:        'opt' | 'fut'
        root:        symbol family prefix / root (e.g. 'NIFTY', 'GOLDM').
                     NOT the price-source underlying — for that, use
                     underlying_ltp_key(root) or option_underlying_quote_key().
        opt_type:    'CE' | 'PE'   (options only)
        strike:      float          (options only)
        expiry:      datetime.date  (best-effort — last Thursday for
                     monthly, parsed exactly for weekly)
    Returns None if the symbol doesn't match a known F&O shape (e.g.
    cash-equity holdings).
    """
    if not symbol:
        return None
    sym = symbol.upper()

    # Try monthly options first (longer match) before falling through to
    # weekly, since NIFTY25APR22000CE would otherwise back-track past the
    # 'A'/'P' boundary in the weekly pattern.
    m = _OPT_MONTHLY.match(sym)
    if m:
        und, yy, mon, strike, opt = m.groups()
        try:
            month  = _MONTH_BY_CODE_LONG[mon]
            year   = 2000 + int(yy)
            expiry = _monthly_expiry(und, year, month)
            return {"kind": "opt", "root": und,
                    "opt_type": opt, "strike": float(strike),
                    "expiry": expiry}
        except Exception:
            pass

    m = _OPT_WEEKLY.match(sym)
    if m:
        und, yy, mon_code, dd, strike, opt = m.groups()
        try:
            month  = _MONTH_BY_CODE_SHORT[mon_code]
            year   = 2000 + int(yy)
            day    = int(dd)
            expiry = date(year, month, day)
            return {"kind": "opt", "root": und,
                    "opt_type": opt, "strike": float(strike),
                    "expiry": expiry}
        except Exception:
            pass

    m = _FUT_MONTHLY.match(sym)
    if m:
        und, yy, mon = m.groups()
        try:
            month  = _MONTH_BY_CODE_LONG[mon]
            year   = 2000 + int(yy)
            # Futures expiry varies per commodity (GOLDM ≈ 5th, CRUDEOIL
            # ≈ 19th, NATURALGAS ≈ 25th, base metals ≈ last day). No
            # single rule fits; the Kite instruments cache is the only
            # reliable source. Parser keeps the equity last-Thursday
            # fallback so callers that don't pass `leg.expiry` get a
            # consistent (if approximate) date.
            expiry = _last_thursday(year, month)
            return {"kind": "fut", "root": und, "expiry": expiry}
        except Exception:
            pass

    return None


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Last date in (year, month) whose weekday() == `weekday`."""
    if month == 12:
        first_next = date(year + 1, 1, 1)
    else:
        first_next = date(year, month + 1, 1)
    from datetime import timedelta
    d = first_next
    while True:
        d = d - timedelta(days=1)
        if d.weekday() == weekday:
            return d


def _last_thursday(year: int, month: int) -> date:
    """Last Thursday of the given month — NSE/NFO equity options monthly expiry."""
    return _last_weekday(year, month, 3)


def _last_friday(year: int, month: int) -> date:
    """Last Friday of the given month — MCX commodity options monthly expiry.
    Used by GOLDM / GOLD / SILVER / CRUDEOIL / etc — the exchange shifted off
    the equity 'last Thursday' convention some time back so equity-style fallback
    over-estimated MCX DTE by ~1-3 weeks."""
    return _last_weekday(year, month, 4)


def _monthly_expiry(underlying: str, year: int, month: int) -> date:
    """Pick the monthly-expiry day appropriate for the underlying's exchange.
    MCX commodities (GOLDM, CRUDEOIL, SILVER, etc.) → last Friday.
    Everything else (NSE/NFO equity) → last Thursday."""
    return (_last_friday(year, month)
            if is_mcx_underlying(underlying)
            else _last_thursday(year, month))


def days_to_expiry(expiry: date, *, ref: Optional[datetime] = None,
                   default_days: int = DEFAULT_DTE_DAYS,
                   close_time: tuple[int, int] = (15, 30)) -> float:
    """Whole + fractional days from `ref` (default: now in IST) to `expiry`.
    Floors at 0. Both sides are normalised to Asia/Kolkata so the day
    boundary lines up with the Indian trading calendar regardless of the
    host server's clock timezone.

    `close_time` is (hour, minute) IST when the option actually ceases
    trading on the expiry date — defaults to (15, 30) for NSE/NFO/BSE/CDS.
    Pass (23, 30) for MCX commodity options. This matters most when the
    option expires TODAY: at 3 PM on a 3:30 PM-close expiry day T is
    30 min, not zero, so theta is visible on the today-value curve.
    After market close on expiry day the result is still negative → max(0)
    → 0, which is correct (zero time value remaining).
    """
    if not expiry:
        return float(default_days)
    if ref is None:
        ref = datetime.now(tz=_IST)
    elif ref.tzinfo is None:
        ref = ref.replace(tzinfo=_IST)
    if isinstance(expiry, datetime):
        expiry = expiry.date()
    expiry_dt = datetime(expiry.year, expiry.month, expiry.day,
                         close_time[0], close_time[1], tzinfo=_IST)
    delta = expiry_dt - ref
    days  = delta.total_seconds() / 86400.0
    return max(0.0, days)


# ── Black-Scholes ─────────────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# A&S 7.1.26 polynomial approximation for erf (max err 1.5e-7). Used by
# the vectorized BS helpers below — scipy isn't a project dep and
# `np.vectorize(math.erf)` is just a loop wrapper (no SIMD). The
# polynomial evaluates entirely in NumPy ufuncs so the whole array gets
# C-level vectorization. Accuracy is ~5 orders of magnitude better than
# the operator's typical 2-decimal payoff display precision.
_AS_A1 =  0.254829592
_AS_A2 = -0.284496736
_AS_A3 =  1.421413741
_AS_A4 = -1.453152027
_AS_A5 =  1.061405429
_AS_P  =  0.3275911

def _erf_vec(x: np.ndarray) -> np.ndarray:
    x  = np.asarray(x, dtype=np.float64)
    sign = np.where(x >= 0.0, 1.0, -1.0)
    ax = np.abs(x)
    t  = 1.0 / (1.0 + _AS_P * ax)
    y  = 1.0 - ((((_AS_A5 * t + _AS_A4) * t + _AS_A3) * t + _AS_A2) * t + _AS_A1) \
              * t * np.exp(-ax * ax)
    return sign * y


_INV_SQRT_2 = 1.0 / math.sqrt(2.0)

def _norm_cdf_vec(x: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + _erf_vec(x * _INV_SQRT_2))


def _black_scholes_vec(S_arr: np.ndarray, K: float, T_years: float,
                       r: float, sigma: float, opt_type: str) -> np.ndarray:
    """Vectorized BS over an array of spots. K, T, sigma, opt_type are
    leg-level scalars. Returns a same-shape ndarray of per-share prices.
    Matches the scalar `black_scholes` output to ~1e-7 (A&S erf bound)."""
    S_arr = np.asarray(S_arr, dtype=np.float64)
    if K <= 0:
        return np.zeros_like(S_arr)
    # Degenerate: at/past expiry or zero vol → intrinsic.
    if T_years <= 0 or sigma <= 0:
        if opt_type == "CE":
            return np.maximum(0.0, S_arr - K)
        return np.maximum(0.0, K - S_arr)
    sqrt_T = math.sqrt(T_years)
    # Mask S<=0 to 1.0 inside the log so we don't get -inf; we zero
    # those entries at the end with `np.where(valid, ...)`.
    valid = S_arr > 0
    safe_S = np.where(valid, S_arr, 1.0)
    d1 = (np.log(safe_S / K) + (r + sigma * sigma / 2.0) * T_years) \
         / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    disc = math.exp(-r * T_years)
    if opt_type == "CE":
        prices = safe_S * _norm_cdf_vec(d1) - K * disc * _norm_cdf_vec(d2)
    else:
        prices = K * disc * _norm_cdf_vec(-d2) - safe_S * _norm_cdf_vec(-d1)
    return np.where(valid, prices, 0.0)


def black_scholes(S: float, K: float, T_years: float, r: float,
                  sigma: float, opt_type: str) -> float:
    """
    Vanilla European option price (no dividend yield — Indian index
    options pay no carry between expiries, so q=0 is fine). T_years
    is time-to-expiry in fractional years.
    """
    if S <= 0 or K <= 0:
        return 0.0
    # Degenerate cases — at expiry or zero vol → intrinsic.
    if T_years <= 0 or sigma <= 0:
        if opt_type == "CE":
            return max(0.0, S - K)
        return max(0.0, K - S)

    sqrt_T = math.sqrt(T_years)
    d1 = (math.log(S / K) + (r + sigma * sigma / 2.0) * T_years) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    if opt_type == "CE":
        return S * _norm_cdf(d1) - K * math.exp(-r * T_years) * _norm_cdf(d2)
    return K * math.exp(-r * T_years) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def implied_vol(price: float, S: float, K: float, T_years: float,
                r: float, opt_type: str,
                *, max_iter: int = 80, tol: float = 1e-3) -> float:
    """
    Bisection IV solver. Robust to weird-priced contracts (deep ITM,
    near-zero time value, pre-open stale LTPs) — falls back to
    DEFAULT_IV when the bisection can't bracket a solution.
    """
    if price <= 0 or S <= 0 or K <= 0 or T_years <= 0:
        return DEFAULT_IV

    intrinsic = max(0.0, S - K) if opt_type == "CE" else max(0.0, K - S)
    if price <= intrinsic + 0.05:
        # All intrinsic — vol is undefined; return a small positive number
        # so downstream re-pricing is well-behaved (price won't change much
        # from spot moves on a deep-ITM contract anyway).
        return 0.0001

    lo, hi = 0.0001, 5.0
    p_lo = black_scholes(S, K, T_years, r, lo, opt_type)
    p_hi = black_scholes(S, K, T_years, r, hi, opt_type)
    # If the target price is outside the bracket, fall back.
    if not (p_lo - 0.5 <= price <= p_hi + 0.5):
        return DEFAULT_IV

    for _ in range(max_iter):
        mid    = 0.5 * (lo + hi)
        p_mid  = black_scholes(S, K, T_years, r, mid, opt_type)
        if abs(p_mid - price) < tol:
            return mid
        if p_mid < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# ── Helpers used by the simulator ─────────────────────────────────────

def detect_underlying(symbol: str, row: Optional[dict] = None) -> Optional[str]:
    """Return the root name for a position row, or None if the
    symbol isn't a recognised derivative."""
    parsed = parse_tradingsymbol(symbol)
    if parsed:
        return parsed["root"]
    return None


# Index-to-Kite-LTP-key mapping. Indian index spot tickers don't follow the
# tradingsymbol convention — NIFTY's spot is "NSE:NIFTY 50", not "NSE:NIFTY".
# Stock underlyings DO match (RELIANCE option underlying = "NSE:RELIANCE"),
# so anything not in this map falls through to "NSE:<NAME>".
_INDEX_LTP_KEY = {
    "NIFTY":      "NSE:NIFTY 50",
    "BANKNIFTY":  "NSE:NIFTY BANK",
    "FINNIFTY":   "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NSE:NIFTY MID SELECT",
    "SENSEX":     "BSE:SENSEX",
    "BANKEX":     "BSE:BANKEX",
}

# MCX-traded commodity underlyings — these have NO NSE/BSE spot ticker.
# `underlying_ltp_key("CRUDEOIL")` would return "NSE:CRUDEOIL" which is
# bogus, so the spot resolver falls through to the median-strike fallback
# and the chart anchors at the wrong number (9000 instead of the actual
# 9106 May futures price). For commodities the option's underlying IS
# the matching monthly futures contract on MCX, so we re-route the spot
# lookup to the futures quote.
_MCX_COMMODITIES = frozenset({
    # Energy
    "CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATGASMINI", "NATURALGASM",
    # Bullion
    "GOLD", "GOLDM", "GOLDMINI", "GOLDPETAL", "GOLDGUINEA",
    "SILVER", "SILVERM", "SILVERMINI", "SILVERMIC", "SILVERMICRO",
    # Base metals
    "COPPER", "COPPERM", "ZINC", "ZINCMINI", "LEAD", "LEADMINI",
    "ALUMINIUM", "ALUMINI", "ALUMINIUMMINI", "NICKEL",
    # Agri
    "MENTHAOIL", "COTTON", "CASTORSEED", "KAPAS", "CARDAMOM",
    "CPO", "RBDPMOLEIN",
})


def is_mcx_underlying(underlying: str) -> bool:
    """True when the underlying is an MCX-traded commodity whose
    "spot" is the matching monthly futures contract, not an
    NSE/BSE index ticker."""
    return (underlying or "").upper() in _MCX_COMMODITIES


def underlying_ltp_key(underlying: str) -> str:
    """Kite quote/ltp key for an underlying's spot. Indices use their
    special tickers; stocks use NSE:<underlying>. Commodities have no
    spot key — callers should detect via `is_mcx_underlying()` and
    look up the matching futures contract instead.

    Most callers should prefer `option_underlying_quote_key(symbol)`
    which handles MCX commodities and per-expiry resolution in one
    call; this lower-level helper exists for the rare path where the
    caller already knows the underlying name in isolation."""
    name = (underlying or "").upper()
    return _INDEX_LTP_KEY.get(name, f"NSE:{name}")


async def lookup_mcx_futures_list(underlying: str, limit: int = 2) -> list[str]:
    """Return the next *limit* non-expired MCX futures for *underlying*,
    sorted by expiry.

    Delegates to the canonical resolver in ``symbol_resolver.py``.
    Returns an empty list when the cache is cold or no commodity matches.
    """
    from backend.api.algo.symbol_resolver import list_active_futures
    return await list_active_futures(underlying, "MCX", limit=limit)


async def lookup_cds_futures_list(underlying: str, limit: int = 2) -> list[str]:
    """Same as lookup_mcx_futures_list but for CDS currency futures
    (USDINR / EURINR / GBPINR / JPYINR).

    Delegates to the canonical resolver in ``symbol_resolver.py``.
    """
    from backend.api.algo.symbol_resolver import list_active_futures
    return await list_active_futures(underlying, "CDS", limit=limit)


async def lookup_mcx_front_month_future(underlying: str) -> str | None:
    """Resolve the FRONT-MONTH liquid MCX futures tradingsymbol for an
    underlying — the contract operators read as "today's spot price".

    Delegates to the canonical resolver in ``symbol_resolver.py``.
    Returns None when the cache is cold / no commodity matches.
    """
    if not underlying:
        return None
    from backend.api.algo.symbol_resolver import list_active_futures
    futures = await list_active_futures(underlying, "MCX", limit=1)
    return futures[0] if futures else None


# Per-cache by-symbol index, keyed by the ID of the `items` list so a
# new cache cycle (24h TTL refresh in instruments.py) drops the old
# index automatically. Walking the ~90k-row items list per call adds
# measurable latency to multi-leg strategy requests; an O(1) Map cuts
# the hot path to a single dict lookup.
_INSTRUMENT_INDEX_BY_ID: dict[int, dict] = {}


def _instrument_index(items) -> dict:
    """Lazy-build a `{symbol_upper → Instrument}` dict for the given
    items list. Returns an empty dict when items is falsy."""
    if not items:
        return {}
    key = id(items)
    cached = _INSTRUMENT_INDEX_BY_ID.get(key)
    if cached is not None:
        return cached
    # Bound the cache so a transient cache rebuild doesn't leak
    # ever-growing dicts. Two entries is enough — current + previous.
    if len(_INSTRUMENT_INDEX_BY_ID) >= 2:
        _INSTRUMENT_INDEX_BY_ID.clear()
    index = {(inst.s or "").upper(): inst for inst in items}
    _INSTRUMENT_INDEX_BY_ID[key] = index
    return index


def _next_nse_future(items, root: str, today_iso: str) -> str | None:
    """Pick the first NSE/NFO future for `root` whose expiry is strictly
    after `today_iso`. Equity-side equivalent of
    `lookup_mcx_front_month_future` for the same-day-expiry rollover.
    Returns the bare tradingsymbol (e.g. NIFTY26JULFUT) or None when no
    later-month future is listed in the cache."""
    if not items or not root:
        return None
    root_upper = root.upper()
    candidates = [
        inst for inst in items
        if (inst.e in ("NFO", "NSE")
            and inst.t == "FUT"
            and (inst.u or "").upper() == root_upper
            and inst.x
            and inst.x > today_iso)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda i: i.x or "")
    return candidates[0].s


def _ist_today_iso() -> str:
    """Return today's date as an ISO string anchored to Asia/Kolkata.
    Falls back to UTC date if the ZoneInfo lookup fails (unlikely in
    production but defensive for test environments)."""
    from datetime import datetime as _dt
    try:
        from zoneinfo import ZoneInfo as _ZI
        return _dt.now(_ZI("Asia/Kolkata")).date().isoformat()
    except Exception:
        return _dt.utcnow().date().isoformat()


async def _load_instruments_items() -> list:
    """Fetch instruments from the TTL cache. Returns an empty list on
    any error so callers can treat a cache-miss as a no-op."""
    from backend.api.cache import get_or_fetch
    from backend.api.routes.instruments import _fetch_instruments, _TTL_SECONDS
    try:
        resp = await get_or_fetch("instruments", _fetch_instruments,
                                  ttl_seconds=_TTL_SECONDS)
        return resp.items if resp else []
    except Exception:
        return []


def _check_option_expired(sym: str, items: list, today_iso: str) -> bool:
    """Return True when the option `sym` has a known expiry on or before
    `today_iso` (i.e. MCX commodity options that expire 5 business days
    before the futures). Uses the O(1) instrument index."""
    if not items:
        return False
    index = _instrument_index(items)
    inst = index.get(sym)
    if inst is None:
        return False
    opt_expiry = (inst.x or "")
    return bool(opt_expiry and opt_expiry <= today_iso)


async def _roll_past_month(
    root: str,
    items: list,
    matched_fut_expiry: str,
    today_iso: str,
) -> str | None:
    """Pick the first future for *root* whose expiry is strictly AFTER
    *matched_fut_expiry*. Using the matched future's expiry as the cutoff
    (not just the option's expiry or today) correctly skips the entire
    matched month — on Jun 16 for a JUN option, the matched JUN future
    expires Jun 18, so the cutoff > Jun 18 lands on JUL future (Jul 20),
    not back on JUN."""
    cutoff = matched_fut_expiry or today_iso
    if is_mcx_underlying(root):
        target_u = root.upper()
        candidates = [
            inst for inst in items
            if (inst.e == "MCX"
                and inst.t == "FUT"
                and (inst.u or "").upper() == target_u
                and inst.x
                and inst.x > cutoff)
        ]
        if candidates:
            candidates.sort(key=lambda i: i.x or "")
            return candidates[0].s
        return await lookup_mcx_front_month_future(root)
    # NSE / NFO equivalent
    return _next_nse_future(items, root, cutoff)


async def _matched_or_roll(
    inst,
    root: str,
    items: list,
    opt_expired: bool,
    today_iso: str,
) -> str | None:
    """Return the matched instrument's symbol when it is still live and
    the option has not expired; otherwise roll past this month."""
    inst_expiry = (inst.x or "")
    is_live = bool(inst_expiry and inst_expiry > today_iso)
    if is_live and not opt_expired:
        return inst.s
    return await _roll_past_month(root, items, inst_expiry, today_iso)


async def _resolve_monthly_option_future(sym: str, m_monthly) -> str | None:
    """Resolve the matching future for a MONTHLY option symbol.

    Pass 1: exact symbol match via O(1) index (covers NSE/NFO).
    Pass 2: prefix match with FUT suffix (covers MCX day-suffix variants,
            e.g. CRUDEOIL26JUL19FUT where the bare form is CRUDEOIL26JULFUT).
    Returns None on cache miss (future not listed yet).
    """
    root, yy, mon, _strike, _opt = m_monthly.groups()
    fut_sym = f"{root}{yy}{mon}FUT"
    items = await _load_instruments_items()
    if not items:
        return None
    today = _ist_today_iso()
    opt_expired = _check_option_expired(sym, items, today)
    fut_sym_upper = fut_sym.upper()
    prefix = f"{root}{yy}{mon}".upper()  # e.g. CRUDEOIL26JUL
    # Pass 1: exact match (covers NSE/NFO) — O(1) via instrument index.
    exact = _instrument_index(items).get(fut_sym_upper)
    if exact is not None:
        return await _matched_or_roll(exact, root, items, opt_expired, today)
    # Pass 2: prefix match ending in FUT (covers MCX day-suffix).
    # When multiple matches exist (rare) we take the FIRST in cache order,
    # which is the contract listed closest to the canonical month.
    for inst in items:
        s = (inst.s or "").upper()
        if s.startswith(prefix) and s.endswith("FUT") and s != fut_sym_upper:
            return await _matched_or_roll(inst, root, items, opt_expired, today)
    # Cache miss — future for this month not listed yet.
    return None


async def _resolve_weekly_option_future(sym: str, m_weekly) -> str | None:
    """Resolve the front-month future for a WEEKLY option symbol.

    Weekly options (Kite single-digit month + 2-digit day format) share
    the front-month future of their underlying index. MCX delegates to
    `lookup_mcx_front_month_future`; NSE/NFO scans the instruments cache
    for the nearest non-expired NFO future for the root."""
    root = m_weekly.group(1)
    if is_mcx_underlying(root):
        return await lookup_mcx_front_month_future(root)
    items = await _load_instruments_items()
    if not items:
        return None
    today = _ist_today_iso()
    return _next_nse_future(items, root, today)


async def lookup_future_for_option(option_symbol: str) -> str | None:
    """Return the futures tradingsymbol matching this option's month token,
    e.g. NIFTY26JUN22000CE → NIFTY26JUNFUT, CRUDEOIL25JUN5800CE → CRUDEOIL25JUNFUT.

    For weekly options (no extractable monthly token — the Kite weekly format
    uses a single-digit month code + 2-digit day with no 3-letter MON string),
    falls back to the front-month future via lookup_mcx_front_month_future (MCX)
    or the first listed NFO future (NSE F&O). Weekly options share the same
    front-month future as their underlying index contract.

    Returns None when the option symbol can't be parsed or no matching future
    is in the instruments cache.
    """
    if not option_symbol:
        return None
    sym = option_symbol.upper().strip()
    m_monthly = _OPT_MONTHLY.match(sym)
    if m_monthly:
        return await _resolve_monthly_option_future(sym, m_monthly)
    m_weekly = _OPT_WEEKLY.match(sym)
    if m_weekly:
        return await _resolve_weekly_option_future(sym, m_weekly)
    return None


async def lookup_mcx_future_for_expiry(underlying: str,
                                       target_expiry: date) -> str | None:
    """Return the MCX FUT tradingsymbol for *underlying* whose expiry is the
    FIRST futures contract expiring ON OR AFTER *target_expiry*.

    This is the calendar-aware counterpart of ``lookup_mcx_front_month_future``.
    When the operator analyses a CRUDEOIL Sep option, the option's underlying
    is the Sep future — using the Jun front-month future as the spot anchor
    gives a BS calibration that is off by the Jun→Sep basis spread (up to
    ₹200–500 on a ₹6800 base).

    Resolution logic:
      1. Filter MCX FUT instruments for *underlying* with a non-empty expiry.
      2. Sort ascending by expiry string (ISO format sorts correctly).
      3. Return the FIRST contract whose expiry date is >= target_expiry.
         For options that expire WITH the front-month (target_expiry is on or
         before the nearest listed futures expiry), this is the front-month —
         i.e. existing behaviour is preserved.
      4. When no contract is on-or-after target_expiry (rare — operator
         analysing a far-out option that has no listed future yet), fall back
         to the LAST listed contract (nearest-available far contract).
      5. Returns None when the instruments cache is cold or no MCX future is
         listed for the underlying at all.

    Return type: bare tradingsymbol (e.g. ``'CRUDEOIL26SEPFUT'``) or None.
    """
    if not underlying or target_expiry is None:
        return None
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
    # Today (IST) — gate out settling-today contracts. Same rollover
    # rule lookup_mcx_front_month_future enforces; operator: "when
    # options expire consider the next crudeoil future as the spot."
    from datetime import datetime as _dt
    try:
        _today_iso = _dt.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
    except Exception:
        _today_iso = _dt.utcnow().date().isoformat()
    target_u = underlying.upper()
    candidates = [
        inst for inst in items
        if (inst.e == "MCX"
            and inst.t == "FUT"
            and (inst.u or "").upper() == target_u
            and inst.x
            and inst.x > _today_iso)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda i: i.x or "")
    target_iso = target_expiry.isoformat()
    # First contract whose expiry >= target_expiry. If target_expiry is
    # today (e.g. the option settles today), we'd want the next-out
    # future — the live-only filter above already handles that since
    # today-expiring contracts have been dropped from the list.
    for inst in candidates:
        if (inst.x or "") >= target_iso:
            return inst.s
    # All listed futures expire before target_expiry — return the last one
    # (farthest available contract is the best we can do)
    return candidates[-1].s


async def front_month_underlying_quote_key(underlying: str) -> str | None:
    """Kite quote key for the "current liquid spot" of an underlying
    NAME, resolved differently per instrument class:

      • Index (NIFTY, BANKNIFTY, …)   → NSE:NIFTY 50 (etc.)
      • Stock (RELIANCE, INFY, …)     → NSE:RELIANCE
      • MCX commodity (CRUDEOIL, …)   → MCX:<front-month-future>
                                         (skips today's-expiry month)

    Counterpart of `option_underlying_quote_key(symbol)`:
      - This one takes the bare NAME and returns the front-month
        future for MCX (operators read this as "today's crude price").
      - The other takes a full TRADINGSYMBOL and returns that
        option's MATCHING-month future for MCX (the spot under
        THIS contract for σ-calibration).

    Both designs are correct in their context; choose by whether you
    have a name or a symbol on hand. Async because MCX path hits the
    instruments cache (deferred imports inside).

    Returns None when underlying is empty or no MCX contract resolves
    (cache cold / commodity has no listed front-month). For non-MCX
    callers, never returns None — falls back to `NSE:<name>` which is
    Kite's default for an equity ticker.
    """
    if not underlying:
        return None
    if is_mcx_underlying(underlying):
        sym = await lookup_mcx_front_month_future(underlying)
        if not sym:
            return None
        return f"MCX:{sym}"
    return underlying_ltp_key(underlying)


def option_quote_key(symbol: str) -> str | None:
    """Kite quote/ltp key for an F&O contract ITSELF (not its
    underlying). Routes commodity contracts to MCX, everything else
    to NFO. Returns None for unparseable input.

    Examples:
      NIFTY26APR22000CE   → NFO:NIFTY26APR22000CE
      RELIANCE26APR2800CE → NFO:RELIANCE26APR2800CE
      CRUDEOIL26JUN9500CE → MCX:CRUDEOIL26JUN9500CE
      CRUDEOIL26JUNFUT    → MCX:CRUDEOIL26JUNFUT

    Counterpart of `option_underlying_quote_key` — that one quotes
    the underlying SPOT for charting; this one quotes the OPTION
    itself for LTP / depth / strategy-analytics batch fetches.
    """
    if not symbol:
        return None
    parsed = parse_tradingsymbol(symbol)
    if not parsed:
        return None
    name = parsed.get("root") or ""
    if is_mcx_underlying(name):
        return f"MCX:{symbol}"
    return f"NFO:{symbol}"


def option_underlying_quote_key(symbol: str) -> str | None:
    """Resolve the Kite quote/ltp key for an option's / future's
    UNDERLYING SPOT.

    One call replaces the parse + branch + futures-lookup dance that
    several call sites (paper.py, sim driver, options.py spot
    resolver) used to duplicate, each with slight variations and
    bug-prone defaults. Centralising here means a single source of
    truth for "what do I quote to chart the underlying line next to
    this option?".

    Returns:
      • NSE:NIFTY 50 (and friends) for index options like NIFTY26APR…
      • NSE:RELIANCE (etc.) for stock options like RELIANCE26APR…
      • MCX:CRUDEOIL26JUNFUT for an MCX commodity option (the
        matching-month future, not the front-month — a June option
        gets the June future, fixing the earlier "May spot under a
        June option chart" bug).
      • None when the symbol is unparseable, or when it parses to an
        MCX commodity but carries no expiry (callers should skip the
        underlying line in that case).
    """
    parsed = parse_tradingsymbol(symbol)
    if not parsed:
        return None
    name = parsed.get("root")
    if not name:
        return None
    if is_mcx_underlying(name):
        expiry = parsed.get("expiry")
        if not expiry:
            return None
        return f"MCX:{futures_symbol_for_expiry(name, expiry)}"
    return underlying_ltp_key(name)


_FUT_MONTH_CODES = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def futures_symbol_for_expiry(underlying: str, expiry: date) -> str:
    """Build the Kite monthly-futures tradingsymbol matching `expiry`'s
    month. CRUDEOIL + 2025-05-19 → CRUDEOIL25MAYFUT. Used as the
    underlying-spot proxy for commodities and as a sanity-check fallback
    for index/stock options when the spot ticker fails."""
    yy  = expiry.year % 100
    mon = _FUT_MONTH_CODES[expiry.month - 1]
    return f"{(underlying or '').upper()}{yy:02d}{mon}FUT"


def _close_time_for_row(row: dict) -> tuple[int, int]:
    """Return the IST market-close time for a position row.

    Reads `row['exchange']` which the sim driver populates on every
    position row (driver.py sets it during seed). Live broker rows carry
    the exchange column from Kite's positions response. Falls back to
    NSE/NFO (15:30) when the exchange is absent or unrecognised.
    """
    exch = (row.get("exchange") or "").upper()
    return (23, 30) if exch == "MCX" else (15, 30)


def calibrate_iv_for_row(row: dict, spot: float,
                         *, risk_free: float = DEFAULT_RISK_FREE,
                         ref_now: Optional[datetime] = None) -> Optional[float]:
    """
    Given a position row + a known underlying spot, calibrate IV so
    Black-Scholes(spot, strike, T, r, σ) matches the row's current
    last_price. Returns the σ, or None if the row isn't an option.
    """
    sym    = str(row.get("tradingsymbol") or "")
    parsed = parse_tradingsymbol(sym)
    if not parsed or parsed["kind"] != "opt":
        return None
    ltp    = row.get("last_price")
    if ltp is None or float(ltp) <= 0:
        return DEFAULT_IV
    expiry = parsed["expiry"]
    T_yrs  = days_to_expiry(expiry, ref=ref_now,
                             close_time=_close_time_for_row(row)) / 365.0
    return implied_vol(float(ltp), float(spot), parsed["strike"],
                       T_yrs, risk_free, parsed["opt_type"])


def reprice_row(row: dict, *, spot: float, sigma: Optional[float],
                risk_free: float = DEFAULT_RISK_FREE,
                ref_now: Optional[datetime] = None) -> Optional[float]:
    """
    Re-price a derivative row given a new underlying `spot` and a cached
    σ (only used for options). Returns the new last_price, or None if
    the row isn't a recognised derivative on this underlying.

    Futures track spot 1:1 (cost-of-carry over a few minutes is sub-tick).
    """
    sym    = str(row.get("tradingsymbol") or "")
    parsed = parse_tradingsymbol(sym)
    if not parsed:
        return None
    if parsed["kind"] == "fut":
        return float(spot)
    if parsed["kind"] == "opt":
        expiry = parsed["expiry"]
        T_yrs  = days_to_expiry(expiry, ref=ref_now,
                                close_time=_close_time_for_row(row)) / 365.0
        sig    = sigma if (sigma and sigma > 0) else DEFAULT_IV
        return black_scholes(float(spot), parsed["strike"],
                             T_yrs, risk_free, sig, parsed["opt_type"])
    return None


# ── Greeks ────────────────────────────────────────────────────────────

def _norm_pdf(x: float) -> float:
    return math.exp(-x * x / 2.0) / math.sqrt(2.0 * math.pi)


def greeks(S: float, K: float, T_years: float, r: float,
           sigma: float, opt_type: str) -> dict:
    """
    Per-share analytical Greeks for a vanilla European option. Returned
    fields:

      delta:  ∂price/∂spot      (dimensionless; multiply by qty for $-delta)
      gamma:  ∂²price/∂spot²    (per ₹1 spot move; tiny number for index opts)
      theta:  ∂price/∂time      (decimal: PER DAY — divide annual θ by 365)
      vega:   ∂price/∂σ         (per 1 % IV change — divide raw vega by 100)
      rho:    ∂price/∂r         (per 1 % rate change — divide raw rho by 100)

    Theta / vega / rho are returned in the trader-friendly units (per day,
    per 1 % vol, per 1 % rate) rather than the raw mathematical units.
    Degenerate cases (T ≤ 0, σ ≤ 0) return zeros for everything except
    delta, where intrinsic-direction is preserved (calls → 1 if ITM,
    puts → -1 if ITM).
    """
    if S <= 0 or K <= 0:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}
    if T_years <= 0 or sigma <= 0:
        # At expiry: delta is sign-of-intrinsic, others vanish.
        if opt_type == "CE":
            d = 1.0 if S > K else 0.0
        else:
            d = -1.0 if S < K else 0.0
        return {"delta": d, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}

    sqrt_T = math.sqrt(T_years)
    d1 = (math.log(S / K) + (r + sigma * sigma / 2.0) * T_years) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    nd1 = _norm_pdf(d1)
    Nd1 = _norm_cdf(d1)
    Nd2 = _norm_cdf(d2)

    if opt_type == "CE":
        delta = Nd1
        theta_yr = (-S * nd1 * sigma / (2.0 * sqrt_T)
                    - r * K * math.exp(-r * T_years) * Nd2)
        rho_raw  = K * T_years * math.exp(-r * T_years) * Nd2
    else:
        delta = Nd1 - 1.0
        theta_yr = (-S * nd1 * sigma / (2.0 * sqrt_T)
                    + r * K * math.exp(-r * T_years) * _norm_cdf(-d2))
        rho_raw  = -K * T_years * math.exp(-r * T_years) * _norm_cdf(-d2)

    gamma     = nd1 / (S * sigma * sqrt_T)
    vega_raw  = S * nd1 * sqrt_T

    # Trader-friendly units.
    return {
        "delta": delta,
        "gamma": gamma,
        "theta": theta_yr / 365.0,        # per day
        "vega":  vega_raw / 100.0,        # per 1 % IV
        "rho":   rho_raw  / 100.0,        # per 1 % rate
    }


# ── Probability of profit (POP) ───────────────────────────────────────

def prob_above(S: float, K: float, T_years: float, r: float, sigma: float) -> float:
    """
    P(S_T ≥ K) under the Black-Scholes log-normal assumption (risk-
    neutral). Uses the standard d2 form. Floors / ceilings at 0/1
    when σ ≤ 0 or T ≤ 0 — those collapse to deterministic outcomes.
    """
    if S <= 0 or K <= 0:
        return 0.0
    if T_years <= 0 or sigma <= 0:
        return 1.0 if S > K else 0.0
    sqrt_T = math.sqrt(T_years)
    d2 = (math.log(S / K) + (r - sigma * sigma / 2.0) * T_years) / (sigma * sqrt_T)
    return _norm_cdf(d2)


# ── Risk metrics + payoff curve ───────────────────────────────────────

def risk_metrics(*, S: float, K: float, T_years: float, r: float,
                 sigma: float, opt_type: str, qty: int,
                 entry_price: float) -> dict:
    """
    Position-level max-profit / max-loss / breakeven / POP for a single-
    leg option position. `qty` is signed (positive = long, negative =
    short). `entry_price` is the per-share premium paid (long) or
    received (short) — typically `average_price` on the broker row, or
    the LTP for a hypothetical "what if I bought this now" view.

    Returned fields are in absolute rupees for the whole position
    (i.e. already multiplied by |qty|). `max_profit` / `max_loss` may be
    `float('inf')` for unlimited-payoff legs; the API serializes that as
    null so the UI can render "∞".
    """
    if qty == 0:
        return {"max_profit": 0.0, "max_loss": 0.0, "breakeven": K, "pop": 0.0,
                "long_short": "flat"}

    long  = qty > 0
    n     = abs(int(qty))
    if opt_type == "CE":
        breakeven = K + entry_price
        if long:
            max_profit = float("inf")           # call going to +∞
            max_loss   = entry_price * n        # premium burns
            pop = prob_above(S, breakeven, T_years, r, sigma)
        else:
            max_profit = entry_price * n        # premium kept if expires worthless
            max_loss   = float("inf")
            pop = 1.0 - prob_above(S, breakeven, T_years, r, sigma)
    else:                                       # PE
        breakeven = K - entry_price
        if long:
            max_profit = max(0.0, K - entry_price) * n   # spot → 0 floor
            max_loss   = entry_price * n
            pop = 1.0 - prob_above(S, breakeven, T_years, r, sigma)
        else:
            max_profit = entry_price * n
            max_loss   = max(0.0, K - entry_price) * n
            pop = prob_above(S, breakeven, T_years, r, sigma)

    return {
        "max_profit": max_profit,
        "max_loss":   max_loss,
        "breakeven":  breakeven,
        "pop":        pop,
        "long_short": "long" if long else "short",
    }


def payoff_curve(*, S: float, K: float, T_years: float, r: float,
                 sigma: float, opt_type: str, qty: int,
                 entry_price: float, span_pct: float = 0.10,
                 points: int = 51) -> list[dict]:
    """
    Build a list of {spot, today_value, expiry_value} entries spanning
    ±span_pct around the current spot. `today_value` uses Black-Scholes
    (current DTE + IV); `expiry_value` is the intrinsic payoff. Both are
    P&L for the WHOLE position (already multiplied by qty), net of
    `entry_price * qty`, so they read as "money you'd make/lose" rather
    than "what the option's worth".

    Used by the /admin/options payoff chart — the operator sees today's
    curve (with time value) sitting above the expiry curve (intrinsic),
    converging as DTE → 0.
    """
    if S <= 0 or qty == 0 or points < 2:
        return []
    S_grid = np.linspace(S * (1.0 - span_pct), S * (1.0 + span_pct), points)
    cost   = entry_price * qty   # signed
    today_vals  = _black_scholes_vec(S_grid, K, T_years, r, sigma, opt_type) * qty - cost
    if opt_type == "CE":
        intrinsic = np.maximum(0.0, S_grid - K)
    else:
        intrinsic = np.maximum(0.0, K - S_grid)
    expiry_vals = intrinsic * qty - cost
    return [
        {"spot":         round(float(S_grid[i]),       4),
         "today_value":  round(float(today_vals[i]),   2),
         "expiry_value": round(float(expiry_vals[i]),  2)}
        for i in range(points)
    ]


# ── Multi-leg helpers ─────────────────────────────────────────────────
#
# A "leg" is a dict with the per-leg state we need:
#   {strike, opt_type, qty (signed), entry_price, T_years, sigma}
#
# All legs in a strategy must share the same underlying and (for v1)
# the same expiry. The route layer enforces that; the math here doesn't
# revalidate.

def _leg_today_expiry_arrays(
    leg: dict,
    S_grid: "np.ndarray",
    r: float,
    eval_T: "float | None",
) -> "tuple[np.ndarray, np.ndarray]":
    """Return *(today_values, expiry_values)* for a single leg, both already
    multiplied by *qty*.

    *S_grid* is the spot axis for the chart. *r* is the risk-free rate.
    *eval_T* is the near-leg expiry horizon for calendar/diagonal spreads
    (None → intrinsic at T=0 for all legs).
    """
    kind  = leg.get("kind") or "opt"
    qty   = int(leg["qty"])
    scale = float(leg.get("scale_ratio") or 1.0)
    s_leg = S_grid * scale

    if kind == "fut":
        today_arr  = s_leg * qty
        expiry_arr = s_leg * qty
        return today_arr, expiry_arr

    K     = float(leg["strike"])
    opt   = leg["opt_type"]
    T_yrs = float(leg.get("T_years") or 0)
    sig   = float(leg.get("sigma") or DEFAULT_IV)

    today_arr = _black_scholes_vec(s_leg, K, T_yrs, r, sig, opt) * qty

    if eval_T is not None and T_yrs > eval_T:
        T_remaining = T_yrs - eval_T
        expiry_arr = _black_scholes_vec(s_leg, K, T_remaining, r, sig, opt) * qty
    else:
        if opt == "CE":
            intrinsic = np.maximum(0.0, s_leg - K)
        else:
            intrinsic = np.maximum(0.0, K - s_leg)
        expiry_arr = intrinsic * qty

    return today_arr, expiry_arr


def multileg_payoff_curve(legs: list[dict], *, S: float,
                          r: float = DEFAULT_RISK_FREE,
                          span_pct: float = 0.10,
                          points: int = 51,
                          eval_T: Optional[float] = None) -> list[dict]:
    """
    Aggregate `(spot, today_value, expiry_value)` curve summed across all
    legs. `today_value` uses each leg's own (T_years, sigma); `expiry_value`
    evaluates at `eval_T` (the near-leg expiry for calendar/diagonal spreads).

    When `eval_T` is None all legs use intrinsic at T=0 (single-expiry
    behaviour, unchanged). When `eval_T` is set and a leg's T_years > eval_T,
    the far leg is re-priced with its remaining time (T_years - eval_T) via
    Black-Scholes rather than intrinsic, which is the correct P&L at the
    near-leg's expiry date for a calendar spread.

    Both curves are net of cumulative entry cost, so they read as total
    position P&L.
    """
    if S <= 0 or not legs or points < 2:
        return []
    S_grid = np.linspace(S * (1.0 - span_pct), S * (1.0 + span_pct), points)
    total_cost = sum(float(l.get("entry_price") or 0) * int(l.get("qty") or 0)
                     for l in legs)

    today_arr  = np.zeros(points, dtype=np.float64)
    expiry_arr = np.zeros(points, dtype=np.float64)
    for l in legs:
        leg_today, leg_expiry = _leg_today_expiry_arrays(l, S_grid, r, eval_T)
        today_arr  += leg_today
        expiry_arr += leg_expiry
    today_arr  -= total_cost
    expiry_arr -= total_cost
    return [
        {"spot":         round(float(S_grid[i]),     4),
         "today_value":  round(float(today_arr[i]),  2),
         "expiry_value": round(float(expiry_arr[i]), 2)}
        for i in range(points)
    ]


# ── Time-slice payoff curves ──────────────────────────────────────────
#
# Operators want to see how the position's value decays between Today
# and Expiry. A single intermediate curve at T-halfway already telegraphs
# theta acceleration; two slices (T-33%, T-67%) give a smoother visual
# without crowding the chart.
#
# Slice fractions are evenly spaced between Today (elapsed=0) and Expiry
# (elapsed=1):
#   1 → [0.5];   2 → [1/3, 2/3];   3 → [0.25, 0.5, 0.75]; …
# Each slice's curve uses the same Black-Scholes machinery the Today
# curve uses — just with `T_years × (1 − elapsed)` instead of full
# `T_years`. Output is parallel to the existing `payoff` array so the
# frontend can map each slice's `values[i]` to `payoff[i].spot` without
# a second spot grid.
#
# Each entry: {label: "T-Nd", elapsed_pct: float, days_left: float,
#              values: list[float]}.  `label` is a compact display
# string the chart legend can render.

def _slice_fractions(time_slices: int) -> list[float]:
    if time_slices <= 0:
        return []
    return [(i + 1) / (time_slices + 1) for i in range(time_slices)]


def _slice_label(days_left: float) -> str:
    """Compact label for a time-slice. Rounds to whole days when the
    remaining DTE is ≥ 1 day, falls back to hours below that so a 3-h
    slice on an expiry-day position doesn't display as `T-0d`."""
    if days_left >= 1:
        return f"T-{days_left:.0f}d"
    return f"T-{max(0.0, days_left * 24):.0f}h"


def intermediate_curves(*, S: float, K: float, T_years: float, r: float,
                        sigma: float, opt_type: str, qty: int,
                        entry_price: float, span_pct: float = 0.10,
                        points: int = 51, time_slices: int = 0) -> list[dict]:
    """
    Single-leg companion to `payoff_curve`. Produces N intermediate-DTE
    Black-Scholes curves between Today (full T_years) and Expiry (T=0).
    Empty list when `time_slices <= 0` or `T_years <= 0` (no decay to
    visualise on a same-day position).
    """
    fractions = _slice_fractions(time_slices)
    if S <= 0 or qty == 0 or points < 2 or not fractions or T_years <= 0:
        return []
    S_grid = np.linspace(S * (1.0 - span_pct), S * (1.0 + span_pct), points)
    cost = entry_price * qty
    out: list[dict] = []
    for p in fractions:
        T_p = T_years * (1.0 - p)
        days_left = max(0.0, T_p * 365.0)
        bs_arr = _black_scholes_vec(S_grid, K, T_p, r, sigma, opt_type)
        values = [round(float(bs_arr[i]) * qty - cost, 2) for i in range(points)]
        out.append({
            "label":       _slice_label(days_left),
            "elapsed_pct": round(p, 3),
            "days_left":   round(days_left, 2),
            "values":      values,
        })
    return out


def _accumulate_leg_slice(legs: list[dict], S_grid, elapsed: float, r: float) -> "np.ndarray":
    """Compute the combined P&L array for all legs at a given time-elapsed fraction.

    Each option leg's T_years is scaled by (1 - elapsed). Futures contribute
    spot-linear payoff (theta-flat). Returns a numpy array of length len(S_grid).
    """
    import numpy as np
    slice_arr = np.zeros(len(S_grid), dtype=np.float64)
    for l in legs:
        kind  = l.get("kind") or "opt"
        qty   = int(l["qty"])
        scale = float(l.get("scale_ratio") or 1.0)
        s_leg = S_grid * scale
        if kind == "fut":
            slice_arr += s_leg * qty
            continue
        K     = float(l["strike"])
        opt   = l["opt_type"]
        T_yrs = float(l.get("T_years") or 0) * (1.0 - elapsed)
        sig   = float(l.get("sigma") or DEFAULT_IV)
        slice_arr += _black_scholes_vec(s_leg, K, T_yrs, r, sig, opt) * qty
    return slice_arr


def multileg_intermediate_curves(legs: list[dict], *, S: float,
                                 r: float = DEFAULT_RISK_FREE,
                                 span_pct: float = 0.10,
                                 points: int = 51,
                                 time_slices: int = 0) -> list[dict]:
    """
    Multi-leg companion to `multileg_payoff_curve`. Each option leg's
    own `T_years` is scaled by `(1 − elapsed)`; futures stay linear in
    spot (theta-flat over the slice horizon). Label uses the longest-
    DTE option leg as the reference clock so a calendar/diagonal would
    label correctly — but v1 strategy enforces same-expiry across legs
    so all option legs share T_years anyway.
    """
    fractions = _slice_fractions(time_slices)
    if S <= 0 or not legs or points < 2 or not fractions:
        return []
    S_grid = np.linspace(S * (1.0 - span_pct), S * (1.0 + span_pct), points)
    total_cost = sum(float(l.get("entry_price") or 0) * int(l.get("qty") or 0)
                     for l in legs)
    base_T_years = max(
        (float(l.get("T_years") or 0) for l in legs
         if (l.get("kind") or "opt") == "opt"),
        default=0.0,
    )
    if base_T_years <= 0:
        return []
    out: list[dict] = []
    for p in fractions:
        T_label   = base_T_years * (1.0 - p)
        days_left = max(0.0, T_label * 365.0)
        slice_arr = _accumulate_leg_slice(legs, S_grid, p, r) - total_cost
        values = [round(float(slice_arr[i]), 2) for i in range(len(S_grid))]
        out.append({
            "label":       _slice_label(days_left),
            "elapsed_pct": round(p, 3),
            "days_left":   round(days_left, 2),
            "values":      values,
        })
    return out


def multileg_greeks(legs: list[dict], *, S: float,
                    r: float = DEFAULT_RISK_FREE) -> dict:
    """
    Position-level Greeks summed across all legs (signed qty applied per
    leg). Linear in qty so summation works directly. Returned in trader
    units (theta/day, vega per 1 % IV, rho per 1 % rate).
    """
    out = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}
    for l in legs:
        qty = int(l["qty"])
        kind = l.get("kind") or "opt"
        if kind == "fut":
            # Futures contribute pure delta (1 per share, signed by qty).
            # Gamma / Theta / Vega / Rho are zero — futures payoff is
            # linear in spot.
            out["delta"] += qty
            continue
        K     = float(l["strike"])
        opt   = l["opt_type"]
        T_yrs = float(l.get("T_years") or 0)
        sig   = float(l.get("sigma") or DEFAULT_IV)
        # Apply scale_ratio so Greeks evaluate at the leg's own contract-
        # month spot rather than the chart's near-month reference spot.
        scale = float(l.get("scale_ratio") or 1.0)
        g = greeks(S * scale, K, T_yrs, r, sig, opt)
        for k in out:
            out[k] += g[k] * qty
    return out


def find_breakevens(curve: list[dict], *, key: str = "expiry_value"
                    ) -> list[float]:
    """
    Linear-interpolated zero-crossings on `curve[*][key]`. Iron-condor-
    shaped strategies have 2 breakevens; verticals usually have 1; a
    fully ITM/OTM strategy has 0.
    """
    if len(curve) < 2:
        return []
    out: list[float] = []
    for i in range(len(curve) - 1):
        a, b = curve[i], curve[i + 1]
        ya, yb = a[key], b[key]
        if ya == 0.0:
            out.append(float(a["spot"]))
            continue
        if (ya < 0) != (yb < 0):
            xa, xb = a["spot"], b["spot"]
            t = ya / (ya - yb)   # linear interp
            out.append(round(xa + t * (xb - xa), 2))
    # Don't double-report the endpoint when ya was exactly zero AND the
    # next segment crosses immediately.
    return sorted(set(round(x, 2) for x in out))


def multileg_pop(curve: list[dict], *, S: float, T_years: float,
                 sigma: float, r: float = DEFAULT_RISK_FREE,
                 key: str = "expiry_value") -> float:
    """
    Probability that the strategy ends profitable AT EXPIRY under the
    Black-Scholes log-normal assumption. Walks the expiry curve, finds
    every contiguous segment where value > 0, and sums
    `prob_above(low) - prob_above(high)` for each. Open-ended segments
    (extending to ∞ or 0) use the analytical limits.
    """
    if not curve or T_years <= 0 or sigma <= 0:
        return 0.0
    # Build segments by sign. A single sweep — O(N).
    segs: list[tuple[float, float, bool]] = []   # (lo, hi, is_profit)
    cur_lo  = curve[0]["spot"]
    cur_pos = curve[0][key] > 0
    for i in range(1, len(curve)):
        a, b = curve[i - 1], curve[i]
        if (a[key] > 0) != (b[key] > 0):
            # Sign change — interpolate the crossing point.
            xa, xb = a["spot"], b["spot"]
            t = a[key] / (a[key] - b[key])
            cross = xa + t * (xb - xa)
            segs.append((cur_lo, cross, cur_pos))
            cur_lo, cur_pos = cross, b[key] > 0
    segs.append((cur_lo, curve[-1]["spot"], cur_pos))

    pop = 0.0
    first_spot = curve[0]["spot"]
    last_spot  = curve[-1]["spot"]
    for lo_s, hi_s, is_profit in segs:
        if not is_profit:
            continue
        # Treat the leftmost / rightmost segment as open-ended so the
        # operator's payoff curve doesn't artificially clip POP.
        lo_open = (abs(lo_s - first_spot) < 1e-6)
        hi_open = (abs(hi_s - last_spot)  < 1e-6)
        p_low  = prob_above(S, max(0.01, lo_s), T_years, r, sigma) if not lo_open else 1.0
        p_high = prob_above(S, max(0.01, hi_s), T_years, r, sigma) if not hi_open else 0.0
        pop += max(0.0, p_low - p_high)
    return min(1.0, max(0.0, pop))


def multileg_extremes(curve: list[dict], *, key: str = "expiry_value"
                      ) -> tuple[float, float]:
    """
    Numerical max profit / max loss off the expiry curve. NOTE: only as
    accurate as the curve's spot range — strategies with unbounded
    payoff (long call, short put) need the operator to widen the span
    or the route layer to flag those legs explicitly.
    """
    if not curve:
        return (0.0, 0.0)
    vals = [p[key] for p in curve]
    return (round(max(vals), 2), round(min(vals), 2))


# ── Expected value + position metrics ─────────────────────────────────

def expected_value(curve: list[dict], *, S: float, T_years: float,
                   sigma: float, r: float = DEFAULT_RISK_FREE,
                   key: str = "expiry_value") -> float:
    """
    E[payoff at expiry] computed by integrating `curve[*][key]` against
    the risk-neutral lognormal pdf of the underlying spot at expiry:

        f(S_T) = (1 / (S_T σ √(2πT))) ·
                 exp(-(ln(S_T/S) − (r − σ²/2)T)² / (2σ²T))

    Trapezoidal rule across the curve's spot grid. The curve typically
    spans ±2.5σ which captures ~99 % of the lognormal mass, so
    truncation error is sub-percent for any reasonable strategy.
    Returns the position-level ₹ expected value (signed-qty payoff
    already baked into the curve via payoff_curve / multileg_payoff_curve).
    """
    if not curve or len(curve) < 2 or T_years <= 0 or sigma <= 0 or S <= 0:
        return 0.0
    spots  = np.fromiter((p["spot"] for p in curve), dtype=np.float64, count=len(curve))
    values = np.fromiter((p[key]    for p in curve), dtype=np.float64, count=len(curve))
    # Risk-neutral lognormal PDF vectorized over the spot grid:
    #   f(S_T) = (1 / (S_T σ √(2πT))) · exp(-(ln(S_T/S) − (r − σ²/2)T)² / (2σ²T))
    sqrt_T = math.sqrt(T_years)
    mu     = math.log(S) + (r - sigma * sigma / 2.0) * T_years
    inv_2v = 1.0 / (2.0 * sigma * sigma * T_years)
    norm_k = 1.0 / (sigma * sqrt_T * math.sqrt(2.0 * math.pi))
    safe   = spots > 0
    z      = np.log(np.where(safe, spots, 1.0)) - mu
    pdf    = np.where(safe, (norm_k / np.where(safe, spots, 1.0))
                              * np.exp(-z * z * inv_2v), 0.0)
    integrand = values * pdf
    # Trapezoidal rule — NumPy 2.x renames trapz → trapezoid.
    integral = float(np.trapezoid(integrand, spots))
    return round(integral, 2)


def risk_reward_ratio(max_profit: float | None,
                      max_loss: float | None) -> float | None:
    """
    Risk-to-reward = max_profit / |max_loss|. Returns None for legs
    where the ratio isn't meaningful (one side is unbounded, or
    max_loss == 0). The route layer surfaces None as JSON null and the
    UI renders "∞" or "—".
    """
    if max_profit is None or max_loss is None:
        return None
    if max_loss == 0:
        return None
    return round(max_profit / abs(max_loss), 3)
