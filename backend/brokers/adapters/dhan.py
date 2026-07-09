"""
Dhan implementation of the `Broker` interface.

Production-wired adapter: orders, holdings, positions, margins, GTT
(Forever) order book, trades, basket margin. Built on the official
`dhanhq` Python SDK (PyPI: `dhanhq`). Auth + token refresh are managed
in `DhanConnection` (backend/brokers/connections.py).

Token refresh — fully headless. The earlier "paste from portal" path
was retired. Tokens are minted via a direct REST POST to
`https://auth.dhan.co/app/generateAccessToken` with `client_id + pin
+ TOTP code` (TOTP seed stored in `broker_accounts.totp_token_enc`).
`dhanhq.auth.DhanLogin` is bypassed because its module-level
`requests.post` calls don't accept a custom session — we need a
source-IP-bound `requests.Session` so Dhan's per-IP session affinity
(one active token per partner-app per source IP) doesn't invalidate
peer accounts. See `_DhanConnection._login_session()`.

IPv6 source-binding — every Dhan account loaded on the same server
binds to a dedicated IPv6 from the server's /48 subnet, same pattern
as Kite multi-account. Login session + runtime SDK session both mount
the source adapter; see CLAUDE.md "Multi-Account IPv6 Source
Binding". Without this, prod logs show a 3-min token rotation loop
between Dhan accounts.

Response normalisation — Dhan's REST responses use different field
names than Kite (e.g. `securityId` vs `instrument_token`,
`tradingSymbol` vs `tradingsymbol`). Every method maps Dhan's
response shape back to the Kite shape the rest of the codebase
consumes. Where a Dhan field has no Kite analogue it's carried
through under the Dhan name. F&O `tradingSymbol` ("CRUDEOIL-16JUL2026
-8500-CE") is canonicalised to Kite form ("CRUDEOIL26JUL8500CE") so
downstream parsers and the instruments cache resolve consistently.
"""

from __future__ import annotations

import threading
from typing import Any, Callable
from urllib.request import urlopen

from backend.brokers.base import Broker
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# ── Instruments cache ──────────────────────────────────────────────────
#
# Dhan publishes a master CSV at the URL below. We fetch it once per IST
# day (cache buster = today's date string) and build two lookup tables:
#   _DHAN_BY_EXCHANGE   — {kite_exchange: list[dict]}  (per-exchange list)
#   _DHAN_BY_SYMBOL     — {(kite_exchange, tradingsymbol): security_id}
# Both are wiped and rebuilt on the first call after midnight IST.
#
# The CSV download is done with stdlib `urllib.request` — no extra deps.
# On any network or parse failure the tables stay empty and callers
# see the "unknown tradingsymbol" error rather than a 500 trace.
#
# URL history:
#   v2 original: https://api.dhan.co/v2/instruments-detailed  (404 since ~Jun 2026)
#   v2 current:  https://images.dhan.co/api-data/api-scrip-master.csv
#
# Schema change (Jun 2026) vs the old /v2/instruments-detailed:
#   • SEM_EXM_EXCH_ID now carries "NSE" / "BSE" / "MCX" directly (was
#     "NSE_EQ" / "BSE_EQ" / "NSE_FNO" etc.).
#   • New SEM_SEGMENT column encodes the sub-segment:
#       D = derivatives (equity options/futures)
#       M = commodity derivatives (MCX options/futures on NSE columns too)
#       E = equity cash
#       C = currency derivatives
#       I = index
#   • Lot-size column renamed: SM_LOT_SIZE → SEM_LOT_UNITS.
#   Tick-size (SEM_TICK_SIZE) and trading-symbol (SEM_TRADING_SYMBOL)
#   column names are unchanged.

_DHAN_INSTRUMENTS_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

# Map (SEM_EXM_EXCH_ID, SEM_SEGMENT) → Kite-style exchange string.
# Used when building the cache so the rest of the codebase never sees
# Dhan's column values directly.
#
# SEM_SEGMENT codes observed in production CSV (Jun 2026):
#   D  = derivatives (equity F&O) on NSE / BSE
#   M  = commodity/MCX derivatives (NSE MCX options also land in NSE+M)
#   E  = equity cash
#   C  = currency derivatives
#   I  = index instruments
_DHAN_EXCH_SEG_TO_EXCHANGE: dict[tuple[str, str], str] = {
    ("NSE", "E"):  "NSE",     # equity cash
    ("BSE", "E"):  "BSE",     # equity cash
    ("NSE", "D"):  "NFO",     # equity derivatives (options + futures)
    ("BSE", "D"):  "BFO",     # BSE equity derivatives
    ("NSE", "M"):  "NFO",     # MCX-style derivatives listed on NSE segment
    ("MCX", "M"):  "MCX",     # commodity derivatives
    ("NSE", "C"):  "CDS",     # currency derivatives
    ("BSE", "C"):  "BCD",     # BSE currency derivatives
    ("NSE", "I"):  "NSE",     # index instruments — treat as NSE
    ("BSE", "I"):  "BSE",     # BSE index
}

# Legacy fallback: old CSV had SEM_EXM_EXCH_ID carrying segment-qualified
# strings. Kept so a future schema revert doesn't silently break things.
_DHAN_SEGMENT_TO_EXCHANGE: dict[str, str] = {
    "NSE_EQ":      "NSE",
    "BSE_EQ":      "BSE",
    "NSE_FNO":     "NFO",
    "BSE_FNO":     "BFO",
    "MCX_COMM":    "MCX",
    "NSE_CURRENCY":"CDS",
    "BSE_CURRENCY":"BCD",
    "IDX_I":       "NSE",
}

# Our exchange vocabulary → Dhan's segment codes for the market-status
# probe. ANY mapped segment reporting active means the exchange is
# open. Module-level constant (slice M6) — was previously a local
# inside `market_status()` and re-allocated per call.
_XCHG_TO_DHAN_MARKET_STATUS: dict[str, tuple[str, ...]] = {
    "NSE": ("NSE_EQ",),
    "BSE": ("BSE_EQ",),
    "NFO": ("NSE_FNO",),
    "BFO": ("BSE_FNO",),
    "CDS": ("NSE_CURRENCY",),
    "MCX": ("MCX_COMM",),
}

_DHAN_OPEN_STATUS_STRINGS = frozenset({"OPEN", "TRADING", "ACTIVE", "Y", "YES", "TRUE"})


def _extract_dhan_status_rows(resp: Any, target_codes: tuple[str, ...]) -> list[dict] | None:
    """Coerce Dhan's market-status response into a flat list of rows.

    Accepts the documented `{status, data: [{exchangeSegment, status, ...}]}`
    envelope AND the flat-dict-by-segment alternate observed across SDK
    builds. Returns None when the shape is unparseable so the caller
    can fall through to the next probe."""
    rows = _unwrap(resp)
    if rows:
        return rows
    if isinstance(resp, dict):
        for code in target_codes:
            v = resp.get(code) or resp.get(code.lower())
            if isinstance(v, dict):
                rows.append({"exchangeSegment": code, **v})
            elif isinstance(v, (str, bool)):
                rows.append({"exchangeSegment": code, "status": v})
        return rows
    return None


def _dhan_row_indicates_open(row: dict, target_codes: tuple[str, ...]) -> bool:
    """Return True when this row's segment matches AND its status
    reads as open (boolean True or one of the accepted strings)."""
    seg = str(row.get("exchangeSegment") or row.get("segment") or "").upper()
    if seg not in target_codes:
        return False
    st = row.get("status")
    if isinstance(st, bool):
        return st
    if isinstance(st, str):
        return st.upper() in _DHAN_OPEN_STATUS_STRINGS
    return False

_dhan_instruments_lock = threading.Lock()
_DHAN_INSTRUMENTS_DATE: str = ""            # IST date string when cache was built
_DHAN_BY_EXCHANGE: dict[str, list[dict]] = {}   # kite_exchange → [instrument rows]
_DHAN_BY_SYMBOL: dict[tuple, str] = {}          # (kite_exchange, tradingsymbol) → security_id


def _ist_today() -> str:
    """Return today's IST date as 'YYYY-MM-DD' (used as cache buster)."""
    from datetime import datetime, timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%Y-%m-%d")


import re as _re

# Dhan F&O tradingsymbol format:
#
# Old format (api.dhan.co/v2/instruments-detailed, pre-Jun 2026):
#   Options:  ROOT-DDmonYYYY-STRIKE-CE|PE   e.g. "CRUDEOIL-16JUL2026-8500-CE"
#   Futures:  ROOT-DDmonYYYY-FUT            e.g. "CRUDEOIL-19JUN2026-FUT"
#
# New format (images.dhan.co/api-data/api-scrip-master.csv, Jun 2026+):
#   Options:  ROOT-MonYYYY-STRIKE-CE|PE     e.g. "CGPOWER-Jun2026-840-PE"
#             (no DD day prefix for equity options)
#   Futures:  ROOT-DDMonYYYY-FUT            e.g. "SILVER-03Jul2026-FUT"
#             (day prefix retained for futures)
#
# Kite F&O tradingsymbol format (what every downstream parser expects):
#   Options:  ROOTYYmmmSTRIKECE|PE          e.g. "CRUDEOIL26JUL8500CE"
#   Futures:  ROOTYYmmmFUT                  e.g. "CRUDEOIL26JULFUT"
#
# Without translation, decomposeSymbol / parse_tradingsymbol / the
# instruments-cache lookup all reject Dhan-format symbols and the
# /admin/derivatives page shows "isn't a recognised option or
# futures contract" above the Legs grid, killing the payoff chart.
#
# The regexes below handle BOTH old (with DD) and new (without DD) formats
# for options; futures still carry the day number in the new CSV.
#   _DHAN_OPT_RE_DAY  — "ROOT-DDMonYYYY-STRIKE-CE|PE" (old + some new)
#   _DHAN_OPT_RE_NDAY — "ROOT-MonYYYY-STRIKE-CE|PE"   (new equity options)
#   _DHAN_FUT_RE      — "ROOT-DDMonYYYY-FUT"           (both old and new)
_DHAN_OPT_RE = _re.compile(
    r"^([A-Z]+)-(\d{1,2})([A-Z]{3})(\d{4})-(\d+(?:\.\d+)?)-(CE|PE)$"
)
_DHAN_OPT_RE_NDAY = _re.compile(
    r"^([A-Z]+)-([A-Z]{3})(\d{4})-(\d+(?:\.\d+)?)-(CE|PE)$"
)
_DHAN_FUT_RE = _re.compile(r"^([A-Z]+)-(\d{1,2})([A-Z]{3})(\d{4})-FUT$")
# Futures without day prefix (defensive; not observed but safe to handle)
_DHAN_FUT_RE_NDAY = _re.compile(r"^([A-Z]+)-([A-Z]{3})(\d{4})-FUT$")


