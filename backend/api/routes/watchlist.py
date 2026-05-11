"""
Watchlist CRUD endpoints.

Per-user named symbol groups for monitoring. Each user gets two
auto-seeded watchlists on first creation:

  - "Default"  — empty; the operator's working list
  - "Markets"  — seeded with major Indian indices + MCX commodities

The /quotes endpoint (live LTP / bid / ask / day-change) lands in W2.
This module is CRUD only.

Auth: every endpoint requires `jwt_guard` — partners can manage their
own watchlists too, since the watchlist is monitoring, not platform
admin. The `user_id` filter is derived from the JWT's `sub` claim, so
one user can never see or mutate another's lists.
"""

from datetime import datetime, timezone
from typing import Optional

import msgspec
from litestar import Controller, Request, delete, get, patch, post
from litestar.exceptions import HTTPException
from sqlalchemy import select

from backend.api.auth_guard import jwt_guard
from backend.api.algo.watchlist_defaults import markets_default_rows
from backend.api.database import async_session
from backend.api.models import User, Watchlist, WatchlistItem
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

# Cap per watchlist — Kite's `quote()` batch tops out around 500 keys
# but the UI gets unreadable past ~50. 100 leaves headroom.
_MAX_ITEMS_PER_LIST = 100


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class WatchlistItemInfo(msgspec.Struct):
    id: int
    watchlist_id: int
    tradingsymbol: str
    exchange: str
    sort_order: int
    added_at: str


class WatchlistInfo(msgspec.Struct):
    id: int
    name: str
    sort_order: int
    is_default: bool
    item_count: int
    created_at: str
    updated_at: str


class WatchlistFull(msgspec.Struct):
    """A single watchlist + every item it contains."""
    id: int
    name: str
    sort_order: int
    is_default: bool
    created_at: str
    updated_at: str
    items: list[WatchlistItemInfo]


class CreateWatchlistRequest(msgspec.Struct):
    name: str


class RenameWatchlistRequest(msgspec.Struct):
    name: Optional[str] = None
    sort_order: Optional[int] = None
    is_default: Optional[bool] = None


class AddItemRequest(msgspec.Struct):
    tradingsymbol: str
    exchange: str
    sort_order: Optional[int] = None


class ReorderItemRequest(msgspec.Struct):
    sort_order: int


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _actor_sub(request) -> str:
    payload = getattr(request.state, "token_payload", {}) or {}
    return payload.get("sub", "")


async def _resolve_user_id(session, username: str) -> int:
    row = await session.execute(select(User.id).where(User.username == username))
    uid = row.scalar_one_or_none()
    if uid is None:
        raise HTTPException(status_code=404, detail="User not found")
    return int(uid)


async def _ensure_default_watchlists(session, user_id: int) -> None:
    """Idempotent: create Default + Markets watchlists for this user if
    they don't already have any. Called lazily by every endpoint so a
    user who pre-dates the watchlist feature still gets their seeded
    lists on first access — no migration sweep needed."""
    row = await session.execute(
        select(Watchlist.id).where(Watchlist.user_id == user_id).limit(1)
    )
    if row.scalar_one_or_none() is not None:
        return  # already seeded
    now = datetime.now(timezone.utc)
    default_list = Watchlist(
        user_id=user_id, name="Default", sort_order=0, is_default=True,
        created_at=now, updated_at=now,
    )
    markets_list = Watchlist(
        user_id=user_id, name="Markets", sort_order=1, is_default=False,
        created_at=now, updated_at=now,
    )
    session.add(default_list)
    session.add(markets_list)
    await session.flush()  # need markets_list.id
    for row in markets_default_rows():
        session.add(WatchlistItem(
            watchlist_id=markets_list.id,
            tradingsymbol=row["tradingsymbol"],
            exchange=row["exchange"],
            sort_order=row["sort_order"],
            added_at=now,
        ))
    await session.commit()
    logger.info(f"Watchlist: seeded Default + Markets for user_id={user_id}")


def _wl_info(wl: Watchlist, item_count: int) -> WatchlistInfo:
    return WatchlistInfo(
        id=wl.id, name=wl.name, sort_order=wl.sort_order,
        is_default=wl.is_default, item_count=item_count,
        created_at=wl.created_at.isoformat() if wl.created_at else "",
        updated_at=wl.updated_at.isoformat() if wl.updated_at else "",
    )


