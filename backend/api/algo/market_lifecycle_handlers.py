"""
Snapshot handlers wired to MarketLifecycle events.

Three families of handlers, all registered at module-import time
(triggered by `register_default_handlers()` in startup):

  ``<exch>:close``           → fire ``snapshot_daily_book()`` so today's
                                positions / holdings / funds / trades rows
                                land in ``daily_book``.

  ``<exch>:close_settled``   → fire ``snapshot_daily_book()`` again
                                **45 min** after close. This overwrites
                                the previous snapshot rows (the
                                ``UPSERT ... ON CONFLICT DO UPDATE``
                                path in daily_snapshot._upsert_rows)
                                so the now-adjusted broker close_price
                                + last_price values land in DB. This is
                                what the close-override path in
                                positions.py reads as
                                "yesterday's EOD" the next session.

  ``nse:close`` (only)       → fire movers snapshot persist + NAV
                                compute. Both are NSE-anchored;
                                duplicating them on MCX would just
                                rewrite NSE-stale rows.

All handlers are async, swallow exceptions, and never raise back to the
dispatcher (the lifecycle module already logs failures, but isolation
here keeps a single broker hiccup from poisoning the audit trail).
"""

from __future__ import annotations

import asyncio
from typing import Optional

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Close → daily_book snapshot
# ---------------------------------------------------------------------------

async def _snapshot_close(exchange: str, event_type: str) -> None:
    """Persist today's positions / holdings / funds / trades rows for the
    just-closed exchange. Idempotent: ``snapshot_daily_book`` upserts on
    ``(date, account, kind, symbol)`` so a repeat call from
    ``:close_settled`` simply overwrites.

    We snapshot every account here — not just the ones for `exchange` —
    because per-account broker calls return rows across multiple
    exchanges and the upsert is keyed on ``(account, kind, symbol)``,
    not exchange. The 23:35 IST follow-up scheduled snapshot keeps
    running for MCX EOD parity; this hook is the event-driven path that
    fires the instant a session boundary is crossed.
    """
    try:
        from backend.api.algo.daily_snapshot import snapshot_daily_book
        result = await snapshot_daily_book()
        logger.info(
            f"market_lifecycle[{exchange}:{event_type}] daily_book snapshot — "
            f"accounts={result.get('accounts')} "
            f"h={result.get('holdings_rows')} "
            f"p={result.get('positions_rows')} "
            f"f={result.get('funds_rows', 0)} "
            f"t={result.get('trades_rows')}"
        )
    except Exception as e:
        logger.warning(
            f"market_lifecycle[{exchange}:{event_type}] "
            f"snapshot_daily_book failed: {e}"
        )


# ---------------------------------------------------------------------------
# Close → NAV snapshot
# ---------------------------------------------------------------------------

async def _snapshot_nav(exchange: str, event_type: str) -> None:
    """Write today's NAV row. NSE-only by intent — MCX close lands at
    23:30 IST and the existing daily 16:00 NAV cron already handles the
    post-equity-close NAV. We keep this hook so a holiday-shortened
    session still produces a NAV row immediately at the close moment
    rather than waiting hours for the 16:00 cron."""
    try:
        from backend.api.algo.nav import write_nav_snapshot
        snap = await write_nav_snapshot()
        try:
            navtot = float(snap.get("nav", 0)) if isinstance(snap, dict) else 0
        except Exception:
            navtot = 0
        logger.info(
            f"market_lifecycle[{exchange}:{event_type}] NAV snapshot ₹{navtot:,.0f}"
        )
    except Exception as e:
        logger.warning(
            f"market_lifecycle[{exchange}:{event_type}] write_nav_snapshot failed: {e}"
        )


# ---------------------------------------------------------------------------
# Close → movers snapshot
# ---------------------------------------------------------------------------

async def _snapshot_movers(exchange: str, event_type: str) -> None:
    """Persist the NSE movers snapshot at session close.

    The route handler (`GET /watchlist/movers`) relies on an in-memory
    dict (`_session_movers`) populated during the trading day.  A process
    restart wipes that dict and, if it happens late in the session or after
    close, leaves the movers universe empty for the rest of the day.

    This handler fires on ``nse:close`` (and is NOT wired to MCX/CDS —
    the movers universe is NSE-only; MCX underlyings are explicitly excluded
    at the quote-fetch stage).  It calls the route-level snapshot writer
    directly, bypassing the in-memory session state.  The writer
    (`_save_movers_snapshot`) uses ``pg_insert(...).on_conflict_do_update``
    so re-fires are idempotent.

    This guarantees a fresh DB row lands at the exact NSE session-close
    moment regardless of polling luck or process restarts.  The next
    `GET /watchlist/movers` call after 15:30 IST will read this row from
    `_load_latest_movers_snapshot()` instead of returning an empty list.
    """
    try:
        from backend.api.routes.watchlist import _force_movers_snapshot
        rows_written = await _force_movers_snapshot()
        logger.info(
            f"market_lifecycle[{exchange}:{event_type}] movers snapshot — "
            f"rows={rows_written}"
        )
    except Exception as e:
        logger.warning(
            f"market_lifecycle[{exchange}:{event_type}] "
            f"movers snapshot failed: {e}"
        )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_REGISTERED = False


def register_default_handlers() -> None:
    """Wire the shipped handlers into the singleton. Idempotent — safe to
    call from startup multiple times (the lifecycle's `register()` itself
    de-dupes by callable identity, but we also gate here to skip the
    log line on subsequent calls)."""
    global _REGISTERED
    if _REGISTERED:
        return
    from backend.api.algo.market_lifecycle import market_lifecycle

    # Per-exchange close + settled-close snapshots. The same handler
    # serves both events; snapshot_daily_book's UPSERT path makes the
    # second call overwrite the first.
    for exch in ("nse", "mcx", "cds"):
        market_lifecycle.register(f"{exch}:close",          _snapshot_close)
        market_lifecycle.register(f"{exch}:close_settled",  _snapshot_close)

    # NAV — fires only on the NSE close. MCX close happens at 23:30 IST
    # which is outside the LP-facing NAV cadence; the daily 16:00 cron
    # remains the authoritative late-session NAV path.
    market_lifecycle.register("nse:close", _snapshot_nav)

    # Movers snapshot — fires only on NSE close. The movers universe is
    # NSE-only (MCX underlyings excluded at quote-fetch time). This
    # guarantees a fresh DB row at the exact session-close moment so
    # off-hours requests serve real last-session data instead of [].
    market_lifecycle.register("nse:close", _snapshot_movers)

    _REGISTERED = True
    logger.info(
        "market_lifecycle: default handlers registered "
        "(nse/mcx/cds close + close_settled → daily_book; "
        "nse:close → NAV + movers)"
    )


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _reset_for_test() -> None:
    """Clear the idempotency latch so tests can re-register."""
    global _REGISTERED
    _REGISTERED = False
