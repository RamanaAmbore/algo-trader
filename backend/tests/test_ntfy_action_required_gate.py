"""
Tests for the 2026-10-23 binary ntfy gate (chase robustness fix #4):
`event_agents.py:_apply_action_required_ntfy_gate` /
`_dispatch_agent`'s channel loop, plus the two source-side flags
(`fill_notify.notify_fills`, `alert_utils._send_order_failure_messages`).

Rule: ntfy is exclusively for "operator must take action" — every
action-required outcome is urgent ntfy; every pure-FYI outcome never
touches ntfy at all, regardless of how the dispatching agent's own
STORED channel list happens to be configured (important: `load_agents()`
reads from the `agents` DB table, so an agent row seeded before this fix
could still carry a stale `{"channel": "ntfy", "enabled": True}` entry
with no priority — the gate must override that, not just the seed dicts).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import backend.api.algo.event_agents as ea


def test_action_required_false_skips_ntfy_channel():
    ch = {"channel": "ntfy", "enabled": True, "priority": "high"}
    out = ea._apply_action_required_ntfy_gate(ch, False)
    assert out is None


def test_action_required_false_skips_even_a_fully_configured_stale_db_row():
    """Simulates an agent's STORED channel list (from the `agents` DB
    table, possibly seeded before this fix and never resynced) still
    carrying a plain enabled ntfy entry with no explicit priority — the
    gate must still skip it for a pure-FYI record."""
    stale_db_channel = {"channel": "ntfy", "enabled": True}
    out = ea._apply_action_required_ntfy_gate(stale_db_channel, False)
    assert out is None


def test_action_required_true_forces_urgent_priority():
    ch = {"channel": "ntfy", "enabled": True}
    out = ea._apply_action_required_ntfy_gate(ch, True)
    assert out["priority"] == "urgent"


def test_action_required_true_overrides_an_existing_lower_priority():
    ch = {"channel": "ntfy", "enabled": True, "priority": "low"}
    out = ea._apply_action_required_ntfy_gate(ch, True)
    assert out["priority"] == "urgent"


def test_action_required_absent_leaves_channel_unchanged():
    """Every record type not yet tagged (ticker/gtt/deploy/etc.) must
    see byte-for-byte unchanged behavior."""
    ch = {"channel": "ntfy", "enabled": True, "priority": "high"}
    out = ea._apply_action_required_ntfy_gate(ch, None)
    assert out == ch


def test_non_ntfy_channel_always_passes_through():
    ch = {"channel": "telegram", "enabled": True}
    assert ea._apply_action_required_ntfy_gate(ch, False) == ch
    assert ea._apply_action_required_ntfy_gate(ch, True) == ch
    assert ea._apply_action_required_ntfy_gate(ch, None) == ch


# ── 2026-10 forced-email gate (operator requirement #5: "action-required
#    alerts go to both ntfy and email") ──────────────────────────────────

def test_email_gate_appends_entry_when_action_required_true_and_no_email_present():
    channels = [{"channel": "ntfy", "enabled": True, "priority": "urgent"}]
    out = ea._apply_action_required_email_gate(channels, True)
    assert out == [
        {"channel": "ntfy", "enabled": True, "priority": "urgent"},
        {"channel": "email", "enabled": True, "gate": False},
    ]
    # Original list is untouched (a new list is returned).
    assert channels == [{"channel": "ntfy", "enabled": True, "priority": "urgent"}]


def test_email_gate_never_duplicates_an_existing_email_entry():
    """`order-failure-alert`'s own static per-slug list already carries
    an email entry — the gate must leave it alone, not add a second."""
    channels = [
        {"channel": "telegram", "enabled": True, "gate": False},
        {"channel": "email", "enabled": True, "gate": False},
    ]
    out = ea._apply_action_required_email_gate(channels, True)
    assert out == channels
    assert sum(1 for c in out if c.get("channel") == "email") == 1


def test_email_gate_is_a_noop_for_false_and_none():
    channels = [{"channel": "ntfy", "enabled": True}]
    assert ea._apply_action_required_email_gate(channels, False) == channels
    assert ea._apply_action_required_email_gate(channels, None) == channels
    # No email entry was ever added for a pure-FYI or untagged record.
    assert all(c.get("channel") != "email" for c in ea._apply_action_required_email_gate(channels, False))
    assert all(c.get("channel") != "email" for c in ea._apply_action_required_email_gate(channels, None))


def test_email_gate_never_touches_other_channels():
    channels = [
        {"channel": "telegram", "enabled": True},
        {"channel": "telegram_info", "enabled": True},
        {"channel": "log", "enabled": True},
    ]
    out = ea._apply_action_required_email_gate(channels, True)
    assert out[:3] == channels
    assert out[3] == {"channel": "email", "enabled": True, "gate": False}


@pytest.mark.asyncio
async def test_chase_cancel_and_order_correction_actually_reach_email_via_send_channel():
    """Integration: `_render_chase_cancel`/`_render_order_correction`
    (the two real renderers this gate exists for) return bare 2-tuples
    with no structured email payload — `_send_channel`'s own default
    `<pre>`-wrapped fallback must still deliver a real email body, not
    a `None` that `_send_email_channel` silently no-ops on."""
    from unittest.mock import AsyncMock as _AsyncMock
    import backend.shared.helpers.utils as u

    sent_email = []
    with patch.object(u, "is_enabled", lambda cap: True), \
         patch.dict(ea.CHANNELS, {"email": ("mail", lambda t, b, tg=None, **k: sent_email.append((t, b, k.get("email"))))}):
        out = ("Order corrected: NIFTY #1", "AlgoOrder #1 corrected OPEN -> FILLED")
        agent = MagicMock(slug="order-correction-alert", tier="urgent")
        channels = ea._apply_action_required_email_gate(
            [{"channel": "ntfy", "enabled": True}], True,
        )
        email_ch = next(c for c in channels if c["channel"] == "email")
        ok = await ea._send_channel(email_ch, out, agent)
    assert ok
    assert len(sent_email) == 1
    title, body, email_payload = sent_email[0]
    assert title == "Order corrected: NIFTY #1"
    assert email_payload is not None, "a default (subject, html_body) pair must be synthesized"
    subject, html_body = email_payload
    assert "Order corrected: NIFTY #1" in subject
    assert "AlgoOrder #1 corrected OPEN -&gt; FILLED" in html_body, (
        "body is html.escape()'d (the '>' in 'OPEN -> FILLED' becomes '&gt;') before being wrapped in <pre>"
    )
    assert "<pre" in html_body


# ── Integration: _dispatch_agent actually skips/forces via the record ──

@pytest.mark.asyncio
async def test_dispatch_agent_skips_ntfy_for_fyi_record_with_stale_enabled_channel():
    agent = MagicMock()
    agent.slug = "fill-alert"
    agent.tier = "medium"
    agent.conditions = {}
    agent.actions = [{"type": "render", "render": "fill"}]
    # Simulates a NOT-YET-RESYNCED DB row for FILL_AGENT, still carrying
    # an enabled ntfy channel (exactly the risk this gate must defend
    # against independent of the seed dict).
    agent.events = [{"channel": "telegram", "enabled": True},
                    {"channel": "ntfy", "enabled": True}]

    rec = {"extra": {"action_required": False}, "logger": "x", "message": "m"}

    with patch.object(ea, "evaluate", return_value=True), \
         patch.object(ea, "RENDERS", {ea._render_key(agent): lambda r: ("T", "B")}), \
         patch.object(ea, "_send_channel", new=AsyncMock(return_value=True)) as mock_send:
        sent = await ea._dispatch_agent(agent, [rec])

    assert sent == 1  # only telegram sent
    sent_channels = [c.get("channel") for c, *_ in
                     [call.args for call in mock_send.call_args_list]]
    assert "ntfy" not in sent_channels


@pytest.mark.asyncio
async def test_dispatch_agent_forces_urgent_ntfy_for_action_required_record():
    agent = MagicMock()
    agent.slug = "order-failure-alert"
    agent.tier = "medium"
    agent.conditions = {}
    agent.actions = [{"type": "render", "render": "order_failure"}]
    agent.events = [{"channel": "ntfy", "enabled": True}]  # no explicit priority

    rec = {"extra": {"action_required": True}, "logger": "x", "message": "m"}

    with patch.object(ea, "evaluate", return_value=True), \
         patch.object(ea, "RENDERS", {ea._render_key(agent): lambda r: ("T", "B")}), \
         patch.object(ea, "_send_channel", new=AsyncMock(return_value=True)) as mock_send:
        sent = await ea._dispatch_agent(agent, [rec])

    # 2026-10 forced-email gate: an action-required record with no
    # `email` entry already present gets one synthesized
    # (`_apply_action_required_email_gate`) ALONGSIDE the forced-urgent
    # ntfy entry — "action-required alerts go to both ntfy and email"
    # (operator requirement #5). `sent == 1` (ntfy only) was this test's
    # value before that gate existed.
    assert sent == 2
    sent_channels = [c.args[0] for c in mock_send.call_args_list]
    ntfy_ch = next(c for c in sent_channels if c["channel"] == "ntfy")
    assert ntfy_ch["priority"] == "urgent"
    email_ch = next(c for c in sent_channels if c["channel"] == "email")
    assert email_ch["enabled"] is True


# ── Source-side flags ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_notify_fills_logs_action_required_false():
    from backend.api.algo.fill_notify import notify_fills

    row = MagicMock()
    row.id = 1
    row.account = "ZG0790"
    row.symbol = "NIFTY25JULFUT"
    row.exchange = "NFO"
    row.transaction_type = "BUY"
    row.quantity = 50
    row.fill_price = 100.0
    row.product = "NRML"
    row.mode = "live"
    row.agent_id = None

    with patch("backend.api.algo.fill_notify.logger") as mock_logger:
        await notify_fills([row])

    _, kwargs = mock_logger.info.call_args
    assert kwargs["extra"]["action_required"] is False


def test_send_order_failure_messages_logs_action_required_true():
    from backend.shared.helpers.alert_utils import _send_order_failure_messages

    with patch("backend.shared.helpers.alert_utils.logger") as mock_logger:
        _send_order_failure_messages(
            masked="ZG****0", symbol="NIFTY25JULFUT", exchange="NFO",
            side="BUY", qty=50, mode="live", source="chase",
            error="chase abandoned", suppressed_count=0, ist_disp="x",
        )

    _, kwargs = mock_logger.warning.call_args
    assert kwargs["extra"]["action_required"] is True


# ── fix #7 — bypass_gates ────────────────────────────────────────────────

def test_send_order_failure_alert_bypass_gates_skips_market_hours_and_dedup():
    from backend.shared.helpers import alert_utils as au

    with patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=False), \
         patch.object(au, "_send_order_failure_messages") as mock_send:
        au.send_order_failure_alert(
            account="ZG0790", symbol="NIFTY25JULFUT", exchange="NFO",
            side="BUY", qty=50, mode="live", source="chase",
            error="chase abandoned after 3 consecutive errors",
            bypass_gates=True,
        )

    mock_send.assert_called_once(), (
        "bypass_gates=True must send even when all markets are closed"
    )


def test_send_order_failure_alert_without_bypass_respects_market_hours_gate():
    from backend.shared.helpers import alert_utils as au

    with patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=False), \
         patch.object(au, "_send_order_failure_messages") as mock_send:
        au.send_order_failure_alert(
            account="ZG0790", symbol="NIFTY25JULFUT", exchange="NFO",
            side="BUY", qty=50, mode="live", source="chase",
            error="plain rejection",
        )

    mock_send.assert_not_called()
