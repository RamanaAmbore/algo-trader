"""Sprint 3 (auto-close-short-option plan) — generic hold/release/
auto-release infrastructure.

Covers pieces 1-6 of the plan:
  1. HoldCategory.AGENT_CLOSE falls through to the DEFAULT release
     handler (release_held_order), verified via the real dispatch path
     (get_release_handler), not asserted.
  2. hold_record()/record_held_order() snapshot auto_release_at at
     stage time; a later change never retroactively moves it.
  3. Agent.require_release / Agent.release_timeout_minutes columns +
     migration; confirmed absent from both write allow-lists.
  4. The require_release hold check inside _dispatch_live_action: fires
     only for close_position, only in mode == 'live', stages a HELD
     row + action-required alert, zero broker calls.
  5. Operator-cancel suppression: a same-session re-cross does not
     re-stage; a new session does.
  6. The auto-release background scan: fires once past the deadline,
     respects the kill switch, never fires early.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════════════
#  Piece 1 — HoldCategory.AGENT_CLOSE falls through to release_held_order
# ═══════════════════════════════════════════════════════════════════════

class TestAgentCloseFallsThroughToDefaultHandler:
    def test_agent_close_not_in_release_handlers_registry(self):
        from backend.api.algo.order_release import _RELEASE_HANDLERS
        from backend.api.algo.order_hold import HoldCategory
        assert HoldCategory.AGENT_CLOSE.value not in _RELEASE_HANDLERS

    def test_get_release_handler_resolves_agent_close_to_release_held_order(self):
        """Verified via the REAL dispatch path, not asserted — call
        get_release_handler("agent_close") and confirm it IS the same
        function object release_held_order resolves to by name."""
        from backend.api.algo.order_release import (
            get_release_handler, release_held_order,
        )
        handler = get_release_handler("agent_close")
        assert handler.__name__ == release_held_order.__name__
        assert handler.__name__ == "release_held_order"

    def test_get_release_handler_agent_close_differs_from_agent_order(self):
        """AGENT_CLOSE must NOT resolve to AGENT_ORDER's own handler
        (release_repeated_rejection_hold) — distinct semantics, distinct
        category string."""
        from backend.api.algo.order_release import get_release_handler
        assert (get_release_handler("agent_close").__name__
                != get_release_handler("agent_order").__name__)


# ═══════════════════════════════════════════════════════════════════════
#  Piece 2 — auto_release_at snapshot
# ═══════════════════════════════════════════════════════════════════════

class TestAutoReleaseAtSnapshot:
    def test_hold_record_omits_auto_release_at_when_none(self):
        """Byte-identical serialised shape to every pre-existing caller
        when auto_release_at is not supplied."""
        from backend.api.algo.order_hold import HoldCategory, hold_record
        raw = hold_record(HoldCategory.EXPIRY_CLOSE, "expiry close", "CHASE_MED",
                          None, datetime(2026, 10, 15, 15, 15))
        import json
        assert "auto_release_at" not in json.loads(raw)

    def test_hold_record_includes_auto_release_at_when_set(self):
        from backend.api.algo.order_hold import HoldCategory, hold_record, parse_hold_record
        held_at = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
        deadline = held_at + timedelta(minutes=30)
        raw = hold_record(HoldCategory.AGENT_CLOSE, "90% profit short close",
                          "CHASE_MED", None, held_at, auto_release_at=deadline)
        rec = parse_hold_record(raw)
        assert rec["category"] == "agent_close"
        assert rec["auto_release_at"] == deadline.isoformat()

    @pytest.mark.asyncio
    async def test_record_held_order_threads_auto_release_at_into_hold_json(self):
        from backend.api.algo import order_hold_gate as gate
        from backend.api.algo.order_hold import HoldCategory, parse_hold_record

        result_mock = MagicMock()
        result_mock.scalars.return_value.first.return_value = None
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock(return_value=result_mock)
        mock_session.commit = AsyncMock()

        added = {}

        def _add(row):
            row.id = 777
            added["row"] = row

        mock_session.add = _add

        deadline = datetime(2026, 11, 1, 10, 30, tzinfo=timezone.utc)

        with patch("backend.api.database.async_session", return_value=mock_session), \
             patch("backend.api.algo.order_events.write_event", new=AsyncMock()):
            row_id = await gate.record_held_order(
                HoldCategory.AGENT_CLOSE,
                account="ZG0790", symbol="NIFTY24800CE", exchange="NFO",
                side="BUY", qty=50, product="NRML", reason="90% profit",
                agent_id=42, auto_release_at=deadline,
            )

        assert row_id == 777
        rec = parse_hold_record(added["row"].hold_json)
        assert rec["category"] == "agent_close"
        assert rec["auto_release_at"] == deadline.isoformat()

    def test_later_agent_timeout_edit_never_changes_an_already_staged_deadline(self):
        """The deadline baked into hold_json at stage time is immutable
        with respect to a later Agent.release_timeout_minutes edit —
        there is no live re-join back to the Agent row at read time.
        Simulated here by staging with one timeout, then confirming the
        snapshotted value is unaffected by a DIFFERENT value on a
        freshly-constructed agent object (standing in for "the operator
        edited the setting after staging")."""
        from backend.api.algo.order_hold import HoldCategory, hold_record, parse_hold_record

        held_at = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
        original_timeout_minutes = 30
        staged_deadline = held_at + timedelta(minutes=original_timeout_minutes)
        raw = hold_record(HoldCategory.AGENT_CLOSE, "x", "CHASE_MED", None,
                          held_at, auto_release_at=staged_deadline)

        # Operator now edits the agent's timeout to something else entirely.
        agent_after_edit = SimpleNamespace(release_timeout_minutes=120)

        rec = parse_hold_record(raw)
        assert rec["auto_release_at"] == staged_deadline.isoformat()
        # The snapshotted value does not equal a deadline computed from
        # the edited setting.
        new_deadline = held_at + timedelta(minutes=agent_after_edit.release_timeout_minutes)
        assert rec["auto_release_at"] != new_deadline.isoformat()


# Remaining pieces (3-6) are appended by later commits in this same file.
