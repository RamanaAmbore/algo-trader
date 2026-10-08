"""
Fix 1 — releasing a held Bracket (template-exit) order must actually place
the exit GTTs, not silently re-hold itself while still reporting success.

`release_template_exit` cleared `hold_json`, committed, then called
`_fire_template_attach_on_fill`. That function's own gate re-checked
`template_exit_held(await template_exit_override(parent_row_id))` against
the SAME still-held global switch / per-order override — so it immediately
re-held the order and returned, while `release_template_exit` still
returned a hardcoded `{"ok": True, "status": "FILLED"}` regardless.

These tests exercise the REAL `release_template_exit` ->
`_fire_template_attach_on_fill` call chain (not a mock of
`release_template_exit` itself) — the existing tests in
test_order_hold_repeated_rejection_release.py only mock
`release_template_exit`, which is why nothing caught this bug.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.api.algo.order_hold import HoldCategory, hold_record
from backend.api.algo.template_attach import AttachResult, GttSpec, TemplatePlan


def _mock_session(row):
    _result = MagicMock()
    _result.scalar_one_or_none.return_value = row

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=_result)
    mock_session.commit = AsyncMock()
    return mock_session


def _held_template_exit_row(**extra) -> SimpleNamespace:
    base = dict(
        id=901, status="FILLED",
        hold_json=hold_record(
            HoldCategory.TEMPLATE_EXIT, "template exits held until released",
            "n/a", None, datetime.now(timezone.utc),
        ),
        detail="", account="ZG0790", symbol="NIFTY24APR25000CE", exchange="NFO",
        transaction_type="BUY", quantity=50, product="NRML",
        attached_gtts_json=None, template_overrides_json=None,
        fill_price=100.0, template_id=1, mode="live",
    )
    base.update(extra)
    return SimpleNamespace(**base)


def _attach_result_with_gtt(gtt_id: str = "GTT-001") -> AttachResult:
    plan = TemplatePlan(
        template_id=1, template_name="Test TP/SL", template_slug=None,
        parent_account="ZG0790", parent_symbol="NIFTY24APR25000CE",
        parent_side="BUY", parent_qty=50, parent_exchange="NFO",
        parent_fill_price=100.0,
        gtts=[GttSpec(trigger_type="single", trigger_values=[120.0],
                      orders=[{}], label="TP", placed_id=gtt_id)],
    )
    return AttachResult(plan=plan, gtt_ids=[gtt_id], sibling_pairs=[])


def _attach_result_empty() -> AttachResult:
    plan = TemplatePlan(
        template_id=1, template_name="Test TP/SL", template_slug=None,
        parent_account="ZG0790", parent_symbol="NIFTY24APR25000CE",
        parent_side="BUY", parent_qty=50, parent_exchange="NFO",
        parent_fill_price=100.0, gtts=[],
    )
    return AttachResult(plan=plan, gtt_ids=[], sibling_pairs=[])


@pytest.mark.asyncio
async def test_release_template_exit_places_real_gtt_and_does_not_rehold():
    """The core regression: release must reach apply_template_to_order and
    persist the real GTT id, and must NEVER re-consult the hold gate."""
    from backend.api.algo import order_release as m

    row = _held_template_exit_row()
    mock_session = _mock_session(row)
    fake_result = _attach_result_with_gtt("GTT-001")

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.api.algo.order_events.write_event", new_callable=AsyncMock), \
         patch("backend.api.algo.template_attach.apply_template_to_order",
               new_callable=AsyncMock, return_value=fake_result) as mock_apply, \
         patch("backend.api.algo.order_hold_gate.template_exit_held",
               return_value=True) as mock_held, \
         patch("backend.api.algo.order_hold_gate.template_exit_override",
               new_callable=AsyncMock) as mock_override, \
         patch("backend.api.algo.order_hold_gate.hold_template_exit",
               new_callable=AsyncMock) as mock_hold_again:
        result = await m.release_template_exit(901, actor="operator")

    assert result["ok"] is True
    assert result["status"] == "FILLED"
    mock_apply.assert_called_once()

    # The gate must never be re-consulted on the release (bypass_hold) path —
    # not "happens to return False this time," but genuinely never called.
    mock_held.assert_not_called()
    mock_override.assert_not_called()
    mock_hold_again.assert_not_called()

    assert row.hold_json is None
    assert row.attached_gtts_json is not None
    assert "GTT-001" in row.attached_gtts_json


@pytest.mark.asyncio
async def test_release_template_exit_restores_hold_when_nothing_placed():
    """If the attach genuinely placed nothing (empty gtt_ids, no wing),
    `ok` must be False and the hold must be restored so the row stays
    retryable in HeldOrdersCard instead of silently vanishing."""
    from backend.api.algo import order_release as m

    row = _held_template_exit_row()
    mock_session = _mock_session(row)
    empty_result = _attach_result_empty()

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.api.algo.order_events.write_event", new_callable=AsyncMock), \
         patch("backend.api.algo.template_attach.apply_template_to_order",
               new_callable=AsyncMock, return_value=empty_result), \
         patch("backend.api.algo.order_hold_gate.hold_template_exit",
               new_callable=AsyncMock) as mock_hold_again:
        result = await m.release_template_exit(901, actor="operator")

    assert result["ok"] is False
    mock_hold_again.assert_called_once()


@pytest.mark.asyncio
async def test_release_template_exit_restores_hold_when_attach_returns_none():
    """apply_template_to_order returning None (e.g. a guard-alert fire)
    must also be treated as a non-success, with the hold restored."""
    from backend.api.algo import order_release as m

    row = _held_template_exit_row()
    mock_session = _mock_session(row)

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.api.algo.order_events.write_event", new_callable=AsyncMock), \
         patch("backend.api.algo.template_attach.apply_template_to_order",
               new_callable=AsyncMock, return_value=None), \
         patch("backend.api.algo.order_hold_gate.hold_template_exit",
               new_callable=AsyncMock) as mock_hold_again:
        result = await m.release_template_exit(901, actor="operator")

    assert result["ok"] is False
    mock_hold_again.assert_called_once()


@pytest.mark.asyncio
async def test_release_template_exit_never_places_broker_order_for_non_live_mode():
    """bypass_hold only skips the hold re-check — it must NEVER skip the
    mode != 'live' gate. A paper/sim/replay/shadow row must still place
    no real broker GTTs on release."""
    from backend.api.algo import order_release as m

    row = _held_template_exit_row(mode="paper")
    mock_session = _mock_session(row)

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.api.algo.order_events.write_event", new_callable=AsyncMock), \
         patch("backend.api.algo.template_attach.apply_template_to_order",
               new_callable=AsyncMock) as mock_apply, \
         patch("backend.api.algo.order_hold_gate.hold_template_exit",
               new_callable=AsyncMock) as mock_hold_again:
        result = await m.release_template_exit(901, actor="operator")

    assert result["ok"] is False
    mock_apply.assert_not_called()
    mock_hold_again.assert_called_once()


@pytest.mark.asyncio
async def test_release_template_exit_refuses_non_template_exit_category():
    from backend.api.algo import order_release as m

    row = _held_template_exit_row(hold_json=hold_record(
        HoldCategory.EXPIRY_CLOSE, "expiry close", "CHASE_MED", None,
        datetime.now(timezone.utc),
    ))
    mock_session = _mock_session(row)

    with patch("backend.api.database.async_session", return_value=mock_session):
        result = await m.release_template_exit(901, actor="operator")

    assert result["ok"] is False
    assert "no held template exits" in result["reason"]


@pytest.mark.asyncio
async def test_release_template_exit_short_circuits_when_already_attached():
    from backend.api.algo import order_release as m

    row = _held_template_exit_row(attached_gtts_json='[{"kind": "gtt", "id": "GTT-OLD"}]')
    mock_session = _mock_session(row)

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.api.algo.template_attach.apply_template_to_order",
               new_callable=AsyncMock) as mock_apply:
        result = await m.release_template_exit(901, actor="operator")

    assert result["ok"] is True
    assert result["reason"] == "exits already attached"
    assert row.hold_json is None
    mock_apply.assert_not_called()
