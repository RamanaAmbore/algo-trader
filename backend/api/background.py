"""
Litestar-integrated background scheduler.

Runs entirely inside the Litestar event loop — no ARQ, no Redis required.
Blocking broker API calls are offloaded to a ThreadPoolExecutor so they
never stall the async event loop.

Three tasks are started on Litestar startup:
  1. _task_performance — refresh holdings/positions/funds every N minutes during market hours,
                         send open/close summaries, fire loss alerts.
  2. _task_market      — warm market cache at startup; re-warm daily at 08:30 IST.
  3. _task_close       — check for segment close summaries (same cadence as performance).

All three tasks are cancelled cleanly on Litestar shutdown.
"""

import asyncio
import time as _time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, time as dtime, timezone
from typing import Optional

import pandas as pd

from backend.shared.helpers.date_time_utils import timestamp_indian, is_market_open, timestamp_display
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import config, get_nearest_time, get_cycle_date

logger = get_logger(__name__)

# Thread pool for blocking broker calls (keeps async loop responsive)
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ramboq-bg")

# ---------------------------------------------------------------------------
# Intraday equity-curve buffer
# ---------------------------------------------------------------------------
# (ist_timestamp_iso, day_pnl_inr, cum_pnl_inr) — one point per performance
# tick (~5 min during market hours). maxlen=200 covers a full 6.5-hour
# Indian trading day at 5-min cadence (~78 points) plus generous headroom.
# Wiped on IST date rollover so the buffer always reflects the current day.
# Each point is (ts, day_pnl, cum_pnl, h_pnl, h_day, p_pnl, p_day) where
# the trailing 4 numbers break down the aggregates the chart used to show:
#   H  = holdings lifetime P&L          (h_pnl)
#   ΔH = holdings today's change        (h_day)
#   P  = positions lifetime P&L         (p_pnl)
#   ΔP = positions today's change       (p_day)  — derived from positions'
#                                                  day_change_val; equals P for
#                                                  pure-intraday (MIS) books,
#                                                  differs for NRML carry-forward
_intraday_equity: deque[tuple[str, float, float, float, float, float, float]] = deque(maxlen=200)
_intraday_equity_date: date | None = None


# ---------------------------------------------------------------------------
# Segment config helpers
# ---------------------------------------------------------------------------

def _parse_time(t: str) -> dtime:
    h, m = map(int, t.split(':'))
    return dtime(h, m)


def _build_segments() -> list[dict]:
    raw = config.get('market_segments', {})
    return [
        {
            'name':             name,
            'hours_start':      _parse_time(s.get('hours_start', '09:15')),
            'hours_end':        _parse_time(s.get('hours_end',   '15:30')),
            'holiday_exchange': s.get('holiday_exchange', 'NSE'),
            'exchanges':        set(s.get('exchanges', [])),
        }
        for name, s in raw.items()
    ]


def _default_seg_state() -> dict:
    return {s['name']: {'last_open': None, 'last_close': None}
            for s in _build_segments()}


def _fetch_special_sessions_safe(exchange: str) -> list:
    """Wrapper around ``fetch_special_sessions`` that never raises.

    Used by background pollers (watchdog, etc.) that need the special-
    session override list but must not crash on a DB outage.  Returns an
    empty list on any error (fail-open — normal calendar logic applies).
    """
    try:
        from backend.brokers.broker_apis import fetch_special_sessions
        return fetch_special_sessions(exchange)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Direct broker fetch helpers
# ---------------------------------------------------------------------------

def _fetch_margins_direct() -> pd.DataFrame:
    """
    Returns pandas DataFrame with REAL (unmasked) account codes. Feeds the
    agent engine + Telegram/email dispatch — both of which go to the owner,
    so masking is unnecessary here. Public `/api/funds` re-applies masking
    on its own output for the marketing site.
    """
    from backend.brokers import broker_apis
    df = pd.concat(broker_apis.fetch_margins(), ignore_index=True)
    total_row = df.select_dtypes(include='number').sum()
    total_row['account'] = 'TOTAL'
    return pd.concat([df, pd.DataFrame([total_row])], ignore_index=True)


def _bg_holdings_add_pct(frame: "pd.DataFrame") -> None:
    """Mutate *frame* in-place: add pnl_percentage and day_change_percentage columns."""
    if 'pnl' in frame.columns and 'inv_val' in frame.columns:
        frame['pnl_percentage'] = frame['pnl'] / frame['inv_val'] * 100
    if 'day_change_val' in frame.columns and 'cur_val' in frame.columns:
        frame['day_change_percentage'] = frame['day_change_val'] / frame['cur_val'] * 100


def _fetch_holdings_direct() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (row_df, summary_df) with real account codes (see _fetch_margins_direct)."""
    from backend.brokers import broker_apis
    raw = pd.concat(broker_apis.fetch_holdings(), ignore_index=True)

    if raw.empty or 'account' not in raw.columns:
        empty = pd.DataFrame(columns=['account', 'inv_val', 'cur_val', 'pnl', 'day_change_val',
                                      'pnl_percentage', 'day_change_percentage'])
        return raw, empty

    sum_cols = [c for c in ['inv_val', 'cur_val', 'pnl', 'day_change_val'] if c in raw.columns]
    grouped = raw.groupby('account')[sum_cols].sum().reset_index()
    _bg_holdings_add_pct(grouped)

    totals = grouped[sum_cols].sum().to_frame().T
    totals['account'] = 'TOTAL'
    _bg_holdings_add_pct(totals)

    summary = pd.concat([grouped, totals], ignore_index=True).fillna(0)
    return raw, summary


def _fetch_positions_direct() -> tuple[pd.DataFrame, pd.DataFrame]:
    from backend.brokers import broker_apis
    from backend.api.algo.pnl_math import apply_day_change_backstop
    raw = pd.concat(broker_apis.fetch_positions(), ignore_index=True)
    if raw.empty or 'account' not in raw.columns:
        empty = pd.DataFrame(columns=['account', 'pnl', 'day_change_val'])
        return raw, empty
    # Apply the shared Case 1 + Case 3 Day P&L backstop so this task's
    # summary agrees with the /api/positions route (both go through
    # `apply_day_change_backstop`). Without this, the NavStrip P "today"
    # slot ships lifetime pnl for new / fully-closed positions when
    # Kite's REST endpoint ships last_price=0 and the polars enrichment
    # gate zeroes day_change_val.
    raw = apply_day_change_backstop(raw)
    # Include day_change_val in the per-account groupby so the
    # performance TOTAL row has the intraday delta that
    # `_perf_extract_total_pnl_fields` feeds into NavStrip P slot 1.
    sum_cols = [c for c in ('pnl', 'day_change_val') if c in raw.columns]
    if sum_cols:
        grouped = raw.groupby('account')[sum_cols].sum().reset_index()
    else:
        grouped = pd.DataFrame(columns=['account'] + sum_cols)
    total_row = {'account': 'TOTAL'}
    for _c in sum_cols:
        total_row[_c] = grouped[_c].sum() if _c in grouped.columns else 0.0
    total   = pd.DataFrame([total_row])
    summary = pd.concat([grouped, total], ignore_index=True)
    return raw, summary


def _bg_build_underlying_keys(
    df_positions: "pd.DataFrame",
) -> "dict[str, str]":
    """Return {underlying_name: kite_ltp_key} for every distinct F&O symbol
    in df_positions.  Pure computation; no IO."""
    from backend.api.algo.derivatives import parse_tradingsymbol, option_underlying_quote_key
    out: dict[str, str] = {}
    for sym in df_positions['tradingsymbol'].dropna().astype(str).unique():
        parsed = parse_tradingsymbol(sym)
        if not parsed:
            continue
        name = parsed.get('root')
        ltp_key = option_underlying_quote_key(sym)
        if name and ltp_key:
            out.setdefault(name, ltp_key)
    return out


def _bg_extract_ltp_from_resp(
    underlyings: "dict[str, str]",
    resp: dict,
) -> "dict[str, float]":
    """Map broker.ltp() response back to {underlying_name: float}."""
    out: dict[str, float] = {}
    for name, key in underlyings.items():
        quote = resp.get(key) or {}
        ltp = quote.get('last_price')
        if ltp is None:
            continue
        try:
            out[name] = float(ltp)
        except (TypeError, ValueError):
            continue
    return out


def _resolve_spot_prices(df_positions: pd.DataFrame) -> dict[str, float]:
    """Fetch underlying spot LTPs for every distinct option/future
    underlying in the open position book. One broker.ltp call covers
    every flavour (NSE index, stock options, MCX commodity futures).

    Returns {underlying_name: ltp}. Empty dict on broker failure or
    empty book. Consumed by ctx.spot_prices for the expiry-aware
    grammar resolvers (is_itm / is_ntm).
    """
    if df_positions is None or df_positions.empty or 'tradingsymbol' not in df_positions.columns:
        return {}
    underlyings = _bg_build_underlying_keys(df_positions)
    if not underlyings:
        return {}
    try:
        from backend.brokers.registry import get_market_data_broker
        broker = get_market_data_broker()
        resp = broker.ltp(list(underlyings.values())) or {}
    except Exception as e:
        logger.debug(f"_resolve_spot_prices: broker.ltp failed: {e}")
        return {}
    return _bg_extract_ltp_from_resp(underlyings, resp)


# ---------------------------------------------------------------------------
# Async wrappers — run blocking calls in thread pool
# ---------------------------------------------------------------------------

async def _run(fn, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, fn, *args)


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def _load_market_from_db():
    """Return MarketResponse if a DB row exists and is <24h old, else None.
    The `refreshed_at` field is derived FROM `generated_at` (the Postgres
    timestamp of when the content was actually written to the DB), not
    from the persisted `refreshed_at` string. Both values come from the
    same write moment originally, but tying the rendered stamp to the
    SQL column makes it unambiguous: the timestamp the operator sees
    is exactly when this content was last updated, even if a future
    code path (e.g. a background re-formatter) updates the string field
    independently."""
    from datetime import datetime, timezone
    from backend.api.database import async_session
    from backend.api.models import MarketReport
    from backend.api.schemas import MarketResponse
    from backend.shared.helpers.date_time_utils import format_dual_tz

    try:
        async with async_session() as s:
            row = await s.get(MarketReport, 1)
        if not row:
            return None
        age = (datetime.now(timezone.utc) - row.generated_at).total_seconds()
        if age >= 86400:
            return None
        return MarketResponse(
            content=row.content,
            cycle_date=row.cycle_date,
            refreshed_at=format_dual_tz(row.generated_at),
        )
    except Exception as e:
        logger.error(f"Background: market DB load failed: {e}")
        return None


async def _save_market_to_db(resp) -> None:
    """Upsert id=1 row with the latest market report."""
    from datetime import datetime, timezone
    from backend.api.database import async_session
    from backend.api.models import MarketReport

    try:
        async with async_session() as s:
            row = await s.get(MarketReport, 1)
            now_utc = datetime.now(timezone.utc)
            if row:
                row.content = resp.content
                row.cycle_date = resp.cycle_date
                row.refreshed_at = resp.refreshed_at
                row.generated_at = now_utc
            else:
                s.add(MarketReport(
                    id=1, content=resp.content, cycle_date=resp.cycle_date,
                    refreshed_at=resp.refreshed_at, generated_at=now_utc,
                ))
            await s.commit()
        logger.info("Background: market report saved to DB")
    except Exception as e:
        logger.error(f"Background: market DB save failed: {e}")


async def _task_market(state: dict) -> None:
    """
    Startup: hydrate cache from DB if <24h old, else call Gemini + save.
    Then daily at 07:00 IST: call Gemini + save.
    """
    from backend.api.routes.market import fetch_fresh
    from backend.api.cache import _store
    import time as _time

    def _hydrate(resp):
        _store["market"] = (_time.monotonic() + 86400, resp)

    cached = await _load_market_from_db()
    if cached:
        _hydrate(cached)
        logger.info(f"Background: market cache hydrated from DB (cycle {cached.cycle_date})")
    else:
        try:
            result = await _run(fetch_fresh)
            if result is None:
                logger.warning("Background: Gemini empty at startup — leaving cache/DB untouched")
            else:
                _hydrate(result)
                await _save_market_to_db(result)
                logger.info(f"Background: market generated at startup (cycle {get_cycle_date()})")
        except Exception as e:
            logger.error(f"Background: market startup warm failed: {e}")

    while True:
        # `performance.market_refresh_time` is HH:MM in IST. Live-tunable
        # from /admin/settings; YAML `market_refresh_time` is the boot
        # fallback.
        from backend.shared.helpers.settings import get_string
        hhmm = get_string(
            "performance.market_refresh_time",
            str(config.get("market_refresh_time", "08:30")),
        )
        try:
            hour, minute = (int(x) for x in str(hhmm).split(":", 1))
        except Exception:
            hour, minute = 7, 0
        now = timestamp_indian()
        next_warm = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now >= next_warm:
            next_warm += timedelta(days=1)
        sleep_s = (next_warm - now).total_seconds()
        logger.info(f"Background: market task sleeping {sleep_s/3600:.1f}h until next warm at {hhmm} IST")
        await asyncio.sleep(sleep_s)

        try:
            result = await _run(fetch_fresh)
            if result is None:
                logger.warning("Background: Gemini empty on daily refresh — keeping previous report")
                continue
            _hydrate(result)
            await _save_market_to_db(result)
            logger.info(f"Background: market cache warmed for cycle {get_cycle_date()}")

            from backend.api.routes.ws import broadcast
            import json
            broadcast(json.dumps({"event": "market_updated", "refreshed_at": timestamp_display()}))
        except Exception as e:
            logger.error(f"Background: market warm failed: {e}")


_PERF_KICK_EVENT: asyncio.Event | None = None


def kick_performance() -> bool:
    """Wake `_task_performance` immediately instead of waiting for its
    next 5-min cycle. Called by the sim driver when an iteration
    auto-stops so the live engine resumes within one broker round-trip
    instead of leaving a 5-min agent gap. No-op if the event hasn't
    been initialized (pre-startup, or _task_performance not running)."""
    if _PERF_KICK_EVENT is None:
        return False
    _PERF_KICK_EVENT.set()
    return True


async def _perf_probe_open_segments(
    segments: list[dict],
    holiday_cache: dict,
    now,
) -> list[dict]:
    """Fan out `is_market_open` probes for every segment in parallel.
    Returns the subset whose exchange is currently open (calendar +
    live-quote probe). Each probe runs in `asyncio.to_thread` so the
    blocking Kite check doesn't stall the event loop."""
    def _probe_open(seg):
        return is_market_open(
            now, holiday_cache.get(seg['holiday_exchange'], set()),
            seg['hours_start'], seg['hours_end'],
            exchange=seg['holiday_exchange'],
        )
    open_results = await asyncio.gather(*(
        asyncio.to_thread(_probe_open, seg) for seg in segments
    ))
    return [seg for seg, ok in zip(segments, open_results) if ok]


async def _perf_fetch_all_broker_data() -> tuple:
    """Serial broker fetches: (df_holdings, sum_holdings, df_positions,
    sum_positions, df_margins). Serial by design — parallel calls raced
    the daily Kite token-refresh (each call kicks its own login + 2FA
    at 23h). Each op capped at 45s so a wedged cycle doesn't wedge the
    poller forever."""
    try:
        (df_holdings, sum_holdings) = await asyncio.wait_for(
            _run(_fetch_holdings_direct), timeout=45
        )
    except asyncio.TimeoutError:
        logger.warning("[BROKER-TIMEOUT] account=all op=holdings timeout=45s")
        df_holdings, sum_holdings = pd.DataFrame(), pd.DataFrame()

    try:
        (df_positions, sum_positions) = await asyncio.wait_for(
            _run(_fetch_positions_direct), timeout=45
        )
    except asyncio.TimeoutError:
        logger.warning("[BROKER-TIMEOUT] account=all op=positions timeout=45s")
        df_positions, sum_positions = pd.DataFrame(), pd.DataFrame()

    try:
        df_margins = await asyncio.wait_for(
            _run(_fetch_margins_direct), timeout=45
        )
    except asyncio.TimeoutError:
        logger.warning("[BROKER-TIMEOUT] account=all op=margins timeout=45s")
        df_margins = pd.DataFrame()

    return df_holdings, sum_holdings, df_positions, sum_positions, df_margins


def _bg_total_field(frame: "pd.DataFrame", col: str, default: float = 0.0) -> float:
    """Safe float accessor for a named column in a TOTAL-row frame."""
    if frame.empty or col not in frame.columns:
        return default
    val = frame[col].iloc[0]
    return float(val) if pd.notna(val) else default


def _perf_extract_total_pnl_fields(
    all_sum_h: pd.DataFrame,
    all_sum_p: pd.DataFrame,
) -> tuple[float, float, float, float]:
    """Pull (h_day, h_pnl, p_pnl, p_day) from the TOTAL row of the
    per-account summary frames. p_day falls back to p_pnl for pure-MIS
    books where the lifetime and today's deltas are the same number."""
    h_total = all_sum_h.loc[all_sum_h['account'] == 'TOTAL']
    p_total = all_sum_p.loc[all_sum_p['account'] == 'TOTAL']

    h_day = _bg_total_field(h_total, 'day_change_val')
    h_pnl = _bg_total_field(h_total, 'pnl')
    p_pnl = _bg_total_field(p_total, 'pnl')
    # p_day falls back to p_pnl for pure-MIS books
    p_day = _bg_total_field(p_total, 'day_change_val', default=p_pnl)
    return h_day, h_pnl, p_pnl, p_day


def _perf_append_intraday_equity(
    all_sum_h: pd.DataFrame,
    all_sum_p: pd.DataFrame,
    now,
    today,
) -> None:
    """Append one point to the intraday equity-curve buffer. Wipes on IST
    date rollover so the chart always reflects the current day only.
    Mutates the module-level `_intraday_equity` deque and
    `_intraday_equity_date` marker."""
    global _intraday_equity, _intraday_equity_date
    try:
        if _intraday_equity_date != today:
            _intraday_equity.clear()
            _intraday_equity_date = today

        h_day, h_pnl, p_pnl, p_day = _perf_extract_total_pnl_fields(
            all_sum_h, all_sum_p,
        )
        day_pnl = h_day + p_day
        cum_pnl = h_pnl + p_pnl
        _intraday_equity.append((
            now.isoformat(), day_pnl, cum_pnl,
            h_pnl, h_day, p_pnl, p_day,
        ))
        logger.info(
            f"Equity-curve point: day=₹{day_pnl:.0f} "
            f"cum=₹{cum_pnl:.0f} (H=₹{h_pnl:.0f} ΔH=₹{h_day:.0f} "
            f"P=₹{p_pnl:.0f} ΔP=₹{p_day:.0f}, n={len(_intraday_equity)})"
        )
    except Exception as _eq_err:
        logger.warning(f"Background: equity-curve append skipped: {_eq_err}")


async def _perf_send_open_summaries(
    open_segments: list[dict],
    seg_state: dict,
    now,
    today,
    open_offset: int,
    all_sum_h: pd.DataFrame,
    all_sum_p: pd.DataFrame,
    ist_display: str,
    df_margins: pd.DataFrame,
    df_positions: pd.DataFrame,
) -> None:
    """Fire the once-per-day open summary per open segment. Uses
    `seg_state[seg['name']]['last_open']` as the idempotency key."""
    from backend.shared.helpers.alert_utils import send_summary
    for seg in open_segments:
        ss = seg_state[seg['name']]
        open_trigger = now.replace(
            hour=seg['hours_start'].hour,
            minute=seg['hours_start'].minute,
            second=0, microsecond=0
        ) + timedelta(minutes=open_offset)
        if ss['last_open'] != today and now >= open_trigger:
            _label = seg['name'].capitalize()
            _dm = df_margins
            _dp = df_positions
            try:
                await _run(lambda: send_summary(all_sum_h, all_sum_p, ist_display,
                                                'open', label=_label,
                                                df_margins=_dm, df_positions=_dp))
                logger.info(f"Background: open summary sent for {seg['name']}")
            except Exception as e:
                logger.error(f"Background: open summary failed for {seg['name']}: {e}")
            ss['last_open'] = today


async def _perf_run_agent_engine(
    sim_active: bool,
    df_holdings: pd.DataFrame,
    df_positions: pd.DataFrame,
    df_margins: pd.DataFrame,
    sum_holdings: pd.DataFrame,
    sum_positions: pd.DataFrame,
    ist_display: str,
    now,
    seg_state: dict,
    alert_state: dict,
    segments: list[dict],
) -> None:
    """Run the v2 agent engine cycle with the just-refreshed portfolio
    context. Skipped while the simulator is active (sim owns run_cycle);
    also skipped on non-prod branches (main-only per CLAUDE.md). Alert
    state dict is threaded through so the grammar evaluator can read
    rate history and write suppression entries."""
    from backend.shared.helpers.utils import is_prod_branch
    if sim_active:
        logger.info("Background: simulator active — skipping real run_cycle (performance cache still fresh)")
        return
    if not is_prod_branch():
        # Mode 1 (dev) — the live agent engine only runs on main.
        # Dev's agent testing happens through the simulator, which
        # owns its own run_cycle invocation. Keeping the live
        # engine off dev avoids cross-process Kite contention with
        # prod AND makes "paper trade without fill simulation"
        # impossible by construction.
        return
    try:
        from backend.api.algo.agent_engine import run_cycle
        from backend.api.routes.algo import _broadcast_event
        # Phase 25 — expiry-aware agents need per-symbol position
        # rows + underlying spots. One broker.ltp covers every
        # distinct underlying; helpers fall back to {} on
        # broker failure so the expiry leaves skip silently
        # rather than 500. Both populate from the same
        # df_positions fetch above, so no extra Kite hit on
        # the position side.
        position_rows = (
            df_positions.to_dict("records")
            if (df_positions is not None and not df_positions.empty)
            else []
        )
        spot_prices = await _run(_resolve_spot_prices, df_positions)
        agent_context = {
            "sum_holdings": sum_holdings,
            "sum_positions": sum_positions,
            "df_margins": df_margins,
            "df_holdings": df_holdings,
            "df_positions": df_positions,
            "position_rows": position_rows,
            "spot_prices":   spot_prices,
            "ist_display": ist_display,
            "now": now,
            "seg_state": seg_state,
            # alert_state is the long-lived dict owned here (pnl_history,
            # session_start, last_alert buckets, funds_*). Passed so the
            # v2 grammar evaluator in run_cycle can read rate history
            # and write its own suppression entries without needing a
            # parallel state store.
            "alert_state": alert_state,
            "segments":    segments,
        }
        await run_cycle(agent_context, broadcast_fn=_broadcast_event)
    except Exception as ae:
        logger.error(f"Background: agent engine failed: {ae}")


