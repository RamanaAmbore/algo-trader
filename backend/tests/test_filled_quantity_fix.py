"""
Regression tests for the 2026-09-30 fix — postback/reconcile fill-status
writers never set `filled_quantity`, structurally blocking template attach.

Confirmed via prod logs (`[TPL-ATTACH] skipping partial fill for parent
#1100: filled=0 of 100 (reconcile path)`) and DB rows (`algo_orders`
#1100-#1109 all show `filled_quantity=0` while `status=FILLED`).

Every writer below transitions an AlgoOrder row to FILLED only when the
broker's own status has already resolved to a *full* fill by this
codebase's own status-mapping convention (Kite COMPLETE, Dhan TRADED via
`_broker_is_fill_status`) — a genuine partial fill leaves the order OPEN
with an increasing `filled_quantity` until the terminal fill status
arrives. So `filled_quantity = quantity` is the correct value in every
one of these contexts, not a guess.

Four writers fixed (all feed the same downstream full-fill gates —
`_pb_wants_template_attach` and `_maybe_fire_template_attach_for_reconcile`
via `_opl_reconcile_attach_eligible` + its inline guard):
  1. `_pb_apply_status_to_row`      (orders_postback.py — Kite postback)
  2. `_sync_apply_row_status`       (orders_postback.py — Dhan/Groww postback,
                                      same defect class, found during this fix)
  3. `_rco_apply_fill_price`        (orders.py — chase/admin "chases/active"
                                      live-row reconcile)
  4. `_rco_stamp_fill_price`        (orders.py — bulk reconcile sweep)
  5. `_rco_reconcile_apply_target`  (orders.py — per-card single-order
                                      reconcile; same defect class, found
                                      during this fix, not in the original
                                      two named locations)

Five quality dimensions:
  1. SSOT   — tests the real writer + real downstream gate functions, not
              reimplementations.
  2. Perf   — n/a (no perf claim in this fix).
  3. Stale  — directly reproduces the prod log signature (filled=0 of N).
  4. Reuse  — same MagicMock row-fixture shape already used in
              test_template_findings.py's TestFullFillGateReconcile /
              TestFullFillGatePostback.
  5. UX     — n/a (backend-only fix, no UI surface).
"""
from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

import pytest


def _live_row(qty=100, filled=0, template_id=1, fill_price=None,
              mode="live", parent_order_id=None, status="OPEN"):
    r = MagicMock()
    r.id = 1
    r.status = status
    r.created_at = datetime.datetime.now(datetime.timezone.utc)
    r.quantity = qty
    r.filled_quantity = filled
    r.template_id = template_id
    r.fill_price = fill_price
    r.mode = mode
    r.parent_order_id = parent_order_id
    r.intent = None
    r.is_close_intent = False
    r.account = "ZG0001"
    r.symbol = "NIFTY25JUL24000CE"
    r.exchange = "NFO"
    r.transaction_type = "SELL"
    r.product = "NRML"
    return r


# ── 1. Kite postback path ──────────────────────────────────────────────────

class TestPbApplyStatusToRowSetsFilledQuantity:
    def test_full_fill_sets_filled_quantity_to_quantity(self):
        from backend.api.routes.orders_postback import _pb_apply_status_to_row
        r = _live_row(qty=100, filled=0)

        changed = _pb_apply_status_to_row(r, new_status="FILLED", price=105.5)

        assert changed is True
        assert r.filled_quantity == 100, (
            "bug: filled_quantity was never written on the FILLED "
            "transition, permanently blocking the full-fill gate"
        )
        assert r.fill_price == 105.5

    def test_unblocks_pb_wants_template_attach(self):
        """Before the fix, filled_quantity stayed 0 forever, so
        `_pb_wants_template_attach` — the exact function named in the
        prod log — always saw filled(0) < quantity(100) and refused."""
        from backend.api.routes.orders_postback import (
            _pb_apply_status_to_row, _pb_wants_template_attach,
        )
        r = _live_row(qty=100, filled=0)

        # Pre-fill: gate correctly refuses (no fill yet at all).
        assert _pb_wants_template_attach(r) is False

        _pb_apply_status_to_row(r, new_status="FILLED", price=105.5)

        assert _pb_wants_template_attach(r) is True, (
            "gate must now pass for a genuinely full fill — this was the "
            "structural block reported in prod (filled=0 of 100)"
        )

    def test_price_less_filled_still_sets_filled_quantity(self):
        """A price-less COMPLETE postback still flips status to FILLED
        (existing behaviour — see `_r.status = new_status` running before
        the `not price` early return) but the function returns False, so
        the row never enters `_filled_rows`. FILLED is a final status, so
        without this, the row would be stuck forever at
        filled_quantity=0 with no later postback able to retry it."""
        from backend.api.routes.orders_postback import _pb_apply_status_to_row
        r = _live_row(qty=100, filled=0, status="OPEN")

        changed = _pb_apply_status_to_row(r, new_status="FILLED", price=None)

        assert changed is False  # unchanged: price-less fill isn't fanned out
        assert r.status == "FILLED"
        assert r.filled_quantity == 100, (
            "status is FILLED regardless of price — filled_quantity must "
            "track it, not silently stay 0 forever on a final status"
        )

    def test_non_filled_transition_leaves_filled_quantity_untouched(self):
        from backend.api.routes.orders_postback import _pb_apply_status_to_row
        r = _live_row(qty=100, filled=0, status="OPEN")
        changed = _pb_apply_status_to_row(r, new_status="CANCELLED", price=None)
        assert changed is False
        assert r.filled_quantity == 0


