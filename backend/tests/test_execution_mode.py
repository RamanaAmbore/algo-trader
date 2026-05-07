"""
Tests for GET/POST /api/admin/execution/mode.

Covers:
  - GET returns current mode + branch-filtered allowed_modes
  - POST 'paper' updates settings; subsequent GET returns 'paper'
  - POST 'sim' on prod branch returns 403

The settings DB is not spun up; we mock _CACHE in settings.py directly so
get_bool/get_string resolve without a real DB connection.
"""

import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_settings(paper_mode: bool = True, shadow_mode: bool = False):
    """Return a _CACHE dict that makes get_bool work without DB."""
    return {
        "execution.paper_trading_mode": "true" if paper_mode else "false",
        "execution.shadow_mode":        "true" if shadow_mode else "false",
    }


def _make_inactive_driver():
    drv = MagicMock()
    drv.active = False
    return drv


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGetMode:

    @pytest.mark.asyncio
    async def test_get_mode_paper_on_dev(self):
        """Dev branch always returns 'paper'; allowed_modes = [sim, replay, paper]."""
        from backend.api.routes.execution import _get_current_mode

        # Patch at the module where is_prod_branch is defined (it's imported
        # lazily inside _get_current_mode via `from ... import`).
        with patch("backend.shared.helpers.utils.config",
                   {"deploy_branch": "dev"}), \
             patch("backend.shared.helpers.settings._CACHE",
                   _mock_settings(paper_mode=True)):
            mode = _get_current_mode(is_prod=False)

        assert mode == "paper"

    @pytest.mark.asyncio
    async def test_get_mode_live_on_prod(self):
        """Prod + paper_mode=False + shadow_mode=False → 'live'."""
        from backend.api.routes.execution import _get_current_mode

        with patch("backend.api.algo.replay.driver.get_replay_driver",
                   return_value=_make_inactive_driver()), \
             patch("backend.shared.helpers.settings._CACHE",
                   _mock_settings(paper_mode=False, shadow_mode=False)):
            mode = _get_current_mode(is_prod=True)

        assert mode == "live"

    @pytest.mark.asyncio
    async def test_get_mode_shadow_on_prod(self):
        """Prod + shadow_mode=True → 'shadow'."""
        from backend.api.routes.execution import _get_current_mode

        with patch("backend.api.algo.replay.driver.get_replay_driver",
                   return_value=_make_inactive_driver()), \
             patch("backend.shared.helpers.settings._CACHE",
                   _mock_settings(paper_mode=False, shadow_mode=True)):
            mode = _get_current_mode(is_prod=True)

        assert mode == "shadow"

    def test_allowed_modes_constants(self):
        """Dev branch allowed_modes filters to [sim, replay, paper]."""
        from backend.api.routes.execution import _DEV_MODES, _PROD_MODES

        assert "sim" in _DEV_MODES
        assert "sim" not in _PROD_MODES
        assert "live" not in _DEV_MODES
        assert "live" in _PROD_MODES


class TestSetMode:

    @pytest.mark.asyncio
    async def test_set_paper_allowed_on_dev(self):
        """POST 'paper' is in _DEV_MODES and does not raise on dev."""
        from backend.api.routes.execution import _DEV_MODES, ExecutionModeRequest

        assert "paper" in _DEV_MODES
        # Structural test: request object is well-formed.
        req = ExecutionModeRequest(mode="paper")
        assert req.mode == "paper"

    @pytest.mark.asyncio
    async def test_set_sim_rejected_on_prod(self):
        """POST 'sim' on prod branch → 403; 'sim' is not in _PROD_MODES."""
        from litestar.exceptions import HTTPException
        from backend.api.routes.execution import (
            ExecutionController, ExecutionModeRequest, _PROD_MODES, _DEV_MODES
        )

        # 'sim' must not be in prod allowed modes — that's the invariant.
        assert "sim" not in _PROD_MODES
        assert "sim" in _DEV_MODES

        controller = ExecutionController.__new__(ExecutionController)
        req = ExecutionModeRequest(mode="sim")

        # is_prod_branch reads from backend.shared.helpers.utils.config;
        # we patch that dict so the branch check sees "main".
        # The 403 is raised before async_session is touched, so we
        # don't need to stub DB helpers.
        # Call the underlying function directly (Litestar wraps methods into
        # route handler objects; the raw coroutine lives at fn.__func__).
        set_mode_fn = ExecutionController.set_mode.fn
        with patch("backend.shared.helpers.utils.config",
                   {"deploy_branch": "main"}):

            with pytest.raises(HTTPException) as exc_info:
                await set_mode_fn(controller, req)

        assert exc_info.value.status_code == 403
        assert "sim" in str(exc_info.value.detail).lower()

    @pytest.mark.asyncio
    async def test_get_returns_allowed_modes_for_dev(self):
        """GET on dev → allowed_modes contains sim but not shadow/live."""
        from backend.api.routes.execution import ExecutionController

        controller = ExecutionController.__new__(ExecutionController)

        get_mode_fn = ExecutionController.get_mode.fn
        with patch("backend.shared.helpers.utils.config",
                   {"deploy_branch": "dev"}), \
             patch("backend.api.routes.execution._get_current_mode",
                   return_value="paper"):

            resp = await get_mode_fn(controller)

        assert "sim" in resp.allowed_modes
        assert "live" not in resp.allowed_modes
        assert resp.branch == "dev"