def _perf_collect_book_pairs(
    df_holdings: pd.DataFrame,
    df_positions: pd.DataFrame,
) -> tuple[list[tuple[str, str]], set[tuple[str, str]]]:
    """Walk both holdings + positions DataFrames and return unique
    (tradingsymbol, exchange) pairs found on either book, plus the seen
    set. Used to seed the KiteTicker subscription universe."""
    book_pairs: list[tuple[str, str]] = []
    book_seen: set[tuple[str, str]] = set()
    for _df, _default_exch in (
        (df_holdings, "NSE"),
        (df_positions, "NFO"),
    ):
        if _df is not None and not _df.empty:
            for _row in _df.itertuples(index=False):
                _sym  = str(getattr(_row, "tradingsymbol", None) or "").strip().upper()
                _exch = str(getattr(_row, "exchange", None) or _default_exch).strip().upper()
                if _sym:
                    _k = (_sym, _exch)
                    if _k not in book_seen:
                        book_seen.add(_k)
                        book_pairs.append(_k)
    return book_pairs, book_seen


async def _bg_resolve_tokens_chunked(
    need_resolve: list[tuple[str, str]],
    rts_fn,
) -> list[tuple[int, str]]:
    """Resolve ticker tokens for *need_resolve* (sym, exch) pairs in chunks
    of 50, returning a flat list of (token, sym) ready for subscribe_with_sym."""
    CHUNK = 50
    all_batch: list[tuple[int, str]] = []
    for i in range(0, len(need_resolve), CHUNK):
        chunk = need_resolve[i: i + CHUNK]
        try:
            toks = await asyncio.gather(
                *(rts_fn(s, e) for s, e in chunk),
                return_exceptions=True,
            )
            all_batch.extend(
                (tok, sym)
                for (sym, _exch), tok in zip(chunk, toks)
                if tok is not None and not isinstance(tok, BaseException)
            )
        except Exception:
            pass
    return all_batch


async def _perf_subscribe_book_symbols(
    df_holdings: pd.DataFrame,
    df_positions: pd.DataFrame,
) -> None:
    """Phase 2 — union live positions + holdings + a daily_book DB
    snapshot backstop, resolve tokens for anything not yet subscribed,
    and hand the batch to the KiteTicker. Circuit-breaker-safe: the
    snapshot union keeps subscriptions warm for accounts whose broker
    is currently down."""
    try:
        from backend.brokers.kite_ticker import get_ticker as _get_ticker
        from backend.api.routes.quote import _resolve_token_for_sym as _rts
        _ticker = _get_ticker()
        _book_pairs, _book_seen = _perf_collect_book_pairs(df_holdings, df_positions)
        # Backstop: union daily_book snapshot for breaker-open accounts.
        try:
            _snap = await _snapshot_book_symbols(days=7)
            for _k in _snap:
                if _k not in _book_seen:
                    _book_seen.add(_k)
                    _book_pairs.append(_k)
        except Exception as _se:
            logger.debug(f"Background: snapshot book symbols skipped in perf: {_se}")
        # Resolve tokens for symbols not yet in the ticker.
        # O(1) check via has_sym() — avoids rebuilding the full set each cycle.
        _need_resolve = [
            (sym, exch) for sym, exch in _book_pairs
            if not _ticker.has_sym(sym)
        ]
        if _need_resolve:
            _all_batch = await _bg_resolve_tokens_chunked(_need_resolve, _rts)
            if _all_batch:
                _ticker.subscribe_with_sym(_all_batch)
    except Exception as _tke:
        logger.debug(f"Background: ticker book-subscribe skipped: {_tke}")


async def _bg_warm_holiday_cache(
    holiday_cache: dict,
    segments: list[dict],
    fetch_holidays,
) -> None:
    """Fill *holiday_cache* for any exchange not yet loaded this year."""
    for seg in segments:
        exch = seg['holiday_exchange']
        if exch not in holiday_cache:
            try:
                holiday_cache[exch] = await _run(fetch_holidays, exch)
            except Exception as e:
                logger.debug(f"Background: holiday load skipped for {exch}: {e}")
                holiday_cache[exch] = set()


def _bg_is_sim_active() -> bool:
    """Return True when a SimDriver is currently active (non-raising)."""
    try:
        from backend.api.algo.sim.driver import get_driver
        return bool(get_driver().active)
    except Exception:
        return False


async def _task_performance(state: dict) -> None:
    """Refresh performance data every N minutes during market hours."""
    from backend.brokers.broker_apis import fetch_holidays
    from backend.api.cache import invalidate
    from backend.api.routes.ws import broadcast
    import json

    # These used to read only from YAML; now pull live from the DB
    # settings cache (which reloads on every PATCH via /admin/settings),
    # falling back to YAML, then to the in-code default. That lets the
    # operator retune the performance cadence at runtime without a
    # redeploy. `interval` is picked up here once per task start; the
    # re-read happens each iteration so a settings change lands on the
    # next tick instead of after a service restart.
    from backend.shared.helpers.settings import get_int
    def _interval():
        return get_int("performance.refresh_interval",
                       config.get("performance_refresh_interval", 5))
    def _open_offset():
        return get_int("performance.open_summary_offset_min",
                       config.get("open_summary_offset_minutes", 15))

    seg_state   = _default_seg_state()
    alert_state = {}
    holiday_cache: dict = {}

    global _PERF_KICK_EVENT
    _PERF_KICK_EVENT = asyncio.Event()

    while True:
        # Re-read each iteration so a /admin/settings tweak lands on the
        # next cycle instead of after a service restart.
        interval    = _interval()
        open_offset = _open_offset()
        # asyncio.wait_for instead of plain sleep so the sim driver can
        # signal an immediate kick via kick_performance() when it
        # auto-stops. Without this, an auto-stopped sim left up to 5 min
        # where the live engine wasn't running (still waiting for the
        # next 5-min cycle) AND the sim engine was already inactive.
        try:
            await asyncio.wait_for(_PERF_KICK_EVENT.wait(), timeout=interval * 60)
        except asyncio.TimeoutError:
            pass
        _PERF_KICK_EVENT.clear()

        # Dev-idle gate — on non-main branches, when execution.dev_active
        # is False AND no sim/replay driver is running, skip the broker
        # fetch entirely. Picking PAPER/SIM/REPLAY from the navbar flips
        # dev_active=True; picking IDLE flips it back. Prod (main) always
        # passes this check. Stops dev from hammering broker APIs when no
        # operator is actively trading.
        from backend.shared.helpers.utils import is_engine_idle
        if is_engine_idle():
            continue

        now   = timestamp_indian()
        today = now.date()

        # Refresh holiday calendars at year boundary
        if not holiday_cache or state.get('_hol_year', None) != today.year:
            holiday_cache = {}
            state['_hol_year'] = today.year

        segments = _build_segments()
        await _bg_warm_holiday_cache(holiday_cache, segments, fetch_holidays)

        # Pass `exchange=` to is_market_open so the live-quote probe
        # can override the calendar verdict. Catches MCX evening
        # sessions on equity holidays (NSE's COM segment lists those
        # as closed but MCX is actually trading) and Muhurat days
        # the calendar doesn't list.
        #
        # Perf: offload to a thread because is_market_open → probe_market_active
        # → kite.quote() is a blocking HTTP call (requests/urllib3) inside the
        # asyncio main thread. py-spy caught a sample blocked in ssl.send.
        # Cache (60s TTL) usually short-circuits this, but on a cache miss the
        # loop stalls for ~100-200ms (longer on a Kite outage).
        open_segments = await _perf_probe_open_segments(segments, holiday_cache, now)

        if not open_segments:
            continue

        sim_active = _bg_is_sim_active()

        try:
            (df_holdings, sum_holdings, df_positions, sum_positions,
             df_margins) = await _perf_fetch_all_broker_data()

            ist_display = timestamp_display()
            perf_key    = get_nearest_time(interval=interval)

            # Full portfolio summaries — _fetch_holdings_direct /
            # _fetch_positions_direct already return (raw, summary) where
            # summary IS the per-account + TOTAL groupby aggregate with the
            # same columns that _summarise_holdings/positions would produce
            # from the unfiltered raw frame. Reuse them directly instead of
            # recomputing the same groupby.
            all_sum_h = sum_holdings
            all_sum_p = sum_positions

            # Intraday equity-curve — one point per tick during market hours.
            # Skipped while the simulator is active so fabricated P&L never
            # pollutes the real intraday history.
            if not sim_active:
                _perf_append_intraday_equity(all_sum_h, all_sum_p, now, today)

            await _perf_send_open_summaries(
                open_segments, seg_state, now, today, open_offset,
                all_sum_h, all_sum_p, ist_display, df_margins, df_positions,
            )

            # Loss alerts are now entirely owned by the v2 agent engine below
            # (loss-* BUILTIN_AGENTS). alert_utils.check_and_alert is retired.

            # Run agent engine with market data context — but skip entirely
            # while the simulator is active. The sim driver owns run_cycle
            # while it's running; mixing a live fire with a fabricated one
            # would corrupt rate history and spam the Telegram group.
            await _perf_run_agent_engine(
                sim_active, df_holdings, df_positions, df_margins,
                sum_holdings, sum_positions, ist_display, now, seg_state,
                alert_state, segments,
            )

            # Phase 2 — seed KiteTicker with any newly-discovered symbols
            # from the live positions + holdings book. The sparkline warm
            # task handles watchlist symbols; this covers the trading book
            # (F&O positions, held equities) which changes intraday.
            # subscribe() is idempotent — re-subscribing known tokens is a
            # no-op. We never unsubscribe stale symbols (Phase 2 simplicity).
            #
            # Backstop: when an account's circuit-breaker is open (e.g.
            # Dhan auth failure), its DataFrame is empty.  Union the live
            # pairs with the last-known symbols from daily_book so Kite
            # ticker subscriptions are maintained across breaker-open
            # periods.  _snapshot_book_symbols() is DB-backed and survives
            # conn_service restart + any unhealthy broker account.
            await _perf_subscribe_book_symbols(df_holdings, df_positions)

            # Invalidate only the caches this refresh actually renewed.
            # News / market / instruments have their own longer TTLs (days)
            # and don't change per tick — evicting them here used to force
            # a cold refetch on the next request for no benefit.
            for _k in ("holdings", "positions", "funds", "orders"):
                invalidate(_k)
            broadcast(json.dumps({
                "event":        "performance_updated",
                "refreshed_at": ist_display,
                "interval_key": perf_key,
            }))
            logger.info(f"Background: performance refreshed — {ist_display}")

        except Exception as e:
            logger.error(f"Background: performance refresh failed: {e}")


async def _task_close(state: dict) -> None:
    """Send close summary for each segment after its close time + offset."""
    from backend.brokers.broker_apis import fetch_holidays
    from backend.shared.helpers.alert_utils import send_summary
    from backend.shared.helpers.summarise import summarise_holdings as _summarise_holdings, summarise_positions as _summarise_positions

    from backend.shared.helpers.settings import get_int
    def _interval_close():
        return get_int("performance.refresh_interval",
                       config.get("performance_refresh_interval", 5))
    def _close_offset():
        return get_int("performance.close_summary_offset_min",
                       config.get("close_summary_offset_minutes", 15))

    seg_state     = state.setdefault('close_seg_state', _default_seg_state())

    while True:
        # Dev-idle gate (see _task_performance comment) — also skip the
        # close-summary task on dev when no operator activity.
        from backend.shared.helpers.utils import is_engine_idle
        if is_engine_idle():
            await asyncio.sleep(60)
            continue
        interval     = _interval_close()
        close_offset = _close_offset()
        await asyncio.sleep(interval * 60)

        now   = timestamp_indian()
        today = now.date()
        segments = _build_segments()

        for seg in segments:
            ss = seg_state[seg['name']]
            if ss['last_close'] == today:
                continue

            close_trigger = now.replace(
                hour=seg['hours_end'].hour,
                minute=seg['hours_end'].minute,
                second=0, microsecond=0
            ) + timedelta(minutes=close_offset)

            # Operator: "I don't see closing summary in telegram after
            # MCX closure" — observed on a partial-session day where
            # NSE was a holiday but MCX evening was open. Kite's MCX
            # holiday list flags today as closed (since one of the two
            # sessions skipped), so the previous `today not in h_set`
            # gate silently swallowed the summary. The close trigger
            # at 23:45 IST is outside the live-quote probe window so
            # `is_trading_day(..., now=...)` returns False too.
            #
            # Fix: drop the holiday gate from this path. The summary
            # is informational — firing on a true holiday at most
            # produces a one-line "no activity" Telegram, which is
            # cheap and clearly diagnostic. The `weekday() < 5` gate
            # still prevents weekend noise (markets are weekend-closed
            # globally; no broker activity to summarise).
            if now.weekday() < 5 and now >= close_trigger:
                try:
                    try:
                        (df_h, sum_h), (df_p, sum_p) = await asyncio.wait_for(
                            _run(lambda: (_fetch_holdings_direct(), _fetch_positions_direct())),
                            timeout=45,
                        )
                    except asyncio.TimeoutError:
                        logger.warning("[BROKER-TIMEOUT] account=all op=holdings+positions timeout=45s")
                        df_h, sum_h, df_p, sum_p = pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

                    try:
                        df_margins = await asyncio.wait_for(
                            _run(_fetch_margins_direct), timeout=45
                        )
                    except asyncio.TimeoutError:
                        logger.warning("[BROKER-TIMEOUT] account=all op=margins timeout=45s")
                        df_margins = pd.DataFrame()

                    ist_display = timestamp_display()

                    _sh = _summarise_holdings(df_h, sum_h, None)
                    _sp = _summarise_positions(df_p)
                    _label = seg['name'].capitalize()
                    _dm = df_margins
                    _dp = df_p
                    await _run(lambda: send_summary(_sh, _sp, ist_display, 'close',
                                                    label=_label,
                                                    df_margins=_dm, df_positions=_dp))
                    ss['last_close'] = today
                    logger.info(f"Background: close summary sent for {seg['name']}")
                except Exception as e:
                    logger.error(f"Background: close summary failed for {seg['name']}: {e}")


# ---------------------------------------------------------------------------
# Litestar lifecycle hooks
# ---------------------------------------------------------------------------

def _bg_parse_expiry_check_time(cfg_time: str) -> tuple[int, int]:
    """Parse 'HH:MM' string → (hh, mm). Falls back to (9, 20) on bad input."""
    try:
        hh_s, mm_s = cfg_time.split(":", 1)
        hh, mm = int(hh_s), int(mm_s)
        if not (0 <= hh < 24 and 0 <= mm < 60):
            raise ValueError("out of range")
        return hh, mm
    except Exception:
        logger.warning(f"Background: algo.expiry_check_time={cfg_time!r} invalid, defaulting to 09:20")
        return 9, 20


async def _task_expiry_check() -> None:
    """Check once daily at 09:20 IST if today is an expiry day and auto-start the engine.

    SAFETY GATE — prod only. expiry.py → chase.py::_place_order calls
    broker.place_order() directly with NO paper_trading_mode / branch
    check, so without this gate dev would race prod to close the same
    Kite-account positions tomorrow (both environments share
    secrets.yaml::kite_accounts). The bg-expiry task itself runs on
    both, but exits immediately on non-main branches.
    """
    from backend.shared.helpers.utils import is_prod_branch
    if not is_prod_branch():
        logger.info("Background: bg-expiry inert on non-main branch "
                    "(expiry close orders are prod-only — see CLAUDE.md)")
        return

    from backend.api.algo.expiry import ExpiryEngine
    from backend.api.routes.algo import _broadcast_event

    from backend.shared.helpers.settings import get_string
    while True:
        now = timestamp_indian()
        # Schedule for algo.expiry_check_time IST daily (default 09:20).
        # Operator can update via /admin/settings (e.g. "15:00" for an
        # EOD-only close window) — re-read each loop so changes apply
        # on the next cycle without a service restart.
        cfg_time = get_string("algo.expiry_check_time", "09:20") or "09:20"
        hh, mm = _bg_parse_expiry_check_time(cfg_time)
        check_time = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now >= check_time:
            check_time += timedelta(days=1)
        sleep_s = (check_time - now).total_seconds()
        logger.info(f"Background: expiry check sleeping {sleep_s/3600:.1f}h until {check_time.strftime('%H:%M')} IST")
        await asyncio.sleep(sleep_s)

        try:
            engine = ExpiryEngine(on_event=_broadcast_event)
            # Quick scan to see if any positions expire today
            positions = await _run(engine._fetch_option_positions)
            today = timestamp_indian().date()
            expiring = [p for p in positions if p.expiry == today]

            if expiring:
                logger.info(f"Background: expiry day detected — {len(expiring)} option positions expiring today")
                _broadcast_event("expiry_day_detected", {"count": len(expiring)})
                # Run full expiry engine
                await engine.run()
            else:
                logger.info("Background: no option positions expiring today")
        except Exception as e:
            logger.error(f"Background: expiry check failed: {e}")


async def _task_nav_compute() -> None:
    """
    Firm-level NAV snapshot daily at 16:00 IST (15 min after the
    per-strategy snapshot task; 30 min after NSE equity close so the
    day's MTM is settled). Writes one row per day into `nav_daily`
    via the idempotent upsert in `nav.write_nav_snapshot()`.

    On boot, if today's NAV row is missing AND it's already past
    16:00 IST AND markets are closed, fire one immediately so the
    /nav page doesn't show a stale curve. Otherwise sleep to the
    next 16:00 IST and tick from there.

    Per-day failures log + drop; the next day's run picks up the
    cadence on its own.
    """
    from backend.api.algo.nav import write_nav_snapshot
    from backend.shared.helpers.date_time_utils import timestamp_indian
    import asyncio as _asyncio

    while True:
        now_ist = timestamp_indian()
        target = now_ist.replace(hour=16, minute=0, second=0, microsecond=0)
        if now_ist >= target:
            target = target + timedelta(days=1)
        sleep_s = max(0, (target - now_ist).total_seconds())
        logger.info(
            f"_task_nav_compute: sleeping {sleep_s/3600:.2f}h "
            f"until {target.isoformat()}"
        )
        await _asyncio.sleep(sleep_s)
        try:
            snap = await write_nav_snapshot()
            logger.info(
                f"_task_nav_compute: wrote NAV ₹{snap['nav']:,.0f} for "
                f"{timestamp_indian().date().isoformat()}"
            )
            try:
                from backend.api.audit import write_audit_event
                write_audit_event(
                    category="system.nav",
                    action="NAV_SNAPSHOT",
                    actor_username="system",
                    actor_role="system",
                    target_type="nav_daily",
                    target_id=timestamp_indian().date().isoformat(),
                    summary=(f"₹{snap['nav']:,.0f} · cash=₹{snap.get('cash_total',0):,.0f}"
                             f" · pos=₹{snap.get('positions_mtm',0):,.0f}"
                             f" · hold=₹{snap.get('holdings_mtm',0):,.0f}")[:1000],
                )
            except Exception:
                pass
        except Exception as exc:
            logger.warning(f"_task_nav_compute: cycle failed: {exc}")
            try:
                from backend.api.audit import write_audit_event
                write_audit_event(
                    category="system.nav",
                    action="NAV_SNAPSHOT_FAILED",
                    actor_username="system",
                    actor_role="system",
                    target_type="nav_daily",
                    target_id=timestamp_indian().date().isoformat(),
                    summary=f"compute failed: {exc}"[:1000],
                    status_code=500,
                )
            except Exception:
                pass


async def _task_monthly_statement() -> None:
    """
    Auto-email LP monthly statements. Wakes daily at 02:00 IST and
    processes any (LP, prior-month) pair that doesn't yet have a
    `monthly_statements` row.

    Cadence rationale:
    - 02:00 IST is well outside the broker-quota window and gives
      `_task_nav_compute` (16:00 IST the prior day) ten hours to
      settle the period's closing NAV.
    - "Prior month" is derived from today's IST date, so the first
      wake on or after the 1st of any month fires. If the server is
      down on the 1st, the 2nd's wake catches up — same row, same
      unique-key, same idempotency guarantee.
    - The DB unique constraint on (user, year, month) is the actual
      lock; if two wakes race (operator manually triggers + cron
      fires), the second INSERT 23505s and we skip cleanly.

    Failures (PDF render, SMTP) land in `monthly_statements.error`
    so admin can inspect; manually deleting the row queues a retry
    on the next wake.

    Gated by `is_enabled('mail')` so dev branches don't blast LPs
    with statements.
    """
    import asyncio as _asyncio
    from datetime import date as _date
    from sqlalchemy import select as _select
    from backend.api.database import async_session
    from backend.api.models import MonthlyStatement, User
    from backend.shared.helpers.date_time_utils import timestamp_indian
    from backend.shared.helpers.utils import is_enabled

    while True:
        now_ist = timestamp_indian()
        target = now_ist.replace(hour=2, minute=0, second=0, microsecond=0)
        if now_ist >= target:
            target = target + timedelta(days=1)
        sleep_s = max(0, (target - now_ist).total_seconds())
        logger.info(
            f"_task_monthly_statement: sleeping {sleep_s/3600:.2f}h "
            f"until {target.isoformat()}"
        )
        await _asyncio.sleep(sleep_s)

        # Two gates: branch-level mail capability (dev/prod) +
        # operator-opt-in setting. The setting defaults to False so
        # deploying this code doesn't auto-fire to every LP at the
        # next 02:00 IST — operator must flip it on from /admin/
        # settings after validating PDFs via the admin Preview
        # button.
        if not is_enabled("mail"):
            logger.info("_task_monthly_statement: mail capability off, skipping")
            continue
        from backend.shared.helpers.settings import get_bool as _get_bool
        if not _get_bool("notifications.monthly_statement_email", False):
            logger.info("_task_monthly_statement: setting opt-in off, skipping")
            continue

        # Prior-month period = (year, month) before today's IST.
        today = timestamp_indian().date()
        first_of_this_month = _date(today.year, today.month, 1)
        prior_period_end = first_of_this_month - timedelta(days=1)
        period_year  = prior_period_end.year
        period_month = prior_period_end.month

        try:
            async with async_session() as s:
                eligible = (await s.execute(
                    _select(User).where(
                        User.is_active.is_(True),
                        User.share_pct > 0,
                        User.email.is_not(None),
                        User.email != "",
                    )
                )).scalars().all()
                # IDs of users already sent this period — single query
                # to avoid one round-trip per LP. With <10 LPs this is
                # negligible either way, but keeps the pattern clean.
                already_sent = (await s.execute(
                    _select(MonthlyStatement.user_id).where(
                        MonthlyStatement.period_year  == period_year,
                        MonthlyStatement.period_month == period_month,
                    )
                )).scalars().all()
            sent_ids = set(already_sent)
            pending = [u for u in eligible if u.id not in sent_ids]
            if not pending:
                logger.info(
                    f"_task_monthly_statement: nothing to send for "
                    f"{period_year}-{period_month:02d} (eligible={len(eligible)})"
                )
                continue

            logger.info(
                f"_task_monthly_statement: processing {len(pending)} LPs "
                f"for period {period_year}-{period_month:02d}"
            )
            sent_ok = 0
            for user in pending:
                await _send_one_monthly_statement(user, period_year, period_month)
                sent_ok += 1
                # Gentle pacing so a burst of SMTP doesn't get
                # rate-limited by Hostinger. Even 10 LPs only costs
                # 10s total.
                await _asyncio.sleep(1)

            logger.info(
                f"_task_monthly_statement: done "
                f"({sent_ok}/{len(pending)} sent)"
            )

        except Exception as exc:
            logger.warning(f"_task_monthly_statement: cycle failed: {exc}")


