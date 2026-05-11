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


class WatchlistQuote(msgspec.Struct):
    """Live market quote for a single watchlist item."""
    item_id: int
    tradingsymbol: str       # the symbol the user stored (e.g. "GOLD")
    quote_symbol: str        # the symbol we resolved + fetched (e.g. "GOLDM25APRFUT")
    exchange: str
    ltp: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None   # prior-day close — used for day change
    change: float = 0.0              # ltp - close (signed ₹)
    change_pct: float = 0.0          # (ltp - close) / close × 100
    volume: int = 0
    stale: bool = False              # true when broker returned no quote


class WatchlistQuotes(msgspec.Struct):
    watchlist_id: int
    refreshed_at: str
    items: list[WatchlistQuote]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


# In-process quote cache. Keyed by watchlist_id. The 5s TTL matches the
# paper engine tick + the simulator default rate, so we share one batch
# across multiple polls from the same UI session.
_QUOTE_CACHE: dict[int, tuple[float, WatchlistQuotes]] = {}
_QUOTE_TTL_SECONDS = 5.0


def _resolve_mcx_commodity(commodity_name: str) -> Optional[str]:
    """Map a bare MCX commodity name (e.g. 'GOLD') to the nearest-month
    future tradingsymbol. Looks up the instrument cache. Returns None
    when no MCX FUT is found for that commodity."""
    try:
        from backend.api.routes.instruments import _CACHE  # type: ignore
        items = (_CACHE.get("items") or []) if isinstance(_CACHE, dict) else []
    except Exception:
        items = []
    if not items:
        return None
    candidates = []
    target_u = commodity_name.upper()
    for inst in items:
        if (
            getattr(inst, "e", "") == "MCX"
            and getattr(inst, "t", "") == "FUT"
            and (getattr(inst, "u", "") or "").upper() == target_u
            and getattr(inst, "x", None)
        ):
            candidates.append(inst)
    if not candidates:
        return None
    # Earliest expiry first — that's the near-month future.
    candidates.sort(key=lambda i: i.x or "")
    return candidates[0].s


def _build_quote_key(item: WatchlistItem) -> tuple[str, str]:
    """Returns (broker_key, quote_symbol). MCX bare commodity names get
    resolved to the near-month future; everything else passes through."""
    sym, exch = item.tradingsymbol, item.exchange
    # Heuristic: MCX commodity names are short + uppercase + no digits.
    # Real futures look like GOLDM25APRFUT / CRUDEOILM25MAY etc.
    if exch == "MCX" and sym.isalpha() and len(sym) <= 12:
        resolved = _resolve_mcx_commodity(sym)
        if resolved:
            return f"MCX:{resolved}", resolved
    return f"{exch}:{sym}", sym


async def _fetch_quotes(items: list[WatchlistItem]) -> list[WatchlistQuote]:
    """One batched broker.quote() call for every distinct key. asyncio
    runs the sync broker call in a thread so the event loop isn't
    blocked on the network round-trip."""
    import asyncio
    from backend.shared.brokers.registry import get_price_broker

    key_map: dict[int, tuple[str, str]] = {}
    for it in items:
        broker_key, quote_sym = _build_quote_key(it)
        key_map[it.id] = (broker_key, quote_sym)

    distinct_keys = sorted({k[0] for k in key_map.values() if k[0]})
    quote_data: dict = {}
    if distinct_keys:
        try:
            broker = get_price_broker()
            quote_data = await asyncio.to_thread(broker.quote, distinct_keys) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Watchlist quote fetch failed: {exc}")
            quote_data = {}

    out: list[WatchlistQuote] = []
    for it in items:
        broker_key, quote_sym = key_map[it.id]
        q = quote_data.get(broker_key) or {}
        ltp    = float(q.get("last_price") or 0.0)
        ohlc   = q.get("ohlc") or {}
        close  = float(ohlc.get("close") or 0.0) or None
        depth  = q.get("depth") or {}
        buys   = depth.get("buy") or []
        sells  = depth.get("sell") or []
        bid    = float(buys[0]["price"])  if buys  and (buys[0].get("price") or 0)  else None
        ask    = float(sells[0]["price"]) if sells and (sells[0].get("price") or 0) else None
        change = (ltp - close) if (close and ltp) else 0.0
        chg_pct = (change / close * 100.0) if close else 0.0
        out.append(WatchlistQuote(
            item_id=it.id,
            tradingsymbol=it.tradingsymbol,
            quote_symbol=quote_sym,
            exchange=it.exchange,
            ltp=ltp,
            bid=bid, ask=ask,
            open=(float(ohlc.get("open"))  if ohlc.get("open")  else None),
            high=(float(ohlc.get("high"))  if ohlc.get("high")  else None),
            low =(float(ohlc.get("low"))   if ohlc.get("low")   else None),
            close=close,
            change=change, change_pct=chg_pct,
            volume=int(q.get("volume") or 0),
            stale=(not q),
        ))
    return out

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

    @get("/{wl_id:int}/quotes")
    async def quotes(self, wl_id: int, request: Request) -> WatchlistQuotes:
        """Batched live quotes for every item in the watchlist. 5-second
        in-process cache so multiple concurrent polls share one Kite
        round-trip. The cache is keyed on watchlist_id only — adds /
        removes will be reflected on the next refresh (max 5s lag)."""
        import time
        from datetime import datetime, timezone
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
            items_row = await session.execute(
                select(WatchlistItem).where(WatchlistItem.watchlist_id == wl_id)
                .order_by(WatchlistItem.sort_order, WatchlistItem.id)
            )
            items = list(items_row.scalars().all())

        # Cache check — same watchlist hit within TTL returns cached.
        now = time.monotonic()
        cached = _QUOTE_CACHE.get(wl_id)
        if cached and (now - cached[0]) < _QUOTE_TTL_SECONDS:
            # Defensive: if the item set changed (operator added/removed
            # between polls), recompute the keys but reuse the broker
            # data we already have.
            if {q.item_id for q in cached[1].items} == {it.id for it in items}:
                return cached[1]

        quotes = await _fetch_quotes(items)
        out = WatchlistQuotes(
            watchlist_id=wl_id,
            refreshed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            items=quotes,
        )
        _QUOTE_CACHE[wl_id] = (now, out)
        return out

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
