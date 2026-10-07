"""Unit tests for the retry_template helpers extracted from
OrdersController.retry_template in backend/api/routes/orders.py.
"""
from types import SimpleNamespace

import pytest

from backend.api.routes.orders import (
    _retry_build_attached_payload,
    _retry_build_gtt_entry,
    _retry_build_result_response,
    _retry_effective_parent_qty,
    _retry_parse_overrides,
    _retry_precheck_row,
)


# ── _retry_parse_overrides ────────────────────────────────────────────────
class TestParseOverrides:
    def test_none_returns_empty(self):
        assert _retry_parse_overrides(None) == {}

    def test_empty_string_returns_empty(self):
        assert _retry_parse_overrides("") == {}

    def test_valid_dict_json_parsed(self):
        assert _retry_parse_overrides('{"wing_premium_pct": 10}') == {
            "wing_premium_pct": 10
        }

    def test_json_null_returns_empty(self):
        assert _retry_parse_overrides("null") == {}

    def test_json_list_returns_empty(self):
        """Only dict payloads count as overrides — an array is invalid."""
        assert _retry_parse_overrides("[1, 2, 3]") == {}

    def test_malformed_json_swallowed(self):
        assert _retry_parse_overrides("not json") == {}


# ── _retry_precheck_row ───────────────────────────────────────────────────
class TestPrecheckRow:
    def test_no_template_id(self):
        r = SimpleNamespace(
            template_id=None, attached_gtts_json=None, status="FILLED",
            mode="live",
        )
        out = _retry_precheck_row(r)
        assert out and "no template attached" in out["reason"]

    def test_already_attached_bails(self):
        r = SimpleNamespace(
            template_id=1, attached_gtts_json='[{}]', status="FILLED",
            mode="live",
        )
        out = _retry_precheck_row(r)
        assert out and "already attached" in out["reason"]

    def test_not_filled_bails_with_status_in_reason(self):
        r = SimpleNamespace(
            template_id=1, attached_gtts_json=None, status="OPEN",
            mode="live",
        )
        out = _retry_precheck_row(r)
        assert out and "OPEN" in out["reason"] and "FILLED" in out["reason"]

    def test_valid_row_returns_none(self):
        r = SimpleNamespace(
            template_id=1, attached_gtts_json=None, status="FILLED",
            mode="live",
        )
        assert _retry_precheck_row(r) is None

    def test_valid_sim_row_returns_none(self):
        """2026-09-30 fix — 'sim' mode is explicitly allowed (routes to
        apply_plan_sim, no real broker call), same as 'live'."""
        r = SimpleNamespace(
            template_id=1, attached_gtts_json=None, status="FILLED",
            mode="sim",
        )
        assert _retry_precheck_row(r) is None

    def test_child_row_with_parent_order_id_is_refused(self):
        """A child AlgoOrder row (e.g. a chase-routed wing's own row,
        `_chase_wing` in template_attach.py) is stamped with both
        `parent_order_id` and `template_id` for traceability, but must
        never be retry-eligible itself — only the templated PARENT order
        (parent_order_id IS NULL) can be retried. Without this guard, a
        FILLED wing row with template_id set and no attached_gtts_json of
        its own (that field is only ever written on the PARENT row) looks
        identical to a stalled/failed attach and would recursively arm a
        second full template against the wing."""
        r = SimpleNamespace(
            template_id=11, attached_gtts_json=None, status="FILLED",
            mode="live", parent_order_id=999,
        )
        out = _retry_precheck_row(r)
        assert out is not None
        assert out["ok"] is False
        assert "child order" in out["reason"]

    def test_parent_row_without_parent_order_id_is_unaffected(self):
        """A genuine templated parent (parent_order_id None) must still
        pass the precheck — this guard only excludes child rows."""
        r = SimpleNamespace(
            template_id=1, attached_gtts_json=None, status="FILLED",
            mode="live", parent_order_id=None,
        )
        assert _retry_precheck_row(r) is None

    @pytest.mark.parametrize("mode", ["paper", "replay", "shadow", None, ""])
    def test_non_live_non_sim_mode_is_refused(self, mode):
        """2026-09-30 fix — companion to the _fire_template_attach_on_fill
        mode gate. Since that fix, a FILLED paper/replay/shadow row with a
        template_id and no attached_gtts_json is its PERMANENT resting
        state (real broker attach is correctly skipped for non-live
        fills), not a failed attach. Retrying it must be refused — the
        retry path (_rco_run_template_attach) only special-cases 'sim';
        every other non-live mode would otherwise fall through to a real
        broker call."""
        r = SimpleNamespace(
            template_id=1, attached_gtts_json=None, status="FILLED",
            mode=mode,
        )
        out = _retry_precheck_row(r)
        assert out is not None
        assert out["ok"] is False
        assert "mode" in out["reason"]