def _dhan_to_kite_symbol(raw: str) -> str:
    """Convert a Dhan F&O tradingsymbol to the Kite-style canonical form.

    Handles both the old format (DD prefix, from /v2/instruments-detailed) and
    the new format (no DD prefix on equity options, from /api-scrip-master.csv).
    Equity / index / unknown shapes fall through unchanged with dashes
    + spaces stripped — same conservative fallback the rest of the
    codebase already uses for non-derivative symbols.
    """
    s = (raw or "").upper().strip()
    if not s:
        return ""
    # Options — old format "ROOT-DDMonYYYY-STRIKE-CE|PE"
    m = _DHAN_OPT_RE.match(s)
    if m:
        root, _dd, mon, yyyy, strike, opt_type = m.groups()
        # Drop trailing .0 on whole-number strikes; preserve halves.
        try:
            strike_f = float(strike)
            strike_disp = (str(int(strike_f)) if strike_f.is_integer()
                           else str(strike_f))
        except ValueError:
            strike_disp = strike
        return f"{root}{yyyy[2:]}{mon}{strike_disp}{opt_type}"
    # Options — new format "ROOT-MonYYYY-STRIKE-CE|PE" (no day prefix)
    m = _DHAN_OPT_RE_NDAY.match(s)
    if m:
        root, mon, yyyy, strike, opt_type = m.groups()
        try:
            strike_f = float(strike)
            strike_disp = (str(int(strike_f)) if strike_f.is_integer()
                           else str(strike_f))
        except ValueError:
            strike_disp = strike
        return f"{root}{yyyy[2:]}{mon}{strike_disp}{opt_type}"
    # Futures — with day prefix "ROOT-DDMonYYYY-FUT"
    m = _DHAN_FUT_RE.match(s)
    if m:
        root, _dd, mon, yyyy = m.groups()
        return f"{root}{yyyy[2:]}{mon}FUT"
    # Futures — without day prefix "ROOT-MonYYYY-FUT" (defensive)
    m = _DHAN_FUT_RE_NDAY.match(s)
    if m:
        root, mon, yyyy = m.groups()
        return f"{root}{yyyy[2:]}{mon}FUT"
    # Fallback: just strip dashes + spaces. Equity / index symbols
    # ("RELIANCE", "NIFTY 50") and any Dhan format the regex doesn't
    # cover yet pass through cleanly.
    return s.replace("-", "").replace(" ", "").strip()


def _parse_dhan_csv_header(lines: list[str]) -> tuple[dict[str, int], bool] | None:
    """Parse the CSV header row and return (col_index, has_seg_col).

    Returns None when required columns are missing (caller aborts cache load).
    `has_seg_col` distinguishes the new schema (SEM_SEGMENT present) from the
    legacy schema (segment code baked into SEM_EXM_EXCH_ID)."""
    header = [h.strip() for h in lines[0].split(",")]
    col = {name: idx for idx, name in enumerate(header)}
    required = {"SEM_SMST_SECURITY_ID", "SEM_TRADING_SYMBOL", "SEM_EXM_EXCH_ID"}
    if not required.issubset(col):
        logger.warning(f"DhanBroker: instruments CSV missing columns "
                       f"{required - set(col)}; cache aborted")
        return None
    return col, "SEM_SEGMENT" in col


def _resolve_dhan_kite_exchange(
    parts: list[str], col: dict[str, int], has_seg_col: bool,
) -> tuple[str | None, str]:
    """Map (SEM_EXM_EXCH_ID [, SEM_SEGMENT]) → Kite exchange string.

    Returns (kite_exch, seg_raw). `kite_exch` is None when the row's
    segment can't be mapped (caller should skip the row)."""
    exch_raw = parts[col["SEM_EXM_EXCH_ID"]].strip()
    if has_seg_col and len(parts) > col["SEM_SEGMENT"]:
        seg_raw = parts[col["SEM_SEGMENT"]].strip()
        kite_exch = _DHAN_EXCH_SEG_TO_EXCHANGE.get((exch_raw, seg_raw))
    else:
        # Old schema: segment code is in SEM_EXM_EXCH_ID itself.
        kite_exch = _DHAN_SEGMENT_TO_EXCHANGE.get(exch_raw)
        seg_raw = exch_raw  # for the row dict below
    return kite_exch, seg_raw


def _extract_dhan_lot_size(parts: list[str], col: dict[str, int]) -> int:
    """Probe legacy + new column names for lot size.

    Lot-size column: SEM_LOT_UNITS (new schema, Jun 2026) or
    SM_LOT_SIZE / SEM_LOT_SIZE (old schema). Try new name first, then
    legacy names. A schema rev that changes the column name would
    otherwise silently zero every lot_size — triggering the MCX qty
    mismatch class (Sprint D)."""
    for lot_col in ("SEM_LOT_UNITS", "SM_LOT_SIZE", "SEM_LOT_SIZE", "LOT_SIZE"):
        if lot_col in col and len(parts) > col[lot_col]:
            try:
                lot_size = int(float(parts[col[lot_col]].strip() or 0))
                if lot_size > 0:
                    return lot_size
            except (ValueError, TypeError):
                pass
    return 0


def _extract_dhan_tick_size(parts: list[str], col: dict[str, int]) -> float:
    """Parse SEM_TICK_SIZE when present, else 0.0."""
    if "SEM_TICK_SIZE" in col and len(parts) > col["SEM_TICK_SIZE"]:
        try:
            return float(parts[col["SEM_TICK_SIZE"]].strip() or 0)
        except (ValueError, TypeError):
            pass
    return 0.0


def _dhan_instrument_token(sid: str) -> int:
    """`instrument_token` is the Kite-shape key every downstream
    consumer reads (options.py historical-data caller's token_map,
    kite.py::get_lot_size's _LOT_INDEX, …). Dhan's native identifier
    is the `security_id` string; we expose BOTH so callers that
    already speak `security_id` keep working AND Kite-shape callers
    don't silently skip Dhan rows. Cast to int when numeric (Dhan IDs
    are always numeric strings) and fall back to 0 when not — same
    convention as `_normalise_holdings`."""
    try:
        return int(sid) if sid and str(sid).isdigit() else 0
    except (TypeError, ValueError):
        return 0


def _load_dhan_instruments() -> None:
    """Fetch Dhan's master CSV and populate the module-level caches.
    Called under _dhan_instruments_lock. Silently no-ops on any failure
    so a network blip doesn't crash the broker registry.

    The public CSV URL changed in Jun 2026 from
      api.dhan.co/v2/instruments-detailed  → (HTTP 404)
    to
      images.dhan.co/api-data/api-scrip-master.csv

    Schema changes in the new URL (vs old /v2/instruments-detailed):
      • SEM_EXM_EXCH_ID now carries "NSE" / "BSE" / "MCX" directly
        instead of "NSE_EQ" / "NSE_FNO" etc. Exchange-to-Kite mapping
        now requires the companion SEM_SEGMENT column ("D", "M", "E",
        "C", "I") to determine the Kite exchange string.
      • Lot-size column renamed: SM_LOT_SIZE → SEM_LOT_UNITS.
      Both old and new column names are probed so a future schema revert
      doesn't silently zero lot sizes.
    """
    global _DHAN_INSTRUMENTS_DATE, _DHAN_BY_EXCHANGE, _DHAN_BY_SYMBOL
    by_exchange: dict[str, list[dict]] = {}
    by_symbol: dict[tuple, str] = {}
    try:
        with urlopen(_DHAN_INSTRUMENTS_URL, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        lines = raw.splitlines()
        if not lines:
            logger.warning("DhanBroker: instruments CSV empty")
            return
        parsed = _parse_dhan_csv_header(lines)
        if parsed is None:
            return
        col, has_seg_col = parsed
        min_col = max(col.get("SEM_SMST_SECURITY_ID", 0),
                      col.get("SEM_TRADING_SYMBOL", 0),
                      col.get("SEM_EXM_EXCH_ID", 0))

        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) <= min_col:
                continue
            kite_exch, seg_raw = _resolve_dhan_kite_exchange(parts, col, has_seg_col)
            if not kite_exch:
                continue
            # Translate Dhan's F&O tradingsymbol to the Kite-style canonical
            # form. Dhan ships symbols as "CRUDEOIL-16JUL2026-8500-CE"
            # (ROOT-DDmonYYYY-STRIKE-CE|PE); the Kite parser expects
            # "CRUDEOIL26JUL8500CE" (ROOTYYmmmSTRIKECE). Without this
            # the security_id lookup misses, and the strategy-analytics
            # endpoint rejects the leg with "isn't a recognised option
            # or futures contract" — payoff chart never renders. Equity
            # / index symbols pass through (just strip dashes + spaces).
            ts_raw = parts[col["SEM_TRADING_SYMBOL"]].strip()
            ts = _dhan_to_kite_symbol(ts_raw)
            sid = parts[col["SEM_SMST_SECURITY_ID"]].strip()
            if not ts or not sid:
                continue
            row = {
                "tradingsymbol":    ts,
                "security_id":      sid,
                "instrument_token": _dhan_instrument_token(sid),
                "exchange":         kite_exch,
                "exchange_segment": seg_raw,
                "lot_size":         _extract_dhan_lot_size(parts, col),
                "tick_size":        _extract_dhan_tick_size(parts, col),
            }
            by_exchange.setdefault(kite_exch, []).append(row)
            by_symbol[(kite_exch, ts)] = sid
        _DHAN_BY_EXCHANGE = by_exchange
        _DHAN_BY_SYMBOL = by_symbol
        _DHAN_INSTRUMENTS_DATE = _ist_today()
        total = sum(len(v) for v in by_exchange.values())
        logger.info(f"DhanBroker: instruments cache loaded — {total} rows "
                    f"across {len(by_exchange)} exchanges "
                    f"({'new' if has_seg_col else 'legacy'} schema)")
    except Exception as e:
        logger.warning(f"DhanBroker: instruments cache load failed: {e}")


def _ensure_dhan_instruments() -> None:
    """Ensure the instruments cache is warm for today's IST date."""
    with _dhan_instruments_lock:
        if _DHAN_INSTRUMENTS_DATE != _ist_today():
            _load_dhan_instruments()


def _resolve_security_id(tradingsymbol: str, kite_exchange: str) -> str:
    """Return the Dhan security_id for a tradingsymbol + Kite exchange.

    Loads the instruments cache lazily (once per IST day). Returns an
    empty string when not found — callers should raise a meaningful
    error rather than passing an empty string to Dhan (which would
    return an opaque 'Invalid security_id' rejection)."""
    _ensure_dhan_instruments()
    return _DHAN_BY_SYMBOL.get((kite_exchange, tradingsymbol), "")


# ── Auth-retry plumbing ──────────────────────────────────────────────
#
# Dhan's SDK doesn't raise on auth failure — it returns a dict
# `{"status": "failure", "remarks": "Invalid access token", ...}`
# instead. To match the "cache → use → re-mint on failure" lifecycle
# Kite (@retry_kite_conn) and Groww (@_retry_groww_auth) already use,
# every broker method that touches the SDK runs its call through
# DhanBroker._safe_call(...).
#
# _safe_call passes the live SDK handle into the operator's lambda
# and inspects the raw response BEFORE normalisation. If the response
# carries an auth-error shape (status=failure + auth-keyword remarks),
# it forces a re-login via get_dhan_conn(test_conn=True) — which
# re-runs _do_login() with the stored PIN + TOTP seed — and retries
# once with the new SDK handle. If the account isn't configured for
# headless re-login, _do_login raises and the original auth-failure
# response propagates to the caller unchanged.
_AUTH_ERROR_HINTS = (
    "invalid access token",
    "invalid token",   # ← primary signal — Dhan's DH-906 always
                       #   carries `error_message: 'Invalid Token'`
                       #   in the remarks dict, which str() renders
                       #   as "...invalid token..." after lowering.
    "token expired",
    "unauthorized",
    "unauthorised",
    "auth failed",
    "401",
    "dh-901",   # Dhan: Invalid Authentication (rare; reported only
                # for a fresh credential rejected by the auth backend
                # rather than an in-session token going stale).
    "dh-906",   # Dhan: Invalid Token — the one the rotation pattern
                # surfaces. Confirmed against prod logs; the earlier
                # "dh-905" entry was a doc-drift typo. Kept "dh-906"
                # as defence-in-depth against the SDK ever surfacing
                # the code without the matching "invalid token" text.
)


def _looks_like_auth_failure(resp: Any) -> bool:
    """True when a Dhan SDK response carries an auth-error signal."""
    if not isinstance(resp, dict):
        return False
    status = str(resp.get("status", "")).lower()
    if status != "failure":
        return False
    remarks = str(resp.get("remarks", "")).lower()
    return any(hint in remarks for hint in _AUTH_ERROR_HINTS)


