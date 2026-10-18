"""
Tests for the rich-alert custom-message bug fix (2026-10).

Pre-fix: `_v2_send_rich_alert` (agent_engine.py) ALWAYS built its own
generic kind/threshold table for telegram/email (ntfy reuses that same
body as plain text), completely ignoring a channel's own `message`
override — Sprint 6's plain-string `msg=`, or Part 2's new `{"$given":
name}` server-evaluated marker. Because `_ae_dispatch_survivor_entry`
skips telegram/email/ntfy in the ordinary `dispatch()` path whenever
`_v2_send_rich_alert` returns `True` (which it does for every REAL
cycle-agent fire, to avoid duplicate delivery), this meant `msg=` never
actually reached the operator on those three channels for a real
threshold-breach agent — only log/websocket/inapp (which always run
through `dispatch()` regardless) ever showed the real custom text.

This file proves the fix: `_v2_send_rich_alert` now checks for a
resolved custom message BEFORE falling back to the generic table, for
both the already-shipped plain-string form and the new `{"$given":
name}` marker.

SSOT: drives the REAL `_v2_send_rich_alert` function (not a mock of it)
with a real `EvalResult`, exercising the actual `_tags_match` gate and
`events._resolve_given_message` resolution it reuses — not a
reimplementation of either.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from backend.api.algo.agent_engine import _v2_send_rich_alert
from backend.api.algo.events import EvalResult


class _Agent:
    """Minimal Agent-row stand-in — mirrors test_agent_action_fanout.py's
    `_Agent`, extended with `events` (channel list) since this is what
    the fix under test actually reads."""
    def __init__(self, id=1, slug="rich-alert-agent", name="Rich Alert Agent",
                 tier="medium", fire_at_time=None, events=None):
        self.id = id
        self.slug = slug
        self.name = name
        self.tier = tier
        self.fire_at_time = fire_at_time
        self.events = events or []
        self.actions = []


def _now():
    return datetime(2026, 10, 13, 10, 0, 0, tzinfo=timezone.utc)


def _match(value=-60000.0, threshold=-50000.0):
    return {
        "metric": "pnl", "scope": "positions.total", "op": "<=",
        "value": value, "threshold": threshold,
        "row": {"account": "TOTAL"},
    }


def _eval_result(condition_text="pnl <= -50000"):
    return EvalResult(triggered=True, condition_text=condition_text, detail={"matches": []})


async def _capture_rich_alert(agent, matches, eval_result, *, sim_mode=False, context=None):
    """Run the real _v2_send_rich_alert and capture the logger.info extra
    dict it builds (the one event_agents.py's RICH_ALERT_AGENT reads
    tg_table/email_table_html off of)."""
    from backend.api.algo import agent_engine as ae_mod
    calls = []
    with patch.object(ae_mod.logger, "info",
                       side_effect=lambda msg, **kw: calls.append(kw.get("extra"))):
        sent = await _v2_send_rich_alert(
            agent, matches, _now(), sim_mode=sim_mode, context=context,
            eval_result=eval_result,
        )
    assert sent is True, "rich alert must report sent=True for a real breach"
    assert len(calls) == 1
    return calls[0]


class TestPlainStringMsgOverride:
    """Sprint 6's already-shipped plain-string msg= — pre-existing bug,
    now fixed."""

    @pytest.mark.asyncio
    async def test_telegram_shows_custom_text_not_generic_table(self):
        agent = _Agent(events=[
            {"channel": "telegram", "enabled": True, "message": "Loss: big drop on NIFTY"},
        ])
        extra = await _capture_rich_alert(agent, [_match()], _eval_result())
        assert extra["tg_table"] == "Loss: big drop on NIFTY"
        assert "TOTAL" not in extra["tg_table"], (
            "generic kind/threshold table must NOT be used when a custom "
            "message is configured"
        )

    @pytest.mark.asyncio
    async def test_email_shows_custom_text_not_generic_table(self):
        agent = _Agent(events=[
            {"channel": "email", "enabled": True, "message": "Loss: big drop on NIFTY"},
        ])
        extra = await _capture_rich_alert(agent, [_match()], _eval_result())
        assert extra["email_table_html"] == "<p>Loss: big drop on NIFTY</p>"

    @pytest.mark.asyncio
    async def test_ntfy_channel_message_also_picked_up(self):
        """ntfy has no separate body in this architecture (it reuses the
        telegram body as plain text downstream) — a message configured
        ONLY on the ntfy channel entry must still drive tg_table/
        email_table_html, since that's the only body ntfy can read from."""
        agent = _Agent(events=[
            {"channel": "ntfy", "enabled": True, "message": "ntfy-only custom text"},
        ])
        extra = await _capture_rich_alert(agent, [_match()], _eval_result())
        assert extra["tg_table"] == "ntfy-only custom text"

    @pytest.mark.asyncio
    async def test_no_message_configured_falls_back_to_generic_table(self):
        """Backward-compat / majority-case regression guard: an agent
        with no msg= anywhere must behave EXACTLY as before this fix."""
        agent = _Agent(events=[
            {"channel": "telegram", "enabled": True},
            {"channel": "email", "enabled": True},
        ])
        extra = await _capture_rich_alert(agent, [_match()], _eval_result())
        assert "TOTAL" in extra["tg_table"], (
            "no custom message configured -> must build the generic "
            "kind/threshold table exactly as before"
        )

    @pytest.mark.asyncio
    async def test_disabled_channel_message_ignored(self):
        agent = _Agent(events=[
            {"channel": "telegram", "enabled": False, "message": "should not be used"},
        ])
        extra = await _capture_rich_alert(agent, [_match()], _eval_result())
        assert extra["tg_table"] != "should not be used"

    @pytest.mark.asyncio
    async def test_eval_result_none_preserves_exact_prior_behavior(self):
        """Any caller that doesn't pass eval_result at all (every
        pre-existing test/call site) must see byte-identical behavior —
        the override check is a true no-op without it."""
        agent = _Agent(events=[
            {"channel": "telegram", "enabled": True, "message": "should never be used"},
        ])
        from backend.api.algo import agent_engine as ae_mod
        calls = []
        with patch.object(ae_mod.logger, "info",
                           side_effect=lambda msg, **kw: calls.append(kw.get("extra"))):
            sent = await _v2_send_rich_alert(agent, [_match()], _now(), sim_mode=False)
        assert sent is True
        assert "TOTAL" in calls[0]["tg_table"]


