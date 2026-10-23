"""test_template_wing_premium_validation.py

Covers audit finding T19 (2026-10-23 template/bracket audit, cluster 2F):
Template CRUD only validated `tp_order_type` / `tp_scales_json` /
`sl_trail_pct`. A template saved with `wing_premium_pct<=0` was accepted
at CRUD time, then on every subsequent fill
`template_attach.py:_parse_template_overrides` raised
`HTTPException(422)` -- caught by `_fire_template_attach_on_fill`'s
generic `except Exception` and logged at WARNING only, with no alert
and no `template_attach_failed` event. The template silently unarmed
forever with no operator signal.

Fix: (1) `templates.py:_validate_wing_premium_pct` rejects the save
outright, mirroring the resolver's own rejection rule exactly, on both
create and PATCH. (2) Defense-in-depth:
`orders_place.py:_opp_handle_fill_path_exception` fires the real
`template_attach_failed` alert specifically for an `HTTPException`
caught in `_fire_template_attach_on_fill`'s except block, covering any
already-saved bad template from before this fix shipped.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.api.models import OrderTemplate
from backend.api.routes.templates import OrderTemplateController
from backend.api.schemas import OrderTemplateCreate, OrderTemplatePatch


def _handler_fn(handler):
    return handler.fn


def _new_controller() -> OrderTemplateController:
    return OrderTemplateController.__new__(OrderTemplateController)


def _mock_session_for_create():
    dup_result = MagicMock()
    dup_result.scalar_one_or_none.return_value = None
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(return_value=dup_result)
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    return session


def _mock_session_for_patch(row):
    no_collision = MagicMock()
    no_collision.scalar_one_or_none.return_value = None
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.get = AsyncMock(return_value=row)
    session.execute = AsyncMock(return_value=no_collision)
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    return session


class TestValidateWingPremiumPctHelper:
    def test_rejects_zero(self):
        from backend.api.routes.templates import _validate_wing_premium_pct
        from litestar.exceptions import HTTPException
        with pytest.raises(HTTPException):
            _validate_wing_premium_pct(0.0)

    def test_rejects_negative(self):
        from backend.api.routes.templates import _validate_wing_premium_pct
        from litestar.exceptions import HTTPException
        with pytest.raises(HTTPException):
            _validate_wing_premium_pct(-5.0)

    def test_accepts_positive(self):
        from backend.api.routes.templates import _validate_wing_premium_pct
        _validate_wing_premium_pct(10.0)  # must not raise

    def test_accepts_none(self):
        from backend.api.routes.templates import _validate_wing_premium_pct
        _validate_wing_premium_pct(None)  # must not raise


class TestCreateTemplateRejectsBadWingPremiumPct:
    @pytest.mark.asyncio
    async def test_create_rejects_zero_wing_premium_pct(self):
        """Pre-fix: this save succeeds silently — the bad value is only
        caught later, on every fill thereafter."""
        from litestar.exceptions import HTTPException

        session = _mock_session_for_create()
        data = OrderTemplateCreate(
            name="Bad Wing Template", applies_to="both",
            wing_premium_pct=0.0,
        )
        with patch("backend.api.routes.templates.async_session",
                   return_value=session):
            controller = _new_controller()
            with pytest.raises(HTTPException) as exc_info:
                await _handler_fn(OrderTemplateController.create_template)(
                    controller, data,
                )
        assert exc_info.value.status_code == 400
        session.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_accepts_positive_wing_premium_pct(self):
        from backend.api.algo.bracket_registry import BracketSlugCollisionError
        session = _mock_session_for_create()
        data = OrderTemplateCreate(
            name="Good Wing Template", applies_to="both",
            wing_premium_pct=10.0,
        )
        with patch("backend.api.routes.templates.async_session",
                   return_value=session), \
             patch("backend.api.algo.bracket_registry.mirror_bracket_upsert",
                   new=AsyncMock()):
            controller = _new_controller()
            out = await _handler_fn(OrderTemplateController.create_template)(
                controller, data,
            )
        assert out.wing_premium_pct == 10.0


class TestPatchTemplateRejectsBadWingPremiumPct:
    @pytest.mark.asyncio
    async def test_patch_rejects_zero_wing_premium_pct(self):
        from litestar.exceptions import HTTPException

        row = OrderTemplate(id=5, name="t", applies_to="both")
        session = _mock_session_for_patch(row)
        data = OrderTemplatePatch(wing_premium_pct=0.0)
        with patch("backend.api.routes.templates.async_session",
                   return_value=session):
            controller = _new_controller()
            with pytest.raises(HTTPException) as exc_info:
                await _handler_fn(OrderTemplateController.patch_template)(
                    controller, 5, data,
                )
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_patch_accepts_none_wing_premium_pct_leaves_unchanged(self):
        """None on a PATCH field means 'leave unchanged' — must not
        trip the new validator."""
        row = OrderTemplate(id=5, name="t", applies_to="both", wing_premium_pct=15.0)
        session = _mock_session_for_patch(row)
        data = OrderTemplatePatch(name="renamed")
        with patch("backend.api.routes.templates.async_session",
                   return_value=session), \
             patch("backend.api.algo.bracket_registry.mirror_bracket_upsert",
                   new=AsyncMock()):
            controller = _new_controller()
            out = await _handler_fn(OrderTemplateController.patch_template)(
                controller, 5, data,
            )
        assert out.wing_premium_pct == 15.0
        assert out.name == "renamed"


class TestFillPathHttpExceptionFiresAlertInsteadOfSilentWarning:
    @pytest.mark.asyncio
    async def test_opp_handle_fill_path_exception_fires_alert_for_http_exception(self):
        from backend.api.routes import orders_place as opl
        from litestar.exceptions import HTTPException

        exc = HTTPException(status_code=422, detail="wing_premium_pct must be > 0")
        with patch("backend.api.algo.template_attach._fire_attach_fail_alert") as mock_alert:
            await opl._opp_handle_fill_path_exception(
                exc, 4242, "NIFTYFUT", "ZG0790", None,
            )
        mock_alert.assert_called_once()
        _, kwargs = mock_alert.call_args
        assert kwargs["order_id"] == 4242
        assert "wing_premium_pct" in kwargs["errors"][0]

    @pytest.mark.asyncio
    async def test_opp_handle_fill_path_exception_stays_silent_for_other_exceptions(self):
        """Scope discipline: a non-HTTPException failure keeps its
        existing silent-WARNING-only behaviour — unchanged."""
        from backend.api.routes import orders_place as opl

        with patch("backend.api.algo.template_attach._fire_attach_fail_alert") as mock_alert:
            await opl._opp_handle_fill_path_exception(
                RuntimeError("some other failure"), 4242, "NIFTYFUT", "ZG0790", None,
            )
        mock_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_fire_template_attach_on_fill_alerts_on_bad_saved_template(self):
        """End-to-end through the real lock + except block: a template
        saved before this fix existed (wing_premium_pct<=0) still fires
        a real alert on fill instead of a silent warning."""
        from backend.api.routes import orders_place as opl
        from litestar.exceptions import HTTPException

        opl._TPL_ATTACH_PERSIST_PENDING.clear()
        row_info = {"overrides": {"wing_premium_pct": 0.0}, "agent_id": None}

        async def _raising_apply(**kwargs):
            raise HTTPException(status_code=422, detail="wing_premium_pct must be > 0")

        with patch("backend.api.algo.template_attach.apply_template_to_order",
                   new=_raising_apply), \
             patch.object(opl, "_opp_load_row_for_attach",
                           new=AsyncMock(return_value=row_info)), \
             patch("backend.api.algo.template_attach._fire_attach_fail_alert") as mock_alert, \
             patch("backend.api.algo.order_hold_gate.template_exit_held", return_value=False), \
             patch("backend.api.algo.order_hold_gate.template_exit_override",
                   new=AsyncMock(return_value=None)):
            result = await opl._fire_template_attach_on_fill(
                parent_row_id=5050, parent_account="ZG0790",
                parent_symbol="NIFTYFUT", parent_exchange="NFO",
                parent_side="SELL", parent_qty=50, fill_price=100.0,
                template_id=1, mode="live",
            )

        assert result is None
        mock_alert.assert_called_once()
        opl._TPL_ATTACH_PERSIST_PENDING.clear()
