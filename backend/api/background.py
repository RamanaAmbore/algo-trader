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

import pandas as pd

from backend.shared.helpers.date_time_utils import timestamp_indian, is_market_open, timestamp_display
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import config, get_nearest_time, get_cycle_date, mask_column

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
    from backend.shared.helpers import broker_apis
    df = pd.concat(broker_apis.fetch_margins(), ignore_index=True)
    total_row = df.select_dtypes(include='number').sum()
    total_row['account'] = 'TOTAL'
    return pd.concat([df, pd.DataFrame([total_row])], ignore_index=True)


def _fetch_holdings_direct() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (row_df, summary_df) with real account codes (see _fetch_margins_direct)."""
    from backend.shared.helpers import broker_apis
    raw = pd.concat(broker_apis.fetch_holdings(), ignore_index=True)

    sum_cols = [c for c in ['inv_val', 'cur_val', 'pnl', 'day_change_val'] if c in raw.columns]
    grouped = raw.groupby('account')[sum_cols].sum().reset_index()
    if 'pnl' in grouped and 'inv_val' in grouped:
        grouped['pnl_percentage']        = grouped['pnl'] / grouped['inv_val'] * 100
    if 'day_change_val' in grouped and 'cur_val' in grouped:
        grouped['day_change_percentage'] = grouped['day_change_val'] / grouped['cur_val'] * 100

    totals = grouped[sum_cols].sum().to_frame().T
    totals['account'] = 'TOTAL'
    if 'pnl' in totals and 'inv_val' in totals:
        totals['pnl_percentage']        = totals['pnl'] / totals['inv_val'] * 100
    if 'day_change_val' in totals and 'cur_val' in totals:
        totals['day_change_percentage'] = totals['day_change_val'] / totals['cur_val'] * 100

    summary = pd.concat([grouped, totals], ignore_index=True).fillna(0)
    return raw, summary


def _fetch_positions_direct() -> tuple[pd.DataFrame, pd.DataFrame]:
    from backend.shared.helpers import broker_apis
    raw = pd.concat(broker_apis.fetch_positions(), ignore_index=True)
    grouped = raw.groupby('account')[['pnl']].sum().reset_index() if 'pnl' in raw.columns \
              else pd.DataFrame(columns=['account', 'pnl'])
    total   = pd.DataFrame([{'account': 'TOTAL', 'pnl': grouped['pnl'].sum()}])
    summary = pd.concat([grouped, total], ignore_index=True)
    return raw, summary


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
    from backend.api.algo.derivatives import (
        parse_tradingsymbol, option_underlying_quote_key,
    )
    underlyings: dict[str, str] = {}   # name → kite_key
    for sym in df_positions['tradingsymbol'].dropna().astype(str).unique():
        parsed = parse_tradingsymbol(sym)
        if not parsed:
            continue
        name = parsed.get('underlying')
        ltp_key = option_underlying_quote_key(sym)
        if name and ltp_key:
            underlyings.setdefault(name, ltp_key)
    if not underlyings:
        return {}
    try:
        from backend.shared.brokers.registry import get_price_broker
        broker = get_price_broker()
        resp = broker.ltp(list(underlyings.values())) or {}
    except Exception as e:
        logger.debug(f"_resolve_spot_prices: broker.ltp failed: {e}")
        return {}
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


async def _task_performance(state: dict) -> None:
    """Refresh performance data every N minutes during market hours."""
    from backend.shared.helpers.broker_apis import fetch_holidays
    from backend.shared.helpers.alert_utils import send_summary
    from backend.shared.helpers.summarise import summarise_holdings as _summarise_holdings, summarise_positions as _summarise_positions
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

    while True:
        # Re-read each iteration so a /admin/settings tweak lands on the
        # next cycle instead of after a service restart.
        interval    = _interval()
        open_offset = _open_offset()
        await asyncio.sleep(interval * 60)

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

        for seg in segments:
            exch = seg['holiday_exchange']
            if exch not in holiday_cache:
                try:
                    holiday_cache[exch] = await _run(fetch_holidays, exch)
                except Exception as e:
                    logger.debug(f"Background: holiday load skipped for {exch}: {e}")
                    holiday_cache[exch] = set()

        # Pass `exchange=` to is_market_open so the live-quote probe
        # can override the calendar verdict. Catches MCX evening
        # sessions on equity holidays (NSE's COM segment lists those
        # as closed but MCX is actually trading) and Muhurat days
        # the calendar doesn't list.
        open_segments = [
            seg for seg in segments
            if is_market_open(now, holiday_cache.get(seg['holiday_exchange'], set()),
                              seg['hours_start'], seg['hours_end'],
                              exchange=seg['holiday_exchange'])
        ]

        if not open_segments:
            continue

        # If the simulator is running we still want the performance cache
        # (and the /performance page that reads from it) to stay fresh with
        # real Kite data — only the live run_cycle is gated off, so the
        # fabricated sim state doesn't race with real agent fires on the
        # same alert_state dict.
        sim_active = False
        try:
            from backend.api.algo.sim.driver import get_driver
            sim_active = bool(get_driver().active)
        except Exception:
            pass

        try:
            # Serial fetches by design: parallelising them raced the daily
            # Kite token-refresh. Three concurrent broker calls would each
            # kick off their own `login()` + 2FA at the 23h boundary, and
            # Kite invalidates tokens issued in parallel for the same app
            # — same failure mode the "1 uvicorn worker" rule in CLAUDE.md
            # prevents. The ~300 ms shaved per cycle wasn't worth locking
            # the website around the refresh window.
            (df_holdings, sum_holdings) = await _run(_fetch_holdings_direct)
            (df_positions, sum_positions) = await _run(_fetch_positions_direct)
            df_margins = await _run(_fetch_margins_direct)
            ist_display = timestamp_display()
            perf_key    = get_nearest_time(interval=interval)

            # Full portfolio summaries (no segment filtering)
            all_sum_h = _summarise_holdings(df_holdings, sum_holdings, None)
            all_sum_p = _summarise_positions(df_positions)

            # Intraday equity-curve — one point per tick during market hours.
            # Skipped while the simulator is active so fabricated P&L never
            # pollutes the real intraday history.
            if not sim_active:
                global _intraday_equity, _intraday_equity_date
                try:
                    # Date rollover: wipe the buffer at the start of a new
                    # IST trading day so the chart always reflects today only.
                    if _intraday_equity_date != today:
                        _intraday_equity.clear()
                        _intraday_equity_date = today

                    h_total = all_sum_h.loc[all_sum_h['account'] == 'TOTAL']
                    p_total = all_sum_p.loc[all_sum_p['account'] == 'TOTAL']

                    h_day  = float(h_total['day_change_val'].iloc[0]) if (
                        not h_total.empty and 'day_change_val' in h_total.columns
                        and pd.notna(h_total['day_change_val'].iloc[0])
                    ) else 0.0
                    h_pnl  = float(h_total['pnl'].iloc[0]) if (
                        not h_total.empty and 'pnl' in h_total.columns
                        and pd.notna(h_total['pnl'].iloc[0])
                    ) else 0.0
                    p_pnl  = float(p_total['pnl'].iloc[0]) if (
                        not p_total.empty and 'pnl' in p_total.columns
                        and pd.notna(p_total['pnl'].iloc[0])
                    ) else 0.0
                    # Positions today's-delta — falls back to p_pnl when
                    # day_change_val isn't populated (pure-MIS book where
                    # the lifetime and today's deltas are the same number).
                    p_day  = float(p_total['day_change_val'].iloc[0]) if (
                        not p_total.empty and 'day_change_val' in p_total.columns
                        and pd.notna(p_total['day_change_val'].iloc[0])
                    ) else p_pnl

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

            # Loss alerts are now entirely owned by the v2 agent engine below
            # (loss-* BUILTIN_AGENTS). alert_utils.check_and_alert is retired.

            # Run agent engine with market data context — but skip entirely
            # while the simulator is active. The sim driver owns run_cycle
            # while it's running; mixing a live fire with a fabricated one
            # would corrupt rate history and spam the Telegram group.
            from backend.shared.helpers.utils import is_prod_branch
            if sim_active:
                logger.info("Background: simulator active — skipping real run_cycle (performance cache still fresh)")
            elif not is_prod_branch():
                # Mode 1 (dev) — the live agent engine only runs on main.
                # Dev's agent testing happens through the simulator, which
                # owns its own run_cycle invocation. Keeping the live
                # engine off dev avoids cross-process Kite contention with
                # prod AND makes "paper trade without fill simulation"
                # impossible by construction.
                pass
            else:
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

            # Phase 2 — seed KiteTicker with any newly-discovered symbols
            # from the live positions + holdings book. The sparkline warm
            # task handles watchlist symbols; this covers the trading book
            # (F&O positions, held equities) which changes intraday.
            # subscribe() is idempotent — re-subscribing known tokens is a
            # no-op. We never unsubscribe stale symbols (Phase 2 simplicity).
            try:
                from backend.shared.helpers.kite_ticker import get_ticker as _get_ticker
                from backend.api.routes.quote import _resolve_token_for_sym as _rts
                _ticker = _get_ticker()
                # Collect tradingsymbol+exchange pairs from both DataFrames.
                _book_pairs: list[tuple[str, str]] = []
                for _df, _default_exch in (
                    (df_holdings, "NSE"),
                    (df_positions, "NFO"),
                ):
                    if _df is not None and not _df.empty:
                        for _, _row in _df.iterrows():
                            _sym  = str(_row.get("tradingsymbol") or "").strip().upper()
                            _exch = str(_row.get("exchange") or _default_exch).strip().upper()
                            if _sym:
                                _book_pairs.append((_sym, _exch))
                # Resolve tokens for symbols not yet in the ticker.
                # Batch into one exchange→instruments call per unique exchange.
                _need_resolve = [
                    (sym, exch) for sym, exch in _book_pairs
                    # O(1) check via has_sym() — avoids rebuilding the full
                    # {v for v in _token_to_sym.values()} set on every cycle.
                    if not _ticker.has_sym(sym)
                ]
                if _need_resolve:
                    for _sym, _exch in _need_resolve[:50]:  # cap 50 per cycle
                        try:
                            _tok = await _rts(_sym, _exch)
                            if _tok is not None:
                                _ticker.subscribe_with_sym([(_tok, _sym)])
                        except Exception:
                            pass
            except Exception as _tke:
                logger.debug(f"Background: ticker book-subscribe skipped: {_tke}")

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
    from backend.shared.helpers.broker_apis import fetch_holidays
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
    holiday_cache: dict = {}

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
            exch = seg['holiday_exchange']
            if exch not in holiday_cache:
                try:
                    holiday_cache[exch] = await _run(fetch_holidays, exch)
                except Exception:
                    holiday_cache[exch] = set()

        for seg in segments:
            ss = seg_state[seg['name']]
            if ss['last_close'] == today:
                continue

            h_set = holiday_cache.get(seg['holiday_exchange'], set())
            close_trigger = now.replace(
                hour=seg['hours_end'].hour,
                minute=seg['hours_end'].minute,
                second=0, microsecond=0
            ) + timedelta(minutes=close_offset)

            if today not in h_set and now.weekday() < 5 and now >= close_trigger:
                try:
                    (df_h, sum_h), (df_p, sum_p) = await _run(
                        lambda: (_fetch_holdings_direct(), _fetch_positions_direct()))
                    df_margins  = await _run(_fetch_margins_direct)
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
        try:
            hh, mm = cfg_time.split(":", 1)
            hh, mm = int(hh), int(mm)
            if not (0 <= hh < 24 and 0 <= mm < 60):
                raise ValueError("out of range")
        except Exception:
            logger.warning(f"Background: algo.expiry_check_time={cfg_time!r} invalid, defaulting to 09:20")
            hh, mm = 9, 20
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


async def _task_daily_snapshot() -> None:
    """
    Capture a daily book snapshot once at startup (so a fresh deploy immediately
    has today's data) and then every day at 15:35 IST (5 min after equity close).
    """
    from backend.api.algo.daily_snapshot import snapshot_daily_book

    # Fire one snapshot immediately at startup so the table is populated
    # without waiting until 15:35.
    try:
        result = await snapshot_daily_book()
        logger.info(
            f"Background: startup daily snapshot — "
            f"accounts={result['accounts']} "
            f"h={result['holdings_rows']} p={result['positions_rows']} t={result['trades_rows']} "
            f"errors={result['errors']}"
        )
    except Exception as e:
        logger.error(f"Background: startup daily snapshot failed: {e}")

    while True:
        now = timestamp_indian()
        # Schedule for 15:35 IST (5 min after NSE equity close at 15:30)
        snap_time = now.replace(hour=15, minute=35, second=0, microsecond=0)
        if now >= snap_time:
            snap_time += timedelta(days=1)
        sleep_s = (snap_time - now).total_seconds()
        logger.info(f"Background: daily snapshot sleeping {sleep_s/3600:.1f}h until 15:35 IST")
        await asyncio.sleep(sleep_s)

        try:
            result = await snapshot_daily_book()
            logger.info(
                f"Background: daily snapshot complete — "
                f"accounts={result['accounts']} "
                f"h={result['holdings_rows']} p={result['positions_rows']} t={result['trades_rows']} "
                f"errors={result['errors']}"
            )
        except Exception as e:
            logger.error(f"Background: daily snapshot failed: {e}")


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
    from backend.api.models import McpAudit
    from backend.shared.helpers.settings import get_int
    from sqlalchemy import delete as sql_delete

    async def _purge_once():
        days = get_int("mcp.audit_retention_days", 90)
        if days <= 0:
            logger.info("Background: mcp_audit cleanup disabled (retention_days=0)")
            return
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        try:
            async with async_session() as s:
                res = await s.execute(
                    sql_delete(McpAudit).where(McpAudit.created_at < cutoff)
                )
                await s.commit()
                logger.info(
                    f"Background: mcp_audit cleanup purged "
                    f"{res.rowcount or 0} rows older than {days} days"
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


async def _task_sparkline_warm(state: dict) -> None:
    """
    Pre-populate the sparkline past-close cache at startup and at each
    market segment open so the operator's first Pulse load is free of
    historical_data calls.

    Symbol universe (capped at 100, deduped):
      1. All distinct tradingsymbols in watchlist_items (DB query).
      2. Live holdings tradingsymbols (one broker fetch; equity symbols
         for which sparklines are most commonly shown).
      3. Live positions tradingsymbols (F&O + commodities in the open book).

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

        # 1. Watchlist items. Bare MCX commodity names ("CRUDEOIL",
        #    "GOLDM", etc.) and CDS currency names ("USDINR") aren't real
        #    Kite instruments — the tradable contract is the near-month
        #    future (CRUDEOIL26JUNFUT). Without this resolution the
        #    sparkline-warm task tries to look up the bare name in the
        #    instruments map, fails, and never subscribes the ticker —
        #    operator sees pinned LTPs stuck at the 30 s REST-poll cadence
        #    instead of the sub-second SSE stream. Resolve to the
        #    front-month contract before adding to the warm set.
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
                # Bare-commodity heuristic: MCX/CDS + all-alpha + <= 12 chars
                # mirrors `_build_quote_key` in watchlist.py. Real futures
                # carry digits in their tradingsymbol (CRUDEOIL26JUNFUT)
                # and pass through untouched.
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

        # 2. Holdings (equity — NSE).
        try:
            from backend.shared.helpers import broker_apis
            dfs = broker_apis.fetch_holdings()
            import pandas as pd
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
            logger.warning(f"sparkline warm: holdings fetch failed: {e}")

        # 3. Positions (F&O / commodities).
        try:
            from backend.shared.helpers import broker_apis
            dfs = broker_apis.fetch_positions()
            import pandas as pd
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
            logger.warning(f"sparkline warm: positions fetch failed: {e}")

        return pairs[:100]  # hard cap

    async def _do_warm(label: str) -> None:
        logger.info(f"sparkline warm: starting ({label})")
        try:
            symbols = await _collect_symbols()
            count = await warm_sparkline_cache(symbols, days=5)
            logger.info(f"sparkline warm: {label} complete — {count} symbols cached")
        except Exception as e:
            logger.error(f"sparkline warm: {label} failed: {e}")

    # Dev-idle gate (see _task_performance) — skip the startup warm on
    # dev when engine is idle so dev doesn't burn historical_data calls
    # before any operator has picked a mode.
    from backend.shared.helpers.utils import is_engine_idle

    # ── Fire immediately at startup ──────────────────────────────────────────
    if not is_engine_idle():
        await _do_warm("startup")
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
    # All three populate _spark_past_cache for the day's IST date. Cache
    # entries with stale dates are evicted lazily on every batch_sparkline
    # call (`_evict_stale`).
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


async def _task_ticker_watchdog(state: dict) -> None:
    CHECK_INTERVAL_S    = 30.0   # how often to poll ticker.status()
    FAILOVER_THRESHOLD_S = 60.0  # how long disconnected before we fail over
    FAILOVER_COOLOFF_S  = 300.0  # don't retry a failed account for 5 min
    ALERT_REFIRE_S      = 1800.0  # re-alert after 30 min of sustained degradation

    from backend.shared.helpers.kite_ticker import get_ticker
    from backend.shared.brokers.registry import get_historical_brokers

    while True:
        try:
            await asyncio.sleep(CHECK_INTERVAL_S)
            # Dev-idle gate — skip ticker watchdog on dev when engine is
            # idle. The ticker isn't started either (per app.py gate) so
            # there's nothing to watch; this short-circuit prevents the
            # alert state machine from misfiring when the operator
            # toggles dev_active off mid-session.
            from backend.shared.helpers.utils import is_engine_idle
            if is_engine_idle():
                continue
            ticker = get_ticker()
            status = ticker.status()
            # Watchdog applies only to a ticker that's STARTED but has
            # been disconnected longer than the threshold. A ticker that
            # never started is the warm task's job to recover.
            if not status.get("started"):
                continue
            if status.get("connected"):
                # Ticker is healthy — clear any outstanding degraded incident.
                if _ticker_alert_state["alert_active"]:
                    from backend.shared.helpers.alert_utils import _send_telegram
                    from backend.shared.helpers.utils import is_enabled
                    _ticker_alert_state["alert_active"] = False
                    now_ts = _time.time()
                    duration_min = int(
                        (now_ts - _ticker_alert_state["incident_start"]) / 60
                    )
                    branch = config.get("deploy_branch", "main")
                    branch_tag = f" [{branch}]" if branch != "main" else ""
                    ts = timestamp_display()
                    connected_acct = status.get("account", ticker.current_account() or "?")
                    msg = (
                        f"TickerWatchdog{branch_tag} — recovered\n"
                        f"Ticker connected on {connected_acct}\n"
                        f"Duration of incident: {duration_min} min\n"
                        f"Time: {ts}"
                    )
                    logger.info(
                        f"ticker watchdog: recovered on {connected_acct} "
                        f"after {duration_min} min"
                    )
                    if is_enabled("telegram"):
                        _send_telegram(msg)
                continue  # healthy
            if ticker.seconds_since_disconnect() < FAILOVER_THRESHOLD_S:
                continue  # within KiteTicker's own retry window

            # Disconnected for too long — find the next eligible account.
            # get_historical_brokers() honours the historical_data_enabled
            # flag in settings and the 30s rate-limit cool-off — exactly
            # the gating the operator wanted ("if it is enabled for
            # historical data in settings, we are good").
            try:
                eligible = get_historical_brokers()
            except Exception as e:
                logger.warning(f"ticker watchdog: eligible-broker lookup failed: {e}")
                continue
            current = ticker.current_account()
            next_kc = None
            for b in eligible:
                acct = getattr(b, "account", "") or ""
                if not acct or acct == current:
                    continue
                if ticker.is_account_in_failover_cooloff(acct, FAILOVER_COOLOFF_S):
                    continue
                # Extract live api_key + access_token from the broker's
                # underlying KiteConnection (same pattern app.py uses).
                kc = getattr(b, "_conn", None) or getattr(b, "kite", None)
                api_key = getattr(kc, "api_key", None)
                access_token = (
                    getattr(kc, "_access_token", None)
                    or getattr(kc, "access_token", None)
                )
                if api_key and access_token:
                    next_kc = (acct, api_key, access_token)
                    break

            if not next_kc:
                # All accounts are in failover cool-off (or unavailable).
                # Alert once on entry, then re-fire every 30 min while degraded.
                from backend.shared.helpers.alert_utils import _send_telegram
                from backend.shared.helpers.utils import is_enabled
                now_ts = _time.time()
                should_alert = (
                    not _ticker_alert_state["alert_active"]
                    or (now_ts - _ticker_alert_state["last_alerted_at"]) > ALERT_REFIRE_S
                )
                if should_alert:
                    if not _ticker_alert_state["alert_active"]:
                        # First entry into the incident.
                        _ticker_alert_state["alert_active"] = True
                        _ticker_alert_state["incident_start"] = now_ts
                    _ticker_alert_state["last_alerted_at"] = now_ts

                    branch = config.get("deploy_branch", "main")
                    branch_tag = f" [{branch}]" if branch != "main" else ""
                    ts = timestamp_display()
                    disconnected_s = ticker.seconds_since_disconnect()
                    acct_list = ", ".join(
                        b_acct for b in eligible
                        if (b_acct := getattr(b, "account", "") or "")
                    ) or "?"
                    is_refire = _ticker_alert_state["last_alerted_at"] != _ticker_alert_state["incident_start"]
                    refire_note = " (re-alert)" if is_refire else ""
                    msg = (
                        f"TickerWatchdog{branch_tag} — degraded{refire_note}\n"
                        f"Both Kite accounts in failover cool-off.\n"
                        f"Disconnect: {current or '?'} → {acct_list} (all blocked)\n"
                        f"Sparkline degrading to broker.ltp() polling.\n"
                        f"Disconnected for: {disconnected_s:.0f}s\n"
                        f"Time: {ts}"
                    )
                    logger.warning(
                        f"ticker watchdog: no eligible failover account "
                        f"(current={current or '?'}, disconnected_s={disconnected_s:.0f}) — "
                        f"continuing to wait for primary to recover"
                    )
                    if is_enabled("telegram"):
                        _send_telegram(msg)
                continue

            acct, api_key, access_token = next_kc
            ok = ticker.restart_with_account(api_key, access_token, acct)
            if ok:
                logger.info(f"ticker watchdog: failover OK → {acct}")
            else:
                logger.warning(f"ticker watchdog: failover to {acct} did not start")

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

    async def _run_once() -> None:
        try:
            # Offload the synchronous file parsing + MaxMind lookups to
            # the thread executor so the asyncio loop stays responsive.
            from backend.scripts.visitor_report import run_daily, _summary_block
            report_path = await _run(run_daily)
            logger.info(f"Background: visitor log report → {report_path}")

            branch = config.get("deploy_branch", "main")
            branch_tag = f" [{branch}]" if branch != "main" else ""

            if is_enabled("telegram"):
                try:
                    from backend.shared.helpers.alert_utils import _send_telegram
                    from backend.scripts.visitor_report import summary_for_telegram
                    tg_body = summary_for_telegram(report_path)
                    msg = f"<b>Visitors{branch_tag}</b>\n{tg_body}"
                    _send_telegram(msg)
                except Exception as tg_err:
                    logger.warning(f"Background: visitor log telegram failed: {tg_err}")

            if is_enabled("mail"):
                try:
                    from backend.shared.helpers.mail_utils import send_email
                    from backend.shared.helpers.alert_utils import get_alert_recipients
                    from backend.scripts.visitor_report import summary_for_email
                    recipients = get_alert_recipients()
                    if recipients:
                        html_body = summary_for_email(report_path)
                        subject = f"RamboQuant Visitors{branch_tag}"
                        for email in recipients:
                            try:
                                send_email(email, email, subject, html_body)
                            except Exception as mail_err:
                                logger.warning(
                                    f"Background: visitor log mail to {email} failed: {mail_err}"
                                )
                except Exception as mail_err:
                    logger.warning(f"Background: visitor log mail failed: {mail_err}")
        except Exception as e:
            logger.error(f"Background: visitor log daily run failed: {e}")

    # First run immediately (startup catch-up — logs from the previous day
    # may not have been processed if the service was restarted mid-day).
    await _run_once()

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


async def on_startup(app) -> None:
    """Start all background tasks. Called by Litestar on startup."""
    state: dict = {}
    # Restore the sparkline cache from disk BEFORE any request handler
    # can fire. Persisted entries are validated against today's IST
    # date inside the loader — stale-day entries are ignored and the
    # startup warm task (queued below) refills them in ~30 s. When the
    # disk file matches today's date, /api/quotes/sparkline serves from
    # the restored cache immediately and the startup warm finds every
    # symbol already cached.
    try:
        from backend.api.routes.quote import load_sparkline_cache_from_disk
        load_sparkline_cache_from_disk()
    except Exception as e:
        logger.warning(f"sparkline cache load skipped: {e}")
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
    ]
    # Mode 2 (real-data paper) runs only on main. The PaperTradeEngine
    # singleton processes its open-order book against real Kite quotes
    # every 5 s, so agent-fired paper orders follow a realistic chase
    # lifecycle (fill / modify / unfilled) without ever hitting Kite's
    # order endpoint. Dev never runs this because dev never runs the
    # live agent engine (see _task_performance's is_prod_branch gate).
    from backend.shared.helpers.utils import is_prod_branch
    if is_prod_branch():
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
    else:
        logger.info("Background: all tasks started (market, performance, close, "
                    "expiry, instruments, daily-snapshot, visitor-log, sparkline-warm, "
                    "ticker-watchdog) "
                    "— live agent engine + paper engine OFF on non-main")


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
