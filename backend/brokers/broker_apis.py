import os
import random
import threading
import pandas as pd
import polars as pl
import time as _time

from backend.api.algo.pnl_math import decomposed_intraday_pnl, naive_day_pnl
from backend.brokers.connections import Connections
from backend.shared.helpers.decorators import for_all_accounts
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


def _emit_conn_event(
    account: str,
    broker_id: str,
    event_type: str,
    detail: dict | None = None,
) -> None:
    """Lazy-import shim so broker_apis can emit connection events without
    a hard import on conn_events (which owns the DB session factory and
    must only be imported inside the conn_service process)."""
    try:
        # lazy import to avoid circular dependency — conn_events → event_queue → database
        from backend.brokers.service.conn_events import _emit_conn_event as _fire
        _fire(account, broker_id, event_type, detail)
    except Exception:
        pass


def _broker_id_safe(account: str) -> str:
    """Best-effort broker_id resolution — never raises."""
    try:
        from backend.brokers.registry import _broker_id_for
        return _broker_id_for(account)
    except Exception:
        return "unknown"


# Auth-error signal strings shared across Kite + Dhan error messages.
# Kept here (not imported from dhan.py) to avoid a cross-adapter import.
_AUTH_ERROR_HINTS_LOWER: tuple[str, ...] = (
    "invalid access token",
    "invalid token",
    "token expired",
    "unauthorized",
    "unauthorised",
    "auth failed",
    "invalid api key",
    "403",
    "401",
    "dh-901",
    "dh-906",
)


def _is_auth_error_str(error: str) -> bool:
    """Return True when the stringified error message looks like an auth / token
    failure (401 / 403 class). Used to select event_type="auth_fail" vs
    "fetch_fail" in _record_fetch without requiring the original exception object.
    """
    low = error.lower()
    return any(hint in low for hint in _AUTH_ERROR_HINTS_LOWER)

# ---------------------------------------------------------------------------
# Last-known-good LTP cache
# ---------------------------------------------------------------------------
# Persists the most recent valid (> 0) LTP per symbol across broker calls.
# When all live sources (PriceBroker.quote + KiteTicker) return 0 or raise,
# this cache allows the route to return a recent real value instead of
# propagating a zero to the frontend.
#
# Shape: {symbol: (unix_ts, ltp)}
# Thread-safe via _LAST_GOOD_LTP_LOCK.
# TTL: entries older than 1 hour are treated as absent so overnight stale
# values don't bleed into the next session.  Process restart clears the
# dict (in-memory only by design — next successful fetch repopulates).
_LAST_GOOD_LTP: dict[str, tuple[float, float]] = {}
_LAST_GOOD_LTP_LOCK = threading.Lock()
_LAST_GOOD_LTP_TTL_S: float = 3600.0  # 1 hour


def record_good_ltp(symbol: str, ltp: float) -> None:
    """Record `ltp` as the last-known-good price for `symbol`.

    Only writes when ltp > 0.  Thread-safe."""
    if not symbol or not (ltp > 0):
        return
    now = _time.time()
    with _LAST_GOOD_LTP_LOCK:
        _LAST_GOOD_LTP[symbol] = (now, float(ltp))


def get_last_good_ltp(symbol: str, max_age_s: float = _LAST_GOOD_LTP_TTL_S) -> float | None:
    """Return the cached last-known-good LTP for `symbol` if it was
    recorded within the last `max_age_s` seconds, else None.

    Thread-safe read; returns None when symbol unknown or entry expired."""
    if not symbol:
        return None
    now = _time.time()
    with _LAST_GOOD_LTP_LOCK:
        entry = _LAST_GOOD_LTP.get(symbol)
    if entry is None:
        return None
    ts, ltp = entry
    if now - ts > max_age_s:
        return None
    return ltp


# ---------------------------------------------------------------------------
# Last-known-good QUOTE cache (open/close/volume/oi/change)
# ---------------------------------------------------------------------------
# Companion to _LAST_GOOD_LTP.  While the LTP cache holds the most recent
# scalar price per symbol, this one holds the full non-LTP snapshot fields
# (open, close, volume, oi, change, change_pct, bid, ask) so /api/quote/batch
# can serve real values during closed hours instead of dropping every
# non-LTP field to null.
#
# Shape: {symbol: (unix_ts, {open, close, volume, oi, change, change_pct, bid, ask})}
# Populated by batch_quote's live path (successful broker.quote() response).
# Read by batch_quote's closed-hours path (skip broker call).
# TTL: 24h — same window used by the closed-hours LTP fallback so both
# scalar and snapshot fields survive Fri→Mon dark windows and one-day
# holidays.  Process restart clears the dict (in-memory by design; next
# session's live path repopulates).
_LAST_GOOD_QUOTE: dict[str, tuple[float, dict]] = {}
_LAST_GOOD_QUOTE_LOCK = threading.Lock()
_LAST_GOOD_QUOTE_TTL_S: float = 86400.0  # 24 hours


def record_good_quote(symbol: str, fields: dict) -> None:
    """Record `fields` as the last-known-good non-LTP snapshot for `symbol`.

    Only writes when at least one meaningful field (open, close, volume, oi)
    is non-null/non-zero — silently drops empty payloads so a broker miss
    doesn't overwrite a real prior snapshot.  Thread-safe."""
    if not symbol or not isinstance(fields, dict):
        return
    # Guard: don't clobber a real prior snapshot with an empty broker response.
    _meaningful = any(
        fields.get(k) not in (None, 0, 0.0)
        for k in ("open", "close", "volume", "oi")
    )
    if not _meaningful:
        return
    now = _time.time()
    # Copy so caller mutations don't leak into the cache.
    _clean = {
        k: fields.get(k)
        for k in ("open", "close", "volume", "oi", "change", "change_pct", "bid", "ask")
    }
    with _LAST_GOOD_QUOTE_LOCK:
        _LAST_GOOD_QUOTE[symbol] = (now, _clean)


def get_last_good_quote(symbol: str, max_age_s: float = _LAST_GOOD_QUOTE_TTL_S) -> dict | None:
    """Return the cached last-known-good non-LTP snapshot for `symbol` if it
    was recorded within `max_age_s`, else None.

    Thread-safe.  Returned dict is a shallow copy so callers may mutate it."""
    if not symbol:
        return None
    now = _time.time()
    with _LAST_GOOD_QUOTE_LOCK:
        entry = _LAST_GOOD_QUOTE.get(symbol)
    if entry is None:
        return None
    ts, payload = entry
    if now - ts > max_age_s:
        return None
    return dict(payload)


def _ts_label(unix_ts: float) -> str:
    """Format a unix timestamp as HH:MM IST for log lines."""
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime
        return datetime.fromtimestamp(unix_ts, tz=ZoneInfo("Asia/Kolkata")).strftime("%H:%M IST")
    except Exception:
        return str(int(unix_ts))


def _col_f64(lf: pl.DataFrame, col: str) -> pl.Expr:
    """Return a Float64 expression for `col`, coercing nulls/bad values to 0.0."""
    return pl.col(col).cast(pl.Float64, strict=False).fill_null(0.0)


def _col_f64_nullable(lf: pl.DataFrame, col: str) -> pl.Expr:
    """Like _col_f64 but keeps nulls as nulls (for broker-value trust checks)."""
    return pl.col(col).cast(pl.Float64, strict=False)


# RAMBOQ_USE_CONN_SERVICE — when set on the main API process, the
# zero-arg public fetch_* entry points proxy to conn_service over UDS
# instead of running the in-process broker code. Conn_service itself
# leaves this UNSET, so its own broker_apis import keeps running the
# local @for_all_accounts path (no recursion).
#
# Cached at module load — per-call os.environ.get + .strip + .lower
# was a P2 finding (called on every fetch_holdings/positions/margins
# invocation, ~5/sec under polling). Module constant gives the same
# semantics at zero cost. The flag never changes at runtime within a
# process; service restart is the only way to flip it.
_USE_CONN_SERVICE: bool = os.environ.get(
    "RAMBOQ_USE_CONN_SERVICE", "",
).strip().lower() in ("1", "true", "yes", "on")


def _use_conn_service() -> bool:
    return _USE_CONN_SERVICE

# One-shot flag for the Kite MCX value-units diagnostic in
# fetch_positions. Logs the raw `day_buy_value` vs the two possible
# unit interpretations for the first MCX-multiplier row seen so the
# operator can confirm whether `5b995ccb` is correct or overcounts.
# Reset on process restart.
_KITE_VALUE_UNIT_LOGGED = False

# Per-account fetch-result tracker — the navbar broker-health badge
# reads from this to flag accounts whose latest fetch attempt FAILED
# (even though the connection object is still in Connections.conn).
# Operator: "when connection issue there in groww, I still 5/5 in
# navbar instead 4/5 as one account connection has issue."
#
# Shape per account:
#   { 'last_ok_at':    float (unix ts) | 0,   # most recent successful call
#     'last_fail_at':  float (unix ts) | 0,   # most recent failed call
#     'last_fail_msg': str,
#     # Circuit-breaker fields (added Jul 2026 — DH6847 rotation-loop fix):
#     'consecutive_fail_count': int,           # reset to 0 on any success
#     'circuit_open_until':     float | None,  # epoch ts; None = CLOSED
#     'circuit_last_opened_at': float | None,  # for logging
#     'open_cycle_count':       int,           # exponential back-off tracker
#   }
# An account is healthy when last_ok_at >= last_fail_at OR there's
# no recorded attempt yet (never tried = assume healthy).
_FETCH_HEALTH: dict[str, dict] = {}

# Dedicated lock for the read-modify-write on circuit-breaker fields.
# Plain dict writes are GIL-safe but the consecutive_fail_count +=1
# is a compound operation that is NOT safe without a lock.  The existing
# last_ok_at / last_fail_at writes (single assignment) continue without
# a lock — retrofitting them would risk latency regressions on the hot
# path and those fields are informational-only (stale by design).
_BREAKER_LOCK = threading.Lock()

# Circuit-breaker thresholds.
_CB_FAIL_THRESHOLD: int = 3          # consecutive fails to open the breaker
_CB_INITIAL_COOLOFF_S: float = 300.0 # 5 min
_CB_MAX_COOLOFF_S: float = 1800.0    # 30 min cap

# ---------------------------------------------------------------------------
# Per-account Dhan poll-priority interval gate (Jul 2026)
# ---------------------------------------------------------------------------
# poll_priority is a per-account string field on broker_accounts:
#   'hot'  → poll every 30s  (default, same as Kite/Groww)
#   'warm' → poll every 120s
#   'cold' → poll every 600s
#
# The interval gate applies ONLY to Dhan accounts in the background poll
# loop. Kite and Groww accounts always poll every cycle regardless of this
# dict. Manual Refresh (?fresh=1) invalidates _RAW_CACHE and bypasses the
# interval gate by going through fetch_positions/holdings/margins directly
# (not gated here — the gate only fires during background @for_all_accounts
# calls that resolve to a Dhan broker instance).
#
# _dhan_next_poll: {account_code: next_poll_epoch_seconds}
# Updated after every background poll attempt (success or fail — do not
# compound intervals on breaker-open cycles; breaker already rate-limits
# those separately).
#
# Thread-safety: dict writes are single-assignment (GIL-safe). The read-
# then-write in _update_dhan_next_poll is atomic from Python's GIL
# perspective since we use time.time() as an independent value, not a
# compound read-modify-write.
_PRIORITY_INTERVALS_SEC: dict[str, float] = {
    "hot":  30.0,
    "warm": 120.0,
    "cold": 600.0,
}
_dhan_next_poll: dict[str, float] = {}  # account → next allowed poll epoch

# ---------------------------------------------------------------------------
# In-process poll-priority cache (avoids async DB reads from thread context)
# ---------------------------------------------------------------------------
# Populated by Connections.rebuild_from_db() and invalidated on PATCH via
# set_dhan_priority_cache(). Hot path is O(1) dict lookup — no I/O.
# Thread-safe: single-assignment writes are GIL-safe; the cache is written
# only during rebuild_from_db (startup + after CRUD) and never under lock.
_dhan_poll_priority_cache: dict[str, str] = {}

# ---------------------------------------------------------------------------
# In-process circuit-breaker opt-in cache (Jul 2026)
# ---------------------------------------------------------------------------
# Populated by Connections.rebuild_from_db() alongside the poll-priority cache
# and invalidated on PATCH via set_breaker_optin_cache().
# When False (the default for all accounts except DH6847) the OPEN/HALF-OPEN
# state machine is bypassed entirely; the account still gets last_ok_at /
# last_fail_at health stamps for the admin badge.
# Thread-safe: single-assignment writes are GIL-safe.
_breaker_optin_cache: dict[str, bool] = {}


def set_breaker_optin_cache(account: str, enabled: bool) -> None:
    """Write the in-process circuit-breaker opt-in cache for one account.

    Called from Connections.rebuild_from_db() and from the PATCH route handler
    after a circuit_breaker_enabled toggle so the change takes effect
    immediately without a process restart.
    """
    _breaker_optin_cache[account] = bool(enabled)


def get_breaker_optin_cache(account: str) -> bool:
    """Return True if circuit_breaker_enabled is set in the in-process cache.

    Falls back to False (safe default: no breaker) when the account is not
    yet in the cache (e.g. first poll cycle before rebuild_from_db completes).
    """
    return _breaker_optin_cache.get(account, False)