async def _bg_compute_and_send_statement(
    user,
    period_year: int,
    period_month: int,
    compute_statement,
    render_statement_pdf,
    send_email,
) -> tuple[Optional[str], bytes, list[str]]:
    """Compute, render, and email one LP's monthly statement.

    Returns (error_str_or_None, pdf_bytes, recipients)."""
    import asyncio as _asyncio
    error: Optional[str] = None
    pdf_bytes: bytes = b""
    recipients: list[str] = []
    try:
        data = await compute_statement(user.id, period_year, period_month)
        if data is None:
            error = "No NAV data for this period"
        else:
            pdf_bytes = await _asyncio.to_thread(render_statement_pdf, data)
            recipients = [user.email]
            subject = f"RamboQuant statement — {data.period_start.strftime('%b %Y')}"
            html_body = _monthly_statement_html(user, data)
            ok, msg = await _asyncio.to_thread(
                send_email,
                user.display_name or user.username,
                user.email,
                subject,
                html_body,
                attachments=[(
                    pdf_bytes,
                    f"ramboquant_{period_year:04d}_{period_month:02d}.pdf",
                    "application/pdf",
                )],
            )
            if not ok:
                error = f"SMTP: {msg}"
    except Exception as exc:
        error = f"render/send: {exc}"
    return error, pdf_bytes, recipients


def _bg_statement_audit(
    user,
    period_year: int,
    period_month: int,
    error: Optional[str],
    pdf_bytes: bytes,
    recipients: list[str],
) -> None:
    """Write the monthly-statement audit row (non-raising)."""
    try:
        from backend.api.audit import write_audit_event
        write_audit_event(
            category="system.statement",
            action=("STATEMENT_SENT" if not error else "STATEMENT_FAILED"),
            actor_username="system",
            actor_role="system",
            target_type="user",
            target_id=str(user.id),
            summary=(f"{period_year}-{period_month:02d} → "
                     f"{', '.join(recipients) or '?'}"
                     + (f" · ERROR: {error}" if error else
                        f" · {len(pdf_bytes)} bytes"))[:1000],
            status_code=(200 if not error else 500),
        )
    except Exception as _aud_e:
        logger.debug(f"statement audit skipped: {_aud_e}")


async def _send_one_monthly_statement(user, period_year: int, period_month: int) -> None:
    """Helper: compute → render → email → audit for one LP. Each
    failure surface inserts a `monthly_statements` row with `error`
    set so the operator can see what went wrong; deleting the row
    requeues the LP for the next bg wake."""
    import asyncio as _asyncio
    from datetime import datetime as _datetime, timezone as _tz
    from sqlalchemy.exc import IntegrityError as _IntegrityError
    from backend.api.algo.investor_statement import (
        compute_statement, render_statement_pdf,
    )
    from backend.api.database import async_session
    from backend.api.models import MonthlyStatement
    from backend.shared.helpers.mail_utils import send_email

    error, pdf_bytes, recipients = await _bg_compute_and_send_statement(
        user, period_year, period_month,
        compute_statement, render_statement_pdf, send_email,
    )

    try:
        async with async_session() as s:
            row = MonthlyStatement(
                user_id=user.id,
                period_year=period_year,
                period_month=period_month,
                sent_at=(_datetime.now(_tz.utc) if not error else None),
                recipients_json=recipients,
                pdf_size_bytes=len(pdf_bytes) or None,
                error=error,
            )
            s.add(row)
            await s.commit()
    except _IntegrityError:
        # Concurrent run inserted first — fine. We've already sent
        # the email; the prior row is the source of truth.
        logger.info(
            f"_send_one_monthly_statement: dup row for u={user.id} "
            f"{period_year}-{period_month:02d} (race / catch-up)"
        )
    except Exception as exc:
        # Row insert failed but email may have gone out. Log loudly
        # — next day's run will re-fire (no row = LP gets a second
        # copy). Operator should manually delete + re-trigger
        # corrective action.
        logger.error(
            f"_send_one_monthly_statement: audit row insert failed for "
            f"u={user.id} {period_year}-{period_month:02d}: {exc}"
        )

    if error:
        logger.warning(
            f"_send_one_monthly_statement: u={user.id} "
            f"{period_year}-{period_month:02d}: {error}"
        )
    else:
        logger.info(
            f"_send_one_monthly_statement: u={user.id} "
            f"{period_year}-{period_month:02d}: sent ({len(pdf_bytes)} bytes)"
        )
    _bg_statement_audit(user, period_year, period_month, error, pdf_bytes, recipients)


def _monthly_statement_html(user, data) -> str:
    """Cream/champagne HTML body matching the LP portal palette.
    Plain inline-styled HTML so it renders consistently across
    Gmail / Outlook / Apple Mail without CSS class support."""
    period_label = data.period_start.strftime("%b %Y")
    fmt_inr = lambda v: _html_inr(v)
    fmt_pct = lambda v: "—" if v is None else f"{'+' if v >= 0 else ''}{v * 100:.2f}%"
    pnl_colour = lambda v: "#14653a" if (v or 0) > 0 else ("#962d2d" if (v or 0) < 0 else "#2a2418")

    return f"""\
<!DOCTYPE html>
<html><body style="margin:0; padding:0; background:#fdfaf2; color:#2a2418; font-family:-apple-system,BlinkMacSystemFont,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#fdfaf2; padding:20px 0;">
  <tr><td align="center">
    <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px; background:#ffffff; border:1px solid #e7e0cf; border-radius:8px;">
      <tr><td style="background:#d4920c; padding:14px 22px;">
        <div style="color:#ffffff; font-weight:800; font-size:16px; letter-spacing:0.06em;">RAMBOQUANT ANALYTICS LLP</div>
        <div style="color:#fffaee; font-size:11px; margin-top:2px;">Statement of Account · {period_label}</div>
      </td></tr>
      <tr><td style="padding:22px 24px;">
        <p style="margin:0 0 14px; font-size:14px;">Hello {user.display_name or user.username},</p>
        <p style="margin:0 0 18px; font-size:13px; line-height:1.55;">
          Your RamboQuant statement for <strong>{period_label}</strong> is attached. A quick summary:
        </p>
        <table width="100%" cellpadding="0" cellspacing="0" style="font-size:13px; margin-bottom:18px;">
          <tr><td style="padding:4px 0; color:#8b7340;">Closing slice</td>
              <td align="right" style="padding:4px 0; font-weight:700; color:#d4920c;">{fmt_inr(data.closing_share)}</td></tr>
          <tr><td style="padding:4px 0; color:#8b7340;">Period P&L</td>
              <td align="right" style="padding:4px 0; font-weight:700; color:{pnl_colour(data.share_period_delta)};">
                {fmt_inr(data.share_period_delta)} ({fmt_pct(data.share_period_pct)})
              </td></tr>
          <tr><td style="padding:4px 0; color:#8b7340;">Since-inception P&L</td>
              <td align="right" style="padding:4px 0; font-weight:700; color:{pnl_colour(data.cumulative_pnl)};">
                {fmt_inr(data.cumulative_pnl)} ({fmt_pct(data.cumulative_pnl_pct)})
              </td></tr>
          <tr><td style="padding:4px 0; color:#8b7340;">Your share of fund</td>
              <td align="right" style="padding:4px 0; font-weight:700;">{data.share_pct:.2f}%</td></tr>
        </table>
        <p style="margin:0 0 14px; font-size:12px; color:#8b7340; line-height:1.55;">
          The attached PDF carries the full breakdown — daily NAV, opening / closing
          slice, and the firm's NAV movement for the period.
        </p>
        <p style="margin:0; font-size:12px; color:#8b7340; line-height:1.55;">
          For questions or to update your contact details, reply to this email.
        </p>
      </td></tr>
      <tr><td style="padding:16px 24px; border-top:1px solid #e7e0cf; font-size:11px; color:#8b7340;">
        RamboQuant Analytics LLP · <a href="https://ramboq.com" style="color:#d4920c; text-decoration:none;">ramboq.com</a>
      </td></tr>
    </table>
  </td></tr>
</table>
</body></html>"""


def _html_inr(v: Optional[float]) -> str:
    if v is None:
        return "—"
    abs_v = abs(v)
    sign = "-" if v < 0 else ""
    if abs_v >= 1e7:
        return f"{sign}₹{abs_v / 1e7:,.2f} Cr"
    if abs_v >= 1e5:
        return f"{sign}₹{abs_v / 1e5:,.2f} L"
    return f"{sign}₹{int(round(abs_v)):,}"


async def _task_strategy_snapshot() -> None:
    """
    Slice 7c — daily per-strategy roll-up at 15:45 IST (10 min after
    NSE equity close, so the day's intraday closes are settled).
    Writes one row per active strategy into `strategy_snapshots` with:
      open_lots_count, open_notional, realised_pnl, unrealised_pnl.

    Idempotent — `UNIQUE(strategy_id, as_of_date)` on the table; an
    INSERT … ON CONFLICT DO UPDATE keeps re-runs (manual operator
    triggers, restart-while-running) safe.

    Powers the per-strategy P&L curve on /strategies/{id}. Until this
    task fires for the first time the detail page shows the "no
    snapshot yet" placeholder; after that the curve renders.

    Failure of one strategy's roll-up doesn't break the others —
    each is in its own try/except.
    """
    import asyncio as _asyncio
    from datetime import date
    from backend.api.database import async_session
    from backend.api.models import Strategy, StrategyLot, StrategySnapshot, AlgoOrder
    from backend.api.algo.lot_ledger import (
        compute_strategy_pnl, compute_unrealised_marked_to_ltp,
    )
    from sqlalchemy import select as _select, func as _func
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from backend.shared.helpers.date_time_utils import timestamp_indian

    async def _do_snapshot() -> int:
        async with async_session() as s:
            strategies = (await s.execute(
                _select(Strategy).where(Strategy.is_active.is_(True))
            )).scalars().all()
            today_ist = timestamp_indian().date()
            written = 0
            _open_states = ("OPEN", "CHASING", "PENDING")
            for strat in strategies:
                try:
                    pnl = await compute_strategy_pnl(s, strat.id)
                    # Open notional — sum (remaining_qty × open_price)
                    # across open lots. Approximate (not LTP-marked)
                    # until the LTP pass lands.
                    notional = (await s.execute(
                        _select(_func.coalesce(
                            _func.sum(StrategyLot.remaining_qty * StrategyLot.open_price),
                            0.0,
                        )).where(StrategyLot.strategy_id == strat.id,
                                 StrategyLot.remaining_qty > 0)
                    )).scalar_one() or 0.0
                    # Unrealised — LTP-marked when the ledger has
                    # open lots (slice 7d); falls back to AlgoOrder.
                    # pnl SUM for strategies with no ledger entries
                    # OR when the LTP feed is unavailable.
                    if pnl["open_lots_count"] > 0:
                        mtm = await compute_unrealised_marked_to_ltp(s, strat.id)
                        if mtm is not None:
                            unrealised = mtm
                        else:
                            unrealised = (await s.execute(
                                _select(_func.coalesce(_func.sum(AlgoOrder.pnl), 0.0))
                                .where(AlgoOrder.strategy_id == strat.id,
                                       AlgoOrder.status.in_(_open_states))
                            )).scalar_one() or 0.0
                    else:
                        unrealised = (await s.execute(
                            _select(_func.coalesce(_func.sum(AlgoOrder.pnl), 0.0))
                            .where(AlgoOrder.strategy_id == strat.id,
                                   AlgoOrder.status.in_(_open_states))
                        )).scalar_one() or 0.0
                    stmt = pg_insert(StrategySnapshot).values(
                        strategy_id=strat.id,
                        as_of_date=today_ist,
                        open_lots_count=pnl["open_lots_count"],
                        open_notional=float(notional or 0.0),
                        realised_pnl=pnl["realised_pnl"],
                        unrealised_pnl=float(unrealised or 0.0),
                    ).on_conflict_do_update(
                        index_elements=["strategy_id", "as_of_date"],
                        set_=dict(
                            open_lots_count=pnl["open_lots_count"],
                            open_notional=float(notional or 0.0),
                            realised_pnl=pnl["realised_pnl"],
                            unrealised_pnl=float(unrealised or 0.0),
                        ),
                    )
                    await s.execute(stmt)
                    written += 1
                except Exception as exc:
                    logger.warning(
                        f"strategy_snapshot: failed for strategy "
                        f"{strat.slug!r} (id={strat.id}): {exc}"
                    )
            await s.commit()
            return written

    while True:
        # Schedule at 15:45 IST every day. Sleep until then; on
        # boot if it's already past 15:45 the loop wakes
        # immediately, fires once, then sleeps to the next day.
        now_ist = timestamp_indian()
        target = now_ist.replace(hour=15, minute=45, second=0, microsecond=0)
        if now_ist >= target:
            target = target.replace(day=now_ist.day) + timedelta(days=1)
        sleep_s = max(0, (target - now_ist).total_seconds())
        logger.info(
            f"_task_strategy_snapshot: sleeping {sleep_s/3600:.2f}h "
            f"until {target.isoformat()}"
        )
        await _asyncio.sleep(sleep_s)
        try:
            written = await _do_snapshot()
            logger.info(
                f"_task_strategy_snapshot: wrote {written} per-strategy "
                f"snapshot rows for {timestamp_indian().date().isoformat()}"
            )
        except Exception as exc:
            logger.warning(f"_task_strategy_snapshot: cycle failed: {exc}")


async def _task_daily_snapshot() -> None:
    """
    Two-pass settlement snapshot.

    Whatever data is present at market close IS the EOD snapshot — no
    separate close-triggered write is needed.  The two settlement passes
    capture exchange-published prices that are only available after close:

      NSE settlement — 16:15 IST: NSE OCP/closing-session prices settled.
      MCX settlement — 00:15 IST: MCX settlement prices.
          MCX closes at 23:30 on trade-date D; 00:15 fires on calendar D+1.
          Dedup is keyed on trade-date D (yesterday when 00:15 ticks).

    Startup: fires once when both markets are closed so a service restart
    has data immediately without waiting until 16:15.  Skipped during
    market hours to avoid polluting daily_book with mid-session LTPs
    (observed incident 2026-06-22).
    """
    from backend.api.algo.daily_snapshot import snapshot_daily_book
    from backend.shared.helpers.date_time_utils import (
        timestamp_indian, is_market_open,
    )
    from backend.brokers.broker_apis import fetch_holidays

    # ── helpers ────────────────────────────────────────────────────────

    async def _probe_nse_mcx(now) -> tuple[bool, bool]:
        """Return (nse_open, mcx_open) using the full holiday pipeline."""
        segs = _build_segments()
        holiday_cache: dict[str, set] = {}
        for seg in segs:
            exch = seg['holiday_exchange']
            if exch not in holiday_cache:
                try:
                    holiday_cache[exch] = await _run(fetch_holidays, exch)
                except Exception:
                    holiday_cache[exch] = set()

        def _is_open(seg) -> bool:
            return is_market_open(
                now,
                holiday_cache.get(seg['holiday_exchange'], set()),
                seg['hours_start'],
                seg['hours_end'],
                exchange=seg['holiday_exchange'],
            )

        nse_segs = [s for s in segs if s['holiday_exchange'] == 'NSE']
        mcx_segs = [s for s in segs if s['holiday_exchange'] == 'MCX']
        nse_open = any(
            ok for ok in await asyncio.gather(
                *(asyncio.to_thread(_is_open, s) for s in nse_segs)
            )
        ) if nse_segs else False
        mcx_open = any(
            ok for ok in await asyncio.gather(
                *(asyncio.to_thread(_is_open, s) for s in mcx_segs)
            )
        ) if mcx_segs else False
        return nse_open, mcx_open

    async def _fire_snapshot(label: str) -> None:
        try:
            result = await snapshot_daily_book()
            logger.info(
                f"Background: daily snapshot [{label}] — "
                f"accounts={result['accounts']} "
                f"h={result['holdings_rows']} p={result['positions_rows']} "
                f"t={result['trades_rows']} errors={result['errors']}"
            )
        except Exception as e:
            logger.error(f"Background: daily snapshot [{label}] failed: {e}")

    # ── startup snapshot (closed hours only) ───────────────────────────
    _now_ist = timestamp_indian()
    _nse_open, _mcx_open = await _probe_nse_mcx(_now_ist)
    if _nse_open or _mcx_open:
        logger.info(
            f"Background: skipping startup daily snapshot — markets open "
            f"(NSE={_nse_open}, MCX={_mcx_open}). Settlement passes still fire."
        )
    else:
        await _fire_snapshot("startup")

    # ── settlement pass deduplication (date | None) ────────────────────
    _nse_settlement_done: Optional[date] = None
    _mcx_settlement_done: Optional[date] = None

    _NSE_SETTLEMENT_H, _NSE_SETTLEMENT_M = 16, 15
    _MCX_SETTLEMENT_H, _MCX_SETTLEMENT_M =  0, 15
    _POLL_INTERVAL_S = 30

    # ── main loop ──────────────────────────────────────────────────────
    while True:
        await asyncio.sleep(_POLL_INTERVAL_S)
        now   = timestamp_indian()
        today = now.date()

        # ---- NSE settlement: 16:15 IST --------------------------------
        if (now.time() >= dtime(_NSE_SETTLEMENT_H, _NSE_SETTLEMENT_M)
                and _nse_settlement_done != today):
            logger.info("Background: 16:15 IST — firing NSE settlement snapshot")
            await _fire_snapshot("nse-settlement")
            _nse_settlement_done = today

        # ---- MCX settlement: 00:15 IST (calendar D+1, trade-date D) ---
        # Guard to [00:15, 02:00) IST prevents a daytime restart from
        # triggering a spurious mid-session MCX snapshot.
        if dtime(_MCX_SETTLEMENT_H, _MCX_SETTLEMENT_M) <= now.time() < dtime(2, 0):
            _mcx_trade_date = today - timedelta(days=1)
            if _mcx_settlement_done != _mcx_trade_date:
                logger.info(
                    f"Background: 00:15 IST — firing MCX settlement snapshot "
                    f"(trade-date {_mcx_trade_date})"
                )
                await _fire_snapshot("mcx-settlement")
                _mcx_settlement_done = _mcx_trade_date


async def _task_instruments() -> None:
    """Warm the Kite instrument cache once at startup and daily at 08:00 IST."""
    from backend.api.routes.instruments import _fetch_instruments
    from backend.api.cache import _store
    import time as _time

    async def _warm():
        try:
            result = await _run(_fetch_instruments)
            _store["instruments"] = (_time.monotonic() + 86400, result)
            logger.info(f"Background: instruments cache warmed ({result.count} rows)")
        except Exception as e:
            logger.error(f"Background: instruments warm failed: {e}")

    await _warm()

    while True:
        now = timestamp_indian()
        # Refresh at 08:00 IST daily (Kite master updates ~07:30)
        next_warm = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if now >= next_warm:
            next_warm += timedelta(days=1)
        sleep_s = (next_warm - now).total_seconds()
        logger.info(f"Background: instruments task sleeping {sleep_s/3600:.1f}h until next warm")
        await asyncio.sleep(sleep_s)
        await _warm()


async def _task_sim_cleanup() -> None:
    """
    Prune sim_iterations + their related sim_mode agent_events and
    mode='sim' algo_orders older than `simulator.iteration_retention_days`
    (default 30). Runs once daily at 03:00 IST (markets closed; sim is
    blocked during market hours anyway so no risk of touching active state).
    Setting `simulator.iteration_retention_days = 0` disables auto-purge.
    """
    from backend.api.database import async_session
    from backend.api.models import SimIteration, AgentEvent, AlgoOrder
    from backend.shared.helpers.settings import get_int
    from sqlalchemy import delete as sql_delete, and_

    async def _purge_once():
        days = get_int("simulator.iteration_retention_days", 30)
        if days <= 0:
            logger.info("Background: sim cleanup disabled (retention_days=0)")
            return
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        try:
            async with async_session() as s:
                # Purge AlgoOrders + AgentEvents created BEFORE the cutoff
                # AND tagged with sim mode. Conservative: timestamp window
                # plus the mode/sim_mode predicate so we never touch a live
                # row even if its timestamp falls in the same window.
                ao = await s.execute(
                    sql_delete(AlgoOrder).where(
                        and_(AlgoOrder.mode == 'sim', AlgoOrder.created_at < cutoff)
                    )
                )
                ae = await s.execute(
                    sql_delete(AgentEvent).where(
                        and_(AgentEvent.sim_mode.is_(True), AgentEvent.timestamp < cutoff)
                    )
                )
                # Finally drop the SimIteration parent rows.
                si = await s.execute(
                    sql_delete(SimIteration).where(SimIteration.started_at < cutoff)
                )
                await s.commit()
                logger.info(
                    f"Background: sim cleanup purged "
                    f"{si.rowcount or 0} iterations, "
                    f"{ae.rowcount or 0} sim events, "
                    f"{ao.rowcount or 0} sim orders (older than {days} days)"
                )
        except Exception as e:
            logger.error(f"Background: sim cleanup failed: {e}")

    await asyncio.sleep(30)  # let the rest of startup settle first
    await _purge_once()

    while True:
        # Daily at 03:00 IST — well outside market hours.
        now = timestamp_indian()
        next_run = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if now >= next_run:
            next_run += timedelta(days=1)
        sleep_s = (next_run - now).total_seconds()
        logger.info(f"Background: sim cleanup sleeping {sleep_s/3600:.1f}h until 03:00 IST")
        await asyncio.sleep(sleep_s)
        await _purge_once()


async def _task_mcp_audit_cleanup() -> None:
    """
    Purge mcp_audit rows older than `mcp.audit_retention_days`
    (default 90). Runs once daily at 03:15 IST — markets closed,
    sim cleanup has finished, low-contention window.

    Setting `mcp.audit_retention_days = 0` disables the purge so
    the table can grow indefinitely (useful while debugging a
    long-running incident — flip back to 90 once done).

    Forensic note: keeping 90 days covers a full quarter's worth
    of LLM-initiated actions which is enough for most compliance
    asks (Composer.trade keeps 1 year, IBKR 7 — a 90-day default
    is the lightweight equivalent for an Indian retail setup).
    """
    from backend.api.database import async_session
    from backend.shared.helpers.settings import get_int

    async def _purge_once():
        days = get_int("mcp.audit_retention_days", 90)
        if days <= 0:
            logger.info("Background: mcp_audit cleanup disabled (retention_days=0)")
            return
        try:
            async with async_session() as s:
                deleted = await _apply_retention(s, "mcp_audit", "created_at", days)
                await s.commit()
                logger.info(
                    f"Background: mcp_audit cleanup purged {deleted} rows older than {days} days"
                )
        except Exception as e:
            logger.error(f"Background: mcp_audit cleanup failed: {e}")

    await asyncio.sleep(45)  # let other startup tasks settle
    await _purge_once()

    while True:
        # Daily at 03:15 IST — 15 min after sim cleanup runs.
        now = timestamp_indian()
        next_run = now.replace(hour=3, minute=15, second=0, microsecond=0)
        if now >= next_run:
            next_run += timedelta(days=1)
        sleep_s = (next_run - now).total_seconds()
        logger.info(f"Background: mcp_audit cleanup sleeping {sleep_s/3600:.1f}h until 03:15 IST")
        await asyncio.sleep(sleep_s)
        await _purge_once()