class TestAccountMasking:
    """The custom-message path sends free-form text (operator-authored,
    or GIVEN-computed) straight to telegram/email/ntfy — text that never
    reached those channels at all before this fix. Must go through the
    same `mask_account_in_text()` chokepoint `events.py:_describe_actions`
    already uses for equivalent free-form text."""

    @pytest.mark.asyncio
    async def test_account_code_in_custom_message_is_masked(self):
        from backend.shared.helpers.utils import mask_account
        agent = _Agent(events=[
            {"channel": "telegram", "enabled": True, "message": "Loss on ZG0790: -60000"},
        ])
        extra = await _capture_rich_alert(agent, [_match()], _eval_result())
        # Computed, not hardcoded — `mask_account`'s `_REGISTRY` is a
        # module global other tests populate via `register_accounts()`,
        # so "ZG0790" doesn't deterministically mask to "ZG####" across
        # the whole suite (it only falls back to the digit-stripping
        # shape when the registry is empty/doesn't know this account).
        expected_masked = mask_account("ZG0790")
        assert "ZG0790" not in extra["tg_table"]
        assert expected_masked in extra["tg_table"]
        assert "-60000" in extra["tg_table"], "P&L digits must survive masking untouched"

    @pytest.mark.asyncio
    async def test_pnl_numbers_and_symbol_names_unaffected_by_masking(self):
        agent = _Agent(events=[
            {"channel": "telegram", "enabled": True, "message": "Loss: -60000 on NIFTY"},
        ])
        extra = await _capture_rich_alert(agent, [_match()], _eval_result())
        assert extra["tg_table"] == "Loss: -60000 on NIFTY"

    @pytest.mark.asyncio
    async def test_given_marker_resolved_text_also_masked(self):
        from backend.shared.helpers.utils import mask_account
        agent = _Agent(id=5, events=[
            {"channel": "telegram", "enabled": True, "message": {"$given": "msg_txt"}},
        ])
        context = {"given": {5: {"msg_txt": "Loss on ZG0790: -60000"}}}
        extra = await _capture_rich_alert(agent, [_match()], _eval_result(), context=context)
        expected_masked = mask_account("ZG0790")
        assert "ZG0790" not in extra["tg_table"]
        assert expected_masked in extra["tg_table"]


