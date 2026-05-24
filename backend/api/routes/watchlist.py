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

from backend.api.auth_guard import jwt_guard, auth_or_demo_guard
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


class MoverRow(msgspec.Struct):
    tradingsymbol: str
    exchange: str
    last_price: float
    previous_close: float
    change_pct: float
    peak_pct: float
    sticky: bool   # True iff abs(change_pct) currently < threshold but was >= threshold earlier today


class MoversResponse(msgspec.Struct):
    movers: list[MoverRow]
    threshold_pct: float
    session_date: str   # ISO date "2026-05-13" — when the sticky set last reset


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


# In-process quote cache. Keyed by watchlist_id. The 5s TTL matches the
# paper engine tick + the simulator default rate, so we share one batch
# across multiple polls from the same UI session.
_QUOTE_CACHE: dict[int, tuple[float, WatchlistQuotes]] = {}
_QUOTE_TTL_SECONDS = 5.0


MOVER_THRESHOLD_PCT: float = 1.5
# How many top movers to surface even when nothing crossed the
# threshold — keeps the section populated on calm days. Anything that
# DID cross the threshold (and stuck for the session) is added on top
# of this count.
MOVER_TOP_N: int = 6

# ---------------------------------------------------------------------------
# Session-sticky movers state
# ---------------------------------------------------------------------------

_session_movers: dict[str, dict] = {}
_session_date: Optional[str] = None  # ISO date string — rolls over at IST midnight


async def _resolve_mcx_commodity(commodity_name: str) -> Optional[str]:
    """Map a bare MCX commodity name (e.g. 'GOLD') to the nearest-month
    future tradingsymbol. Reads the shared instruments cache (24h TTL,
    warmed at startup + 08:00 IST). Returns None when no MCX FUT is
    found for that commodity (cache cold or broker fetch failed)."""
    from backend.api.cache import get_or_fetch
    from backend.api.routes.instruments import _fetch_instruments, _TTL_SECONDS
    try:
        resp = await get_or_fetch("instruments", _fetch_instruments,
                                   ttl_seconds=_TTL_SECONDS)
        items = resp.items if resp else []
    except Exception:
        items = []
    if not items:
        return None
    target_u = commodity_name.upper()
    candidates = [
        inst for inst in items
        if (inst.e == "MCX"
            and inst.t == "FUT"
            and (inst.u or "").upper() == target_u
            and inst.x)
    ]
    if not candidates:
        return None
    # Earliest expiry first — that's the near-month future.
    candidates.sort(key=lambda i: i.x or "")
    return candidates[0].s


async def _resolve_cds_currency(currency_name: str) -> Optional[str]:
    """Map a bare CDS currency pair name (e.g. 'USDINR') to the nearest-month
    future tradingsymbol. Mirrors _resolve_mcx_commodity for the CDS exchange.
    Returns None when no CDS FUT is found (cache cold or broker fetch failed)."""
    from backend.api.cache import get_or_fetch
    from backend.api.routes.instruments import _fetch_instruments, _TTL_SECONDS
    try:
        resp = await get_or_fetch("instruments", _fetch_instruments,
                                   ttl_seconds=_TTL_SECONDS)
        items = resp.items if resp else []
    except Exception:
        items = []
    if not items:
        return None
    target_u = currency_name.upper()
    candidates = [
        inst for inst in items
        if (inst.e == "CDS"
            and inst.t == "FUT"
            and (inst.u or "").upper() == target_u
            and inst.x)
    ]
    if not candidates:
        return None
    # Earliest expiry first — that's the near-month future.
    candidates.sort(key=lambda i: i.x or "")
    return candidates[0].s


async def _build_quote_key(item: WatchlistItem) -> tuple[str, str]:
    """Returns (broker_key, quote_symbol). MCX bare commodity names get
    resolved to the near-month future; everything else passes through."""
    sym, exch = item.tradingsymbol, item.exchange
    # Heuristic: MCX commodity names are short + uppercase + no digits.
    # Real futures look like GOLDM25APRFUT / CRUDEOILM25MAY etc.
    if exch == "MCX" and sym.isalpha() and len(sym) <= 12:
        resolved = await _resolve_mcx_commodity(sym)
        if resolved:
            return f"MCX:{resolved}", resolved
    # CDS currency pair names are short + uppercase + no digits.
    # Real futures look like USDINR25MAYFUT.
    if exch == "CDS" and sym.isalpha() and len(sym) <= 12:
        resolved = await _resolve_cds_currency(sym)
        if resolved:
            return f"CDS:{resolved}", resolved
    return f"{exch}:{sym}", sym