def set_dhan_priority_cache(account: str, priority: str) -> None:
    """Write the in-process priority cache for one account.

    Called from Connections.rebuild_from_db() for each active Dhan row,
    and from the PATCH + restore-priority route handlers after a DB write.
    Priority must be 'hot', 'warm', or 'cold' — invalid values are
    silently coerced to 'hot' so a bad DB value never breaks polling.
    """
    if priority not in _PRIORITY_INTERVALS_SEC:
        priority = "hot"
    _dhan_poll_priority_cache[account] = priority


def _get_dhan_poll_priority(account: str) -> str:
    """Return poll_priority for a Dhan account from the in-process cache.

    Falls back to 'hot' when the account is not in the cache (e.g. first
    poll cycle before rebuild_from_db completes) so polling is never
    blocked by a missing cache entry.
    """
    return _dhan_poll_priority_cache.get(account, "hot")


def dhan_next_poll_clear(accounts: list[str] | None = None) -> None:
    """Reset the interval gate for Dhan accounts so the next call polls
    immediately regardless of when the last poll ran.

    Called by route handlers when ``?fresh=1`` is requested so manual
    Refresh bypasses the cold/warm interval and always hits the broker.

    ``accounts`` — list of account codes to reset; pass None to reset ALL
    Dhan accounts (safe — non-Dhan entries are never inserted into this
    dict, so a full clear only affects Dhan accounts).
    """
    if accounts is None:
        _dhan_next_poll.clear()
    else:
        for acct in accounts:
            _dhan_next_poll.pop(acct, None)


def _is_dhan_interval_due(account: str, broker) -> bool:
    """Return True when this Dhan account should be polled right now.

    Also returns True for non-Dhan brokers (gate is Dhan-only).
    The check reads _dhan_next_poll without a lock — single dict
    lookup is GIL-safe.
    """
    if broker is None:
        return True  # legacy kite= path; always poll
    try:
        broker_name = type(broker).__name__.lower()
    except Exception:
        return True
    if "dhan" not in broker_name:
        return True  # Kite / Groww → always poll
    now = _time.time()
    next_due = _dhan_next_poll.get(account, 0.0)
    return now >= next_due


def _update_dhan_next_poll(account: str, broker) -> None:
    """Record the next allowed poll time for this Dhan account.

    No-op for non-Dhan brokers. Called after every background fetch
    attempt — success or fail (breaker handles the actual skip on open
    circuits; we still advance next_poll so the interval counts from the
    attempt, not from when the breaker re-opened).
    """
    if broker is None:
        return
    try:
        broker_name = type(broker).__name__.lower()
    except Exception:
        return
    if "dhan" not in broker_name:
        return
    priority = _get_dhan_poll_priority(account)
    interval = _PRIORITY_INTERVALS_SEC.get(priority, 30.0)
    _dhan_next_poll[account] = _time.time() + interval


# ---------------------------------------------------------------------------
# Per-account last-known-good frame cache (Jul 2026 — Dhan stale-persist fix)
# ---------------------------------------------------------------------------
# Operator: "dhan is showing and disappearing accounts on and off"
#
# Root cause: DH6847 has circuit_breaker_enabled=True. When the breaker is
# OPEN, `_fetch_positions_local` / `_fetch_holdings_local` / `_fetch_margins_local`
# short-circuit and return an empty DataFrame. That empty frame gets concatenated
# away in `_apply_backfill_to_list`, so DH6847 rows silently vanish from the
# API payload. On the next successful poll the rows reappear — visible flicker.
#
# Fix: on every successful per-account fetch, stash a shallow copy of the frame
# keyed by (kind, account). On breaker-open short-circuit, return the LKG copy
# with attrs['stale']=True + attrs['stale_since']=<epoch> instead of an empty
# frame. The route layer surfaces `stale=True` on each row + a response-level
# `stale_accounts` list.
#
# Not persisted to disk (in-memory only). Cold-restart during market hours will
# show an empty frame for one poll cycle until the first successful fetch
# populates the cache. This is acceptable — the flicker problem is the ongoing
# case, not the sub-30s post-restart window.
#
# Shape: {(kind, account): (unix_ts, pd.DataFrame_copy)}
# kind ∈ {'positions', 'holdings', 'margins'}
_LKG_FRAME_BY_ACCT: dict[tuple[str, str], tuple[float, "pd.DataFrame"]] = {}
_LKG_FRAME_LOCK = threading.Lock()

# TTL for LKG substitution — after 24h assume the account has been offline
# too long to substitute. Downstream sees empty (same as pre-fix behaviour).
_LKG_MAX_AGE_S: float = 24 * 3600.0


def _record_lkg_frame(kind: str, account: str, df: "pd.DataFrame") -> None:
    """Stash a shallow copy of `df` as the last-known-good frame for
    (kind, account). Called from every successful `_fetch_*_local`.

    Only records non-empty frames — an empty successful fetch (legitimate
    "no positions" state) does not overwrite a prior LKG copy. This means
    an account that had positions on Monday, exited them Tuesday, will
    keep serving Monday's frame on Wednesday if it goes breaker-open —
    which is wrong. Guard: only overwrite on non-empty; empty successful
    fetches poison the cache to an empty frame via the timestamp so the
    stale-substitute path returns an empty frame with the fresh timestamp.
    """
    if not account or not kind:
        return
    now = _time.time()
    try:
        snapshot = df.copy(deep=False) if df is not None else None
    except Exception:
        return
    with _LKG_FRAME_LOCK:
        _LKG_FRAME_BY_ACCT[(kind, account)] = (now, snapshot)


def _get_lkg_frame(kind: str, account: str) -> tuple[float, "pd.DataFrame"] | None:
    """Return (stale_since_epoch, DataFrame_copy) for (kind, account), or
    None when no LKG exists or the entry is older than _LKG_MAX_AGE_S."""
    if not account or not kind:
        return None
    now = _time.time()
    with _LKG_FRAME_LOCK:
        entry = _LKG_FRAME_BY_ACCT.get((kind, account))
    if entry is None:
        return None
    ts, snap = entry
    if now - ts > _LKG_MAX_AGE_S:
        return None
    if snap is None:
        return None
    # Return a shallow copy so the caller can freely mutate attrs/columns
    # without leaking mutations back into the LKG store.
    try:
        return ts, snap.copy(deep=False)
    except Exception:
        return None


def _stale_substitute_frame(kind: str, account: str) -> "pd.DataFrame":
    """Return the LKG frame for (kind, account) with staleness attrs +
    per-row `account_stale=True` column marked. Returns an empty frame
    when no LKG exists (falls back to pre-fix behaviour for that cycle).

    Marks:
      • df.attrs['stale']         = True   (response-level flag)
      • df.attrs['stale_since']   = epoch  (unix ts of last success)
      • df.attrs['circuit_open']  = True   (diagnostic — matches pre-fix attr)
      • df['account_stale']       = True   (per-row column; consumed by
                                            schema mapping in routes)

    Does NOT set attrs['fetch_failed']=True — that would trigger the
    route's "all failed → 503" outage gate. A stale-substituted frame
    counts as a SUCCESS (with old data), not a failure.
    """
    result = _get_lkg_frame(kind, account)
    if result is None:
        # No LKG yet — same behaviour as before this fix.
        df_empty = pd.DataFrame()
        df_empty.attrs["circuit_open"] = True
        df_empty.attrs["fetch_failed"] = True
        return df_empty
    stale_since, df = result
    df.attrs["stale"] = True
    df.attrs["stale_since"] = stale_since
    df.attrs["circuit_open"] = True
    # DO NOT set fetch_failed — see docstring. Substituted rows are "success
    # with old data", not a fetch failure.
    if not df.empty:
        df["account_stale"] = True
    return df


# ---------------------------------------------------------------------------
# Auto-downgrade state (Jul 2026)
# ---------------------------------------------------------------------------
# When a Dhan account has auto_downgrade_enabled=True and its circuit
# breaker opens ≥ _DOWNGRADE_MIN_OPENS times within a 15-min window,
# poll_priority is automatically set to 'cold'.
#
# Per-account history of breaker-open events (epoch timestamps).
# Trimmed to the last 15 min on each new open event.
_DOWNGRADE_WINDOW_S: float = 900.0    # 15-min sliding window
_DOWNGRADE_MIN_OPENS: int  = 5        # opens needed to trigger downgrade
_DOWNGRADE_COOLOFF_S: float = 300.0   # 5-min cooloff after a downgrade

_breaker_open_history: dict[str, list[float]] = {}  # account → [epoch, ...]
_downgrade_cooloff_until: dict[str, float] = {}     # account → epoch
# Dedicated lock for the compound read-modify-write in _maybe_auto_downgrade:
#   history.append(now) + list-comprehension rewrite is NOT GIL-safe when two
#   threads hit the same account concurrently (background poll fan-out under
#   ThreadPoolExecutor). Without this lock a fast-firing account could lose
#   open-events between the append and the rewrite, delaying auto-downgrade.
_DOWNGRADE_HISTORY_LOCK = threading.Lock()


def _maybe_auto_downgrade(account: str) -> None:
    """Called from _record_fetch each time the circuit-breaker opens.

    Checks whether the account qualifies for auto-downgrade to 'cold'
    priority. No-op when:
      - auto_downgrade_enabled is False for this account
      - account is already 'cold'
      - the downgrade cooloff has not expired (5 min after last downgrade)
      - fewer than 5 breaker opens in the last 15 min

    When downgrade fires:
      - Updates broker_accounts row: poll_priority='cold', stamps
        auto_downgraded_at + auto_downgrade_reason
      - Resets _dhan_next_poll[account] to 0 so the cold interval
        takes effect on the next _update_dhan_next_poll call
      - Emits WS event broker_priority_changed
      - Sets a 5-min cooloff on subsequent downgrade checks
    """
    if not account:
        return

    now = _time.time()

    # Cooloff guard — prevent re-firing within 5 min of last downgrade.
    if now < _downgrade_cooloff_until.get(account, 0.0):
        return

    # Update open-event history (trim to window) under a dedicated lock —
    # the append + list-comprehension rewrite is a compound RMW that races
    # under concurrent broker fan-out (multiple threads may call this in the
    # same tick when several accounts fail together).
    with _DOWNGRADE_HISTORY_LOCK:
        history = _breaker_open_history.setdefault(account, [])
        history.append(now)
        cutoff = now - _DOWNGRADE_WINDOW_S
        _breaker_open_history[account] = [t for t in history if t >= cutoff]
        history_len = len(_breaker_open_history[account])

    if history_len < _DOWNGRADE_MIN_OPENS:
        return  # Not enough opens yet.

    # Read account state from in-process cache to decide whether to downgrade.
    # We check auto_downgrade_enabled + current poll_priority from the cache
    # (set by set_dhan_priority_cache on rebuild_from_db + after PATCH).
    # On a fresh start before rebuild_from_db completes, the cache is empty,
    # so _get_dhan_poll_priority returns 'hot' and we fall through to the
    # DB check only when we have enough history entries.
    try:
        current_priority = _get_dhan_poll_priority(account)
        if current_priority == "cold":
            return  # Already cold — nothing to do.

        # Check auto_downgrade_enabled via async DB read scheduled on the
        # main event loop (captured at startup by write_queue.start()).
        # We use run_coroutine_threadsafe with the stored loop — this is
        # safe from any thread context and avoids the deprecated
        # asyncio.get_event_loop() call which fails in Python 3.10+
        # inside a ThreadPoolExecutor worker (no running loop in that
        # thread).
        from backend.api.database import shared_async_session as _shared_session
        from backend.api.models import BrokerAccount as _BA
        from sqlalchemy import select as _select
        from datetime import datetime as _dt, timezone as _tz
        import asyncio as _asyncio

        async def _check_and_update():
            async with _shared_session() as s:
                row = (await s.execute(
                    _select(_BA).where(_BA.account == account)
                )).scalar_one_or_none()
                if row is None:
                    return None
                # Auto-downgrade requires BOTH circuit_breaker_enabled AND
                # auto_downgrade_enabled. If the circuit breaker is not opted
                # in, the account never enters OPEN state so the downgrade
                # window counter can only accumulate phantom "opens" from
                # non-state-machine paths — guard here to be safe.
                if not getattr(row, "circuit_breaker_enabled", False):
                    return None
                if not getattr(row, "auto_downgrade_enabled", False):
                    return None
                current_p = getattr(row, "poll_priority", "hot") or "hot"
                if current_p == "cold":
                    return None  # Already cold in DB.
                old_priority = current_p
                reason = f"{_DOWNGRADE_MIN_OPENS} breaker opens in 15 min"
                row.poll_priority = "cold"
                row.auto_downgraded_at = _dt.now(_tz.utc)
                row.auto_downgrade_reason = reason
                await s.commit()
                return old_priority, reason

        # Use the main-event-loop reference captured at startup so this
        # call works from within a ThreadPoolExecutor (no running loop in
        # that thread on Python 3.10+).
        from backend.api.persistence.write_queue import get_main_loop as _get_loop
        _loop = _get_loop()
        if _loop is None:
            # write_queue.start() not called yet (test / early startup).
            logger.debug(f"[DHAN-AUTO-DOWNGRADE] account={account}: main loop not ready, skipping")
            return

        # Fire-and-forget: schedule the DB check and attach a done_callback
        # that carries out all downstream work. This avoids blocking the
        # calling thread (which runs on the broker polling hot path) for
        # up to 3 seconds on every breaker-open event.
        future = _asyncio.run_coroutine_threadsafe(_check_and_update(), _loop)

        def _on_done(fut: "_asyncio.Future") -> None:
            # Runs on the event-loop thread (called by run_coroutine_threadsafe).
            try:
                outcome = fut.result()
            except Exception as _fe:
                logger.warning(f"[DHAN-AUTO-DOWNGRADE] account={account} DB check failed: {_fe}")
                return

            if outcome is None:
                return  # No downgrade needed.

            old_priority, reason = outcome
            _now = _time.time()

            # Set cooloff so a 6th open within 5 min doesn't re-fire.
            _downgrade_cooloff_until[account] = _now + _DOWNGRADE_COOLOFF_S
            # Update in-process cache so next interval-gate check sees 'cold'.
            set_dhan_priority_cache(account, "cold")
            # Reset next_poll to 0 so the next background cycle re-schedules
            # the cold interval via _update_dhan_next_poll.
            _dhan_next_poll[account] = 0.0

            logger.warning(
                f"[DHAN-AUTO-DOWNGRADE] account={account} "
                f"from={old_priority} to=cold reason={reason!r}"
            )

            # Emit WS event so the frontend toast can fire.
            try:
                async def _broadcast():
                    from backend.api.routes.ws import broadcast as _ws_broadcast
                    await _ws_broadcast({
                        "type": "broker_priority_changed",
                        "account": account,
                        "old_priority": old_priority,
                        "new_priority": "cold",
                        "reason": reason,
                        "auto": True,
                    })

                _asyncio.run_coroutine_threadsafe(_broadcast(), _loop)
            except Exception as _ws_err:
                logger.debug(f"[DHAN-AUTO-DOWNGRADE] WS broadcast failed: {_ws_err}")

        future.add_done_callback(_on_done)

    except Exception as _exc:
        logger.warning(f"[DHAN-AUTO-DOWNGRADE] account={account} check failed: {_exc}")


