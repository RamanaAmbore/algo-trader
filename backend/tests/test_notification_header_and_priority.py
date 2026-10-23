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
    resolve_origin,
    _TIER_TO_NTFY_PRIORITY,
)


# ---------------------------------------------------------------------------
# resolve_origin()
# ---------------------------------------------------------------------------

class TestResolveOrigin:
    def test_real_agent_name_is_trusted_as_is(self):
        assert resolve_origin(None, "Chase close positions") == "Chase close positions"
        assert resolve_origin(42, "Chase close positions") == "Chase close positions"

    def test_no_agent_id_and_no_name_is_manual(self):
        assert resolve_origin(None, None) == "manual"

    def test_manual_sentinel_name_is_manual_case_insensitive(self):
        assert resolve_origin(7, "Manual operator order") == "manual"
        assert resolve_origin(7, "MANUAL OPERATOR ORDER") == "manual"
        assert resolve_origin(None, "manual operator order") == "manual"

    def test_bare_real_agent_id_with_no_name_is_not_manual(self):
        # Advisor/P2 guard: an unresolved name must never be misread as
        # "no agent at all" — a real, non-sentinel id renders self-
        # describing text, not "manual".
        assert resolve_origin(42, None) == "Agent #42"

    def test_cached_manual_sentinel_id_resolves_to_manual_with_no_name(self):
        from backend.shared.helpers import alert_utils
        alert_utils._MANUAL_AGENT_ID_CACHE["id"] = 99
        try:
            assert resolve_origin(99, None) == "manual"
            assert resolve_origin(7, None) == "Agent #7"
        finally:
            alert_utils._MANUAL_AGENT_ID_CACHE["id"] = None


# ---------------------------------------------------------------------------
# format_notification_header()
# ---------------------------------------------------------------------------

class TestFormatNotificationHeader:
    def test_with_agent_name_and_id_includes_identity_line(self):
        header = format_notification_header("loss-margin-low", 42, ist_display="FIXED_TS")
        lines = header.splitlines()
        assert lines[0] == "Agent: loss-margin-low (#42)"
        assert lines[1] == "FIXED_TS"

    def test_without_agent_name_falls_back_to_bare_agent_id(self):
        # 2026-10 audit fix: the header now ALWAYS carries an origin line
        # — a bare, unresolved agent_id with no name renders as the
        # self-describing "Agent #{id}" rather than silently omitting
        # any origin at all.
        header = format_notification_header(None, 42, ist_display="FIXED_TS")
        lines = header.splitlines()
        assert lines[0] == "Agent #42"
        assert lines[1] == "FIXED_TS"

    def test_without_agent_id_still_shows_the_real_name(self):
        # A genuinely real agent_name reaching this call with no
        # agent_id (e.g. fill_notify's replayed-record shape, which
        # never carries agent_id) still shows the real name — it is NOT
        # "manual" just because the id happens to be missing.
        header = format_notification_header("some-agent", None, ist_display="FIXED_TS")
        lines = header.splitlines()
        assert lines[0] == "Agent: some-agent"
        assert lines[1] == "FIXED_TS"

    def test_without_either_shows_manual(self):
        # 2026-10 operator requirement: "make sure it has agent info in
        # the beginning... if it is not by agent, add manual" — no
        # longer a bare timestamp.
        header = format_notification_header(None, None, ist_display="FIXED_TS")
        lines = header.splitlines()
        assert lines[0] == "Agent: manual"
        assert lines[1] == "FIXED_TS"

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

    @pytest.mark.parametrize("agent_name, agent_id", [
        ("loss-margin-low", 42),
        (None, 42),
        ("some-agent", None),
        (None, None),
    ])
    def test_origin_is_always_the_first_line_regardless_of_inputs(self, agent_name, agent_id):
        # 2026-10 audit fix invariant: every call ALWAYS emits an origin
        # line first — never silently falls back to a bare timestamp.
        header = format_notification_header(agent_name, agent_id, ist_display="FIXED_TS")
        first_line = header.splitlines()[0]
        assert first_line.startswith("Agent") or first_line.startswith("Agent #")
        assert header.splitlines()[1] == "FIXED_TS"


# ---------------------------------------------------------------------------
# _TIER_TO_NTFY_PRIORITY mapping
# ---------------------------------------------------------------------------

class TestTierToNtfyPriorityMapping:
    @pytest.mark.parametrize("tier,expected", [
        ("critical", "urgent"),
        ("high", "high"),
        ("medium", "default"),
        ("low", "low"),
        # 2026-10-16 fix — "info" used to be deliberately left unmapped,
        # on the assumption that falling through to send_ntfy_alert's
        # own clock-based day/night default was fine. It wasn't: that
        # default is "urgent" for essentially the whole IST trading day
        # (22:00-07:00 ET == daytime IST), so a routine info-tier
        # scheduled notification (market-open-nse) got the "urgent"
        # 3x-repeat-send treatment meant for genuinely critical alerts —
        # live incident: operator saw 3 ntfy pushes for one market-open
        # notification. "info" now maps explicitly to "low".
        ("info", "low"),
    ])
    def test_every_real_tier_maps(self, tier, expected):
        assert _TIER_TO_NTFY_PRIORITY.get(tier) == expected

    def test_unknown_tier_maps_to_none_not_a_guessed_default(self):
        # .get() with no second arg -> None for a genuinely UNRECOGNIZED
        # tier (not "info", which is now an explicitly mapped real tier
        # — see test_every_real_tier_maps above) — the CALLER is
        # responsible for omitting the priority kwarg when this is
        # None, letting send_ntfy_alert's own clock-based default apply.
        assert _TIER_TO_NTFY_PRIORITY.get("not-a-real-tier") is None
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
