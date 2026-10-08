"""
P0 fix (2026-10): an unverified GTT must never be silently discarded.

`broker.place_gtt` can succeed (returns an id) while `_verify_gtt_accepted`
cannot confirm the broker actually holds it as accepted — either because
the id is simply absent from `broker.get_gtts()` ("not present", a
documented read-after-write gap for brokers like Groww whose get_gtts()
server-side-filters to ACTIVE only) or because the status read itself
raised ("status read failed: ..."). Neither outcome is a confirmed broker
rejection. Before this fix, `_ta_live_place_one_gtt` dropped the id
completely in both cases — never calling `broker.cancel_gtt`, never
recording the id anywhere — so a GTT that might genuinely be live at the
broker vanished from `AttachResult` entirely. If every leg in a template
hit this (no wing), `attached_gtts_json` stayed NULL on the AlgoOrder row,
so `_opp_load_row_for_attach`'s idempotency guard never tripped and a
later retrigger (chase/postback race, redelivered postback, admin
reconcile, manual Retry-attach) could place ANOTHER live GTT for the same
leg — risking two live exit GTTs on one position or an orphan GTT nothing
ever cancels.

Fix: on an ambiguous verify outcome, try `broker.cancel_gtt` first; only
if that cancel itself cannot be confirmed does the id get tracked (same
`placed_id` / `gtt_ids` plumbing a normally-accepted GTT uses, flagged
`GttSpec.unverified = True`) so it is never silently dropped.

Five test dimensions per project convention:
  SSOT   — unverified-but-tracked GTTs use the SAME placed_id/gtt_ids
           plumbing a verified GTT uses, so no second code path to drift
  Perf   — exactly one cancel_gtt attempt per ambiguous leg, no retry storm
  Stale  — the old silent-discard path (no cancel, no tracking) is dead
  Reuse  — downstream consumer `_opp_build_attach_entries` needs zero
           changes to surface the tracked id into attached_gtts_json
  UX     — CRITICAL log + a result.errors entry surface the unverified
           state to the operator instead of a silent vanish
"""
from __future__ import annotations

import json

import pytest
from unittest.mock import MagicMock

from backend.api.algo.template_attach import (
    AttachResult,
    TemplatePlan,
    GttSpec,
    apply_plan_live,
    resolve_template_plan,
    _gtt_verify_reason_is_ambiguous,
    _GTT_NOT_PRESENT_REASON,
)
from backend.api.routes.orders_place import _opp_build_attach_entries


# ── Module-level market-hours bypass (matches sibling template_attach
# test modules — apply_plan_live checks market hours for wing legs; this
# plan carries no wing, but keep parity with the convention). ─────────

@pytest.fixture(autouse=True)
def _market_open(monkeypatch):
    monkeypatch.setattr(
        "backend.api.algo.agent_engine._symbol_exchange_open",
        lambda *_a, **_kw: True,
    )
    monkeypatch.setattr(
        "backend.api.algo.agent_engine._build_now_ctx",
        lambda: {"nse_open": True, "mcx_open": True},
    )
    # Speed/determinism: _verify_gtt_accepted retries up to twice with a
    # 0.5s sleep between reads when the id is "not present" — collapse
    # that to a no-op for the test run.
    monkeypatch.setattr("backend.api.algo.template_attach.time.sleep", lambda *_a, **_kw: None)


_NFO_TEMPLATE = {
    "id": 10, "slug": "default-bull", "name": "Default Bull",
    "applies_to": "buy_any",
    "tp_pct": 10.0, "sl_pct": 5.0,
    "wing_premium_pct": None, "wing_strike_offset": None,
    "tp_order_type": "LIMIT",
    "tp_scales_json": None,
    "sl_trail_pct": None,
}

_NFO_OVERRIDES = {
    "tp_pct": 10.0,
    "sl_pct": 5.0,
    "wing_premium_pct": None,
    "wing_strike_offset": None,
}


def _make_plan() -> TemplatePlan:
    return resolve_template_plan(
        _NFO_TEMPLATE, _NFO_OVERRIDES,
        parent_account="ZG0790",
        parent_symbol="NIFTY25JULFUT",
        parent_side="BUY",
        parent_qty=75,
        parent_exchange="NFO",
        parent_fill_price=24000.0,
        parent_lot_size=75,
    )


def _make_broker() -> MagicMock:
    broker = MagicMock()
    broker.broker_id = "zerodha_kite"
    broker.place_gtt.return_value = "gtt-amb-1"
    broker.translate_qty.side_effect = lambda exch, qty, ls: qty
    return broker


