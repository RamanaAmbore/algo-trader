"""Held automated orders: list them and release one by one."""
from litestar import Controller, Request, get, post
from litestar.exceptions import HTTPException

from backend.api.auth_guard import admin_guard, auth_or_demo_guard, is_admin_request
from backend.shared.helpers.utils import mask_account, mask_account_in_text


class HeldOrdersController(Controller):
    path = "/api/orders/held"

    @get("/", guards=[auth_or_demo_guard])
    async def list_held(self, request: Request) -> dict:
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
            # Mask account codes for everyone who is NOT admin/designated —
            # same convention this codebase already applies on /orders,
            # /orders/drafts, /orders/chases/* etc (see orders.py's
            # `do_mask = not is_admin_request(request)` pattern). This route
            # is gated by `auth_or_demo_guard`, which also admits anonymous
            # demo sessions, so without this branch a demo visitor saw raw
            # account codes.
            do_mask = not is_admin_request(request)
            masked_acct = mask_account if do_mask else (lambda a: a)
            masked_text = mask_account_in_text if do_mask else (lambda t: t)
            return {"held": [
                {"id": r.id, "account": masked_acct(r.account), "symbol": r.symbol,
                 "exchange": r.exchange, "side": r.transaction_type,
                 "qty": int(r.quantity), "product": r.product,
                 "hold": masked_text(r.hold_json or "")}
                for r in rows
            ]}

    @post("/{order_id:int}/release", guards=[admin_guard])
    async def release(self, order_id: int) -> dict:
        from backend.api.algo.order_release import get_release_handler
        from backend.api.algo.order_hold import parse_hold_record
        from backend.api.database import async_session
        from backend.api.models import AlgoOrder
        from sqlalchemy import select
        async with async_session() as s:
            row = (await s.execute(select(AlgoOrder).where(AlgoOrder.id == order_id))).scalar_one_or_none()
        rec = parse_hold_record(row.hold_json) if row else None
        _category = rec.get("category") if rec else None
        # Dispatched via the registry in order_release.py — adding a new
        # hold category means registering a handler there, not editing
        # this route. See that module's docstring for the full pattern.
        handler = get_release_handler(_category)
        result = await handler(order_id, actor="operator")
        if not result["ok"]:
            raise HTTPException(status_code=409, detail=result["reason"])
        return result

    @post("/{order_id:int}/cancel", guards=[admin_guard])
    async def cancel(self, order_id: int) -> dict:
        from backend.api.algo.order_release import cancel_held_order
        result = await cancel_held_order(order_id, actor="operator")
        if not result["ok"]:
            raise HTTPException(status_code=409, detail=result["reason"])
        return result