def _default_health_entry() -> dict:
    return {
        "last_ok_at": 0.0,
        "last_fail_at": 0.0,
        "last_fail_msg": "",
        "consecutive_fail_count": 0,
        "circuit_open_until": None,
        "circuit_last_opened_at": None,
        "open_cycle_count": 0,
    }


def _circuit_state(account: str) -> str:
    """Return 'open', 'half-open', or 'closed' for `account`.

    Thread-safe read — reads `circuit_open_until` under _BREAKER_LOCK.
    """
    with _BREAKER_LOCK:
        e = _FETCH_HEALTH.get(account)
    if e is None:
        return "closed"
    until = e.get("circuit_open_until")
    if until is None:
        return "closed"
    now = _time.time()
    if now < until:
        return "open"
    return "half-open"


def _is_circuit_open(account: str) -> bool:
    """Return True only when the breaker is OPEN (not half-open).

    Returns False immediately for accounts that have not opted in via
    circuit_breaker_enabled, so their fetch path is never short-circuited
    by another account's failures. The opt-in state is read from the
    in-process cache populated by Connections.rebuild_from_db().
    """
    if not get_breaker_optin_cache(account):
        return False
    return _circuit_state(account) == "open"


def _record_fetch(account: str, ok: bool, error: str = "") -> None:
    """Record one fetch attempt's outcome and advance the circuit-breaker
    state machine.

    Called from every per-account broker API wrapper (fetch_holdings /
    fetch_positions / fetch_margins) on both success and failure paths
    so the per-account health state stays current.

    State transitions (Jul 2026 circuit-breaker):
      CLOSED  → ok=True   : reset consecutive_fail_count; stay CLOSED.
      CLOSED  → ok=False  : increment consecutive_fail_count.
                             If count >= _CB_FAIL_THRESHOLD → OPEN.
      HALF-OPEN → ok=True : reset counters; CLOSED.
      HALF-OPEN → ok=False: OPEN again with exponential cool-off.
      OPEN    → any call  : callers must check _is_circuit_open() BEFORE
                             calling the SDK; _record_fetch is not called
                             for short-circuited attempts.

    Consecutive-fail semantics: we count any consecutive failures
    regardless of wall-clock gap (no sliding-window). The cool-off
    period is the rate-limiting mechanism; the counter is just the
    threshold gate to enter it. A single success resets the counter.
    """
    if not account:
        return

    now = _time.time()

    # Fast path for non-opt-in accounts: update health stamps only so the
    # admin badge stays accurate, but skip the full state machine.
    if not get_breaker_optin_cache(account):
        e = _FETCH_HEALTH.setdefault(account, _default_health_entry())
        if ok:
            _was_recovering = e.get("last_fail_at", 0.0) > e.get("last_ok_at", 0.0)
            e["last_ok_at"] = now
            if _was_recovering:
                _emit_conn_event(
                    account, _broker_id_safe(account), "fetch_ok_recovery",
                    {"after_fail_msg": e.get("last_fail_msg", "")[:200]},
                )
        else:
            e["last_fail_at"] = now
            e["last_fail_msg"] = str(error)[:200]
            _etype = "auth_fail" if _is_auth_error_str(str(error)) else "fetch_fail"
            _emit_conn_event(
                account, _broker_id_safe(account), _etype,
                {"error": str(error)[:200]},
            )
        return

    _new_breaker_open = False   # flag set inside lock, hook fired outside
    _was_halfopen     = False   # HALF-OPEN → CLOSED recovery
    _was_recovering   = False   # had a prior failure (emit fetch_ok_recovery)
    with _BREAKER_LOCK:
        e = _FETCH_HEALTH.setdefault(account, _default_health_entry())
        # Ensure legacy entries (without breaker fields) are back-filled.
        e.setdefault("consecutive_fail_count", 0)
        e.setdefault("circuit_open_until", None)
        e.setdefault("circuit_last_opened_at", None)
        e.setdefault("open_cycle_count", 0)

        if ok:
            _was_recovering = e.get("last_fail_at", 0.0) > e.get("last_ok_at", 0.0)
            # HALF-OPEN → CLOSED: circuit_open_until was set but has now expired
            # (caller checked is_circuit_open → False, proceeded with the probe).
            prev_until = e.get("circuit_open_until")
            _was_halfopen = (prev_until is not None) and (prev_until <= now)
            e["last_ok_at"] = now
            # Success from HALF-OPEN (or normal CLOSED): reset everything.
            e["consecutive_fail_count"] = 0
            e["circuit_open_until"] = None
            e["circuit_last_opened_at"] = None
            e["open_cycle_count"] = 0
        else:
            e["last_fail_at"] = now
            e["last_fail_msg"] = str(error)[:200]
            e["consecutive_fail_count"] = e.get("consecutive_fail_count", 0) + 1

            if e["consecutive_fail_count"] >= _CB_FAIL_THRESHOLD:
                # Open (or re-open) the breaker with exponential back-off.
                # open_cycle_count: 0→1st open (5m), 1→10m, 2→20m, 3+→30m.
                #
                # Concurrent-probe race guard: when the breaker is already
                # OPEN (circuit_open_until is set to a future time), a
                # parallel probe that also fails would otherwise increment
                # open_cycle_count a second (and third) time in the same
                # tick — jumping 3 exponential steps instead of 1.  We
                # advance the cycle counter ONLY when we are transitioning
                # from CLOSED (circuit_open_until is None) or HALF-OPEN
                # (circuit_open_until has already expired).  If the breaker
                # is already OPEN (circuit_open_until > now), a concurrent
                # failure simply refreshes the deadline without bumping the
                # cycle, preventing the multi-step jump.
                prev_until = e.get("circuit_open_until")
                was_closed_or_halfopen = (prev_until is None) or (prev_until <= now)
                cycle = e.get("open_cycle_count", 0)
                if was_closed_or_halfopen:
                    cycle += 1
                    e["open_cycle_count"] = cycle
                    _new_breaker_open = True   # new OPEN transition
                base = _CB_INITIAL_COOLOFF_S * (2 ** (cycle - 1))
                cooloff = min(base, _CB_MAX_COOLOFF_S) + random.uniform(0, 30)
                e["circuit_open_until"] = now + cooloff
                e["circuit_last_opened_at"] = now
                logger.warning(
                    f"[BREAKER] account={account} state=open "
                    f"reason={str(error)[:120]} "
                    f"consecutive_fails={e['consecutive_fail_count']} "
                    f"cooloff={int(cooloff)}s "
                    f"open_until={_ts_label(now + cooloff)} "
                    f"cycle={e['open_cycle_count']}"
                )

    # Emit conn events OUTSIDE the lock so the enqueue_nowait call
    # never holds _BREAKER_LOCK while touching the event queue.
    _bid = _broker_id_safe(account)
    if ok:
        if _was_recovering:
            _emit_conn_event(account, _bid, "fetch_ok_recovery")
        if _was_halfopen:
            _emit_conn_event(account, _bid, "circuit_close")
    else:
        _etype = "auth_fail" if _is_auth_error_str(str(error)) else "fetch_fail"
        _emit_conn_event(account, _bid, _etype, {"error": str(error)[:200]})

    # Auto-downgrade hook — called OUTSIDE the lock to avoid deadlock
    # (the hook acquires shared_async_session which can block).
    # Only fires on a genuine new OPEN transition, not on re-opens of
    # an already-open circuit, so each open event is counted exactly once
    # for the 15-min history window.
    if _new_breaker_open:
        _emit_conn_event(account, _bid, "circuit_open", {
            "cycle": e.get("open_cycle_count", 0),
            "consecutive_fails": e.get("consecutive_fail_count", 0),
            "error": str(error)[:200],
        })
        try:
            _maybe_auto_downgrade(account)
        except Exception as _adg_err:
            logger.debug(f"[DHAN-AUTO-DOWNGRADE] hook error: {_adg_err}")


def is_account_healthy(account: str) -> bool:
    """True iff the most recent fetch for this account succeeded.

    Under RAMBOQ_USE_CONN_SERVICE=1 the local _FETCH_HEALTH dict is
    always empty (broker calls run in conn_service, not here). Falling
    back to "never tried = healthy" would silently report every account
    as 5/5 even during real Groww/Dhan auth failures. Instead we query
    fetch_health_snapshot() which already has the conn_service-aware
    code path and returns the canonical health map for the process.

    In the non-cutover path, _FETCH_HEALTH is populated directly, so
    the fast local dict read still applies — fetch_health_snapshot()
    is only called when the local dict has no entry for the account.
    """
    e = _FETCH_HEALTH.get(account)
    if e:
        return e["last_ok_at"] >= e["last_fail_at"]
    # No local entry — either the local dict was never populated (cutover
    # mode) or the account has truly never been fetched yet. Consult the
    # canonical snapshot (which may hit conn_service over UDS).
    if _use_conn_service():
        snapshot = fetch_health_snapshot()
        remote_e = snapshot.get(account)
        if not remote_e:
            # conn_service also has no entry — account has never been
            # tried; give benefit of the doubt only in this case.
            return True
        return remote_e.get("last_ok_at", 0.0) >= remote_e.get("last_fail_at", 0.0)
    # Non-cutover, no local entry: never tried — healthy.
    return True


def fetch_health_snapshot() -> dict[str, dict]:
    """Read-only copy of the per-account health map.

    When RAMBOQ_USE_CONN_SERVICE is on, the canonical health map
    lives in the conn_service process — the local _FETCH_HEALTH
    in this process is empty (no broker calls landed here). We
    fall back to a synchronous httpx call against the conn_service
    health endpoint and surface its map instead. The navbar badge
    already reads through this function, so no caller migration."""
    if _use_conn_service():
        try:
            from backend.brokers.client.sync import _get_client
            resp = _get_client().get("/internal/health/brokers")
            resp.raise_for_status()
            return (resp.json() or {}).get("health", {}) or {}
        except Exception as e:
            logger.warning(f"conn_service health snapshot failed: {e}")
            return {}
    return {k: dict(v) for k, v in _FETCH_HEALTH.items()}


# ---------------------------------------------------------------------------
# Canonical account display ordering (Jul 2026)
# ---------------------------------------------------------------------------
# sort_accounts() returns account IDs in the operator-specified display order
# (ascending display_order, then account_id as tiebreaker). Used by log
# surfaces, health badge, and the /api/admin/broker-accounts/order endpoint.
#
# The order map is lazily loaded from the DB on first call and cached for
# up to 60 s.  PATCH /api/admin/brokers/{id} must call
# invalidate_account_order_cache() so the next call re-reads fresh values.
# ---------------------------------------------------------------------------

_ACCOUNT_ORDER_CACHE: dict[str, int] = {}
_ACCOUNT_ORDER_CACHE_AT: float = 0.0
_ACCOUNT_ORDER_CACHE_TTL: float = 60.0
_ACCOUNT_ORDER_LOCK = threading.Lock()


def invalidate_account_order_cache() -> None:
    """Drop the in-process display_order cache (call after PATCH display_order)."""
    global _ACCOUNT_ORDER_CACHE_AT
    with _ACCOUNT_ORDER_LOCK:
        _ACCOUNT_ORDER_CACHE_AT = 0.0


