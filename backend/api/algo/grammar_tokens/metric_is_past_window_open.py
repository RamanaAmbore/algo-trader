"""Metric resolver — `is_past_window_open`: has today's trading session
begun (08:00 IST broker-connectivity window boundary)."""

from __future__ import annotations


def _metric_is_past_window_open(ctx, row):
    """1.0 from today's 08:00 IST broker-connectivity window boundary
    through the rest of the same trading session (including the 23:31-
    23:59 IST tail, before the closed overnight window actually starts
    at 23:31); 0.0 only during 00:00-07:59 IST, before today's own 08:00
    boundary has passed. This is a "has today's session begun" flag, not
    a live "is the window currently open" check — it does NOT flip back
    to 0.0 at 23:31 like `_segment_is_open()` would. Delegates to the
    same trading-session-date convention `expiry_freeze.session_date_of()`
    already uses (00:00-07:59 IST attributed to the PRIOR session) rather
    than re-deriving the boundary — a bare `now.time() >= 08:00` check
    would be wrong at e.g. 23:50 IST (still today's session, correctly
    "past window open", but also "past 08:00" only by coincidence of
    being later in the same calendar day — the session-date compare is
    the one that generalises correctly across midnight). None when
    ctx.now is unavailable."""
    now = ctx.now
    if now is None:
        return None
    from backend.api.algo.expiry_freeze import session_date_of
    return 1.0 if session_date_of(now) == now.date() else 0.0
