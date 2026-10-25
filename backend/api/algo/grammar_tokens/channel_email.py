"""Notify channel resolver — `email`: email to every address in
secrets.alert_emails."""

from __future__ import annotations

import sys


def _send_email_channel(title: str, body: str, tg: "str | None" = None,
                        email: "tuple | None" = None, recipients=None, **_kw) -> None:
    # Lazy import — `resolve_email_recipients` stays in `event_agents.py`
    # (it is a public, independently-tested helper, not exclusive to this
    # channel), and `event_agents.py`'s own `CHANNELS` dict imports this
    # function back FROM this module at its own top level. An eager
    # top-level import here, the other direction, would deadlock on
    # whichever module is imported first.
    from backend.api.algo.event_agents import resolve_email_recipients
    from backend.shared.helpers.mail_utils import send_email
    if not email:
        return
    subject, html_body = email
    for addr in resolve_email_recipients(recipients):
        try:
            send_email("RamboQuant", addr, subject, html_body)
        except Exception as e:
            sys.stderr.write(f"event_agents: email to {addr} failed: {e}\n")
