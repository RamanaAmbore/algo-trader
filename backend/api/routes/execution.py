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

# Navbar dropdown lists ONLY the persistent master-toggle modes
# (LIVE / PAPER / SHADOW). SIM and REPLAY are transient workspaces —
# they live on /admin/execution as tabs and surface as banners under
# the navbar when active. Mixing transient and persistent modes in
# one dropdown was the root cause of the "what does REPLAY mean?"
# confusion. Industry convention (IB TWS, ThinkOrSwim, QuantConnect,
# NinjaTrader) keeps the mode picker for persistent state and routes
# sim/backtest to dedicated workspaces.
#
# `_get_current_mode` below still returns 'sim'/'replay' when those
# drivers are active so the chip auto-flips to a read-only indicator
# during a run — operators see what's running without it cluttering
# the picker.
# Every mode the operator can navigate to from the navbar dropdown.
# Settings-toggle modes (paper/live/shadow) commit the corresponding
# setting via setExecutionMode; driver-active modes (sim/replay)
# don't toggle a setting — clicking them navigates to
# /admin/execution?mode=<slug> where the operator starts the driver.
# Dev branch forces paper for any settings-toggle pick, so LIVE and
# SHADOW are excluded from the dev list (clicking them would be a
# silent no-op).
_DEV_MODES  = ["idle", "paper", "sim", "replay"]
_PROD_MODES = ["paper", "live", "shadow", "sim", "replay"]


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

    # Check sim driver. Available on both branches now that sim is a
    # navbar mode option on prod too.
    try:
        from backend.api.algo.sim.driver import get_driver
        if get_driver().active:
            return "sim"
    except Exception:
        pass

    if not is_prod:
        # Dev: IDLE when the engine kill-switch is off (default state on
        # boot); PAPER once the operator picks PAPER from the navbar.
        # Sim/replay drivers were already short-circuited above.
        if not get_bool("execution.dev_active", False):
            return "idle"
        return "paper"

    if get_bool("execution.shadow_mode", False):
        return "shadow"
    if get_bool("execution.paper_trading_mode", False):
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
        from backend.shared.helpers.settings import get_bool, reload_cache
        from sqlalchemy import select
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

        # Dev-only target — turn the engine OFF. KiteTicker (if running)
        # stops; background tasks short-circuit; no broker calls until
        # operator picks another mode. Prod can't reach this branch
        # (target validated against _PROD_MODES which excludes 'idle').
        if target == "idle":
            updates["execution.dev_active"] = "false"
            _stop_drivers()
            _stop_ticker_if_running()

        elif target == "paper":
            updates["execution.paper_trading_mode"] = "true"
            updates["execution.shadow_mode"]        = "false"
            # On dev, picking paper also flips the engine ON.
            if not is_prod:
                updates["execution.dev_active"] = "true"
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
            # return. Stop the opposite driver if it is running.
            # On dev, also flip the engine ON so SimDriver writes flow
            # through the same code paths the live engine uses.
            if not is_prod:
                updates["execution.dev_active"] = "true"
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
            # Await the reload synchronously — invalidate_cache schedules
            # a background task, but _get_current_mode below reads the
            # cache and would race against the reload.
            await reload_cache()

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


def _stop_ticker_if_running() -> None:
    """Best-effort KiteTicker shutdown — used by the navbar IDLE
    pick on dev. Wraps the per-instance teardown so a callsite that
    doesn't care about the underlying socket state can request "stop
    if up" without exception bookkeeping."""
    try:
        from backend.shared.helpers.kite_ticker import get_ticker
        ticker = get_ticker()
        if getattr(ticker, "_started", False):
            ticker.stop()
            logger.info("[execution] KiteTicker stopped (idle pick)")
    except Exception as e:
        logger.warning(f"[execution] could not stop ticker: {e}")