async def _task_holiday_refresh() -> None:
    """Daily NSE-holiday refresh cron — 04:00 IST.

    Fetches trading holidays from the NSE public API for every distinct
    `holiday_exchange` in `market_segments` (NSE + MCX today) and UPSERTs
    them into the `market_holidays` DB table with `source='nse_auto'`.

    Retry cadence:
      • First attempt at `holiday_refresh_time` (default 04:00 IST) each day.
      • On failure (network error / empty response), retry every 30 min
        until 08:00 IST, then give up for the day. The prior day's rows
        remain in the table; `fetch_holidays` Tier 3 still serves them.

    Idempotent — the ON CONFLICT UPDATE path just refreshes `captured_at`.
    Zero-op when the NSE API is reachable and nothing changed.

    Ships as its own asyncio task so a stuck HTTP call (10 s timeout in
    `_fetch_holidays_from_nse`) does not block any other cron.
    """
    from backend.brokers.broker_apis import (
        _fetch_holidays_from_nse, _upsert_market_holidays_coro,
        _mirror_to_holidays_store,
    )
    from backend.shared.helpers.utils import config as _cfg

    def _next_run_at(now_ist: datetime) -> datetime:
        """Return the next 04:00 IST slot after `now_ist`."""
        target_str = str(_cfg.get("holiday_refresh_time", "04:00") or "04:00")
        try:
            hh, mm = (int(x) for x in target_str.split(":", 1))
        except Exception:
            hh, mm = 4, 0
        slot = now_ist.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if slot <= now_ist:
            slot += timedelta(days=1)
        return slot

    def _exchanges_to_refresh() -> list[str]:
        """De-dup list of `holiday_exchange` values across configured segments."""
        segs = _cfg.get("market_segments", {}) or {}
        seen: list[str] = []
        for _name, seg in segs.items():
            exch = (seg or {}).get("holiday_exchange", "NSE").upper().strip()
            if exch and exch not in seen:
                seen.append(exch)
        return seen or ["NSE"]

    async def _refresh_once(exch: str) -> tuple[bool, int]:
        """Fetch → upsert for one exchange. Returns (success, count).

        A `success=False` return signals the caller to retry on the
        every-30-min schedule.
        """
        # Diff against prior known set for logging.
        prev: set = set()
        try:
            from backend.brokers.broker_apis import (
                _read_market_holidays_async as _read,
            )
            prev = await _read(exch)
        except Exception:
            prev = set()

        # Blocking NSE HTTP call — offload to a thread so the event loop
        # stays responsive (10 s timeout inside _fetch_holidays_from_nse).
        try:
            loop = asyncio.get_running_loop()
            got: set = await loop.run_in_executor(
                None, _fetch_holidays_from_nse, exch,
            )
        except Exception as e:
            logger.warning(f"[HOLIDAY-REFRESH] exchange={exch} NSE fetch raised: {e}")
            return False, 0
        if not got:
            logger.warning(
                f"[HOLIDAY-REFRESH] exchange={exch} NSE returned empty — "
                "will retry"
            )
            return False, 0

        try:
            n = await _upsert_market_holidays_coro(exch, got, "nse_auto")
        except Exception as e:
            logger.error(f"[HOLIDAY-REFRESH] exchange={exch} DB upsert failed: {e}")
            return False, 0

        _mirror_to_holidays_store(exch, got)
        added = sorted(d.isoformat() for d in (got - prev))
        removed = sorted(d.isoformat() for d in (prev - got))
        logger.info(
            f"[HOLIDAY-REFRESH] exchange={exch} prev={len(prev)} now={len(got)} "
            f"added={added} removed={removed}"
        )
        return True, n

    async def _do_all() -> dict:
        """Fire refresh for every exchange; retry pending ones every 30 min
        until 08:00 IST. Returns dict of outcomes."""
        exchanges = _exchanges_to_refresh()
        pending: set[str] = set(exchanges)
        outcomes: dict[str, tuple[bool, int]] = {}
        while pending:
            for exch in list(pending):
                ok, n = await _refresh_once(exch)
                outcomes[exch] = (ok, n)
                if ok:
                    pending.discard(exch)

            if not pending:
                break

            # Retry gate — 08:00 IST hard stop.
            now_ist = timestamp_indian()
            if now_ist.hour >= 8:
                logger.warning(
                    f"[HOLIDAY-REFRESH] give-up after 08:00 IST — still "
                    f"pending: {sorted(pending)}"
                )
                break
            await asyncio.sleep(30 * 60)
        return outcomes

    while True:
        now = timestamp_indian()
        nxt = _next_run_at(now)
        sleep_s = (nxt - now).total_seconds()
        logger.info(
            f"Background: holiday refresh sleeping {sleep_s/3600:.1f}h until "
            f"{nxt.strftime('%H:%M')} IST"
        )
        try:
            await asyncio.sleep(sleep_s)
        except asyncio.CancelledError:
            raise
        try:
            outcomes = await _do_all()
            logger.info(f"Background: holiday refresh complete — {outcomes}")
        except Exception as e:
            logger.error(f"Background: holiday refresh crashed: {e}")


async def _task_hedge_proxy_regression() -> None:
    """Periodic β regression for every active hedge-proxy pair.

    Stage 4 of the proxy-hedge feature. Runs once a day at 02:30 IST
    — outside market hours, low broker-quota window, after the daily
    sparkline warm has settled. For each active row whose
    `regression_at` is older than `hedge_proxies.regression_max_age_days`
    (default 7) the task fetches 60 days of daily closes for the
    proxy + target and writes back β + R² + regression_at.

    Idempotent: a row regressed yesterday gets skipped today. Operators
    can still hit the "Compute β" button in /admin/settings for an
    immediate ad-hoc run.

    Disabled by setting `hedge_proxies.regression_enabled = False` — the
    operator-triggered button keeps working independently.
    """
    from backend.api.database import async_session
    from backend.api.models import HedgeProxy
    from backend.api.routes.hedge_proxies import _compute_regression
    from backend.shared.helpers.settings import get_bool, get_int
    from sqlalchemy import select as sql_select

    async def _run_once():
        if not get_bool("hedge_proxies.regression_enabled", True):
            logger.info("Background: hedge-proxy regression disabled")
            return
        max_age = get_int("hedge_proxies.regression_max_age_days", 7)
        window  = get_int("hedge_proxies.regression_window_days", 60)
        try:
            from backend.brokers.registry import get_market_data_broker
            broker = get_market_data_broker()
        except Exception as exc:
            logger.warning(f"hedge-proxy regression: no broker available: {exc}")
            return

        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age)
        try:
            async with async_session() as s:
                rows = (await s.execute(
                    sql_select(HedgeProxy).where(HedgeProxy.is_active.is_(True))
                )).scalars().all()
        except Exception as exc:
            logger.error(f"hedge-proxy regression: load failed: {exc}")
            return

        ran = 0
        skipped = 0
        failed = 0
        # Collect (row_id, patch_dict) from broker calls first, then
        # write all updates in ONE session.  Each broker call is an
        # asyncio.to_thread blocking operation followed by asyncio.sleep
        # for rate-pacing (1 s per row × Kite 3 req/s budget).  Holding
        # a pooled DB connection open across those sleeps wastes a pool
        # slot for no benefit; batch-writing avoids the per-row session
        # churn while keeping broker work fully paced.
        _pending_writes: list[tuple[int, dict]] = []  # (row.id, patch)
        for row in rows:
            # Skip rows that regressed within the freshness window —
            # daily firing should still hit a row's slot exactly once
            # per max_age window.
            if row.regression_at and row.regression_at >= cutoff:
                skipped += 1
                continue
            try:
                beta, r2, n, sigma_t, sigma_p = await asyncio.to_thread(
                    _compute_regression, broker, row.proxy_symbol, row.target_root, window,
                )
            except Exception as exc:
                # Sprint D — record the failure on the row so the UI
                # can flag it. Still stamps `regression_at` to enforce
                # the freshness gate; operator can clear by editing
                # the pair or hitting /compute manually.
                logger.warning(
                    f"hedge-proxy regression: {row.proxy_symbol}→{row.target_root} failed: {exc}"
                )
                _pending_writes.append((row.id, {
                    "regression_at": datetime.now(timezone.utc),
                    "regression_error": f"broker error: {str(exc)[:200]}",
                }))
                failed += 1
                await asyncio.sleep(1.0)
                continue
            if beta is None:
                # Resolution failure or not enough overlapping bars.
                # Stamp `regression_at` anyway so we don't retry the
                # same broken pair on every run — operator should
                # delete the row or fix the symbol.
                _pending_writes.append((row.id, {
                    "regression_at": datetime.now(timezone.utc),
                    "regression_error": f"too few overlapping bars (n={n}, need ≥ 15)",
                }))
                failed += 1
                await asyncio.sleep(1.0)
                continue
            _st = f"{sigma_t:.3f}" if sigma_t is not None else "—"
            logger.info(
                f"hedge-proxy regression: {row.proxy_symbol}→{row.target_root} "
                f"β={beta:.4f} R²={r2:.3f} σ_t={_st} n={n}"
            )
            _pending_writes.append((row.id, {
                "beta": float(beta),
                "correlation": float(r2 if r2 is not None else 1.0),
                "target_sigma": float(sigma_t) if sigma_t is not None else None,
                "proxy_sigma":  float(sigma_p) if sigma_p is not None else None,
                "regression_at": datetime.now(timezone.utc),
                "regression_error": None,
            }))
            ran += 1
            # Pace per-row work to stay within Kite's 3 req/s historical
            # budget (each row burns 2 historical_data calls).
            await asyncio.sleep(1.0)

        # Single write session — apply all collected patches in one commit.
        if _pending_writes:
            try:
                async with async_session() as s:
                    for _row_id, _patch in _pending_writes:
                        db_row = await s.get(HedgeProxy, _row_id)
                        if db_row:
                            for _k, _v in _patch.items():
                                setattr(db_row, _k, _v)
                    await s.commit()
            except Exception as exc:
                logger.warning(f"hedge-proxy regression: batch write-back failed: {exc}")

        logger.info(
            f"hedge-proxy regression: cycle complete — "
            f"ran={ran} skipped={skipped} failed={failed} of {len(rows)}"
        )

    await asyncio.sleep(120)  # let startup + broker rebuild settle
    await _run_once()

    while True:
        # Daily at 02:30 IST — markets closed, sparkline warm done at
        # 00:30, mcp_audit cleanup at 03:15 leaves a quiet slot.
        now = timestamp_indian()
        next_run = now.replace(hour=2, minute=30, second=0, microsecond=0)
        if now >= next_run:
            next_run += timedelta(days=1)
        sleep_s = (next_run - now).total_seconds()
        logger.info(f"Background: hedge-proxy regression sleeping {sleep_s/3600:.1f}h until 02:30 IST")
        await asyncio.sleep(sleep_s)
        await _run_once()


def _bg_trail_entry_key(entry: dict) -> Optional[tuple[str, str]]:
    """Return (account, 'EXCH:SYM') for a trailing-eligible GTT entry, or None."""
    if not (isinstance(entry, dict)
            and entry.get("kind") == "gtt"
            and entry.get("sl_trail_pct") not in (None, "")):
        return None
    account = str(entry.get("parent_account") or "")
    sym     = str(entry.get("parent_symbol") or "")
    exch    = str(entry.get("parent_exchange") or "NFO")
    if not (account and sym):
        return None
    return account, f"{exch}:{sym}"


def _collect_trail_keys_by_account(rows) -> dict[str, set[str]]:
    """Pass 1 — walk trailing-eligible entries and bucket the distinct
    (exchange:symbol) keys we need broker.ltp for per account."""
    import json as _json
    keys_by_account: dict[str, set[str]] = {}
    for row in rows:
        try:
            attached = _json.loads(row.attached_gtts_json or "[]")
        except Exception:
            continue
        if not isinstance(attached, list):
            continue
        for entry in attached:
            result = _bg_trail_entry_key(entry)
            if result:
                account, key = result
                keys_by_account.setdefault(account, set()).add(key)
    return keys_by_account


async def _batch_ltp_by_account(
    keys_by_account: dict[str, set[str]],
) -> dict[tuple[str, str], float]:
    """Pass 2 — fan out broker.ltp across accounts in parallel and flatten
    the response into `{(account, key): ltp}`. Silently skips accounts
    whose broker registry lookup or LTP call fails."""
    from backend.brokers.registry import get_broker

    ltp_map: dict[tuple[str, str], float] = {}
    accts = list(keys_by_account.keys())

    async def _ltp_for(acct: str):
        try:
            broker = get_broker(acct)
        except Exception:
            return None
        try:
            return await asyncio.to_thread(
                broker.ltp, list(keys_by_account[acct])
            )
        except Exception as e:
            logger.debug(f"[TRAIL] batched ltp failed for {acct}: {e}")
            return None

    results = await asyncio.gather(*(_ltp_for(a) for a in accts))
    for account, resp in zip(accts, results):
        if resp is None:
            continue
        for k in keys_by_account[account]:
            try:
                ltp_v = float((resp.get(k) or {}).get("last_price") or 0)
            except (TypeError, ValueError):
                ltp_v = 0.0
            if ltp_v > 0:
                ltp_map[(account, k)] = ltp_v
    return ltp_map


def _build_trail_modify_kwargs(
    entry: dict,
    proposed: float,
    parent_side: str,
    parent_qty: int,
    parent_product: str,
    trigger_type: str,
    row_id: int,
) -> Optional[tuple[list[float], list[dict]]]:
    """Assemble `(trigger_values, orders)` for `broker.modify_gtt`.

    Returns `None` when a two-leg OCO entry lacks a tp_trigger snapshot
    (pre-Sprint-A entries) — caller should skip the modify but persist
    any watermark advance already made.
    """
    exit_side = "SELL" if parent_side == "BUY" else "BUY"
    if trigger_type == "two-leg":
        tp_trigger = entry.get("tp_trigger")
        if tp_trigger is None:
            # Pre-Sprint-A entries (attached before the
            # tp_trigger snapshot landed) don't carry
            # the TP value. Skip — the watermark advance
            # above still commits so the moment the
            # operator re-attaches with a freshly-seeded
            # entry, trailing kicks in. Logged once at
            # info level so we can spot stragglers.
            logger.info(
                f"[TRAIL] #{row_id} skipping legacy "
                f"two-leg entry without tp_trigger — "
                f"re-attach to enable trailing"
            )
            return None
        new_triggers = [float(tp_trigger), proposed]
        # OCO `orders` array parallel-indexes triggers:
        # [TP order at index 0, SL order at index 1].
        orders_payload = [
            {
                "transaction_type": exit_side,
                "quantity":         parent_qty,
                "price":            float(tp_trigger),
                "order_type":       "LIMIT",
                "product":          parent_product,
            },
            {
                "transaction_type": exit_side,
                "quantity":         parent_qty,
                "price":            proposed,
                "order_type":       "LIMIT",
                "product":          parent_product,
            },
        ]
    else:
        new_triggers = [proposed]
        orders_payload = [{
            "transaction_type": exit_side,
            "quantity":         parent_qty,
            "price":            proposed,
            "order_type":       "LIMIT",
            "product":          parent_product,
        }]
    return new_triggers, orders_payload


async def _send_trail_partial_modify_alert(
    entry: dict,
    row,
    proposed: float,
    parent_symbol: str,
    err: BaseException,
) -> None:
    """Persist + alert for Dhan asymmetric GTT (M-2 audit fix)."""
    entry["partial_modify_error"] = (
        f"ENTRY_LEG updated, TARGET_LEG rejected "
        f"({str(err)[:120]})"
    )
    entry.pop("sl_trail_pct", None)
    logger.warning(
        f"[TRAIL] #{row.id} Dhan asymmetric GTT — "
        f"entry trigger ratcheted to {proposed:.2f} "
        f"but target trigger stale. Operator "
        f"intervention required."
    )
    try:
        from backend.shared.helpers.utils import is_enabled
        if is_enabled('telegram'):
            from backend.shared.helpers.alert_utils import _send_telegram
            await asyncio.to_thread(
                _send_telegram,
                f"⚠ Dhan GTT asymmetric: "
                f"{parent_symbol} on {row.account} — "
                f"trail ratcheted entry but target "
                f"leg rejected. Cancel + recreate "
                f"or verify at broker.",
            )
    except Exception:
        pass


def _bg_trail_entry_is_valid(fields: dict) -> bool:
    """True when the required trail-entry fields are present and non-zero."""
    return bool(
        fields["gtt_id"]
        and fields["parent_symbol"]
        and fields["account"]
        and fields["trail_pct"] > 0
        and fields["parent_qty"] > 0
    )


def _bg_trail_entry_build_fields(entry: dict) -> dict:
    """Construct the field dict from a trailing-eligible GTT entry (no validation)."""
    return {
        "gtt_id":          entry.get("id"),
        "trail_pct":       float(entry["sl_trail_pct"]),
        "current_trigger": float(entry.get("current_trigger") or 0),
        "parent_side":     str(entry.get("parent_side") or ""),
        "parent_symbol":   str(entry.get("parent_symbol") or ""),
        "parent_exchange": str(entry.get("parent_exchange") or "NFO"),
        "account":         str(entry.get("parent_account") or ""),
        "parent_qty":      int(entry.get("parent_qty") or 0),
        "parent_product":  str(entry.get("parent_product") or "NRML"),
        "trigger_type":    str(entry.get("trigger_type") or "single"),
    }


def _extract_trail_entry_fields(entry: dict) -> Optional[dict]:
    """Pull the operator-configured fields from a trailing entry. Returns
    `None` when the entry is not a valid trailing-eligible GTT dict."""
    if not isinstance(entry, dict):
        return None
    if entry.get("kind") != "gtt":
        return None
    if entry.get("sl_trail_pct") in (None, ""):
        return None
    fields = _bg_trail_entry_build_fields(entry)
    if not _bg_trail_entry_is_valid(fields):
        return None
    return fields


def _compute_trail_watermark(
    entry: dict,
    ltp: float,
    parent_side: str,
    trail_pct: float,
    current_trigger: float,
) -> tuple[float, bool, bool]:
    """Update highest/lowest LTP watermarks in-place on `entry` and return
    `(proposed_trigger, more_favorable, watermark_changed)`.
    `more_favorable` = True when the trail should ratchet the broker
    trigger. `watermark_changed` = True when high/low advanced and the
    caller needs to persist the mutation."""
    prior_high = float(entry.get("highest_ltp") or 0)
    prior_low  = (float(entry["lowest_ltp"])
                  if entry.get("lowest_ltp") else None)
    high = max(prior_high, ltp)
    low  = ltp if prior_low is None else min(prior_low, ltp)
    if parent_side == "BUY":
        proposed = high * (1.0 - trail_pct / 100.0)
        more_favorable = proposed > current_trigger
    else:
        proposed = low  * (1.0 + trail_pct / 100.0)
        more_favorable = (current_trigger > 0
                          and proposed < current_trigger)
    watermark_changed = (high != prior_high or low != prior_low)
    if watermark_changed:
        entry["highest_ltp"] = high
        entry["lowest_ltp"]  = low
    return proposed, more_favorable, watermark_changed


async def _process_trail_entry(
    entry: dict,
    row,
    ltp_map: dict[tuple[str, str], float],
) -> bool:
    """Full per-entry trailing pipeline: watermark → modify_gtt →
    persistence book-keeping. Returns True when `entry` was mutated in
    a way the caller must persist."""
    from backend.brokers.registry import get_broker

    fields = _extract_trail_entry_fields(entry)
    if fields is None:
        return False
    try:
        broker = get_broker(fields["account"])
    except Exception:
        return False
    key = f"{fields['parent_exchange']}:{fields['parent_symbol']}"
    ltp = ltp_map.get((fields["account"], key), 0.0)
    if ltp <= 0:
        return False
    # Phase 3C #3 — persist watermark advances even when
    # we DON'T issue a modify_gtt (no favorable move, or
    # two-leg OCO deferred). Previously the in-memory
    # update was discarded every poll cycle because
    # `changed` was only set after a successful
    # modify_gtt call.
    proposed, more_favorable, watermark_changed = _compute_trail_watermark(
        entry, ltp,
        fields["parent_side"], fields["trail_pct"], fields["current_trigger"],
    )
    if not more_favorable:
        return watermark_changed
    proposed = round(proposed, 4)
    built = _build_trail_modify_kwargs(
        entry, proposed,
        fields["parent_side"], fields["parent_qty"], fields["parent_product"],
        fields["trigger_type"], row.id,
    )
    if built is None:
        return watermark_changed
    new_triggers, orders_payload = built
    try:
        await asyncio.to_thread(
            broker.modify_gtt,
            fields["gtt_id"],
            trigger_type=fields["trigger_type"],
            tradingsymbol=fields["parent_symbol"],
            exchange=fields["parent_exchange"],
            last_price=ltp,
            trigger_values=new_triggers,
            orders=orders_payload,
        )
    except NotImplementedError:
        # Broker has no modify_gtt — Dhan / Groww today.
        # Drop the trail metadata so we stop retrying
        # every interval for this row.
        entry.pop("sl_trail_pct", None)
        return True
    except Exception as e:
        # Audit fix (M-2) — detect Dhan asymmetric GTT
        # state. When modify_gtt's two-leg dispatch hit
        # a TARGET_LEG rejection after ENTRY_LEG already
        # succeeded, the broker's GTT is now half-modified.
        # Pre-fix this was logged at DEBUG and the
        # operator never knew. Now persist
        # `partial_modify_error` in the entry +
        # WARNING-level log + Telegram alert so the
        # operator can cancel + recreate or knowingly
        # accept the asymmetric state. Stop ratcheting
        # on subsequent polls — re-modify would keep
        # bumping ENTRY while TARGET drifts.
        if getattr(e, "dhan_partial_modify", False):
            await _send_trail_partial_modify_alert(
                entry, row, proposed, fields["parent_symbol"], e,
            )
            return True
        logger.debug(
            f"[TRAIL] modify_gtt failed for #{row.id} "
            f"gtt={fields['gtt_id']}: {e}"
        )
        return watermark_changed
    entry["current_trigger"] = proposed
    logger.info(
        f"[TRAIL] #{row.id} {fields['parent_side']} {fields['parent_symbol']} "
        f"trigger {fields['current_trigger']:.2f} → {proposed:.2f} "
        f"(LTP {ltp:.2f}, trail {fields['trail_pct']}%)"
    )
    return True


async def _process_trail_row(
    row,
    ltp_map: dict[tuple[str, str], float],
) -> Optional[tuple[int, str]]:
    """Walk every attached-GTT entry on `row`, mutate in-place via
    `_process_trail_entry`, and return a `(id, json_str)` pending-update
    tuple when at least one entry changed."""
    import json as _json
    try:
        attached = _json.loads(row.attached_gtts_json or "[]")
    except Exception:
        return None
    if not isinstance(attached, list):
        return None
    changed = False
    for entry in attached:
        entry_changed = await _process_trail_entry(entry, row, ltp_map)
        if entry_changed:
            changed = True
    if changed:
        return (row.id, _json.dumps(attached))
    return None


