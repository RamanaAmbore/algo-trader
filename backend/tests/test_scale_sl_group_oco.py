"""test_scale_sl_group_oco.py

Covers audit finding T14 (2026-10-23 template/bracket audit, cluster 2B):
a scale-out TP ladder (2+ TP rungs, e.g. "TP+30% × 50") placed alongside
one SL leg had NO OCO pairing at all. `pair_two_singles`
(template_attach.py) only ever matched an EXACT `{"TP", "SL"}` label set
with exactly 2 legs — a scale ladder's rung labels never match "TP"
literally, so when SL fired and flattened the position, every surviving
scale-ladder TP GTT stayed live with no sibling-cancel wiring, risking an
unintended naked position on a later price move through one of them.

Fix: `_ta_detect_scale_sl_group` detects a scale-ladder-plus-SL shape and
records `(sl_placed_id, [tp_placed_id, ...])` on `AttachResult.sibling_groups`
(deliberately asymmetric — SL side only, so TP rungs never cancel each
other on ordinary sequential scale-out fires). `_opp_build_attach_entries`
writes this onto the SL leg's `attached_gtts_json` entry as `sibling_ids`.
`_task_oco_pair_watcher`'s `_oco_process_group_entry` cancels every
surviving TP rung once SL fires/is gone, and symmetrically cancels the
now-pointless SL once every TP rung has already fired/gone.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest


def _gtt(label, placed_id, trigger_values=None):
    from backend.api.algo.template_attach import GttSpec
    spec = GttSpec(
        trigger_type="single",
        trigger_values=trigger_values or [100.0],
        orders=[{"quantity": 50, "transaction_type": "SELL", "product": "NRML"}],
        label=label,
    )
    spec.placed_id = placed_id
    return spec


def _plan(gtts):
    from backend.api.algo.template_attach import TemplatePlan
    return TemplatePlan(
        template_id=1, template_name="t", template_slug="t",
        parent_account="ZG0790", parent_symbol="NIFTY25JULFUT",
        parent_side="BUY", parent_qty=150, parent_exchange="NFO",
        parent_fill_price=100.0, parent_lot_size=1,
        gtts=gtts, wing=None,
    )


class TestDetectScaleSlGroup:
    def test_classic_two_leg_tp_sl_pair_returns_none(self):
        """The exact {"TP","SL"} shape is pair_two_singles's territory —
        the group detector must stay out of its way."""
        from backend.api.algo.template_attach import _ta_detect_scale_sl_group

        plan = _plan([_gtt("TP", "G1"), _gtt("SL", "G2")])
        assert _ta_detect_scale_sl_group(plan) is None

    def test_scale_ladder_plus_sl_forms_a_group(self):
        """Pre-fix: no mechanism recorded this shape at all — this
        assertion fails against the pre-fix code (the function/field
        doesn't exist)."""
        from backend.api.algo.template_attach import _ta_detect_scale_sl_group

        plan = _plan([
            _gtt("TP+30% × 50", "TP1"),
            _gtt("TP+60% × 50", "TP2"),
            _gtt("SL", "SL1"),
        ])
        group = _ta_detect_scale_sl_group(plan)
        assert group == ("SL1", ["TP1", "TP2"])

    def test_rejected_rung_excluded_from_group(self):
        """A rung that never got placed_id (rejected) must never be
        wired as something the watcher tries to cancel."""
        from backend.api.algo.template_attach import _ta_detect_scale_sl_group

        plan = _plan([
            _gtt("TP+30% × 50", None),   # rejected
            _gtt("TP+60% × 50", "TP2"),
            _gtt("SL", "SL1"),
        ])
        group = _ta_detect_scale_sl_group(plan)
        assert group == ("SL1", ["TP2"])

    def test_single_rung_scale_out_plus_sl_also_forms_a_group(self):
        """Even a single-rung scale-out (100% close) uses a non-literal
        TP label ("TP+100% × 150"), so it ALSO misses pair_two_singles's
        exact label match — the group path must still catch it."""
        from backend.api.algo.template_attach import _ta_detect_scale_sl_group

        plan = _plan([
            _gtt("TP+100% × 150", "TP1"),
            _gtt("SL", "SL1"),
        ])
        group = _ta_detect_scale_sl_group(plan)
        assert group == ("SL1", ["TP1"])

    def test_no_sl_leg_never_forms_a_group(self):
        """A pure TP-only ladder (no SL at all) must never be grouped —
        there's nothing to pair rungs against, and rungs must never
        cancel each other."""
        from backend.api.algo.template_attach import _ta_detect_scale_sl_group

        plan = _plan([
            _gtt("TP+30% × 50", "TP1"),
            _gtt("TP+60% × 50", "TP2"),
        ])
        assert _ta_detect_scale_sl_group(plan) is None


class TestAttachEntriesCarrySiblingIdsOnSlOnly:
    def test_sl_entry_gets_sibling_ids_tp_entries_get_nothing(self):
        from backend.api.routes.orders_place import _opp_build_attach_entries
        from backend.api.algo.template_attach import AttachResult

        plan = _plan([
            _gtt("TP+30% × 50", "TP1"),
            _gtt("TP+60% × 50", "TP2"),
            _gtt("SL", "SL1"),
        ])
        result = AttachResult(plan=plan)
        result.gtt_ids = ["TP1", "TP2", "SL1"]
        result.sibling_groups = [("SL1", ["TP1", "TP2"])]

        entries = _opp_build_attach_entries(result, 100.0, "BUY", "NRML")
        by_id = {e["id"]: e for e in entries}

        assert by_id["TP1"].get("sibling_ids") is None
        assert by_id["TP1"].get("sibling_id") is None
        assert by_id["TP2"].get("sibling_ids") is None
        assert by_id["SL1"]["sibling_ids"] == ["TP1", "TP2"]
        assert by_id["SL1"]["parent_account"] == "ZG0790"
        assert by_id["SL1"]["parent_exchange"] == "NFO"
        assert by_id["SL1"]["parent_symbol"] == "NIFTY25JULFUT", (
            "the watcher's position-confirmation gate needs the symbol "
            "to query the real broker position"
        )


class TestOcoGttIsLive:
    def test_missing_id_is_not_live(self):
        from backend.api.background import _oco_gtt_is_live
        assert _oco_gtt_is_live("X1", {}) is False

    def test_active_status_is_live(self):
        from backend.api.background import _oco_gtt_is_live
        assert _oco_gtt_is_live("X1", {"X1": {"status": "active"}}) is True

    def test_triggered_status_counts_as_gone_not_live(self):
        """Kite keeps a fired GTT listed with status='triggered' instead
        of dropping it — the group watcher must treat that as GONE, or
        it would never detect an SL fire on Kite at all."""
        from backend.api.background import _oco_gtt_is_live
        assert _oco_gtt_is_live("X1", {"X1": {"status": "triggered"}}) is False

    def test_cancelled_status_counts_as_gone(self):
        from backend.api.background import _oco_gtt_is_live
        assert _oco_gtt_is_live("X1", {"X1": {"status": "cancelled"}}) is False

    def test_missing_status_field_defaults_to_live(self):
        from backend.api.background import _oco_gtt_is_live
        assert _oco_gtt_is_live("X1", {"X1": {}}) is True


def _group_entry(**over) -> dict:
    base = {
        "kind": "gtt", "label": "SL", "id": "SL1",
        "sibling_ids": ["TP1", "TP2"],
        "parent_account": "ZG0790",
        "parent_exchange": "NFO",
        "parent_symbol": "NIFTY25JULFUT",
    }
    base.update(over)
    return base


class TestOcoProcessGroupEntry:
    """2026-10-23 follow-up fix — every cancel decision below is gated on
    `_oco_group_position_is_flat` confirming the real broker position is
    exactly flat. `_oco_gtt_is_live` reporting a leg as 'gone' is NOT by
    itself proof the position closed (a rejected-at-placement rung, a
    manually-cancelled leg, or a fired-but-unfilled exit all read as
    'gone' too) — see `_oco_group_position_is_flat`'s own docstring.
    Tests that expect a real cancel patch the gate to return True;
    `TestOcoGroupPositionGate` below covers the gate itself.
    """

    @pytest.mark.asyncio
    async def test_sl_fired_cancels_surviving_tp_rungs(self):
        """Core T14 regression: SL gone, two TP rungs still live at the
        broker, position confirmed flat — both must be cancelled."""
        from backend.api import background as bg_mod

        entry = _group_entry()
        broker = MagicMock()
        broker.cancel_gtt = MagicMock()
        broker_gtts = {
            "TP1": {"status": "active"},
            "TP2": {"status": "active"},
            # SL1 absent entirely — fired and dropped from the listing.
        }

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(bg_mod, "_oco_group_position_is_flat", AsyncMock(return_value=True))
            changed = await bg_mod._oco_process_group_entry(
                entry, broker, row_id=1, broker_gtts=broker_gtts,
            )

        assert changed is True
        assert broker.cancel_gtt.call_count == 2
        cancelled_ids = {c.args[0] for c in broker.cancel_gtt.call_args_list}
        assert cancelled_ids == {"TP1", "TP2"}
        assert "sibling_ids" not in entry

    @pytest.mark.asyncio
    async def test_sl_looks_fired_but_position_not_confirmed_flat_does_not_cancel(self):
        """The follow-up regression: a rung rejected at placement (never
        reaches the broker) also reads as 'gone'. Without the position
        gate, this would wrongly cancel a TP rung still protecting an
        open position. `is_flat=False` (position still open) must block
        the cancel entirely."""
        from backend.api import background as bg_mod

        entry = _group_entry(sibling_ids=["TP1"])
        broker = MagicMock()
        broker.cancel_gtt = MagicMock()
        broker_gtts = {"TP1": {"status": "active"}}  # SL1 gone

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(bg_mod, "_oco_group_position_is_flat", AsyncMock(return_value=False))
            changed = await bg_mod._oco_process_group_entry(
                entry, broker, row_id=1, broker_gtts=broker_gtts,
            )

        assert changed is False
        broker.cancel_gtt.assert_not_called()
        assert entry["sibling_ids"] == ["TP1"]

    @pytest.mark.asyncio
    async def test_sl_looks_fired_but_position_check_fails_does_not_cancel(self):
        """`None` (unknown — fetch failed) must be just as blocking as
        `False`; never treated as flat."""
        from backend.api import background as bg_mod

        entry = _group_entry(sibling_ids=["TP1"])
        broker = MagicMock()
        broker.cancel_gtt = MagicMock()
        broker_gtts = {"TP1": {"status": "active"}}

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(bg_mod, "_oco_group_position_is_flat", AsyncMock(return_value=None))
            changed = await bg_mod._oco_process_group_entry(
                entry, broker, row_id=1, broker_gtts=broker_gtts,
            )

        assert changed is False
        broker.cancel_gtt.assert_not_called()

    @pytest.mark.asyncio
    async def test_sl_fired_but_cancel_fails_keeps_id_for_retry(self):
        from backend.api import background as bg_mod

        entry = _group_entry(sibling_ids=["TP1"])
        broker = MagicMock()
        broker.cancel_gtt = MagicMock(side_effect=RuntimeError("network blip"))
        broker_gtts = {"TP1": {"status": "active"}}

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(bg_mod, "_oco_group_position_is_flat", AsyncMock(return_value=True))
            changed = await bg_mod._oco_process_group_entry(
                entry, broker, row_id=1, broker_gtts=broker_gtts,
            )

        assert changed is True
        assert entry["sibling_ids"] == ["TP1"], (
            "a failed cancel must leave the id tracked so the next poll retries"
        )

    @pytest.mark.asyncio
    async def test_ladder_fully_closed_cancels_now_pointless_sl(self):
        """Symmetric direction: every TP rung already fired/gone, position
        confirmed flat — SL must be cancelled too."""
        from backend.api import background as bg_mod

        entry = _group_entry()
        broker = MagicMock()
        broker.cancel_gtt = MagicMock()
        broker_gtts = {"SL1": {"status": "active"}}  # neither TP listed — both fired

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(bg_mod, "_oco_group_position_is_flat", AsyncMock(return_value=True))
            changed = await bg_mod._oco_process_group_entry(
                entry, broker, row_id=1, broker_gtts=broker_gtts,
            )

        assert changed is True
        broker.cancel_gtt.assert_called_once_with("SL1", exchange="NFO")
        assert "sibling_ids" not in entry

    @pytest.mark.asyncio
    async def test_ladder_looks_closed_but_position_not_flat_does_not_cancel_sl(self):
        """The mirror follow-up regression: every rung reads 'gone' (one
        was cancelled manually) but the broker position is still open —
        the SL must NOT be cancelled."""
        from backend.api import background as bg_mod

        entry = _group_entry()
        broker = MagicMock()
        broker.cancel_gtt = MagicMock()
        broker_gtts = {"SL1": {"status": "active"}}

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(bg_mod, "_oco_group_position_is_flat", AsyncMock(return_value=False))
            changed = await bg_mod._oco_process_group_entry(
                entry, broker, row_id=1, broker_gtts=broker_gtts,
            )

        assert changed is False
        broker.cancel_gtt.assert_not_called()
        assert entry["sibling_ids"] == ["TP1", "TP2"]

    @pytest.mark.asyncio
    async def test_normal_progress_prunes_without_cancelling(self):
        """One rung has fired on its own schedule, SL and the other
        rung are still live — this is ordinary scale-out progress, not
        a fire condition. No cancel_gtt call, no position check even
        needed; just prune the stale id."""
        from backend.api import background as bg_mod

        entry = _group_entry()
        broker = MagicMock()
        broker.cancel_gtt = MagicMock()
        broker_gtts = {
            "SL1": {"status": "active"},
            "TP2": {"status": "active"},
            # TP1 absent — fired on its own, expected scale-out behaviour.
        }

        changed = await bg_mod._oco_process_group_entry(
            entry, broker, row_id=1, broker_gtts=broker_gtts,
        )

        assert changed is True
        broker.cancel_gtt.assert_not_called()
        assert entry["sibling_ids"] == ["TP2"]

    @pytest.mark.asyncio
    async def test_no_change_when_everything_still_live(self):
        from backend.api import background as bg_mod

        entry = _group_entry()
        broker = MagicMock()
        broker.cancel_gtt = MagicMock()
        broker_gtts = {
            "SL1": {"status": "active"},
            "TP1": {"status": "active"},
            "TP2": {"status": "active"},
        }

        changed = await bg_mod._oco_process_group_entry(
            entry, broker, row_id=1, broker_gtts=broker_gtts,
        )

        assert changed is False
        broker.cancel_gtt.assert_not_called()
        assert entry["sibling_ids"] == ["TP1", "TP2"]

    @pytest.mark.asyncio
    async def test_dispatcher_routes_group_entries_to_group_handler(self):
        """_oco_process_one_entry must route a sibling_ids entry to the
        group handler instead of the classic pairwise sibling_id path."""
        from backend.api import background as bg_mod

        entry = _group_entry(sibling_ids=["TP1"])
        broker = MagicMock()
        broker.cancel_gtt = MagicMock()
        broker_gtts = {"TP1": {"status": "active"}}  # SL1 gone — SL fired

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(bg_mod, "_oco_group_position_is_flat", AsyncMock(return_value=True))
            changed = await bg_mod._oco_process_one_entry(
                entry, {}, broker, row_id=1, broker_gtts=broker_gtts,
            )

        assert changed is True
        broker.cancel_gtt.assert_called_once_with("TP1", exchange="NFO")


class TestOcoGroupPositionGate:
    @pytest.mark.asyncio
    async def test_returns_true_when_net_qty_is_exactly_zero(self):
        from backend.api import background as bg_mod

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "backend.api.routes.orders_place._fetch_net_position_qty",
                AsyncMock(return_value=0.0),
            )
            out = await bg_mod._oco_group_position_is_flat("ZG0790", "NFO", "NIFTYFUT")
        assert out is True

    @pytest.mark.asyncio
    async def test_returns_false_when_net_qty_nonzero(self):
        from backend.api import background as bg_mod

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "backend.api.routes.orders_place._fetch_net_position_qty",
                AsyncMock(return_value=50.0),
            )
            out = await bg_mod._oco_group_position_is_flat("ZG0790", "NFO", "NIFTYFUT")
        assert out is False

    @pytest.mark.asyncio
    async def test_returns_none_when_no_matching_row(self):
        from backend.api import background as bg_mod

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "backend.api.routes.orders_place._fetch_net_position_qty",
                AsyncMock(return_value=None),
            )
            out = await bg_mod._oco_group_position_is_flat("ZG0790", "NFO", "NIFTYFUT")
        assert out is None

    @pytest.mark.asyncio
    async def test_returns_none_on_fetch_exception(self):
        from backend.api import background as bg_mod

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "backend.api.routes.orders_place._fetch_net_position_qty",
                AsyncMock(side_effect=RuntimeError("broker down")),
            )
            out = await bg_mod._oco_group_position_is_flat("ZG0790", "NFO", "NIFTYFUT")
        assert out is None