# ── _retry_effective_parent_qty ──────────────────────────────────────────
# M7: function is now async — DB re-fetch attempted first; falls back to
# in-memory row when DB raises. SimpleNamespace has no .id so the DB path
# raises AttributeError, exercising the fallback branch without mocking.

@pytest.mark.asyncio
class TestEffectiveParentQty:
    async def test_partial_fill_uses_filled(self):
        r = SimpleNamespace(filled_quantity=25, quantity=50)
        assert await _retry_effective_parent_qty(r) == 25

    async def test_zero_filled_uses_original(self):
        r = SimpleNamespace(filled_quantity=0, quantity=50)
        assert await _retry_effective_parent_qty(r) == 50

    async def test_none_filled_uses_original(self):
        r = SimpleNamespace(filled_quantity=None, quantity=50)
        assert await _retry_effective_parent_qty(r) == 50

    async def test_none_quantity_returns_zero(self):
        r = SimpleNamespace(filled_quantity=None, quantity=None)
        assert await _retry_effective_parent_qty(r) == 0


# ── _retry_build_gtt_entry ───────────────────────────────────────────────
def _plan():
    return SimpleNamespace(
        parent_side="BUY",
        parent_symbol="NIFTY26JUN20000CE",
        parent_exchange="NFO",
        parent_account="ZG0790",
        parent_qty=50,
        parent_fill_price=100.0,
        notes=["n1"],
        gtts=[],
    )


class TestBuildGttEntry:
    def test_single_trigger_with_trail(self):
        spec = SimpleNamespace(
            label="SL", trigger_type="single",
            trigger_values=[95.0], sl_trail_pct=2.0,
        )
        e = _retry_build_gtt_entry(spec, "gid1", _plan(), "NRML")
        assert e["kind"] == "gtt"
        assert e["sl_trail_pct"] == 2.0
        assert e["current_trigger"] == 95.0
        assert e["highest_ltp"] == 100.0
        assert e["lowest_ltp"] == 100.0
        # single-trigger gets no tp_trigger
        assert "tp_trigger" not in e

    def test_two_leg_with_trail_gets_tp_trigger(self):
        spec = SimpleNamespace(
            label="OCO", trigger_type="two-leg",
            trigger_values=[110.0, 90.0], sl_trail_pct=1.5,
        )
        e = _retry_build_gtt_entry(spec, "gid2", _plan(), "NRML")
        assert e["sl_trail_pct"] == 1.5
        assert e["current_trigger"] == 90.0
        assert e["tp_trigger"] == 110.0

    def test_two_leg_no_trail_still_populates_tp_trigger(self):
        spec = SimpleNamespace(
            label="OCO", trigger_type="two-leg",
            trigger_values=[110.0, 90.0], sl_trail_pct=None,
        )
        e = _retry_build_gtt_entry(spec, "gid3", _plan(), "NRML")
        assert e["tp_trigger"] == 110.0
        assert "sl_trail_pct" not in e
        assert "current_trigger" not in e

    def test_single_no_trail(self):
        spec = SimpleNamespace(
            label="SL", trigger_type="single",
            trigger_values=[95.0], sl_trail_pct=None,
        )
        e = _retry_build_gtt_entry(spec, "gid4", _plan(), "NRML")
        assert "sl_trail_pct" not in e
        assert "current_trigger" not in e
        assert "tp_trigger" not in e

    def test_parent_metadata_always_populated(self):
        spec = SimpleNamespace(
            label="SL", trigger_type="single",
            trigger_values=[95.0], sl_trail_pct=None,
        )
        e = _retry_build_gtt_entry(spec, "gid5", _plan(), "MIS")
        assert e["parent_side"] == "BUY"
        assert e["parent_symbol"] == "NIFTY26JUN20000CE"
        assert e["parent_exchange"] == "NFO"
        assert e["parent_account"] == "ZG0790"
        assert e["parent_qty"] == 50
        assert e["parent_product"] == "MIS"