class TestGivenMarkerMsgOverride:
    """Part 2's new `{"$given": "<name>"}` server-evaluated marker."""

    @pytest.mark.asyncio
    async def test_resolves_given_marker_to_bound_value_on_telegram(self):
        agent = _Agent(id=5, events=[
            {"channel": "telegram", "enabled": True, "message": {"$given": "msg_txt"}},
        ])
        context = {"given": {5: {"msg_txt": "Loss: -60000 on NIFTY"}}}
        extra = await _capture_rich_alert(agent, [_match()], _eval_result(), context=context)
        assert extra["tg_table"] == "Loss: -60000 on NIFTY"

    @pytest.mark.asyncio
    async def test_resolves_given_marker_on_email_too(self):
        agent = _Agent(id=5, events=[
            {"channel": "email", "enabled": True, "message": {"$given": "msg_txt"}},
        ])
        context = {"given": {5: {"msg_txt": "Loss: -60000 on NIFTY"}}}
        extra = await _capture_rich_alert(agent, [_match()], _eval_result(), context=context)
        assert extra["email_table_html"] == "<p>Loss: -60000 on NIFTY</p>"

    @pytest.mark.asyncio
    async def test_unresolved_given_marker_falls_back_to_generic_table(self):
        """The binding never resolved this tick (e.g. its own GIVEN
        evaluation failed) -> must fall back to the generic table, never
        send a blank/broken message."""
        agent = _Agent(id=5, events=[
            {"channel": "telegram", "enabled": True, "message": {"$given": "never_defined"}},
        ])
        context = {"given": {5: {}}}
        extra = await _capture_rich_alert(agent, [_match()], _eval_result(), context=context)
        assert "TOTAL" in extra["tg_table"]

    @pytest.mark.asyncio
    async def test_given_marker_without_context_falls_back_to_generic_table(self):
        agent = _Agent(id=5, events=[
            {"channel": "telegram", "enabled": True, "message": {"$given": "msg_txt"}},
        ])
        extra = await _capture_rich_alert(agent, [_match()], _eval_result(), context=None)
        assert "TOTAL" in extra["tg_table"]


class TestCustomMessageOverridesScheduledLine:
    """An explicit msg= must win over the 'Scheduled — HH:MM IST' special
    case too (ordering requirement, not just over the generic table)."""

    @pytest.mark.asyncio
    async def test_custom_message_wins_over_scheduled_line(self):
        agent = _Agent(tier="info", fire_at_time="09:15", events=[
            {"channel": "telegram", "enabled": True, "message": "Market opened"},
        ])
        extra = await _capture_rich_alert(agent, [_match()], _eval_result())
        assert extra["tg_table"] == "Market opened"
        assert "Scheduled" not in extra["tg_table"]

    @pytest.mark.asyncio
    async def test_scheduled_line_still_renders_when_no_custom_message(self):
        """Regression guard for the existing scheduled-agent behavior —
        must be byte-identical when no msg= is configured."""
        agent = _Agent(tier="info", fire_at_time="09:15", events=[
            {"channel": "telegram", "enabled": True},
        ])
        extra = await _capture_rich_alert(agent, [_match()], _eval_result())
        assert extra["tg_table"] == f"{agent.name} — Scheduled — 09:15 IST"