async def _flush_trail_updates(pending_updates: list[tuple[int, str]]) -> None:
    """Batch flush — one session, N UPDATE statements, one commit."""
    if not pending_updates:
        return
    from backend.api.database import async_session
    from backend.api.models import AlgoOrder
    from sqlalchemy import update as _update
    async with async_session() as s2:
        for _rid, _json_str in pending_updates:
            await s2.execute(
                _update(AlgoOrder)
                .where(AlgoOrder.id == _rid)
                .values(attached_gtts_json=_json_str)
            )
        await s2.commit()


async def _task_trail_stop() -> None:
    """Trailing-stop background poller (Phase 3B).

    Walks every `algo_orders` row where:
      • mode = 'live'
      • status = 'FILLED' (parent already executed; SL leg sits at broker)
      • `attached_gtts_json` contains at least one entry with
        `sl_trail_pct` set + a `current_trigger` (seeded by
        `_fire_template_attach_on_fill`)

    For each such row:
      1. Fetch current LTP via `broker.ltp([key])` (one round-trip per
         unique symbol; cached implicitly by Kite's quote endpoint).
      2. Update highest_ltp (long parent) / lowest_ltp (short parent).
      3. Compute new trigger:
           long  → high × (1 − sl_trail_pct/100)
           short → low  × (1 + sl_trail_pct/100)
         Only commits if it's more favorable than the current trigger
         (trail never moves against the operator).
      4. Calls `broker.modify_gtt(gtt_id, new_trigger)` and persists
         the new state back into `attached_gtts_json`.

    Kite is the only broker with native modify_gtt today; Dhan + Groww
    raise NotImplementedError, so the poller silently skips those
    rows. The seeder note documents this limitation.
    """
    from backend.api.database import async_session
    from backend.api.models import AlgoOrder
    from backend.shared.helpers.settings import get_int
    from sqlalchemy import select as _sel

    while True:
        try:
            interval = max(5, get_int("templates.trail_poll_interval_seconds", 30))
        except Exception:
            interval = 30
        await asyncio.sleep(interval)
        try:
            async with async_session() as s:
                rows = (await s.execute(
                    _sel(AlgoOrder).where(
                        AlgoOrder.mode == "live",
                        AlgoOrder.status == "FILLED",
                        AlgoOrder.attached_gtts_json.is_not(None),
                    ).limit(500)
                )).scalars().all()
            # Collect per-row attached_gtts_json updates here and flush in
            # one session at the end of the cycle. Pre-fix the loop opened
            # one async_session per changed row → SELECT + UPDATE + COMMIT,
            # which at ~500 trailing rows during a burst became ~500
            # sequential DB round-trips per 30s tick — enough to starve
            # the asyncio scheduler and make `/api/positions` (and the
            # Refresh button that drives it) feel sluggish.
            # Phase 3D #5 — batch broker.ltp per account. Pass 1 collects
            # every (account, exchange:symbol) key referenced by a trailing
            # SL entry, deduped per account. Pass 2 issues one batched
            # broker.ltp([keys...]) per account. Pass 3 walks the rows
            # again and uses the pre-fetched LTP map. Prior version did
            # one ltp() call per entry — at 100 trailing rows that was
            # 100 round-trips every poll cycle.
            keys_by_account = _collect_trail_keys_by_account(rows)
            ltp_map = await _batch_ltp_by_account(keys_by_account)
            pending_updates: list[tuple[int, str]] = []
            for row in rows:
                pending = await _process_trail_row(row, ltp_map)
                if pending is not None:
                    pending_updates.append(pending)
            await _flush_trail_updates(pending_updates)
        except Exception as e:
            logger.debug(f"[TRAIL] poll iteration failed: {e}")


def _bg_oco_first_sibling_account(attached: list) -> Optional[str]:
    """Return the first parent_account from any sibling entry, or None."""
    for e in attached:
        if isinstance(e, dict) and e.get("sibling_id"):
            acct = e.get("parent_account")
            if acct:
                return acct
    return None


def _oco_parse_entries(
    rows: list,
    json_loads: object,
) -> tuple[dict, dict]:
    """Group OCO rows by account and build the attached-entries map.

    Returns:
        rows_by_account: dict[account_str, list[row]]
        attached_by_row: dict[row_id, list[dict]]
    """
    rows_by_account: dict[str, list] = {}
    attached_by_row: dict[int, list] = {}
    for row in rows:
        try:
            attached = json_loads(row.attached_gtts_json or "[]")  # type: ignore[operator]
        except Exception:
            continue
        if not isinstance(attached, list):
            continue
        acct = _bg_oco_first_sibling_account(attached)
        if not acct:
            continue
        rows_by_account.setdefault(acct, []).append(row)
        attached_by_row[row.id] = attached
    return rows_by_account, attached_by_row


async def _oco_build_gtts_map(
    accts: list[str],
) -> dict[str, dict[str, dict]]:
    """Fetch GTTs for all accounts in parallel; return id-keyed map per account."""
    gtts_by_account: dict[str, dict[str, dict]] = {}
    results = await asyncio.gather(*(_oco_fetch_account_gtts(a) for a in accts))
    for acct, gtts in zip(accts, results):
        if gtts is None:
            continue
        gtts_by_account[acct] = {
            str(g.get("id") or g.get("gtt_id")): g
            for g in (gtts or [])
            if isinstance(g, dict)
        }
    return gtts_by_account


async def _oco_flush_updates(
    pending: list[tuple[int, str]],
    async_session: object,
    AlgoOrder: object,
    update_stmt: object,
) -> None:
    """Batch-flush OCO JSON updates in one DB session."""
    if not pending:
        return
    async with async_session() as s2:  # type: ignore[operator]
        for _rid, _json_str in pending:
            await s2.execute(
                update_stmt(AlgoOrder)  # type: ignore[operator]
                .where(AlgoOrder.id == _rid)  # type: ignore[union-attr]
                .values(attached_gtts_json=_json_str)
            )
        await s2.commit()


async def _oco_fetch_account_gtts(acct: str) -> Optional[list]:
    """Fetch GTTs for one account; returns None on any error."""
    from backend.brokers.registry import get_broker
    try:
        broker = get_broker(acct)
    except Exception:
        return None
    try:
        return await asyncio.to_thread(broker.get_gtts)
    except Exception as e:
        logger.debug(f"[OCO-WATCH] get_gtts failed for {acct}: {e}")
        return None


async def _oco_handle_settled_pair(
    entry: dict,
    by_id: dict,
    row_id: int,
    my_id: str,
    sib_id: str,
) -> None:
    """Handle the both-settled case: log + optional Telegram + clear pointers."""
    logger.warning(
        f"[OCO-WATCH] row={row_id} both legs settled "
        f"within one poll window (my={my_id} sib={sib_id}). "
        f"Symbol={entry.get('parent_symbol', '?')} — "
        f"verify no double-close at the broker."
    )
    try:
        from backend.shared.helpers.utils import is_enabled
        if is_enabled('telegram'):
            from backend.shared.helpers.alert_utils import _send_telegram
            await asyncio.to_thread(
                _send_telegram,
                f"⚠ OCO double-fire (emulated): "
                f"{entry.get('parent_symbol', '?')} on "
                f"{entry.get('parent_account', '?')} — "
                f"both legs settled in one 15s poll "
                f"window. Verify broker position; "
                f"manual close may be needed.",
            )
    except Exception as _alert_err:
        logger.debug(f"[OCO-WATCH] double-fire alert failed: {_alert_err}")
    entry.pop("sibling_id", None)
    sib_entry = by_id.get(sib_id)
    if sib_entry:
        sib_entry.pop("sibling_id", None)


async def _oco_cancel_survivor(
    entry: dict,
    by_id: dict,
    broker: object,
    row_id: int,
    my_id: str,
    sib_id: str,
) -> bool:
    """Cancel the surviving sibling GTT; clears pointers on success. Returns changed flag."""
    sib_entry = by_id.get(sib_id) or {}
    sib_exchange = (
        sib_entry.get("parent_exchange")
        or entry.get("parent_exchange")
        or "NFO"
    )
    try:
        await asyncio.to_thread(broker.cancel_gtt, sib_id, exchange=sib_exchange)
        logger.info(
            f"[OCO-WATCH] row={row_id} cancelled survivor "
            f"sibling={sib_id} (my id={my_id} fired)"
        )
        entry.pop("sibling_id", None)
        if sib_entry:
            sib_entry.pop("sibling_id", None)
        return True
    except Exception as e:
        logger.warning(
            f"[OCO-WATCH] row={row_id} cancel sibling "
            f"{sib_id} failed: {e}"
        )
        return False


async def _oco_process_account_entries(
    acct: str,
    acct_rows: list,
    attached_by_row: dict,
    broker_gtts: dict,
) -> list[tuple[int, str]]:
    """Process all OCO rows for one account; returns (row_id, json_str) update pairs."""
    import json as _json
    from backend.brokers.registry import get_broker

    pending: list[tuple[int, str]] = []
    try:
        broker = get_broker(acct)
    except Exception:
        return pending

    for row in acct_rows:
        attached = attached_by_row.get(row.id) or []
        changed = False
        by_id: dict[str, dict] = {
            str(e.get("id")): e
            for e in attached
            if isinstance(e, dict) and e.get("id")
        }
        for entry in attached:
            if not (isinstance(entry, dict) and entry.get("sibling_id")):
                continue
            my_id  = str(entry.get("id") or "")
            sib_id = str(entry.get("sibling_id") or "")
            if not (my_id and sib_id):
                continue
            my_active  = my_id in broker_gtts
            sib_active = sib_id in broker_gtts
            if my_active or not sib_active:
                if not my_active and not sib_active:
                    await _oco_handle_settled_pair(entry, by_id, row.id, my_id, sib_id)
                    changed = True
                continue
            # MY leg gone, sibling still alive → cancel it.
            did_cancel = await _oco_cancel_survivor(entry, by_id, broker, row.id, my_id, sib_id)
            if did_cancel:
                changed = True
        if changed:
            pending.append((row.id, _json.dumps(attached)))
    return pending


async def _task_oco_pair_watcher() -> None:
    """OCO sibling-watcher for brokers without native OCO (Groww).

    Sprint C — when `apply_plan_live` placed two single-trigger GTTs
    instead of a real OCO (because `broker.capabilities.gtt_oco` is
    False), each `attached_gtts_json` entry carries a `sibling_id`
    pointer. When one leg fires (FILLED / TRIGGERED at the broker),
    this task cancels the surviving sibling so the operator doesn't
    end up with a naked stop or take-profit on the book.

    Polling cadence: `templates.oco_pair_poll_seconds`, default 15s
    (faster than trail-stop's 30s because the OCO race window
    matters more — a fired-but-not-cancelled sibling can produce a
    second unwanted fill). Source of truth is `broker.get_gtts()`
    filtered by `attached_gtts_json` entries.

    Failure shape: best-effort. A sibling-cancel that fails is logged
    and retried next cycle; the entry stays in `attached_gtts_json`
    until either the cancel succeeds or the operator manually clears
    the row. No background failure mode can fire an extra order — the
    worst case is a stale entry that no longer maps to live broker
    state.
    """
    from backend.api.database import async_session
    from backend.api.models import AlgoOrder
    from backend.shared.helpers.settings import get_int
    from sqlalchemy import select as _sel, update as _update
    import json as _json

    # Perf: lift the polled query to module scope so SQLAlchemy doesn't
    # rebuild the `where()` clause + recompute the cache key on every
    # cycle. py-spy showed `_compile_w_cache` + `_boolean_compare`
    # firing per-tick — small but pure savings.
    _stmt = (
        _sel(AlgoOrder)
        .where(
            AlgoOrder.mode == "live",
            AlgoOrder.status == "FILLED",
            AlgoOrder.attached_gtts_json.is_not(None),
        )
        .limit(500)
    )

    while True:
        try:
            # 15s default: faster than trail-stop (30s) because the OCO
            # race window is shorter — a fired-but-not-cancelled sibling
            # can produce a second fill within seconds of the first.
            # Matches the poll_only GTT detection lag documented in
            # BrokerCapabilities.oco_pair_poll_seconds.
            interval = max(5, get_int("templates.oco_pair_poll_seconds", 15))
        except Exception:
            interval = 15
        await asyncio.sleep(interval)
        try:
            async with async_session() as s:
                rows = (await s.execute(_stmt)).scalars().all()
            rows_by_account, attached_by_row = _oco_parse_entries(rows, _json.loads)
            if not rows_by_account:
                continue
            gtts_by_account = await _oco_build_gtts_map(list(rows_by_account.keys()))
            pending_oco_updates: list[tuple[int, str]] = []
            for acct, acct_rows in rows_by_account.items():
                broker_gtts = gtts_by_account.get(acct, {})
                updates = await _oco_process_account_entries(
                    acct, acct_rows, attached_by_row, broker_gtts
                )
                pending_oco_updates.extend(updates)
            await _oco_flush_updates(pending_oco_updates, async_session, AlgoOrder, _update)
        except Exception as e:
            logger.debug(f"[OCO-WATCH] poll iteration failed: {e}")


async def _snapshot_book_symbols(days: int = 7) -> list[tuple[str, str]]:
    """Return (tradingsymbol, exchange) pairs from recent daily_book rows.

    Queried from DB — survives conn_service restart, Dhan circuit-breaker
    open at boot, or any account being unhealthy. Acts as a cold-start
    backstop so Kite ticker subscriptions are seeded even when all live
    broker fetches return empty.

    ``days`` controls how far back to look. 7 days (the default) covers
    weekends + public holidays so a Monday-morning restart after a long
    weekend still picks up Friday's positions.  Symbols whose most recent
    row is older than ``days`` days are excluded — they represent stale
    closed/expired positions not worth subscribing.

    Exchange fallback per kind:
      * 'holdings'  → NSE  (equity default, matches broker_apis.fetch_holdings)
      * 'positions' → NFO  (F&O/commodity default, matches broker_apis.fetch_positions)
    """
    from backend.api.database import async_session
    from backend.api.models import DailyBook
    from sqlalchemy import select as _sa_select

    cutoff = (timestamp_indian().date() - timedelta(days=days))
    try:
        async with async_session() as session:
            stmt = (
                _sa_select(DailyBook.symbol, DailyBook.exchange, DailyBook.kind)
                .where(
                    DailyBook.kind.in_(["positions", "holdings"]),
                    DailyBook.date >= cutoff,
                )
                .distinct()
            )
            result = await session.execute(stmt)
            rows = result.fetchall()

        pairs: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            sym  = str(row.symbol or "").upper().strip()
            exch = str(row.exchange or "").upper().strip()
            kind = str(row.kind or "").lower().strip()
            if not sym:
                continue
            if not exch:
                exch = "NSE" if kind == "holdings" else "NFO"
            key = (sym, exch)
            if key not in seen:
                seen.add(key)
                pairs.append(key)
        return pairs
    except Exception as e:
        logger.warning(f"snapshot book symbols: DB query failed: {e}")
        return []


async def _sparkline_collect_watchlist(seen: set, pairs: list) -> None:
    """Collect (symbol, exchange) pairs from watchlist DB rows into *pairs*."""
    try:
        from backend.api.database import async_session
        from backend.api.models import WatchlistItem
        from sqlalchemy import select as sa_select
        from backend.api.routes.watchlist import (
            _resolve_mcx_commodity,
            _resolve_cds_currency,
        )
        async with async_session() as sess:
            rows = (await sess.execute(
                sa_select(WatchlistItem.tradingsymbol, WatchlistItem.exchange)
            )).all()
        for row in rows:
            sym  = (row.tradingsymbol or "").upper().strip()
            exch = (row.exchange or "NSE").upper().strip()
            if not sym:
                continue
            if exch == "MCX" and sym.isalpha() and len(sym) <= 12:
                resolved = await _resolve_mcx_commodity(sym)
                if resolved:
                    sym = resolved.upper().strip()
            elif exch == "CDS" and sym.isalpha() and len(sym) <= 12:
                resolved = await _resolve_cds_currency(sym)
                if resolved:
                    sym = resolved.upper().strip()
            key = (sym, exch)
            if key not in seen:
                seen.add(key)
                pairs.append(key)
    except Exception as e:
        logger.warning(f"sparkline warm: watchlist query failed: {e}")


def _sparkline_collect_holdings_cached(seen: set, pairs: list, cache_hit) -> None:
    """Collect holdings symbols from the in-process cache (fast path)."""
    try:
        cached_h = cache_hit("holdings")
        if cached_h is not None and getattr(cached_h, "rows", None):
            for row in cached_h.rows:
                sym  = str(getattr(row, "tradingsymbol", "") or "").upper().strip()
                exch = str(getattr(row, "exchange", "") or "NSE").upper().strip()
                if sym:
                    key = (sym, exch)
                    if key not in seen:
                        seen.add(key)
                        pairs.append(key)
    except Exception as e:
        logger.warning(f"sparkline warm: holdings (cached) collect failed: {e}")


def _sparkline_collect_holdings_live(seen: set, pairs: list) -> None:
    """Collect holdings symbols via live broker fetch (cold-start fallback)."""
    try:
        from backend.brokers import broker_apis
        import pandas as pd
        dfs = broker_apis.fetch_holdings()
        df_h = pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()
        if not df_h.empty and "tradingsymbol" in df_h.columns:
            _h_exch = df_h["exchange"] if "exchange" in df_h.columns else pd.Series(["NSE"] * len(df_h))
            for sym_raw, exch_raw in zip(df_h["tradingsymbol"], _h_exch):
                sym  = str(sym_raw or "").upper().strip()
                exch = str(exch_raw or "NSE").upper().strip()
                if sym:
                    key = (sym, exch)
                    if key not in seen:
                        seen.add(key)
                        pairs.append(key)
    except Exception as e:
        logger.warning(f"sparkline warm: holdings (live) collect failed: {e}")


def _sparkline_collect_positions(seen: set, pairs: list, cache_hit) -> None:
    """Collect positions symbols — cached first, then live broker fallback."""
    try:
        cached_p = cache_hit("positions")
        if cached_p is not None and getattr(cached_p, "rows", None):
            for row in cached_p.rows:
                sym  = str(getattr(row, "tradingsymbol", "") or "").upper().strip()
                exch = str(getattr(row, "exchange", "") or "NFO").upper().strip()
                if sym:
                    key = (sym, exch)
                    if key not in seen:
                        seen.add(key)
                        pairs.append(key)
        else:
            from backend.brokers import broker_apis
            import pandas as pd
            dfs = broker_apis.fetch_positions()
            df_p = pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()
            if not df_p.empty and "tradingsymbol" in df_p.columns:
                _p_exch = df_p["exchange"] if "exchange" in df_p.columns else pd.Series(["NFO"] * len(df_p))
                for sym_raw, exch_raw in zip(df_p["tradingsymbol"], _p_exch):
                    sym  = str(sym_raw or "").upper().strip()
                    exch = str(exch_raw or "NFO").upper().strip()
                    if sym:
                        key = (sym, exch)
                        if key not in seen:
                            seen.add(key)
                            pairs.append(key)
    except Exception as e:
        logger.warning(f"sparkline warm: positions collect failed: {e}")


async def _sparkline_collect_snapshot(seen: set, pairs: list) -> None:
    """Collect symbols from daily_book DB backstop (7-day window)."""
    try:
        snap_pairs = await _snapshot_book_symbols(days=7)
        for key in snap_pairs:
            if key not in seen:
                seen.add(key)
                pairs.append(key)
    except Exception as e:
        logger.warning(f"sparkline warm: snapshot book symbols failed: {e}")


def _sparkline_collect_movers(seen: set, pairs: list) -> None:
    """Collect the mover universe (indices + F&O largecap) into *pairs*."""
    try:
        from backend.shared.helpers.mover_universe import mover_warm_pairs
        for key in mover_warm_pairs():
            if key not in seen:
                seen.add(key)
                pairs.append(key)
    except Exception as e:
        logger.warning(f"sparkline warm: mover universe collect failed: {e}")