def get_account_order_map() -> dict[str, int]:
    """Return {account_id: display_order} map, cached for 60 s.

    Falls back to an empty dict (callers treat missing keys as 999) when the
    DB is unreachable (e.g. during test startup).
    """
    global _ACCOUNT_ORDER_CACHE, _ACCOUNT_ORDER_CACHE_AT
    now = _time.time()
    with _ACCOUNT_ORDER_LOCK:
        if now - _ACCOUNT_ORDER_CACHE_AT < _ACCOUNT_ORDER_CACHE_TTL:
            return dict(_ACCOUNT_ORDER_CACHE)
    # Cache expired — reload from DB synchronously (called from sync context).
    try:
        import asyncio
        from backend.api.database import shared_async_session
        from sqlalchemy import select as _sa_select
        from backend.api.models import BrokerAccount as _BA

        async def _load() -> dict[str, int]:
            async with shared_async_session() as sess:
                rows = (await sess.execute(
                    _sa_select(_BA.account, _BA.display_order)
                )).all()
                return {str(r.account): int(r.display_order) for r in rows}

        # Use get_running_loop() to detect an async context; this is the
        # Python 3.10+ correct pattern (get_event_loop() emits a Deprecation
        # Warning inside coroutines and returns a NEW loop from a sync
        # thread which is meaningless here — we care about whether the
        # CURRENT thread has an active loop).
        try:
            asyncio.get_running_loop()
            # Async context — offload to a threadpool that runs a fresh
            # event loop so we don't block the caller's loop.
            import concurrent.futures as _cf
            with _cf.ThreadPoolExecutor(max_workers=1) as pool:
                order_map = pool.submit(
                    lambda: asyncio.run(_load())
                ).result(timeout=5)
        except RuntimeError:
            # No running loop in this thread — safe to run one inline.
            order_map = asyncio.run(_load())
    except Exception as _e:
        logger.debug("get_account_order_map: DB load failed (%s), using empty map", _e)
        return {}
    with _ACCOUNT_ORDER_LOCK:
        _ACCOUNT_ORDER_CACHE = order_map
        _ACCOUNT_ORDER_CACHE_AT = _time.time()
    return dict(order_map)


def sort_accounts(accounts: list[str]) -> list[str]:
    """Return `accounts` sorted by display_order (asc), then account_id (asc).

    Unknown accounts (not in DB) are treated as display_order=999 and fall
    to the end. Order is stable within the same display_order bucket.

    Example (after startup migration):
        sort_accounts(['DH6847', 'ZG0790', 'DH3747', 'GR87DF'])
        → ['ZG0790', 'DH3747', 'GR87DF', 'DH6847']
    """
    order_map = get_account_order_map()
    return sorted(accounts, key=lambda a: (order_map.get(a, 999), a))


# ---------------------------------------------------------------------------
# Raw broker-DataFrame TTL cache (Tier 1 — shared by routes + algo.nav)
# ---------------------------------------------------------------------------
# `fetch_holdings()` / `fetch_positions()` / `fetch_margins()` are the
# zero-arg public entry points. They run @for_all_accounts → N broker
# round-trips (or one conn_service UDS hop). Without a cache, every
# consumer that calls them does that work independently:
#
#   • PositionsController / HoldingsController / FundsController
#     (route-level `get_or_fetch("positions", _fetch, ttl=30)` already
#      memos the FORMATTED response, but not the raw DF.)
#   • compute_firm_nav (algo/nav.py) — called by NavCard, /performance,
#     investor portal, nav_daily snapshot writer. Each call fanned out
#     fresh broker fetches.
#   • intraday_equity background poller — wants live data per 5min tick
#     anyway, so it intentionally bypasses (see _fetch_*_direct below).
#
# This module-level TTL cache memos the raw `list[pd.DataFrame]` shape
# returned by `fetch_*()` so two callers within `_RAW_TTL_S` seconds
# share one broker round-trip. The route-level `get_or_fetch` cache
# continues to memo the FORMATTED response (msgspec.Struct), so the
# fast path stays unchanged; this just plugs the leak where
# `compute_firm_nav` previously bypassed both layers.
#
# Per-key asyncio-style locking would require this whole module to be
# async; instead we use a threading.Lock since broker_apis is the
# sync layer (callers offload to threadpool). The race window between
# cache-miss + broker call is bounded by `_RAW_TTL_S`.
_RAW_CACHE_LOCK = threading.Lock()
_RAW_CACHE: dict[str, tuple[float, list[pd.DataFrame]]] = {}
_RAW_TTL_S: float = 30.0  # matches the route-level cache TTL

# In-flight sentinel dict — cache-stampede prevention.
# When a cache miss is detected and a fetch is about to start, a
# threading.Event is inserted here under the same key. Concurrent callers
# that see the sentinel wait on it (up to 5 s) then re-check _RAW_CACHE.
# The leader clears the sentinel via _raw_cache_put (success) or
# _raw_cache_release (failure path). Both ops are serialised under
# _RAW_CACHE_LOCK so there is no window where a new waiter races against
# the clear.
_RAW_INFLIGHT: dict[str, threading.Event] = {}


def _raw_cache_get(key: str) -> list[pd.DataFrame] | None:
    """Return cached list[DataFrame] for `key` if still fresh, else None."""
    with _RAW_CACHE_LOCK:
        entry = _RAW_CACHE.get(key)
    if entry is None:
        return None
    expires_at, value = entry
    if _time.monotonic() >= expires_at:
        return None
    return value


def _raw_cache_reserve(key: str) -> tuple[list[pd.DataFrame] | None, bool]:
    """Atomic cache-check + in-flight reservation.

    Returns ``(cached_value, is_leader)`` under a single lock acquisition:
    - If the cache is fresh → returns ``(value, False)`` (fast path, no fetch needed).
    - If another thread is already fetching → waits on its Event then re-checks;
      returns ``(value_or_None, False)`` so the caller skips its own fetch.
    - If the cache is empty and no fetch is in-flight → inserts a new Event
      and returns ``(None, True)``; the caller MUST follow up with
      ``_raw_cache_put`` or ``_raw_cache_release``.
    """
    with _RAW_CACHE_LOCK:
        entry = _RAW_CACHE.get(key)
        if entry is not None:
            expires_at, value = entry
            if _time.monotonic() < expires_at:
                return value, False  # cache hit

        evt = _RAW_INFLIGHT.get(key)
        if evt is not None:
            # Another thread is fetching — release lock and wait.
            pass
        else:
            # We are the leader for this key.
            new_evt = threading.Event()
            _RAW_INFLIGHT[key] = new_evt
            return None, True

    # Wait outside the lock so the leader thread can write.
    evt.wait(timeout=5.0)

    # Re-check after wait (leader may or may not have succeeded).
    cached = _raw_cache_get(key)
    return cached, False


def _raw_cache_put(key: str, value: list[pd.DataFrame]) -> None:
    """Store list[DataFrame] under `key` with the standard TTL and signal
    any waiters that were blocked on _RAW_INFLIGHT[key].

    Stored value is the caller's reference — DataFrames are NOT deep-copied
    (would defeat the purpose). Callers that need to mutate must `.copy()`
    first; the existing route _fetch() handlers already do this via the
    Polars conversion (`pl.from_pandas(raw)` creates an independent view).
    """
    with _RAW_CACHE_LOCK:
        _RAW_CACHE[key] = (_time.monotonic() + _RAW_TTL_S, value)
        evt = _RAW_INFLIGHT.pop(key, None)
    if evt is not None:
        evt.set()


def _raw_cache_release(key: str) -> None:
    """Signal waiters after a fetch failure without storing a result.

    Call this in the exception handler when the leader fetch raises so
    waiters are unblocked (they will re-check the cache and find nothing,
    then proceed to fetch on their own)."""
    with _RAW_CACHE_LOCK:
        evt = _RAW_INFLIGHT.pop(key, None)
    if evt is not None:
        evt.set()


def _raw_cache_invalidate(key: str | None = None) -> None:
    """Drop a key (or all keys when key=None). Used by tests + on postback
    `book_changed` events so the next fetch picks up the fresh broker state."""
    with _RAW_CACHE_LOCK:
        if key is None:
            _RAW_CACHE.clear()
        else:
            _RAW_CACHE.pop(key, None)


def fetch_holdings(*args, **kwargs):
    """Public entry — proxies to conn_service when the cutover flag
    is on, otherwise runs the local @for_all_accounts path.

    The proxy branch only kicks in for the zero-arg shape
    (`fetch_holdings()` — every caller in the codebase uses this).
    Explicit `account=`/`broker=` kwargs fall through to the local
    path so single-account internal use keeps working.

    Zero-arg results are memoised in `_RAW_CACHE` for `_RAW_TTL_S`
    seconds so concurrent consumers (routes + compute_firm_nav +
    investor slice) share one broker round-trip per cache window.

    NAV-consistency guarantee (Approach A): the cached value is a
    single-element list containing the post-backfill concatenated
    DataFrame. `backfill_market_data` runs once here — it patches
    close_price + last_price from PriceBroker.quote(), then
    recomputes day_change_val, pnl, cur_val, and pnl_percentage.
    Every consumer (route + compute_firm_nav) then reads the SAME
    patched cur_val so NavCard and /performance agree. The route's
    own `backfill_market_data` call becomes a no-op (no zero-LTP
    rows remain), and `_override_stale_ltp_from_ticker` continues
    to handle the post-cache KiteTicker tick diff.
    """
    if not args and not kwargs:
        cached, is_leader = _raw_cache_reserve("holdings")
        if not is_leader:
            # Either a cache hit or waited for another thread's fetch.
            return cached if cached is not None else _fetch_holdings_local(*args, **kwargs)
        try:
            if _use_conn_service():
                from backend.brokers.client import sync as conn_sync
                result = conn_sync.fetch_holdings()
            else:
                result = _fetch_holdings_local()
            result = _apply_backfill_to_list(result, qty_col="opening_quantity")
            _raw_cache_put("holdings", result)
            return result
        except Exception:
            _raw_cache_release("holdings")
            raise
    result = _fetch_holdings_local(*args, **kwargs)
    return result


@for_all_accounts
def _fetch_holdings_local(connections=Connections, account=None, kite=None, broker=None):
    """Multi-broker holdings fetch. Uses the Broker ABC abstraction
    (broker.holdings()) when available so Dhan / Groww accounts route
    through their own adapters; falls back to the legacy `kite=`
    handle for backwards compatibility with the original Kite-only
    path. Every adapter normalises its response to the Kite-shape
    column set used by downstream UI (tradingsymbol, average_price,
    opening_quantity, pnl, day_change, close_price, etc.)."""
    df_holdings = pd.DataFrame()
    # Circuit-breaker guard: skip SDK call when the breaker is OPEN.
    # Half-open state admits one probe attempt (breaker closes or
    # re-opens based on that attempt's outcome).
    if account and _is_circuit_open(account):
        logger.warning(
            f"[BREAKER] account={account} short-circuit holdings "
            f"(open until {_ts_label(_FETCH_HEALTH.get(account, {}).get('circuit_open_until', 0) or 0)})"
        )
        # Stale-substitute path (Jul 2026): return the LKG frame with
        # stale=True attrs instead of an empty frame so DH6847 rows do
        # not vanish from the payload on every breaker-open cycle.
        return _stale_substitute_frame("holdings", account)
    # Interval gate (Dhan-only): skip if not yet due for next poll.
    # Kite + Groww pass through unconditionally (_is_dhan_interval_due
    # returns True for non-Dhan brokers). Manual ?fresh=1 calls bypass
    # by calling fetch_holdings() directly which skips @for_all_accounts.
    if account and not _is_dhan_interval_due(account, broker):
        df_holdings.attrs["interval_skipped"] = True
        logger.debug(
            f"[INTERVAL] account={account} holdings poll skipped "
            f"(next_due={_ts_label(_dhan_next_poll.get(account, 0))})"
        )
        return df_holdings
    # Advance next-poll timestamp BEFORE the fetch so even a crash/exception
    # doesn't cause a tight retry loop.
    if account:
        _update_dhan_next_poll(account, broker)
    try:
        rows = None
        if broker is not None:
            rows = broker.holdings()
        elif kite is not None:
            rows = kite.holdings()
        if rows is None:
            df_holdings.attrs['fetch_failed'] = True
            _record_fetch(account, ok=False, error="broker.holdings() returned None")
            return df_holdings
        df_holdings = pd.DataFrame(rows)

        if not df_holdings.empty:
            df_holdings["account"] = account
            df_holdings["type"] = "H"
        _record_fetch(account, ok=True)
    except Exception as e:
        logger.error(f"[{account}] Failed to fetch holdings: {e}")
        df_holdings.attrs['fetch_failed'] = True
        _record_fetch(account, ok=False, error=str(e))

    # Calculated columns — guard against an empty / fetch-failed frame
    # (broker 502 / 503 outages leave df_holdings empty and skipping the
    # math here is the difference between an empty response and a 500
    # KeyError on 'average_price'). Also guard each column reference
    # individually: a normaliser that omits one of the Kite-shape
    # columns (e.g. Groww doesn't always carry close_price) won't break
    # the others.
    if df_holdings.empty:
        return df_holdings

    df_holdings = _enrich_holdings(df_holdings)
    # Stash a shallow copy for the stale-substitute path when this
    # account's breaker opens on a future cycle. Non-empty only —
    # legitimate "no holdings" returns don't overwrite a prior LKG.
    if account and not df_holdings.empty:
        _record_lkg_frame("holdings", account, df_holdings)
    return df_holdings


