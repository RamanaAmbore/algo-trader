"""Notify channel resolver — `log`: write to the app log only — useful
for silent testing of new agents."""

from __future__ import annotations

from backend.shared.helpers.ramboq_logger import get_logger

# Logger name deliberately pinned to "backend.api.algo.events" (NOT this
# module's own __name__) — same singleton-by-name reasoning
# `event_agents.py`'s own `_EVENTS_LOGGER` module comment documents: this
# channel's send function is a direct port of events.py's old
# _log_channel, and every LogEvent row it writes must keep showing up
# under that same logger name so stored rows look identical to what they
# did before. Calling `get_logger()` directly here (rather than
# importing `_EVENTS_LOGGER` from `event_agents.py`) also avoids a
# circular import: `event_agents.py`'s `CHANNELS` dict imports
# `_send_log_channel` back FROM this module at its own top level.
_EVENTS_LOGGER = get_logger("backend.api.algo.events")


def _send_log_channel(title: str, body: str, tg: "str | None" = None, *,
                      log_line: "str | None" = None, agent_slug: "str | None" = None,
                      sim_mode: bool = False, **_kw) -> None:
    """Plain log write — the 'log' channel's entire delivery IS this
    logger.warning call. No broadcast_fn, no external service — fully
    self-contained, same as before this channel moved off
    events.py's old direct-dispatch `_log_channel`.

    `log_line`/`agent_slug`/`sim_mode` are pre-built by
    events.py:_classify_recorded_channel onto this channel's own record
    (the generic `out` render tuple only carries the SHARED telegram/ntfy/
    email bodies, not a per-channel one) and threaded through by
    `_send_channel`'s "log" branch below. Falls back to `title — body`
    when called directly without those (e.g. a future renderer that
    doesn't pre-build a log line).

    The `extra` dict here is deliberately identical to the old
    `_log_channel`'s shape — `tags`/`agent_slug`/`sim_mode` only, NEVER
    `alert_event` — adding `alert_event: "breach"` would make this very
    write re-match BREACH_AGENT's own log condition on the next flush and
    loop forever.
    """
    _EVENTS_LOGGER.warning(
        log_line or f"{title} — {body}",
        extra={"tags": ["agent"], "agent_slug": agent_slug, "sim_mode": bool(sim_mode)},
    )
