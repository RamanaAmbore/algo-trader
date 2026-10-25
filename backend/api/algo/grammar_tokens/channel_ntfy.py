"""Notify channel resolver — `ntfy`: push notification via ntfy.sh.
Priority sets urgency; defaults to the firing agent's tier when
omitted."""

from __future__ import annotations


def _send_ntfy(title: str, body: str, tg: "str | None" = None, priority: "str | None" = None) -> None:
    """Push a notification via ntfy.sh. `tg` is accepted for call-
    signature parity with the other channel senders but unused — ntfy
    has no separate plain-text/HTML body distinction."""
    from backend.shared.helpers import alert_utils
    alert_utils.send_ntfy_alert(title, body, priority=priority)