class TestRendererChainDeliversTextIntact:
    """Chains the captured `extra` dict through the REAL
    `event_agents._render_rich_alert` (the function RICH_ALERT_AGENT
    actually invokes to build what gets sent) to prove the custom text
    survives into all three channel bodies — ntfy in particular goes
    through `_html_to_plain`, the exact HTML-stripping step events.py's
    own module docstring warns is unsafe for arbitrary operator text
    ("Do NOT run this over arbitrary user/operator-authored text") —
    this fix is the first path that puts real operator/GIVEN text
    through it, so round-trip fidelity must be verified, not assumed.
    """

    async def _render(self, agent, message):
        agent.events = [{"channel": "telegram", "enabled": True, "message": message}]
        extra = await _capture_rich_alert(agent, [_match()], _eval_result())
        from backend.api.algo.event_agents import _render_rich_alert
        rec = {"extra": extra}
        title, ntfy_body, tg_html, (email_subject, email_html) = _render_rich_alert(rec)
        return title, ntfy_body, tg_html, email_subject, email_html

    @pytest.mark.asyncio
    async def test_plain_string_message_survives_verbatim_in_ntfy_body(self):
        agent = _Agent()
        _title, ntfy_body, tg_html, _subj, email_html = await self._render(
            agent, "Loss: -60000 on NIFTY",
        )
        assert "Loss: -60000 on NIFTY" in ntfy_body, (
            f"custom message must survive _html_to_plain verbatim, got {ntfy_body!r}"
        )
        assert "Loss: -60000 on NIFTY" in tg_html, "must appear (HTML-escaped-safe) in the telegram body"
        assert "Loss: -60000 on NIFTY" in email_html, "must appear in the email HTML body"

    @pytest.mark.asyncio
    async def test_given_marker_resolved_text_survives_in_all_three_bodies(self):
        agent = _Agent(id=5, events=[
            {"channel": "telegram", "enabled": True, "message": {"$given": "msg_txt"}},
        ])
        context = {"given": {5: {"msg_txt": "Loss: -60000 on NIFTY"}}}
        extra = await _capture_rich_alert(agent, [_match()], _eval_result(), context=context)
        from backend.api.algo.event_agents import _render_rich_alert
        title, ntfy_body, tg_html, (email_subject, email_html) = _render_rich_alert({"extra": extra})
        assert "Loss: -60000 on NIFTY" in ntfy_body
        assert "Loss: -60000 on NIFTY" in tg_html
        assert "Loss: -60000 on NIFTY" in email_html

    @pytest.mark.asyncio
    async def test_operator_comparison_syntax_with_angle_bracket_survives_ntfy_round_trip(self):
        """The one shape events.py's own docstring specifically warns
        about: free text containing '<'/'>'/'&' characters (operator
        condition syntax) must not be corrupted by the escape -> strip
        -> unescape round trip ntfy's body goes through."""
        agent = _Agent()
        _title, ntfy_body, tg_html, _subj, email_html = await self._render(
            agent, "pnl < -5000 & falling",
        )
        assert "pnl < -5000 & falling" in ntfy_body, (
            f"operator syntax with <, & must round-trip verbatim through "
            f"_html_to_plain, got {ntfy_body!r}"
        )

    @pytest.mark.asyncio
    async def test_masked_account_survives_renderer_chain_still_masked(self):
        from backend.shared.helpers.utils import mask_account
        agent = _Agent()
        _title, ntfy_body, tg_html, _subj, email_html = await self._render(
            agent, "Loss on ZG0790: -60000",
        )
        expected_masked = mask_account("ZG0790")
        for body in (ntfy_body, tg_html, email_html):
            assert "ZG0790" not in body
            assert expected_masked in body


class TestEndToEndSurvivorEntryWiring:
    """Structural guard: _ae_dispatch_survivor_entry must actually pass
    eval_result=result into _v2_send_rich_alert — a regression here
    would silently re-break this fix even with every test above passing
    (they all call _v2_send_rich_alert directly)."""

    @pytest.mark.asyncio
    async def test_survivor_entry_threads_eval_result_into_rich_alert(self):
        from backend.api.algo import agent_engine as ae_mod

        agent = _Agent(events=[
            {"channel": "telegram", "enabled": True, "message": "custom via survivor entry"},
        ])
        result = _eval_result()
        entry = {
            "agent": agent, "matches": [_match()], "result": result,
            "sim_mode": False, "new_status": "active", "bypass_schedule": True,
        }

        captured_extra = {}
        async def _fake_rich_alert(*a, **kw):
            captured_extra.update(kw)
            return False   # pretend it didn't send, so dispatch()/execute() below are simple no-ops

        with patch.object(ae_mod, "_v2_send_rich_alert", new=AsyncMock(side_effect=_fake_rich_alert)), \
             patch.object(ae_mod, "dispatch", new=AsyncMock()):
            await ae_mod._ae_dispatch_survivor_entry(entry, _now(), {}, None)

        assert captured_extra.get("eval_result") is result, (
            "_ae_dispatch_survivor_entry must pass the real EvalResult "
            "through to _v2_send_rich_alert as eval_result=, or the "
            "custom-message override check above can never activate in "
            "the real dispatch path"
        )


