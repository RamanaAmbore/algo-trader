"""Action resolver — `send_summary`: send a portfolio summary
(holdings/positions/margins) via the configured alert channels, per
market segment."""

from __future__ import annotations

import asyncio


async def _action_send_summary(context: dict, params: dict):
    """Send portfolio summary via existing send_summary."""
    from backend.shared.helpers.alert_utils import send_summary

    segments = params.get("segments", ["equity", "commodity"])
    summary_type = params.get("summary_type", "open")

    sum_holdings = context.get("sum_holdings")
    sum_positions = context.get("sum_positions")
    df_margins = context.get("df_margins")
    ist_display = context.get("ist_display", "")
    # 2026-10-31 fix (operator: "the alerts should show paper and
    # simulation mode explicitly ... wire it") — this action can fire
    # from inside a sim/replay cycle (unlike background.py's own
    # scheduled open/close summary, which is always a real market-hours
    # cron task and never passes these). Before this fix, send_summary()
    # had no way to know, so a sim/replay-fired summary carried no
    # distinguishing tag at all.
    sim_mode = bool(context.get("sim_mode"))
    replay_mode = bool(context.get("replay_mode"))

    if sum_holdings is None:
        return

    for seg_name in segments:
        label = seg_name.capitalize()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: send_summary(sum_holdings, sum_positions, ist_display,
                                 summary_type, label=label, df_margins=df_margins,
                                 sim_mode=sim_mode, replay_mode=replay_mode),
        )