# ── 2. Dhan/Groww postback path (same defect class, found during this fix) ──

class TestSyncApplyRowStatusSetsFilledQuantity:
    def test_full_fill_sets_filled_quantity_to_quantity(self):
        from backend.api.routes.orders_postback import _sync_apply_row_status
        r = _live_row(qty=75, filled=0, status="OPEN")

        changed = _sync_apply_row_status(
            r, new_status="FILLED", price=150.25, broker_id="dhan",
            status="TRADED", status_message="",
        )

        assert changed is True
        assert r.filled_quantity == 75

    def test_non_fill_transition_leaves_filled_quantity_untouched(self):
        from backend.api.routes.orders_postback import _sync_apply_row_status
        r = _live_row(qty=75, filled=0, status="OPEN")
        changed = _sync_apply_row_status(
            r, new_status="CANCELLED", price=None, broker_id="dhan",
            status="CANCELLED", status_message="",
        )
        assert changed is False
        assert r.filled_quantity == 0


# ── 3. Chase / admin "chases/active" live-row reconcile ────────────────────

class TestRcoApplyFillPriceSetsFilledQuantity:
    def test_sets_filled_quantity_to_quantity(self):
        from backend.api.routes.orders import _rco_apply_fill_price
        r = _live_row(qty=60, filled=0)
        bo = {"average_price": 120.0}

        _rco_apply_fill_price(r, bo)

        assert r.filled_quantity == 60
        assert r.fill_price == 120.0

    def test_sets_filled_quantity_even_when_average_price_absent(self):
        """average_price missing/zero must not block filled_quantity —
        the row's OWN quantity is broker-truth-independent here."""
        from backend.api.routes.orders import _rco_apply_fill_price
        r = _live_row(qty=60, filled=0)
        _rco_apply_fill_price(r, {})
        assert r.filled_quantity == 60


# ── 4. Bulk reconcile sweep ("/reconcile" endpoint) ─────────────────────────

class TestRcoStampFillPriceSetsFilledQuantity:
    def test_sets_filled_quantity_to_quantity(self):
        from backend.api.routes.orders import _rco_stamp_fill_price
        r = _live_row(qty=40, filled=0)
        bo = {"average_price": 99.0}

        _rco_stamp_fill_price(r, bo)

        assert r.filled_quantity == 40


# ── 5. Per-card single-order reconcile (same defect class, found during
#      this fix — not one of the two originally-named locations) ──────────

class TestRcoReconcileApplyTargetSetsFilledQuantity:
    def test_full_fill_sets_filled_quantity_and_flags_attach(self):
        from backend.api.routes.orders import _rco_reconcile_apply_target
        r = _live_row(qty=60, filled=0, status="OPEN")
        bo = {"average_price": 200.0}

        updated, note, attach_after_commit = _rco_reconcile_apply_target(
            r, bo, "COMPLETE", "FILLED",
        )

        assert updated is True
        assert attach_after_commit is True
        assert r.filled_quantity == 60

    def test_filled_without_average_price_still_sets_filled_quantity(self):
        """No usable average_price on the broker order dict → the row
        still flips to FILLED (target) via the fallthrough branch, but
        attach_after_commit is False since there's no fill_price to work
        with. FILLED is a final status, so filled_quantity must still be
        set here — otherwise the row is stuck at 0 forever with no later
        reconcile able to retry it."""
        from backend.api.routes.orders import _rco_reconcile_apply_target
        r = _live_row(qty=60, filled=0, status="OPEN")

        updated, note, attach_after_commit = _rco_reconcile_apply_target(
            r, {}, "COMPLETE", "FILLED",
        )

        assert updated is True
        assert attach_after_commit is False
        assert r.filled_quantity == 60

    def test_unblocks_maybe_fire_template_attach_for_reconcile(self):
        """Full round-trip: writer output must satisfy both
        `_opl_reconcile_attach_eligible` and the inline full-fill guard
        inside `_maybe_fire_template_attach_for_reconcile`, which creates
        the attach task via asyncio.create_task."""
        from backend.api.routes.orders import _rco_reconcile_apply_target
        from backend.api.routes.orders_place import (
            _maybe_fire_template_attach_for_reconcile,
        )

        r = _live_row(qty=60, filled=0, status="OPEN")
        bo = {"average_price": 200.0}
        _rco_reconcile_apply_target(r, bo, "COMPLETE", "FILLED")

        with patch("backend.api.routes.orders_place.asyncio") as mock_asyncio:
            _maybe_fire_template_attach_for_reconcile(r)
            mock_asyncio.create_task.assert_called_once()