# Module-level ledger of recent Dhan account login + invalidation
# events. Lets `_check_dhan_rotation_pattern` detect the "every time
# one Dhan account logs in, the other's token immediately dies"
# pattern — a strong signal that Dhan is enforcing one active session
# per partner app per source IP. The cure is per-account IPv6 source
# binding (same shape we use for Kite multi-account); the diagnostic
# is needed first to confirm the cause.
import threading as _dhan_threading

_DHAN_LOGIN_HISTORY: dict[str, "datetime"] = {}
_DHAN_HISTORY_LOCK = _dhan_threading.Lock()


def record_dhan_login_event(account: str) -> None:
    """Stamp `now` against this Dhan account's last successful login.
    Called from DhanConnection at the moment a fresh token is minted
    + saved. Pure side-channel; no functional change to the auth
    flow."""
    from datetime import datetime as _dt, timezone as _tz
    with _DHAN_HISTORY_LOCK:
        _DHAN_LOGIN_HISTORY[account] = _dt.now(_tz.utc)


def _check_dhan_rotation_pattern(
    failing_account: str,
    failing_token_created_at,
) -> None:
    """Emit a diagnostic ERROR log when the timing of THIS account's
    token going bad matches a recent successful login from a DIFFERENT
    Dhan account. Threshold: any other account's login complete within
    the failing token's entire lifetime — if A's token was minted at
    T0 and went bad now, and B logged in at T_B with T0 ≤ T_B ≤ now,
    the rotation pattern fires.

    The pattern points at one of two operator-actionable causes:
      (a) Dhan's "one active token per partner-app per source-IP"
          semantic — fix by setting a distinct `source_ip` on each
          Dhan broker_account row (the runtime SDK session + the
          login session both mount the IPv6 adapter when source_ip
          is configured; see DhanConnection._mount_source_ip_adapter
          and ._login_session()). If both Dhan rows already carry
          dedicated IPv6 addresses, this isn't the cause — fall to (b).
      (b) A single physical Dhan account split across two RamboQuant
          records — fix by removing one record.

    Operator should check the Dhan dashboard's
    Settings → DhanHQ Trading APIs → Token validity dropdown for
    cause (c) — if it's set to 5 min, that explains short-lived
    tokens but NOT cross-account rotation.
    """
    if failing_token_created_at is None:
        return
    from datetime import datetime as _dt, timezone as _tz
    now = _dt.now(_tz.utc)
    with _DHAN_HISTORY_LOCK:
        snapshot = dict(_DHAN_LOGIN_HISTORY)
    suspects: list[tuple[str, float]] = []
    for other_account, other_login_at in snapshot.items():
        if other_account == failing_account:
            continue
        # Only the OTHER account's login matters if it happened during
        # OUR token's lifetime — that's when it could have invalidated us.
        if failing_token_created_at <= other_login_at <= now:
            gap_s = (now - other_login_at).total_seconds()
            suspects.append((other_account, gap_s))
    if suspects:
        suspects.sort(key=lambda x: x[1])  # nearest in time first
        details = ", ".join(
            f"{acct} logged in {gap:.0f}s ago" for acct, gap in suspects
        )
        logger.error(
            f"Dhan rotation pattern detected: {failing_account!r}'s "
            f"token went bad after another Dhan account's recent login "
            f"({details}). Likely Dhan's one-active-session-per-IP "
            f"limit. Mitigation: bind each Dhan account to its own "
            f"IPv6 (set source_ip in /admin/brokers — same pattern as "
            f"Kite multi-account) OR verify both records aren't the "
            f"same physical Dhan account."
        )


# Dhan exchange-segment constants. The SDK uses opaque integer codes;
# we accept the Kite-style string ("NSE", "NFO", "MCX", ...) at the
# Broker boundary and translate here. Kite's "BSE" and "BFO" map to
# Dhan's BSE_EQ / BSE_FNO. CDS / BCD don't have direct Dhan counterparts
# (currency derivatives) — left out until needed.
_EXCHANGE_TO_DHAN: dict[str, str] = {
    "NSE": "NSE_EQ",
    "BSE": "BSE_EQ",
    "NFO": "NSE_FNO",
    "BFO": "BSE_FNO",
    "MCX": "MCX_COMM",
}

# Kite transaction_type ("BUY" / "SELL") is identical to Dhan; no map needed.
# Kite product ("CNC" / "MIS" / "NRML") → Dhan product type.
_PRODUCT_TO_DHAN: dict[str, str] = {
    "CNC":  "CNC",     # Cash and carry (delivery)
    "MIS":  "INTRADAY",
    "NRML": "MARGIN",  # F&O carry-forward
}

# Kite order_type → Dhan order_type. Same strings.
_ORDER_TYPE_TO_DHAN: dict[str, str] = {
    "MARKET": "MARKET",
    "LIMIT":  "LIMIT",
    "SL":     "STOP_LOSS",
    "SL-M":   "STOP_LOSS_MARKET",
}


def _dhan_exchange(kite_exchange: str) -> str:
    """Translate a Kite-style exchange string to Dhan's exchange-segment."""
    seg = _EXCHANGE_TO_DHAN.get(kite_exchange)
    if not seg:
        raise ValueError(f"No Dhan exchange-segment mapping for {kite_exchange!r}")
    return seg