class TestPerAccountBreachAccountCodeMasked:
    """The SEPARATE masking leak fix (2026-10, bundled into this same
    commit): a per-account (non-TOTAL) breach alert rendered via the
    GENERIC kind/threshold table (no msg= override at all) must never
    carry the real broker account code — fixed upstream in
    `_v2_match_to_alertrow` (masks `scope` via `mask_account()`), so
    every consumer of a row's `scope` field gets the masked value for
    free. This is the end-to-end proof through the real
    `_v2_send_rich_alert` pipeline, not just the unit-level
    `test_v2_match_to_alertrow.py` coverage."""

    @pytest.mark.asyncio
    async def test_generic_table_never_contains_real_account_code(self):
        from backend.shared.helpers.utils import mask_account

        agent = _Agent(events=[{"channel": "telegram", "enabled": True}])
        per_account_match = {
            "metric": "pnl", "scope": "positions.any_acct", "op": "<=",
            "value": -8000.0, "threshold": -5000.0,
            "row": {"account": "ZG0790", "pnl": -8000.0},
        }
        extra = await _capture_rich_alert(agent, [per_account_match], _eval_result())
        assert "ZG0790" not in extra["tg_table"], (
            "the real account code must never reach the rendered alert text"
        )
        assert "ZG0790" not in extra["email_table_html"]
        assert mask_account("ZG0790") in extra["tg_table"]


class TestSubjectDetailNoLongerDuplicatesAgentPrefix:
    """P1 fix (2026-10 audit, Group C item 9) — `_v2_send_rich_alert`'s
    own `subject_detail` (built at agent_engine.py ~line 1211) used to
    be `format_notification_header(...)`'s FULL first line, "Agent:
    {name} (#{id})". Concatenated with `dispatch_payload`'s base email
    prefix ("RamboQuant Agent: ") downstream in event_agents.py's
    `_render_rich_alert`, this produced a duplicated subject reading
    "RamboQuant Agent: Agent: {name} (#{id})". The real triggering
    agent's name/id must still be present, just without the duplicate
    "Agent:" label."""

    @pytest.mark.asyncio
    async def test_subject_detail_has_no_duplicate_agent_label(self):
        agent = _Agent(name="loss-margin-low", id=42,
                       events=[{"channel": "telegram", "enabled": True}])
        extra = await _capture_rich_alert(agent, [_match()], _eval_result())
        assert "Agent: Agent:" not in extra["subject_detail"]
        assert "loss-margin-low (#42)" in extra["subject_detail"]

    @pytest.mark.asyncio
    async def test_end_to_end_email_subject_has_no_duplicate_agent_label(self):
        """Full round trip through dispatch_payload, matching exactly
        what event_agents.py:_render_rich_alert builds for the real
        email Subject header."""
        from backend.shared.helpers.alert_utils import dispatch_payload

        agent = _Agent(name="loss-margin-low", id=42,
                       events=[{"channel": "telegram", "enabled": True}])
        extra = await _capture_rich_alert(agent, [_match()], _eval_result())
        with patch('backend.shared.helpers.alert_utils.config', {'deploy_branch': 'main'}):
            payload = dispatch_payload(
                "alert", extra["ist_display"], extra["tg_table"], extra["email_table_html"],
                extra["subject_detail"], sim_mode=False, mode_tag="",
                agent_name=extra.get("agent_name"), agent_id=extra.get("agent_id"),
            )
        assert "Agent: Agent:" not in payload["email_subject"]
        assert payload["email_subject"] == "RamboQuant Agent: loss-margin-low (#42)"
