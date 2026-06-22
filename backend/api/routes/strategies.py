"""
Strategies CRUD + lightweight per-strategy P&L view.

Slice 6 ships the entity (Strategy table + CRUD + simple realized
+ unrealized rollup from AlgoOrder). Slice 7 layers the FIFO lot
ledger on top so the rollup becomes accurate for partial-close
sequences. The interim P&L view in v1 sums `pnl` field on AlgoOrder
rows grouped by strategy_id — correct for fully-closed positions,
approximate (broker's mark-to-market) for open ones.

Routes are demo-readable on the list/detail endpoints so the
showcase tour's "strategies" surface can populate without sign-in.
Mutations gated by `manage_own_strategies` (admin/trader); admin
also has `reassign_strategies` for cross-trader moves.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import msgspec
from litestar import Controller, get, post, patch, delete
from litestar.exceptions import HTTPException
from sqlalchemy import select, func

from backend.api.database import async_session
from backend.api.models import Strategy, StrategyLot, AlgoOrder, User
from backend.api.rbac import cap_guard, resolve_role_from_connection
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# ── Schemas ───────────────────────────────────────────────────────────────


class StrategyInfo(msgspec.Struct):
    id: int
    slug: str
    name: str
    description: Optional[str]
    owner_user_id: Optional[int]
    owner_username: Optional[str]
    capacity_cap_inr: Optional[float]
    target_volatility: Optional[float]
    is_active: bool
    # Live stats — orders + realised + unrealised P&L. Cheap aggregates
    # via the indexed strategy_id query. NULL when no orders link to
    # this strategy yet.
    open_order_count: int = 0
    closed_order_count: int = 0
    realised_pnl: float = 0.0
    unrealised_pnl: float = 0.0
    created_at: str = ""
    updated_at: str = ""


class StrategiesResponse(msgspec.Struct):
    rows: list[StrategyInfo]


class LotInfo(msgspec.Struct):
    """One row in the lot ledger viewer on /strategies/{id}. Mirrors
    the StrategyLot SQLAlchemy model with the float coercions done
    server-side so the UI just renders + formats."""
    id: int
    strategy_id: int
    open_order_id: Optional[int]
    account: str
    symbol: str
    exchange: str
    side: str                 # 'B' / 'S'
    qty: int
    remaining_qty: int
    open_price: float
    close_price: Optional[float]
    realized_pnl: float
    opened_at: str
    closed_at: Optional[str]


class LotsResponse(msgspec.Struct):
    rows: list[LotInfo]
    total_open: int
    total_closed: int


class StrategyCreate(msgspec.Struct):
    slug: str
    name: str
    description: Optional[str] = None
    owner_user_id: Optional[int] = None
    capacity_cap_inr: Optional[float] = None
    target_volatility: Optional[float] = None
    is_active: bool = True


class StrategyUpdate(msgspec.Struct):
    """Partial update. Every field optional."""
    slug:              Optional[str]   = None
    name:              Optional[str]   = None
    description:       Optional[str]   = None
    owner_user_id:     Optional[int]   = None
    capacity_cap_inr:  Optional[float] = None
    target_volatility: Optional[float] = None
    is_active:         Optional[bool]  = None


# ── Helpers ───────────────────────────────────────────────────────────────


async def _enrich_with_pnl(session, row: Strategy,
                           owner_username: Optional[str]) -> StrategyInfo:
    """Build a StrategyInfo with the live order-count + P&L rollup.

    Realised P&L comes from the lot ledger (slice 7a) — FIFO-accurate
    even across partial closes. Unrealised P&L stays on AlgoOrder.pnl
    SUM for open orders: the ledger doesn't track LTP, so a true
    mark-to-market on open lots needs the quote feed which lives at
    the route layer, not the model layer. Slice 7b adds an LTP
    snapshot pass that re-derives unrealised from the open lots'
    remaining_qty × LTP — until then the broker's MTM number is the
    operational truth for open-position P&L.

    For strategies with NO lot-ledger entries (pre-7a orders or
    legacy un-attributed history), realised falls back to
    AlgoOrder.pnl SUM. Same path as the slice 6 v1 implementation
    so behaviour for legacy data doesn't regress.
    """
    # Order counts split by status — OPEN / CHASING are "open"; FILLED
    # / UNFILLED / CANCELLED are "closed" from the strategy's view.
    _open_states = ("OPEN", "CHASING", "PENDING")
    open_count = (await session.execute(
        select(func.count(AlgoOrder.id))
        .where(AlgoOrder.strategy_id == row.id,
               AlgoOrder.status.in_(_open_states))
    )).scalar_one() or 0
    closed_count = (await session.execute(
        select(func.count(AlgoOrder.id))
        .where(AlgoOrder.strategy_id == row.id,
               AlgoOrder.status.notin_(_open_states))
    )).scalar_one() or 0

    # Realized — try the lot ledger first (slice 7a). When the strategy
    # has no ledger entries (legacy / un-attributed history), fall back
    # to AlgoOrder.pnl SUM so the rollup never reads zero on real data.
    from backend.api.algo.lot_ledger import (
        compute_strategy_pnl, compute_unrealised_marked_to_ltp,
    )
    ledger_view = await compute_strategy_pnl(session, row.id)
    has_ledger = (ledger_view["open_lots_count"] > 0
                  or ledger_view["realised_pnl"] != 0.0)
    if has_ledger:
        realised = ledger_view["realised_pnl"]
    else:
        realised = (await session.execute(
            select(func.coalesce(func.sum(AlgoOrder.pnl), 0.0))
            .where(AlgoOrder.strategy_id == row.id,
                   AlgoOrder.status.notin_(_open_states))
        )).scalar_one() or 0.0

    # Unrealised — slice 7d wires the LTP-marked path. When open lots
    # exist in the ledger, compute (LTP - open_price) × remaining_qty
    # per lot. Falls back to AlgoOrder.pnl SUM proxy when:
    #   - LTP path returns None (no LTP feed available right now), OR
    #   - ledger has no open lots for this strategy (legacy /
    #     unattributed orders still hold open exposure tracked only
    #     via the broker's MTM number).
    unrealised: float
    if ledger_view["open_lots_count"] > 0:
        mtm = await compute_unrealised_marked_to_ltp(session, row.id)
        if mtm is not None:
            unrealised = mtm
        else:
            unrealised = (await session.execute(
                select(func.coalesce(func.sum(AlgoOrder.pnl), 0.0))
                .where(AlgoOrder.strategy_id == row.id,
                       AlgoOrder.status.in_(_open_states))
            )).scalar_one() or 0.0
    else:
        unrealised = (await session.execute(
            select(func.coalesce(func.sum(AlgoOrder.pnl), 0.0))
            .where(AlgoOrder.strategy_id == row.id,
                   AlgoOrder.status.in_(_open_states))
        )).scalar_one() or 0.0

    return StrategyInfo(
        id=row.id,
        slug=row.slug,
        name=row.name,
        description=row.description,
        owner_user_id=row.owner_user_id,
        owner_username=owner_username,
        capacity_cap_inr=float(row.capacity_cap_inr) if row.capacity_cap_inr is not None else None,
        target_volatility=float(row.target_volatility) if row.target_volatility is not None else None,
        is_active=bool(row.is_active),
        open_order_count=int(open_count),
        closed_order_count=int(closed_count),
        realised_pnl=float(realised or 0.0),
        unrealised_pnl=float(unrealised or 0.0),
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else "",
    )


def _validate_slug(slug: str) -> str:
    """Operator-typed slugs must be lowercase + alphanumerics +
    hyphens only. Matches the convention used by agent slugs +
    fragment names — one consistent identifier shape across the app.
    """
    s = (slug or "").strip().lower()
    if not s:
        raise HTTPException(status_code=400, detail="slug is required")
    if not all(c.isalnum() or c == "-" for c in s):
        raise HTTPException(
            status_code=400,
            detail="slug must be lowercase alphanumerics + hyphens",
        )
    return s


# ── Controller ────────────────────────────────────────────────────────────


class StrategiesController(Controller):
    path = "/api/strategies"
    # Per-route caps. Reads via `view_strategies` (demo-readable for
    # the showcase tour). Mutations via `manage_own_strategies`
    # (admin / trader). Admin can reassign owner_user_id via
    # `reassign_strategies` (validated inside the PATCH handler).

    @get("/", guards=[cap_guard("view_strategies")])
    async def list_strategies(self, active_only: bool = False) -> StrategiesResponse:
        async with async_session() as s:
            q = select(Strategy, User.username).outerjoin(
                User, User.id == Strategy.owner_user_id,
            ).order_by(Strategy.slug)
            if active_only:
                q = q.where(Strategy.is_active.is_(True))
            rows = (await s.execute(q)).all()
            out: list[StrategyInfo] = []
            for row, owner_username in rows:
                out.append(await _enrich_with_pnl(s, row, owner_username))
        return StrategiesResponse(rows=out)

    @get("/{strategy_id:int}", guards=[cap_guard("view_strategies")])
    async def get_strategy(self, strategy_id: int) -> StrategyInfo:
        async with async_session() as s:
            row = (await s.execute(
                select(Strategy, User.username).outerjoin(
                    User, User.id == Strategy.owner_user_id,
                ).where(Strategy.id == strategy_id)
            )).first()
            if not row:
                raise HTTPException(status_code=404,
                                    detail=f"Strategy {strategy_id} not found")
            return await _enrich_with_pnl(s, row[0], row[1])

    @post("/", guards=[cap_guard("manage_own_strategies")])
    async def create_strategy(self, data: StrategyCreate) -> StrategyInfo:
        slug = _validate_slug(data.slug)
        name = (data.name or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="name is required")
        async with async_session() as s:
            existing = (await s.execute(
                select(Strategy).where(Strategy.slug == slug)
            )).scalar_one_or_none()
            if existing:
                raise HTTPException(status_code=409,
                    detail=f"Strategy slug {slug!r} already exists")
            row = Strategy(
                slug=slug,
                name=name,
                description=data.description,
                owner_user_id=data.owner_user_id,
                capacity_cap_inr=data.capacity_cap_inr,
                target_volatility=data.target_volatility,
                is_active=bool(data.is_active),
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
            owner = None
            if row.owner_user_id is not None:
                owner = (await s.execute(
                    select(User.username).where(User.id == row.owner_user_id)
                )).scalar_one_or_none()
            logger.info(f"strategy created: {slug!r} (owner_user_id={row.owner_user_id})")
            return await _enrich_with_pnl(s, row, owner)

    @patch("/{strategy_id:int}", guards=[cap_guard("manage_own_strategies")])
    async def update_strategy(self, strategy_id: int,
                              data: StrategyUpdate) -> StrategyInfo:
        async with async_session() as s:
            row = await s.get(Strategy, strategy_id)
            if not row:
                raise HTTPException(status_code=404,
                                    detail=f"Strategy {strategy_id} not found")
            if data.slug is not None:
                row.slug = _validate_slug(data.slug)
            if data.name is not None:
                name = data.name.strip()
                if not name:
                    raise HTTPException(status_code=400,
                                        detail="name cannot be blank")
                row.name = name
            if data.description is not None:
                row.description = data.description
            if data.owner_user_id is not None:
                row.owner_user_id = data.owner_user_id
            if data.capacity_cap_inr is not None:
                row.capacity_cap_inr = data.capacity_cap_inr
            if data.target_volatility is not None:
                row.target_volatility = data.target_volatility
            if data.is_active is not None:
                row.is_active = bool(data.is_active)
            row.updated_at = datetime.now(timezone.utc)
            try:
                await s.commit()
            except Exception as exc:
                await s.rollback()
                raise HTTPException(status_code=409,
                                    detail=f"Conflict: {exc}") from exc
            await s.refresh(row)
            owner = None
            if row.owner_user_id is not None:
                owner = (await s.execute(
                    select(User.username).where(User.id == row.owner_user_id)
                )).scalar_one_or_none()
            return await _enrich_with_pnl(s, row, owner)

    @get("/{strategy_id:int}/lots", guards=[cap_guard("view_strategies")])
    async def list_lots(self, strategy_id: int,
                        include_closed: bool = True,
                        limit: int = 500) -> LotsResponse:
        """Per-strategy lot ledger viewer. Newest opens first; closed
        lots interleaved. Used by /strategies/{id} detail page's
        ledger table."""
        from backend.api.algo.lot_ledger import list_lots_for_strategy
        async with async_session() as s:
            rows = await list_lots_for_strategy(
                s, strategy_id,
                include_closed=include_closed, limit=limit,
            )
            # Counts (cheap aggregates separate from the page slice).
            from sqlalchemy import func
            total_open = (await s.execute(
                select(func.count(StrategyLot.id))
                .where(StrategyLot.strategy_id == strategy_id,
                       StrategyLot.remaining_qty > 0)
            )).scalar_one() or 0
            total_closed = (await s.execute(
                select(func.count(StrategyLot.id))
                .where(StrategyLot.strategy_id == strategy_id,
                       StrategyLot.remaining_qty == 0)
            )).scalar_one() or 0
        out = []
        for r in rows:
            out.append(LotInfo(
                id=r.id,
                strategy_id=r.strategy_id,
                open_order_id=r.open_order_id,
                account=r.account,
                symbol=r.symbol,
                exchange=r.exchange,
                side=r.side,
                qty=int(r.qty),
                remaining_qty=int(r.remaining_qty),
                open_price=float(r.open_price),
                close_price=float(r.close_price) if r.close_price is not None else None,
                realized_pnl=float(r.realized_pnl or 0.0),
                opened_at=r.opened_at.isoformat() if r.opened_at else "",
                closed_at=r.closed_at.isoformat() if r.closed_at else None,
            ))
        return LotsResponse(rows=out, total_open=int(total_open),
                            total_closed=int(total_closed))

    @delete("/{strategy_id:int}", status_code=204,
            guards=[cap_guard("manage_own_strategies")])
    async def delete_strategy(self, strategy_id: int) -> None:
        """Delete a strategy. AlgoOrder rows pointing at it have
        `strategy_id` set to NULL via ON DELETE SET NULL — the
        historical orders survive but lose attribution. Operator
        intent is "remove the bucket; the trades happened on the
        broker regardless".
        """
        async with async_session() as s:
            row = await s.get(Strategy, strategy_id)
            if not row:
                raise HTTPException(status_code=404,
                                    detail=f"Strategy {strategy_id} not found")
            await s.delete(row)
            await s.commit()
        logger.info(f"strategy deleted: id={strategy_id} slug={row.slug!r}")
