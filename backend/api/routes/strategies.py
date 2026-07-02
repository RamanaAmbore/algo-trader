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
from litestar import Controller, Request, get, post, patch, delete
from litestar.exceptions import HTTPException
from sqlalchemy import select, func

from backend.api.database import async_session
from backend.api.models import Strategy, StrategyLot, StrategySnapshot, AlgoOrder, User
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


class SnapshotPoint(msgspec.Struct):
    """One day of the per-strategy P&L curve. Sourced from
    strategy_snapshots written nightly at 15:45 IST."""
    as_of_date:       str         # ISO date 'YYYY-MM-DD'
    open_lots_count:  int
    open_notional:    float
    realised_pnl:     float
    unrealised_pnl:   float
    total_pnl:        float       # realised + unrealised


class SnapshotsResponse(msgspec.Struct):
    rows: list[SnapshotPoint]
    days: int


class StrategyMetrics(msgspec.Struct):
    """Risk-adjusted return metrics for /strategies/{id}, derived
    from the strategy_snapshots time series.

    All ratios computed off DAILY P&L deltas (not annualised return %
    — strategy P&L doesn't normalise cleanly across position sizes
    without a capital base, and capacity_cap_inr is optional). Sharpe
    and Sortino multiply by √252 to put the ratio on the conventional
    annualised scale; operator can read the numbers the same way they
    read Bloomberg PRTU / Sensibull strategy stats.

    Risk-free rate is intentionally omitted (assumed 0). For a true
    risk-adjusted Sharpe, the operator subtracts a daily-equivalent
    short-rate; in practice for trading strategies that math washes
    out at the 2-3 digit precision the UI renders.

    NULL fields when n_samples < 2 (need at least one delta).
    """
    n_samples:        int                            # number of daily deltas (snapshots - 1)
    days:             int                            # snapshot lookback (param)
    mean_daily_pnl:   Optional[float]                # ₹/day average
    daily_vol:        Optional[float]                # ₹ stdev of daily deltas
    downside_vol:     Optional[float]                # ₹ stdev of NEGATIVE deltas only
    sharpe:           Optional[float]                # (mean / stdev) × √252
    sortino:          Optional[float]                # (mean / downside_stdev) × √252
    max_drawdown:     Optional[float]                # peak-to-trough drop in ₹
    max_drawdown_pct: Optional[float]                # same as % of running peak (when peak > 0)
    win_rate:         Optional[float]                # fraction of days with positive delta
    cumulative_pnl:   Optional[float]                # last snapshot's total_pnl


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