def _item_info(it: WatchlistItem) -> WatchlistItemInfo:
    return WatchlistItemInfo(
        id=it.id, watchlist_id=it.watchlist_id,
        tradingsymbol=it.tradingsymbol, exchange=it.exchange,
        sort_order=it.sort_order,
        added_at=it.added_at.isoformat() if it.added_at else "",
    )


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class WatchlistController(Controller):
    path = "/api/watchlist"
    guards = [jwt_guard]

    @get("/")
    async def list_watchlists(self, request: Request) -> list[WatchlistInfo]:
        """List every watchlist owned by the authenticated user. Auto-
        seeds Default + Markets on first call for any user that doesn't
        have any lists yet."""
        username = _actor_sub(request)
        async with async_session() as session:
            user_id = await _resolve_user_id(session, username)
            await _ensure_default_watchlists(session, user_id)
            result = await session.execute(
                select(Watchlist).where(Watchlist.user_id == user_id)
                .order_by(Watchlist.sort_order, Watchlist.id)
            )
            wls = result.scalars().all()
            # One COUNT(*) per list — cheap at ~5 lists × 100 items.
            from sqlalchemy import func
            counts: dict[int, int] = {}
            for wl in wls:
                r = await session.execute(
                    select(func.count(WatchlistItem.id))
                    .where(WatchlistItem.watchlist_id == wl.id)
                )
                counts[wl.id] = int(r.scalar() or 0)
        return [_wl_info(wl, counts.get(wl.id, 0)) for wl in wls]

    @post("/")
    async def create_watchlist(self, data: CreateWatchlistRequest, request: Request) -> WatchlistInfo:
        name = (data.name or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="Name required")
        if len(name) > 64:
            raise HTTPException(status_code=422, detail="Name too long (max 64)")
        username = _actor_sub(request)
        async with async_session() as session:
            user_id = await _resolve_user_id(session, username)
            await _ensure_default_watchlists(session, user_id)
            # Dedupe on (user_id, name) — defended by the unique index
            # but checking ahead gives a friendlier 409 message.
            existing = await session.execute(
                select(Watchlist.id).where(
                    Watchlist.user_id == user_id, Watchlist.name == name,
                )
            )
            if existing.scalar_one_or_none() is not None:
                raise HTTPException(status_code=409, detail=f"Watchlist '{name}' already exists")
            now = datetime.now(timezone.utc)
            # Place new lists after every existing one.
            from sqlalchemy import func
            max_sort = await session.execute(
                select(func.coalesce(func.max(Watchlist.sort_order), -1))
                .where(Watchlist.user_id == user_id)
            )
            wl = Watchlist(
                user_id=user_id, name=name,
                sort_order=int(max_sort.scalar() or -1) + 1,
                is_default=False, created_at=now, updated_at=now,
            )
            session.add(wl)
            await session.commit()
        return _wl_info(wl, 0)

    @get("/{wl_id:int}")
    async def get_watchlist(self, wl_id: int, request: Request) -> WatchlistFull:
        username = _actor_sub(request)
        async with async_session() as session:
            user_id = await _resolve_user_id(session, username)
            await _ensure_default_watchlists(session, user_id)
            wl_row = await session.execute(
                select(Watchlist).where(
                    Watchlist.id == wl_id, Watchlist.user_id == user_id,
                )
            )
            wl = wl_row.scalar_one_or_none()
            if not wl:
                raise HTTPException(status_code=404, detail="Watchlist not found")
            items_row = await session.execute(
                select(WatchlistItem).where(WatchlistItem.watchlist_id == wl_id)
                .order_by(WatchlistItem.sort_order, WatchlistItem.id)
            )
            items = items_row.scalars().all()
        return WatchlistFull(
            id=wl.id, name=wl.name, sort_order=wl.sort_order,
            is_default=wl.is_default,
            created_at=wl.created_at.isoformat() if wl.created_at else "",
            updated_at=wl.updated_at.isoformat() if wl.updated_at else "",
            items=[_item_info(it) for it in items],
        )

    @patch("/{wl_id:int}")
    async def rename_watchlist(
        self, wl_id: int, data: RenameWatchlistRequest, request: Request,
    ) -> WatchlistInfo:
        username = _actor_sub(request)
        async with async_session() as session:
            user_id = await _resolve_user_id(session, username)
            wl_row = await session.execute(
                select(Watchlist).where(
                    Watchlist.id == wl_id, Watchlist.user_id == user_id,
                )
            )
            wl = wl_row.scalar_one_or_none()
            if not wl:
                raise HTTPException(status_code=404, detail="Watchlist not found")
            if data.name is not None:
                name = (data.name or "").strip()
                if not name:
                    raise HTTPException(status_code=422, detail="Name required")
                if len(name) > 64:
                    raise HTTPException(status_code=422, detail="Name too long")
                wl.name = name
            if data.sort_order is not None:
                wl.sort_order = int(data.sort_order)
            if data.is_default is not None:
                # Only one default at a time per user. When marking this
                # one default, unmark every other.
                if data.is_default:
                    from sqlalchemy import update
                    await session.execute(
                        update(Watchlist)
                        .where(Watchlist.user_id == user_id, Watchlist.id != wl_id)
                        .values(is_default=False)
                    )
                wl.is_default = bool(data.is_default)
            wl.updated_at = datetime.now(timezone.utc)
            await session.commit()
            # Recount items for the return payload.
            from sqlalchemy import func
            r = await session.execute(
                select(func.count(WatchlistItem.id))
                .where(WatchlistItem.watchlist_id == wl_id)
            )
            count = int(r.scalar() or 0)
        return _wl_info(wl, count)

    @delete("/{wl_id:int}", status_code=200)
    async def delete_watchlist(self, wl_id: int, request: Request) -> dict:
        username = _actor_sub(request)
        async with async_session() as session:
            user_id = await _resolve_user_id(session, username)
            wl_row = await session.execute(
                select(Watchlist).where(
                    Watchlist.id == wl_id, Watchlist.user_id == user_id,
                )
            )
            wl = wl_row.scalar_one_or_none()
            if not wl:
                raise HTTPException(status_code=404, detail="Watchlist not found")
            # Cascade delete via FK constraint on watchlist_items.
            await session.delete(wl)
            await session.commit()
        logger.info(f"Watchlist: deleted id={wl_id} by {username!r}")
        return {"detail": f"Watchlist {wl_id} deleted"}

    # ── Items ─────────────────────────────────────────────────────────

    @post("/{wl_id:int}/items", status_code=201)
    async def add_item(
        self, wl_id: int, data: AddItemRequest, request: Request,
    ) -> WatchlistItemInfo:
        tradingsymbol = (data.tradingsymbol or "").strip().upper()
        exchange      = (data.exchange      or "").strip().upper()
        if not tradingsymbol or not exchange:
            raise HTTPException(status_code=422, detail="tradingsymbol + exchange required")
        username = _actor_sub(request)
        async with async_session() as session:
            user_id = await _resolve_user_id(session, username)
            wl_row = await session.execute(
                select(Watchlist).where(
                    Watchlist.id == wl_id, Watchlist.user_id == user_id,
                )
            )
            wl = wl_row.scalar_one_or_none()
            if not wl:
                raise HTTPException(status_code=404, detail="Watchlist not found")
            # Cap + dedupe checks.
            from sqlalchemy import func
            count_r = await session.execute(
                select(func.count(WatchlistItem.id))
                .where(WatchlistItem.watchlist_id == wl_id)
            )
            if int(count_r.scalar() or 0) >= _MAX_ITEMS_PER_LIST:
                raise HTTPException(
                    status_code=409,
                    detail=f"Watchlist cap reached ({_MAX_ITEMS_PER_LIST} items)",
                )
            dup_r = await session.execute(
                select(WatchlistItem.id).where(
                    WatchlistItem.watchlist_id == wl_id,
                    WatchlistItem.tradingsymbol == tradingsymbol,
                    WatchlistItem.exchange      == exchange,
                )
            )
            if dup_r.scalar_one_or_none() is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"{exchange}:{tradingsymbol} already in this watchlist",
                )
            max_sort_r = await session.execute(
                select(func.coalesce(func.max(WatchlistItem.sort_order), -1))
                .where(WatchlistItem.watchlist_id == wl_id)
            )
            sort_val = (data.sort_order if data.sort_order is not None
                        else int(max_sort_r.scalar() or -1) + 1)
            now = datetime.now(timezone.utc)
            it = WatchlistItem(
                watchlist_id=wl_id,
                tradingsymbol=tradingsymbol, exchange=exchange,
                sort_order=sort_val, added_at=now,
            )
            session.add(it)
            wl.updated_at = now
            await session.commit()
        return _item_info(it)

    @patch("/{wl_id:int}/items/{item_id:int}")
    async def reorder_item(
        self, wl_id: int, item_id: int, data: ReorderItemRequest, request: Request,
    ) -> WatchlistItemInfo:
        username = _actor_sub(request)
        async with async_session() as session:
            user_id = await _resolve_user_id(session, username)
            # Confirm the item belongs to a watchlist the user owns.
            row = await session.execute(
                select(WatchlistItem).join(Watchlist).where(
                    WatchlistItem.id == item_id,
                    WatchlistItem.watchlist_id == wl_id,
                    Watchlist.user_id == user_id,
                )
            )
            it = row.scalar_one_or_none()
            if not it:
                raise HTTPException(status_code=404, detail="Item not found")
            it.sort_order = int(data.sort_order)
            await session.commit()
        return _item_info(it)

    @delete("/{wl_id:int}/items/{item_id:int}", status_code=200)
    async def remove_item(
        self, wl_id: int, item_id: int, request: Request,
    ) -> dict:
        username = _actor_sub(request)
        async with async_session() as session:
            user_id = await _resolve_user_id(session, username)
            row = await session.execute(
                select(WatchlistItem).join(Watchlist).where(
                    WatchlistItem.id == item_id,
                    WatchlistItem.watchlist_id == wl_id,
                    Watchlist.user_id == user_id,
                )
            )
            it = row.scalar_one_or_none()
            if not it:
                raise HTTPException(status_code=404, detail="Item not found")
            await session.delete(it)
            await session.commit()
        return {"detail": f"Item {item_id} removed"}


# ---------------------------------------------------------------------------
# Public helper — used by user-creation paths to seed defaults eagerly.
# ---------------------------------------------------------------------------

async def seed_default_watchlists_for_user(user_id: int) -> None:
    """Run the same idempotent seed used by the lazy path. Called from
    auth.register / admin.create_user / scripts/manage.py so new users
    have their lists populated before they ever hit the /watchlist
    endpoint."""
    async with async_session() as session:
        await _ensure_default_watchlists(session, user_id)
