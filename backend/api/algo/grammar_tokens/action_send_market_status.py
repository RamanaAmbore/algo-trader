"""Action resolver — `send_market_status`: send the NSE/MCX market
open-or-closed status report (hours when open, holiday/weekend reason
when closed) via the configured alert channels — Telegram always (open
or closed), ntfy only when at least one segment is open today.

`_market_status_for_segment` / `_holiday_reason` /
`_format_market_status_message` are private helpers used exclusively by
`_action_live_send_market_status` — moved alongside it verbatim.
"""

from __future__ import annotations

import asyncio

# ── send_market_status (Item 3, 2026-10-22) ─────────────────────────────
#
# Daily 05:35 IST report of NSE/MCX open-or-closed status, hours when
# open, closure reason when closed. Reads the already-refreshed 05:30
# holiday calendar (`fetch_holidays` Tier 1-3 cache) — does NOT force a
# fresh NSE network fetch of its own. Deliberately bypasses the generic
# `_dispatch_agent()`/RICH_ALERT_AGENT channel fan-out (see
# `_action_live_send_market_status`'s own docstring) because the
# telegram-vs-ntfy routing is content-conditional (ntfy only fires when
# at least one segment is open today), which that generic mechanism
# can't express.


async def _market_status_for_segment(seg_key: str, now) -> dict:
    """Return ``{"open": bool, "hours": str|None, "reason": str|None}``
    for one `market_segments` entry (``"equity"`` | ``"commodity"``) for
    TODAY — a calendar-only determination ("is this segment scheduled to
    trade today"), not a clock-window "is it open RIGHT NOW" check. This
    report fires at 05:35 IST, before every session's own start, so the
    latter would always read closed.

    Mirrors `is_market_open()`'s own precedence (special session >
    holiday > weekday > session hours) but intentionally skips the live
    quote-probe layer `is_trading_day()` falls back to — this reads only
    the already-refreshed 05:30 holiday calendar, per the plan's "use the
    already-refreshed data, not a fresh recomputation" instruction.

    `reason` for a holiday closure reads `market_holidays.reason` when
    populated — in practice this column is not currently backfilled by
    the automated NSE-feed pipeline (`_fetch_holidays_from_nse` discards
    the feed's own holiday-name field), so it falls back to the generic
    "Holiday" string. Fixing that backfill is a separate, out-of-scope
    change.
    """
    from backend.shared.helpers.utils import config as _cfg
    from backend.brokers.broker_apis import fetch_holidays, fetch_special_sessions_async

    seg_cfg = (_cfg.get("market_segments", {}) or {}).get(seg_key, {}) or {}
    exch = (seg_cfg.get("holiday_exchange") or "NSE").upper()
    today = now.date()

    sessions = seg_cfg.get("sessions") or [{
        "start": seg_cfg.get("hours_start", "09:15"),
        "end": seg_cfg.get("hours_end", "15:30"),
    }]
    hours_str = f"{sessions[0]['start']}–{sessions[-1]['end']} IST"

    # ① Special-session override — highest precedence, same as is_market_open.
    # Awaited async — this coroutine runs from a main-loop background
    # tick (`background.py:_ds_tick_market_status_report`), not inside
    # asyncio.to_thread, so a sync `fetch_special_sessions()` call would
    # fail open to `[]` on a cold cache hit (same bug class as the
    # 2026-10-20 "4-site residual sweep").
    try:
        special = await fetch_special_sessions_async(exch)
    except Exception:
        special = []
    for row in (special or []):
        if row.get("date") == today:
            start, end = row.get("start"), row.get("end")
            if start == end:
                return {"open": False, "hours": None, "reason": "Special closure"}
            try:
                hrs = f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')} IST"
            except Exception:
                hrs = hours_str
            return {"open": True, "hours": hrs, "reason": None}

    # ② Weekend.
    if today.weekday() >= 5:
        return {"open": False, "hours": None, "reason": "Weekend"}

    # ③ Holiday calendar (already-refreshed cache — no fresh NSE fetch).
    try:
        holidays = fetch_holidays(exch)
    except Exception:
        holidays = set()
    if today in holidays:
        if bool(seg_cfg.get("evening_open_on_holidays", False)):
            # Matches is_market_open()'s own hardcoded 17:00 evening-open
            # threshold — absent an auto-inserted zero-width special
            # session (full-day closure), the segment still opens for its
            # evening leg.
            return {"open": True, "hours": f"17:00–{sessions[-1]['end']} IST", "reason": None}
        reason = await _holiday_reason(exch, today)
        return {"open": False, "hours": None, "reason": reason or "Holiday"}

    return {"open": True, "hours": hours_str, "reason": None}


async def _holiday_reason(exch: str, today) -> "str | None":
    """Best-effort read of `market_holidays.reason` for (exch, today).
    Returns None on any error or when the column is unset — see
    `_market_status_for_segment`'s docstring for why this is usually
    unset in practice today."""
    try:
        from backend.api.database import async_session
        from sqlalchemy import text as _sql_text
        async with async_session() as s:
            row = (await s.execute(
                _sql_text("SELECT reason FROM market_holidays WHERE exchange=:e AND date=:d"),
                {"e": exch, "d": today},
            )).first()
        return row[0] if row and row[0] else None
    except Exception:
        return None


def _format_market_status_message(now, equity: dict, commodity: dict) -> str:
    date_str = now.strftime("%Y-%m-%d")
    lines = [f"Market Status — {date_str}"]
    for label, st in (("NSE/Equity", equity), ("MCX/Commodity", commodity)):
        if st["open"]:
            lines.append(f"{label}: OPEN {st['hours']}")
        else:
            lines.append(f"{label}: CLOSED ({st['reason'] or 'Closed'})")
    return "\n".join(lines)


async def _action_live_send_market_status(context: dict, params: dict) -> None:
    """Send the daily 05:35 IST NSE/MCX market open-or-closed status
    report (Item 3, 2026-10-22).

    Telegram fires unconditionally (open or closed, full detail either
    way). ntfy fires only when at least one segment is open today —
    operator default for the "NSE and MCX disagree" edge case: ntfy
    fires if EITHER segment is open (so the operator doesn't miss a
    tradeable day); Telegram's message always covers both segments
    regardless.
    """
    from backend.shared.helpers.date_time_utils import timestamp_indian
    from backend.shared.helpers import alert_utils

    now = timestamp_indian()
    equity = await _market_status_for_segment("equity", now)
    commodity = await _market_status_for_segment("commodity", now)
    message = _format_market_status_message(now, equity, commodity)
    any_open = bool(equity["open"] or commodity["open"])

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None, lambda: alert_utils._send_telegram(message, parse_mode=None),
    )
    if any_open:
        await loop.run_in_executor(
            None,
            lambda: alert_utils.send_ntfy_alert("Market Status", message, priority="low"),
        )