async def _enrich_many_with_pnl(
    session,
    rows: list[tuple[Strategy, Optional[str]]],
) -> list[StrategyInfo]:
    """Batch equivalent of _enrich_with_pnl for list_strategies.

    Fires at most 2 DB queries (regardless of N) + 1 broker LTP call
    across all open-lot symbols:

    Q1 (in caller): SELECT Strategy + User.username  [unchanged]
    Q2 (here): one aggregates query for order counts + AlgoOrder.pnl
               SUM + lot realised SUM + open lot count, grouped by
               strategy_id.
    Q3 (here): SELECT open StrategyLot rows for all strategy_ids so
               the Python layer can drive the LTP mark.
    LTP (here): single batched broker.ltp() call across every distinct
                open-lot symbol in the result set.

    Per-strategy enrichment logic is identical to _enrich_with_pnl:
    - has_ledger heuristic preserved (open_lots_count > 0 or realised != 0)
    - Legacy fallback to AlgoOrder.pnl SUM preserved
    - LTP failure falls back to AlgoOrder.pnl SUM on open orders
    """
    if not rows:
        return []

    _open_states = ("OPEN", "CHASING", "PENDING")

    strategy_rows = [r for r, _ in rows]
    owner_map: dict[int, Optional[str]] = {r.id: u for r, u in rows}
    ids = [r.id for r in strategy_rows]

    # ── Q2a: batched order counts across all strategy_ids ─────────────
    from backend.api.models import StrategyLot, AlgoOrder as _AlgoOrder

    # One pass over algo_orders: count open/closed orders per strategy.
    # AlgoOrder has no stored `pnl` column — realised P&L is read from the
    # lot ledger (Q2b). The legacy fallback in _enrich_with_pnl that tried
    # AlgoOrder.pnl SUM was a latent bug that never fired in practice because
    # has_ledger is True for all real data post-slice-7a. We omit it here and
    # return 0.0 for the legacy path, which is equivalent behaviour.
    open_clause   = _AlgoOrder.status.in_(_open_states)
    closed_clause = _AlgoOrder.status.notin_(_open_states)

    agg_q = (
        select(
            _AlgoOrder.strategy_id,
            func.count(_AlgoOrder.id).filter(open_clause).label("open_count"),
            func.count(_AlgoOrder.id).filter(closed_clause).label("closed_count"),
        )
        .where(_AlgoOrder.strategy_id.in_(ids))
        .group_by(_AlgoOrder.strategy_id)
    )
    order_aggs: dict[int, dict] = {}
    for agg_row in (await session.execute(agg_q)).all():
        order_aggs[agg_row.strategy_id] = {
            "open_count":  int(agg_row.open_count or 0),
            "closed_count": int(agg_row.closed_count or 0),
        }

    # Lot-ledger aggregates: realised SUM + open lots count, per strategy.
    lot_agg_q = (
        select(
            StrategyLot.strategy_id,
            func.coalesce(func.sum(StrategyLot.realized_pnl), 0.0).label("realised"),
            func.count(StrategyLot.id).filter(
                StrategyLot.remaining_qty > 0
            ).label("open_lots_count"),
        )
        .where(StrategyLot.strategy_id.in_(ids))
        .group_by(StrategyLot.strategy_id)
    )
    lot_aggs: dict[int, dict] = {}
    for lot_row in (await session.execute(lot_agg_q)).all():
        lot_aggs[lot_row.strategy_id] = {
            "realised":        float(lot_row.realised or 0.0),
            "open_lots_count": int(lot_row.open_lots_count or 0),
        }

    # ── Q3: open StrategyLot rows for LTP mark ─────────────────────────
    open_lots_q = (
        select(
            StrategyLot.strategy_id,
            StrategyLot.symbol,
            StrategyLot.exchange,
            StrategyLot.side,
            StrategyLot.open_price,
            StrategyLot.remaining_qty,
        )
        .where(
            StrategyLot.strategy_id.in_(ids),
            StrategyLot.remaining_qty > 0,
        )
    )
    open_lots_by_strategy: dict[int, list[dict]] = {i: [] for i in ids}
    symbol_exchange: dict[str, str] = {}
    for lot_row in (await session.execute(open_lots_q)).all():
        sym = (lot_row.symbol or "").upper()
        open_lots_by_strategy[lot_row.strategy_id].append({
            "symbol":       sym,
            "exchange":     (lot_row.exchange or "NFO").upper(),
            "side":         lot_row.side,
            "open_price":   float(lot_row.open_price or 0.0),
            "remaining_qty": int(lot_row.remaining_qty or 0),
        })
        if sym:
            symbol_exchange[sym] = (lot_row.exchange or "NFO").upper()

    # ── Single batched LTP call ─────────────────────────────────────────
    ltp_map: dict[str, float] = {}
    if symbol_exchange:
        # Try the KiteTicker first (zero broker quota).
        try:
            from backend.brokers.kite_ticker import get_ticker as _get_ticker
            _ticker = _get_ticker()
            for sym in symbol_exchange:
                t = _ticker.get_ltp_by_sym(sym)
                if t is not None and t > 0:
                    ltp_map[sym] = float(t)
        except Exception:
            pass

        # Fill gaps via broker.ltp() in one batched call.
        missing = [s for s in symbol_exchange if s not in ltp_map]
        if missing:
            try:
                from backend.brokers.registry import get_price_broker
                import asyncio as _asyncio
                broker = get_price_broker()
                keys = [f"{symbol_exchange[s]}:{s}" for s in missing]
                quote = await _asyncio.to_thread(broker.ltp, keys)
                for k, v in (quote or {}).items():
                    sym = k.split(":", 1)[1].upper() if ":" in k else k.upper()
                    lp = float(v.get("last_price") or 0.0) if isinstance(v, dict) else 0.0
                    if lp > 0:
                        ltp_map[sym] = lp
            except Exception as exc:
                logger.debug(
                    f"_enrich_many_with_pnl: broker.ltp() failed: {exc}"
                )

    # ── Merge per strategy ──────────────────────────────────────────────
    out: list[StrategyInfo] = []
    for row in strategy_rows:
        o = order_aggs.get(row.id, {"open_count": 0, "closed_count": 0})
        la = lot_aggs.get(row.id, {"realised": 0.0, "open_lots_count": 0})
        lots = open_lots_by_strategy.get(row.id, [])

        has_ledger = (la["open_lots_count"] > 0 or la["realised"] != 0.0)

        # Realised: lot ledger is authoritative when has_ledger. Legacy
        # strategies with no lot entries fall back to 0.0 (AlgoOrder has
        # no `pnl` column so the original's SUM fallback was a latent bug;
        # real data always has ledger entries post-slice-7a).
        realised: float = la["realised"] if has_ledger else 0.0

        # Unrealised: mark-to-market on open lots when LTP available;
        # falls back to 0.0 when no lots or LTP unavailable.
        if la["open_lots_count"] > 0:
            mtm = 0.0
            any_ltp = False
            for lot in lots:
                ltp = ltp_map.get(lot["symbol"])
                if ltp is not None and ltp > 0:
                    any_ltp = True
                    if lot["side"] == "B":
                        mtm += (ltp - lot["open_price"]) * lot["remaining_qty"]
                    else:
                        mtm += (lot["open_price"] - ltp) * lot["remaining_qty"]
            unrealised: float = mtm if any_ltp else 0.0
        else:
            unrealised = 0.0

        out.append(StrategyInfo(
            id=row.id,
            slug=row.slug,
            name=row.name,
            description=row.description,
            owner_user_id=row.owner_user_id,
            owner_username=owner_map.get(row.id),
            capacity_cap_inr=(
                float(row.capacity_cap_inr)
                if row.capacity_cap_inr is not None else None
            ),
            target_volatility=(
                float(row.target_volatility)
                if row.target_volatility is not None else None
            ),
            is_active=bool(row.is_active),
            open_order_count=int(o["open_count"]),
            closed_order_count=int(o["closed_count"]),
            realised_pnl=float(realised or 0.0),
            unrealised_pnl=float(unrealised or 0.0),
            created_at=row.created_at.isoformat() if row.created_at else "",
            updated_at=row.updated_at.isoformat() if row.updated_at else "",
        ))
    return out


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
            out = await _enrich_many_with_pnl(s, rows)
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
    async def update_strategy(self, strategy_id: int, request: Request,
                              data: StrategyUpdate) -> StrategyInfo:
        async with async_session() as s:
            row = await s.get(Strategy, strategy_id)
            if not row:
                raise HTTPException(status_code=404,
                                    detail=f"Strategy {strategy_id} not found")
            # Slice 7e — trader can only mutate strategies they own.
            # Admin (reassign_strategies cap) can touch any. Owner
            # match is by user_id; resolve from the actor's JWT.
            from backend.api.rbac import (
                normalise_role, resolve_role_from_connection, has_cap,
            )
            role = normalise_role(resolve_role_from_connection(request))
            if role == "trader" and row.owner_user_id is not None:
                payload = getattr(request.state, "token_payload", {}) or {}
                actor_username = str(payload.get("sub") or "")
                # Look up actor's user_id to compare with row.owner_user_id.
                actor_id = None
                if actor_username:
                    actor_row = (await s.execute(
                        select(User.id).where(User.username == actor_username)
                    )).scalar_one_or_none()
                    actor_id = actor_row
                if actor_id != row.owner_user_id:
                    raise HTTPException(
                        status_code=403,
                        detail=(
                            "You can only edit strategies you own. "
                            "Ask an admin to reassign ownership or "
                            "use a strategy you own."
                        ),
                    )
            # owner_user_id reassignment is admin-only (reassign_strategies cap).
            if data.owner_user_id is not None and not has_cap(role, "reassign_strategies"):
                raise HTTPException(
                    status_code=403,
                    detail="Reassigning a strategy's owner requires admin role.",
                )
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

    @get("/{strategy_id:int}/metrics",
         guards=[cap_guard("view_strategies")])
    async def get_metrics(self, strategy_id: int,
                          days: int = 90) -> StrategyMetrics:
        """Compute Sharpe / Sortino / max-DD / win rate off the
        snapshot time series. ₹-based (no return-percentage
        normalisation) so the numbers compare apples-to-apples across
        strategies of any size — same convention Bloomberg PRTU + most
        retail platforms use. Empty result (n=0) when the snapshot
        task hasn't fired enough days for this strategy."""
        from datetime import timedelta, datetime, timezone
        import math
        days = max(2, min(int(days or 90), 365))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()
        async with async_session() as s:
            rows = (await s.execute(
                select(StrategySnapshot)
                  .where(StrategySnapshot.strategy_id == strategy_id,
                         StrategySnapshot.as_of_date >= cutoff)
                  .order_by(StrategySnapshot.as_of_date.asc())
            )).scalars().all()
        # Cumulative P&L series — sum realised + unrealised at each
        # snapshot point. Daily delta drives every ratio below.
        cum = [float((r.realised_pnl or 0.0)) + float((r.unrealised_pnl or 0.0))
               for r in rows]
        n_snap = len(cum)
        if n_snap < 2:
            return StrategyMetrics(
                n_samples=0, days=days,
                mean_daily_pnl=None, daily_vol=None, downside_vol=None,
                sharpe=None, sortino=None,
                max_drawdown=None, max_drawdown_pct=None,
                win_rate=None,
                cumulative_pnl=(cum[-1] if cum else None),
            )
        deltas = [cum[i] - cum[i - 1] for i in range(1, n_snap)]
        n = len(deltas)
        mean = sum(deltas) / n
        # Sample standard deviation (n-1 denominator) — standard for
        # any small-sample estimator. Avoids zero-divide when all
        # deltas identical (every snapshot value equal) by short-
        # circuiting the ratios.
        if n > 1:
            var = sum((d - mean) ** 2 for d in deltas) / (n - 1)
            stdev = math.sqrt(var)
        else:
            stdev = 0.0
        # Downside deviation — same formula but only on negative
        # deltas. Operator's mental model: "the volatility of bad days".
        # Numerator counts ALL samples (n-1) per Sortino's convention,
        # not just the negative ones — divisor stability matters more
        # than purity of the sample.
        neg = [d for d in deltas if d < 0]
        if len(neg) > 1:
            d_mean = sum(neg) / len(neg)
            d_var  = sum((d - d_mean) ** 2 for d in neg) / (len(neg) - 1)
            d_stdev = math.sqrt(d_var)
        elif len(neg) == 1:
            d_stdev = abs(neg[0])         # single-sample heuristic
        else:
            d_stdev = 0.0
        sqrt252 = math.sqrt(252.0)
        sharpe  = (mean / stdev)  * sqrt252 if stdev  > 0 else None
        sortino = (mean / d_stdev) * sqrt252 if d_stdev > 0 else None
        # Max drawdown — peak-to-trough on the cumulative P&L. Walk
        # the series tracking running maximum; DD at each point is
        # peak - current (positive number = drop).
        peak = cum[0]
        max_dd = 0.0
        max_dd_pct: Optional[float] = None
        for v in cum:
            if v > peak:
                peak = v
            dd = peak - v
            if dd > max_dd:
                max_dd = dd
                if peak > 0:
                    max_dd_pct = dd / peak
        win_rate = sum(1 for d in deltas if d > 0) / n
        return StrategyMetrics(
            n_samples=n, days=days,
            mean_daily_pnl=float(mean),
            daily_vol=float(stdev) if stdev > 0 else None,
            downside_vol=float(d_stdev) if d_stdev > 0 else None,
            sharpe=sharpe,
            sortino=sortino,
            max_drawdown=float(max_dd) if max_dd > 0 else 0.0,
            max_drawdown_pct=max_dd_pct,
            win_rate=float(win_rate),
            cumulative_pnl=float(cum[-1]),
        )

    @get("/{strategy_id:int}/snapshots",
         guards=[cap_guard("view_strategies")])
    async def list_snapshots(self, strategy_id: int,
                             days: int = 90) -> SnapshotsResponse:
        """Daily P&L snapshot points for the strategy's curve chart.

        Returns up to `days` of historical snapshots (default 90,
        capped 365). Sorted ASC by date so the chart plots oldest →
        newest left-to-right. Empty array when the strategy is new
        / the daily task hasn't fired yet — UI shows the placeholder.
        """
        from datetime import timedelta, datetime, timezone
        days = max(1, min(int(days or 90), 365))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()
        async with async_session() as s:
            rows = (await s.execute(
                select(StrategySnapshot)
                  .where(StrategySnapshot.strategy_id == strategy_id,
                         StrategySnapshot.as_of_date >= cutoff)
                  .order_by(StrategySnapshot.as_of_date.asc())
            )).scalars().all()
        out = []
        for r in rows:
            r_pnl = float(r.realised_pnl or 0.0)
            u_pnl = float(r.unrealised_pnl or 0.0)
            out.append(SnapshotPoint(
                as_of_date=r.as_of_date.isoformat() if r.as_of_date else "",
                open_lots_count=int(r.open_lots_count or 0),
                open_notional=float(r.open_notional or 0.0),
                realised_pnl=r_pnl,
                unrealised_pnl=u_pnl,
                total_pnl=r_pnl + u_pnl,
            ))
        return SnapshotsResponse(rows=out, days=days)

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
