"""
Sprint 8 (CLI/text grammar agent-authoring effort) — wires the additive
`OrderTemplate.group_name` / `OrderTemplate.tags` ORM columns (added in
Sprint 7, see test_agents_group_and_templates_group_tags_migration.py)
into `OrderTemplateController`'s request/response schemas and route
handlers (`backend/api/routes/templates.py`).

Mirrors the exact `group`→`group_name` reserved-SQL-keyword fix already
proven for `Agent`/`AgentUpdateRequest` in `backend/api/routes/agents.py`
(`update_agent`'s explicit `if data.group is not None: agent.group_name
= data.group` outside the generic setattr loop) — the identical trap
exists here: `OrderTemplate` has no `group` column, only `group_name`,
so a naive `setattr(row, "group", ...)` inside the generic
`_apply_patch_fields` loop would silently create a bogus instance
attribute instead of persisting anything.

Covers:
  1. `_to_out` surfaces `group`/`tags` from the ORM row (group_name →
     group rename, tags passthrough).
  2. `create_template` persists `data.group` → `OrderTemplate.group_name`
     and `data.tags` → `OrderTemplate.tags` (direct constructor kwargs,
     not the patch-loop).
  3. `_apply_patch_fields` persists `tags` via the generic setattr loop
     (same request/ORM field name) but does NOT touch `group`/`group_name`
     at all — proving `group` was correctly excluded from `_PATCH_FIELDS`.
  4. `patch_template`'s own explicit `if data.group is not None: row.group_name
     = data.group` line actually runs and persists — the real trap-avoidance
     fix, tested at the full-handler level (not just the extracted helper).
  5. `None` on either field in a PATCH means "leave unchanged" (existing
     convention for every other field in this struct).

Five quality dimensions:
  SSOT   — tests the real `_to_out` / `_apply_patch_fields` / `create_template`
           / `patch_template` functions, not reimplementations.
  Perf   — n/a (no perf claim in this fix).
  Stale  — directly reproduces the Sprint 7 `Agent.group_name` /
           `AgentUpdateRequest.group` trap this change mirrors; asserts
           `group` is excluded from `_PATCH_FIELDS` so a future regression
           (someone "helpfully" adding `"group"` back into that tuple)
           is caught immediately.
  Reuse  — same `_FakeOrderTemplateRow`-style minimal-row pattern already
           used in test_wing_max_spread_pct_override.py; same mocked-session
           pattern already used in test_order_release_template_exit.py.
  UX     — n/a (backend-only; frontend grammar wiring is a parallel task).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.api.models import OrderTemplate
from backend.api.routes.templates import (
    OrderTemplateController,
    _apply_patch_fields,
    _PATCH_FIELDS,
    _to_out,
)
from backend.api.schemas import OrderTemplateCreate, OrderTemplatePatch


def _handler_fn(handler):
    """Extract the raw coroutine from a Litestar-decorated method —
    same helper used in test_agent_kind_tier_topic.py. Litestar's
    `Controller.__init__` requires a real `owner` (router) argument,
    so route-handler methods are invoked as plain unbound functions
    against a `__new__`-constructed instance instead."""
    return handler.fn


def _new_controller() -> OrderTemplateController:
    return OrderTemplateController.__new__(OrderTemplateController)


# ── _to_out — group_name → group rename, tags passthrough ────────────────

class TestToOutSurfacesGroupAndTags:
    def test_group_name_surfaced_as_group(self):
        row = OrderTemplate(name="t", applies_to="both")
        row.id = 1
        row.group_name = "scalping"
        row.tags = ["fast", "intraday"]
        out = _to_out(row)
        assert out.group == "scalping"
        assert out.tags == ["fast", "intraday"]

    def test_none_group_name_surfaced_as_none(self):
        row = OrderTemplate(name="t", applies_to="both")
        row.id = 1
        row.group_name = None
        row.tags = []
        out = _to_out(row)
        assert out.group is None
        assert out.tags == []

    def test_null_tags_defaults_to_empty_list_not_none(self):
        """Defensive: a row whose `tags` column is somehow None (e.g. a
        pre-migration row read before the server_default back-fills)
        must not surface `None` through the API — list(None or []) → []."""
        row = OrderTemplate(name="t", applies_to="both")
        row.id = 1
        row.group_name = None
        row.tags = None
        out = _to_out(row)
        assert out.tags == []


# ── _PATCH_FIELDS — structural guard against the reserved-keyword trap ───

class TestPatchFieldsExcludesGroup:
    def test_tags_is_in_patch_fields(self):
        assert "tags" in _PATCH_FIELDS

    def test_group_is_not_in_patch_fields(self):
        """`group` must NEVER be added to the generic setattr-loop tuple —
        OrderTemplate has no `group` column (only `group_name`), so a
        setattr(row, "group", ...) would silently create a bogus instance
        attribute instead of persisting. Mirrors the Agent.group_name /
        AgentUpdateRequest.group trap documented in agents.py."""
        assert "group" not in _PATCH_FIELDS


# ── _apply_patch_fields — tags via loop, group via explicit line ─────────

class TestApplyPatchFields:
    def test_tags_applied_via_generic_loop(self):
        row = OrderTemplate(name="t", applies_to="both")
        row.tags = []
        data = OrderTemplatePatch(tags=["swing", "options"])
        _apply_patch_fields(row, data)
        assert row.tags == ["swing", "options"]

    def test_group_persisted_via_explicit_line_not_generic_loop(self):
        """`_apply_patch_fields` persists `group` → `group_name` via its
        own explicit post-loop line (NOT via the generic `_PATCH_FIELDS`
        setattr loop — see `test_group_is_not_in_patch_fields` above,
        which proves `group` never enters that loop at all). Confirms
        the mutation actually happens, and that no bogus `.group`
        instance attribute is ever created in the process."""
        row = OrderTemplate(name="t", applies_to="both")
        row.group_name = "original"
        data = OrderTemplatePatch(group="new-group")
        _apply_patch_fields(row, data)
        assert row.group_name == "new-group"
        assert not hasattr(row, "group")

    def test_none_group_means_unchanged_in_apply_patch_fields(self):
        row = OrderTemplate(name="t", applies_to="both")
        row.group_name = "keep-this"
        data = OrderTemplatePatch()  # group=None (unset)
        _apply_patch_fields(row, data)
        assert row.group_name == "keep-this"

    def test_none_tags_means_unchanged(self):
        row = OrderTemplate(name="t", applies_to="both")
        row.tags = ["keep", "me"]
        data = OrderTemplatePatch()  # tags=None (unset)
        _apply_patch_fields(row, data)
        assert row.tags == ["keep", "me"]


# ── Full handler round-trip — create_template ─────────────────────────────

def _mock_session_for_create(dup_exists: bool = False):
    dup_result = MagicMock()
    dup_result.scalar_one_or_none.return_value = (
        MagicMock() if dup_exists else None
    )
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(return_value=dup_result)
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    return session


class TestCreateTemplatePersistsGroupAndTags:
    @pytest.mark.asyncio
    async def test_group_and_tags_persisted_to_orm_row(self):
        session = _mock_session_for_create()
        data = OrderTemplateCreate(
            name="My Scalper",
            applies_to="buy_any",
            group="scalping",
            tags=["fast", "intraday"],
        )
        with patch(
            "backend.api.routes.templates.async_session",
            return_value=session,
        ):
            controller = _new_controller()
            out = await _handler_fn(OrderTemplateController.create_template)(controller, data)

        # The row passed to session.add() is the real OrderTemplate ORM
        # instance constructed inside create_template — inspect it
        # directly rather than re-deriving from `out`.
        # Sprint E — create_template now ALSO calls session.add() for
        # the mirrored Agent(kind='bracket') row; find the OrderTemplate-
        # typed call specifically rather than assuming it's the last one.
        added_row = next(
            c.args[0] for c in session.add.call_args_list
            if isinstance(c.args[0], OrderTemplate)
        )
        assert isinstance(added_row, OrderTemplate)
        assert added_row.group_name == "scalping"
        assert added_row.tags == ["fast", "intraday"]
        assert not hasattr(added_row, "group")

        assert out.group == "scalping"
        assert out.tags == ["fast", "intraday"]

    @pytest.mark.asyncio
    async def test_absent_group_and_tags_default_safely(self):
        session = _mock_session_for_create()
        data = OrderTemplateCreate(name="Plain Template", applies_to="both")
        with patch(
            "backend.api.routes.templates.async_session",
            return_value=session,
        ):
            controller = _new_controller()
            out = await _handler_fn(OrderTemplateController.create_template)(controller, data)

        # Sprint E — create_template now ALSO calls session.add() for
        # the mirrored Agent(kind='bracket') row; find the OrderTemplate-
        # typed call specifically rather than assuming it's the last one.
        added_row = next(
            c.args[0] for c in session.add.call_args_list
            if isinstance(c.args[0], OrderTemplate)
        )
        assert added_row.group_name is None
        assert added_row.tags == []
        assert out.group is None
        assert out.tags == []


# ── Full handler round-trip — patch_template ──────────────────────────────

def _mock_session_for_patch(row):
    # Sprint E — patch_template now also calls bracket_registry.
    # mirror_bracket_upsert(s, row), which runs its own
    # `select(Agent).where(Agent.slug == slug)` through `session.execute`.
    # Must return a plain (non-async) result object whose
    # `scalar_one_or_none()` resolves synchronously to None (no
    # colliding Agent row) — the same shape `_mock_session_for_create`'s
    # `dup_result` already uses for the analogous OrderTemplate
    # duplicate-name check.
    no_collision_result = MagicMock()
    no_collision_result.scalar_one_or_none.return_value = None
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.get = AsyncMock(return_value=row)
    session.execute = AsyncMock(return_value=no_collision_result)
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    return session


class TestPatchTemplatePersistsGroupAndTags:
    @pytest.mark.asyncio
    async def test_group_persisted_via_explicit_handler_line(self):
        """The real trap-avoidance test: patch_template's own explicit
        `if data.group is not None: row.group_name = data.group` line
        (NOT the generic _apply_patch_fields loop) is what must persist
        this — exercised here at the full-handler level."""
        row = OrderTemplate(name="Existing", applies_to="both")
        row.id = 42
        row.group_name = None
        row.tags = []
        session = _mock_session_for_patch(row)

        data = OrderTemplatePatch(group="new-group")
        with patch(
            "backend.api.routes.templates.async_session",
            return_value=session,
        ):
            controller = _new_controller()
            out = await _handler_fn(OrderTemplateController.patch_template)(controller, 42, data)

        assert row.group_name == "new-group"
        assert out.group == "new-group"
        assert not hasattr(row, "group")

    @pytest.mark.asyncio
    async def test_tags_persisted_via_generic_loop_in_full_handler(self):
        row = OrderTemplate(name="Existing", applies_to="both")
        row.id = 43
        row.group_name = None
        row.tags = ["old"]
        session = _mock_session_for_patch(row)

        data = OrderTemplatePatch(tags=["new", "fresh"])
        with patch(
            "backend.api.routes.templates.async_session",
            return_value=session,
        ):
            controller = _new_controller()
            out = await _handler_fn(OrderTemplateController.patch_template)(controller, 43, data)

        assert row.tags == ["new", "fresh"]
        assert out.tags == ["new", "fresh"]

    @pytest.mark.asyncio
    async def test_none_group_means_unchanged_in_full_handler(self):
        row = OrderTemplate(name="Existing", applies_to="both")
        row.id = 44
        row.group_name = "keep-this"
        row.tags = []
        session = _mock_session_for_patch(row)

        data = OrderTemplatePatch()  # group=None, tags=None → unset
        with patch(
            "backend.api.routes.templates.async_session",
            return_value=session,
        ):
            controller = _new_controller()
            out = await _handler_fn(OrderTemplateController.patch_template)(controller, 44, data)

        assert row.group_name == "keep-this"
        assert out.group == "keep-this"

    @pytest.mark.asyncio
    async def test_both_group_and_tags_persisted_together(self):
        row = OrderTemplate(name="Existing", applies_to="both")
        row.id = 45
        row.group_name = None
        row.tags = []
        session = _mock_session_for_patch(row)

        data = OrderTemplatePatch(group="combo", tags=["a", "b"])
        with patch(
            "backend.api.routes.templates.async_session",
            return_value=session,
        ):
            controller = _new_controller()
            out = await _handler_fn(OrderTemplateController.patch_template)(controller, 45, data)

        assert row.group_name == "combo"
        assert row.tags == ["a", "b"]
        assert out.group == "combo"
        assert out.tags == ["a", "b"]
