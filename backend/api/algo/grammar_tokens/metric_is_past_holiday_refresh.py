"""Metric resolver — `is_past_holiday_refresh`: 1.0 once today's
`holiday_refresh_time` clock boundary has passed."""

from __future__ import annotations


def _metric_is_past_holiday_refresh(ctx, row):
    """1.0 once today's `holiday_refresh_time` (backend_config.yaml,
    default 05:30 IST) clock boundary has passed; 0.0 before it. Face-
    value clock check only — does not confirm the refresh job actually
    ran or succeeded. None when ctx.now is unavailable."""
    now = ctx.now
    if now is None:
        return None
    from backend.shared.helpers.utils import config as _cfg
    target_str = str(_cfg.get("holiday_refresh_time", "05:30") or "05:30")
    try:
        hh, mm = (int(x) for x in target_str.split(":", 1))
    except Exception:
        hh, mm = 5, 30
    target_t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return 1.0 if now >= target_t else 0.0
