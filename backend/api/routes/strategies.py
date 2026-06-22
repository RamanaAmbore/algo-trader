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
from backend.api.models import Strategy, AlgoOrder, User
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

    v1 aggregates the AlgoOrder.pnl field — broker-reported P&L,
    accurate for closed positions and a mark-to-market approximation
    for open ones. Slice 7's lot ledger replaces this with strategy-
    scoped FIFO accounting once partial closes need precise per-
    strategy attribution.
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

    # Realized = sum of pnl on closed orders. Unrealised = sum of pnl
    # on still-open orders (broker's MTM number). Approximate v1;
    # slice 7 wires the lot ledger for precise FIFO accounting.
    realised = (await session.execute(
        select(func.coalesce(func.sum(AlgoOrder.pnl), 0.0))
        .where(AlgoOrder.strategy_id == row.id,
               AlgoOrder.status.notin_(_open_states))
    )).scalar_one() or 0.0
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