# ── _retry_build_attached_payload ────────────────────────────────────────
class TestBuildAttachedPayload:
    def test_empty_result_returns_empty(self):
        result = SimpleNamespace(
            plan=None, gtt_ids=None, wing_order_id=None, errors=[],
        )
        assert _retry_build_attached_payload(result, "NRML") == []

    def test_wing_only(self):
        result = SimpleNamespace(
            plan=None, gtt_ids=None, wing_order_id="W1", errors=[],
        )
        payload = _retry_build_attached_payload(result, "NRML")
        assert len(payload) == 1
        assert payload[0] == {
            "kind": "wing", "label": "Wing", "id": "W1", "chased": False,
        }

    def test_wing_only_chased_sets_chased_flag(self):
        """A chase-routed wing (`wing_order_id == "chase"`, `wing_chased`
        True — see `_ta_live_place_wing` in template_attach.py) must carry
        `chased: True` in its own retry-built entry, mirroring
        `_opp_build_attach_entries` field-for-field, so no downstream
        reader of this entry mistakes the "chase" sentinel for a real,
        cancellable broker order id."""
        result = SimpleNamespace(
            plan=None, gtt_ids=None, wing_order_id="chase",
            wing_chased=True, errors=[],
        )
        payload = _retry_build_attached_payload(result, "NRML")
        assert len(payload) == 1
        assert payload[0] == {
            "kind": "wing", "label": "Wing", "id": "chase", "chased": True,
        }

    def test_gtts_and_wing(self):
        spec = SimpleNamespace(
            label="SL", trigger_type="single",
            trigger_values=[95.0], sl_trail_pct=None,
        )
        plan = _plan()
        plan.gtts = [spec]
        result = SimpleNamespace(
            plan=plan, gtt_ids=["gid1"], wing_order_id="W1", errors=[],
        )
        payload = _retry_build_attached_payload(result, "NRML")
        assert len(payload) == 2
        assert payload[0]["kind"] == "gtt"
        assert payload[1]["kind"] == "wing"


# ── _retry_build_result_response ─────────────────────────────────────────
class TestBuildResultResponse:
    def test_no_attach_no_errors_is_failure(self):
        result = SimpleNamespace(
            plan=_plan(), gtt_ids=[], wing_order_id=None, errors=[],
        )
        r = _retry_build_result_response(result, attached_now=False)
        assert r["ok"] is False
        assert "nothing to attach" in r["reason"]
        assert r["attached"] is False

    def test_attached_now_true_is_success(self):
        result = SimpleNamespace(
            plan=_plan(), gtt_ids=["gid1"], wing_order_id=None, errors=[],
        )
        r = _retry_build_result_response(result, attached_now=True)
        assert r["ok"] is True
        assert r["attached"] is True

    def test_no_attach_with_errors_is_ok_but_not_attached(self):
        """Errors present → surface them (don't silently swallow)."""
        result = SimpleNamespace(
            plan=_plan(), gtt_ids=[], wing_order_id=None,
            errors=["chain scan empty"],
        )
        r = _retry_build_result_response(result, attached_now=False)
        assert r["ok"] is True
        assert r["attached"] is False
        assert "chain scan empty" in r["errors"]

    def test_plan_none_notes_defaults_to_empty(self):
        result = SimpleNamespace(
            plan=None, gtt_ids=[], wing_order_id=None, errors=[],
        )
        r = _retry_build_result_response(result, attached_now=True)
        assert r["notes"] == []
