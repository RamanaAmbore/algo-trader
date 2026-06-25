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
        name = parsed.get('root')
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
        #
        # Perf: offload to a thread because is_market_open → probe_market_active
        # → kite.quote() is a blocking HTTP call (requests/urllib3) inside the
        # asyncio main thread. py-spy caught a sample blocked in ssl.send.
        # Cache (60s TTL) usually short-circuits this, but on a cache miss the
        # loop stalls for ~100-200ms (longer on a Kite outage).
        def _probe_open(seg):
            return is_market_open(
                now, holiday_cache.get(seg['holiday_exchange'], set()),
                seg['hours_start'], seg['hours_end'],
                exchange=seg['holiday_exchange'],
            )
        # asyncio.gather of to_thread calls so each segment's probe runs in
        # parallel and the event loop stays responsive throughout.
        open_results = await asyncio.gather(*(
            asyncio.to_thread(_probe_open, seg) for seg in segments
        ))
        open_segments = [seg for seg, ok in zip(segments, open_results) if ok]

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
                    # Parallelize the token lookups — each _rts() is a cache
                    # hit (typically <5ms) but the await chain serializes
                    # them. With 50 symbols that was 50 coroutine context
                    # switches in series. asyncio.gather runs them
                    # concurrently and `subscribe_with_sym` is cheap
                    # in-memory so we can apply all subscriptions in one
                    # batch after the gather.
                    capped = _need_resolve[:50]
                    try:
                        toks = await asyncio.gather(
                            *(_rts(_s, _e) for _s, _e in capped),
                            return_exceptions=True,
                        )
                        _batch = [(_tok, _sym)
                                  for (_sym, _exch), _tok in zip(capped, toks)
                                  if _tok is not None
                                  and not isinstance(_tok, BaseException)]
                        if _batch:
                            _ticker.subscribe_with_sym(_batch)
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
            subject = (
                f"RamboQuant statement — {data.period_start.strftime('%b %Y')}"
            )
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
    # Audit trail — both success and failure write a row so the
    # operator can verify "did LP X get their May statement?"
    # without leaving /admin/audit.
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
    Capture a daily book snapshot once at startup (so a fresh deploy immediately
    has today's data) and then every day at 15:35 IST (5 min after equity close).
    """
    from backend.api.algo.daily_snapshot import snapshot_daily_book
    from backend.shared.helpers.date_time_utils import (
        timestamp_indian, is_market_open,
    )

    # Fire one snapshot immediately at startup so the table is populated
    # without waiting until 15:35 — but ONLY when both NSE and MCX are
    # closed. A mid-session deploy that captures live LTPs as "today's
    # snapshot" pollutes daily_book — the close-override in positions.py
    # reads the most recent row as the prior-session EOD, which then
    # collapses day_change_val to zero (observed on 2026-06-22 ~09:38
    # IST after a mid-session deploy: CRUDEOIL options' true EOD of
    # 220 got overridden to 264.5, the deploy-moment mid-session LTP,
    # making Day P&L read zero across every MCX position).
    #
    # Skipping the startup snapshot during market hours is safe: the
    # 15:35 IST scheduled run still fires, and any operator who needs
    # a fresh snapshot mid-session can trigger it via /admin/exec.
    from datetime import time as _dt_time
    _now_ist = timestamp_indian()
    _nse_open = is_market_open(_now_ist, set(),
                               _dt_time(9, 15), _dt_time(15, 30))
    _mcx_open = is_market_open(_now_ist, set(),
                               _dt_time(9, 0), _dt_time(23, 30))
    if _nse_open or _mcx_open:
        logger.info(
            f"Background: skipping startup daily snapshot — markets open "
            f"(NSE={_nse_open}, MCX={_mcx_open}). Scheduled 15:35 IST run still fires."
        )
    else:
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
            from backend.shared.brokers.registry import get_price_broker
            broker = get_price_broker()
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
                async with async_session() as s:
                    db_row = await s.get(HedgeProxy, row.id)
                    if db_row:
                        db_row.regression_at = datetime.now(timezone.utc)
                        db_row.regression_error = f"broker error: {str(exc)[:200]}"
                        await s.commit()
                logger.warning(
                    f"hedge-proxy regression: {row.proxy_symbol}→{row.target_root} failed: {exc}"
                )
                failed += 1
                continue
            if beta is None:
                # Resolution failure or not enough overlapping bars.
                # Stamp `regression_at` anyway so we don't retry the
                # same broken pair on every run — operator should
                # delete the row or fix the symbol.
                async with async_session() as s:
                    db_row = await s.get(HedgeProxy, row.id)
                    if db_row:
                        db_row.regression_at = datetime.now(timezone.utc)
                        db_row.regression_error = (
                            f"too few overlapping bars (n={n}, need ≥ 15)"
                        )
                        await s.commit()
                failed += 1
                continue
            try:
                async with async_session() as s:
                    db_row = await s.get(HedgeProxy, row.id)
                    if db_row:
                        db_row.beta = float(beta)
                        db_row.correlation = float(r2 if r2 is not None else 1.0)
                        db_row.target_sigma = float(sigma_t) if sigma_t is not None else None
                        db_row.proxy_sigma  = float(sigma_p) if sigma_p is not None else None
                        db_row.regression_at = datetime.now(timezone.utc)
                        # Successful run — clear any stale failure marker
                        db_row.regression_error = None
                        await s.commit()
                _st = f"{sigma_t:.3f}" if sigma_t is not None else "—"
                logger.info(
                    f"hedge-proxy regression: {row.proxy_symbol}→{row.target_root} "
                    f"β={beta:.4f} R²={r2:.3f} σ_t={_st} n={n}"
                )
                ran += 1
            except Exception as exc:
                logger.warning(f"hedge-proxy regression: write-back failed for {row.id}: {exc}")
                failed += 1
            # Pace per-row work to stay within Kite's 3 req/s historical
            # budget (each row burns 2 historical_data calls).
            await asyncio.sleep(1.0)

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
    from backend.shared.brokers.registry import get_broker
    from backend.shared.helpers.settings import get_int
    from sqlalchemy import select as _sel, update as _update
    import json as _json

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
            pending_updates: list[tuple[int, str]] = []
            # Phase 3D #5 — batch broker.ltp per account. Pass 1 collects
            # every (account, exchange:symbol) key referenced by a trailing
            # SL entry, deduped per account. Pass 2 issues one batched
            # broker.ltp([keys...]) per account. Pass 3 walks the rows
            # again and uses the pre-fetched LTP map. Prior version did
            # one ltp() call per entry — at 100 trailing rows that was
            # 100 round-trips every poll cycle.
            keys_by_account: dict[str, set[str]] = {}
            for row in rows:
                try:
                    attached = _json.loads(row.attached_gtts_json or "[]")
                except Exception:
                    continue
                if not isinstance(attached, list):
                    continue
                for entry in attached:
                    if not (isinstance(entry, dict)
                            and entry.get("kind") == "gtt"
                            and entry.get("sl_trail_pct") not in (None, "")):
                        continue
                    account = str(entry.get("parent_account") or "")
                    sym     = str(entry.get("parent_symbol") or "")
                    exch    = str(entry.get("parent_exchange") or "NFO")
                    if account and sym:
                        keys_by_account.setdefault(account, set()).add(f"{exch}:{sym}")
            # Parallelize broker.ltp across accounts. Pre-fix the for
            # loop awaited each account's ltp sequentially, costing
            # ~200ms × N accounts on the trail-stop hot path. Now all
            # accounts fan out via asyncio.gather; total wall-time =
            # max(per-account ltp) ≈ 200ms regardless of account count.
            ltp_map: dict[tuple[str, str], float] = {}
            _accts = list(keys_by_account.keys())
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
            results = await asyncio.gather(*(_ltp_for(a) for a in _accts))
            for account, resp in zip(_accts, results):
                if resp is None:
                    continue
                for k in keys_by_account[account]:
                    try:
                        ltp_v = float((resp.get(k) or {}).get("last_price") or 0)
                    except (TypeError, ValueError):
                        ltp_v = 0.0
                    if ltp_v > 0:
                        ltp_map[(account, k)] = ltp_v
            for row in rows:
                try:
                    attached = _json.loads(row.attached_gtts_json or "[]")
                except Exception:
                    continue
                if not isinstance(attached, list):
                    continue
                changed = False
                for entry in attached:
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("kind") != "gtt":
                        continue
                    if entry.get("sl_trail_pct") in (None, ""):
                        continue
                    gtt_id          = entry.get("id")
                    trail_pct       = float(entry["sl_trail_pct"])
                    current_trigger = float(entry.get("current_trigger") or 0)
                    parent_side     = str(entry.get("parent_side") or "")
                    parent_symbol   = str(entry.get("parent_symbol") or "")
                    parent_exchange = str(entry.get("parent_exchange") or "NFO")
                    account         = str(entry.get("parent_account") or "")
                    parent_qty      = int(entry.get("parent_qty") or 0)
                    parent_product  = str(entry.get("parent_product") or "NRML")
                    trigger_type    = str(entry.get("trigger_type") or "single")
                    if not (gtt_id and parent_symbol and account
                            and trail_pct > 0 and parent_qty > 0):
                        continue
                    try:
                        broker = get_broker(account)
                    except Exception:
                        continue
                    key = f"{parent_exchange}:{parent_symbol}"
                    ltp = ltp_map.get((account, key), 0.0)
                    if ltp <= 0:
                        continue
                    # Phase 3C #3 — persist watermark advances even when
                    # we DON'T issue a modify_gtt (no favorable move, or
                    # two-leg OCO deferred). Previously the in-memory
                    # update was discarded every poll cycle because
                    # `changed` was only set after a successful
                    # modify_gtt call. Compare against the persisted
                    # value before mutating so we know when there's a
                    # real diff to write back.
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
                    if high != prior_high or low != prior_low:
                        entry["highest_ltp"] = high
                        entry["lowest_ltp"]  = low
                        changed = True
                    if not more_favorable:
                        continue
                    proposed = round(proposed, 4)
                    # Build the modify_gtt kwargs. Single trigger has
                    # one value; two-leg OCO needs [tp, new_sl] so the
                    # untouched TP slot rides through unchanged.
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
                                f"[TRAIL] #{row.id} skipping legacy "
                                f"two-leg entry without tp_trigger — "
                                f"re-attach to enable trailing"
                            )
                            continue
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
                    try:
                        await asyncio.to_thread(
                            broker.modify_gtt,
                            gtt_id,
                            trigger_type=trigger_type,
                            tradingsymbol=parent_symbol,
                            exchange=parent_exchange,
                            last_price=ltp,
                            trigger_values=new_triggers,
                            orders=orders_payload,
                        )
                    except NotImplementedError:
                        # Broker has no modify_gtt — Dhan / Groww today.
                        # Drop the trail metadata so we stop retrying
                        # every interval for this row.
                        entry.pop("sl_trail_pct", None)
                        changed = True
                        continue
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
                            entry["partial_modify_error"] = (
                                f"ENTRY_LEG updated, TARGET_LEG rejected "
                                f"({str(e)[:120]})"
                            )
                            entry.pop("sl_trail_pct", None)
                            changed = True
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
                            continue
                        logger.debug(
                            f"[TRAIL] modify_gtt failed for #{row.id} "
                            f"gtt={gtt_id}: {e}"
                        )
                        continue
                    entry["current_trigger"] = proposed
                    changed = True
                    logger.info(
                        f"[TRAIL] #{row.id} {parent_side} {parent_symbol} "
                        f"trigger {current_trigger:.2f} → {proposed:.2f} "
                        f"(LTP {ltp:.2f}, trail {trail_pct}%)"
                    )
                if changed:
                    pending_updates.append((row.id, _json.dumps(attached)))
            # Batch flush — one session, N UPDATE statements, one commit.
            # Drops the per-row SELECT (we already have the id from the
            # outer query; the UPDATE is a no-op if the row was deleted
            # between the SELECT and now, which is acceptable).
            if pending_updates:
                async with async_session() as s2:
                    for _rid, _json_str in pending_updates:
                        await s2.execute(
                            _update(AlgoOrder)
                            .where(AlgoOrder.id == _rid)
                            .values(attached_gtts_json=_json_str)
                        )
                    await s2.commit()
        except Exception as e:
            logger.debug(f"[TRAIL] poll iteration failed: {e}")


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
    from backend.shared.brokers.registry import get_broker
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
            # Same batch-flush rationale as _task_trail_stop: collect
            # per-row updates into a list and flush in one session at
            # the end of the cycle. Pre-fix the inner s2 session opens
            # were N round-trips for N changed rows; OCO at 500 rows
            # behaves the same way as trail-stop under burst load.
            pending_oco_updates: list[tuple[int, str]] = []
            # Group by account so we hit broker.get_gtts() once per
            # account, not once per OCO entry.
            rows_by_account: dict[str, list] = {}
            attached_by_row: dict[int, list] = {}
            for row in rows:
                try:
                    attached = _json.loads(row.attached_gtts_json or "[]")
                except Exception:
                    continue
                if not isinstance(attached, list):
                    continue
                has_sibling = any(
                    isinstance(e, dict) and e.get("sibling_id")
                    for e in attached
                )
                if not has_sibling:
                    continue
                # Sibling-bearing entries always carry parent_account
                # (the persistence step stamps it alongside sibling_id).
                acct = None
                for e in attached:
                    if isinstance(e, dict) and e.get("sibling_id"):
                        acct = e.get("parent_account")
                        if acct:
                            break
                if not acct:
                    continue
                rows_by_account.setdefault(acct, []).append(row)
                attached_by_row[row.id] = attached
            if not rows_by_account:
                continue
            # One broker.get_gtts() per account, fired in PARALLEL —
            # pre-fix the for loop awaited each account's GTT fetch
            # sequentially, costing ~300ms × N accounts on every OCO
            # watcher tick. Now wall-time = max(per-account get_gtts).
            gtts_by_account: dict[str, dict[str, dict]] = {}
            _accts = list(rows_by_account.keys())
            async def _gtts_for(acct: str):
                try:
                    broker = get_broker(acct)
                except Exception:
                    return None
                try:
                    return await asyncio.to_thread(broker.get_gtts)
                except Exception as e:
                    logger.debug(f"[OCO-WATCH] get_gtts failed for {acct}: {e}")
                    return None
            results = await asyncio.gather(*(_gtts_for(a) for a in _accts))
            for acct, gtts in zip(_accts, results):
                if gtts is None:
                    continue
                gtts_by_account[acct] = {
                    str(g.get("id") or g.get("gtt_id")): g
                    for g in (gtts or [])
                    if isinstance(g, dict)
                }
            # Walk each row, decide if a sibling needs cancelling.
            for acct, acct_rows in rows_by_account.items():
                broker_gtts = gtts_by_account.get(acct, {})
                try:
                    broker = get_broker(acct)
                except Exception:
                    continue
                for row in acct_rows:
                    attached = attached_by_row.get(row.id) or []
                    changed = False
                    # Build a quick id→entry map so we can mark siblings
                    # as cancelled in the same JSON blob.
                    by_id: dict[str, dict] = {
                        str(e.get("id")): e
                        for e in attached
                        if isinstance(e, dict) and e.get("id")
                    }
                    for entry in attached:
                        if not (isinstance(entry, dict)
                                and entry.get("sibling_id")):
                            continue
                        my_id  = str(entry.get("id") or "")
                        sib_id = str(entry.get("sibling_id") or "")
                        if not (my_id and sib_id):
                            continue
                        # If MY id is no longer active on the broker
                        # (fired or already cancelled) AND my sibling
                        # IS still active → cancel the sibling.
                        my_active  = my_id in broker_gtts
                        sib_active = sib_id in broker_gtts
                        if my_active or not sib_active:
                            # Either we're still waiting (both active)
                            # or both already gone — nothing to do.
                            if not my_active and not sib_active:
                                # Both legs settled — clear sibling
                                # pointer so we stop polling this row.
                                # Audit fix (H-8) — when BOTH legs of
                                # an emulated OCO settle within one
                                # 15s poll window, the operator may
                                # have double-closed the position (TP
                                # fired AND SL fired before the pair
                                # watcher could cancel the sibling).
                                # Pre-fix this branch was silent. Now
                                # log at WARNING level + fire a
                                # Telegram alert so the operator can
                                # verify and manually close any over-
                                # exit. is_enabled('telegram') gates
                                # the alert per the platform's notify
                                # config.
                                logger.warning(
                                    f"[OCO-WATCH] row={row.id} both legs settled "
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
                                    logger.debug(
                                        f"[OCO-WATCH] double-fire alert failed: {_alert_err}"
                                    )
                                entry.pop("sibling_id", None)
                                changed = True
                            continue
                        # MY leg gone, sibling still alive → cancel it.
                        sib_entry = by_id.get(sib_id) or {}
                        sib_exchange = (
                            sib_entry.get("parent_exchange")
                            or entry.get("parent_exchange")
                            or "NFO"
                        )
                        try:
                            await asyncio.to_thread(
                                broker.cancel_gtt, sib_id, exchange=sib_exchange
                            )
                            logger.info(
                                f"[OCO-WATCH] row={row.id} cancelled survivor "
                                f"sibling={sib_id} (my id={my_id} fired)"
                            )
                            # Drop sibling pointer on both ends — pair
                            # is fully resolved.
                            entry.pop("sibling_id", None)
                            if sib_entry:
                                sib_entry.pop("sibling_id", None)
                            changed = True
                        except Exception as e:
                            logger.warning(
                                f"[OCO-WATCH] row={row.id} cancel sibling "
                                f"{sib_id} failed: {e}"
                            )
                    if changed:
                        pending_oco_updates.append((row.id, _json.dumps(attached)))
            # Batch flush — one session, N UPDATE statements, one commit.
            if pending_oco_updates:
                async with async_session() as s2:
                    for _rid, _json_str in pending_oco_updates:
                        await s2.execute(
                            _update(AlgoOrder)
                            .where(AlgoOrder.id == _rid)
                            .values(attached_gtts_json=_json_str)
                        )
                    await s2.commit()
        except Exception as e:
            logger.debug(f"[OCO-WATCH] poll iteration failed: {e}")


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

        # 2. + 3. Holdings + Positions — prefer the in-process cache
        # populated by _task_performance + the /api/holdings, /api/positions
        # endpoints. At the 09:00 / 09:15 segment-open boundaries the
        # performance cycle has typically run within the last 30 s, so
        # the cache hit avoids a duplicate broker fan-out. Falls through
        # to a direct broker_apis call only when the cache is cold (cold
        # startup, dev box that hasn't received any HTTP traffic yet).
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

        # Holdings (equity — NSE).
        try:
            cached_h = _cache_hit("holdings")
            if cached_h is not None and getattr(cached_h, "rows", None):
                for row in cached_h.rows:
                    sym  = str(getattr(row, "tradingsymbol", "") or "").upper().strip()
                    exch = str(getattr(row, "exchange", "") or "NSE").upper().strip()
                    if sym:
                        key = (sym, exch)
                        if key not in seen:
                            seen.add(key)
                            pairs.append(key)
            else:
                from backend.shared.helpers import broker_apis
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
            logger.warning(f"sparkline warm: holdings collect failed: {e}")

        # Positions (F&O / commodities).
        try:
            cached_p = _cache_hit("positions")
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
                from backend.shared.helpers import broker_apis
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

        # 4. Mover universe — indices + F&O largecap + NIFTY midcap +
        # smallcap. Without this the Winners/Losers tab on /pulse pays
        # a cold-cache hit every time a new symbol rotates into the
        # top movers (operator: "winners and losers sparklines are
        # slow to update"). The mover set is ~250 symbols static; we
        # warm them at boot + midnight rollover so they're hot before
        # the operator's first /pulse load. Symbols already added
        # above (positions/holdings/watchlist) are deduped, so the
        # operator's actual book never gets evicted by the mover top-up.
        try:
            from backend.shared.helpers.mover_universe import mover_warm_pairs
            for key in mover_warm_pairs():
                if key not in seen:
                    seen.add(key)
                    pairs.append(key)
        except Exception as e:
            logger.warning(f"sparkline warm: mover universe collect failed: {e}")

        # Hard cap: operator's book (positions + holdings + watchlist)
        # is never truncated. Mover universe fills remaining capacity
        # up to the 300-symbol ceiling. The comment above ("truncation
        # only ever drops mover universe symbols") was ONLY true when
        # the book was small — a large book (> 300 symbols) would
        # silently drop movers and potentially book symbols added late.
        # Fix: split at the book/mover boundary and cap movers separately.
        from backend.shared.helpers.mover_universe import mover_warm_pairs as _mwp
        _mover_set = set(_mwp())
        book_pairs  = [p for p in pairs if p not in _mover_set]
        mover_pairs = [p for p in pairs if p in _mover_set]
        cap         = 300
        remaining   = max(0, cap - len(book_pairs))
        return book_pairs + mover_pairs[:remaining]

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
    # fire-and-forget so subsequent task startup (scheduled-warm loop below)
    # is not blocked for the 50-90 s the warm cycle takes. The operator's
    # first request may still pay a cold-cache miss for the first ~30 s but
    # the rest of app startup completes without waiting.
    if not is_engine_idle():
        asyncio.create_task(_do_warm("startup"))
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


async def _task_ticker_watchdog(state: dict) -> None:
    CHECK_INTERVAL_S    = 30.0   # how often to poll ticker.status()
    FAILOVER_THRESHOLD_S = 60.0  # how long disconnected before we fail over
    FAILOVER_COOLOFF_S  = 300.0  # don't retry a failed account for 5 min
    ALERT_REFIRE_S      = 1800.0  # re-alert after 30 min of sustained degradation

    from backend.shared.helpers.kite_ticker import get_ticker
    from backend.shared.brokers.registry import get_historical_brokers
    from backend.shared.helpers.broker_apis import fetch_holidays

    # Per-watchdog holiday cache keyed by year so off-hours gating doesn't
    # hammer nseindia.com every 30 s. Refreshes naturally at year rollover.
    _wd_holiday_cache: dict = {}
    _wd_holiday_year: int | None = None

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

            # Market-hours gate — when no segment is open (overnight,
            # weekend, holiday) Kite drops the WebSocket as expected:
            # there are no ticks to deliver. The watchdog has no useful
            # work in that window. Alerting here was pure noise — the
            # operator got "TickerWatchdog — degraded" pings at 3 AM
            # because the WS legitimately closed at 23:30 IST after MCX
            # session end. Clear any active incident silently on entry
            # so the next session starts with a clean slate; we don't
            # ping a recovery because there was no real recovery — the
            # market just closed.
            now = timestamp_indian().replace(tzinfo=None)
            segments = _build_segments()
            if _wd_holiday_year != now.year:
                _wd_holiday_cache = {}
                _wd_holiday_year = now.year
            for seg in segments:
                exch = seg['holiday_exchange']
                if exch not in _wd_holiday_cache:
                    try:
                        _wd_holiday_cache[exch] = await asyncio.to_thread(
                            fetch_holidays, exch
                        )
                    except Exception:
                        _wd_holiday_cache[exch] = set()
            any_open = any(
                is_market_open(now, _wd_holiday_cache.get(seg['holiday_exchange'], set()),
                               seg['hours_start'], seg['hours_end'])
                for seg in segments
            )
            if not any_open:
                if _ticker_alert_state["alert_active"]:
                    _ticker_alert_state["alert_active"] = False
                    _ticker_alert_state["incident_start"] = 0.0
                    _ticker_alert_state["last_alerted_at"] = 0.0
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
                        await asyncio.to_thread(_send_telegram, msg)
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
                        await asyncio.to_thread(_send_telegram, msg)
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
            from backend.scripts.visitor_report import arun_daily, _summary_block
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


async def _task_purge_persistence_caches() -> None:
    """Daily 03:00 IST — purge stale rows from persistence-layer tables.

    ohlcv_daily:          rows older than 5 years (immutable; 5y covers all UI ranges).
    instruments_snapshot: rows older than 7 days (latest snapshot sufficient;
                          anything older can be re-fetched if needed — keeps table tiny).
    holidays_snapshot:    no purge (years are tiny + useful for backtest of any year).
    intraday_bars:        rows older than 90 days (intraday rarely queried beyond 3 months).
    """
    from sqlalchemy import text
    from backend.api.database import async_session

    async def _run_once():
        try:
            async with async_session() as session:
                ohlcv_res = await session.execute(text(
                    "DELETE FROM ohlcv_daily WHERE date < now() - interval '5 years'"
                ))
                instr_res = await session.execute(text(
                    "DELETE FROM instruments_snapshot WHERE date < now() - interval '7 days'"
                ))
                intraday_res = await session.execute(text(
                    "DELETE FROM intraday_bars WHERE date < now() - interval '90 days'"
                ))
                await session.commit()
                ohlcv_deleted    = ohlcv_res.rowcount    if ohlcv_res.rowcount    >= 0 else 0
                instr_deleted    = instr_res.rowcount    if instr_res.rowcount    >= 0 else 0
                intraday_deleted = intraday_res.rowcount if intraday_res.rowcount >= 0 else 0
            logger.info(
                f"Background: persistence cache purge complete — "
                f"ohlcv_daily: {ohlcv_deleted} row(s), "
                f"instruments_snapshot: {instr_deleted} row(s), "
                f"intraday_bars: {intraday_deleted} row(s)"
            )
        except Exception as exc:
            logger.warning(f"Background: persistence cache purge failed: {exc}")

    await asyncio.sleep(180)   # let startup settle

    while True:
        now = timestamp_indian()
        next_run = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if now >= next_run:
            next_run += timedelta(days=1)
        sleep_s = (next_run - now).total_seconds()
        logger.info(f"Background: persistence cache purge sleeping {sleep_s/3600:.1f}h until 03:00 IST")
        await asyncio.sleep(sleep_s)
        await _run_once()


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
        asyncio.create_task(_task_hedge_proxy_regression(), name="bg-hedge-proxy-regression"),
        asyncio.create_task(_task_trail_stop(),          name="bg-trail-stop"),
        asyncio.create_task(_task_oco_pair_watcher(),    name="bg-oco-pair-watcher"),
        asyncio.create_task(_task_strategy_snapshot(),   name="bg-strategy-snapshot"),
        asyncio.create_task(_task_monthly_statement(),   name="bg-monthly-statement"),
        asyncio.create_task(_task_nav_compute(),         name="bg-nav-compute"),
        asyncio.create_task(_task_purge_persistence_caches(), name="bg-purge-persistence"),
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
