"""test_oco_orphan_gtt_cancel.py

2026-11-04 fix (live operator report: "when a position is closed [by
any means], the gtt order on that position [should be] closed
automatically... also, this should generate an alert in telegram, as
there is nothing to act on for ntfy").

Root cause: `_task_oco_pair_watcher`'s sibling-aware dispatch
(`_oco_process_one_entry`/`_oco_process_group_entry`) only ever reacts
when ONE leg of a tracked pair/group itself looks gone (fired,
cancelled, rejected) at the broker. It does nothing when a position
closes through a completely different path — a manual ticket close,
the `close_position`/`chase_close_positions` agent actions, or expiry
auto-close — while the attached GTT(s) are still resting untouched at
the broker. A bare Kite native 2-leg OCO GTT (no `sibling_id`/
`sibling_ids` at all — the broker links both legs server-side) was
ALSO never reachable by that sibling-aware path, and was excluded from
this entire watcher's row selection before this fix.

Covers:
  1. `_bg_oco_has_any_gtt_entry` — the broadened row-inclusion gate.
  2. `_oco_parse_entries` — now derives account from `row.account`
     directly and includes a row with only a bare `kind=="gtt"` entry.
  3. `_oco_cancel_closed_position_gtts` — the new generalized safety
     net: bare Kite entry, classic pair (both legs live), scale-ladder
     group (all legs live), the position-not-flat/unknown fail-safe
     gates, the alert it fires, and the stale sibling-pointer cleanup.
  4. `_oco_process_account_entries` — wiring: the new check runs for
     every row and its `changed` flag feeds the same persist path as
     the pre-existing sibling-aware dispatch.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


# ─────────────────────────────────────────────────────────────────────────
# 1. _bg_oco_has_any_gtt_entry (plain sync — no asyncio mark needed)
# ─────────────────────────────────────────────────────────────────────────

class TestBgOcoHasAnyGttEntry:
    def test_bare_kind_gtt_entry_with_no_sibling_fields_is_included(self):
        from backend.api.background import _bg_oco_has_any_gtt_entry

        attached = [{"kind": "gtt", "label": "OCO", "id": "G1"}]
        assert _bg_oco_has_any_gtt_entry(attached) is True

    def test_sibling_id_entry_is_included(self):
        from backend.api.background import _bg_oco_has_any_gtt_entry

        attached = [{"kind": "gtt", "id": "A", "sibling_id": "B"}]
        assert _bg_oco_has_any_gtt_entry(attached) is True

    def test_sibling_ids_entry_is_included(self):
        from backend.api.background import _bg_oco_has_any_gtt_entry

        attached = [{"kind": "gtt", "id": "SL1", "sibling_ids": ["TP1"]}]
        assert _bg_oco_has_any_gtt_entry(attached) is True

    def test_non_gtt_entry_excluded(self):
        from backend.api.background import _bg_oco_has_any_gtt_entry

        attached = [{"kind": "wing", "id": "W1"}]
        assert _bg_oco_has_any_gtt_entry(attached) is False

    def test_empty_list_excluded(self):
        from backend.api.background import _bg_oco_has_any_gtt_entry

        assert _bg_oco_has_any_gtt_entry([]) is False


# ─────────────────────────────────────────────────────────────────────────
# 2. _oco_parse_entries — bare-GTT row inclusion + row.account sourcing
#    (plain sync — no asyncio mark needed)
# ─────────────────────────────────────────────────────────────────────────

class TestOcoParseEntriesIncludesBareGttRows:
    def test_bare_kite_native_gtt_row_is_now_included(self):
        """Pre-fix: a row whose ONLY attached entry has no sibling_id/
        sibling_ids at all (Kite's native 2-leg OCO shape) was silently
        excluded from this entire watcher's row set. Confirmed to fail
        against the pre-fix code (which gated on `_bg_oco_first_sibling_
        account` finding a `parent_account` on a sibling entry — never
        present here)."""
        import json as _json
        from backend.api.background import _oco_parse_entries

        class _Row:
            id = 42
            account = "ZJ6294"
            attached_gtts_json = _json.dumps([
                {"kind": "gtt", "label": "OCO", "id": "KITE_GTT_1"},
            ])

        rows_by_account, attached_by_row = _oco_parse_entries([_Row()], _json.loads)

        assert "ZJ6294" in rows_by_account
        assert len(rows_by_account["ZJ6294"]) == 1
        assert 42 in attached_by_row

    def test_row_with_no_gtt_entries_at_all_still_excluded(self):
        import json as _json
        from backend.api.background import _oco_parse_entries

        class _Row:
            id = 43
            account = "ZJ6294"
            attached_gtts_json = _json.dumps([{"kind": "wing", "id": "W1"}])

        rows_by_account, attached_by_row = _oco_parse_entries([_Row()], _json.loads)
        assert rows_by_account == {}
        assert attached_by_row == {}

    def test_row_with_no_account_excluded(self):
        import json as _json
        from backend.api.background import _oco_parse_entries

        class _Row:
            id = 44
            account = None
            attached_gtts_json = _json.dumps([{"kind": "gtt", "id": "G1"}])

        rows_by_account, attached_by_row = _oco_parse_entries([_Row()], _json.loads)
        assert rows_by_account == {}


# ─────────────────────────────────────────────────────────────────────────
# 3. _oco_cancel_closed_position_gtts
# ─────────────────────────────────────────────────────────────────────────

def _row(id_=1, account="ZG0790", exchange="NFO", symbol="NIFTY25JULFUT"):
    r = MagicMock()
    r.id = id_
    r.account = account
    r.exchange = exchange
    r.symbol = symbol
    return r


class TestOcoCancelClosedPositionGtts:
    pytestmark = pytest.mark.asyncio

    async def test_bare_kite_gtt_cancelled_when_position_flat(self):
        """The core fix: a bare Kite native 2-leg GTT, still live, whose
        position closed through some other order — confirmed flat —
        must be cancelled even though it carries no sibling pointer at
        all."""
        from backend.api import background as bg_mod

        attached = [{"kind": "gtt", "id": "KITE_GTT_1"}]
        broker = MagicMock()
        broker.cancel_gtt = MagicMock()
        broker_gtts = {"KITE_GTT_1": {"status": "active"}}
        row = _row()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(bg_mod, "_oco_group_position_is_flat", AsyncMock(return_value=True))
            changed = await bg_mod._oco_cancel_closed_position_gtts(
                row, attached, broker, broker_gtts,
            )

        assert changed is True
        broker.cancel_gtt.assert_called_once_with("KITE_GTT_1", exchange="NFO")

    async def test_not_cancelled_when_position_not_flat(self):
        from backend.api import background as bg_mod

        attached = [{"kind": "gtt", "id": "KITE_GTT_1"}]
        broker = MagicMock()
        broker.cancel_gtt = MagicMock()
        broker_gtts = {"KITE_GTT_1": {"status": "active"}}
        row = _row()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(bg_mod, "_oco_group_position_is_flat", AsyncMock(return_value=False))
            changed = await bg_mod._oco_cancel_closed_position_gtts(
                row, attached, broker, broker_gtts,
            )

        assert changed is False
        broker.cancel_gtt.assert_not_called()

    async def test_not_cancelled_when_position_flat_is_unknown(self):
        """`None` (fetch failed / no matching row) must NEVER be
        treated as flat — fail-safe, same convention as every other
        caller of `_oco_group_position_is_flat`."""
        from backend.api import background as bg_mod

        attached = [{"kind": "gtt", "id": "KITE_GTT_1"}]
        broker = MagicMock()
        broker.cancel_gtt = MagicMock()
        broker_gtts = {"KITE_GTT_1": {"status": "active"}}
        row = _row()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(bg_mod, "_oco_group_position_is_flat", AsyncMock(return_value=None))
            changed = await bg_mod._oco_cancel_closed_position_gtts(
                row, attached, broker, broker_gtts,
            )

        assert changed is False
        broker.cancel_gtt.assert_not_called()

    async def test_no_live_entries_short_circuits_without_position_check(self):
        """Every attached GTT already gone at the broker — the function
        must return False WITHOUT even calling the (potentially
        expensive, real-broker-fetching) position-flat check."""
        from backend.api import background as bg_mod

        attached = [{"kind": "gtt", "id": "G1"}]
        broker = MagicMock()
        broker.cancel_gtt = MagicMock()
        broker_gtts = {"G1": {"status": "cancelled"}}
        row = _row()

        with pytest.MonkeyPatch.context() as mp:
            flat_check = AsyncMock(return_value=True)
            mp.setattr(bg_mod, "_oco_group_position_is_flat", flat_check)
            changed = await bg_mod._oco_cancel_closed_position_gtts(
                row, attached, broker, broker_gtts,
            )

        assert changed is False
        flat_check.assert_not_called()
        broker.cancel_gtt.assert_not_called()

    async def test_classic_pair_both_legs_live_cancels_both_and_strips_pointers(self):
        """Generalizes beyond 'one leg fired' — a Groww-emulated TP/SL
        pair where NEITHER leg has fired, but the position already
        closed via another order, must have BOTH legs cancelled and
        both stale sibling_id pointers stripped."""
        from backend.api import background as bg_mod

        entry_tp = {"kind": "gtt", "label": "TP", "id": "TP1", "sibling_id": "SL1"}
        entry_sl = {"kind": "gtt", "label": "SL", "id": "SL1", "sibling_id": "TP1"}
        attached = [entry_tp, entry_sl]
        broker = MagicMock()
        broker.cancel_gtt = MagicMock()
        broker_gtts = {"TP1": {"status": "active"}, "SL1": {"status": "active"}}
        row = _row()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(bg_mod, "_oco_group_position_is_flat", AsyncMock(return_value=True))
            changed = await bg_mod._oco_cancel_closed_position_gtts(
                row, attached, broker, broker_gtts,
            )

        assert changed is True
        assert broker.cancel_gtt.call_count == 2
        cancelled_ids = {c.args[0] for c in broker.cancel_gtt.call_args_list}
        assert cancelled_ids == {"TP1", "SL1"}
        assert "sibling_id" not in entry_tp
        assert "sibling_id" not in entry_sl

    async def test_scale_ladder_group_all_legs_live_cancels_all(self):
        """Generalizes the scale-ladder-plus-SL shape too — SL and
        every TP rung still live, position already closed elsewhere."""
        from backend.api import background as bg_mod

        entry_sl = {"kind": "gtt", "label": "SL", "id": "SL1", "sibling_ids": ["TP1", "TP2"]}
        entry_tp1 = {"kind": "gtt", "label": "TP+30%", "id": "TP1"}
        entry_tp2 = {"kind": "gtt", "label": "TP+60%", "id": "TP2"}
        attached = [entry_sl, entry_tp1, entry_tp2]
        broker = MagicMock()
        broker.cancel_gtt = MagicMock()
        broker_gtts = {
            "SL1": {"status": "active"}, "TP1": {"status": "active"}, "TP2": {"status": "active"},
        }
        row = _row()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(bg_mod, "_oco_group_position_is_flat", AsyncMock(return_value=True))
            changed = await bg_mod._oco_cancel_closed_position_gtts(
                row, attached, broker, broker_gtts,
            )

        assert changed is True
        cancelled_ids = {c.args[0] for c in broker.cancel_gtt.call_args_list}
        assert cancelled_ids == {"SL1", "TP1", "TP2"}
        assert "sibling_ids" not in entry_sl

    async def test_scale_ladder_sl_cancel_fails_partial_rung_prune(self):
        """SL's own cancel attempt fails (stays live, retried next
        cycle) while one TP rung cancels successfully and the other
        stays live — the SL entry's `sibling_ids` list must be pruned
        down to just the still-live rung, not cleared and not left
        stale with the gone one."""
        from backend.api import background as bg_mod

        entry_sl = {"kind": "gtt", "label": "SL", "id": "SL1", "sibling_ids": ["TP1", "TP2"]}
        entry_tp1 = {"kind": "gtt", "label": "TP+30%", "id": "TP1"}
        entry_tp2 = {"kind": "gtt", "label": "TP+60%", "id": "TP2"}
        attached = [entry_sl, entry_tp1, entry_tp2]
        broker = MagicMock()

        def _cancel(gtt_id, exchange=None):
            if gtt_id in ("SL1", "TP2"):
                raise RuntimeError("broker rejected cancel")
            return None

        broker.cancel_gtt = MagicMock(side_effect=_cancel)
        broker_gtts = {
            "SL1": {"status": "active"}, "TP1": {"status": "active"}, "TP2": {"status": "active"},
        }
        row = _row()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(bg_mod, "_oco_group_position_is_flat", AsyncMock(return_value=True))
            changed = await bg_mod._oco_cancel_closed_position_gtts(
                row, attached, broker, broker_gtts,
            )

        assert changed is True
        assert entry_sl["sibling_ids"] == ["TP2"]  # TP1 pruned, TP2 (still live) kept

    async def test_scale_ladder_sl_cancel_fails_all_rungs_cancelled_clears_sibling_ids(self):
        """SL's own cancel fails, but BOTH TP rungs cancel successfully
        — `sibling_ids` must be cleared entirely (the `else` branch,
        not left as an empty list)."""
        from backend.api import background as bg_mod

        entry_sl = {"kind": "gtt", "label": "SL", "id": "SL1", "sibling_ids": ["TP1", "TP2"]}
        entry_tp1 = {"kind": "gtt", "label": "TP+30%", "id": "TP1"}
        entry_tp2 = {"kind": "gtt", "label": "TP+60%", "id": "TP2"}
        attached = [entry_sl, entry_tp1, entry_tp2]
        broker = MagicMock()

        def _cancel(gtt_id, exchange=None):
            if gtt_id == "SL1":
                raise RuntimeError("broker rejected cancel")
            return None

        broker.cancel_gtt = MagicMock(side_effect=_cancel)
        broker_gtts = {
            "SL1": {"status": "active"}, "TP1": {"status": "active"}, "TP2": {"status": "active"},
        }
        row = _row()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(bg_mod, "_oco_group_position_is_flat", AsyncMock(return_value=True))
            changed = await bg_mod._oco_cancel_closed_position_gtts(
                row, attached, broker, broker_gtts,
            )

        assert changed is True
        assert "sibling_ids" not in entry_sl

    async def test_partial_cancel_failure_only_strips_the_succeeded_one(self):
        """One broker cancel call fails (still-live id) — the entry
        whose OWN cancel succeeded has its pointer stripped, and so
        does its surviving partner's now-dangling pointer TO it (SL1's
        own `sibling_id` pointed at TP1, which is now gone — that
        pointer is stale and must not linger even though SL1 itself is
        still live and will be retried next cycle). The function still
        reports changed=True for the one real cancellation."""
        from backend.api import background as bg_mod

        entry_tp = {"kind": "gtt", "id": "TP1", "sibling_id": "SL1"}
        entry_sl = {"kind": "gtt", "id": "SL1", "sibling_id": "TP1"}
        attached = [entry_tp, entry_sl]
        broker = MagicMock()

        def _cancel(gtt_id, exchange=None):
            if gtt_id == "SL1":
                raise RuntimeError("broker rejected cancel")
            return None

        broker.cancel_gtt = MagicMock(side_effect=_cancel)
        broker_gtts = {"TP1": {"status": "active"}, "SL1": {"status": "active"}}
        row = _row()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(bg_mod, "_oco_group_position_is_flat", AsyncMock(return_value=True))
            changed = await bg_mod._oco_cancel_closed_position_gtts(
                row, attached, broker, broker_gtts,
            )

        assert changed is True
        assert "sibling_id" not in entry_tp  # TP1 cancelled successfully
        assert "sibling_id" not in entry_sl  # stale pointer to now-gone TP1 cleared
        assert entry_sl["id"] == "SL1"  # SL1's own entry is untouched otherwise — still live, retried next cycle

    async def test_missing_row_account_returns_false_without_crashing(self):
        from backend.api import background as bg_mod

        attached = [{"kind": "gtt", "id": "G1"}]
        broker = MagicMock()
        broker.cancel_gtt = MagicMock()
        broker_gtts = {"G1": {"status": "active"}}
        row = _row(account=None)

        changed = await bg_mod._oco_cancel_closed_position_gtts(
            row, attached, broker, broker_gtts,
        )

        assert changed is False
        broker.cancel_gtt.assert_not_called()

    async def test_all_cancel_attempts_fail_returns_false(self):
        """Every cancel attempt fails at the broker (none actually
        cancelled) — must return False (no mutation, no alert), not
        True, even though a cancel was genuinely attempted."""
        from backend.api import background as bg_mod

        attached = [{"kind": "gtt", "id": "G1"}]
        broker = MagicMock()
        broker.cancel_gtt = MagicMock(side_effect=RuntimeError("broker down"))
        broker_gtts = {"G1": {"status": "active"}}
        row = _row()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(bg_mod, "_oco_group_position_is_flat", AsyncMock(return_value=True))
            changed = await bg_mod._oco_cancel_closed_position_gtts(
                row, attached, broker, broker_gtts,
            )

        assert changed is False

    async def test_strip_stale_sibling_pointers_skips_non_dict_entries(self):
        """A malformed/non-dict item in `attached` (defensive — should
        never happen in practice) must be skipped, not raise."""
        from backend.api.background import _oco_strip_stale_sibling_pointers

        attached = ["not-a-dict", {"id": "G1", "sibling_id": "G2"}]
        _oco_strip_stale_sibling_pointers(attached, {"G1"})
        assert attached[0] == "not-a-dict"
        assert "sibling_id" not in attached[1]

    async def test_orphan_cancel_alert_failure_is_swallowed(self, monkeypatch):
        """A failure while building/sending the alert (e.g.
        `generic_notice_identity_extra` raising) must never propagate —
        the real cancel already happened and must not be undone or
        surfaced as an error."""
        from backend.api import background as bg_mod

        async def _boom(alert_event):
            raise RuntimeError("identity lookup failed")

        monkeypatch.setattr(
            "backend.api.algo.event_agents.generic_notice_identity_extra", _boom,
        )

        # Should not raise.
        await bg_mod._oco_orphan_cancel_alert("ZG0790", "NIFTY25JULFUT", {"G1"})

    async def test_fires_telegram_only_alert_on_real_cancel(self, monkeypatch):
        """Operator: 'this should generate an alert in telegram, as
        there is nothing to act on for ntfy' — confirms the log call
        carries `alert_event='gtt_orphan_cancelled'` with no
        `action_required` flag (so it never forces ntfy/email), and
        that `alert_routing.py`'s per-slug override for this slug is
        telegram-only."""
        from backend.api import background as bg_mod
        from backend.api.algo import alert_routing

        assert alert_routing._PER_SLUG_OVERRIDES["gtt-orphan-cancelled-alert"] == [
            {"channel": "telegram", "enabled": True, "gate": False},
        ]

        attached = [{"kind": "gtt", "id": "KITE_GTT_1"}]
        broker = MagicMock()
        broker.cancel_gtt = MagicMock()
        broker_gtts = {"KITE_GTT_1": {"status": "active"}}
        row = _row()

        captured = {}

        def _fake_info(fmt, *args, **kwargs):
            captured["extra"] = kwargs.get("extra") or {}

        monkeypatch.setattr(bg_mod, "_oco_group_position_is_flat", AsyncMock(return_value=True))
        monkeypatch.setattr(bg_mod.logger, "info", _fake_info)

        changed = await bg_mod._oco_cancel_closed_position_gtts(
            row, attached, broker, broker_gtts,
        )

        assert changed is True
        extra = captured["extra"]
        assert extra["alert_event"] == "gtt_orphan_cancelled"
        assert extra["tags"] == ["gtt", "alert"]
        assert "action_required" not in extra


# ─────────────────────────────────────────────────────────────────────────
# 4. _oco_process_account_entries wiring
# ─────────────────────────────────────────────────────────────────────────

class TestOcoProcessAccountEntriesWiring:
    pytestmark = pytest.mark.asyncio

    async def test_bare_gtt_row_persisted_when_orphan_cancel_fires(self):
        """End-to-end through the per-account loop: a bare Kite GTT row
        (no sibling pairing) that the sibling-aware dispatch would
        never have touched must still land in the `pending` persist
        list once the new orphan-cancel check mutates it."""
        from backend.api import background as bg_mod

        row = _row(id_=7)
        attached = [{"kind": "gtt", "id": "KITE_GTT_1"}]
        acct_rows = [row]
        attached_by_row = {7: attached}
        broker_gtts = {"KITE_GTT_1": {"status": "active"}}

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(bg_mod, "_oco_group_position_is_flat", AsyncMock(return_value=True))

            class _FakeBroker:
                def cancel_gtt(self, gtt_id, exchange=None):
                    return None

            mp.setattr(bg_mod, "get_broker", lambda acct: _FakeBroker(), raising=False)
            from backend.brokers import registry as _registry_mod
            mp.setattr(_registry_mod, "get_broker", lambda acct: _FakeBroker())

            pending = await bg_mod._oco_process_account_entries(
                "ZG0790", acct_rows, attached_by_row, broker_gtts,
            )

        assert len(pending) == 1
        assert pending[0][0] == 7

    async def test_row_with_nothing_live_produces_no_pending_update(self):
        from backend.api import background as bg_mod

        row = _row(id_=8)
        attached = [{"kind": "gtt", "id": "G1"}]
        acct_rows = [row]
        attached_by_row = {8: attached}
        broker_gtts = {"G1": {"status": "cancelled"}}

        class _FakeBroker:
            def cancel_gtt(self, gtt_id, exchange=None):
                return None

        with pytest.MonkeyPatch.context() as mp:
            from backend.brokers import registry as _registry_mod
            mp.setattr(_registry_mod, "get_broker", lambda acct: _FakeBroker())
            pending = await bg_mod._oco_process_account_entries(
                "ZG0790", acct_rows, attached_by_row, broker_gtts,
            )

        assert pending == []