async def _task_sparkline_warm(state: dict) -> None:
    """
    Pre-populate the sparkline past-close cache at startup and at each
    market segment open so the operator's first Pulse load is free of
    historical_data calls.

    Symbol universe (capped at 300, deduped):
      1. All distinct tradingsymbols in watchlist_items (DB query).
      2. Live holdings tradingsymbols (one broker fetch; equity symbols
         for which sparklines are most commonly shown).
      3. Live positions tradingsymbols (F&O + commodities in the open book).
      4. Backstop: last-known symbols from daily_book (7-day window).
         Ensures Dhan / unhealthy-account symbols subscribe to Kite ticker
         even when the broker circuit-breaker is open at cold-start.

    Positions and holdings come from the same in-process broker call used
    by _task_performance — no extra Kite session or rate-limit budget is
    consumed. Errors (DB unavailable, broker unloaded) skip silently.

    Schedule:
      • Immediately at app startup (async, before sleeping).
      • Once per market-segment open (NSE 09:15 IST, MCX 09:00 IST).
        Waits for the earliest next-open boundary, fires, then waits for
        the next one. One warm per open boundary per day.
    """
    from backend.api.routes.quote import warm_sparkline_cache

    async def _collect_symbols() -> list[tuple[str, str]]:
        """Return deduplicated (tradingsymbol, exchange) pairs from watchlist + book."""
        pairs: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()

        # 1. Watchlist — resolves bare MCX/CDS virtual roots to front-month futures.
        await _sparkline_collect_watchlist(seen, pairs)

        # 2. + 3. Holdings + Positions — prefer the in-process cache populated by
        # _task_performance + the /api/holdings, /api/positions endpoints. Falls
        # through to a direct broker_apis call only when the cache is cold.
        from backend.api.cache import _store as _cache_store
        import time as _t

        def _cache_hit(key: str):
            entry = _cache_store.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at > _t.monotonic() and value is not None:
                return value
            return None

        cached_h = _cache_hit("holdings")
        if cached_h is not None and getattr(cached_h, "rows", None):
            _sparkline_collect_holdings_cached(seen, pairs, _cache_hit)
        else:
            _sparkline_collect_holdings_live(seen, pairs)

        _sparkline_collect_positions(seen, pairs, _cache_hit)

        # 4. Backstop: DB snapshot from daily_book (7-day window).
        await _sparkline_collect_snapshot(seen, pairs)

        # 5. Mover universe — indices + F&O largecap + NIFTY midcap + smallcap.
        _sparkline_collect_movers(seen, pairs)

        # Hard cap: operator's book is never truncated. Movers fill remaining
        # capacity up to the 300-symbol ceiling.
        from backend.shared.helpers.mover_universe import mover_warm_pairs as _mwp
        _mover_set = set(_mwp())
        book_pairs  = [p for p in pairs if p not in _mover_set]
        mover_pairs = [p for p in pairs if p in _mover_set]
        cap         = 300
        remaining   = max(0, cap - len(book_pairs))
        return book_pairs + mover_pairs[:remaining]

    async def _register_universe_with_ticker(
        symbols: list[tuple[str, str]],
        label: str,
    ) -> int:
        """Resolve tokens for all universe symbols and register them in the
        ticker's local sym→token map.  This ensures `_token_to_sym.get(tok)`
        returns a valid sym string in `_poll_loop`, so SSE tick payloads carry
        a non-empty `sym` field and the frontend never drops them.

        Processes all symbols in chunks of 50 (matching the per-tick cap) but
        runs ALL chunks sequentially at startup — no budget concern here since
        this is a one-time registration, not a hot path.

        Returns the count of newly-registered symbols."""
        try:
            from backend.brokers.kite_ticker import get_ticker as _gt
            from backend.api.routes.quote import _resolve_token_for_sym as _rts
            _tk = _gt()
            need = [
                (sym, exch) for sym, exch in symbols
                if not _tk.has_sym(sym)
            ]
            if not need:
                logger.info(
                    f"sparkline warm: ticker universe registration ({label}) — "
                    f"all {len(symbols)} symbols already registered"
                )
                return 0
            registered = 0
            chunk_size = 50
            for i in range(0, len(need), chunk_size):
                chunk = need[i : i + chunk_size]
                try:
                    toks = await asyncio.gather(
                        *(_rts(s, e) for s, e in chunk),
                        return_exceptions=True,
                    )
                    batch = [
                        (tok, sym)
                        for (sym, _exch), tok in zip(chunk, toks)
                        if tok is not None and not isinstance(tok, BaseException)
                    ]
                    if batch:
                        _tk.subscribe_with_sym(batch)
                        registered += len(batch)
                except Exception as _ce:
                    logger.debug(
                        f"sparkline warm: ticker register chunk {i} failed: {_ce}"
                    )
            logger.info(
                f"sparkline warm: ticker universe registration ({label}) — "
                f"{registered}/{len(need)} newly registered "
                f"(universe={len(symbols)})"
            )
            return registered
        except Exception as e:
            logger.warning(f"sparkline warm: ticker universe registration failed: {e}")
            return 0

    async def _do_warm(label: str) -> int:
        logger.info(f"sparkline warm: starting ({label})")
        try:
            symbols = await _collect_symbols()
            # Register all universe symbols in the ticker's sym→token map
            # BEFORE warming the sparkline cache.  This closes the gap where
            # the mmap poller ships `sym: ""` for tokens not yet known to the
            # main API process, causing the frontend to drop tick payloads and
            # cells to flicker between live and close_price.
            await _register_universe_with_ticker(symbols, label)
            count = await warm_sparkline_cache(symbols, days=5)
            logger.info(f"sparkline warm: {label} complete — {count} symbols cached")
            return count
        except Exception as e:
            logger.error(f"sparkline warm: {label} failed: {e}")
            return 0

    async def _do_warm_with_retry(label: str) -> None:
        """Startup variant — if the first warm cycle returns 0 symbols
        (broker not loaded yet, DB unavailable, full rate-limit storm),
        schedule one retry after 60 s. Without this, a transient startup
        failure leaves the cache cold until the next segment-open boundary
        (up to ~12 hours away on a weekend deploy) and the first operator
        page-load pays the full cold-miss cost for every sparkline."""
        count = await _do_warm(label)
        if count > 0:
            return
        logger.warning(
            f"sparkline warm: {label} produced 0 cached symbols — "
            "retrying in 60s"
        )
        await asyncio.sleep(60)
        retry = await _do_warm(f"{label}-retry")
        if retry == 0:
            logger.warning(
                f"sparkline warm: {label}-retry also produced 0 cached "
                "symbols — leaving cache cold until next boundary"
            )

    # Dev-idle gate (see _task_performance) — skip the startup warm on
    # dev when engine is idle so dev doesn't burn historical_data calls
    # before any operator has picked a mode.
    from backend.shared.helpers.utils import is_engine_idle

    # ── Fire immediately at startup ──────────────────────────────────────────
    # fire-and-forget so subsequent task startup (scheduled-warm loop below)
    # is not blocked for the 50-90 s the warm cycle takes. The operator's
    # first request may still pay a cold-cache miss for the first ~30 s but
    # the rest of app startup completes without waiting.
    if not is_engine_idle():
        asyncio.create_task(_do_warm_with_retry("startup"))
    else:
        logger.info("sparkline warm: skipped startup — engine idle (dev)")

    # ── Then at each market-segment open boundary + daily IST midnight ───────
    #
    # Three boundary types per day:
    #   - 00:30 IST  "midnight"   — fires right after the date rolls so the
    #                                next operator glance (overnight, pre-
    #                                market, weekend) reads from a hot
    #                                cache. Without this, the cache evicts
    #                                at midnight and stays empty until the
    #                                09:00 MCX-open warm — every
    #                                /api/quotes/sparkline call between
    #                                those hours triggers ~30-100 lazy
    #                                per-symbol historical_data fetches.
    #                                The 30-minute offset gives Kite a few
    #                                minutes to settle yesterday's final
    #                                daily aggregate (last bar reliably
    #                                shows YESTERDAY's close, not today
    #                                pre-market).
    #   - 09:00 IST  "commodity"  — MCX session open
    #   - 09:15 IST  "equity"     — NSE session open
    #
    # All three trigger warm_sparkline_cache which pre-fills ohlcv_store
    # (past daily closes) and intraday_store (today's 30-min bars) via the
    # persistence pipeline.
    segments = _build_segments()
    seg_warm_dates: dict[str, date | None] = {s['name']: None for s in segments}
    midnight_warm_date: date | None = None

    _MIDNIGHT_HH, _MIDNIGHT_MM = 0, 30

    while True:
        # Re-read segments on every loop iteration so config changes land.
        segments = _build_segments()
        now  = timestamp_indian()
        today = now.date()

        # Find the soonest next-warm boundary — earliest of all segment
        # opens AND the daily 00:30 IST midnight boundary. A boundary
        # fires once per day when the clock crosses it.
        next_fire: datetime | None = None
        for seg in segments:
            open_dt = now.replace(
                hour=seg['hours_start'].hour,
                minute=seg['hours_start'].minute,
                second=0, microsecond=0,
            )
            if now >= open_dt:
                open_dt = open_dt + timedelta(days=1)
            if next_fire is None or open_dt < next_fire:
                next_fire = open_dt

        midnight_dt = now.replace(
            hour=_MIDNIGHT_HH, minute=_MIDNIGHT_MM,
            second=0, microsecond=0,
        )
        if now >= midnight_dt:
            midnight_dt = midnight_dt + timedelta(days=1)
        if next_fire is None or midnight_dt < next_fire:
            next_fire = midnight_dt

        if next_fire is None:
            # No segments configured AND no midnight — sleep and retry.
            await asyncio.sleep(3600)
            continue

        sleep_s = (next_fire - now).total_seconds()
        logger.info(
            f"sparkline warm: sleeping {sleep_s / 3600:.1f}h until "
            f"next boundary at {next_fire.strftime('%H:%M')} IST"
        )
        await asyncio.sleep(max(sleep_s, 1))

        # Identify which boundary(ies) just fired. Warm once per
        # boundary per IST date.
        now   = timestamp_indian()
        today = now.date()

        # Daily midnight warm — only fires once per IST date and only
        # after 00:30. Doesn't depend on segments being configured.
        midnight_dt_now = now.replace(
            hour=_MIDNIGHT_HH, minute=_MIDNIGHT_MM,
            second=0, microsecond=0,
        )
        if now >= midnight_dt_now and midnight_warm_date != today:
            midnight_warm_date = today
            if is_engine_idle():
                logger.info("sparkline warm: skipped daily-midnight — engine idle (dev)")
            else:
                await _do_warm("daily-midnight")

        # Per-segment opens — warm once per segment per IST date.
        for seg in segments:
            if seg_warm_dates[seg['name']] == today:
                continue
            open_dt = now.replace(
                hour=seg['hours_start'].hour,
                minute=seg['hours_start'].minute,
                second=0, microsecond=0,
            )
            if now >= open_dt:
                seg_warm_dates[seg['name']] = today
                if is_engine_idle():
                    logger.info(f"sparkline warm: skipped {seg['name']}-open — engine idle (dev)")
                    continue
                await _do_warm(f"{seg['name']}-open")


# ── Ticker watchdog ──────────────────────────────────────────────────────
#
# Failover for the KiteTicker WebSocket. Runs every 30s. If the ticker is
# started but has been disconnected for > FAILOVER_THRESHOLD seconds, picks
# the next eligible Kite account (historical_data_enabled = True, not the
# currently-bound account, not in a 5-min do-not-retry cool-off) and
# restarts the ticker against it. Previously-subscribed tokens carry over.
# When the failed account recovers + its cool-off expires, we don't bounce
# back automatically — the new account stays primary until ITS WebSocket
# breaks. Operator can force a re-assign by adjusting the
# connections.sparkline_account setting + restarting.
# Per-incident alert state for _task_ticker_watchdog.  Lives in-memory only —
# a process restart is itself a recovery event so resetting is correct.
_ticker_alert_state: dict = {
    "alert_active":    False,  # True while we are in a "no eligible account" incident
    "last_alerted_at": 0.0,   # unix ts of the most recent Telegram ping
    "incident_start":  0.0,   # unix ts when the current incident began
}


async def _watchdog_deferred_recycle() -> None:
    """Execute HARD-mode deferred ticker recycle if the flag is pending."""
    try:
        from backend.api.persistence.runtime_state import consume_ticker_reset_pending
        from backend.brokers.kite_ticker import get_ticker as _get_ticker
        if consume_ticker_reset_pending():
            try:
                _get_ticker().recycle()
                logger.info("ticker watchdog: ran deferred HARD-mode recycle")
            except Exception as e:
                logger.warning(f"ticker watchdog: deferred HARD-mode recycle failed: {e}")
    except Exception as e:
        logger.warning(f"ticker watchdog: pending-flag check failed: {e}")


async def _watchdog_check_market_open(
    holiday_cache: dict,
    holiday_year_ref: list,
    fetch_holidays: object,
) -> bool:
    """Refresh holiday cache if needed and return True if any segment is open."""
    now = timestamp_indian().replace(tzinfo=None)
    segments = _build_segments()
    if holiday_year_ref[0] != now.year:
        holiday_cache.clear()
        holiday_year_ref[0] = now.year
    for seg in segments:
        exch = seg['holiday_exchange']
        if exch not in holiday_cache:
            try:
                holiday_cache[exch] = await asyncio.to_thread(fetch_holidays, exch)  # type: ignore[operator]
            except Exception:
                holiday_cache[exch] = set()
    return any(
        is_market_open(
            now,
            holiday_cache.get(seg['holiday_exchange'], set()),
            seg['hours_start'],
            seg['hours_end'],
            special_sessions=_fetch_special_sessions_safe(seg['holiday_exchange']),
        )
        for seg in segments
    )


async def _watchdog_handle_recovery(ticker: object, status: dict) -> None:
    """Send a Telegram recovery alert when a previously-degraded ticker reconnects."""
    from backend.shared.helpers.alert_utils import _send_telegram
    from backend.shared.helpers.utils import is_enabled
    _ticker_alert_state["alert_active"] = False
    now_ts = _time.time()
    duration_min = int((now_ts - _ticker_alert_state["incident_start"]) / 60)
    branch = config.get("deploy_branch", "main")
    branch_tag = f" [{branch}]" if branch != "main" else ""
    ts = timestamp_display()
    connected_acct = status.get("account", ticker.current_account() or "?")  # type: ignore[union-attr]
    msg = (
        f"TickerWatchdog{branch_tag} — recovered\n"
        f"Ticker connected on {connected_acct}\n"
        f"Duration of incident: {duration_min} min\n"
        f"Time: {ts}"
    )
    logger.info(f"ticker watchdog: recovered on {connected_acct} after {duration_min} min")
    if is_enabled("telegram"):
        await asyncio.to_thread(_send_telegram, msg)


def _watchdog_select_failover(
    eligible: list,
    current: Optional[str],
    ticker: object,
    failover_cooloff_s: float,
) -> Optional[tuple[str, str, str]]:
    """Return (acct, api_key, access_token) for the next eligible failover account, or None."""
    for b in eligible:
        acct = getattr(b, "account", "") or ""
        if not acct or acct == current:
            continue
        if ticker.is_account_in_failover_cooloff(acct, failover_cooloff_s):  # type: ignore[union-attr]
            continue
        kc = getattr(b, "_conn", None) or getattr(b, "kite", None)
        api_key = getattr(kc, "api_key", None)
        access_token = (
            getattr(kc, "_access_token", None)
            or getattr(kc, "access_token", None)
        )
        if api_key and access_token:
            return (acct, api_key, access_token)
    return None


def _bg_build_degraded_msg(
    ticker: object,
    eligible: list,
    current: Optional[str],
    is_refire: bool,
) -> str:
    """Build the Telegram message for a ticker-degraded incident."""
    branch = config.get("deploy_branch", "main")
    branch_tag = f" [{branch}]" if branch != "main" else ""
    ts = timestamp_display()
    disconnected_s = ticker.seconds_since_disconnect()  # type: ignore[union-attr]
    acct_list = ", ".join(
        b_acct for b in eligible
        if (b_acct := getattr(b, "account", "") or "")
    ) or "?"
    refire_note = " (re-alert)" if is_refire else ""
    return (
        f"TickerWatchdog{branch_tag} — degraded{refire_note}\n"
        f"Both Kite accounts in failover cool-off.\n"
        f"Disconnect: {current or '?'} → {acct_list} (all blocked)\n"
        f"Sparkline degrading to broker.ltp() polling.\n"
        f"Disconnected for: {disconnected_s:.0f}s\n"
        f"Time: {ts}"
    )


async def _watchdog_alert_degraded(
    ticker: object,
    eligible: list,
    current: Optional[str],
    alert_refire_s: float,
) -> None:
    """Fire (or re-fire) the Telegram degraded alert when all accounts are blocked."""
    from backend.shared.helpers.alert_utils import _send_telegram
    from backend.shared.helpers.utils import is_enabled
    now_ts = _time.time()
    should_alert = (
        not _ticker_alert_state["alert_active"]
        or (now_ts - _ticker_alert_state["last_alerted_at"]) > alert_refire_s
    )
    if not should_alert:
        return
    if not _ticker_alert_state["alert_active"]:
        _ticker_alert_state["alert_active"] = True
        _ticker_alert_state["incident_start"] = now_ts
    _ticker_alert_state["last_alerted_at"] = now_ts
    is_refire = _ticker_alert_state["last_alerted_at"] != _ticker_alert_state["incident_start"]
    msg = _bg_build_degraded_msg(ticker, eligible, current, is_refire)
    disconnected_s = ticker.seconds_since_disconnect()  # type: ignore[union-attr]
    logger.warning(
        f"ticker watchdog: no eligible failover account "
        f"(current={current or '?'}, disconnected_s={disconnected_s:.0f}) — "
        f"continuing to wait for primary to recover"
    )
    if is_enabled("telegram"):
        await asyncio.to_thread(_send_telegram, msg)


def _bg_watchdog_reset_alert_state() -> None:
    """Clear the ticker-alert incident state when markets are closed."""
    if _ticker_alert_state["alert_active"]:
        _ticker_alert_state["alert_active"] = False
        _ticker_alert_state["incident_start"] = 0.0
        _ticker_alert_state["last_alerted_at"] = 0.0


async def _bg_watchdog_failover_or_alert(
    ticker: object,
    get_brokers,
    failover_cooloff_s: float,
    alert_refire_s: float,
) -> None:
    """Attempt ticker failover; fall back to degraded alert if no account available."""
    try:
        eligible = get_brokers()
    except Exception as e:
        logger.warning(f"ticker watchdog: eligible-broker lookup failed: {e}")
        return
    current = ticker.current_account()  # type: ignore[union-attr]
    next_kc = _watchdog_select_failover(eligible, current, ticker, failover_cooloff_s)
    if not next_kc:
        await _watchdog_alert_degraded(ticker, eligible, current, alert_refire_s)
        return
    acct, api_key, access_token = next_kc
    ok = ticker.restart_with_account(api_key, access_token, acct)  # type: ignore[union-attr]
    if ok:
        logger.info(f"ticker watchdog: failover OK → {acct}")
    else:
        logger.warning(f"ticker watchdog: failover to {acct} did not start")


async def _bg_watchdog_one_cycle(
    get_ticker,
    get_historical_brokers,
    failover_threshold_s: float,
    failover_cooloff_s: float,
    alert_refire_s: float,
) -> None:
    """Single watchdog poll: check ticker health and act if disconnected too long."""
    ticker = get_ticker()
    status = ticker.status()
    # Watchdog applies only to a ticker that's STARTED but has
    # been disconnected longer than the threshold. A ticker that
    # never started is the warm task's job to recover.
    if not status.get("started"):
        return
    if status.get("connected"):
        if _ticker_alert_state["alert_active"]:
            await _watchdog_handle_recovery(ticker, status)
        return  # healthy
    if ticker.seconds_since_disconnect() < failover_threshold_s:
        return  # within KiteTicker's own retry window
    await _bg_watchdog_failover_or_alert(
        ticker, get_historical_brokers,
        failover_cooloff_s, alert_refire_s,
    )


async def _task_ticker_watchdog(state: dict) -> None:
    CHECK_INTERVAL_S    = 30.0   # how often to poll ticker.status()
    # Raised from 60s in BG to absorb one full Twisted reconnect cycle.
    # KiteTicker's internal exponential backoff can reach 60–120s; with
    # the old 60s window, the second watchdog tick could trigger a
    # failover stop() exactly as Twisted was about to reconnect, killing
    # the recovery and pointlessly restarting on a different account.
    FAILOVER_THRESHOLD_S = 90.0  # how long disconnected before we fail over
    FAILOVER_COOLOFF_S  = 300.0  # don't retry a failed account for 5 min
    ALERT_REFIRE_S      = 1800.0  # re-alert after 30 min of sustained degradation

    from backend.brokers.kite_ticker import get_ticker
    from backend.brokers.registry import get_historical_brokers
    from backend.brokers.broker_apis import fetch_holidays
    from backend.brokers.client import is_cutover_on

    # Per-watchdog holiday cache keyed by year so off-hours gating doesn't
    # hammer nseindia.com every 30 s. Refreshes naturally at year rollover.
    _wd_holiday_cache: dict = {}
    _wd_holiday_year_ref: list = [None]  # mutable container for year tracking

    # Cutover branch — when KiteTicker lives in conn_service, the WS
    # lifecycle is its job (conn_service has its own ticker_watchdog).
    # Main API just reads the mmap buffer; failover-stop / restart-
    # with-account calls on MmapTickReader are no-ops AND get_historical
    # _brokers returns the conn_service account list, which would emit
    # false "degraded" Telegram alerts every 30min during market hours.
    # Skip this whole task in cutover mode.
    if is_cutover_on():
        logger.info(
            "ticker_watchdog: skipped — conn_service owns the WebSocket "
            "(its own watchdog supervises restart). Main API is mmap-reader only."
        )
        return

    while True:
        try:
            await asyncio.sleep(CHECK_INTERVAL_S)
            await _watchdog_deferred_recycle()

            from backend.shared.helpers.utils import is_engine_idle
            if is_engine_idle():
                continue

            any_open = await _watchdog_check_market_open(
                _wd_holiday_cache, _wd_holiday_year_ref, fetch_holidays
            )
            if not any_open:
                _bg_watchdog_reset_alert_state()
                continue

            await _bg_watchdog_one_cycle(
                get_ticker, get_historical_brokers,
                FAILOVER_THRESHOLD_S, FAILOVER_COOLOFF_S, ALERT_REFIRE_S,
            )

        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("ticker watchdog: unexpected error")


async def _task_visitor_log_daily() -> None:
    """
    Parse the day's nginx access.log once per day at 23:35 IST — five
    minutes after MCX closure (23:30 IST). Reporting at end-of-trading-day
    aligns the visitor digest with the operator's mental model of a
    trading session rather than the UTC calendar day.

    Upserts visitor_log, writes a markdown report, and sends the
    summary block to Telegram (gated by is_enabled('telegram')).
    The task fires immediately on startup with a 60-second delay to let
    DB init complete, then sleeps until the next 23:35 IST.
    """
    from backend.shared.helpers.utils import is_enabled

    await asyncio.sleep(60)  # let DB init settle

    async def _run_once(send_telegram: bool = True) -> None:
        try:
            # Use `arun_daily` (the async variant) directly so the
            # call runs on this task's event loop — asyncpg's pool is
            # bound to this loop. The prior path `await _run(run_daily)`
            # offloaded to a worker thread which then created its own
            # loop via asyncio.run; the resulting connection was on a
            # different loop than the upsert futures expected, producing
            # the chronic "Future attached to a different loop" errors
            # py-spy + log review caught.
            from backend.scripts.visitor_report import arun_daily
            report_path = await arun_daily()
            logger.info(f"Background: visitor log report → {report_path}")

            branch = config.get("deploy_branch", "main")
            branch_tag = f" [{branch}]" if branch != "main" else ""

            if send_telegram and is_enabled("telegram"):
                try:
                    from backend.shared.helpers.alert_utils import _send_telegram
                    from backend.scripts.visitor_report import summary_for_telegram
                    tg_body = summary_for_telegram(report_path)
                    msg = f"<b>Visitors{branch_tag}</b>\n{tg_body}"
                    await asyncio.to_thread(_send_telegram, msg)
                except Exception as tg_err:
                    logger.warning(f"Background: visitor log telegram failed: {tg_err}")
            # Operator request (Jun 2026): visitor reports ship to the
            # RamboQuant alerts Telegram channel only — no email. The
            # full markdown report stays on disk at .log/visitors-<date>.md
            # for any audit needs.
        except Exception as e:
            logger.error(f"Background: visitor log daily run failed: {e}")

    # Startup catch-up — refresh the markdown report on disk so an
    # operator who SSHs in mid-day sees today's data. The Telegram
    # dispatch is SUPPRESSED here so every redeploy doesn't fire a
    # visitor alert (operator: "why every deployment is creating
    # visitor alert"). The Telegram dispatch happens only on the
    # scheduled 23:35 IST run below.
    await _run_once(send_telegram=False)

    while True:
        # Daily at visitors.report_time_ist (default 23:35 IST — five min
        # after MCX closes at 23:30 IST so the day's commodity-session
        # traffic is fully captured). Re-read the setting on every
        # iteration so live edits via /admin/settings take effect on the
        # next scheduling cycle.
        from backend.shared.helpers.settings import get_string
        time_str = get_string("visitors.report_time_ist", "23:35")
        try:
            hh_s, mm_s = time_str.split(":", 1)
            hh, mm = int(hh_s), int(mm_s)
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                raise ValueError(f"out-of-range: {time_str}")
        except (ValueError, AttributeError) as parse_err:
            logger.warning(
                f"Background: visitor log report_time_ist={time_str!r} is "
                f"invalid ({parse_err}); falling back to 23:35"
            )
            hh, mm = 23, 35
        now = timestamp_indian()
        next_run = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now >= next_run:
            next_run += timedelta(days=1)
        sleep_s = (next_run - now).total_seconds()
        logger.info(
            f"Background: visitor log task sleeping {sleep_s/3600:.1f}h "
            f"until {hh:02d}:{mm:02d} IST"
        )
        await asyncio.sleep(sleep_s)
        await _run_once()