class TestApplyPlanLiveIntegration:
    """End-to-end through the real `apply_plan_live` + MagicMock broker,
    not just the unit-level `_ta_detect_scale_sl_group` — proves the
    group actually gets populated on `AttachResult` for a real scale-out
    + SL plan, not just on a hand-built plan with pre-set `placed_id`s."""

    def test_scale_out_plus_sl_plan_populates_sibling_groups(self):
        from backend.api.algo.template_attach import apply_plan_live
        from unittest.mock import MagicMock as _MM

        plan = _plan([
            _gtt("TP+30% × 50", None),
            _gtt("TP+60% × 50", None),
            _gtt("SL", None),
        ])
        broker = _MM()
        broker.broker_id = "zerodha_kite"
        broker.capabilities.gtt_single = True
        broker.validate_gtt_exchange.return_value = None
        broker.translate_qty.side_effect = lambda exch, qty, ls: qty
        broker.place_gtt.side_effect = ["G1", "G2", "G3"]
        broker.get_gtts.return_value = [
            {"id": "G1", "status": "active"},
            {"id": "G2", "status": "active"},
            {"id": "G3", "status": "active"},
        ]

        result = apply_plan_live(plan, broker, parent_order_id=1)

        assert result.gtt_ids == ["G1", "G2", "G3"]
        assert result.sibling_groups == [("G3", ["G1", "G2"])]
        assert result.sibling_pairs == []


class TestOcoParseEntriesPicksUpGroupOnlyRows:
    def test_row_with_only_sibling_ids_is_included(self):
        """A row whose ONLY OCO pointer is the plural `sibling_ids`
        (no classic singular `sibling_id` anywhere) must still be
        picked up for the watcher to process — `_bg_oco_first_sibling_
        account` is the gate `_oco_parse_entries` relies on."""
        import json as _json
        from backend.api.background import _oco_parse_entries

        class _Row:
            id = 99
            attached_gtts_json = _json.dumps([
                {"kind": "gtt", "label": "SL", "id": "SL1",
                 "sibling_ids": ["TP1", "TP2"],
                 "parent_account": "ZG0790", "parent_exchange": "NFO",
                 "parent_symbol": "NIFTY25JULFUT"},
                {"kind": "gtt", "label": "TP+30% × 50", "id": "TP1"},
                {"kind": "gtt", "label": "TP+60% × 50", "id": "TP2"},
            ])

        rows_by_account, attached_by_row = _oco_parse_entries([_Row()], _json.loads)

        assert "ZG0790" in rows_by_account
        assert len(rows_by_account["ZG0790"]) == 1
        assert 99 in attached_by_row