async def _fetch_quotes(items: list[WatchlistItem]) -> list[WatchlistQuote]:
    """One batched broker.quote() call for every distinct key. asyncio
    runs the sync broker call in a thread so the event loop isn't
    blocked on the network round-trip."""
    import asyncio
    from backend.shared.brokers.registry import get_price_broker

    key_map: dict[int, tuple[str, str]] = {}
    for it in items:
        broker_key, quote_sym = await _build_quote_key(it)
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
    lists on first access — no migration sweep needed.

    For existing users who already have Markets, top up any symbols
    from the seed list that aren't already present (additive only —
    never re-adds a symbol the user explicitly removed, since this
    only fires on the full original seed-list count match)."""
    # One-off rename: an early seed shipped "NIFTY SMALLCAP 100" but
    # Kite's quote key is the abbreviated "NIFTY SMLCAP 100". Migrate
    # any rows that still carry the wrong name. Idempotent.
    from sqlalchemy import update
    await session.execute(
        update(WatchlistItem)
        .where(
            WatchlistItem.tradingsymbol == "NIFTY SMALLCAP 100",
            WatchlistItem.exchange == "NSE",
        )
        .values(tradingsymbol="NIFTY SMLCAP 100")
    )
    await session.commit()

    row = await session.execute(
        select(Watchlist.id, Watchlist.name).where(Watchlist.user_id == user_id)
    )
    existing = list(row.all())
    if existing:
        # Top-up logic for the Markets list. Only add symbols that
        # aren't there yet, never remove. Skip entirely if the user
        # has clearly customised the list (item count below the
        # original seed size, suggesting deliberate removals).
        markets = next((rid for (rid, name) in existing if name == "Markets"), None)
        if markets:
            seed_pairs = {(r["tradingsymbol"], r["exchange"])
                          for r in markets_default_rows()}
            cur_row = await session.execute(
                select(WatchlistItem.tradingsymbol, WatchlistItem.exchange)
                .where(WatchlistItem.watchlist_id == markets)
            )
            # Cast each Row to a plain tuple so the set difference
            # against seed_pairs (also tuples) works correctly.
            cur_pairs = {(r[0], r[1]) for r in cur_row.all()}
            missing = seed_pairs - cur_pairs
            # Only top up if the existing list has at least as many of
            # the OTHER seed entries as expected (i.e. user hasn't been
            # removing items). The original seed had ≥10 entries.
            if missing and len(cur_pairs & seed_pairs) >= len(seed_pairs) - len(missing):
                now = datetime.now(timezone.utc)
                from sqlalchemy import func
                max_sort_r = await session.execute(
                    select(func.coalesce(func.max(WatchlistItem.sort_order), -1))
                    .where(WatchlistItem.watchlist_id == markets)
                )
                next_sort = int(max_sort_r.scalar() or -1) + 1
                for sym, exch in sorted(missing):
                    session.add(WatchlistItem(
                        watchlist_id=markets, tradingsymbol=sym,
                        exchange=exch, sort_order=next_sort, added_at=now,
                    ))
                    next_sort += 1
                await session.commit()
                logger.info(
                    f"Watchlist: topped up Markets for user_id={user_id} "
                    f"with {sorted(missing)}"
                )
        return
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
    # Controller-level default — every route needs an authenticated
    # user EXCEPT the global session-sticky movers feed (overridden
    # per-route to auth_or_demo_guard so prod demo visitors can see
    # it from Market Pulse).
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

    # ── Movers ────────────────────────────────────────────────────────

    @get("/movers", guards=[auth_or_demo_guard])
    async def get_movers(self) -> MoversResponse:
        """Session-sticky movers: underlyings that have moved ≥5% intraday.
        Once a name crosses the threshold it stays in the result for the rest
        of the IST calendar day. One cached broker.quote() batch per 30 s."""
        import asyncio
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo
        from backend.api.cache import get_or_fetch
        from backend.api.routes.instruments import _fetch_instruments, _TTL_SECONDS as _INST_TTL
        from backend.api.algo.derivatives import underlying_ltp_key, is_mcx_underlying
        from backend.shared.brokers.registry import get_price_broker

        global _session_movers, _session_date

        # Session rollover at IST midnight.
        ist_today = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
        if _session_date != ist_today:
            _session_movers = {}
            _session_date = ist_today

        # Build universe: underlyings that have at least one CE/PE row.
        try:
            resp = await get_or_fetch("instruments", _fetch_instruments,
                                      ttl_seconds=_INST_TTL)
            all_items = resp.items if resp else []
        except Exception:
            all_items = []

        # Collect unique underlyings with options chains.
        underlyings_with_opts: set[str] = set()
        for inst in all_items:
            if inst.t in ("CE", "PE") and inst.u:
                underlyings_with_opts.add(inst.u.upper())

        if not underlyings_with_opts:
            return MoversResponse(
                movers=[], threshold_pct=MOVER_THRESHOLD_PCT, session_date=ist_today,
            )

        # Build Kite quote keys. MCX commodities have no NSE spot — skip them
        # (their "underlying" is the futures contract itself; no option chain
        # in the traditional sense for volatility movers).
        key_to_underlying: dict[str, str] = {}
        for name in underlyings_with_opts:
            if is_mcx_underlying(name):
                continue
            key = underlying_ltp_key(name)
            key_to_underlying[key] = name

        # Cached 30 s quote batch for the movers universe.
        _MOVERS_QUOTE_TTL = 30

        async def _fetch_movers_quotes() -> dict:
            keys = list(key_to_underlying.keys())
            if not keys:
                return {}
            try:
                broker = get_price_broker()
                return await asyncio.to_thread(broker.quote, keys) or {}
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Movers quote fetch failed: {exc}")
                return {}

        from backend.api.cache import get_or_fetch as _gof
        from backend.shared.helpers.utils import get_nearest_time
        cache_key = f"movers_quotes_{get_nearest_time(_MOVERS_QUOTE_TTL)}"
        quote_data: dict = await _gof(cache_key, _fetch_movers_quotes,
                                      ttl_seconds=_MOVERS_QUOTE_TTL)

        # Compute change_pct for every underlying. Update session-sticky
        # state for anything that crossed the threshold. Always collect
        # a separate "live snapshot" so the section is populated even on
        # calm days when nothing crossed the threshold.
        live_snapshot: dict[str, dict] = {}
        for kite_key, underlying in key_to_underlying.items():
            q = quote_data.get(kite_key) or {}
            ltp = float(q.get("last_price") or 0.0)
            ohlc = q.get("ohlc") or {}
            prev_close = float(ohlc.get("close") or 0.0)
            if ltp == 0.0 or prev_close == 0.0:
                # Update last_price/last_pct if already sticky.
                if underlying in _session_movers:
                    if ltp:
                        _session_movers[underlying]["last_price"] = ltp
                continue
            change_pct = (ltp - prev_close) / prev_close * 100.0
            live_snapshot[underlying] = {
                "peak_pct": change_pct,
                "last_pct": change_pct,
                "last_price": ltp,
                "previous_close": prev_close,
                "exchange": "NSE",
            }

            if underlying in _session_movers:
                entry = _session_movers[underlying]
                entry["last_pct"] = change_pct
                entry["last_price"] = ltp
                entry["previous_close"] = prev_close
                if abs(change_pct) > abs(entry["peak_pct"]):
                    entry["peak_pct"] = change_pct
            elif abs(change_pct) >= MOVER_THRESHOLD_PCT:
                _session_movers[underlying] = {
                    "first_seen_at": datetime.now(timezone.utc).isoformat(),
                    "peak_pct": change_pct,
                    "last_pct": change_pct,
                    "last_price": ltp,
                    "previous_close": prev_close,
                    "exchange": "NSE",  # all non-MCX underlyings resolve via NSE
                }

        # Combine: every session-sticky underlying (crossed the threshold
        # today) + top-N from the live snapshot to keep the section
        # populated on calm days. Sticky entries override snapshot when
        # both have data for the same underlying.
        combined: dict[str, dict] = {}
        # Top-N live first (lower priority).
        snapshot_sorted = sorted(
            live_snapshot.items(),
            key=lambda kv: abs(kv[1]["last_pct"]),
            reverse=True,
        )[:MOVER_TOP_N]
        for u, entry in snapshot_sorted:
            combined[u] = entry
        # Then sticky entries overlay (higher priority).
        for u, entry in _session_movers.items():
            combined[u] = entry

        rows: list[MoverRow] = []
        for underlying, entry in combined.items():
            change_pct = entry["last_pct"]
            rows.append(MoverRow(
                tradingsymbol=underlying,
                exchange=entry["exchange"],
                last_price=entry["last_price"],
                previous_close=entry["previous_close"],
                change_pct=change_pct,
                peak_pct=entry.get("peak_pct", change_pct),
                # `sticky` now means: this underlying DID cross the
                # threshold today and is being held in the session-list
                # even if it has since reverted under the threshold.
                # Top-N live entries that never crossed are NOT sticky.
                sticky=(underlying in _session_movers
                        and abs(change_pct) < MOVER_THRESHOLD_PCT),
            ))

        rows.sort(key=lambda r: abs(r.change_pct), reverse=True)
        return MoversResponse(
            movers=rows,
            threshold_pct=MOVER_THRESHOLD_PCT,
            session_date=ist_today,
        )

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