async def _apply_retention(
    session,
    table: str,
    ts_col: str,
    days: int,
    extra_where: str = "",
) -> int:
    """Single-statement DELETE with a time-based cutoff.

    Canonical helper used by every daily retention task so the
    pattern stays consistent and adding a new table is one line.

    Args:
        session:     Active async SQLAlchemy session (already open).
        table:       Bare table name (e.g. ``"audit_log"``).
        ts_col:      Timestamp column name (e.g. ``"created_at"``).
        days:        Rows older than this many days are deleted.
        extra_where: Optional additional SQL predicate (AND-joined),
                     e.g. ``"AND sim_mode = true"``.  Caller is
                     responsible for safe values (no user input here).

    Returns:
        Number of rows deleted (≥ 0).
    """
    from sqlalchemy import text as _text
    where = f"{ts_col} < now() - interval '{days} days'"
    if extra_where:
        where = f"{where} {extra_where}"
    res = await session.execute(_text(f"DELETE FROM {table} WHERE {where}"))
    return res.rowcount if res.rowcount >= 0 else 0


async def _task_purge_persistence_caches() -> None:
    """Daily 03:10 IST — purge stale rows from persistence-layer and
    operational tables.

    Scheduled 10 minutes after `_task_sim_cleanup` (which runs at 03:00 IST)
    so the two DELETE-heavy tasks don't compete for the connection pool at
    the same instant. mcp_audit cleanup at 03:15 stays clear of both.

    Persistence-layer tables
    ────────────────────────
    ohlcv_daily:          rows older than 5 years (immutable; 5y covers all UI ranges).
    instruments_snapshot: rows older than 7 days (latest snapshot sufficient;
                          anything older can be re-fetched if needed — keeps table tiny).
    holidays_snapshot:    no purge (years are tiny + useful for backtest of any year).
    intraday_bars:        rows older than 90 days (intraday rarely queried beyond 3 months).
    movers_snapshots:     rows older than 7 days (off-hours fallback; one row per day).

    Operational tables added in retention-audit sweep (Jun 2026)
    ────────────────────────────────────────────────────────────
    algo_events:          write-only diagnostic journal; rows older than
                          ``retention.algo_events_days`` (default 30) dropped.
                          184K rows at audit time → first run reclaims ~25 MB.
    algo_order_events:    per-order timeline; rows older than
                          ``retention.algo_order_events_days`` (default 90)
                          dropped.  Covers every UI query window.
    auth_tokens:          one-time verify/reset tokens; expired rows older than
                          ``retention.auth_tokens_days`` (default 7) beyond
                          their expiry are dropped.  Active tokens are untouched
                          regardless of created_at.
    """
    from sqlalchemy import text
    from backend.api.database import async_session
    from backend.shared.helpers.settings import get_int

    async def _run_once():
        # Fixed-horizon persistence tables (not configurable — these are
        # cache layers with well-understood TTLs that never need operator
        # adjustment).
        ohlcv_days    = 5 * 365   # 5 years
        instr_days    = 7
        intraday_days = 90
        movers_days   = 7         # keep last week of winners/losers snapshots

        # Configurable operational tables.
        algo_events_days       = get_int("retention.algo_events_days",       30)
        algo_oe_days           = get_int("retention.algo_order_events_days",  90)
        auth_tokens_days       = get_int("retention.auth_tokens_days",         7)

        try:
            async with async_session() as session:
                # ── Persistence-layer ──────────────────────────────────
                ohlcv_del    = await _apply_retention(
                    session, "ohlcv_daily",          "date",       ohlcv_days)
                instr_del    = await _apply_retention(
                    session, "instruments_snapshot", "date",       instr_days)
                intraday_del = await _apply_retention(
                    session, "intraday_bars",        "date",       intraday_days)
                movers_del   = await _apply_retention(
                    session, "movers_snapshots",     "date",       movers_days)

                # ── Operational tables ────────────────────────────────
                ae_del = 0
                if algo_events_days > 0:
                    ae_del = await _apply_retention(
                        session, "algo_events", "timestamp", algo_events_days)

                aoe_del = 0
                if algo_oe_days > 0:
                    aoe_del = await _apply_retention(
                        session, "algo_order_events", "ts", algo_oe_days)

                # auth_tokens: drop only expired rows whose expiry is
                # older than `auth_tokens_days` so an operator who is
                # mid-reset doesn't lose their token early.
                at_del = 0
                if auth_tokens_days > 0:
                    res = await session.execute(text(
                        f"DELETE FROM auth_tokens "
                        f"WHERE expires_at < now() - interval '{auth_tokens_days} days'"
                    ))
                    at_del = res.rowcount if res.rowcount >= 0 else 0

                await session.commit()

            logger.info(
                f"Background: persistence cache purge complete — "
                f"ohlcv_daily: {ohlcv_del} row(s), "
                f"instruments_snapshot: {instr_del} row(s), "
                f"intraday_bars: {intraday_del} row(s), "
                f"movers_snapshots: {movers_del} row(s), "
                f"algo_events: {ae_del} row(s), "
                f"algo_order_events: {aoe_del} row(s), "
                f"auth_tokens: {at_del} row(s)"
            )
        except Exception as exc:
            logger.warning(f"Background: persistence cache purge failed: {exc}")

    await asyncio.sleep(180)   # let startup settle

    while True:
        now = timestamp_indian()
        next_run = now.replace(hour=3, minute=10, second=0, microsecond=0)
        if now >= next_run:
            next_run += timedelta(days=1)
        sleep_s = (next_run - now).total_seconds()
        logger.info(f"Background: persistence cache purge sleeping {sleep_s/3600:.1f}h until 03:10 IST")
        await asyncio.sleep(sleep_s)
        await _run_once()


async def _task_purge_audit_log() -> None:
    """Daily 03:20 IST — purge old audit_log rows.

    Scheduled 5 minutes after `_task_purge_persistence_caches` (03:10)
    and 5 minutes after `_task_mcp_audit_cleanup` (03:15) so heavy
    DELETE operations stay staggered across the quiet overnight window.

    audit_log is NOT a compliance log in the SEBI Cat-III sense (that
    role belongs to nav_daily + daily_book which are kept forever).
    It IS a forensic trail for operator investigation windows — 365
    days covers a full calendar year of action history which is more
    than enough for any incident postmortem. Operator can extend via
    /admin/settings → retention.audit_log_days.

    Setting ``retention.audit_log_days = 0`` disables the purge so
    the table can grow indefinitely during extended debugging periods.
    """
    from backend.api.database import async_session
    from backend.shared.helpers.settings import get_int

    async def _purge_once():
        days = get_int("retention.audit_log_days", 365)
        if days <= 0:
            logger.info("Background: audit_log retention disabled (days=0)")
            return
        try:
            async with async_session() as session:
                deleted = await _apply_retention(session, "audit_log", "created_at", days)
                await session.commit()
            logger.info(
                f"Background: audit_log purged {deleted} row(s) older than {days} days"
            )
        except Exception as exc:
            logger.error(f"Background: audit_log purge failed: {exc}")

    await asyncio.sleep(210)  # startup settle — runs after persistence purge

    await _purge_once()

    while True:
        now = timestamp_indian()
        next_run = now.replace(hour=3, minute=20, second=0, microsecond=0)
        if now >= next_run:
            next_run += timedelta(days=1)
        sleep_s = (next_run - now).total_seconds()
        logger.info(f"Background: audit_log purge sleeping {sleep_s/3600:.1f}h until 03:20 IST")
        await asyncio.sleep(sleep_s)
        await _purge_once()


async def _task_purge_visitor_log() -> None:
    """Daily 03:25 IST — purge old visitor_log rows.

    visitor_log holds one row per unique (ip, UTC-date).  Row-level
    detail beyond 90 days is rarely needed — page-level aggregates are
    more useful at that horizon and take a fraction of the space.

    Scheduled 5 minutes after ``_task_purge_audit_log`` (03:20) to
    keep DELETE-heavy tasks staggered across the quiet overnight window.

    Setting ``retention.visitor_log_days = 0`` disables the purge so
    the table can grow indefinitely (useful during traffic investigations
    that span more than the default window).
    """
    from backend.api.database import async_session
    from backend.shared.helpers.settings import get_int

    async def _purge_once():
        days = get_int("retention.visitor_log_days", 90)
        if days <= 0:
            logger.info("Background: visitor_log retention disabled (days=0)")
            return
        try:
            async with async_session() as session:
                deleted = await _apply_retention(session, "visitor_log", "created_at", days)
                await session.commit()
            logger.info(
                f"Background: visitor_log purged {deleted} row(s) older than {days} days"
            )
        except Exception as exc:
            logger.error(f"Background: visitor_log purge failed: {exc}")

    await asyncio.sleep(240)  # startup settle — runs after audit_log purge

    await _purge_once()

    while True:
        now = timestamp_indian()
        next_run = now.replace(hour=3, minute=25, second=0, microsecond=0)
        if now >= next_run:
            next_run += timedelta(days=1)
        sleep_s = (next_run - now).total_seconds()
        logger.info(f"Background: visitor_log purge sleeping {sleep_s/3600:.1f}h until 03:25 IST")
        await asyncio.sleep(sleep_s)
        await _purge_once()


async def _task_purge_impersonation_events() -> None:
    """Daily 03:30 IST — purge old impersonation_events rows.

    impersonation_events is a forensic surface — one row per sudo
    session (admin/designated views platform as partner).  365 days
    matches the audit_log horizon so the two trails are co-extensive.

    Scheduled 5 minutes after ``_task_purge_visitor_log`` (03:25).

    Setting ``retention.impersonation_events_days = 0`` disables the
    purge (useful during extended compliance investigations).
    """
    from backend.api.database import async_session
    from backend.shared.helpers.settings import get_int

    async def _purge_once():
        days = get_int("retention.impersonation_events_days", 365)
        if days <= 0:
            logger.info("Background: impersonation_events retention disabled (days=0)")
            return
        try:
            async with async_session() as session:
                deleted = await _apply_retention(
                    session, "impersonation_events", "started_at", days
                )
                await session.commit()
            logger.info(
                f"Background: impersonation_events purged {deleted} row(s) older than {days} days"
            )
        except Exception as exc:
            logger.error(f"Background: impersonation_events purge failed: {exc}")

    await asyncio.sleep(270)  # startup settle

    await _purge_once()

    while True:
        now = timestamp_indian()
        next_run = now.replace(hour=3, minute=30, second=0, microsecond=0)
        if now >= next_run:
            next_run += timedelta(days=1)
        sleep_s = (next_run - now).total_seconds()
        logger.info(
            f"Background: impersonation_events purge sleeping "
            f"{sleep_s/3600:.1f}h until 03:30 IST"
        )
        await asyncio.sleep(sleep_s)
        await _purge_once()


async def _task_purge_admin_email_events() -> None:
    """Daily 03:35 IST — purge old admin_email_events rows.

    admin_email_events records every /api/admin/email-partners POST —
    who sent what to whom.  Delivery-status audit beyond 90 days is
    rarely investigated; 90 days covers two full monthly-statement
    cycles and the most likely incident-response window.

    Scheduled 5 minutes after ``_task_purge_impersonation_events``
    (03:30).

    Setting ``retention.admin_email_events_days = 0`` disables the
    purge so the table can grow indefinitely.
    """
    from backend.api.database import async_session
    from backend.shared.helpers.settings import get_int

    async def _purge_once():
        days = get_int("retention.admin_email_events_days", 90)
        if days <= 0:
            logger.info("Background: admin_email_events retention disabled (days=0)")
            return
        try:
            async with async_session() as session:
                deleted = await _apply_retention(
                    session, "admin_email_events", "created_at", days
                )
                await session.commit()
            logger.info(
                f"Background: admin_email_events purged {deleted} row(s) older than {days} days"
            )
        except Exception as exc:
            logger.error(f"Background: admin_email_events purge failed: {exc}")

    await asyncio.sleep(300)  # startup settle

    await _purge_once()

    while True:
        now = timestamp_indian()
        next_run = now.replace(hour=3, minute=35, second=0, microsecond=0)
        if now >= next_run:
            next_run += timedelta(days=1)
        sleep_s = (next_run - now).total_seconds()
        logger.info(
            f"Background: admin_email_events purge sleeping "
            f"{sleep_s/3600:.1f}h until 03:35 IST"
        )
        await asyncio.sleep(sleep_s)
        await _purge_once()


async def _task_market_lifecycle() -> None:
    """Poll the MarketLifecycle singleton every 30s.

    Detects per-exchange open/close transitions (and the 45-min
    `close_settled` follow-up window) and fires registered handlers.
    The handlers themselves live in
    `backend/api/algo/market_lifecycle_handlers.py`; this task is just
    the heartbeat that drives `market_lifecycle.poll()`.

    Exception-safe: a transient poll failure logs + retries on the
    next 30s tick. The lifecycle's internal state holds across the
    error so a one-off broker probe glitch does not turn a real
    transition into a missed event.
    """
    from backend.api.algo.market_lifecycle import market_lifecycle
    from backend.api.algo.market_lifecycle_handlers import register_default_handlers

    # Wire default handlers once at task start so the singleton is ready
    # for the very first poll. Idempotent.
    register_default_handlers()

    # Brief startup settle — give the broker singleton a chance to
    # rebuild from DB so the first poll's `fetch_holidays` calls hit a
    # warm cache instead of forcing a cold KiteHTTP fetch.
    await asyncio.sleep(8)

    while True:
        try:
            result = await market_lifecycle.poll()
            events = result.get("events", [])
            if events:
                summary = ", ".join(
                    f"{e['exchange']}:{e['event_type']}({e['handlers_run']}/{e['handlers_failed']}f)"
                    for e in events
                )
                logger.info(f"Background: market_lifecycle fired — {summary}")
        except Exception as e:
            logger.warning(f"Background: market_lifecycle poll failed: {e}")
        await asyncio.sleep(30)


async def _task_funds_offhours() -> None:
    """Low-cadence funds refresh during closed-market hours.

    Operator may transfer money during off-hours; the funds-cache must
    still update so /performance + NavCard reflect the new balance
    without waiting until the next session.

    Runs every 30 min when NO segment is open. When any segment is
    open this task short-circuits because the regular `_task_performance`
    is already firing funds fetches at the standard 5-min cadence.
    """
    from backend.shared.helpers.date_time_utils import is_any_segment_open
    from backend.api.cache import invalidate

    # Startup settle.
    await asyncio.sleep(60)

    while True:
        try:
            now_ist = timestamp_indian()
            if not is_any_segment_open(now_ist):
                # Off-hours fetch — refresh funds only (cheapest broker
                # call) so cash/balance moves are reflected for
                # NAV + /performance. Positions + holdings need no
                # tick refresh during closed hours; the daily snapshot
                # task handles their EOD capture.
                try:
                    await _run(_fetch_margins_direct)
                    invalidate("funds")
                    logger.info("Background: funds refreshed during closed-market window")
                except Exception as e:
                    logger.warning(f"Background: off-hours funds refresh failed: {e}")
            # 30 min cadence — operator transfers are minute-grained
            # but rare; 30 min is the right balance vs broker quota.
        except Exception as e:
            logger.warning(f"Background: _task_funds_offhours iteration failed: {e}")
        await asyncio.sleep(30 * 60)


async def _task_closed_hours_refresh() -> None:
    """Low-cadence positions/holdings/funds snapshot during closed-market hours.

    Post-settlement, brokers update position close prices and realised P&L
    values that are only available after the exchange publishes them (up to
    ~30–60 min after segment close). This task refreshes daily_book every
    30 min when NO segment is open so snapshot routes always serve fresh
    post-settlement data rather than the last live-session write.

    Complements ``_task_funds_offhours`` (funds-only, 30 min) and
    ``_task_daily_snapshot`` (settlement pass at 16:15 / 00:15) — this is
    a rolling backstop for the in-between windows.

    When any segment opens, the iteration is skipped (the live
    ``_task_performance`` poller runs at the standard 5-min cadence and
    ``snapshot_daily_book`` would pollute daily_book with mid-session LTPs).
    """
    from backend.shared.helpers.date_time_utils import is_any_segment_open
    from backend.api.algo.daily_snapshot import snapshot_daily_book
    from backend.api.cache import invalidate
    from backend.brokers.broker_apis import _raw_cache_invalidate

    logger.info("Background: closed-hours refresh loop started")

    # Startup settle — let the daily-snapshot startup pass fire first so
    # we don't duplicate the immediate post-boot write.
    await asyncio.sleep(120)

    while True:
        try:
            now_ist = timestamp_indian()
            if not is_any_segment_open(now_ist):
                # Bust the 30s broker raw cache so snapshot_daily_book
                # polls the broker, not the in-memory memoisation tier.
                # (snapshot_daily_book calls broker.holdings/positions
                # directly, but raw cache busting keeps API routes coherent
                # for the next inbound request after the snapshot lands.)
                try:
                    _raw_cache_invalidate("positions")
                    _raw_cache_invalidate("holdings")
                    _raw_cache_invalidate("margins")
                except Exception as _ce:
                    logger.debug(f"Background: closed-hours cache invalidate warning: {_ce}")

                try:
                    result = await snapshot_daily_book()
                    # Invalidate the API-side TTL cache so routes serve the
                    # freshly-written daily_book rows on the next request.
                    invalidate("positions")
                    invalidate("holdings")
                    invalidate("funds")
                    logger.info(
                        f"Background: closed-hours refresh complete "
                        f"at {now_ist.isoformat()} — "
                        f"accounts={result['accounts']} "
                        f"h={result['holdings_rows']} "
                        f"p={result['positions_rows']} "
                        f"errors={result['errors']}"
                    )
                except Exception as e:
                    logger.warning(f"Background: closed-hours refresh snapshot failed: {e}")
        except asyncio.CancelledError:
            logger.info("Background: closed-hours refresh loop exiting (cancelled)")
            raise
        except Exception as e:
            logger.warning(f"Background: _task_closed_hours_refresh iteration failed: {e}")

        await asyncio.sleep(30 * 60)


async def _bg_resolve_watchlist_sym(
    sym: str,
    exch: str,
    resolve_mcx,
    resolve_cds,
) -> str:
    """Resolve a virtual MCX/CDS root to its front-month contract symbol."""
    if exch == "MCX" and sym.isalpha() and len(sym) <= 12:
        resolved = await resolve_mcx(sym)
        if resolved:
            return resolved.upper().strip()
    elif exch == "CDS" and sym.isalpha() and len(sym) <= 12:
        resolved = await resolve_cds(sym)
        if resolved:
            return resolved.upper().strip()
    return sym


async def _backfill_collect_watchlist(
    symbols: list[tuple[str, str]],
    seen: set[tuple[str, str]],
) -> None:
    """Append watchlist symbols (with MCX/CDS resolution) into symbols/seen in place."""
    try:
        from backend.api.database import async_session
        from backend.api.models import WatchlistItem
        from sqlalchemy import select as sa_select
        from backend.api.routes.watchlist import _resolve_mcx_commodity, _resolve_cds_currency

        async with async_session() as sess:
            rows = (await sess.execute(
                sa_select(WatchlistItem.tradingsymbol, WatchlistItem.exchange)
            )).all()
        for row in rows:
            sym  = (row.tradingsymbol or "").upper().strip()
            exch = (row.exchange or "NSE").upper().strip()
            if not sym:
                continue
            sym = await _bg_resolve_watchlist_sym(sym, exch, _resolve_mcx_commodity, _resolve_cds_currency)
            key = (sym, exch)
            if key not in seen:
                seen.add(key)
                symbols.append(key)
    except Exception as exc:
        logger.warning(f"backfill warm: watchlist collect failed: {exc}")


def _bg_append_book_symbols(
    df: "pd.DataFrame",
    symbols: list[tuple[str, str]],
    seen: set[tuple[str, str]],
    default_exch: str,
) -> None:
    """Drain a broker DataFrame into symbols/seen in place (no IO)."""
    if df.empty or "tradingsymbol" not in df.columns:
        return
    exchanges = (
        df["exchange"] if "exchange" in df.columns
        else pd.Series([default_exch] * len(df))
    )
    for sym_raw, exch_raw in zip(df["tradingsymbol"], exchanges):
        sym  = str(sym_raw or "").upper().strip()
        exch = str(exch_raw or default_exch).upper().strip()
        if sym:
            key = (sym, exch)
            if key not in seen:
                seen.add(key)
                symbols.append(key)


async def _backfill_collect_holdings(
    symbols: list[tuple[str, str]],
    seen: set[tuple[str, str]],
) -> None:
    """Append holding symbols from broker API into symbols/seen in place."""
    try:
        import pandas as pd
        from backend.brokers import broker_apis
        dfs = await asyncio.to_thread(broker_apis.fetch_holdings)
        df_h = pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()
        _bg_append_book_symbols(df_h, symbols, seen, "NSE")
    except Exception as exc:
        logger.warning(f"backfill warm: holdings collect failed: {exc}")


async def _backfill_collect_positions(
    symbols: list[tuple[str, str]],
    seen: set[tuple[str, str]],
) -> None:
    """Append position symbols from broker API into symbols/seen in place."""
    try:
        import pandas as pd
        from backend.brokers import broker_apis
        dfs = await asyncio.to_thread(broker_apis.fetch_positions)
        df_p = pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()
        _bg_append_book_symbols(df_p, symbols, seen, "NFO")
    except Exception as exc:
        logger.warning(f"backfill warm: positions collect failed: {exc}")


def _backfill_collect_movers(
    symbols: list[tuple[str, str]],
    seen: set[tuple[str, str]],
) -> None:
    """Append mover-universe symbols into symbols/seen in place."""
    try:
        from backend.shared.helpers.mover_universe import mover_warm_pairs
        for key in mover_warm_pairs():
            if key not in seen:
                seen.add(key)
                symbols.append(key)
    except Exception as exc:
        logger.warning(f"backfill warm: mover universe collect failed: {exc}")


