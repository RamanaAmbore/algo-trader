"""Held automated orders: list them and release one by one."""
from litestar import Controller, get, post
from litestar.exceptions import HTTPException

from backend.api.auth_guard import admin_guard, auth_or_demo_guard


class HeldOrdersController(Controller):
    path = "/api/orders/held"

    @get("/", guards=[auth_or_demo_guard])
    async def list_held(self) -> dict:
        from sqlalchemy import select
        from backend.api.database import async_session
        from backend.api.models import AlgoOrder
        from sqlalchemy import or_
        async with async_session() as s:
            rows = (await s.execute(
                select(AlgoOrder).where(or_(
                    AlgoOrder.status == "HELD",
                    AlgoOrder.hold_json.isnot(None)))
                .order_by(AlgoOrder.id.desc()).limit(200))).scalars().all()
            return {"held": [
                {"id": r.id, "account": r.account, "symbol": r.symbol,
                 "exchange": r.exchange, "side": r.transaction_type,
                 "qty": int(r.quantity), "product": r.product,
                 "hold": r.hold_json or ""}
                for r in rows
            ]}

    @post("/{order_id:int}/release", guards=[admin_guard])
    async def release(self, order_id: int) -> dict:
        from backend.api.algo.order_release import release_held_order, release_template_exit
        from backend.api.algo.order_hold import parse_hold_record
        from backend.api.database import async_session
        from backend.api.models import AlgoOrder
        from sqlalchemy import select
        async with async_session() as s:
            row = (await s.execute(select(AlgoOrder).where(AlgoOrder.id == order_id))).scalar_one_or_none()
        rec = parse_hold_record(row.hold_json) if row else None
        if rec and rec.get("category") == "template_exit":
            result = await release_template_exit(order_id, actor="operator")
        else:
            result = await release_held_order(order_id, actor="operator")
        if not result["ok"]:
            raise HTTPException(status_code=409, detail=result["reason"])
        return result