class DhanBroker(Broker):
    """Dhan adapter. See module docstring for the auth + normalisation
    contract."""

    def __init__(self, conn: "DhanConnection") -> None:  # type: ignore[name-defined]
        self._conn = conn

    # ── Identity + escape hatch ───────────────────────────────────────

    @property
    def account(self) -> str:
        return self._conn.account

    @property
    def broker_id(self) -> str:
        return "dhan"

    @property
    def dhan(self):
        """Underlying `dhanhq` SDK handle. Re-validates the access token
        on every access (DhanConnection re-mints when expired). Escape
        hatch for SDK features not lifted into the Broker ABC."""
        return self._conn.get_dhan_conn()

    def _safe_call(self, sdk_call: Callable[[Any], Any]) -> Any:
        """Invoke an SDK call with auto re-login on auth failure.

        `sdk_call` is a one-arg lambda receiving the live SDK handle —
        e.g. `lambda d: d.get_holdings()`. If the raw response carries
        an auth-failure shape, we evict the cached token (via
        get_dhan_conn(test_conn=True)) and retry once with the freshly
        minted SDK handle. Network / 5xx / param exceptions propagate
        immediately — only auth-shaped failures trigger the retry."""
        resp = sdk_call(self.dhan)
        if _looks_like_auth_failure(resp):
            # Capture token age at invalidation time — critical signal
            # for "why are these tokens dying so fast?" investigations.
            # If the operator's Dhan dashboard has token validity set
            # to 5 min, every token dies at ~5 min. If the dashboard
            # is set to 24 h but tokens die in 3 min, something else
            # is invalidating them (probably another Dhan account
            # logging in from the same source IP — Dhan's "one active
            # session per partner-app per IP" semantic). Cross-check
            # against `Dhan rotation` log line below to confirm.
            from datetime import datetime as _dt, timezone as _tz
            created = self._conn._conn_created_at
            age_s = "unknown"
            if created is not None:
                age = _dt.now(_tz.utc) - created
                age_s = f"{age.total_seconds():.0f}s"
            logger.warning(
                f"DhanBroker for {self.account!r} got auth failure "
                f"(remarks={resp.get('remarks')!r}, token_age={age_s}). "
                f"Forcing re-login via PIN+TOTP and retrying once."
            )
            # Cross-account rotation signal — if another Dhan account
            # logged in within the recent past (≤ this token's lifetime),
            # the timing strongly suggests Dhan invalidated THIS token
            # when the other account's session opened. Surfacing this
            # so the operator can confirm via the `dhan_tokens.json`
            # cache mtime + `Dhan login complete for ...` events.
            _check_dhan_rotation_pattern(self.account, created)
            fresh = self._conn.get_dhan_conn(test_conn=True)
            resp = sdk_call(fresh)
        # D2 — persistent-auth-failure guard: if the retry still
        # returns an auth-failure dict, raise so _record_fetch(ok=False)
        # fires in the per-account broker wrappers (fetch_holdings /
        # fetch_positions / fetch_margins). Without this, _unwrap() on
        # the auth-failure dict produces an empty list, the route returns
        # empty panels, and the navbar badge never learns the account is
        # unhealthy — it stays "5/5" instead of reflecting the real state.
        if _looks_like_auth_failure(resp):
            remarks = resp.get("remarks", "auth failure")
            raise RuntimeError(
                f"Dhan auth failure for {self.account!r} persisted after re-login: "
                f"{remarks!r}"
            )
        return resp

    # ── Account state ─────────────────────────────────────────────────

    def profile(self) -> dict:
        """Dhan exposes `get_fund_limits()` as the lightest auth-check
        call; there's no profile() equivalent that returns a user_name.
        Synthesise a Kite-shape dict so the /admin/brokers test button
        gets a recognisable success message."""
        try:
            funds = self._safe_call(lambda d: d.get_fund_limits())
            data = funds.get("data") if isinstance(funds, dict) else None
            return {
                "user_id":   self._conn.client_id,
                "user_name": f"Dhan {self._conn.client_id}",
                "broker":    "DHAN",
                "data":      data,
            }
        except Exception as e:
            raise RuntimeError(f"Dhan auth check failed: {e}") from e

    def holdings(self) -> list[dict]:
        resp = self._safe_call(lambda d: d.get_holdings())
        return _normalise_holdings(resp)

    def positions(self) -> dict:
        resp = self._safe_call(lambda d: d.get_positions())
        return _normalise_positions(resp)

    def margins(self, segment: str | None = None) -> dict:
        resp = self._safe_call(lambda d: d.get_fund_limits())
        # Audit cycle 8 — log the raw Dhan fund_limits response ONCE per
        # account so we can confirm which fields actually arrive in prod
        # (Dhan v2 documentation is incomplete on the realized-P&L +
        # option-premium fields the normaliser optimistically reads).
        global _DHAN_MARGINS_LOGGED
        try:
            if self.account not in _DHAN_MARGINS_LOGGED:
                _DHAN_MARGINS_LOGGED.add(self.account)
                _raw = resp.get("data") if isinstance(resp, dict) else resp
                logger.info(
                    f"Dhan margins[{self.account}] raw response keys: "
                    f"{sorted((_raw or {}).keys()) if isinstance(_raw, dict) else type(_raw).__name__}"
                )
        except Exception:
            pass
        return _normalise_margins(resp, segment)

    def orders(self) -> list[dict]:
        resp = self._safe_call(lambda d: d.get_order_list())
        return _normalise_orders(resp)

    def order_status(self, order_id: str) -> dict:
        """Audit fix (M-1) — per-id status endpoint. Pre-fix this fell
        back to the ABC default (filter `orders()`) which fetched the
        entire day book on every chase tick. With 10 open Dhan orders
        the 20-s poll cycle generated 10 full-book fetches → wasted
        bandwidth + risked hitting Dhan's 20 orders/sec rate limit.

        Uses Dhan SDK's `get_order_by_id` (the targeted single-order
        endpoint) when available. Falls back to the ABC default only
        if the SDK doesn't expose it (older versions) so the chase
        loop keeps working through SDK drift.

        Returns the matched order dict in Kite-shape via the existing
        `_normalise_orders` envelope, or {} on miss (matches ABC
        contract)."""
        sdk = self.dhan
        # Prefer the most-specific Dhan SDK method when present.
        # `get_order_by_id` is the canonical name in v2; some forks use
        # `get_order_status`. Both return the same Dhan envelope.
        single_fn = (getattr(sdk, "get_order_by_id", None)
                     or getattr(sdk, "get_order_status", None))
        if single_fn is None:
            # SDK version doesn't expose a per-id endpoint — fall back
            # to the ABC default (full-book filter) so behaviour stays
            # correct even if accuracy drops.
            return super().order_status(order_id)
        try:
            resp = self._safe_call(lambda d: single_fn(str(order_id)))
        except Exception as e:
            logger.debug(f"DhanBroker.order_status({order_id}) failed: {e}")
            return {}
        # The single-order endpoint wraps a single row, not a list. The
        # existing _normalise_orders helper handles both shapes via
        # _unwrap (single dict → list of one).
        rows = _normalise_orders(resp)
        return rows[0] if rows else {}

    def trades(self) -> list[dict]:
        resp = self._safe_call(lambda d: d.get_trade_book())
        return _normalise_trades(resp)

    def funds_ledger(self, from_date: str, to_date: str) -> list[dict]:
        """Pull Dhan's funds-ledger statement for a date range.
        Returns a list of normalised per-(date, segment) summary rows
        ready for upsert into `daily_book[kind='funds']`:

            [
              {
                "date":            date,
                "segment":         "equity" | "commodity" | ...,
                "cash_available":  float,   # EOD running balance
                "opening_balance": float,   # SOD running balance (best-effort)
                "debits":          float,   # Σ debit on this day+segment
                "realised_m2m":    float,   # net daily move (credit − debit)
                "net":             float,   # same as cash_available
                "payload":         dict,    # raw Dhan entries for forensics
              },
              ...
            ]

        The Dhan SDK exposes this as `get_ledger_report(from_date,
        to_date)` returning the standard `{status, data: [entry, …]}`
        envelope. Each entry is a voucher-level row:

            {
              "voucherdate": "DD/MM/YYYY" or "YYYY-MM-DD",
              "exchange":    "NSE_EQ" | "NSE_FNO" | "MCX_COMM" | ...,
              "debit":       "0.00",      # str — converted to float
              "credit":      "1234.56",   # str — converted to float
              "runbal":      "100000.00", # str — EOD running balance
              "narration":   "Day MTM Settlement Charges",
              "voucherdesc": "...",
              "vouchernumber": "..."
            }

        We aggregate per `(voucherdate, segment)` so the output maps
        cleanly onto the `daily_book` unique constraint
        `(date, account, kind, symbol)`. Multiple Dhan exchange codes
        collapse to two segments here (equity / commodity) to match
        the existing snapshot pipeline's shape.

        Method-name discovery — `get_ledger_report` is the v2 SDK
        name. We log at DEBUG when missing so the operator gets a
        clear diagnostic if a future SDK version renames it.
        (Slice P5: dropped the vestigial `get_funds_ledger` /
        `ledger_report` probes — neither name has shipped in any
        installed dhanhq build; they were defensive probes for a
        hypothetical fork that doesn't exist.)
        """
        from datetime import date as _date

        sdk = self.dhan
        ledger_method_name = "get_ledger_report"
        if getattr(sdk, ledger_method_name, None) is None:
            logger.warning(
                "DhanBroker.funds_ledger: SDK is missing "
                f"`{ledger_method_name}` — returning []"
            )
            return []

        try:
            resp = self._safe_call(
                lambda d: getattr(d, ledger_method_name)(
                    from_date=from_date, to_date=to_date
                )
            )
        except TypeError:
            # SDK signature may be positional rather than kwarg.
            try:
                resp = self._safe_call(
                    lambda d: getattr(d, ledger_method_name)(from_date, to_date)
                )
            except Exception as e:
                logger.warning(f"DhanBroker.funds_ledger SDK call failed: {e}")
                return []
        except Exception as e:
            logger.warning(f"DhanBroker.funds_ledger SDK call failed: {e}")
            return []

        entries = _unwrap(resp)
        if not entries:
            return []

        # Group by (voucherdate, segment). Each segment bucket collects
        # debits / credits + tracks first + last runbal as a proxy for
        # SOD and EOD cash. Dhan returns entries in chronological order
        # within a day so first/last == open/close.
        from collections import defaultdict
        groups: dict[tuple[_date, str], dict] = defaultdict(lambda: {
            "debits": 0.0, "credits": 0.0,
            "open_runbal": None, "close_runbal": None,
            "raw": [],
        })
        for e in entries:
            d = _parse_dhan_date(e.get("voucherdate"))
            if d is None:
                continue
            seg = _dhan_exchange_to_segment(e.get("exchange") or "")
            key = (d, seg)
            try:
                debit  = float(e.get("debit")  or 0)
                credit = float(e.get("credit") or 0)
                runbal = float(e.get("runbal") or 0)
            except (TypeError, ValueError):
                continue
            g = groups[key]
            g["debits"]  += debit
            g["credits"] += credit
            if g["open_runbal"] is None:
                g["open_runbal"] = runbal
            g["close_runbal"] = runbal
            g["raw"].append(e)

        out: list[dict] = []
        for (d, seg), g in groups.items():
            close_bal = g["close_runbal"]
            open_bal  = g["open_runbal"]
            # M2M proxy: net daily move = credits − debits. NOT just
            # mark-to-market; includes brokerage / STT / DP charges /
            # etc. Documented so the operator reads it as 'net daily
            # cash flow' rather than 'realised P&L'.
            net_move = g["credits"] - g["debits"]
            out.append({
                "date":            d,
                "segment":         seg,
                "cash_available":  close_bal,
                "opening_balance": (close_bal - net_move
                                    if close_bal is not None else open_bal),
                "debits":          g["debits"],
                "realised_m2m":    net_move,
                "net":             close_bal,
                "payload":         {"entries": g["raw"]},
            })

        # Newest first matches the daily_book ordering convention.
        out.sort(key=lambda r: r["date"], reverse=True)
        return out

    # ── Market data ───────────────────────────────────────────────────

    def ltp(self, symbols: list[str]) -> dict:
        """Audit fix (B-2) — was returning {} by design which silently
        broke `_task_trail_stop` for every Dhan trailing position (the
        poller reads ltp <= 0 and skips). Now resolves each quote key
        through the instruments cache and batches per Dhan exchange
        segment via `dhan.ohlc_data()`. Returns Kite-shape map keyed
        by the original quote string: {"NSE:RELIANCE": {"last_price":
        2500.0}, ...}. Symbols that can't be resolved are silently
        dropped (Kite behaviour) so the trail-stop loop's
        `ltp_map.get(key, 0)` fallback works."""
        if not symbols:
            return {}
        # Parse "EXCHANGE:TRADINGSYMBOL" keys + resolve security_ids.
        # Dhan's quote APIs accept {seg: [sid, sid, ...]} so we batch.
        # Index-name forms ("NSE:NIFTY 50") need NSE_INDEX segment in
        # Dhan; the instruments cache already encodes that mapping
        # for indexes the operator's templates actually quote against.
        try:
            _ensure_dhan_instruments()
        except Exception as _inst_err:
            # Network failure on first hit — fall back to empty so
            # PriceBroker walks the chain. Same conservative bias as
            # the historical_data and instruments paths.
            logger.warning(
                f"DhanBroker.ltp: instruments cache unavailable — "
                f"returning {{}} for {len(symbols)} symbol(s): {_inst_err}"
            )
            return {}
        seg_to_sids: dict[str, list[str]] = {}
        # Reverse map: (seg, sid) → original quote key so the response
        # round-trips into the Kite-style dict the caller expects.
        sid_to_key: dict[tuple[str, str], str] = {}
        for key in symbols:
            if ":" not in str(key):
                continue
            ex_kite, ts = str(key).split(":", 1)
            ts = ts.strip().upper()
            ex_kite = ex_kite.strip().upper()
            sid = _resolve_security_id(ts, ex_kite)
            if not sid:
                continue
            seg = _EXCHANGE_TO_DHAN.get(ex_kite)
            if not seg:
                continue
            seg_to_sids.setdefault(seg, []).append(sid)
            sid_to_key[(seg, sid)] = key
        if not seg_to_sids:
            return {}
        # Single SDK call covers every segment in one batch. Dhan's
        # response shape: {"data": {"NSE_EQ": {"<sid>": {"last_price":
        # 2500.0, ...}}, ...}}. The SDK normalizes the wrapper but the
        # per-row dict is what we need.
        try:
            resp = self._safe_call(lambda d: d.ohlc_data(securities=seg_to_sids))
        except Exception as e:
            logger.debug(f"DhanBroker.ltp ohlc_data failed: {e}")
            return {}
        out: dict = {}
        # Unwrap the outer envelope (status / data / remarks).
        data = resp.get("data") if isinstance(resp, dict) else None
        if not isinstance(data, dict):
            logger.warning(
                f"DhanBroker.ltp: ohlc_data returned unexpected shape "
                f"(got {type(data).__name__}) for {len(symbols)} symbol(s) — returning {{}}"
            )
            return {}
        for seg, by_sid in data.items():
            if not isinstance(by_sid, dict):
                continue
            for sid, row in by_sid.items():
                key = sid_to_key.get((str(seg), str(sid)))
                if not key:
                    continue
                # Dhan returns last_price for OHLC rows. Fall back to
                # ohlc.close → close → 0 to be defensive against SDK
                # version drift.
                lp = 0.0
                if isinstance(row, dict):
                    lp = float(row.get("last_price")
                               or (row.get("ohlc") or {}).get("close")
                               or row.get("close")
                               or 0)
                if lp > 0:
                    out[key] = {"last_price": lp, "instrument_token": sid}
        return out

    def quote(self, symbols: list[str]) -> dict:
        """Empty dict — `quote()` is a richer shape than `ltp()`
        (depth + OI + day-change + OHLC), and Dhan's batch quote API
        is more rate-limited than the OHLC one used by `ltp()`. The
        platform's PriceBroker walks to the next adapter (Kite) on
        empty, which is the right behaviour for the operator-facing
        chart + depth surfaces. Wire later if Dhan-only deployments
        emerge."""
        return {}

    def instruments(self, exchange: str | None = None) -> list[dict]:
        """Load Dhan instruments from the master CSV
        (images.dhan.co/api-data/api-scrip-master.csv).
        Cached per IST day — first call fetches, subsequent calls read from memory.
        Returns a Kite-shape list (tradingsymbol, security_id, exchange, lot_size,
        tick_size, exchange_segment). Returns [] on network failure so PriceBroker /
        get_historical_brokers fall through to the next adapter cleanly."""
        _ensure_dhan_instruments()
        if exchange:
            return list(_DHAN_BY_EXCHANGE.get(exchange, []))
        # No exchange filter — merge all
        out: list[dict] = []
        for rows in _DHAN_BY_EXCHANGE.values():
            out.extend(rows)
        return out

    def historical_data(
        self,
        instrument_token: int,
        from_date: Any,
        to_date: Any,
        interval: str = "day",
    ) -> list[dict]:
        """Not wired yet — returns empty bars. PriceBroker fallback
        chain moves on to the next adapter (typically Kite). Same
        rationale as instruments() above: silent empty beats noisy
        NotImplementedError when the adapter is intentionally
        partial."""
        return []

    def holidays(self, exchange: str) -> set[str]:
        """Dhan doesn't publish a holidays endpoint. Empty set so
        PriceBroker falls over to Kite without an exception trace."""
        return set()

    def market_status(self, exchange: str) -> bool | None:
        """Probe Dhan's market-status / exchange-hours endpoint for
        `exchange`. Returns True if open, False if closed, None when
        the SDK doesn't expose the method or the call fails. The
        market_probe layer caches results, so this adapter call only
        fires once per cache TTL per exchange.

        Method discovery probes the most common SDK method names
        across dhanhq versions (`get_market_status`,
        `market_status`, `get_exchange_status`). Maps Dhan's
        per-segment status payload to our exchange vocabulary:
            NSE_EQ / BSE_EQ / NSE_CURRENCY / BSE_CURRENCY → equity
              (NSE / BSE)
            NSE_FNO / BSE_FNO → derivatives (NFO / BFO)
            MCX_COMM → commodity (MCX)
        """
        resp = self._call_market_status_sdk(exchange)
        if resp is None:
            return None
        target_codes = _XCHG_TO_DHAN_MARKET_STATUS.get((exchange or "").upper())
        if not target_codes:
            return None
        rows = _extract_dhan_status_rows(resp, target_codes)
        if rows is None:
            # SDK returned an unparseable shape — fall through.
            return None
        for row in rows:
            if _dhan_row_indicates_open(row, target_codes):
                return True
        # All mapped segments report closed.
        return False

    def _call_market_status_sdk(self, exchange: str) -> Any | None:
        """Discover the SDK method by name (so _safe_call's retry path
        picks up the FRESH SDK handle — same stale-handle pattern as
        funds_ledger above) and invoke. Returns raw response or None
        on miss/failure."""
        sdk = self.dhan
        status_method_name = next(
            (n for n in ("get_market_status", "market_status", "get_exchange_status")
             if getattr(sdk, n, None) is not None),
            None,
        )
        if status_method_name is None:
            return None
        try:
            return self._safe_call(
                lambda d: getattr(d, status_method_name)()
            )
        except Exception as e:
            logger.debug(f"DhanBroker.market_status({exchange}) SDK call failed: {e}")
            return None

    # ── Order entry ───────────────────────────────────────────────────

    def basket_order_margins(self, orders: list[dict]) -> list[dict]:
        """Dhan exposes per-order margin calculation but no batch endpoint.
        Loop over orders calling `margin_calculator()` per order; return
        a Kite-shape list with `total` populated. Slower than Kite's
        single round-trip but functionally equivalent."""
        out: list[dict] = []
        for o in orders:
            try:
                ex_seg  = _dhan_exchange(o.get("exchange", ""))
                txn     = o.get("transaction_type", "BUY")
                qty     = int(o.get("quantity", 0))
                price   = float(o.get("price") or 0)
                product = _PRODUCT_TO_DHAN.get(o.get("product", "MIS"), "INTRADAY")
                # Dhan's SDK method name has shifted between versions —
                # try `margin_calculator()` first, fall back to the
                # raw POST if missing. Either path returns a dict with
                # a `data.totalMargin` field we map to Kite's `total`.
                if hasattr(self.dhan, "margin_calculator"):
                    # Slice Q — resolve security_id from tradingsymbol +
                    # exchange when the caller didn't supply it, mirroring
                    # place_order. Pre-fix: always used o.get("security_id",
                    # "") which was always "" → Dhan returned 0 margin →
                    # every paper-engine order registered OPEN not REJECTED.
                    sid = (str(o.get("security_id") or "")
                           or _resolve_security_id(
                               str(o.get("tradingsymbol", "")),
                               str(o.get("exchange", ""))))
                    resp = self._safe_call(lambda d: d.margin_calculator(
                        security_id=sid,
                        exchange_segment=ex_seg,
                        transaction_type=txn,
                        quantity=qty,
                        product_type=product,
                        price=price,
                    ))
                else:
                    raise RuntimeError("dhanhq SDK missing margin_calculator method")
                data = resp.get("data") if isinstance(resp, dict) else {}
                out.append({
                    "total":     float(data.get("totalMargin", 0) or 0),
                    "var":       float(data.get("spanMargin", 0) or 0),
                    "exposure":  float(data.get("exposureMargin", 0) or 0),
                    "available": {"cash": float(data.get("availableBalance", 0) or 0)},
                    "raw":       resp,
                })
            except Exception as e:
                logger.warning(f"DhanBroker.basket_order_margins failed for "
                               f"{o.get('tradingsymbol')}: {e}")
                out.append({"total": 0.0, "error": str(e), "raw": None})
        return out

    def place_order(self, **kwargs: Any) -> str:
        """Translate Kite kwargs to Dhan and dispatch. Returns Dhan order_id.

        Accepts the same kwargs as KiteBroker.place_order: tradingsymbol,
        exchange, transaction_type, quantity, product, order_type, price,
        trigger_price, validity, tag, variety.

        security_id is resolved from tradingsymbol + exchange via the
        instruments cache (loaded from Dhan's master CSV once per IST
        day). If the symbol is unknown, raises RuntimeError with a clear
        message pointing at the cache — operator should check whether the
        Dhan instruments CSV has loaded successfully."""
        # Audit fix (M-3) — `variety` is Kite-semantic
        # ("regular" / "amo" / "bo" / "co"). Pre-fix the value flowed
        # through **kwargs and was silently dropped at the Dhan SDK
        # boundary — AMO orders submitted with `variety="amo"` landed
        # as regular-hours, with no error. Now: AMO needs an explicit
        # productType on the Dhan side; raise a clear error so the
        # caller knows the request isn't honored. Other varieties are
        # absorbed silently (regular is the default; bo/co aren't
        # supported by the platform's order pipeline today).
        _variety = str(kwargs.pop("variety", "regular") or "regular").lower()
        if _variety in ("amo", "after_market", "after-market"):
            raise NotImplementedError(
                "Dhan adapter does not yet route AMO orders. Submit during "
                "market hours or route via the Kite-mirrored account."
            )
        exchange      = kwargs.get("exchange", "")
        tradingsymbol = kwargs.get("tradingsymbol", "")

        # Resolve security_id — prefer explicit kwarg over instruments lookup
        # so callers that already have security_id (e.g. basket_order_margins)
        # don't pay the cache lookup cost unnecessarily.
        security_id = str(kwargs.get("security_id") or "")
        if not security_id:
            security_id = _resolve_security_id(tradingsymbol, exchange)
        if not security_id:
            raise RuntimeError(
                f"Dhan: unknown tradingsymbol {tradingsymbol!r} on {exchange!r} — "
                f"symbol not found in instruments cache. Ensure Dhan instruments "
                f"CSV loaded successfully (check DhanBroker.instruments())."
            )

        ex_seg  = _dhan_exchange(exchange)
        product = _PRODUCT_TO_DHAN.get(kwargs.get("product", "MIS"), "INTRADAY")
        otype   = _ORDER_TYPE_TO_DHAN.get(kwargs.get("order_type", "MARKET"), "MARKET")

        # Truncate correlation_id (tag) to 20 chars — Dhan enforces
        # a similar cap on correlationId as Kite does on tag.
        _DHAN_CORR_MAX = 20
        tag = kwargs.get("tag")
        if tag is not None:
            tag = str(tag)[:_DHAN_CORR_MAX]

        resp = self._safe_call(lambda d: d.place_order(
            security_id=security_id,
            exchange_segment=ex_seg,
            transaction_type=kwargs.get("transaction_type", "BUY"),
            quantity=int(kwargs.get("quantity", 0)),
            order_type=otype,
            product_type=product,
            price=float(kwargs.get("price") or 0),
            trigger_price=float(kwargs.get("trigger_price") or 0),
            validity=kwargs.get("validity", "DAY"),
            **({"tag": tag} if tag else {}),
        ))
        if not isinstance(resp, dict) or resp.get("status") != "success":
            raise RuntimeError(f"Dhan place_order rejected: {resp}")
        return str(resp.get("data", {}).get("orderId", ""))

    def modify_order(self, order_id: str, **kwargs: Any) -> str:
        resp = self._safe_call(lambda d: d.modify_order(
            order_id=order_id,
            quantity=int(kwargs.get("quantity", 0)) if kwargs.get("quantity") else None,
            price=float(kwargs.get("price") or 0) if kwargs.get("price") else None,
            trigger_price=(float(kwargs.get("trigger_price") or 0)
                           if kwargs.get("trigger_price") else None),
            order_type=_ORDER_TYPE_TO_DHAN.get(kwargs.get("order_type", ""), None),
        ))
        if not isinstance(resp, dict) or resp.get("status") != "success":
            raise RuntimeError(f"Dhan modify_order rejected: {resp}")
        return order_id

    def cancel_order(self, order_id: str, **kwargs: Any) -> str:
        resp = self._safe_call(lambda d: d.cancel_order(order_id=order_id))
        if not isinstance(resp, dict) or resp.get("status") != "success":
            raise RuntimeError(f"Dhan cancel_order rejected: {resp}")
        return order_id

    # ── GTT (Forever Orders) ──────────────────────────────────────────
    #
    # Dhan calls these "Forever Orders". The dhanhq SDK inherits from
    # ForeverOrder which provides: place_forever / modify_forever /
    # cancel_forever / get_forever. The capability matrix declares
    # gtt_single=True and gtt_oco=True.
    #
    # Kite's "single" → Dhan's order_flag="SINGLE"
    # Kite's "two-leg" (OCO) → Dhan's order_flag="OCO"
    #
    # Shape mapping (Kite → Dhan):
    #   trigger_type="single"  → order_flag="SINGLE"
    #                            trigger_Price  = trigger_values[0]
    #                            price          = orders[0]["price"]
    #   trigger_type="two-leg" → order_flag="OCO"
    #                            leg 0: trigger_Price, price, quantity
    #                            leg 1: trigger_Price1, price1, quantity1
    #
    # Dhan docs: https://dhanhq.co/docs/api-reference/v2/forever-orders/

    def place_gtt(
        self,
        *,
        trigger_type: str,
        tradingsymbol: str,
        exchange: str,
        last_price: float,
        orders: list[dict],
        trigger_values: list[float],
        tag: str | None = None,
    ) -> str:
        """Place a Dhan Forever Order (GTT). Returns the Dhan order_id."""
        # Sprint C — Dhan Forever covers equity / F&O segments but the
        # adapter has no MCX/NCO commodity wiring (Dhan's positions
        # coverage is incomplete on MCX too — see CLAUDE.md
        # "Multi-Account IPv6 Source Binding" notes on the CRUDEOIL
        # symbol resolution failure). Raise a clear runtime error here
        # so the template-attach path surfaces a single readable error
        # in AttachResult.errors instead of the SDK's opaque rejection
        # ("security_id not found"). Operators with a Dhan account
        # placing MCX templates should mirror to their Kite account.
        if exchange in ("MCX", "NCO"):
            # Audit fix — raise NotImplementedError, not RuntimeError. The
            # template-attach pipeline + trail-stop background task catch
            # NotImplementedError as "broker doesn't support this feature"
            # and surface it through AttachResult.errors. Pre-fix the
            # RuntimeError bubbled up uncaught, producing a 500 + no exit
            # GTT for any MCX template attach on Dhan.
            raise NotImplementedError(
                f"Dhan Forever Order does not cover MCX/NCO — operator "
                f"should attach the template via the Kite-mirrored "
                f"account (got exchange={exchange!r}, symbol={tradingsymbol!r})"
            )
        security_id = _resolve_security_id(tradingsymbol, exchange)
        if not security_id:
            raise RuntimeError(
                f"Dhan place_gtt: unknown symbol {tradingsymbol!r} on {exchange!r}"
            )
        ex_seg  = _dhan_exchange(exchange)
        order0  = orders[0] if orders else {}
        product = _PRODUCT_TO_DHAN.get(order0.get("product", "NRML"), "MARGIN")
        otype   = _ORDER_TYPE_TO_DHAN.get(order0.get("order_type", "LIMIT"), "LIMIT")
        qty0    = int(order0.get("quantity", 0))
        price0  = float(order0.get("price") or 0)
        trig0   = float(trigger_values[0]) if trigger_values else 0.0
        txn0    = order0.get("transaction_type", "SELL")

        _DHAN_CORR_MAX = 20
        corr = str(tag)[:_DHAN_CORR_MAX] if tag else None

        if trigger_type == "single":
            resp = self._safe_call(lambda d: d.place_forever(
                security_id=security_id,
                exchange_segment=ex_seg,
                transaction_type=txn0,
                product_type=product,
                order_type=otype,
                quantity=qty0,
                price=price0,
                trigger_Price=trig0,
                order_flag="SINGLE",
                tag=corr,
                symbol=tradingsymbol,
            ))
        else:
            # OCO — two-leg. Leg 0: entry/stop, Leg 1: target.
            order1  = orders[1] if len(orders) > 1 else {}
            otype1  = _ORDER_TYPE_TO_DHAN.get(order1.get("order_type", "LIMIT"), "LIMIT")
            qty1    = int(order1.get("quantity", qty0))
            price1  = float(order1.get("price") or 0)
            trig1   = float(trigger_values[1]) if len(trigger_values) > 1 else 0.0
            resp = self._safe_call(lambda d: d.place_forever(
                security_id=security_id,
                exchange_segment=ex_seg,
                transaction_type=txn0,
                product_type=product,
                order_type=otype,
                quantity=qty0,
                price=price0,
                trigger_Price=trig0,
                order_flag="OCO",
                price1=price1,
                trigger_Price1=trig1,
                quantity1=qty1,
                tag=corr,
                symbol=tradingsymbol,
            ))

        if not isinstance(resp, dict) or resp.get("status") != "success":
            raise RuntimeError(f"Dhan place_gtt rejected: {resp}")
        data = resp.get("data") or {}
        if isinstance(data, dict):
            return str(data.get("orderId") or data.get("order_id") or "")
        return str(data)

    def modify_gtt(
        self,
        gtt_id: str,
        *,
        trigger_type: str,
        tradingsymbol: str,
        exchange: str,
        last_price: float,
        orders: list[dict],
        trigger_values: list[float],
    ) -> str:
        """Modify an existing Dhan Forever Order. Returns the (same) order_id.

        For OCO (`trigger_type='two-leg'`) Dhan requires TWO modify
        calls — one per leg — because `modify_forever` only updates the
        leg named by `leg_name`. Sprint C fix (audit defect): the prior
        implementation hardcoded `leg_name='ENTRY_LEG'` so the
        target-side TP update never reached the broker. The Sprint A
        two-leg trail-stop wired `[tp_trigger, sl_trigger]` through
        modify_gtt expecting both to land; only the entry leg was
        actually changing.
        """
        order_flag = "SINGLE" if trigger_type == "single" else "OCO"
        order0  = orders[0] if orders else {}
        otype0  = _ORDER_TYPE_TO_DHAN.get(order0.get("order_type", "LIMIT"), "LIMIT")
        qty0    = int(order0.get("quantity", 0))
        price0  = float(order0.get("price") or 0)
        trig0   = float(trigger_values[0]) if trigger_values else 0.0
        # Single GTT (or first leg of OCO) → ENTRY_LEG.
        resp = self._safe_call(lambda d: d.modify_forever(
            order_id=gtt_id,
            order_flag=order_flag,
            order_type=otype0,
            leg_name="ENTRY_LEG",
            quantity=qty0,
            price=price0,
            trigger_price=trig0,
            disclosed_quantity=0,
            validity="DAY",
        ))
        if not isinstance(resp, dict) or resp.get("status") != "success":
            raise RuntimeError(f"Dhan modify_gtt rejected (entry leg): {resp}")
        # OCO needs a second call for the target leg with leg 1's
        # qty/price/trigger.
        if trigger_type == "two-leg" and len(orders) > 1 and len(trigger_values) > 1:
            order1 = orders[1]
            otype1 = _ORDER_TYPE_TO_DHAN.get(order1.get("order_type", "LIMIT"), "LIMIT")
            qty1   = int(order1.get("quantity", qty0))
            price1 = float(order1.get("price") or 0)
            trig1  = float(trigger_values[1])
            resp1  = self._safe_call(lambda d: d.modify_forever(
                order_id=gtt_id,
                order_flag="OCO",
                order_type=otype1,
                leg_name="TARGET_LEG",
                quantity=qty1,
                price=price1,
                trigger_price=trig1,
                disclosed_quantity=0,
                validity="DAY",
            ))
            if not isinstance(resp1, dict) or resp1.get("status") != "success":
                # Audit fix (M-2) — asymmetric GTT state. The ENTRY_LEG
                # modify already succeeded; the GTT now has the NEW
                # entry trigger paired with the OLD target trigger.
                # Pre-fix the caller saw a generic RuntimeError + DEBUG
                # log and the operator's trail-stop poller kept calling
                # us with stale state, oblivious to the half-modified
                # GTT. Now: log at WARNING + raise with an explicit
                # `dhan_partial_modify=True` flag so the trail-stop
                # task can persist a `partial_modify_error` slot in
                # attached_gtts_json and the OrderCard tooltip can
                # surface "⚠ GTT asymmetric — entry updated, target
                # stale".
                logger.warning(
                    f"Dhan modify_gtt {gtt_id}: ENTRY_LEG succeeded but "
                    f"TARGET_LEG rejected ({resp1}). GTT is now "
                    f"ASYMMETRIC — entry trigger updated, target stale. "
                    f"Operator should cancel + recreate or accept the "
                    f"half-modified state."
                )
                _err = RuntimeError(
                    f"Dhan modify_gtt rejected (target leg, ENTRY already "
                    f"modified — GTT is asymmetric): {resp1}"
                )
                # Sentinel attribute the trail-stop task checks via
                # `getattr(err, "dhan_partial_modify", False)`. Sticks
                # to the exception so the caller can branch without
                # parsing the error message string.
                _err.dhan_partial_modify = True   # type: ignore[attr-defined]
                _err.dhan_modified_leg = "ENTRY_LEG"   # type: ignore[attr-defined]
                raise _err
        return gtt_id

    def cancel_gtt(self, gtt_id: str, *, exchange: str | None = None) -> str:
        """Cancel a Dhan Forever Order. Returns the cancelled order_id.
        `exchange` is accepted for parity with the ABC + Groww signature
        (the OCO pair-watcher passes it via kwarg). Dhan addresses a
        Forever Order by gtt_id alone, so we ignore the hint here.
        Pre-fix this overrode the ABC without the kwarg → TypeError when
        the OCO rollback path called `broker.cancel_gtt(sib_id,
        exchange=sib_exchange)`, leaving one leg of an emulated OCO
        alive on the book."""
        del exchange  # unused on Dhan
        resp = self._safe_call(lambda d: d.cancel_forever(order_id=gtt_id))
        if not isinstance(resp, dict) or resp.get("status") != "success":
            raise RuntimeError(f"Dhan cancel_gtt rejected: {resp}")
        return gtt_id

    def get_gtts(self) -> list[dict]:
        """List all active Dhan Forever Orders, normalised to Kite GTT shape."""
        resp = self._safe_call(lambda d: d.get_forever())
        rows = _unwrap(resp)
        if not isinstance(rows, list):
            rows = []
        out: list[dict] = []
        for r in rows:
            flag = (r.get("orderFlag") or "SINGLE").upper()
            ttype = "single" if flag == "SINGLE" else "two-leg"
            seg = r.get("exchangeSegment") or ""
            kite_exch = _DHAN_SEGMENT_TO_EXCHANGE.get(seg, seg)
            # Build trigger_values list from the response fields
            t0 = float(r.get("triggerPrice") or r.get("trigger_Price") or 0)
            t1 = float(r.get("triggerPrice1") or r.get("trigger_Price1") or 0)
            trigger_values = [t0, t1] if ttype == "two-leg" else [t0]
            out.append({
                "gtt_id":       str(r.get("orderId") or r.get("order_id") or ""),
                "status":       (r.get("orderStatus") or r.get("status") or "").lower(),
                "trigger_type": ttype,
                "tradingsymbol": r.get("tradingSymbol") or r.get("symbol") or "",
                "exchange":     kite_exch,
                "trigger_values": trigger_values,
                "last_price":   float(r.get("lastTradedPrice") or 0),
                "orders": [{
                    "transaction_type": r.get("transactionType") or "SELL",
                    "quantity":         int(r.get("quantity") or 0),
                    "price":            float(r.get("price") or 0),
                    "order_type":       r.get("orderType") or "LIMIT",
                    "product":          r.get("productType") or "NRML",
                }],
                "created_at":   r.get("createTime") or "",
                "_raw":         r,
            })
        return out

    # ── Qty translation ───────────────────────────────────────────────

    def translate_qty(self, exchange: str, raw_qty: int, lot_size: int) -> int:
        """Convert canonical-contract qty (the unit our routes + position
        normalisers use internally) to Dhan's wire format.

        Dhan's API takes quantity IN LOTS for MCX/NCO and IN CONTRACTS
        for NSE/BSE F&O — same convention Kite uses. The position-data
        normaliser (`_normalise_positions`) multiplies netQty × multiplier
        to convert Dhan's lot-based read response back to contracts so
        every downstream surface (Legs grid, day_change_val formula,
        analytics, paper engine) treats Dhan + Kite rows uniformly. This
        method undoes that for the OUTBOUND order: contract qty → lots
        on MCX/NCO, identity on NSE/BSE F&O.

        operator on /admin/derivatives: Dhan CRUDEOIL position was
        showing qty=1 while Kite showed qty=300 for the same 3 lots —
        because the read path stayed in lots while Kite read in contracts.
        The fix normalises BOTH read + write to contracts internally."""
        if exchange in ("MCX", "NCO") and lot_size > 0 and raw_qty >= lot_size:
            translated = max(1, raw_qty // lot_size)
            if translated != raw_qty:
                logger.info(
                    f"[DHAN-QTY] {exchange}: contracts={raw_qty} → lots={translated} "
                    f"(lot_size={lot_size})"
                )
            return translated
        return raw_qty

    def normalise_qty(self, exchange: str, raw_qty: int, lot_size: int) -> int:
        """Back-compat alias — prefer translate_qty in new code."""
        return self.translate_qty(exchange, raw_qty, lot_size)


# ── Response normalisers ──────────────────────────────────────────────
#
# Each helper converts Dhan's REST response (lists of camelCase dicts)
# into the Kite shape callers expect (snake_case + Kite field names).
# Unknown/extra Dhan fields pass through so an operator inspecting the
# raw payload still sees the full picture.


def _unwrap(resp: Any) -> list[dict]:
    """Dhan responses wrap the payload in {status, data} envelopes —
    unwrap to the list inside `data` (or [] on shape mismatch)."""
    if isinstance(resp, dict):
        data = resp.get("data")
        if isinstance(data, list):
            return data
    return []


def _normalise_holdings(resp: Any) -> list[dict]:
    """Dhan holdings field map → Kite. Carries through any field we don't
    explicitly translate, so downstream summarise helpers still find
    expected keys + adapter authors see the full Dhan payload.

    Type-match Kite carefully — pandas+polars conversion downstream is
    strict about column dtypes when rows from multiple brokers are
    concatenated. instrument_token MUST be int (Kite shape), not the
    str Dhan returns; opening_quantity MUST be present (holdings model
    field) — we use totalQty as the proxy since Dhan doesn't expose a
    separate start-of-day count.
    """
    out: list[dict] = []
    for h in _unwrap(resp):
        # Dhan splits "settled to demat" (totalQty) and "T+1 pending"
        # (t1Qty). The contract observed in production:
        #   • Fresh CNC buy (today):   totalQty=0, t1Qty=N    → qty=N
        #   • Fully settled (T+3):     totalQty=N, t1Qty=0    → qty=N
        #   • Both populated:          totalQty=N, t1Qty=N    → qty=N
        #     (Dhan reports the SAME N shares in both fields — not
        #     additive. Confirmed by operator who has 2 SIEMENS shares
        #     but saw qty=4 when v1 of this code summed the fields.)
        #
        # Right interpretation: max(totalQty, t1Qty). Equivalent to
        # "use settled if any, else use pending" — handles all three
        # cases without double-counting.
        #
        # Prior code (qty = _t_settled + t1q) was based on the
        # assumption that t1Qty was strictly INCREMENTAL to totalQty.
        # The "both populated" case (operator: 'I have only 2 siemens
        # shares' but display showed 4) proves Dhan's v2 contract
        # actually reports T+1 shares in BOTH fields once they appear
        # in the holdings list.
        _t_settled = int(h.get("totalQty",  0) or 0)
        t1q = int(h.get("t1Qty",     0) or 0)
        qty = max(_t_settled, t1q)
        # Dhan returns securityId as a numeric string ("21131"); coerce
        # to int so concat with Kite holdings doesn't trip polars.
        try:
            inst_tok = int(h.get("securityId") or 0)
        except (TypeError, ValueError):
            inst_tok = 0

        avg_price  = float(h.get("avgCostPrice", 0) or 0)
        last_price = float(h.get("lastTradedPrice", 0) or 0)

        # Derive close_price + pnl + day_change when Dhan's response
        # omits them (the holdings endpoint frequently does — only
        # avgCostPrice + lastTradedPrice + totalQty are reliably
        # populated). Without the derivation downstream sees:
        #   close_price = 0 → day_change_pct == 100% (broken display)
        #   pnl         = 0 → P&L column shows 0 even on big movers
        # Kite responses ship these computed, so we mirror that here
        # to keep the cross-broker concat downstream comparable.
        close_price = float(
            h.get("previousClosePrice", h.get("closePrice", 0)) or 0
        )
        # Leave close_price=0 when truly missing — the broker_apis
        # `backfill_market_data` helper (called after pd.concat at
        # the /api/holdings endpoint) batches a PriceBroker.quote()
        # call across every missing-close row from every account and
        # patches them in one round-trip. The earlier fallback to
        # last_price (close=last → day_change=0 → frontend `—`)
        # masked these rows from the backfill mask and looked like a
        # silent zero on the Day P&L column. PriceBroker outage
        # leaves close=0 and the fetch_holdings recompute falls
        # through to broker-reported day_change_val (also 0) — same
        # safe end state as the prior fallback.

        pnl_raw = h.get("unrealisedProfit")
        if pnl_raw in (None, 0, "0", 0.0):
            pnl = (last_price - avg_price) * qty
        else:
            pnl = float(pnl_raw)

        # day_change is per-share ₹ to match Kite's convention (= ltp − close).
        # Dhan's raw `dayChange` field is the day's TOTAL position change
        # (qty × per-share); using it directly produces a portfolio-₹
        # value that downstream consumers misread as per-share. Earlier
        # path also fell back to `(ltp − close) × qty` which doubled the
        # qty multiplication when `broker_apis.fetch_holdings` then
        # multiplied by opening_quantity again — net 500× drift for a
        # 500-share row in the fallback branch. Derive from price diff
        # directly so the unit is multiplier-invariant + qty-invariant.
        day_change = last_price - close_price

        day_change_pct_raw = h.get("dayChangePerc")
        if day_change_pct_raw in (None, 0, "0", 0.0):
            day_change_pct = (
                ((last_price - close_price) / close_price * 100.0)
                if close_price > 0 else 0.0
            )
        else:
            day_change_pct = float(day_change_pct_raw)

        # Translate Dhan F&O symbol → Kite-style (see _dhan_to_kite_symbol)
        # so every downstream parser + chart works without per-vendor branches.
        _raw_ts_h = str(h.get("tradingSymbol") or h.get("symbol") or "")
        # Dhan reports equity holdings with exchange="ALL" (cross-exchange
        # marker — the same ISIN is held against both NSE + BSE liquidity).
        # That literal leaks into the close-price backfill which builds
        # "ALL:TEJASNET" quote keys that no broker can resolve → close=0 →
        # day_change_val=0 → /pulse + /performance show '—' for Day P&L
        # on every Dhan equity row. Resolution order:
        #   1. exchangeSegment ("NSE_EQ" → "NSE") if Dhan provides it
        #   2. exchange field mapped through the segment table
        #   3. "NSE" as the safe equity-default for "ALL" / blank
        _seg_h = str(h.get("exchangeSegment") or "").upper()
        _exch_raw_h = str(h.get("exchange") or "").upper()
        _kite_exch_h = (
            _DHAN_SEGMENT_TO_EXCHANGE.get(_seg_h)
            or (_DHAN_SEGMENT_TO_EXCHANGE.get(_exch_raw_h, _exch_raw_h)
                if _exch_raw_h and _exch_raw_h != "ALL"
                else "NSE")
        )
        out.append({
            "tradingsymbol":   _dhan_to_kite_symbol(_raw_ts_h),
            "exchange":        _kite_exch_h,
            "instrument_token": inst_tok,
            "isin":             h.get("isin"),
            "quantity":         qty,
            # opening_quantity is required by the holdings model + drives
            # inv_val / cur_val / pnl_percentage derivations downstream.
            # Dhan doesn't expose a separate "opening" count, so default
            # to totalQty (same shape as Kite holdings T0 → T+x).
            "opening_quantity": qty,
            "t1_quantity":      int(h.get("t1Qty",     0) or 0),
            "average_price":    avg_price,
            "last_price":       last_price,
            "close_price":      close_price,
            "pnl":              pnl,
            "day_change":       day_change,
            "day_change_percentage": day_change_pct,
            "product":          "CNC",  # Holdings are always delivery on Dhan
            "_raw":             h,
        })
    return out


def _normalise_positions(resp: Any) -> dict:
    """Dhan positions → Kite-shape {net: [...], day: [...]}. Dhan
    returns one flat list; we map each row to a `net` entry. `day`
    is empty until Dhan exposes intraday-only positions separately."""
    net: list[dict] = []
    for p in _unwrap(resp):
        net.append(_normalise_position_row(p))
    return {"net": net, "day": []}


def _normalise_position_row(p: dict) -> dict:
    """Map one Dhan position row to the Kite-shape dict.

    Split from the loop body to keep _normalise_positions readable;
    the per-row work is what pushes CC — separates translation from
    iteration."""
    try:
        inst_tok = int(p.get("securityId") or 0)
    except (TypeError, ValueError):
        inst_tok = 0
    # Translate Dhan's F&O tradingsymbol to Kite-style canonical
    # form via `_dhan_to_kite_symbol` (e.g. "CRUDEOIL-16JUL2026-8500-CE"
    # → "CRUDEOIL26JUL8500CE"). Without this every downstream parser
    # (decomposeSymbol on the frontend, parse_tradingsymbol in the
    # strategy endpoint, the instruments-cache lookup, etc.) rejects
    # Dhan-format symbols and the Legs grid shows "isn't a recognised
    # option or futures contract" above the payoff chart.
    raw_ts = str(p.get("tradingSymbol") or "")
    ts = _dhan_to_kite_symbol(raw_ts)

    _mult, qty_contracts, ovn_contracts, dbq_contracts, dsq_contracts = \
        _normalise_position_quantities(p)

    avg, ltp, close, pnl_calc, dcv_calc, realised = \
        _normalise_position_prices_and_pnl(p, qty_contracts)

    _kite_exch_p = _normalise_position_exchange(p)

    return {
        "tradingsymbol":   ts,
        "exchange":        _kite_exch_p,
        "instrument_token": inst_tok,
        "product":         {"INTRADAY": "MIS",
                            "MARGIN":   "NRML",
                            "CNC":      "CNC"}.get(p.get("productType", ""),
                                                    "NRML"),
        "quantity":           qty_contracts,
        "overnight_quantity": ovn_contracts,
        "day_buy_quantity":   dbq_contracts,
        "day_sell_quantity":  dsq_contracts,
        # Day-trade cash values — forwarded to the /admin/derivatives
        # Candidates panel where `splitClosedReopened` uses them to
        # split a closed-and-reopened leg into two display rows. The
        # post-BH6 broker_apis day_change_val formula also reads
        # these (`_bq × LTP − _bv`), so the units MUST match the
        # contract-units quantities above.
        #
        # Always derive from `dayBuyAvg × dbq_contracts` rather than
        # trusting Dhan's pre-computed `dayBuyValue` field. Dhan's
        # docs are ambiguous on whether dayBuyValue is in
        # `lots × price` or `contracts × price` (= absolute ₹); the
        # derivation `dayBuyAvg × dbq_contracts` is contract-units
        # regardless because dbq_contracts is post-multiply. Same
        # for day_sell. (Audit Jun 26 2026 — risk surfaced when the
        # broker_apis MCX × multiplier patch landed.)
        "day_buy_value":      float(p.get("dayBuyAvg",  0) or 0) * dbq_contracts,
        "day_sell_value":     float(p.get("daySellAvg", 0) or 0) * dsq_contracts,
        # Multiplier=1 on the normalised row — qty is now in contracts
        # so the broker_apis day_change_val formula treats it the same
        # as Kite's contract-qty (no extra multiplication needed).
        "multiplier":      1,
        "close_price":     close,
        "average_price":   avg,
        "last_price":      ltp,
        "buy_price":       float(p.get("buyAvg",       0) or 0),
        "sell_price":      float(p.get("sellAvg",      0) or 0),
        "buy_quantity":    int(p.get("buyQty",         0) or 0) * _mult,
        "sell_quantity":   int(p.get("sellQty",        0) or 0) * _mult,
        # Pre-computed pnl + day_change_val from our own formulas.
        # broker_apis.fetch_positions recomputes both at the central
        # chokepoint (universal (LTP-avg)*qty / (LTP-close)*qty rule),
        # which overwrites these when LTP+avg > 0 and LTP+close > 0.
        # The pre-computed values survive as the pre-open / cold-LTP
        # fallback so routes that don't run the recompute (raw-broker
        # views, demo serialisation) still return a sensible number
        # rather than 0.
        "pnl":               pnl_calc,
        "realised":          realised,
        "unrealised":        pnl_calc,
        "day_change_val":    dcv_calc,
        "_raw":              p,
    }


def _normalise_position_quantities(p: dict) -> tuple[int, int, int, int, int]:
    """Convert Dhan's lot-based qty fields to contracts.

    Dhan returns netQty / dayBuy/SellQty in LOTS, not contracts.
    Kite returns positions already in CONTRACTS — and every
    downstream surface (Legs grid display qty, day_change_val
    formula in broker_apis.py, options strategy analytics, sim
    paper-trade engine, agent rules referring to qty) expects
    the CONTRACTS convention. Multiply by Dhan's `multiplier`
    (lot size for the contract) to align both adapters before
    the row hits broker_apis. Order placement re-divides via
    `DhanBroker.translate_qty` (contracts → lots) so the SDK call
    still sees Dhan's expected unit. `multiplier=1` in the output
    dict because the qty is now already in contracts — the
    broker_apis day-PnL formula doesn't need to re-multiply.

    Returns (multiplier, qty, overnight_qty, day_buy_qty, day_sell_qty).
    """
    _mult = int(p.get("multiplier", 1) or 1) or 1
    qty_contracts = int(p.get("netQty",      0) or 0) * _mult
    ovn_contracts = int(p.get("carryFwdQty", 0) or 0) * _mult
    dbq_contracts = int(p.get("dayBuyQty",   0) or 0) * _mult
    dsq_contracts = int(p.get("daySellQty",  0) or 0) * _mult
    return _mult, qty_contracts, ovn_contracts, dbq_contracts, dsq_contracts


def _normalise_position_prices_and_pnl(
    p: dict, qty_contracts: int
) -> tuple[float, float, float, float, float, float]:
    """Compute P&L ourselves — don't trust Dhan's pre-computed
    unrealisedProfit / realisedProfit fields, which have shown a
    ~100× off-by-lot-size discrepancy on F&O contracts (Dhan
    appears to compute these in LOTS while we display CONTRACTS,
    and there's no way to flip the convention from the API).
    Operator: "the entry price and current price difference is
    P&L. from yesterday's closing price and today's price is day
    P&L."
    Formulas (signed; long qty>0, short qty<0):
      pnl            = (LTP - avg_price)   × qty   (lifetime / unrealised)
      day_change_val = (LTP - close_price) × qty   (today's change)

    Dhan SDK field names per the v2 positions schema:
      costPrice         — net average across buy + sell legs
      buyAvg / sellAvg  — per-side averages
      lastTradedPrice   — current LTP
      previousClosePrice / closePrice — yesterday's close

    The earlier shape used `netAvgPrice` which is NOT a Dhan
    field — every position came back with avg=0, the (ltp>0 AND
    avg>0) guard kicked in, and pnl_calc was silently set to 0.
    That's why Dhan P&L looked "wrong" on the legs panel even
    though qty and ltp were correct. Fall back through three
    candidate fields so the adapter works across Dhan API
    revisions: costPrice → netAvgPrice (legacy guess) → side-
    appropriate buyAvg / sellAvg.

    Returns (avg, ltp, close, pnl_calc, dcv_calc, realised)."""
    avg = float(p.get("costPrice", p.get("netAvgPrice", 0)) or 0)
    if avg <= 0:
        # Sided fallback — for a long net position use the buy
        # average; for a short net position use the sell average.
        # Neither field is present on a flat (qty=0) row, but we
        # don't surface P&L for flat rows so the 0 path is safe.
        avg = float(p.get("buyAvg", 0) or 0) if qty_contracts >= 0 \
              else float(p.get("sellAvg", 0) or 0)
    ltp = float(p.get("lastTradedPrice", p.get("ltp", 0)) or 0)
    close = float(p.get("previousClosePrice",
                        p.get("previousClose",
                              p.get("closePrice", 0))) or 0)
    pnl_calc = (ltp - avg)   * qty_contracts if (ltp > 0 and avg > 0) else 0.0
    dcv_calc = (ltp - close) * qty_contracts if (ltp > 0 and close > 0) else 0.0
    # Keep Dhan's realisedProfit verbatim — that's a closed-book
    # figure they're authoritative on.
    realised = float(p.get("realisedProfit", 0) or 0)
    return avg, ltp, close, pnl_calc, dcv_calc, realised


def _normalise_position_exchange(p: dict) -> str:
    """Normalise the exchange field the same way holdings do: prefer
    the canonical exchangeSegment map ("NSE_FNO" → "NFO", "MCX_COMM"
    → "MCX") so CRUDEOIL on Dhan reads as MCX (matching Kite) rather
    than the bare "NFO" string Dhan's positions endpoint sometimes
    ships for commodity options. Fall through to p.get("exchange")
    when no segment is present, defaulting to NFO for derivatives."""
    _seg_p = str(p.get("exchangeSegment") or "").upper()
    _exch_raw_p = str(p.get("exchange") or "").upper()
    return (
        _DHAN_SEGMENT_TO_EXCHANGE.get(_seg_p)
        or (_DHAN_SEGMENT_TO_EXCHANGE.get(_exch_raw_p, _exch_raw_p)
            if _exch_raw_p and _exch_raw_p != "ALL"
            else "NFO")
    )


# Set of Dhan accounts whose raw fund_limits response keys have already
# been logged at INFO. One-time per account; resets only on process
# restart. Used by margins() above to confirm field names against
# Dhan's incomplete v2 documentation without spamming logs every poll.
_DHAN_MARGINS_LOGGED: set[str] = set()


def _normalise_margins(resp: Any, segment: str | None) -> dict:
    """Dhan margins endpoint returns a single dict (not per-segment).
    Map to Kite's `equity` shape; if the caller passed segment='commodity'
    we still return the same payload (Dhan doesn't slice this way).

    Audit cycle 8: realized-P&L + option-premium fields now resolve
    through a fallback chain across plausible Dhan v2 field names. Each
    candidate matches a documented Dhan response variant. The one-time
    INFO log in margins() above surfaces the actual keys so we can
    confirm or tighten this mapping per account."""
    data = resp.get("data") if isinstance(resp, dict) else {}
    if not isinstance(data, dict):
        data = {}

    # Available-cash field name chain — Dhan's typo `availabelBalance`
    # plus the spelled-correctly variant for forward-compat.
    _cash = float(
        data.get("availabelBalance",
                 data.get("availableBalance", 0)) or 0
    )

    # Realised M2M chain — Dhan v2 has shown all four spellings across
    # SDK builds. First non-zero wins; falls back to 0.0 only when none
    # of the candidates are present in the response.
    _realised = float(
        data.get("realizedProfit",
                 data.get("realisedProfit",
                          data.get("realizedPnl",
                                   data.get("realisedPnl", 0)))) or 0
    )

    # Option premium currently parked in long options. Same fallback
    # pattern; the strip already derives this from positions so the
    # broker-side field is a cross-check, not load-bearing.
    _opt_prem = float(
        data.get("optionPremium",
                 data.get("optionsPremium",
                          data.get("optionsTraded", 0))) or 0
    )

    payload = {
        "enabled": True,
        "net":     _cash,
        "available": {
            # adhoc_margin maps to sodLimit (Dhan's credit-limit field).
            # Documented mismatch: this is a credit facility, not real cash.
            "adhoc_margin":      float(data.get("sodLimit",        0) or 0),
            "cash":              _cash,
            # opening_balance: Dhan's `sodLimit` is the start-of-day credit
            # limit, not the SOD actual cash balance. The Funds-table
            # `cash` column reads this and therefore shows credit-limit
            # for Dhan rows — strip CA is unaffected (reads `live_balance`).
            "opening_balance":   float(data.get("sodLimit",        0) or 0),
            "live_balance":      _cash,
            "collateral":        float(data.get("collateralAmount", 0) or 0),
            "intraday_payin":    0.0,
        },
        "utilised": {
            "debits":            float(data.get("utilizedAmount",   0) or 0),
            "exposure":          0.0,
            "m2m_realised":      _realised,
            "m2m_unrealised":    0.0,
            "option_premium":    _opt_prem,
            "payout":            float(data.get("withdrawableBalance", 0) or 0),
            "span":              0.0,
            "holding_sales":     0.0,
            "turnover":          0.0,
            "liquid_collateral": 0.0,
            "stock_collateral":  float(data.get("collateralAmount", 0) or 0),
        },
        "_raw": data,
    }
    if segment == "commodity":
        # No per-segment slice from Dhan today — return same payload.
        return payload
    return payload


# Audit fix — map Dhan terminal status strings to Kite canonical values.
# Pre-fix the chase loop checked `status == "COMPLETE"` (Kite's COMPLETE
# string) against Dhan's verbatim "TRADED", so every Dhan order chase
# stalled at chase_max_attempts and terminated UNFILLED, never firing
# the FILLED branch + template attach. PENDING/TRANSIT map to OPEN so
# the chase loop's mid-flight handling treats Dhan TRANSIT the same
# way it treats Kite OPEN.
_DHAN_STATUS_TO_KITE = {
    "TRADED":            "COMPLETE",
    "EXECUTED":          "COMPLETE",
    "FILLED":            "COMPLETE",
    "PENDING":           "OPEN",
    "TRANSIT":           "OPEN",
    "OPEN":              "OPEN",
    # PARTIALLY_TRADED — mid-fill state. Map to OPEN so the chase loop's
    # mid-flight handling treats it like a Kite OPEN order; the chase
    # engine will detect the partial fill via the filled_qty delta.
    "PARTIALLY_TRADED":  "OPEN",
    # AMO / GTT mid-flight states — treat as OPEN so the chase loop
    # continues monitoring until a terminal state arrives.
    "MODIFY_PENDING":              "OPEN",
    "AMENDED":                     "OPEN",
    "TRIGGER_PENDING":             "OPEN",
    "AFTER_MARKET_ORDER_REQ_RECEIVED": "OPEN",
    "CANCELLED":         "CANCELLED",
    "REJECTED":          "REJECTED",
    "EXPIRED":           "EXPIRED",
}


def _normalise_orders(resp: Any) -> list[dict]:
    out: list[dict] = []
    for o in _unwrap(resp):
        # Translate Dhan F&O symbol → Kite-style (see _dhan_to_kite_symbol)
        # so orders + positions display under one canonical tradingsymbol.
        _raw_ts_o = str(o.get("tradingSymbol") or "")
        _raw_status = (o.get("orderStatus") or "").upper()
        _status = _DHAN_STATUS_TO_KITE.get(_raw_status, _raw_status)
        out.append({
            "order_id":         str(o.get("orderId") or ""),
            "tradingsymbol":    _dhan_to_kite_symbol(_raw_ts_o),
            "exchange":         o.get("exchange") or "",
            "status":           _status,
            "transaction_type": o.get("transactionType") or "BUY",
            "order_type":       o.get("orderType") or "MARKET",
            "product":          {"INTRADAY": "MIS",
                                 "MARGIN":   "NRML",
                                 "CNC":      "CNC"}.get(o.get("productType", ""),
                                                         "NRML"),
            "quantity":         int(o.get("quantity",         0) or 0),
            "filled_quantity":  int(o.get("filledQty",        0) or 0),
            "pending_quantity": int(o.get("remainingQty",     0) or 0),
            "price":            float(o.get("price",          0) or 0),
            "trigger_price":    float(o.get("triggerPrice",   0) or 0),
            "average_price":    float(o.get("averageTradedPrice", 0) or 0),
            "order_timestamp":  o.get("createTime")  or "",
            "exchange_timestamp": o.get("exchangeTime") or "",
            "status_message":   o.get("orderStatusMessage") or "",
            "_raw":             o,
        })
    return out


def _normalise_trades(resp: Any) -> list[dict]:
    out: list[dict] = []
    for t in _unwrap(resp):
        _raw_ts_t = str(t.get("tradingSymbol") or "")
        out.append({
            "trade_id":         str(t.get("tradeId")   or ""),
            "order_id":         str(t.get("orderId")   or ""),
            "tradingsymbol":    _dhan_to_kite_symbol(_raw_ts_t),
            "exchange":         t.get("exchange")      or "",
            "transaction_type": t.get("transactionType") or "BUY",
            "quantity":         int(t.get("tradedQuantity", 0) or 0),
            "average_price":    float(t.get("tradedPrice",  0) or 0),
            "exchange_timestamp": t.get("exchangeTime")   or "",
            "_raw":             t,
        })
    return out


def _parse_dhan_date(s: Any):
    """Parse a Dhan ledger `voucherdate` string into a `datetime.date`.
    Handles DD/MM/YYYY (the documented v2 format), YYYY-MM-DD, and
    ISO timestamps. Returns None on a shape mismatch — the caller
    skips that ledger entry rather than crashing the whole pull."""
    from datetime import date as _date, datetime as _dt
    if not s:
        return None
    s = str(s).strip()
    # DD/MM/YYYY
    if "/" in s:
        try:
            dd, mm, yy = s.split("/")
            return _date(int(yy), int(mm), int(dd))
        except (ValueError, IndexError):
            return None
    # ISO date or datetime
    try:
        return _dt.fromisoformat(s[:10]).date()
    except ValueError:
        return None


# Map Dhan's per-exchange segment codes to our daily_book.segment
# vocabulary ('equity' / 'commodity'). Anything unrecognised collapses
# to 'equity' (the safest default — equity wallet covers NSE / BSE
# cash + F&O + CDS for almost every operator).
_DHAN_SEGMENT_MAP = {
    "NSE_EQ":       "equity",
    "BSE_EQ":       "equity",
    "NSE_FNO":      "equity",
    "BSE_FNO":      "equity",
    "NSE_CURRENCY": "equity",
    "BSE_CURRENCY": "equity",
    "MCX_COMM":     "commodity",
}


def _dhan_exchange_to_segment(exchange: str) -> str:
    return _DHAN_SEGMENT_MAP.get((exchange or "").upper(), "equity")