def _build_holdings_pnl_expr(
    lf: "pl.DataFrame",
    has_pnl: bool,
) -> "pl.Expr":
    """Polars expression for the `pnl` column in holdings enrichment.
    Trust broker pnl when not-null; otherwise compute from (ltp-avg)*qty.
    Caller must confirm has_ltp + has_avg + has_qty before calling.
    """
    _ltp = _col_f64(lf, "last_price")
    _avg = _col_f64(lf, "average_price")
    _qty = _col_f64(lf, "opening_quantity")
    _pnl_calc = (_ltp - _avg) * _qty
    if has_pnl:
        _broker_pnl = _col_f64_nullable(lf, "pnl")
        return (
            pl.when(_broker_pnl.is_not_null())
            .then(_broker_pnl)
            .otherwise(_pnl_calc)
            .alias("pnl")
        )
    return (
        pl.when((_ltp > 0) & (_avg > 0))
        .then(_pnl_calc)
        .otherwise(pl.lit(0.0))
        .alias("pnl")
    )


def _build_holdings_curval_exprs(lf: "pl.DataFrame") -> list["pl.Expr"]:
    """Polars expressions for cur_val and pnl_percentage.
    Depends on the `pnl` alias already being planned in the same
    with_columns() call; caller must include both in the same pass.
    """
    _pnl_expr = pl.col("pnl")
    _inv_expr = _col_f64(lf, "inv_val")
    return [
        (_inv_expr + _pnl_expr).alias("cur_val"),
        pl.when(_inv_expr != 0.0)
        .then(_pnl_expr / _inv_expr * 100.0)
        .otherwise(pl.lit(0.0))
        .alias("pnl_percentage"),
    ]


def _build_holdings_dcv_expr(
    lf: "pl.DataFrame",
    has_avg: bool,
    has_pnl: bool,
    has_dcv: bool,
) -> "pl.Expr":
    """Polars expression for day_change_val in holdings enrichment.
    Caller must confirm has_ltp + has_close + has_qty before calling.
    Day P&L = pnl − overnight_pnl (handles still-held / partial-sold /
    full-sold positions). Falls back to (ltp-close)*qty when intraday
    fields or broker pnl are absent.
    """
    _ltp = _col_f64(lf, "last_price")
    _cls = _col_f64(lf, "close_price")
    _qty = _col_f64(lf, "opening_quantity")
    if has_avg and has_pnl:
        _avg2      = _col_f64(lf, "average_price")
        _pnl2      = _col_f64_nullable(lf, "pnl")
        _overnight = (_cls - _avg2) * _qty
        _dcv_calc = pl.when(_pnl2.is_not_null()).then(
            _pnl2 - _overnight
        ).otherwise(
            (_ltp - _cls) * _qty
        )
    else:
        _dcv_calc = (_ltp - _cls) * _qty

    if has_dcv:
        _broker_dcv = _col_f64_nullable(lf, "day_change_val")
        return (
            pl.when(_broker_dcv.is_not_null())
            .then(_broker_dcv)
            .otherwise(_dcv_calc)
            .alias("day_change_val")
        )
    return (
        pl.when((_ltp > 0) & (_cls > 0))
        .then(_dcv_calc)
        .otherwise(pl.lit(0.0))
        .alias("day_change_val")
    )


def _enrich_holdings(df: pd.DataFrame) -> pd.DataFrame:
    """Polars-vectorized computed-column enrichment for holdings DataFrames.

    Converts the pandas DataFrame to a Polars DataFrame once, applies all
    derived-column expressions in a single with_columns pass (one compiled
    expression plan), then converts back to pandas. This replaces ~8 separate
    pd.to_numeric().fillna() + Series-arithmetic sequences with a single
    Polars evaluation — ~2-3× faster for typical 20-50 row per-account frames
    because Polars casts object-dtype columns to Float64 faster than pandas.

    All semantics from the original pandas path are preserved exactly — the
    same fallback priority rules, NaN-vs-0 sentinel distinctions, and the
    broker-value trust hierarchy (use broker column when not-null, fall back
    to our formula otherwise).
    """
    cols = set(df.columns)

    # ── inv_val = avg × opening_qty ──────────────────────────────────
    if {"average_price", "opening_quantity"}.issubset(cols):
        df["inv_val"] = (
            pd.to_numeric(df["average_price"], errors="coerce").fillna(0)
            * pd.to_numeric(df["opening_quantity"], errors="coerce").fillna(0)
        )

    # ── Polars block: pnl, cur_val, pnl_percentage, price_change,
    #    day_change_val ─────────────────────────────────────────────
    # Build a polars frame from the current pandas df (after inv_val).
    # All subsequent derived columns are computed here and written back
    # in one go — avoids repeated from_pandas / to_pandas round-trips.
    cols = set(df.columns)  # refresh after inv_val

    has_ltp    = "last_price"       in cols
    has_avg    = "average_price"    in cols
    has_qty    = "opening_quantity" in cols
    has_close  = "close_price"      in cols
    has_pnl    = "pnl"              in cols
    has_invval = "inv_val"          in cols
    has_dcv    = "day_change_val"   in cols

    lf = pl.from_pandas(df, nan_to_null=True)
    computed_exprs: list[pl.Expr] = []

    if has_ltp and has_avg and has_qty:
        computed_exprs.append(_build_holdings_pnl_expr(lf, has_pnl))
        if not has_pnl:
            has_pnl = True  # will exist after this pass

    if has_pnl and has_invval:
        # These depend on pnl, which may have just been (re)written above.
        # We reference the planned alias directly.
        computed_exprs.extend(_build_holdings_curval_exprs(lf))

    if has_close and has_avg:
        computed_exprs.append(
            (_col_f64(lf, "close_price") - _col_f64(lf, "average_price"))
            .alias("price_change")
        )

    if has_ltp and has_close and has_qty:
        computed_exprs.append(
            _build_holdings_dcv_expr(lf, has_avg, has_pnl, has_dcv)
        )
    elif {"day_change", "opening_quantity"}.issubset(cols):
        computed_exprs.append(
            (_col_f64(lf, "day_change") * _col_f64(lf, "opening_quantity"))
            .alias("day_change_val")
        )

    if computed_exprs:
        lf = lf.with_columns(computed_exprs)
        # Write back only the columns that now exist in the polars frame
        # (avoids KeyError if an expression alias didn't fire).
        for c in ("pnl", "inv_val", "cur_val", "pnl_percentage",
                  "price_change", "day_change_val"):
            if c in lf.columns:
                df[c] = lf[c].to_pandas()

    # authorised_date — keep in pandas (datetime parse + strftime is
    # one line; not worth a polars round-trip for a rarely-present column).
    if "authorised_date" in df.columns:
        df["authorised_date"] = pd.to_datetime(
            df["authorised_date"], errors="coerce"
        ).dt.strftime("%d%b%y")

    return df


def _apply_backfill_to_list(
    frames: list[pd.DataFrame],
    qty_col: str = "quantity",
) -> list[pd.DataFrame]:
    """Concatenate `frames`, run `backfill_market_data` on the combined
    frame, then return it as a single-element list.

    This is the Approach A helper — it runs once at the broker boundary
    so the `_RAW_CACHE` always stores post-patch data. Every consumer
    (route + compute_firm_nav + investor slice) therefore reads the same
    cur_val / pnl / day_change_val values without needing independent
    backfill calls.

    Returns the original list unchanged when:
      • the list is empty or all frames are empty (no-op for outage states)
      • backfill raises unexpectedly (safety net — caller gets raw frames)

    The single-element list shape preserves the existing iteration
    contract (`for df in result: ...`) so callers need no migration.
    """
    if not frames:
        return frames
    non_empty = [f for f in frames if not f.empty]
    if not non_empty:
        return frames
    try:
        combined = pd.concat(non_empty, ignore_index=True)
        backfill_market_data(combined)
        return [combined]
    except Exception as _e:
        logger.warning(f"_apply_backfill_to_list: backfill failed, returning raw frames: {_e}")
        return frames


def fetch_positions(*args, **kwargs):
    """Public entry — proxies to conn_service when the cutover flag
    is on, otherwise runs the local @for_all_accounts path.

    Zero-arg results are memoised in `_RAW_CACHE` for `_RAW_TTL_S`
    seconds — see `fetch_holdings` docstring for the rationale.

    Same NAV-consistency guarantee as fetch_holdings (Approach A):
    the cached value is a post-backfill concatenated DataFrame in a
    single-element list. compute_firm_nav reads the same unrealised /
    pnl values as the positions route.
    """
    if not args and not kwargs:
        cached, is_leader = _raw_cache_reserve("positions")
        if not is_leader:
            return cached if cached is not None else _fetch_positions_local(*args, **kwargs)
        try:
            if _use_conn_service():
                from backend.brokers.client import sync as conn_sync
                result = conn_sync.fetch_positions()
            else:
                result = _fetch_positions_local()
            result = _apply_backfill_to_list(result, qty_col="quantity")
            _raw_cache_put("positions", result)
            return result
        except Exception:
            _raw_cache_release("positions")
            raise
    result = _fetch_positions_local(*args, **kwargs)
    return result


def _extract_net_rows(broker, kite):
    """Unwrap broker.positions() or kite.positions() to a flat list of net rows.
    Returns None when neither source is available (caller records failure).
    """
    if broker is not None:
        resp = broker.positions()
        # Every adapter (Kite + Dhan + Groww) normalises to the Kite-shape
        # dict {net: [...], day: [...]}.
        if isinstance(resp, dict):
            return resp.get("net", [])
        if isinstance(resp, list):
            return resp
        return None
    if kite is not None:
        return kite.positions()["net"]
    return None


def _maybe_log_kite_mcx_diag(df: "pd.DataFrame") -> None:
    """Fire a one-time operator diagnostic for the first MCX row with
    day_buy_quantity > 0, verifying whether Kite ships day_buy_value in
    lot-units or absolute ₹.  Idempotent — sets _KITE_VALUE_UNIT_LOGGED
    so subsequent calls are a cheap boolean check.
    """
    if _KITE_VALUE_UNIT_LOGGED:
        return
    if df.empty or 'multiplier' not in df.columns:
        return
    _diag = df[
        (df['multiplier'] > 1)
        & (df.get('day_buy_quantity', 0) > 0)
    ].head(1)
    if _diag.empty:
        return
    _r = _diag.iloc[0].to_dict()
    _mult_v   = _r.get('multiplier', 1)
    _dbq_lots = _r.get('day_buy_quantity', 0)
    _dbv_raw  = _r.get('day_buy_value', 0)
    _avg      = _r.get('average_price', 0)
    _expected_lot_units  = float(_dbq_lots) * float(_avg)
    _expected_abs_rupees = float(_dbq_lots) * float(_mult_v) * float(_avg)
    logger.warning(
        f"[KITE-MCX-DIAG] {_r.get('tradingsymbol', '?')} "
        f"mult={_mult_v} dbq_lots={_dbq_lots} "
        f"day_buy_value={_dbv_raw:.2f} avg={_avg:.4f} | "
        f"if≈{_expected_lot_units:.2f} → LOT-UNITS (5b995ccb correct), "
        f"if≈{_expected_abs_rupees:.2f} → ABSOLUTE ₹ (5b995ccb overcounts {_mult_v}×)"
    )
    globals()['_KITE_VALUE_UNIT_LOGGED'] = True


def _apply_mcx_multiplier(df: "pd.DataFrame") -> None:
    """Multiply quantity-field columns by `multiplier` (lot_size) in-place.

    MCX commodities: Kite ships `quantity` in LOTS but `last_price` /
    `close_price` are per CONTRACT so we convert to contract units so
    downstream `qty × price = ₹` works uniformly.

    REVERTED Jun 26 2026 (commit 5b995ccb): day_buy_value / day_sell_value
    are NOT scaled — Kite ships them as ABSOLUTE ₹ already.  Only the qty
    columns (quantity, overnight_quantity, day_buy/sell_quantity) are
    multiplied.
    """
    if df.empty or 'multiplier' not in df.columns:
        return
    _mult = df['multiplier']
    df['quantity'] = df['quantity'] * _mult
    for _c in ('overnight_quantity', 'day_buy_quantity', 'day_sell_quantity'):
        if _c in df.columns:
            df[_c] = df[_c] * _mult


@for_all_accounts
def _fetch_positions_local(connections=Connections, account=None, kite=None, broker=None):
    """Multi-broker positions fetch. Same broker-vs-kite resolution
    pattern as fetch_holdings; non-Kite adapters return Kite-shape
    rows via their respective normalisers."""
    df_positions = pd.DataFrame()
    # Circuit-breaker guard.
    if account and _is_circuit_open(account):
        logger.warning(
            f"[BREAKER] account={account} short-circuit positions "
            f"(open until {_ts_label(_FETCH_HEALTH.get(account, {}).get('circuit_open_until', 0) or 0)})"
        )
        # Stale-substitute path (Jul 2026) — see holdings variant for
        # rationale. DH6847 rows persist across breaker-open cycles.
        return _stale_substitute_frame("positions", account)
    # Interval gate (Dhan-only) — same pattern as holdings.
    if account and not _is_dhan_interval_due(account, broker):
        df_positions.attrs["interval_skipped"] = True
        logger.debug(
            f"[INTERVAL] account={account} positions poll skipped "
            f"(next_due={_ts_label(_dhan_next_poll.get(account, 0))})"
        )
        return df_positions
    if account:
        _update_dhan_next_poll(account, broker)
    try:
        net_rows = _extract_net_rows(broker, kite)
        if net_rows is None:
            df_positions.attrs['fetch_failed'] = True
            _record_fetch(account, ok=False, error="broker.positions() returned None")
            return df_positions
        df_positions = pd.DataFrame(net_rows)
        _record_fetch(account, ok=True)
        # One-time diagnostic — verifies Kite ships day_buy_value in
        # lot-units (lots × price) vs absolute ₹. Fires once per
        # process restart for the first MCX-multiplier row encountered.
        _maybe_log_kite_mcx_diag(df_positions)
        _apply_mcx_multiplier(df_positions)
        if not df_positions.empty:
            df_positions["account"] = account
            df_positions["type"] = "P"
    except Exception as e:
        logger.error(f"[{account}] Failed to fetch positions: {e}")
        df_positions.attrs['fetch_failed'] = True
        _record_fetch(account, ok=False, error=str(e))
        return df_positions

    if df_positions.empty:
        return df_positions

    df_positions = _enrich_positions(df_positions)
    # Stash a shallow copy for the stale-substitute path when this
    # account's breaker opens on a future cycle. Non-empty only —
    # legitimate "no positions" returns don't overwrite a prior LKG.
    if account and not df_positions.empty:
        _record_lkg_frame("positions", account, df_positions)
    return df_positions


