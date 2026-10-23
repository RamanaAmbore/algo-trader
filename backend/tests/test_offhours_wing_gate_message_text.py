"""test_offhours_wing_gate_message_text.py

Covers audit finding T25 (2026-10-23 template/bracket audit, cluster 2I):
`_check_offhours_wing_gate`'s error/alert text promised "attach
deferred" when the exchange is closed and the template has a wing leg.
No retry scheduler exists anywhere in this codebase for this path (only
a `TODO(#2)` placeholder elsewhere) -- TP/SL are ALSO dropped, not just
the wing (the caller returns this `AttachResult` immediately, before
TP/SL are ever built), and nothing ever retries. An operator reading
"deferred" would wait for something that will never happen.

Fix: the message now says the attach was REFUSED, and is explicit that
TP/SL/wing are all skipped. Text-only fix -- no retry scheduler added.
"""
from unittest.mock import MagicMock, patch


def test_offhours_wing_gate_message_says_refused_not_deferred():
    """Core T25 regression: the word 'deferred' must not appear, and the
    message must say the attach was refused."""
    from backend.api.algo.template_attach import _check_offhours_wing_gate

    template_with_wing = {"wing_strike_offset": 0}

    with patch("backend.api.algo.agent_engine._symbol_exchange_open",
               return_value=False), \
         patch("backend.api.algo.agent_engine._build_now_ctx", return_value={}), \
         patch("backend.api.algo.template_attach._fire_attach_fail_alert"):
        early_result, note = _check_offhours_wing_gate(
            apply_path="live", parent_exchange="NFO",
            template=template_with_wing, parent_lot_size=1,
            parent_account="ZG0790", parent_symbol="NIFTY25JUL24000CE",
            parent_side="SELL", parent_qty=50, parent_fill_price=100.0,
            parent_order_id=1, parent_agent_id=None,
        )

    assert early_result is not None
    assert len(early_result.errors) == 1
    msg = early_result.errors[0]
    assert "deferred" not in msg.lower()
    assert "refused" in msg.lower()


def test_offhours_wing_gate_message_mentions_tp_sl_also_skipped():
    """The message must be explicit that TP/SL are skipped too, not
    just the wing -- the pre-fix wording only mentioned the wing leg,
    implying TP/SL might still be attached (they are not; the whole
    plan is refused)."""
    from backend.api.algo.template_attach import _check_offhours_wing_gate

    template_with_wing = {"wing_strike_offset": 0}

    with patch("backend.api.algo.agent_engine._symbol_exchange_open",
               return_value=False), \
         patch("backend.api.algo.agent_engine._build_now_ctx", return_value={}), \
         patch("backend.api.algo.template_attach._fire_attach_fail_alert"):
        early_result, _ = _check_offhours_wing_gate(
            apply_path="live", parent_exchange="NFO",
            template=template_with_wing, parent_lot_size=1,
            parent_account="ZG0790", parent_symbol="NIFTY25JUL24000CE",
            parent_side="SELL", parent_qty=50, parent_fill_price=100.0,
            parent_order_id=1, parent_agent_id=None,
        )

    msg = early_result.errors[0]
    assert "TP/SL" in msg or ("tp" in msg.lower() and "sl" in msg.lower())
    assert "nothing will retry" in msg.lower() or "no retry" in msg.lower()


def test_offhours_wing_gate_alert_also_carries_refused_wording():
    """The fired alert (not just the in-memory AttachResult.errors) must
    carry the corrected text too -- _fire_attach_fail_alert is called
    with the same err_msg string."""
    from backend.api.algo.template_attach import _check_offhours_wing_gate

    template_with_wing = {"wing_strike_offset": 0}
    captured = {}

    def _fake_alert(*, order_id, symbol, account, errors, agent_id):
        captured["errors"] = errors

    with patch("backend.api.algo.agent_engine._symbol_exchange_open",
               return_value=False), \
         patch("backend.api.algo.agent_engine._build_now_ctx", return_value={}), \
         patch("backend.api.algo.template_attach._fire_attach_fail_alert",
               new=_fake_alert):
        _check_offhours_wing_gate(
            apply_path="live", parent_exchange="NFO",
            template=template_with_wing, parent_lot_size=1,
            parent_account="ZG0790", parent_symbol="NIFTY25JUL24000CE",
            parent_side="SELL", parent_qty=50, parent_fill_price=100.0,
            parent_order_id=1, parent_agent_id=None,
        )

    assert "deferred" not in captured["errors"][0].lower()
    assert "refused" in captured["errors"][0].lower()


def test_offhours_wing_gate_no_wing_template_still_returns_activate_note():
    """A template with NO wing leg still gets the (unrelated, correct)
    'will activate at next session open' note for GTT-only plans --
    this path is untouched by the T25 fix and must keep working."""
    from backend.api.algo.template_attach import _check_offhours_wing_gate

    template_no_wing = {"tp_pct": 30.0}

    with patch("backend.api.algo.agent_engine._symbol_exchange_open",
               return_value=False), \
         patch("backend.api.algo.agent_engine._build_now_ctx", return_value={}):
        early_result, note = _check_offhours_wing_gate(
            apply_path="live", parent_exchange="NFO",
            template=template_no_wing, parent_lot_size=1,
            parent_account="ZG0790", parent_symbol="NIFTY25JUL24000CE",
            parent_side="SELL", parent_qty=50, parent_fill_price=100.0,
            parent_order_id=1, parent_agent_id=None,
        )

    assert early_result is None
    assert note is not None and "next session open" in note
