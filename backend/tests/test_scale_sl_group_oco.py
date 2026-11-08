"""test_scale_sl_group_oco.py

Originally covered audit finding T14 (2026-10-23 template/bracket audit,
cluster 2B) end-to-end: a scale-out TP ladder's lack of OCO pairing with
its SL leg, from detection (`_ta_detect_scale_sl_group`,
`template_attach.py`) through attach-entry construction
(`_opp_build_attach_entries`, `orders_place.py`) to the background OCO
watcher (`_task_oco_pair_watcher`'s `_oco_process_group_entry`).

Bracket/Template-GTT removal (T3.1, 2026-11) deleted the detection and
attach-entry stages (`template_attach.py`, the `_opp_build_attach_entries`
tests) — their test coverage was removed with them. The background OCO
watcher itself is untouched (explicitly out of scope for this
sub-sprint; its own removal is a later sub-sprint), and its tests below
are unaffected.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

# _gtt/_plan helpers, TestDetectScaleSlGroup and
# TestAttachEntriesCarrySiblingIdsOnSlOnly removed (Bracket/
# Template-GTT removal, T3.1, 2026-11) — all covered
# _ta_detect_scale_sl_group (backend.api.algo.template_attach, deleted)
# and _opp_build_attach_entries (orders_place.py, deleted). The OCO
# watcher tests below (background.py) are untouched — that module is
# explicitly out of scope for this sub-sprint.

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



# TestApplyPlanLiveIntegration removed (Bracket/Template-GTT removal,
# T3.1, 2026-11) — it ran end-to-end through apply_plan_live, deleted
# along with backend.api.algo.template_attach.


class TestOcoParseEntriesPicksUpGroupOnlyRows:
    def test_row_with_only_sibling_ids_is_included(self):
        """A row whose ONLY OCO pointer is the plural `sibling_ids`
        (no classic singular `sibling_id` anywhere) must still be
        picked up for the watcher to process — `_bg_oco_has_any_gtt_
        entry` is the gate `_oco_parse_entries` relies on.

        2026-11-04 amendment — `_oco_parse_entries` now derives the
        account from the row's OWN `.account` column (a real, NOT NULL
        AlgoOrder field), not by scanning entries for `parent_account`
        — see `_bg_oco_has_any_gtt_entry`'s docstring for why (a bare
        Kite GTT entry never carries `parent_account` at all). The
        fixture `_Row` class gained `.account` to match; the entries'
        own `parent_account` fields are left as-is (still read by the
        sibling-aware dispatch's position-flat gate)."""
        import json as _json
        from backend.api.background import _oco_parse_entries

        class _Row:
            id = 99
            account = "ZG0790"
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