def _enrich_positions(df: pd.DataFrame) -> pd.DataFrame:
    """Polars-vectorized computed-column enrichment for positions DataFrames.

    Converts to Polars once, evaluates all P&L and day-change expressions in a
    single with_columns pass, converts back to pandas. Replaces ~10 sequential
    pd.to_numeric().fillna() + Series-arithmetic chains. All semantics preserved:

      • day_change   = LTP − close (cosmetic per-share delta)
      • pnl          = broker value when not-null, else (LTP−avg)×qty
      • day_change_val:
          1. Decomposed intraday formula (full field set) — freezes closed positions
          2. broker.m2m when intraday fields absent
          3. Adapter-shipped day_change_val when present
          4. Naive (LTP−close)×qty otherwise
      • day_change_percentage = day_change_val / |close × qty| × 100
      • pnl_percentage        = pnl / |avg × qty| × 100
    """
    cols = set(df.columns)
    _intraday_fields = {'overnight_quantity', 'day_buy_quantity',
                        'day_sell_quantity', 'day_buy_value', 'day_sell_value'}
    has_intraday = _intraday_fields.issubset(cols)

    lf = pl.from_pandas(df, nan_to_null=True)

    _ltp = _col_f64(lf, 'last_price')
    _avg = _col_f64(lf, 'average_price')
    _cls = _col_f64(lf, 'close_price')
    _qty = _col_f64(lf, 'quantity')

    _pnl_calc = (_ltp - _avg) * _qty

    # ── day_change_val (decomposed intraday formula or fallbacks) ────
    if has_intraday:
        # Canonical formula — see `backend/api/algo/pnl_math.py` for the
        # rationale. `decomposed_intraday_pnl` takes scalars or polars
        # exprs interchangeably (each op is `+ * −` so polars Expr math
        # broadcasts the same way pandas Series math does).
        _oq = _col_f64(lf, 'overnight_quantity')
        _bq = _col_f64(lf, 'day_buy_quantity')
        _sq = _col_f64(lf, 'day_sell_quantity')
        _bv = _col_f64(lf, 'day_buy_value')
        _sv = _col_f64(lf, 'day_sell_value')
        _dcv_calc_expr = decomposed_intraday_pnl(
            _oq, _ltp, _cls, _bq, _bv, _sv, _sq,
        )
        # Validity guard: zero when LTP unhealthy (pre-open warm-up).
        _dcv_expr = pl.when(_ltp > 0).then(_dcv_calc_expr).otherwise(pl.lit(0.0))
    else:
        # Pre-intraday fallback. When close is missing/zero for fresh same-day
        # buys, use (LTP − avg) × qty so the row shows movement since entry.
        _dcv_naive = naive_day_pnl(_ltp, _cls, _qty)
        _dcv_entry = (_ltp - _avg) * _qty
        _dcv_calc_expr = pl.when((_cls <= 0) & (_avg > 0) & (_ltp > 0)).then(_dcv_entry).otherwise(_dcv_naive)
        if 'm2m' in cols:
            _broker_m2m = _col_f64_nullable(lf, 'm2m')
            _dcv_expr = (
                pl.when(_broker_m2m.is_not_null())
                .then(_broker_m2m)
                .otherwise(_dcv_calc_expr)
            )
        elif 'day_change_val' in cols:
            _broker_dcv = _col_f64_nullable(lf, 'day_change_val')
            _dcv_expr = (
                pl.when(_broker_dcv.is_not_null())
                .then(_broker_dcv)
                .otherwise(_dcv_calc_expr)
            )
        else:
            _dcv_expr = pl.when((_ltp > 0) & (_cls > 0)).then(_dcv_calc_expr).otherwise(pl.lit(0.0))

    # ── pnl ──────────────────────────────────────────────────────────
    if 'pnl' in cols:
        _broker_pnl = _col_f64_nullable(lf, 'pnl')
        _pnl_expr = (
            pl.when(_broker_pnl.is_not_null())
            .then(_broker_pnl)
            .otherwise(_pnl_calc)
        )
    else:
        _pnl_expr = (
            pl.when((_ltp > 0) & (_avg > 0))
            .then(_pnl_calc)
            .otherwise(pl.lit(0.0))
        )

    # ── day_change_percentage ─────────────────────────────────────────
    _prev_val = (_cls * _qty).abs()
    _dcp_expr = (
        pl.when(_prev_val != 0.0)
        .then(pl.col("day_change_val") / _prev_val * 100.0)
        .otherwise(pl.lit(0.0))
    )

    # ── pnl_percentage ────────────────────────────────────────────────
    _cost_basis = (_avg * _qty).abs()
    _pnl_pct_expr = (
        pl.when(_cost_basis != 0.0)
        .then(pl.col("pnl") / _cost_basis * 100.0)
        .otherwise(pl.lit(0.0))
    )

    # First pass: day_change, pnl, day_change_val (independent of each other).
    lf = lf.with_columns([
        (_ltp - _cls).alias("day_change"),
        _pnl_expr.alias("pnl"),
        _dcv_expr.alias("day_change_val"),
    ])
    # Second pass: percentages that depend on the first pass results.
    # Contract A note: a position opened TODAY has close_price=0 (no prior
    # session for this symbol), so |close × qty| collapses to 0 and the
    # percent would round to 0. Fall back to |avg × qty| (= notional at
    # entry) so opened-today rows still show a meaningful Day % — the same
    # number the operator computes mentally as `(LTP − entry)/entry × 100`.
    _close_denom = (_cls * _col_f64(lf, 'quantity')).abs()
    _avg_denom   = (_col_f64(lf, 'average_price') * _col_f64(lf, 'quantity')).abs()
    lf = lf.with_columns([
        pl.when(_close_denom != 0.0)
        .then(pl.col("day_change_val") / _close_denom * 100.0)
        .when(_avg_denom != 0.0)
        .then(pl.col("day_change_val") / _avg_denom * 100.0)
        .otherwise(pl.lit(0.0))
        .alias("day_change_percentage"),
        pl.when((_col_f64(lf, 'average_price') * _col_f64(lf, 'quantity')).abs() != 0.0)
        .then(pl.col("pnl") / (_col_f64(lf, 'average_price') * _col_f64(lf, 'quantity')).abs() * 100.0)
        .otherwise(pl.lit(0.0))
        .alias("pnl_percentage"),
    ])

    # Write computed columns back to pandas.
    for c in ("day_change", "pnl", "day_change_val",
              "day_change_percentage", "pnl_percentage"):
        if c in lf.columns:
            df[c] = lf[c].to_pandas()

    return df


def backfill_market_data(df) -> int:
    """Generalised market-data backfill. Operator: "if the fields any
    time available from dhan or groww, it can be backfilled from kite
    using symbol. only cost price is required from the broker."

    Industry pattern (IBKR / Bloomberg PRTU / Sensibull / Streak):
    each row of a multi-broker book is split into two slices —
      account-specific facts → trust the source broker
        (avg_price, quantity, opening_quantity, realised, account)
      market-data facts → one canonical source for the whole book
        (close_price, last_price, day_change_*, instrument identity)

    Kite's `quote()` is the most complete market-data feed across
    Dhan / Groww / Kite, so we route every market-data lookup
    through `PriceBroker.quote()` (which prefers Kite, then falls
    through to Dhan, then Groww via the registry's preference
    order). Source brokers that already populate close_price /
    last_price keep their values — backfill only kicks in on
    zero / missing, never overwriting a non-zero broker value.

    Called by the /api/positions and /api/holdings endpoints AFTER
    `pd.concat(broker_apis.fetch_*())` so the PriceBroker.quote
    call is ONE batched round-trip across every missing-field row
    from every broker — not N per N accounts (the prior shape
    called the lookup inside the per-account `@for_all_accounts`
    body and burned N quote() calls per poll).

    No-op when both close_price and last_price are already
    populated on every row (Kite always returns them; Dhan + Groww
    sometimes don't). Exception-safe: a PriceBroker outage leaves
    rows untouched and downstream P&L fallback behaviour matches
    the pre-patch state.

    Returns the count of patched rows (informational for callers'
    debug logs).
    """
    if df is None or df.empty:
        return 0
    if 'close_price' not in df.columns and 'last_price' not in df.columns:
        return 0

    _missing, _key_per_row, _unique_keys = _bmd_build_key_index(df)
    if _missing is None or not _unique_keys:
        return 0

    _close_lookup, _ltp_lookup = _bmd_fetch_lookups(_unique_keys)

    _row_indices = df.index[_missing].tolist()
    _patched_indices = _bmd_patch_rows(
        df, _row_indices, _key_per_row, _close_lookup, _ltp_lookup, _unique_keys
    )
    if not _patched_indices:
        return 0

    _bmd_recompute_derived(df, _patched_indices)
    return len(_patched_indices)


def _bmd_build_key_index(df):
    """Compute the missing-row mask, the per-row quote key list, and
    the deduplicated list of quote keys to fetch.

    Returns (mask_series, key_per_row_list, unique_keys_list). When no
    row needs backfill, returns (None, [], [])."""
    # A row needs backfill if EITHER close_price or last_price is
    # zero / missing. Unions across both criteria so the single
    # batched quote call covers everything.
    _cls_missing = (pd.to_numeric(df['close_price'], errors='coerce').fillna(0).le(0)
                    if 'close_price' in df.columns
                    else pd.Series(False, index=df.index))
    _ltp_missing = (pd.to_numeric(df['last_price'], errors='coerce').fillna(0).le(0)
                    if 'last_price' in df.columns
                    else pd.Series(False, index=df.index))
    _missing = _cls_missing | _ltp_missing
    if not _missing.any():
        return None, [], []

    _missing_rows = df[_missing]
    _key_per_row: list[str] = []
    _seen_keys: set[str] = set()
    _unique_keys: list[str] = []
    for _, _row in _missing_rows.iterrows():
        _exch = str(_row.get('exchange', '') or 'NFO').upper()
        _sym  = str(_row.get('tradingsymbol', '') or '').upper()
        if _sym:
            _k = f"{_exch}:{_sym}"
            _key_per_row.append(_k)
            if _k not in _seen_keys:
                _seen_keys.add(_k)
                _unique_keys.append(_k)
        else:
            _key_per_row.append('')
    return _missing, _key_per_row, _unique_keys


def _bmd_fetch_lookups(unique_keys: list[str]) -> tuple[dict[str, float], dict[str, float]]:
    """Fetch quotes for the deduplicated key list and return two
    lookup dicts keyed by 'EXCHANGE:SYMBOL':
      - close_lookup: OHLC.close (fallback to top-level close_price)
      - ltp_lookup: top-level last_price

    Falls back to KiteTicker for LTP on PriceBroker outage. Zeros
    are treated as "broker didn't have it either" and excluded from
    both dicts. Also records positive LTPs into the last-known-good
    cache for downstream stale-rescue."""
    _q = _bmd_fetch_quotes(unique_keys)
    return _bmd_extract_lookups(_q)


def _bmd_fetch_quotes(unique_keys: list[str]) -> dict:
    """One batched PriceBroker.quote() with KiteTicker fallback for LTP.

    Returns a dict shaped like Kite's quote response (or a minimal
    synthesised subset from the ticker on PriceBroker outage)."""
    try:
        from backend.brokers.registry import get_market_data_broker
        _pb = get_market_data_broker()
        return _pb.quote(unique_keys) or {}
    except Exception as _e:
        logger.warning(
            f"PriceBroker market-data backfill failed (1 batched call for "
            f"{len(unique_keys)} symbols): {_e}"
        )
        # PriceBroker outage (all brokers rate-limited / token expired).
        # Fall back to KiteTicker for LTP on missing rows so the
        # alternating-0 symptom is suppressed even during cool-off.
        # close_price cannot come from the ticker (no OHLC there) —
        # only last_price is patched here; the day_change_val recompute
        # below handles the rest.
        _q: dict = {}
        try:
            from backend.brokers.kite_ticker import get_ticker as _gt
            _ticker_fb = _gt()
            for _k in unique_keys:
                # key shape is "EXCHANGE:SYMBOL" — strip exchange prefix.
                _sym_fb = _k.split(":", 1)[-1] if ":" in _k else _k
                _ltp_fb = _ticker_fb.get_ltp_by_sym(_sym_fb)
                if _ltp_fb and _ltp_fb > 0:
                    # Synthesise a minimal quote-like entry so the
                    # extraction loop below can populate _ltp_lookup.
                    _q[_k] = {"last_price": _ltp_fb}
                    record_good_ltp(_sym_fb, _ltp_fb)
        except Exception as _te:
            logger.debug(f"PriceBroker backfill: ticker fallback also failed: {_te}")
            # Both PriceBroker and KiteTicker are unavailable.
            # last-good-ltp fallback happens below in the patch loop.
            pass
        return _q