def _backfill_apply_cap(symbols: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Apply the 300-symbol cap (book pairs first, then movers)."""
    try:
        from backend.shared.helpers.mover_universe import mover_warm_pairs as _mwp
        _mover_set  = set(_mwp())
        book_pairs  = [p for p in symbols if p not in _mover_set]
        mover_pairs = [p for p in symbols if p in _mover_set]
        cap         = 300
        remaining   = max(0, cap - len(book_pairs))
        return book_pairs + mover_pairs[:remaining]
    except Exception:
        return symbols[:300]


async def _backfill_run_ohlcv(symbols: list[tuple[str, str]]) -> None:
    """Run the OHLCV daily backfill and log the result."""
    from backend.api.persistence.backfill import backfill_ohlcv_daily
    try:
        result = await backfill_ohlcv_daily(symbols, target_days=365, max_concurrent=3)
        logger.info(
            f"backfill warm: ohlcv_daily done — "
            f"filled={result['filled']}, skipped_cooloff={result['skipped_cooloff']}, "
            f"errors={len(result['errors'])}"
        )
    except Exception as exc:
        logger.error(f"backfill warm: ohlcv_daily failed: {exc}")


async def _backfill_run_intraday(symbols: list[tuple[str, str]]) -> None:
    """Run today's intraday backfill (deferred when no segment is open)."""
    from backend.api.persistence.backfill import backfill_intraday_today
    from backend.shared.helpers.date_time_utils import is_any_segment_open
    try:
        now_ist = timestamp_indian()
        if is_any_segment_open(now_ist):
            result2 = await backfill_intraday_today(symbols, interval="30minute", max_concurrent=3)
            logger.info(
                f"backfill warm: intraday_today done — "
                f"filled={result2['filled']}, skipped_cooloff={result2['skipped_cooloff']}, "
                f"errors={len(result2['errors'])}"
            )
        else:
            logger.info(
                "backfill warm: intraday_today deferred — no segment open "
                "(today's bars already in DB from prior session write-back)"
            )
    except Exception as exc:
        logger.error(f"backfill warm: intraday_today failed: {exc}")


async def _task_warm_backfill() -> None:
    """One-shot startup backfill for ohlcv_daily and intraday_bars.

    Fires 60 s after process start to give the conn_service time to mint
    fresh broker tokens.  Runs exactly once per process lifetime.

    OHLCV backfill (historical — broker serves even when markets closed):
    always runs on startup regardless of market hours.  Checks coverage for
    every symbol in the warm universe (watchlist + holdings + positions +
    movers, 300-symbol cap) and force-fetches any symbol with fewer than
    target_days × 0.7 bars.

    Intraday backfill (today's bars — broker only during open hours):
    deferred when no segment is open because today's intraday bars are still
    accumulating during the session.  When markets are closed the intraday
    bars from the prior session are already in intraday_bars via the
    persistence pipeline's regular write-back.
    """
    # Guard: only fire once per process (idempotent against module reloads).
    if getattr(_task_warm_backfill, "_fired", False):
        return
    _task_warm_backfill._fired = True   # type: ignore[attr-defined]

    # Startup settle — wait for conn_service token mint.
    await asyncio.sleep(60)

    # Build the same symbol universe as _task_sparkline_warm (300-symbol cap).
    symbols: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    await _backfill_collect_watchlist(symbols, seen)
    await _backfill_collect_holdings(symbols, seen)
    await _backfill_collect_positions(symbols, seen)
    _backfill_collect_movers(symbols, seen)
    symbols = _backfill_apply_cap(symbols)

    if not symbols:
        logger.warning("backfill warm: empty symbol universe — nothing to backfill")
        return

    logger.info(f"backfill warm: universe={len(symbols)} symbols")

    await _backfill_run_ohlcv(symbols)
    await _backfill_run_intraday(symbols)


def _parse_perf_snapshot_rows(snap: dict) -> list:
    """Convert a perf_baseline JSON dict to a list of PerfSnapshot ORM rows.

    Module-level so it can be imported directly by tests and other callers
    without pulling in the full background-task machinery.

    Args:
        snap: Parsed JSON from ``scripts/perf_baseline.py``. Expected shape::

            {
                "captured_at": "2026-07-01T04:00:00Z",
                "commit": "<sha>",
                "frontend": {"pages": {"<route>": {...}}},
                "backend":  {"routes": {"<label>": {...}}},
            }

    Returns:
        List of unsaved ``PerfSnapshot`` instances ready for
        ``session.add_all(...)``.
    """
    from backend.api.models import PerfSnapshot

    rows: list[PerfSnapshot] = []
    try:
        raw_ts = snap.get("captured_at", "")
        # Normalise trailing Z → +00:00 for fromisoformat on Python ≤ 3.10
        captured_at = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        captured_at = datetime.now(timezone.utc)

    commit_sha: Optional[str] = snap.get("commit") or None
    if commit_sha and len(commit_sha) > 40:
        commit_sha = commit_sha[:40]

    # Frontend pages + lib components
    for route, row in snap.get("frontend", {}).get("pages", {}).items():
        cc_avg: Optional[float] = row.get("cyclomatic_avg")
        cc_max: Optional[int]   = row.get("cyclomatic_max")
        hotspots = row.get("cyclomatic_hotspots") or None
        # Runtime block (present only when _merge_runtime_into_rows() was called)
        rt = row.get("runtime") or {}
        lcp_ms:  Optional[int]   = rt.get("lcp_ms")
        tbt_ms:  Optional[int]   = rt.get("tbt_ms")
        heap_mb: Optional[float] = rt.get("heap_mb")
        rows.append(PerfSnapshot(
            captured_at=captured_at,
            commit_sha=commit_sha,
            side="FE",
            page_or_route=route,
            loc=row.get("loc"),
            effect_count=row.get("effect_count"),
            state_count=row.get("state_count"),
            derived_count=row.get("derived_count"),
            cc_max=cc_max,
            cc_avg=cc_avg,
            hotspots_json=hotspots,
            lcp_ms=lcp_ms,
            tbt_ms=tbt_ms,
            heap_mb=heap_mb,
        ))

    # Backend routes
    for route, row in snap.get("backend", {}).get("routes", {}).items():
        rows.append(PerfSnapshot(
            captured_at=captured_at,
            commit_sha=commit_sha,
            side="BE",
            page_or_route=route,
            loc=row.get("loc"),
            cc_max=row.get("cyclomatic_max"),
            cc_avg=row.get("cyclomatic_avg"),
            hotspots_json=row.get("cyclomatic_hotspots") or None,
        ))
    return rows


def _merge_runtime_into_rows(rows: list, capture_json: dict) -> int:
    """Patch LCP/TBT/heap from a ``perf_capture_latest.json`` blob into the
    already-parsed PerfSnapshot ORM row list.

    Module-level so tests can import and exercise it directly.

    The capture JSON has the same ``frontend.pages.<route>.runtime.*`` shape
    as ``perf_baseline.py``'s ``--with-runtime`` merge step.  We match by
    ``page_or_route`` (FE side only) and set the three runtime fields in
    place.

    Args:
        rows:         List of ``PerfSnapshot`` instances from
                      :func:`_parse_perf_snapshot_rows`.
        capture_json: Parsed ``.log/perf_capture_latest.json``.

    Returns:
        Number of FE rows that received at least one non-None runtime field.
    """
    cap_pages: dict = (
        capture_json.get("frontend", {}).get("pages", {})
    )
    patched = 0
    for row in rows:
        if row.side != "FE":
            continue
        rt = cap_pages.get(row.page_or_route, {}).get("runtime") or {}
        if not rt:
            continue
        lcp  = rt.get("lcp_ms")
        tbt  = rt.get("tbt_ms")
        heap = rt.get("heap_mb")
        if lcp is None and tbt is None and heap is None:
            continue
        row.lcp_ms  = lcp
        row.tbt_ms  = tbt
        row.heap_mb = heap
        patched += 1
    return patched


async def _task_perf_snapshot() -> None:
    """Daily 04:00 IST — run perf_baseline.py and persist results to perf_snapshots.

    Two-step execution for graceful degradation:

    **Step 1 — static baseline (~5 s)**
    ``scripts/perf_baseline.py --with-cyclomatic --no-build`` is run first.
    It writes ``.log/perf_baseline_<ts>.json`` to disk. If this step fails
    no rows are inserted for this cycle and the error is logged.

    **Step 2 — Playwright runtime capture (~2-4 min, optional)**
    When ``perf_snapshot.runtime_enabled`` is True in the settings DB,
    ``scripts/perf_capture_run.sh`` is executed as a separate subprocess with
    ``PLAYWRIGHT_USER`` / ``PLAYWRIGHT_PASS`` injected from ``secrets.yaml``.
    Its output ``.log/perf_capture_latest.json`` is then merged into the
    static rows via :func:`_merge_runtime_into_rows` before the DB insert.

    **Graceful degradation**:
    - Playwright timeout (``perf_snapshot.runtime_timeout_s``, default 600)
      → static-only rows are inserted; a WARNING is logged.
    - Playwright non-zero exit or network unreachable → same static-only path.
    - ``runtime_enabled = False`` → static-only (no subprocess launched).
    - Static step failure → no rows inserted; error logged.

    Startup backfill: any ``.log/perf_baseline_*.json`` files found at boot
    that have not yet been ingested are imported once. After that the cron
    is the sole writer. Idempotency: a (captured_at, side, page_or_route)
    combination can appear multiple times without constraint (each nightly
    run inserts fresh rows); the /latest endpoint uses DISTINCT ON to
    surface the most recent.
    """
    import json as _json
    import os as _os
    from pathlib import Path
    from backend.api.database import async_session
    from backend.shared.helpers.settings import get_int, get_bool

    ROOT = Path(__file__).resolve().parent.parent.parent
    LOG_DIR = ROOT / ".log"
    SCRIPT = ROOT / "scripts" / "perf_baseline.py"
    RT_SCRIPT = ROOT / "scripts" / "perf_capture_run.sh"
    VENV_PY = ROOT / "venv" / "bin" / "python"
    PYTHON = str(VENV_PY) if VENV_PY.exists() else "python"

    async def _run_static() -> Optional[dict]:
        """Run perf_baseline.py --with-cyclomatic --no-build.

        Returns the parsed JSON dict on success, None on any failure.
        The JSON is written to .log/ by the script before we read it.
        """
        cmd = [PYTHON, str(SCRIPT), "--with-cyclomatic", "--no-build"]
        logger.info("Background: _task_perf_snapshot static: %s", " ".join(cmd))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(ROOT),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            except asyncio.TimeoutError:
                proc.kill()
                logger.error(
                    "Background: _task_perf_snapshot static step timed out after 120s"
                )
                return None
        except Exception as exc:
            logger.error(
                "Background: _task_perf_snapshot static step launch failed: %s", exc
            )
            return None

        if proc.returncode != 0:
            logger.error(
                "Background: perf_baseline.py exited %d — stderr: %s",
                proc.returncode,
                (stderr or b"").decode("utf-8", "replace")[:500],
            )
            return None

        # Find the freshest perf_baseline_*.json written by this run.
        try:
            latest = max(
                LOG_DIR.glob("perf_baseline_*.json"),
                key=lambda p: p.stat().st_mtime,
                default=None,
            )
            if latest is None:
                logger.warning("Background: _task_perf_snapshot — no baseline JSON found")
                return None
            return _json.loads(latest.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error(
                "Background: _task_perf_snapshot failed to read static JSON: %s", exc
            )
            return None

    async def _run_runtime(timeout_s: int) -> Optional[dict]:
        """Run scripts/perf_capture_run.sh against dev.ramboq.com.

        Returns the parsed perf_capture_latest.json dict on success,
        None on timeout / non-zero exit / missing JSON.  Merging into
        static rows is the caller's responsibility.
        """
        if not RT_SCRIPT.exists():
            logger.warning(
                "Background: _task_perf_snapshot runtime: %s missing — skipping",
                RT_SCRIPT.name,
            )
            return None

        from backend.shared.helpers.utils import secrets as _secrets
        pw_user = (_secrets.get("playwright_user") or "rambo")
        pw_pass = (_secrets.get("playwright_pass") or "admin1234")

        # Merge env — keep full PATH (cron may have a stripped environment).
        runtime_env = {
            **_os.environ,
            "PLAYWRIGHT_USER": pw_user,
            "PLAYWRIGHT_PASS": pw_pass,
            "PLAYWRIGHT_BASE_URL": "https://dev.ramboq.com",
            "PERF_CAPTURE_QUIET": "1",
        }

        logger.info(
            "Background: _task_perf_snapshot runtime: bash %s (timeout %ds)",
            RT_SCRIPT.name, timeout_s,
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", str(RT_SCRIPT),
                cwd=str(ROOT),
                env=runtime_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=float(timeout_s)
                )
            except asyncio.TimeoutError:
                proc.kill()
                logger.warning(
                    "Background: _task_perf_snapshot runtime timed out after %ds"
                    " — inserting static-only rows",
                    timeout_s,
                )
                return None
        except Exception as exc:
            logger.error(
                "Background: _task_perf_snapshot runtime launch failed: %s", exc
            )
            return None

        if proc.returncode not in (0, 1):
            # Exit 1 = partial run (spec completed but individual pages errored).
            # The script still writes perf_capture_latest.json in that case —
            # attempt a best-effort read. Exits 2/3/4 are hard failures.
            logger.warning(
                "Background: perf_capture_run.sh exited %d — stderr: %s",
                proc.returncode,
                (stderr or b"").decode("utf-8", "replace")[:400],
            )
            if proc.returncode >= 2:
                return None

        cap_latest = LOG_DIR / "perf_capture_latest.json"
        if not cap_latest.exists():
            logger.warning(
                "Background: _task_perf_snapshot — perf_capture_latest.json"
                " missing after runtime run; inserting static-only rows"
            )
            return None
        try:
            return _json.loads(cap_latest.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error(
                "Background: _task_perf_snapshot failed to read capture JSON: %s", exc
            )
            return None

    async def _run_and_insert() -> None:
        """Full nightly cycle: static → optional runtime → DB insert."""
        from backend.shared.helpers.settings import get_int, get_bool

        # ── Step 1: static (always) ────────────────────────────────────────
        snap = await _run_static()
        if snap is None:
            logger.error(
                "Background: _task_perf_snapshot static step failed — skipping cycle"
            )
            return

        orm_rows = _parse_perf_snapshot_rows(snap)
        if not orm_rows:
            logger.warning("Background: _task_perf_snapshot — empty row set from JSON")
            return

        # ── Step 2: runtime (optional) ────────────────────────────────────
        runtime_enabled = get_bool("perf_snapshot.runtime_enabled", False)
        runtime_count = 0
        if runtime_enabled:
            timeout_s = get_int("perf_snapshot.runtime_timeout_s", 600)
            cap_json = await _run_runtime(timeout_s=timeout_s)
            if cap_json is not None:
                runtime_count = _merge_runtime_into_rows(orm_rows, cap_json)
                logger.info(
                    "Background: _task_perf_snapshot runtime merged %d FE row(s)",
                    runtime_count,
                )
            else:
                logger.info(
                    "Background: _task_perf_snapshot runtime unavailable"
                    " — inserting static-only rows"
                )

        # ── Step 3: insert ────────────────────────────────────────────────
        async with async_session() as session:
            session.add_all(orm_rows)
            await session.commit()
        logger.info(
            "Background: _task_perf_snapshot inserted %d row(s) for capture %s"
            " (%d with runtime metrics)",
            len(orm_rows),
            snap.get("captured_at", "?"),
            runtime_count,
        )

    async def _backfill_from_disk() -> None:
        """One-shot: ingest any existing .log/perf_baseline_*.json files.

        Runs only when the perf_snapshots table is empty. Idempotent:
        once any row is present the backfill is skipped on subsequent
        boots.
        """
        async with async_session() as session:
            from sqlalchemy import select as _sel, func as _func
            from backend.api.models import PerfSnapshot
            count = (await session.execute(
                _sel(_func.count()).select_from(PerfSnapshot)
            )).scalar_one()
        if count > 0:
            logger.info(
                "Background: perf_snapshot backfill skipped (%d rows already present)", count
            )
            return

        files = sorted(LOG_DIR.glob("perf_baseline_*.json"), key=lambda p: p.name)
        if not files:
            logger.info("Background: perf_snapshot backfill — no .log/perf_baseline_*.json found")
            return

        total = 0
        for f in files:
            try:
                snap = _json.loads(f.read_text(encoding="utf-8"))
                orm_rows = _parse_perf_snapshot_rows(snap)
                if orm_rows:
                    async with async_session() as session:
                        session.add_all(orm_rows)
                        await session.commit()
                    total += len(orm_rows)
                    logger.info(
                        "Background: perf_snapshot backfill: ingested %s (%d rows)",
                        f.name, len(orm_rows),
                    )
            except Exception as exc:
                logger.warning("Background: perf_snapshot backfill: error on %s: %s", f.name, exc)

        logger.info("Background: perf_snapshot backfill complete — %d total rows inserted", total)

    # Startup: one-shot disk backfill (deferred 120s to let DB settle).
    await asyncio.sleep(120)
    try:
        await _backfill_from_disk()
    except Exception as exc:
        logger.error("Background: perf_snapshot backfill failed: %s", exc)

    # Daily cron at 04:00 IST.
    while True:
        now = timestamp_indian()
        next_run = now.replace(hour=4, minute=0, second=0, microsecond=0)
        if now >= next_run:
            next_run += timedelta(days=1)
        sleep_s = (next_run - now).total_seconds()
        logger.info(
            "Background: _task_perf_snapshot sleeping %.1fh until 04:00 IST", sleep_s / 3600
        )
        await asyncio.sleep(sleep_s)
        try:
            await _run_and_insert()
        except Exception as exc:
            logger.error("Background: _task_perf_snapshot iteration failed: %s", exc)


async def _task_purge_perf_snapshots() -> None:
    """Daily 04:05 IST — purge old perf_snapshots rows.

    Scheduled 5 minutes after ``_task_perf_snapshot`` (04:00) so the
    nightly insert lands before the purge window fires.

    Setting ``retention.perf_snapshots_days = 0`` disables the purge
    so the table grows indefinitely (useful for long-term trend analysis).
    """
    from backend.api.database import async_session
    from backend.shared.helpers.settings import get_int

    async def _purge_once():
        days = get_int("retention.perf_snapshots_days", 365)
        if days <= 0:
            logger.info("Background: perf_snapshots retention disabled (days=0)")
            return
        try:
            async with async_session() as session:
                deleted = await _apply_retention(session, "perf_snapshots", "captured_at", days)
                await session.commit()
            logger.info(
                "Background: perf_snapshots purged %d row(s) older than %d days",
                deleted, days,
            )
        except Exception as exc:
            logger.error("Background: perf_snapshots purge failed: %s", exc)

    await asyncio.sleep(300)  # startup settle — 5 min after process start

    await _purge_once()

    while True:
        now = timestamp_indian()
        next_run = now.replace(hour=4, minute=5, second=0, microsecond=0)
        if now >= next_run:
            next_run += timedelta(days=1)
        sleep_s = (next_run - now).total_seconds()
        logger.info(
            "Background: perf_snapshots purge sleeping %.1fh until 04:05 IST", sleep_s / 3600
        )
        await asyncio.sleep(sleep_s)
        await _purge_once()


async def on_startup(app) -> None:
    """Start all background tasks. Called by Litestar on startup."""
    state: dict = {}
    # Kick the batched WebSocket-event persist loop — collapses bursts of
    # agent fires into one-commit-per-second instead of one-task-per-event.
    from backend.api.routes.algo import start_persist_flush
    start_persist_flush()
    app.state.bg_tasks = [
        asyncio.create_task(_task_market(state),         name="bg-market"),
        asyncio.create_task(_task_performance(state),    name="bg-performance"),
        asyncio.create_task(_task_close(state),          name="bg-close"),
        asyncio.create_task(_task_expiry_check(),        name="bg-expiry"),
        asyncio.create_task(_task_instruments(),         name="bg-instruments"),
        asyncio.create_task(_task_daily_snapshot(),      name="bg-daily-snapshot"),
        asyncio.create_task(_task_sim_cleanup(),         name="bg-sim-cleanup"),
        asyncio.create_task(_task_mcp_audit_cleanup(),   name="bg-mcp-audit-cleanup"),
        asyncio.create_task(_task_visitor_log_daily(),   name="bg-visitor-log"),
        asyncio.create_task(_task_sparkline_warm(state), name="bg-sparkline-warm"),
        asyncio.create_task(_task_ticker_watchdog(state), name="bg-ticker-watchdog"),
        asyncio.create_task(_task_holiday_refresh(),     name="bg-holiday-refresh"),
        asyncio.create_task(_task_hedge_proxy_regression(), name="bg-hedge-proxy-regression"),
        asyncio.create_task(_task_trail_stop(),          name="bg-trail-stop"),
        asyncio.create_task(_task_oco_pair_watcher(),    name="bg-oco-pair-watcher"),
        asyncio.create_task(_task_strategy_snapshot(),   name="bg-strategy-snapshot"),
        asyncio.create_task(_task_monthly_statement(),   name="bg-monthly-statement"),
        asyncio.create_task(_task_nav_compute(),         name="bg-nav-compute"),
        asyncio.create_task(_task_purge_persistence_caches(), name="bg-purge-persistence"),
        asyncio.create_task(_task_purge_audit_log(),           name="bg-purge-audit-log"),
        asyncio.create_task(_task_purge_visitor_log(),         name="bg-purge-visitor-log"),
        asyncio.create_task(_task_purge_impersonation_events(), name="bg-purge-impersonation-events"),
        asyncio.create_task(_task_purge_admin_email_events(),   name="bg-purge-admin-email-events"),
        asyncio.create_task(_task_market_lifecycle(),    name="bg-market-lifecycle"),
        asyncio.create_task(_task_funds_offhours(),      name="bg-funds-offhours"),
        asyncio.create_task(_task_closed_hours_refresh(), name="bg-closed-hours-refresh"),
        asyncio.create_task(_task_warm_backfill(),       name="bg-warm-backfill"),
        asyncio.create_task(_task_perf_snapshot(),       name="bg-perf-snapshot"),
        asyncio.create_task(_task_purge_perf_snapshots(), name="bg-purge-perf-snapshots"),
    ]
    # Mode 2 (real-data paper) runs on BOTH main and dev branches.
    # The PaperTradeEngine singleton processes its open-order book against
    # real Kite quotes every 5 s so paper orders follow a realistic chase
    # lifecycle (fill / modify / unfilled) without ever hitting Kite's
    # order endpoint.  The live agent engine is a separate concern gated
    # inside _task_performance by its own is_prod_branch check; paper mode
    # is independent of that gate and must be active wherever operators
    # can select PAPER (including dev.ramboq.com).
    from backend.api.algo.paper import get_prod_paper_engine
    paper_engine = get_prod_paper_engine()
    # Re-register OPEN paper orders from the DB so a service restart
    # doesn't leave in-flight chases stranded (their AlgoOrder rows
    # would stay OPEN forever otherwise).
    try:
        recovered = await paper_engine.recover_from_db()
        if recovered:
            logger.info(f"Background: paper engine recovered {recovered} "
                        "OPEN order(s) from previous run")
    except Exception as e:
        logger.warning(f"Background: paper engine recovery failed: {e}")
    app.state.bg_tasks.append(
        asyncio.create_task(paper_engine.tick_loop(interval_seconds=5),
                            name="bg-paper-chase")
    )
    logger.info("Background: all tasks started (market, performance, close, "
                "expiry, instruments, daily-snapshot, visitor-log, sparkline-warm, "
                "ticker-watchdog, paper-chase)")


async def on_shutdown(app) -> None:
    """Cancel all background tasks. Called by Litestar on shutdown."""
    tasks: list[asyncio.Task] = getattr(app.state, 'bg_tasks', [])
    for task in tasks:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    _executor.shutdown(wait=False)
    logger.info("Background: all tasks stopped")
