"""
Tests for Part 2's backend wiring in `backend/api/algo/events.py` — a
channel's `message` value carrying a `{"$given": "<name>"}` marker
(parallel to `actions.py`'s existing `{"$row": "<name>"}` marker),
resolved against `context["given"][agent.id]` — the per-agent bindings
dict `agent_engine._cycle_evaluate_given_bindings` computes once per
tick, before this agent's condition is evaluated.

Covers:
  - `_resolve_given_message()` as a pure function: passthrough for a
    plain string, resolves a marker to the real bound value, falls back
    to None (-> default condition-text message) when the name is
    unresolved this tick.
  - `dispatch()` end-to-end: a channel's `message: {"$given": "msg_txt"}`
    renders the resolved text in its own custom-message record, given a
    populated `context["given"][agent.id]`; an unresolved marker falls
    back to the ordinary messageless/default group instead of crashing
    or sending a blank message.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ═══════════════════════════════════════════════════════════════════════
#  _resolve_given_message — pure function
# ═══════════════════════════════════════════════════════════════════════

class TestResolveGivenMessage:
    def test_plain_string_passes_through_unchanged(self):
        from backend.api.algo.events import _resolve_given_message
        agent = MagicMock(id=1, slug="a1")
        context = {"given": {1: {"msg_txt": "irrelevant"}}}
        assert _resolve_given_message("custom ntfy text", agent, context) == "custom ntfy text"

    def test_none_value_passes_through_unchanged(self):
        from backend.api.algo.events import _resolve_given_message
        agent = MagicMock(id=1, slug="a1")
        assert _resolve_given_message(None, agent, {}) is None

    def test_row_ref_marker_is_not_mistaken_for_given_marker(self):
        from backend.api.algo.events import _resolve_given_message
        agent = MagicMock(id=1, slug="a1")
        marker = {"$row": "symbol"}
        assert _resolve_given_message(marker, agent, {}) is marker

    def test_resolves_given_marker_to_bound_value(self):
        from backend.api.algo.events import _resolve_given_message
        agent = MagicMock(id=1, slug="a1")
        context = {"given": {1: {"msg_txt": "Loss: -60000 on NIFTY"}}}
        resolved = _resolve_given_message({"$given": "msg_txt"}, agent, context)
        assert resolved == "Loss: -60000 on NIFTY"

    def test_resolves_whole_number_float_binding_without_trailing_zero(self):
        """Same whole-number formatting rule as agent_engine's
        `_given_format_part_value` — a bare operator-expression binding
        used directly as `msg=` (no parts/concatenation at all) must
        format identically to the same value threaded through a
        `parts`-array binding (2026-10 formatting-consistency fix)."""
        from backend.api.algo.events import _resolve_given_message
        agent = MagicMock(id=1, slug="a1")
        context = {"given": {1: {"p": -60000.0}}}
        resolved = _resolve_given_message({"$given": "p"}, agent, context)
        assert resolved == "-60000"

    def test_resolves_fractional_float_binding_with_normal_str(self):
        from backend.api.algo.events import _resolve_given_message
        agent = MagicMock(id=1, slug="a1")
        context = {"given": {1: {"p": -60000.5}}}
        resolved = _resolve_given_message({"$given": "p"}, agent, context)
        assert resolved == "-60000.5"

    def test_unresolved_name_returns_none(self):
        from backend.api.algo.events import _resolve_given_message
        agent = MagicMock(id=1, slug="a1")
        context = {"given": {1: {}}}
        assert _resolve_given_message({"$given": "never_defined"}, agent, context) is None

    def test_binding_resolved_to_none_also_returns_none(self):
        from backend.api.algo.events import _resolve_given_message
        agent = MagicMock(id=1, slug="a1")
        context = {"given": {1: {"p": None}}}
        assert _resolve_given_message({"$given": "p"}, agent, context) is None

    def test_missing_context_entirely_returns_none(self):
        from backend.api.algo.events import _resolve_given_message
        agent = MagicMock(id=1, slug="a1")
        assert _resolve_given_message({"$given": "p"}, agent, None) is None

    def test_agent_id_mismatch_returns_none(self):
        from backend.api.algo.events import _resolve_given_message
        agent = MagicMock(id=2, slug="a2")   # no entry for id=2
        context = {"given": {1: {"p": 5.0}}}
        assert _resolve_given_message({"$given": "p"}, agent, context) is None

    def test_resolved_marker_value_is_masked(self):
        """Masking lives HERE (not in agent_engine._given_resolve_parts,
        which stores the raw value since actions.py:resolve_given_ref_params
        also reads that same bindings dict for a live DO-action param) —
        this is the ONE shared resolver every `$given` message marker
        goes through, on EVERY channel (log/websocket/inapp via the
        ordinary dispatch() path, telegram/email/ntfy via either the
        ordinary path or the rich-alert override). Verified directly
        here so a regression can't hide behind
        `_v2_rich_alert_custom_message`'s own (now redundant-but-
        harmless) outer mask."""
        from backend.api.algo.events import _resolve_given_message
        from backend.shared.helpers.utils import mask_account
        agent = MagicMock(id=1, slug="a1")
        context = {"given": {1: {"m": "Loss on ZG0790"}}}
        resolved = _resolve_given_message({"$given": "m"}, agent, context)
        assert "ZG0790" not in resolved
        assert mask_account("ZG0790") in resolved


# ═══════════════════════════════════════════════════════════════════════
#  dispatch() end-to-end
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_dispatch_resolves_given_message_marker_into_custom_record():
    """A channel's `message: {"$given": "msg_txt"}` renders the real
    bound text in its OWN custom-message record — mirrors the existing
    `test_dispatch_splits_custom_message_channels_into_their_own_records`
    shape exactly, but with a GIVEN marker instead of a literal string."""
    from backend.api.algo import events
    from backend.api.algo.events import dispatch, EvalResult

    agent = MagicMock()
    agent.id = 1
    agent.name = "Test Agent"
    agent.slug = "test-agent"
    agent.tier = "medium"
    agent.events = [
        {"channel": "telegram", "enabled": True},
        {"channel": "ntfy", "enabled": True, "message": {"$given": "msg_txt"}},
    ]
    eval_result = EvalResult(triggered=True, condition_text="pnl > 1000", detail={})
    context = {"given": {1: {"msg_txt": "Loss: -60000 on NIFTY"}}}

    info_calls = []
    with patch.object(events, "_RECORDED_CHANNELS", frozenset({"telegram", "email", "ntfy", "log"})), \
         patch('backend.api.algo.template_registry.resolve_events', return_value=agent.events), \
         patch('backend.api.algo.events._log_event', new_callable=AsyncMock), \
         patch.object(events.logger, "info", side_effect=lambda msg, **kw: info_calls.append((msg, kw.get("extra")))):
        await dispatch(agent, eval_result, sim_mode=False, context=context)

    assert len(info_calls) == 2
    shared = next(extra for _, extra in info_calls if len(extra["channels"]) == 1
                  and extra["channels"][0]["channel"] == "telegram")
    custom = next(extra for _, extra in info_calls if extra["channels"][0]["channel"] == "ntfy")

    assert custom["channels"][0]["message"] == "Loss: -60000 on NIFTY"
    assert "Condition: Loss: -60000 on NIFTY" in custom["ntfy_body"]
    assert "pnl > 1000" not in custom["ntfy_body"]
    assert "Condition: pnl > 1000" in shared["telegram_body"]


@pytest.mark.asyncio
async def test_dispatch_masks_given_marker_on_log_channel():
    """Proves the masking-relocation claim end-to-end for a channel the
    rich-alert override NEVER touches: `log` (and, by the same code
    path, websocket/inapp) always runs through the ordinary dispatch()
    path, never `_v2_rich_alert_custom_message`. If masking had stayed
    in `agent_engine._given_resolve_parts` (or were removed from
    `_resolve_given_message` entirely), this channel would leak the
    real account code — this test fails if that regression ever
    reoccurs."""
    from backend.api.algo import events
    from backend.api.algo.events import dispatch, EvalResult
    from backend.shared.helpers.utils import mask_account

    agent = MagicMock()
    agent.id = 1
    agent.name = "Test Agent"
    agent.slug = "test-agent"
    agent.tier = "medium"
    agent.events = [
        {"channel": "log", "enabled": True, "message": {"$given": "m"}},
    ]
    eval_result = EvalResult(triggered=True, condition_text="pnl > 1000", detail={})
    context = {"given": {1: {"m": "Loss on ZG0790"}}}

    info_calls = []
    with patch.object(events, "_RECORDED_CHANNELS", frozenset({"telegram", "email", "ntfy", "log"})), \
         patch('backend.api.algo.template_registry.resolve_events', return_value=agent.events), \
         patch('backend.api.algo.events._log_event', new_callable=AsyncMock), \
         patch.object(events.logger, "info", side_effect=lambda msg, **kw: info_calls.append((msg, kw.get("extra")))):
        await dispatch(agent, eval_result, sim_mode=False, context=context)

    assert len(info_calls) == 1
    _, extra = info_calls[0]
    log_line = extra["channels"][0]["log_line"]
    assert "ZG0790" not in log_line
    assert mask_account("ZG0790") in log_line


@pytest.mark.asyncio
async def test_dispatch_unresolved_given_marker_falls_back_to_default_group():
    """When the referenced GIVEN name never resolved this tick (binding
    failed/absent), the channel must NOT send a broken/blank message —
    it falls back to the ordinary messageless/condition-text group,
    same shape as `test_dispatch_treats_empty_string_message_as_no_message`."""
    from backend.api.algo import events
    from backend.api.algo.events import dispatch, EvalResult

    agent = MagicMock()
    agent.id = 1
    agent.name = "Test Agent"
    agent.slug = "test-agent"
    agent.tier = "medium"
    agent.events = [
        {"channel": "telegram", "enabled": True, "message": {"$given": "never_defined"}},
    ]
    eval_result = EvalResult(triggered=True, condition_text="pnl > 1000", detail={})
    context = {"given": {1: {}}}   # binding never resolved this tick

    info_calls = []
    with patch.object(events, "_RECORDED_CHANNELS", frozenset({"telegram", "email", "ntfy", "log"})), \
         patch('backend.api.algo.template_registry.resolve_events', return_value=agent.events), \
         patch('backend.api.algo.events._log_event', new_callable=AsyncMock), \
         patch.object(events.logger, "info", side_effect=lambda msg, **kw: info_calls.append((msg, kw.get("extra")))):
        await dispatch(agent, eval_result, sim_mode=False, context=context)

    assert len(info_calls) == 1, "unresolved $given must fall back to the shared/default group"
    _, extra = info_calls[0]
    assert "message" not in extra["channels"][0]
    assert "Condition: pnl > 1000" in extra["telegram_body"]


@pytest.mark.asyncio
async def test_dispatch_with_no_context_kwarg_still_works_for_plain_messages():
    """Backward compatibility — every existing caller that doesn't pass
    `context=` (or passes None) must behave exactly as before for a
    PLAIN string message (no $given marker involved at all)."""
    from backend.api.algo import events
    from backend.api.algo.events import dispatch, EvalResult

    agent = MagicMock()
    agent.id = 1
    agent.name = "Test Agent"
    agent.slug = "test-agent"
    agent.tier = "medium"
    agent.events = [
        {"channel": "ntfy", "enabled": True, "message": "custom ntfy text"},
    ]
    eval_result = EvalResult(triggered=True, condition_text="pnl > 1000", detail={})

    info_calls = []
    with patch.object(events, "_RECORDED_CHANNELS", frozenset({"telegram", "email", "ntfy", "log"})), \
         patch('backend.api.algo.template_registry.resolve_events', return_value=agent.events), \
         patch('backend.api.algo.events._log_event', new_callable=AsyncMock), \
         patch.object(events.logger, "info", side_effect=lambda msg, **kw: info_calls.append((msg, kw.get("extra")))):
        await dispatch(agent, eval_result, sim_mode=False)   # no context kwarg at all

    assert len(info_calls) == 1
    _, extra = info_calls[0]
    assert extra["channels"][0]["message"] == "custom ntfy text"