def _bmd_extract_lookups(quote_resp: dict) -> tuple[dict[str, float], dict[str, float]]:
    """Extract close-price + last-price lookups from a quote response.

    Only positive values land in the lookup tables — zeros are
    treated as "broker didn't have it either". Recording into the
    last-known-good cache happens on positive LTPs."""
    _close_lookup: dict[str, float] = {}
    _ltp_lookup: dict[str, float] = {}
    for _k, _v in quote_resp.items():
        if not isinstance(_v, dict):
            continue
        _ohlc = _v.get('ohlc') if isinstance(_v.get('ohlc'), dict) else {}
        _cls_val = _ohlc.get('close') if _ohlc else None
        if _cls_val is None:
            _cls_val = _v.get('close_price')
        try:
            _f_cls = float(_cls_val) if _cls_val is not None else 0.0
        except (TypeError, ValueError):
            _f_cls = 0.0
        if _f_cls > 0:
            _close_lookup[_k] = _f_cls

        _ltp_val = _v.get('last_price')
        try:
            _f_ltp = float(_ltp_val) if _ltp_val is not None else 0.0
        except (TypeError, ValueError):
            _f_ltp = 0.0
        if _f_ltp > 0:
            _ltp_lookup[_k] = _f_ltp
            # Record for last-known-good cache; strip exchange prefix for
            # the canonical symbol-only key used by get_last_good_ltp().
            _sym_only = _k.split(":", 1)[-1] if ":" in _k else _k
            record_good_ltp(_sym_only, _f_ltp)
    return _close_lookup, _ltp_lookup


