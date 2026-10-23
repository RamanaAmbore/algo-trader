"""test_template_attach_persist_failure_hold.py

Covers audit finding T16 (2026-10-23 template/bracket audit, cluster 2D):
`_opp_persist_attached_gtts` runs AFTER broker placement; a DB write
error there used to log a WARNING only -- no alert, no
`template_attach_failed` event -- leaving live GTTs resting at the
broker while `attached_gtts_json` stayed NULL locally. That kept the
idempotency guard open: a redelivered postback, a reconcile sweep, or a
manual Retry-attach would all read "nothing attached" and place a
SECOND live set of exits on top of the first, now-untracked, one.

Fix: `_opp_persist_with_retry` retries the DB write a few times before
giving up. On exhaustion, `_opp_handle_persist_failure` holds the
unpersisted payload in `_TPL_ATTACH_PERSIST_PENDING` (in-process, keyed
by parent_row_id) and fires the SAME `template_attach_failed` alert
path a broker placement failure already uses -- with
`placed_untracked_ids` set so the rendered footer tells the operator
NOT to arm exits manually (they already exist at the broker).
`_fire_template_attach_on_fill` checks the hold before ever calling
`apply_template_to_order` again for the same row.
`_rco_attached_gtts_already_set` (the retry-template in-lock guard) and
`_retry_precheck_row` (the retry route's own precheck) both recognise a
pending hold too.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_persist_with_retry_succeeds_after_transient_failures(monkeypatch):
    from backend.api.routes import orders_place as opl

    sleeps: list[float] = []
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(side_effect=lambda s: sleeps.append(s)))

    call_count = {"n": 0}

    async def _fake_persist(row_id, attached, fill_price):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise RuntimeError("transient DB blip")

    with patch.object(opl, "_opp_persist_attached_gtts", new=_fake_persist):
        ok = await opl._opp_persist_with_retry(42, [{"id": "G1"}], 100.0)

    assert ok is True
    assert call_count["n"] == 3
    assert sleeps == [0.5, 1.0]


@pytest.mark.asyncio
async def test_persist_with_retry_exhausts_and_returns_false(monkeypatch):
    from backend.api.routes import orders_place as opl

    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    with patch.object(opl, "_opp_persist_attached_gtts",
                       new=AsyncMock(side_effect=RuntimeError("DB down"))):
        ok = await opl._opp_persist_with_retry(42, [{"id": "G1"}], 100.0)

    assert ok is False


@pytest.mark.asyncio
async def test_handle_persist_failure_holds_payload_and_fires_alert():
    """Core T16 regression: persist exhausted → a pending hold is
    recorded AND the template-attach-fail alert fires with the real
    broker ids so OCO/trailing/cancel-on-close don't track them, and
    the operator is told NOT to arm duplicates."""
    from backend.api.routes import orders_place as opl

    opl._TPL_ATTACH_PERSIST_PENDING.clear()
    attached = [{"kind": "gtt", "id": "G1", "label": "TP"},
                {"kind": "wing", "id": "W1"}]

    captured = {}

    def _fake_alert(*, order_id, symbol, account, errors, agent_id, placed_untracked_ids):
        captured["order_id"] = order_id
        captured["placed_untracked_ids"] = placed_untracked_ids
        captured["errors"] = errors

    with patch("backend.api.algo.template_attach._fire_attach_fail_alert",
               new=_fake_alert), \
         patch("backend.api.algo.order_events.write_event", new=AsyncMock()):
        await opl._opp_handle_persist_failure(
            777, attached, 100.0, "NIFTYFUT", "ZG0790", None,
        )

    assert 777 in opl._TPL_ATTACH_PERSIST_PENDING
    assert opl._TPL_ATTACH_PERSIST_PENDING[777]["attached"] == attached
    assert captured["order_id"] == 777
    assert set(captured["placed_untracked_ids"]) == {"G1", "W1"}
    opl._TPL_ATTACH_PERSIST_PENDING.clear()


@pytest.mark.asyncio
async def test_handle_persist_failure_survives_write_event_also_failing():
    """Even if the audit-log write ALSO fails (DB genuinely down), the
    alert must still have fired and the hold must still be set — no
    exception propagates to the caller."""
    from backend.api.routes import orders_place as opl

    opl._TPL_ATTACH_PERSIST_PENDING.clear()
    attached = [{"kind": "gtt", "id": "G1"}]

    with patch("backend.api.algo.template_attach._fire_attach_fail_alert") as mock_alert, \
         patch("backend.api.algo.order_events.write_event",
               new=AsyncMock(side_effect=RuntimeError("DB down"))):
        await opl._opp_handle_persist_failure(
            778, attached, 100.0, "NIFTYFUT", "ZG0790", None,
        )

    mock_alert.assert_called_once()
    assert 778 in opl._TPL_ATTACH_PERSIST_PENDING
    opl._TPL_ATTACH_PERSIST_PENDING.clear()


@pytest.mark.asyncio
async def test_flush_pending_persist_pops_on_success():
    from backend.api.routes import orders_place as opl

    opl._TPL_ATTACH_PERSIST_PENDING[901] = {
        "attached": [{"id": "G1"}], "fill_price": 100.0,
    }
    with patch.object(opl, "_opp_persist_attached_gtts", new=AsyncMock()):
        ok = await opl._opp_flush_pending_persist(901)

    assert ok is True
    assert 901 not in opl._TPL_ATTACH_PERSIST_PENDING


@pytest.mark.asyncio
async def test_flush_pending_persist_keeps_hold_on_repeat_failure():
    from backend.api.routes import orders_place as opl

    opl._TPL_ATTACH_PERSIST_PENDING[902] = {
        "attached": [{"id": "G1"}], "fill_price": 100.0,
    }
    with patch.object(opl, "_opp_persist_attached_gtts",
                       new=AsyncMock(side_effect=RuntimeError("still down"))):
        ok = await opl._opp_flush_pending_persist(902)

    assert ok is False
    assert 902 in opl._TPL_ATTACH_PERSIST_PENDING
    opl._TPL_ATTACH_PERSIST_PENDING.clear()


@pytest.mark.asyncio
async def test_flush_pending_persist_noop_when_nothing_held():
    from backend.api.routes import orders_place as opl

    opl._TPL_ATTACH_PERSIST_PENDING.clear()
    ok = await opl._opp_flush_pending_persist(999)
    assert ok is False


@pytest.mark.asyncio
async def test_fire_template_attach_on_fill_never_places_twice_when_persist_keeps_failing():
    """Core T16 regression (end-to-end through the real lock + pending
    hold): first call places exits at the broker but persist fails
    every attempt. A SECOND call for the SAME row (simulating a
    redelivered postback or reconcile sweep racing the hold) must NOT
    call apply_template_to_order again -- it must just retry the
    persist and return None.

    Pre-fix: this test fails because apply_template_to_order is
    awaited twice (the second call had no way to know exits were
    already placed, since attached_gtts_json was never written)."""
    from backend.api.routes import orders_place as opl

    opl._TPL_ATTACH_PERSIST_PENDING.clear()

    fake_result = MagicMock()
    fake_result.plan = MagicMock()
    fake_result.plan.gtts = []
    fake_result.gtt_ids = ["G1"]
    fake_result.wing_order_id = None
    fake_result.errors = []

    apply_mock = AsyncMock(return_value=fake_result)
    row_info = {"overrides": {}, "agent_id": None}

    async def _always_fail_persist(*a, **kw):
        raise RuntimeError("DB down")

    async def _no_sleep(_s):
        return None

    with patch("backend.api.algo.template_attach.apply_template_to_order",
               new=apply_mock), \
         patch.object(opl, "_opp_load_row_for_attach",
                       new=AsyncMock(return_value=row_info)), \
         patch.object(opl, "_opp_build_attach_entries",
                       return_value=[{"kind": "gtt", "id": "G1"}]), \
         patch.object(opl, "_opp_persist_attached_gtts",
                       new=_always_fail_persist), \
         patch("asyncio.sleep", new=_no_sleep), \
         patch("backend.api.algo.template_attach._fire_attach_fail_alert"), \
         patch("backend.api.algo.order_events.write_event", new=AsyncMock()), \
         patch("backend.api.algo.order_hold_gate.template_exit_held", return_value=False), \
         patch("backend.api.algo.order_hold_gate.template_exit_override", new=AsyncMock(return_value=None)):

        result1 = await opl._fire_template_attach_on_fill(
            parent_row_id=4242, parent_account="ZG0790",
            parent_symbol="NIFTYFUT", parent_exchange="NFO",
            parent_side="SELL", parent_qty=50, fill_price=100.0,
            template_id=1, mode="live",
        )
        assert apply_mock.await_count == 1
        assert 4242 in opl._TPL_ATTACH_PERSIST_PENDING

        result2 = await opl._fire_template_attach_on_fill(
            parent_row_id=4242, parent_account="ZG0790",
            parent_symbol="NIFTYFUT", parent_exchange="NFO",
            parent_side="SELL", parent_qty=50, fill_price=100.0,
            template_id=1, mode="live",
        )

    assert apply_mock.await_count == 1, (
        "apply_template_to_order must never run a second time for a row "
        "with a pending (unpersisted-but-placed) hold"
    )
    assert result2 is None
    opl._TPL_ATTACH_PERSIST_PENDING.clear()


class TestRetryPathRecognisesPendingHold:
    @pytest.mark.asyncio
    async def test_rco_attached_gtts_already_set_true_for_pending_row(self):
        from backend.api.routes import orders as orders_mod
        from backend.api.routes import orders_place as opl

        opl._TPL_ATTACH_PERSIST_PENDING[5151] = {
            "attached": [{"id": "G1"}], "fill_price": 100.0,
        }
        with patch.object(opl, "_opp_persist_attached_gtts", new=AsyncMock()):
            out = await orders_mod._rco_attached_gtts_already_set(5151)

        assert out is True
        opl._TPL_ATTACH_PERSIST_PENDING.clear()

    def test_retry_precheck_row_refuses_pending_row_with_clear_reason(self):
        from backend.api.routes import orders as orders_mod
        from backend.api.routes import orders_place as opl

        opl._TPL_ATTACH_PERSIST_PENDING[5152] = {
            "attached": [{"id": "G1"}], "fill_price": 100.0,
        }
        row = MagicMock()
        row.id = 5152
        row.template_id = 1
        row.parent_order_id = None
        row.attached_gtts_json = None
        row.status = "FILLED"
        row.mode = "live"

        out = orders_mod._retry_precheck_row(row)

        assert out is not None
        assert out["ok"] is False
        assert "not recorded" in out["reason"]
        opl._TPL_ATTACH_PERSIST_PENDING.clear()

    def test_retry_precheck_row_proceeds_when_nothing_pending(self):
        from backend.api.routes import orders as orders_mod
        from backend.api.routes import orders_place as opl

        opl._TPL_ATTACH_PERSIST_PENDING.clear()
        row = MagicMock()
        row.id = 5153
        row.template_id = 1
        row.parent_order_id = None
        row.attached_gtts_json = None
        row.status = "FILLED"
        row.mode = "live"

        out = orders_mod._retry_precheck_row(row)
        assert out is None


class TestAttachFailMessagePlacedUntrackedIds:
    def test_default_output_is_byte_identical_to_before(self):
        """No placed_untracked_ids -> footer text unchanged, protecting
        test_alert_golden.py's exact assertion."""
        from backend.api.algo.template_attach import template_attach_fail_message

        msg = template_attach_fail_message(
            order_id=1088, symbol="NIFTY26OCTFUT", account="ZG0790",
            err_summary="qty < lot_size", ist_label="Tue, Oct 06 2026, 14:30 IST",
        )
        assert "Parent order FILLED. Exits NOT attached." in msg
        assert "Arm exits manually if needed." in msg
        assert "PLACED at broker" not in msg

    def test_placed_untracked_ids_changes_footer_to_not_arm_manually(self):
        from backend.api.algo.template_attach import template_attach_fail_message

        msg = template_attach_fail_message(
            order_id=1088, symbol="NIFTY26OCTFUT", account="ZG0790",
            err_summary="DB persist failed", ist_label="Tue, Oct 06 2026, 14:30 IST",
            placed_untracked_ids=["G1", "W1"],
        )
        assert "PLACED at broker (ids G1, W1) but NOT recorded" in msg
        assert "Do NOT arm manually" in msg
        assert "Exits NOT attached" not in msg


@pytest.mark.asyncio
async def test_render_attach_fail_passes_placed_untracked_ids_through():
    from backend.api.algo import event_agents

    rec = {
        "extra": {
            "order_id": 1088, "symbol": "NIFTY26OCTFUT", "account": "ZG0790",
            "err_summary": "DB persist failed", "ist_label": "x",
            "placed_untracked_ids": ["G1"],
        },
    }
    title, plain, html, email = event_agents._render_attach_fail(rec)
    assert "PLACED at broker" in html
    assert "Do NOT arm manually" in html
