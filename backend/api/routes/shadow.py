"""
Shadow mode control plane — `/api/shadow/*`.

Shadow mode sits between Paper and Live on the confidence ladder:
  Paper → Shadow → Live

Orders are validated via Kite's basket_margin and the exact broker payload
is logged, but no order is actually placed. Prod only.

Endpoints
  GET  /api/shadow/status    — shadow engine status + recent count
  GET  /api/shadow/orders    — recent mode='shadow' algo orders
  POST /api/shadow/promote   — set paper_trading_mode=false, shadow_mode=false
  POST /api/shadow/clear     — wipe shadow rows from algo_orders
"""

from __future__ import annotations

import msgspec
from litestar import Controller, get, post
from litestar.exceptions import HTTPException
from sqlalchemy import delete as sql_delete, desc, select

from backend.api.auth_guard import admin_guard
from backend.api.database import async_session
from backend.api.models import AlgoOrder, Setting
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


class ShadowController(Controller):
    path = "/api/shadow"
    guards = [admin_guard]

    @get("/status")
    async def status(self) -> dict:
        from backend.shared.helpers.utils import config, is_prod_branch
        from backend.shared.helpers.settings import get_bool

        branch = config.get("deploy_branch", "dev") or "dev"
        is_prod = is_prod_branch()
        shadow_on = get_bool("execution.shadow_mode", False)

        # Count recent shadow orders
        async with async_session() as s:
            count_result = (await s.execute(
                select(AlgoOrder)
                .where(AlgoOrder.mode == "shadow")
            )).scalars().all()

        return {
            "enabled": is_prod,
            "shadow_active": shadow_on and is_prod,
            "branch": branch,
            "order_count": len(count_result),
        }

    @get("/orders")
    async def orders(self, limit: int = 50) -> list[dict]:
        limit = min(max(1, limit), 200)
        async with async_session() as s:
            rows = (await s.execute(
                select(AlgoOrder)
                .where(AlgoOrder.mode == "shadow")
                .order_by(desc(AlgoOrder.created_at))
                .limit(limit)
            )).scalars().all()
        return [
            {
                "id": r.id,
                "account": r.account,
                "symbol": r.symbol,
                "exchange": r.exchange,
                "side": r.transaction_type,
                "quantity": r.quantity,
                "initial_price": r.initial_price,
                "status": r.status,
                "mode": r.mode,
                "detail": r.detail,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]

    @post("/promote")
    async def promote(self) -> dict:
        """Promote from shadow to live: set paper_trading_mode=false, shadow_mode=false."""
        from backend.api.models import Setting
        from backend.shared.helpers.settings import invalidate_cache

        keys_to_set = {
            "execution.shadow_mode":        "false",
            "execution.paper_trading_mode": "false",
        }

        promoted = []
        async with async_session() as s:
            for key, val in keys_to_set.items():
                row = (await s.execute(
                    select(Setting).where(Setting.key == key)
                )).scalar_one_or_none()
                if row:
                    row.value = val
                    promoted.append(f"{key} → {val}")
            await s.commit()

        invalidate_cache()
        logger.warning(f"[SHADOW] Promoted to live: {promoted}")
        return {"promoted": promoted}

    @post("/clear")
    async def clear(self) -> dict:
        async with async_session() as s:
            r1 = await s.execute(
                sql_delete(AlgoOrder).where(AlgoOrder.mode == "shadow")
            )
            await s.commit()
        return {"deleted_orders": r1.rowcount}
