"""
Bug 2 (2026-09-30 audit) — wing orders never recorded in
`attached_gtts_json` at fill time, risking a duplicate live wing order.

Investigation finding: `template_attach.py`'s OWN responsibility — placing
the wing leg and returning its broker order id on `AttachResult.wing_order_id`
— is already correct (`_ta_live_place_wing`, template_attach.py:1668-1683,
and the sim-mode equivalent at line ~1489). `TestApplyPlanLiveWingOrderId`
below confirms this passes today, with NO source change required.

The actual gap is entirely in `backend/api/routes/orders_place.py`'s
`_opp_build_attach_entries` (~line 579-639), which builds the
`attached_gtts_json` payload by iterating ONLY `result.plan.gtts` — it never
reads `result.wing_order_id` at all. For a wing-only plan (or when every GTT
fails but the wing succeeds), `attached` comes back `[]`; the caller's
`if attached:` guard (orders_place.py ~line 782) then skips
`_opp_persist_attached_gtts` entirely, so `AlgoOrder.attached_gtts_json`
stays NULL. The idempotency check `_opp_load_row_for_attach`
(`if _row.attached_gtts_json: return None`) then treats NULL as "never
attached" and lets a second trigger (chase + postback racing the same fill,
or an operator Re-attach click) place a SECOND live wing order.

The retry path already gets this right: `_retry_build_attached_payload`
(orders.py:308-325) appends `{"kind": "wing", "label": "Wing", "id":
result.wing_order_id}` whenever `result.wing_order_id` is set — the exact
shape `_opp_build_attach_entries` is missing.

FIXED (2026-09-30, same day, follow-up pass): `orders_place.py:
_opp_build_attach_entries` now appends a `{"kind": "wing", "label":
"Wing", "id": result.wing_order_id}` entry (mirroring
`_retry_build_attached_payload`) whenever `result.wing_order_id` is
truthy. `test_opp_build_attach_entries_includes_wing_order` below
asserts this directly and now passes (its `xfail(strict=True)` marker
was removed as part of landing the fix).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ─────────────────────────────────────────────────────────────────────────
# template_attach.py's own contract IS correct — confirms no fix is needed
# (or possible) on this side.
# ─────────────────────────────────────────────────────────────────────────

def _wing_only_plan():
    from backend.api.algo.template_attach import TemplatePlan, WingSpec
    wing = WingSpec(
        tradingsymbol="NIFTY25APR22200CE",
        transaction_type="BUY",
        quantity=10,
        exchange="NFO",
    )
    return TemplatePlan(
        template_id=11,
        template_name="Wing Only",
        template_slug="wing-only",
        parent_account="ACC1",
        parent_symbol="NIFTY25APR22000PE",
        parent_side="SELL",
        parent_qty=10,
        parent_exchange="NFO",
        parent_fill_price=100.0,
        parent_lot_size=1,
        gtts=[],          # deliberately empty — GTTs failed, or plan is wing-only
        wing=wing,
    )


class TestApplyPlanLiveWingOrderId:
    """Confirms `apply_plan_live` (template_attach.py) correctly returns
    the wing's broker order id on `AttachResult.wing_order_id` even when
    there are no GTTs at all — this half of the pipeline is NOT the bug."""

    def test_wing_only_plan_sets_wing_order_id_with_no_gtts(self):
        from backend.api.algo.template_attach import apply_plan_live

        plan = _wing_only_plan()
        mock_broker = MagicMock()
        mock_broker.broker_id = "zerodha_kite"
        mock_broker.capabilities.gtt_single = True
        mock_broker.translate_qty.side_effect = lambda e, q, ls: q
        mock_broker.place_order.return_value = "wing-order-999"

        with (
            __import__("unittest.mock", fromlist=["patch"]).patch(
                "backend.api.algo.agent_engine._symbol_exchange_open",
                return_value=True,
            ),
            __import__("unittest.mock", fromlist=["patch"]).patch(
                "backend.api.algo.agent_engine._build_now_ctx",
                return_value={},
            ),
        ):
            result = apply_plan_live(plan, mock_broker)

        assert result.gtt_ids == []
        assert result.wing_order_id == "wing-order-999"
        assert mock_broker.place_order.call_count == 1
        assert not mock_broker.place_gtt.called


# ─────────────────────────────────────────────────────────────────────────
# The actual gap — orders_place.py:_opp_build_attach_entries (out of scope)
# ─────────────────────────────────────────────────────────────────────────

class _FakePlan:
    gtts: list = []
    parent_qty = 10
    parent_symbol = "NIFTY25APR22000PE"
    parent_exchange = "NFO"
    parent_account = "ACC1"


class _FakeAttachResult:
    """Minimal stand-in for `AttachResult` — a wing-only fill where every
    GTT failed (or none were planned) but the wing itself succeeded."""
    plan = _FakePlan()
    gtt_ids: list = []
    wing_order_id = "wing-order-999"
    sibling_pairs: list = []
    errors: list = []


def test_opp_build_attach_entries_includes_wing_order():
    # Fixed 2026-09-30 (same day) — _opp_build_attach_entries now appends
    # a {"kind": "wing", ...} entry when result.wing_order_id is set,
    # mirroring _retry_build_attached_payload's existing wing handling
    # (orders.py). xfail marker removed now that the fix has landed.
    from backend.api.routes.orders_place import _opp_build_attach_entries

    result = _FakeAttachResult()
    attached = _opp_build_attach_entries(
        result, fill_price=100.0, parent_side="SELL",
    )

    wing_entries = [e for e in attached if e.get("kind") == "wing"]
    assert len(wing_entries) == 1, (
        "attached_gtts_json must carry a wing entry when "
        "result.wing_order_id is set, even with zero GTTs — otherwise "
        "the idempotency check can't see the wing was already placed "
        "and a duplicate live wing order can be placed on a second "
        "trigger."
    )
    assert wing_entries[0]["id"] == "wing-order-999"
    assert wing_entries[0]["chased"] is False


class _FakeChasedAttachResult(_FakeAttachResult):
    """A wing-only plan where the LIMIT wing was handed to chase instead
    of placed directly — `wing_order_id` is the "chase" sentinel, not a
    real broker order id (see `_ta_live_place_wing`, template_attach.py)."""
    wing_order_id = "chase"
    wing_chased = True


def test_opp_build_attach_entries_records_chase_routed_wing():
    """A chase-routed wing (never a real broker order id) must still be
    recorded in attached_gtts_json — not just non-double-recorded, but
    recorded at all — with an explicit `chased` flag so no downstream
    reader mistakes the "chase" sentinel for a cancellable broker order
    id. Before the fix, `_ta_live_place_wing` cleared `wing_order_id` to
    `None` for this exact case, so `attached` came back empty and the
    idempotency check in `_opp_load_row_for_attach` treated the row as
    "never attached" — a second trigger (chase + postback racing the
    same fill, or a manual Re-attach) could then place a SECOND live
    wing order with nothing to catch the duplicate."""
    from backend.api.routes.orders_place import _opp_build_attach_entries

    result = _FakeChasedAttachResult()
    attached = _opp_build_attach_entries(
        result, fill_price=100.0, parent_side="BUY",
    )

    wing_entries = [e for e in attached if e.get("kind") == "wing"]
    assert len(wing_entries) == 1, (
        "chase-routed wing must still produce a non-empty attached_gtts_json "
        "entry so the idempotency check sees it was already handed off"
    )
    assert wing_entries[0]["id"] == "chase"
    assert wing_entries[0]["chased"] is True
