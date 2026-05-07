"""
Execution-mode endpoint — `/api/admin/execution/*`.

Single source of truth for the mode picker: what mode is the system in right
now, and which modes are available on the current branch.

Endpoints
  GET  /api/admin/execution/mode  — current mode + allowed_modes
  POST /api/admin/execution/mode  — switch mode (updates settings flags)

Admin-guarded. On dev branches only sim/replay/paper are valid targets;
on prod replay/paper/shadow/live are valid.
"""

from __future__ import annotations

import msgspec
from litestar import Controller, get, post
from litestar.exceptions import HTTPException

from backend.api.auth_guard import admin_guard
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

_DEV_MODES  = ["sim", "replay", "paper"]
_PROD_MODES = ["replay", "paper", "shadow", "live"]


class ExecutionModeResponse(msgspec.Struct):
    mode: str                 # current effective mode
    branch: str               # raw deploy_branch value
    allowed_modes: list[str]  # branch-filtered list


class ExecutionModeRequest(msgspec.Struct):
    mode: str


def _get_current_mode(is_prod: bool) -> str:
    """Compute the effective execution mode from live settings."""
    from backend.shared.helpers.settings import get_bool

    # Check replay driver.
    try:
        from backend.api.algo.replay.driver import get_replay_driver
        if get_replay_driver().active:
            return "replay"
    except Exception:
        pass

    # Check sim driver (dev only).
    if not is_prod:
        try:
            from backend.api.algo.sim.driver import get_driver
            if get_driver().active:
                return "sim"
        except Exception:
            pass

    if not is_prod:
        # Dev always forces paper regardless of the master toggle.
        return "paper"

    if get_bool("execution.shadow_mode", False):
        return "shadow"
    if get_bool("execution.paper_trading_mode", True):
        return "paper"
    return "live"


class ExecutionController(Controller):
    path = "/api/admin/execution"
    guards = [admin_guard]

    @get("/mode")
    async def get_mode(self) -> ExecutionModeResponse:
        from backend.shared.helpers.utils import config, is_prod_branch

        branch  = config.get("deploy_branch", "dev") or "dev"
        is_prod = is_prod_branch()
        mode    = _get_current_mode(is_prod)
        allowed = _PROD_MODES if is_prod else _DEV_MODES

        return ExecutionModeResponse(
            mode=mode,
            branch=branch,
            allowed_modes=allowed,
        )

    @post("/mode")
    async def set_mode(self, data: ExecutionModeRequest) -> ExecutionModeResponse:
        """
        Switch execution mode.

        paper  → paper_trading_mode=True, shadow_mode=False
        shadow → paper_trading_mode=False, shadow_mode=True   (prod only)
        live   → paper_trading_mode=False, shadow_mode=False  (prod only)
        sim    → start sim driver if not running              (dev only)
        replay → start replay driver if not running           (both)

        For sim/replay the caller is expected to configure the driver
        separately via /api/simulator/start or /api/replay/start. The
        POST here only validates the mode is reachable and returns the
        resulting state.
        """
        from backend.shared.helpers.utils import config, is_prod_branch
        from backend.shared.helpers.settings import get_bool, invalidate_cache
        from sqlalchemy import select, update as sql_update
        from backend.api.database import async_session
        from backend.api.models import Setting

        branch  = config.get("deploy_branch", "dev") or "dev"
        is_prod = is_prod_branch()
        allowed = _PROD_MODES if is_prod else _DEV_MODES
        target  = (data.mode or "").lower().strip()

        if target not in allowed:
            raise HTTPException(
                status_code=403,
                detail=(f"Mode '{target}' is not available on the "
                        f"{'prod' if is_prod else 'dev'} branch. "
                        f"Allowed: {allowed}"),
            )

        # Apply flags based on target mode.
        updates: dict[str, str] = {}

        if target == "paper":
            updates["execution.paper_trading_mode"] = "true"
            updates["execution.shadow_mode"]        = "false"
            # Stop any running sim/replay.
            _stop_drivers()

        elif target == "shadow":
            updates["execution.paper_trading_mode"] = "false"
            updates["execution.shadow_mode"]        = "true"
            _stop_drivers()

        elif target == "live":
            updates["execution.paper_trading_mode"] = "false"
            updates["execution.shadow_mode"]        = "false"
            _stop_drivers()

        elif target in ("sim", "replay"):
            # The driver is started separately; we just validate and
            # return.  Stop the opposite driver if it is running.
            _stop_drivers(exclude=target)

        if updates:
            # Upsert — these rows are intentionally not in SEEDS (the seeder
            # auto-prunes them and the navbar combobox is the only writer),
            # so a plain UPDATE would no-op silently when the row is absent.
            async with async_session() as s:
                for key, val in updates.items():
                    existing = await s.execute(
                        select(Setting).where(Setting.key == key)
                    )
                    row = existing.scalar_one_or_none()
                    if row is not None:
                        row.value = val
                    else:
                        s.add(Setting(
                            category="execution",
                            key=key,
                            value_type="bool",
                            value=val,
                            default_value=val,
                            description="Set by /api/admin/execution/mode (navbar mode chip).",
                        ))
                await s.commit()
            invalidate_cache()

        mode = _get_current_mode(is_prod)
        logger.info(f"[execution] mode switched to '{target}' (effective: '{mode}') "
                    f"on branch '{branch}'")

        return ExecutionModeResponse(
            mode=mode,
            branch=branch,
            allowed_modes=allowed,
        )


def _stop_drivers(exclude: str | None = None) -> None:
    """Best-effort stop for sim and replay drivers."""
    if exclude != "sim":
        try:
            from backend.api.algo.sim.driver import get_driver
            drv = get_driver()
            if drv.active:
                drv.stop()
        except Exception:
            pass

    if exclude != "replay":
        try:
            from backend.api.algo.replay.driver import get_replay_driver
            drv = get_replay_driver()
            if drv.active:
                drv.stop()
        except Exception:
            pass
