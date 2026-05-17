"""
Live mode status — `/api/live/*`.

Dedicated status endpoint for the master paper_trading_mode toggle.

Endpoints
  GET  /api/live/status    — effective execution mode (PAPER / LIVE / DEV)
"""

from __future__ import annotations

import msgspec
from litestar import Controller, get
from litestar.exceptions import HTTPException

from backend.api.auth_guard import admin_guard
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


class LiveStatus(msgspec.Struct):
    branch: str               # current deploy_branch value
    enabled: bool             # True only on the main (prod) branch
    paper_trading_mode: bool  # current value of the master toggle
    effective_mode: str       # "PAPER", "LIVE", or "DEV (paper-forced)"
    shadow_mode: bool         # current value of shadow toggle


class LiveController(Controller):
    path = "/api/live"
    guards = [admin_guard]

    @get("/status")
    async def status(self) -> LiveStatus:
        from backend.shared.helpers.utils import config, is_prod_branch
        from backend.shared.helpers.settings import get_bool

        branch = config.get("deploy_branch", "dev") or "dev"
        is_prod = is_prod_branch()

        paper_trading_mode = get_bool("execution.paper_trading_mode", False)
        shadow_mode        = get_bool("execution.shadow_mode", False)

        if not is_prod:
            effective_mode = "DEV (paper-forced)"
        elif shadow_mode:
            effective_mode = "SHADOW"
        elif paper_trading_mode:
            effective_mode = "PAPER"
        else:
            effective_mode = "LIVE"

        return LiveStatus(
            branch=branch,
            enabled=is_prod,
            paper_trading_mode=paper_trading_mode,
            effective_mode=effective_mode,
            shadow_mode=shadow_mode,
        )
