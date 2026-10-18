"""P1 fix (2026-10 audit, Group C items 10 & 11) — channel backstops on
the seeded event-agent definitions in `backend/api/algo/event_agents.py`.

Item 10: `TEMPLATE_GUARD_AGENT` / `TEMPLATE_ATTACH_FAIL_AGENT` declared
no `email` channel at all, despite `template_attach.py`'s own
docstring explicitly promising "Email lands in the alert inbox (durable
record for end-of-day review)."

Item 11: `CHASE_CANCEL_AGENT` (a possible duplicate live order resting
unconfirmed) and `TEMPLATE_ATTACH_URGENT_AGENT` (covers
`wing_unprotected` — an unprotected live SELL position) were ntfy-only,
with no Telegram/email backstop, despite being two of the most severe
alert types in the system.
"""
from __future__ import annotations

from backend.api.algo import event_agents


def _channels(agent_dict: dict) -> set[str]:
    return {e.get("channel") for e in agent_dict["events"] if isinstance(e, dict)}


class TestTemplateEmailBackstop:
    def test_template_guard_agent_has_email_channel(self):
        assert "email" in _channels(event_agents.TEMPLATE_GUARD_AGENT)

    def test_template_attach_fail_agent_has_email_channel(self):
        assert "email" in _channels(event_agents.TEMPLATE_ATTACH_FAIL_AGENT)

    def test_template_guard_agent_still_has_telegram_and_ntfy(self):
        """Adding email must not have dropped the existing channels."""
        chans = _channels(event_agents.TEMPLATE_GUARD_AGENT)
        assert {"telegram", "ntfy", "email"} <= chans

    def test_template_attach_fail_agent_still_has_telegram_and_ntfy(self):
        chans = _channels(event_agents.TEMPLATE_ATTACH_FAIL_AGENT)
        assert {"telegram", "ntfy", "email"} <= chans


class TestSevereAlertTelegramBackstop:
    def test_chase_cancel_agent_has_telegram_channel(self):
        assert "telegram" in _channels(event_agents.CHASE_CANCEL_AGENT)

    def test_chase_cancel_agent_still_has_ntfy_urgent(self):
        ntfy = [e for e in event_agents.CHASE_CANCEL_AGENT["events"]
                if e.get("channel") == "ntfy"]
        assert len(ntfy) == 1 and ntfy[0].get("priority") == "urgent"

    def test_template_attach_urgent_agent_has_telegram_channel(self):
        """Covers wing_unprotected (unprotected live SELL position)."""
        assert "telegram" in _channels(event_agents.TEMPLATE_ATTACH_URGENT_AGENT)

    def test_template_attach_urgent_agent_still_has_ntfy_urgent(self):
        ntfy = [e for e in event_agents.TEMPLATE_ATTACH_URGENT_AGENT["events"]
                if e.get("channel") == "ntfy"]
        assert len(ntfy) == 1 and ntfy[0].get("priority") == "urgent"
