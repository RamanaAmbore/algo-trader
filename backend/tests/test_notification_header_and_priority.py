"""
Unit tests for the standard notification header (`format_notification_header`)
and tier-driven ntfy priority (`_TIER_TO_NTFY_PRIORITY`), both in
backend/shared/helpers/alert_utils.py.

Golden-string coverage for every renderer that now embeds these lives in
test_alert_golden.py; these tests cover the two building blocks directly,
plus the channel-scoping regression caught during this feature's own
implementation (see TestSendChannelPriorityScoping below).
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from backend.shared.helpers.alert_utils import (
    format_notification_header,
    _TIER_TO_NTFY_PRIORITY,
)


# ---------------------------------------------------------------------------
# format_notification_header()
# ---------------------------------------------------------------------------

class TestFormatNotificationHeader:
    def test_with_agent_name_and_id_includes_identity_line(self):
        header = format_notification_header("loss-margin-low", 42, ist_display="FIXED_TS")
        lines = header.splitlines()
        assert lines[0] == "Agent: loss-margin-low (#42)"
        assert lines[1] == "FIXED_TS"

    def test_without_agent_name_is_timestamp_only(self):
        header = format_notification_header(None, 42, ist_display="FIXED_TS")
        assert header == "FIXED_TS"
        assert "Agent:" not in header

    def test_without_agent_id_is_timestamp_only(self):
        header = format_notification_header("some-agent", None, ist_display="FIXED_TS")
        assert header == "FIXED_TS"
        assert "Agent:" not in header

    def test_without_either_is_timestamp_only(self):
        header = format_notification_header(None, None, ist_display="FIXED_TS")
        assert header == "FIXED_TS"

    def test_omitting_ist_display_calls_the_real_dual_tz_clock(self):
        # No ist_display kwarg -> falls through to timestamp_display(),
        # which must produce the dual-tz "IST | ET" shape.
        header = format_notification_header(None, None)
        assert " IST | " in header
        assert ("EDT" in header) or ("EST" in header)

    def test_agent_id_zero_is_falsy_but_still_a_valid_id(self):
        # agent_id=0 would be a legitimate row id in principle; the
        # function's own contract checks `agent_id is not None`, not
        # truthiness, so id 0 must still render. Guards against a future
        # refactor accidentally using `if agent_id` instead of `is not None`.
        header = format_notification_header("agent-zero", 0, ist_display="FIXED_TS")
        assert header.splitlines()[0] == "Agent: agent-zero (#0)"

    def test_first_line_only_is_safe_for_single_line_contexts(self):
        header = format_notification_header("x", 1, ist_display="FIXED_TS")
        first_line = header.splitlines()[0]
        assert "\n" not in first_line


# ---------------------------------------------------------------------------
# _TIER_TO_NTFY_PRIORITY mapping
# ---------------------------------------------------------------------------

class TestTierToNtfyPriorityMapping:
    @pytest.mark.parametrize("tier,expected", [
        ("critical", "urgent"),
        ("high", "high"),
        ("medium", "default"),
        ("low", "low"),
    ])
    def test_every_real_tier_maps(self, tier, expected):
        assert _TIER_TO_NTFY_PRIORITY.get(tier) == expected

    def test_unknown_tier_maps_to_none_not_a_guessed_default(self):
        # .get() with no second arg -> None for an unmapped tier (e.g. the
        # "info" pseudo-tier some defensive getattr() fallbacks use) — the
        # CALLER is responsible for omitting the priority kwarg when this
        # is None, letting send_ntfy_alert's own clock-based default apply.
        assert _TIER_TO_NTFY_PRIORITY.get("info") is None
        assert _TIER_TO_NTFY_PRIORITY.get(None) is None


# ---------------------------------------------------------------------------
# events.py:_send_ntfy_channel — explicit override wins, tier is the
# fallback default, no-tier/no-override omits the kwarg.
# ---------------------------------------------------------------------------

class TestEventsSendNtfyChannelPriorityDerivation:
    @pytest.mark.asyncio
    async def test_explicit_channel_priority_wins_over_tier(self):
        from backend.api.algo.events import _send_ntfy_channel

        agent = MagicMock(name="agent", tier="critical")
        agent.name = "loss-funds-negative"
        with patch("backend.shared.helpers.alert_utils.send_ntfy_alert") as mock_send:
            await _send_ntfy_channel({"priority": "low"}, agent, "tg body", None)
        mock_send.assert_called_once()
        assert mock_send.call_args.kwargs["priority"] == "low"

    @pytest.mark.asyncio
    async def test_tier_becomes_the_default_when_no_explicit_priority(self):
        from backend.api.algo.events import _send_ntfy_channel

        agent = MagicMock(name="agent", tier="critical")
        agent.name = "loss-funds-negative"
        with patch("backend.shared.helpers.alert_utils.send_ntfy_alert") as mock_send:
            await _send_ntfy_channel({}, agent, "tg body", None)
        mock_send.assert_called_once()
        assert mock_send.call_args.kwargs["priority"] == "urgent"

    @pytest.mark.asyncio
    async def test_no_tier_and_no_override_passes_none_letting_clock_fallback_apply(self):
        from backend.api.algo.events import _send_ntfy_channel

        agent = MagicMock(name="agent", tier=None)
        agent.name = "some-agent"
        with patch("backend.shared.helpers.alert_utils.send_ntfy_alert") as mock_send:
            await _send_ntfy_channel({}, agent, "tg body", None)
        mock_send.assert_called_once()
        assert mock_send.call_args.kwargs["priority"] is None


# ---------------------------------------------------------------------------
# event_agents.py:_send_channel — regression guard for the bug caught
# during this feature's own implementation: deriving priority from
# agent.tier unconditionally (regardless of channel type) would pass a
# `priority` kwarg to `_send_telegram_html`, which has no such parameter
# (plain positional signature, no **kwargs) -> TypeError -> every
# telegram send for any agent with a tier (i.e. every real agent, since
# Agent.tier is NOT NULL default "medium") would silently stop sending,
# swallowed by _send_channel's own try/except. Must be scoped to ntfy only.
# ---------------------------------------------------------------------------

class TestSendChannelPriorityScoping:
    """CHANNELS is a module-level dict binding each channel name to the
    real function OBJECT at import time (`CHANNELS = {"ntfy": ("ntfy",
    _send_ntfy), ...}`) — patching the module attribute (e.g.
    `patch("event_agents._send_ntfy")`) does NOT affect calls made via
    `CHANNELS["ntfy"]`, since that tuple already holds a direct reference
    to the original function object. Patch the CHANNELS dict's own entry
    instead, which is what `_send_channel` actually reads."""

    @pytest.mark.asyncio
    async def test_ntfy_channel_gets_tier_derived_priority(self):
        from backend.api.algo import event_agents

        agent = MagicMock(name="agent", tier="high", slug="test-agent")
        ch = {"channel": "ntfy", "enabled": True, "gate": False}
        out = ("title", "body")
        mock_send = MagicMock()
        with patch.dict(event_agents.CHANNELS, {"ntfy": ("ntfy", mock_send)}):
            ok = await event_agents._send_channel(ch, out, agent)
        assert ok is True
        mock_send.assert_called_once()
        assert mock_send.call_args.kwargs.get("priority") == "high"

    @pytest.mark.asyncio
    async def test_telegram_channel_never_receives_a_priority_kwarg(self):
        """The actual regression: a tier-bearing agent sending over
        telegram must NOT get a `priority` kwarg — _send_telegram_html has
        no such parameter and would raise TypeError, which _send_channel's
        try/except would swallow, silently dropping the telegram send."""
        from backend.api.algo import event_agents

        agent = MagicMock(name="agent", tier="critical", slug="test-agent")
        ch = {"channel": "telegram", "enabled": True, "gate": False}
        out = ("title", "body", "tg_html")
        mock_send = MagicMock()
        with patch.dict(event_agents.CHANNELS, {"telegram": ("telegram", mock_send)}):
            ok = await event_agents._send_channel(ch, out, agent)
        assert ok is True
        mock_send.assert_called_once()
        assert "priority" not in mock_send.call_args.kwargs

    @pytest.mark.asyncio
    async def test_email_channel_never_receives_a_priority_kwarg_either(self):
        """_send_email_channel happens to tolerate an extra kwarg via
        **_kw, but scoping to ntfy-only means it's never offered one in
        the first place — priority is an ntfy-only concept."""
        from backend.api.algo import event_agents

        agent = MagicMock(name="agent", tier="critical", slug="test-agent")
        ch = {"channel": "email", "enabled": True, "gate": False}
        out = ("title", "body", "tg_html", ("subject", "<html></html>"))
        mock_send = MagicMock()
        with patch.dict(event_agents.CHANNELS, {"email": ("mail", mock_send)}):
            ok = await event_agents._send_channel(ch, out, agent)
        assert ok is True
        mock_send.assert_called_once()
        assert "priority" not in mock_send.call_args.kwargs

    @pytest.mark.asyncio
    async def test_explicit_ntfy_priority_override_still_wins_over_tier(self):
        from backend.api.algo import event_agents

        agent = MagicMock(name="agent", tier="low", slug="test-agent")
        ch = {"channel": "ntfy", "enabled": True, "gate": False, "priority": "urgent"}
        out = ("title", "body")
        mock_send = MagicMock()
        with patch.dict(event_agents.CHANNELS, {"ntfy": ("ntfy", mock_send)}):
            ok = await event_agents._send_channel(ch, out, agent)
        assert ok is True
        mock_send.assert_called_once()
        assert mock_send.call_args.kwargs.get("priority") == "urgent"