# ── Pure classifier unit tests ─────────────────────────────────────────

def test_ambiguous_classifier_true_for_not_present():
    assert _gtt_verify_reason_is_ambiguous(_GTT_NOT_PRESENT_REASON) is True


def test_ambiguous_classifier_true_for_status_read_failed():
    assert _gtt_verify_reason_is_ambiguous("status read failed: boom") is True


def test_ambiguous_classifier_false_for_confirmed_rejection():
    assert _gtt_verify_reason_is_ambiguous("broker status rejected") is False


# ── "not present" + cancel_gtt ALSO fails → id must be tracked, never dropped ──

def test_not_present_and_cancel_fails_tracks_id_as_unverified():
    plan = _make_plan()
    broker = _make_broker()
    broker.get_gtts.return_value = []  # id absent from the list -> "not present"
    broker.cancel_gtt.side_effect = RuntimeError("broker unreachable")

    result = apply_plan_live(plan, broker)

    assert broker.cancel_gtt.called, (
        "must attempt broker.cancel_gtt on an ambiguous verify outcome, "
        "never silently drop the id without trying to clean it up"
    )

    unverified_specs = [g for g in result.plan.gtts if g.unverified]
    assert unverified_specs, (
        "at least one GTT spec must be flagged unverified when cancel_gtt "
        "also fails to confirm cleanup — the id must not vanish"
    )
    for spec in unverified_specs:
        assert spec.placed_id == "gtt-amb-1"
        assert spec.placed_id in result.gtt_ids

    assert any("UNVERIFIED" in e for e in result.errors), (
        f"expected an UNVERIFIED marker in result.errors, got: {result.errors}"
    )

    # Downstream consumer (orders_place.py) must surface the tracked id so
    # attached_gtts_json ends up non-NULL — this is what makes
    # `_opp_load_row_for_attach`'s idempotency guard refuse a duplicate
    # attach on a later retrigger.
    attached = _opp_build_attach_entries(result, fill_price=24000.0, parent_side="BUY")
    assert attached, (
        "attach entries must not be empty — an empty list means "
        "attached_gtts_json stays NULL and the idempotency guard never trips"
    )
    ids = [e.get("id") for e in attached]
    assert "gtt-amb-1" in ids

    attached_gtts_json = json.dumps(attached)
    assert attached_gtts_json and attached_gtts_json != "[]" and attached_gtts_json != "null", (
        "attached_gtts_json must be a non-NULL/non-empty payload so a second "
        "attach attempt on the same row is correctly blocked"
    )


# ── "status read failed" + cancel_gtt SUCCEEDS → id is cleanly cancelled ──

def test_status_read_failed_and_cancel_succeeds_cleans_up():
    plan = _make_plan()
    broker = _make_broker()
    broker.get_gtts.side_effect = RuntimeError("network timeout")  # verify read itself raises
    # broker.cancel_gtt is a plain MagicMock — succeeds (no exception) by default

    result = apply_plan_live(plan, broker)

    assert broker.cancel_gtt.called, (
        "must attempt broker.cancel_gtt on an ambiguous verify outcome"
    )
    cancel_call_ids = [
        (c.args[0] if c.args else c.kwargs.get("gtt_id"))
        for c in broker.cancel_gtt.call_args_list
    ]
    assert "gtt-amb-1" in cancel_call_ids, (
        f"cancel_gtt must be called with the unverified id, got calls: "
        f"{broker.cancel_gtt.call_args_list}"
    )

    # A confirmed-cancelled GTT has nothing live left to track.
    assert not any(g.unverified for g in result.plan.gtts), (
        "a GTT that was successfully cancelled must not be flagged unverified"
    )
    assert any("cancelled" in e for e in result.errors), (
        f"expected a 'cancelled' note in result.errors, got: {result.errors}"
    )


# ── Confirmed broker rejection must still be dropped with no cancel attempt ──

def test_confirmed_rejection_is_dropped_without_cancel_attempt():
    plan = _make_plan()
    broker = _make_broker()
    broker.get_gtts.return_value = [{"id": "gtt-amb-1", "status": "rejected"}]

    result = apply_plan_live(plan, broker)

    assert not broker.cancel_gtt.called, (
        "a confirmed broker rejection has nothing live to clean up — "
        "cancel_gtt must not be called"
    )
    assert not any(g.unverified for g in result.plan.gtts)
    assert not result.gtt_ids
    assert any("not accepted at broker" in e for e in result.errors)
