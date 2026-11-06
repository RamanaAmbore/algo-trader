"""Action resolver — `send_market_status`: send the NSE/MCX market
open-or-closed status report (hours when open, holiday/weekend reason
when closed) via the configured alert channels — Telegram and email
always (open or closed); no ntfy (2026-11-04 channel-routing audit —
this is routine market-info content, not action-required).

`_market_status_for_segment` / `_holiday_reason` /
`_format_market_status_message` are private helpers used exclusively by
`_action_live_send_market_status` — moved alongside it verbatim.
"""

from __future__ import annotations

# ── send_market_status (Item 3, 2026-10-22; log+agent conversion 2026-11-03) ─
#
# Daily 05:35 IST report of NSE/MCX open-or-closed status, hours when
# open, closure reason when closed. Reads the already-refreshed 05:30
# holiday calendar (`fetch_holidays` Tier 1-3 cache) — does NOT force a
# fresh NSE network fetch of its own. Logs through `_DISPATCH_LOGGER`
# and is rendered/dispatched by the dedicated `MARKET_STATUS_ALERT_AGENT`
# (`event_agents.py`) — it no longer sends Telegram/ntfy directly. The
# telegram-vs-ntfy routing stays content-conditional (ntfy only when at
# least one segment is open today), which `alert_routing.py`'s static
# per-agent channel resolution can't express — carried instead as a
# per-record `channels` list on the log record itself, read via the
# SAME generic per-record dynamic-channel mechanism `_render_breach`'s
# `out[4]` already uses (`_action_live_send_market_status`'s own
# docstring has the full chain).


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
            row_reason = row.get("reason")
            if start == end:
                return {"open": False, "hours": None, "reason": row_reason or "Special closure"}
            try:
                hrs = f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')} IST"
            except Exception:
                hrs = hours_str
            return {"open": True, "hours": hrs, "reason": row_reason}

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
            return {"open": True, "hours": f"17:00–{sessions[-1]['end']} IST", "reason": "Evening session (holiday)"}
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
            suffix = f" ({st['reason']})" if st["reason"] else ""
            lines.append(f"{label}: OPEN {st['hours']}{suffix}")
        else:
            lines.append(f"{label}: CLOSED ({st['reason'] or 'Closed'})")
    return "\n".join(lines)


async def _action_live_send_market_status(context: dict, params: dict) -> None:
    """Send the daily 05:35 IST NSE/MCX market open-or-closed status
    report (Item 3, 2026-10-22).

    Telegram and email fire unconditionally (open or closed, full detail
    either way). No ntfy — 2026-11-04 channel-routing audit (operator:
    "Email market info and summary... ntfy for more high priority
    alerts"): a daily routine status report is market-info content, not
    an interrupt-the-phone event, regardless of whether NSE/MCX agree on
    being open today. (The old ntfy condition fired if EITHER segment
    was open; this paragraph is kept only as history for why `any_open`
    briefly existed in this function.)

    2026-11-03 fix (operator: "audit all the alerts and make sure every
    one of them is generated by alert agent. The real functionality
    should add the messages to log with proper tags so that the alert
    agent pick them... notification, format, tags, names, message,
    everything is driven by tags, info levels, and messages in the
    logs") — this used to call `alert_utils._send_telegram`/
    `send_ntfy_alert` DIRECTLY, bypassing the log-record + event-agent
    pipeline every other alert in this codebase goes through
    (documented at the top of this module as a deliberate exception,
    now overridden by this instruction). `MARKET_STATUS_ALERT_AGENT`
    (`event_agents.py`) is the dedicated `kind='event'` agent that picks
    this up.

    2026-11-05 simplification (operator: "move market status onto the
    static events config now that it no longer needs content-
    conditional routing") — this record no longer carries its own
    `extra['channels']` override. That per-record mechanism existed only
    because the old telegram-vs-ntfy choice depended on whether a
    segment was open that day — content `alert_routing.py`'s static
    per-agent resolution couldn't see. The 2026-11-04 audit already
    removed that conditionality (both branches now send the identical
    fixed set below), so `MARKET_STATUS_ALERT_AGENT`'s own static
    `events` list is the SSOT for channels again — the normal path every
    other static agent uses, resolved by `_dispatch_agent()`. This also
    closes the bug the per-record path caused: its own "log" channel
    entry never carried a baked-in agent slug, which made the delivery
    echo get wrongly flagged as a fresh, unrouted alert-shaped record
    (see `event_agents.py:_send_channel`'s own fix for the general case).

    Logs through `_DISPATCH_LOGGER` (`backend.api.algo.events`), never
    this module's own `logging.getLogger(__name__)` — the exact logger-
    name mistake that silently dropped order-failure and summary alerts
    for ~19 days (see CLAUDE.md's "Alert message format" entry) is the
    one thing that must not be repeated here. Logged at INFO: a DEBUG
    record is structurally dropped before dispatch
    (`_dispatch_agent`'s own exemption), so INFO is required, not just
    conventional.
    """
    from backend.shared.helpers.date_time_utils import timestamp_indian
    from backend.shared.helpers.alert_utils import _DISPATCH_LOGGER

    now = timestamp_indian()
    equity = await _market_status_for_segment("equity", now)
    commodity = await _market_status_for_segment("commodity", now)
    message = _format_market_status_message(now, equity, commodity)

    # Channel selection (log, telegram, email — ntfy deliberately
    # excluded per the 2026-11-04 channel-routing audit comment above)
    # now lives entirely on `MARKET_STATUS_ALERT_AGENT.events`, not here.
    #
    # 2026-11-06 (operator SSOT instruction: "the source is process that
    # generated error... keeping the name as agent is not correct") — a
    # literal process name, not a fake-agent lookup: this daily 05:35
    # IST report is a genuinely un-caused scheduled job. Replaces the
    # prior `get_agent_id_by_slug("market-status-alert")` round-trip.
    _DISPATCH_LOGGER.info(
        "[MARKET-STATUS] %s", message.replace("\n", " | "),
        extra={
            "tags": ["agent", "alert"], "alert_event": "market_status",
            "title": "Market Status", "body": message,
            "source_type": "process", "source_name": "Market status report",
        },
    )