def _bmd_is_missing_val(value) -> bool:
    """A row's close/last_price is missing when it's NaN, non-numeric, or ≤0."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return True
    if v != v:  # NaN
        return True
    return v <= 0


def _bmd_patch_one_row(
    df, idx, k: str, has_close: bool, has_ltp: bool,
    close_lookup: dict, ltp_lookup: dict,
) -> tuple[bool, bool]:
    """Patch close_price + last_price on ONE row where the source broker
    came back with 0. Never overwrites a non-zero broker value.

    Returns (touched, from_stale_cache):
      * touched = True when either column was written
      * from_stale_cache = True when LTP came from the last-known-good
        in-process cache (row should be marked `last_price_stale=True`)."""
    touched = False
    from_stale = False
    if has_close and _bmd_is_missing_val(df.at[idx, 'close_price']):
        cls_p = close_lookup.get(k)
        if cls_p:
            df.at[idx, 'close_price'] = cls_p
            touched = True
    if has_ltp and _bmd_is_missing_val(df.at[idx, 'last_price']):
        ltp_p = ltp_lookup.get(k)
        if ltp_p:
            df.at[idx, 'last_price'] = ltp_p
            touched = True
        else:
            # Last resort: use the last-known-good LTP from the
            # in-process cache (populated by every previous
            # successful fetch).  Mark the row as stale so
            # callers can surface a staleness indicator.
            sym_only = k.split(":", 1)[-1] if ":" in k else k
            cached_ltp = get_last_good_ltp(sym_only)
            if cached_ltp is not None:
                df.at[idx, 'last_price'] = cached_ltp
                from_stale = True
                touched = True
    return touched, from_stale


def _bmd_mark_stale_column(df, stale_indices: set) -> None:
    """Mark rows whose LTP came from the last-known-good cache so
    routes can propagate the staleness flag to the response schema."""
    if not stale_indices:
        return
    if 'last_price_stale' not in df.columns:
        df['last_price_stale'] = False
    for idx in stale_indices:
        df.at[idx, 'last_price_stale'] = True
    logger.info(
        f"market-data backfill: {len(stale_indices)} rows rescued "
        f"by last-known-good LTP cache (live sources unavailable)"
    )


def _bmd_log_unresolved(unresolved: list[str], unique_keys) -> None:
    """Diagnostic: log symbols where PriceBroker.quote() returned
    neither close nor LTP. These rows stay at 0 → Day P&L = 0
    downstream — the canonical "Dhan Day P&L shows zero while
    Kite shows non-zero" symptom. When the operator reports it,
    this log line names the exact symbols that failed so the
    next step is deterministic (usually: symbol not in Kite
    instruments cache, or broker quote returned no ohlc)."""
    if not unresolved:
        return
    logger.warning(
        f"market-data backfill: {len(unresolved)}/{len(unique_keys)} "
        f"symbols unresolved by PriceBroker; rows stay at close=0 / "
        f"ltp=0 → Day P&L=0. Unresolved: {unresolved[:10]}"
        + (f" (+{len(unresolved)-10} more)" if len(unresolved) > 10 else "")
    )


def _bmd_patch_rows(df, row_indices, key_per_row, close_lookup, ltp_lookup, unique_keys) -> set:
    """Patch close_price + last_price in place, but ONLY on rows
    where the source broker came back with 0. Never overwrite a
    non-zero broker value — Dhan/Groww LTP may be a fresher tick
    than the snapshot-time Kite quote.

    Rows rescued via last-known-good cache get `last_price_stale=True`.
    Emits a warning log for symbols that resolved neither close nor LTP.
    Returns the set of patched row indices."""
    has_close = 'close_price' in df.columns
    has_ltp   = 'last_price'  in df.columns
    patched_indices: set = set()
    stale_patched_indices: set = set()  # rows rescued by last-good cache
    unresolved: list[str] = []
    for idx, k in zip(row_indices, key_per_row):
        if not k:
            continue
        touched, from_stale = _bmd_patch_one_row(
            df, idx, k, has_close, has_ltp, close_lookup, ltp_lookup,
        )
        if from_stale:
            stale_patched_indices.add(idx)
        if touched:
            patched_indices.add(idx)
        elif k not in close_lookup and k not in ltp_lookup:
            unresolved.append(k)

    _bmd_mark_stale_column(df, stale_patched_indices)
    _bmd_log_unresolved(unresolved, unique_keys)
    return patched_indices


def _bmd_recompute_derived(df, patched_indices: set) -> None:
    """Re-run the (LTP - close) × qty recompute on patched rows only.
    The per-account fetch already wrote a value (0 or broker-
    reported) the consumer treats as authoritative — overwrite it
    now that we have real market data. Also recomputes pnl / cur_val /
    pnl_percentage / day_change / day_change_percentage as available."""
    _qty_col = 'opening_quantity' if 'opening_quantity' in df.columns else 'quantity'
    if _qty_col not in df.columns or 'last_price' not in df.columns:
        return

    _idx_array = pd.Index(sorted(patched_indices))
    _ltp_p = pd.to_numeric(df.loc[_idx_array, 'last_price'], errors='coerce').fillna(0)
    _cls_p = pd.to_numeric(df.loc[_idx_array, 'close_price'], errors='coerce').fillna(0)
    _qty_p = pd.to_numeric(df.loc[_idx_array, _qty_col], errors='coerce').fillna(0)
    _dcv_p = (_ltp_p - _cls_p) * _qty_p
    _valid_p = (_ltp_p > 0) & (_cls_p > 0)
    if 'day_change_val' in df.columns:
        df.loc[_idx_array, 'day_change_val'] = _dcv_p.where(
            _valid_p, df.loc[_idx_array, 'day_change_val']
        )
    else:
        df.loc[_idx_array, 'day_change_val'] = _dcv_p.where(_valid_p, 0.0)
    if 'day_change' in df.columns:
        df.loc[_idx_array, 'day_change'] = _ltp_p - _cls_p

    # day_change_percentage rides off close × qty in the consumer's
    # per-account summary. For row-level we update the column
    # directly so the API response is consistent.
    if 'day_change_percentage' in df.columns:
        _prev_val_p = (_cls_p * _qty_p).abs()
        _pct_p = (_dcv_p / _prev_val_p.replace(0, pd.NA) * 100).fillna(0)
        df.loc[_idx_array, 'day_change_percentage'] = _pct_p.where(
            _valid_p, df.loc[_idx_array, 'day_change_percentage']
        )

    # Recompute pnl on rows where LTP was patched and we have a
    # cost basis. The source broker's pnl on those rows was
    # computed against the (broken) zero LTP, so it's typically
    # wrong (= -cost × qty for a long position). Cost basis is
    # the only field we trust the source broker for here — it's
    # the account-specific fact only that broker knows.
    if 'average_price' in df.columns and 'pnl' in df.columns:
        _avg_p = pd.to_numeric(df.loc[_idx_array, 'average_price'], errors='coerce').fillna(0)
        _pnl_calc = (_ltp_p - _avg_p) * _qty_p
        # Include realised when present (positions carry it; holdings
        # typically don't because holdings are open-only).
        if 'realised' in df.columns:
            _rea_p = pd.to_numeric(df.loc[_idx_array, 'realised'], errors='coerce').fillna(0)
            _pnl_calc = _pnl_calc + _rea_p
        _valid_pnl = (_ltp_p > 0) & (_avg_p > 0)
        df.loc[_idx_array, 'pnl'] = _pnl_calc.where(
            _valid_pnl, df.loc[_idx_array, 'pnl']
        )
        # cur_val + pnl_percentage chain off pnl — keep them
        # consistent when present.
        if 'inv_val' in df.columns and 'cur_val' in df.columns:
            _inv_p = pd.to_numeric(df.loc[_idx_array, 'inv_val'], errors='coerce').fillna(0)
            df.loc[_idx_array, 'cur_val'] = _inv_p + df.loc[_idx_array, 'pnl']
            if 'pnl_percentage' in df.columns:
                _pp = (df.loc[_idx_array, 'pnl'] / _inv_p.replace(0, pd.NA) * 100).fillna(0)
                df.loc[_idx_array, 'pnl_percentage'] = _pp


# Back-compat alias — the function used to be narrower (close only).
# Old name still resolves so external scripts / future-refactor
# callers don't break in the same deploy as the rename.
backfill_close_prices = backfill_market_data


def fetch_margins(*args, **kwargs):
    """Public entry — proxies to conn_service when the cutover flag
    is on, otherwise runs the local @for_all_accounts path.

    Zero-arg results are memoised in `_RAW_CACHE` for `_RAW_TTL_S`
    seconds — see `fetch_holdings` docstring for the rationale.
    """
    if not args and not kwargs:
        cached, is_leader = _raw_cache_reserve("margins")
        if not is_leader:
            return cached if cached is not None else _fetch_margins_local(*args, **kwargs)
        try:
            if _use_conn_service():
                from backend.brokers.client import sync as conn_sync
                result = conn_sync.fetch_margins()
            else:
                result = _fetch_margins_local()
            _raw_cache_put("margins", result)
            return result
        except Exception:
            _raw_cache_release("margins")
            raise
    result = _fetch_margins_local(*args, **kwargs)
    return result


@for_all_accounts
def _fetch_margins_local(connections=Connections, account=None, kite=None, broker=None):
    """Multi-broker margins fetch. broker.margins(segment) returns the
    same Kite-shape dict every adapter normalises to."""
    df_margins = pd.DataFrame()
    # Circuit-breaker guard.
    if account and _is_circuit_open(account):
        logger.warning(
            f"[BREAKER] account={account} short-circuit margins "
            f"(open until {_ts_label(_FETCH_HEALTH.get(account, {}).get('circuit_open_until', 0) or 0)})"
        )
        # Stale-substitute path (Jul 2026) — see holdings variant for
        # rationale. DH6847 funds row persists across breaker-open cycles.
        return _stale_substitute_frame("margins", account)
    # Interval gate (Dhan-only) — same pattern as holdings.
    if account and not _is_dhan_interval_due(account, broker):
        df_margins.attrs["interval_skipped"] = True
        logger.debug(
            f"[INTERVAL] account={account} margins poll skipped "
            f"(next_due={_ts_label(_dhan_next_poll.get(account, 0))})"
        )
        return df_margins
    if account:
        _update_dhan_next_poll(account, broker)
    try:
        if broker is not None:
            margins_data = broker.margins(segment="equity")
        elif kite is not None:
            margins_data = kite.margins(segment="equity")
        else:
            return df_margins
        df_margins = pd.DataFrame([margins_data])

        # Flatten 'utilised' if it exists
        if "utilised" in df_margins.columns:
            utilised_df = pd.json_normalize(df_margins["utilised"])
            # Optional: prefix column names
            utilised_df = utilised_df.add_prefix("util ")
            # Drop original nested column and concat flattened
            df_margins = pd.concat([df_margins.drop(columns=["utilised"]), utilised_df], axis=1)

        # Flatten 'available' if needed
        if "available" in df_margins.columns:
            available_df = pd.json_normalize(df_margins["available"])
            available_df = available_df.add_prefix("avail ")
            df_margins = pd.concat([df_margins.drop(columns=["available"]), available_df], axis=1)

        if not df_margins.empty:
            df_margins["account"] = account
            df_margins["type"] = "C"
        _record_fetch(account, ok=True)
    except Exception as e:
        logger.error(f"[{account}] Failed to fetch margins: {e}")
        _record_fetch(account, ok=False, error=str(e))

    # Stash a shallow copy for the stale-substitute path when this
    # account's breaker opens on a future cycle. Non-empty only.
    if account and not df_margins.empty:
        _record_lkg_frame("margins", account, df_margins)
    return df_margins


# Daily-TTL cache for fetch_holidays. The holiday list only changes once
# per year, but the agent engine's _build_context calls fetch_holidays on
# every run_cycle (every 5 min real-path, every 2 s in sim) — without
# this, every tick fired a blocking HTTP GET to nseindia.com.
# Format: {exchange: (cached_date, set_of_dates)}
_HOLIDAY_CACHE: dict[str, tuple] = {}

# Mapping from Kite exchange names to NSE holiday API segment keys.
# Module-level constant — avoids rebuilding this dict on every cache-miss
# call to fetch_holidays (which runs inside the agent-engine hot path).
_NSE_SEGMENT_MAP: dict[str, str] = {
    "NSE": "CM",
    "BSE": "CM",
    "NFO": "FO",
    "CDS": "CD",
    "MCX": "COM",
}

# Daily-TTL cache for fetch_special_sessions.
# Format: {exchange: (cached_date, list_of_session_dicts)}
# Each dict has keys: date (datetime.date), start (datetime.time),
# end (datetime.time).  Empty lists are cached to avoid retry-storm.
_SPECIAL_SESSION_CACHE: dict[str, tuple] = {}


def fetch_special_sessions(exchange: str = "NSE") -> list[dict]:
    """Return today's special-session override rows for ``exchange``.

    Queries the ``market_special_sessions`` DB table (populated by
    ``seed_special_sessions`` at startup + by the operator directly).
    Results are cached with a daily TTL — the same bust-on-date-rollover
    pattern as ``_HOLIDAY_CACHE``.  Empty lists are cached so a missing
    table (fresh deploy) never causes a retry-storm.

    Returns a list of dicts, each with:
      ``{"date": datetime.date, "start": datetime.time, "end": datetime.time}``

    The list only contains rows whose ``date`` field equals today (IST).
    Callers pass this list as ``special_sessions=`` to ``is_market_open``.
    Fail-open: on any DB error returns ``[]``.
    """
    from datetime import date as _dt_date

    exch = (exchange or "NSE").upper().strip()
    today = _dt_date.today()

    cached = _SPECIAL_SESSION_CACHE.get(exch)
    if cached and cached[0] == today:
        return cached[1]

    rows: list[dict] = []
    try:
        rows = _read_special_sessions_sync(exch, today)
    except Exception:
        pass  # DB unavailable — fail open (return empty list)

    _SPECIAL_SESSION_CACHE[exch] = (today, rows)
    return rows


def _read_special_sessions_sync(exchange: str, today) -> list[dict]:
    """Blocking DB read for market_special_sessions rows matching today.

    Intentionally sync so ``fetch_special_sessions`` can be called from
    non-async code (same rationale as ``_read_market_holidays_sync``).
    Fires at most once per (exchange, day) per process — subsequent calls
    hit the in-process ``_SPECIAL_SESSION_CACHE``.
    """
    import asyncio
    from sqlalchemy import select
    from datetime import date as _dt_date, time as _dt_time

    async def _async_read() -> list[dict]:
        from backend.api.database import async_session
        from backend.api.models import MarketSpecialSession
        async with async_session() as sess:
            result = await sess.execute(
                select(MarketSpecialSession).where(
                    MarketSpecialSession.exchange == exchange,
                    MarketSpecialSession.date == today,
                )
            )
            rows = result.scalars().all()
            return [
                {
                    "date":  r.date if isinstance(r.date, _dt_date) else r.date,
                    "start": r.start_time if isinstance(r.start_time, _dt_time) else r.start_time,
                    "end":   r.end_time if isinstance(r.end_time, _dt_time) else r.end_time,
                }
                for r in rows
            ]

    # Use get_running_loop() to detect an async context (Python 3.10+
    # correct pattern; get_event_loop() emits DeprecationWarning inside
    # coroutines and would create a NEW loop in sync thread contexts —
    # meaningless here).
    try:
        try:
            asyncio.get_running_loop()
            # Inside an async context — run in thread to avoid blocking loop.
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(asyncio.run, _async_read())
                return fut.result(timeout=5)
        except RuntimeError:
            # No running loop — safe to run one inline.
            return asyncio.run(_async_read())
    except Exception:
        return []


def fetch_holidays(exchange="NSE"):
    """
    Fetch trading holidays for `exchange`. Read priority (four tiers):

      1. ``holidays_store._MEM_CACHE`` (in-process LRU) — sync dict read.
         Populated by the async persistent-store path.
      2. Module-level ``_HOLIDAY_CACHE`` (daily-TTL fallback) — used when
         the persistent store has not been warmed yet.
      3. ``market_holidays`` PostgreSQL table — durable across restarts,
         populated daily by ``_task_holiday_refresh`` at 04:00 IST.
      4. NSE public API (``nseindia.com/api/holiday-master``) — cold-boot
         fallback ONLY. Also invoked directly by ``_task_holiday_refresh``
         which is what normally populates Tier 3.

    On Tier-3 or Tier-4 hit the result is mirrored into Tiers 1+2 so the
    next call (which may be on the agent-engine hot path) short-circuits.

    This function is sync so it can be called from non-async code. Do NOT
    make it async — Tier 3 does a blocking DB query, which is acceptable
    because it fires at most once per (exchange, day) per process.
    """
    from datetime import datetime as dt_datetime, date as dt_date

    exch = exchange.upper().strip()

    # ── Tier 1: check holidays_store memory cache (sync read) ─────────────────
    try:
        from backend.api.persistence.holidays_store import (
            _MEM_CACHE as _hol_mem,
            _ist_year as _hol_year,
        )
        yr = _hol_year()
        hol_key = (exch, yr)
        if hol_key in _hol_mem:
            # Mirror into _HOLIDAY_CACHE so future sync callers get fast path.
            cached_set = _hol_mem[hol_key]
            today = dt_date.today()
            _HOLIDAY_CACHE[exchange] = (today, cached_set)
            return cached_set
    except Exception:
        pass  # persistent store not available — fall through

    # ── Tier 2: module-level _HOLIDAY_CACHE (daily TTL) ───────────────────────
    today = dt_date.today()
    cached = _HOLIDAY_CACHE.get(exchange)
    if cached and cached[0] == today:
        return cached[1]

    # ── Tier 3: market_holidays DB table ──────────────────────────────────────
    # Read all rows for `exchange` in the current IST calendar year. If the
    # cron has populated the table, this satisfies the request without any
    # network I/O; on a cold DB (first boot after migration) it returns an
    # empty set and we fall through to Tier 4.
    try:
        db_hols = _read_market_holidays_sync(exch)
        if db_hols:  # only accept non-empty; empty could mean cron hasn't run yet
            _HOLIDAY_CACHE[exchange] = (today, db_hols)
            _mirror_to_holidays_store(exch, db_hols)
            return db_hols
    except Exception:
        pass  # DB unavailable / not initialised — fall through to Tier 4

    # ── Tier 4: NSE public API (cold-boot fallback) ───────────────────────────
    holidays: set = _fetch_holidays_from_nse(exchange)

    # Cache even on failure (empty set) — avoids retry-hammering nseindia
    # all day if the API is down. Next day's call retries naturally.
    _HOLIDAY_CACHE[exchange] = (today, holidays)

    # Fire-and-forget: populate persistent store + DB table so future
    # restarts hit Tier 1/2/3 instead of Tier 4.
    if holidays:
        _trigger_holidays_store_populate(exch, holidays)
        _upsert_market_holidays_async(exch, holidays, source="nse_auto")

    return holidays


def _fetch_holidays_from_nse(exchange: str) -> set:
    """Direct NSE public-API fetch (Tier 4 in `fetch_holidays`; ALSO the
    primary path invoked by `_task_holiday_refresh`). Returns a set of
    `date` objects; empty set on any failure so the caller can decide
    what to do (cache empty, retry, etc.)."""
    import requests
    from datetime import datetime as dt_datetime

    holidays: set = set()
    try:
        resp = requests.get(
            "https://www.nseindia.com/api/holiday-master?type=trading",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        segment = _NSE_SEGMENT_MAP.get(exchange, "CM")
        entries = data.get(segment, [])
        for h in entries:
            d = h.get("tradingDate", "")
            if d:
                try:
                    holidays.add(dt_datetime.strptime(d, "%d-%b-%Y").date())
                except ValueError:
                    pass
    except Exception:
        pass
    return holidays


def _read_market_holidays_sync(exchange: str) -> set:
    """Synchronous read of `market_holidays` for `exchange` in the current
    IST year. Uses the sync-friendly connection path via `asyncio.run` on a
    scratch loop when no loop is running, else schedules the coroutine.

    Returns a set of `date` objects. Never raises — DB errors return empty.
    """
    from datetime import date as dt_date
    import asyncio as _asyncio

    try:
        try:
            _asyncio.get_running_loop()
            # We're inside an event loop — cannot use asyncio.run(). Fall
            # back to a threadpool executor that spins up a fresh loop.
            import concurrent.futures as _cf
            with _cf.ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(_asyncio.run, _read_market_holidays_async(exchange))
                return fut.result(timeout=5.0)
        except RuntimeError:
            # No running loop — safe to run one inline.
            return _asyncio.run(_read_market_holidays_async(exchange))
    except Exception:
        return set()


async def _read_market_holidays_async(exchange: str) -> set:
    """Async DB query — returns set of `date` for `exchange` in the current
    IST calendar year. Filtered to current year to match the semantic
    contract of the legacy tiers (year-scoped cache)."""
    from datetime import date as dt_date
    from sqlalchemy import select, and_, extract
    try:
        from backend.api.database import async_session
        from backend.api.models import MarketHoliday
    except Exception:
        return set()
    year = dt_date.today().year
    async with async_session() as s:
        rows = await s.execute(
            select(MarketHoliday.date).where(
                and_(
                    MarketHoliday.exchange == exchange,
                    extract("year", MarketHoliday.date) == year,
                )
            )
        )
        return {r[0] for r in rows.all()}


def _mirror_to_holidays_store(exchange: str, holidays: set) -> None:
    """After a Tier-3 hit, populate the in-process `holidays_store` cache
    so future callers short-circuit at Tier 1 rather than repeating the
    DB read."""
    try:
        from backend.api.persistence.holidays_store import (
            _MEM_CACHE as _hol_mem,
            _ist_year as _hol_year,
        )
        yr = _hol_year()
        _hol_mem[(exchange, yr)] = holidays
    except Exception:
        pass


def _upsert_market_holidays_async(
    exchange: str, holidays: set, source: str = "nse_auto",
) -> None:
    """Fire-and-forget UPSERT into `market_holidays`. Called from Tier-4
    NSE fallback (best-effort) and from `_task_holiday_refresh` (authoritative).

    Idempotent PK on (exchange, date). A row that was previously stored but
    is missing from the current `holidays` set is NOT deleted — a mid-year
    holiday removal is rare enough to warrant operator review rather than
    silent auto-delete.
    """
    try:
        import asyncio as _asyncio
        loop = _asyncio.get_running_loop()
        loop.create_task(_upsert_market_holidays_coro(exchange, holidays, source))
    except RuntimeError:
        # No running loop — likely import-time or a sync test. Skip.
        pass
    except Exception:
        pass


async def _upsert_market_holidays_coro(
    exchange: str, holidays: set, source: str,
) -> int:
    """Async body of `_upsert_market_holidays_async`. Returns rows inserted
    or updated (best-effort; PG doesn't distinguish on ON CONFLICT DO UPDATE)."""
    from datetime import datetime, timezone
    from sqlalchemy import text as _text
    try:
        from backend.api.database import async_session
    except Exception:
        return 0
    if not holidays:
        return 0
    now_utc = datetime.now(timezone.utc)
    rows = [
        {"exchange": exchange, "date": d, "source": source, "captured_at": now_utc}
        for d in holidays
    ]
    _upsert_sql = _text("""
        INSERT INTO market_holidays (exchange, date, source, captured_at)
        VALUES (:exchange, :date, :source, :captured_at)
        ON CONFLICT (exchange, date) DO UPDATE SET
            source      = EXCLUDED.source,
            captured_at = EXCLUDED.captured_at
    """)
    try:
        async with async_session() as s:
            await s.execute(_upsert_sql, rows)
            await s.commit()
        return len(rows)
    except Exception:
        return 0


def _trigger_holidays_store_populate(exchange: str, holidays: set) -> None:
    """Schedule a background write to holidays_store from a sync context.

    Schedules on the running event loop if one exists. Silently skips
    in test / import-only contexts where no loop is running.
    """
    try:
        import asyncio as _asyncio
        from backend.api.persistence.holidays_store import (
            _MEM_CACHE as _hol_mem,
            _ist_year as _hol_year,
            _enqueue_db as _hol_enqueue,
        )
        yr = _hol_year()
        key = (exchange, yr)
        # Populate Tier 1 synchronously — it's just a dict write.
        if key not in _hol_mem:
            _hol_mem[key] = holidays
        # Enqueue DB write — also sync (just puts to a queue).
        _hol_enqueue(exchange, yr, holidays)
    except Exception:
        pass  # never let background populate surface to the caller


def update_books(holdings, positions, margins):
    """Return all data combined into one DataFrame (optional)."""
    dfs = [holdings, positions, margins]
    dfs = [df for df in dfs if not df.empty]
    return pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()


