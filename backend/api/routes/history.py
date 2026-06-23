"""
History — multi-day forensic surface for orders, trades, and funds.

GET /api/admin/history/orders  — AlgoOrder table, date + account + symbol + status filters
GET /api/admin/history/trades  — daily_book where kind='trades'
GET /api/admin/history/funds   — daily_book where kind='funds' (per-account ledger)

All three are paginated (default 50/page, cap 500). Cap-gated by
`view_audit` — same gate as the audit log; admin / risk / ops only.

Operator workflow: pick a date range + accounts + status pill, scan
the table, drill via the Audit log for the request_id of any row that
looks suspicious.
"""

from __future__ import annotations

from datetime import date as _date, datetime, timedelta, timezone
from typing import Optional

import msgspec
from litestar import Controller, get
from sqlalchemy import select, and_, desc, func as _func

from backend.api.database import async_session
from backend.api.models import AlgoOrder, DailyBook
from backend.api.rbac import cap_guard


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class _OrderRow(msgspec.Struct):
    id:               int
    created_at:       str
    account:          str
    symbol:           str
    exchange:         str
    transaction_type: str          # BUY / SELL
    quantity:         int
    filled_quantity:  int
    initial_price:    Optional[float]
    fill_price:       Optional[float]
    slippage:         Optional[float]
    status:           str
    mode:             str          # sim / paper / live / shadow / replay
    engine:           str
    broker_order_id:  Optional[str]
    detail:           Optional[str]


class OrderListResponse(msgspec.Struct):
    rows:    list[_OrderRow]
    total:   int
    limit:   int
    offset:  int
    counts:  dict[str, int]        # status histogram


class _TradeRow(msgspec.Struct):
    date:        str               # ISO date
    account:     str
    segment:     str
    symbol:      str
    exchange:    Optional[str]
    qty:         int
    avg_cost:    Optional[float]
    notional:    Optional[float]   # qty × avg_cost
    captured_at: str


class TradeListResponse(msgspec.Struct):
    rows:    list[_TradeRow]
    total:   int
    limit:   int
    offset:  int
    summary: dict[str, float]      # total_notional, etc.


class _FundsRow(msgspec.Struct):
    date:           str
    account:        str
    segment:        str            # 'equity' | 'commodity'
    cash_available: Optional[float]
    opening_balance:Optional[float]
    debits_today:   int
    realised_m2m:   Optional[float]
    net:            Optional[float]


class FundsListResponse(msgspec.Struct):
    rows:           list[_FundsRow]
    total:          int
    earliest_date:  Optional[str]  # 'tracking started X days ago' hint


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_date(s: Optional[str]) -> Optional[_date]:
    if not s:
        return None
    try:
        return _date.fromisoformat(s.strip()[:10])
    except (ValueError, AttributeError):
        return None


def _accounts_filter(s: Optional[str]) -> list[str]:
    if not s:
        return []
    return [a.strip() for a in s.split(",") if a.strip()]


def _default_range(days: int = 30) -> tuple[_date, _date]:
    """Inclusive [from_date, to_date]. Default to the last `days` days
    ending today (IST)."""
    today = datetime.now(timezone.utc).date()
    return (today - timedelta(days=days), today)


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class HistoryController(Controller):
    path = "/api/admin/history"

    @get("/orders", guards=[cap_guard("view_audit")])
    async def list_orders(
        self,
        from_date:   Optional[str] = None,
        to_date:     Optional[str] = None,
        accounts:    Optional[str] = None,   # comma-separated
        symbols:     Optional[str] = None,   # comma-separated
        status:      Optional[str] = None,   # OPEN / FILLED / CANCELLED / REJECTED / UNFILLED
        mode:        Optional[str] = None,   # live / paper / sim / replay / shadow
        limit:       int = 50,
        offset:      int = 0,
    ) -> OrderListResponse:
        """AlgoOrder history. Date filter is on `created_at` so the
        operator sees every order they PLACED in the range, not just
        the ones that filled in the range."""
        limit  = max(1, min(int(limit or 50), 500))
        offset = max(0, int(offset or 0))
        df = _parse_date(from_date)
        dt = _parse_date(to_date)
        if not df or not dt:
            df, dt = _default_range(30)
        # Convert to UTC datetimes spanning the inclusive IST range.
        # Approximation — date filter doesn't have to be ms-precise.
        from_dt = datetime.combine(df, datetime.min.time(), tzinfo=timezone.utc)
        to_dt   = datetime.combine(dt + timedelta(days=1),
                                   datetime.min.time(), tzinfo=timezone.utc)

        conditions = [
            AlgoOrder.created_at >= from_dt,
            AlgoOrder.created_at <  to_dt,
        ]
        accts = _accounts_filter(accounts)
        if accts:
            conditions.append(AlgoOrder.account.in_(accts))
        syms = _accounts_filter(symbols)
        if syms:
            conditions.append(AlgoOrder.symbol.in_([s.upper() for s in syms]))
        if status:
            conditions.append(AlgoOrder.status == status.strip().upper())
        if mode:
            conditions.append(AlgoOrder.mode == mode.strip().lower())

        async with async_session() as s:
            count_q = select(_func.count(AlgoOrder.id)).where(and_(*conditions))
            total = (await s.execute(count_q)).scalar_one() or 0

            counts_q = (
                select(AlgoOrder.status, _func.count(AlgoOrder.id))
                  .where(and_(*conditions))
                  .group_by(AlgoOrder.status)
            )
            counts: dict[str, int] = {}
            for st, c in (await s.execute(counts_q)).all():
                counts[str(st)] = int(c)

            rows = (await s.execute(
                select(AlgoOrder).where(and_(*conditions))
                  .order_by(desc(AlgoOrder.created_at))
                  .limit(limit).offset(offset)
            )).scalars().all()

        return OrderListResponse(
            rows=[
                _OrderRow(
                    id=r.id,
                    created_at=r.created_at.isoformat() if r.created_at else "",
                    account=r.account,
                    symbol=r.symbol,
                    exchange=r.exchange,
                    transaction_type=r.transaction_type,
                    quantity=int(r.quantity or 0),
                    filled_quantity=int(r.filled_quantity or 0),
                    initial_price=(float(r.initial_price)
                                   if r.initial_price is not None else None),
                    fill_price=(float(r.fill_price)
                                if r.fill_price is not None else None),
                    slippage=(float(r.slippage)
                              if r.slippage is not None else None),
                    status=r.status,
                    mode=r.mode,
                    engine=r.engine,
                    broker_order_id=r.broker_order_id,
                    detail=r.detail,
                )
                for r in rows
            ],
            total=int(total), limit=limit, offset=offset, counts=counts,
        )

    @get("/trades", guards=[cap_guard("view_audit")])
    async def list_trades(
        self,
        from_date:   Optional[str] = None,
        to_date:     Optional[str] = None,
        accounts:    Optional[str] = None,
        symbols:     Optional[str] = None,
        limit:       int = 50,
        offset:      int = 0,
    ) -> TradeListResponse:
        """daily_book trade rows. Date filter on `daily_book.date`
        (the trading day, not capture timestamp)."""
        limit  = max(1, min(int(limit or 50), 500))
        offset = max(0, int(offset or 0))
        df = _parse_date(from_date)
        dt = _parse_date(to_date)
        if not df or not dt:
            df, dt = _default_range(30)

        conditions = [
            DailyBook.kind == "trades",
            DailyBook.date >= df,
            DailyBook.date <= dt,
        ]
        accts = _accounts_filter(accounts)
        if accts:
            conditions.append(DailyBook.account.in_(accts))
        syms = _accounts_filter(symbols)
        if syms:
            conditions.append(DailyBook.symbol.in_([s.upper() for s in syms]))

        async with async_session() as s:
            total = (await s.execute(
                select(_func.count(DailyBook.id)).where(and_(*conditions))
            )).scalar_one() or 0

            # Notional sum across the filtered set. Sums qty * avg_cost
            # at the DB level so we don't have to page through every
            # row client-side just to surface a total.
            notional_q = select(_func.coalesce(
                _func.sum(DailyBook.qty * DailyBook.avg_cost), 0.0
            )).where(and_(*conditions))
            total_notional = float((await s.execute(notional_q)).scalar_one() or 0.0)

            rows = (await s.execute(
                select(DailyBook).where(and_(*conditions))
                  .order_by(desc(DailyBook.date),
                            desc(DailyBook.captured_at))
                  .limit(limit).offset(offset)
            )).scalars().all()

        return TradeListResponse(
            rows=[
                _TradeRow(
                    date=r.date.isoformat() if r.date else "",
                    account=r.account,
                    segment=r.segment,
                    symbol=r.symbol,
                    exchange=r.exchange,
                    qty=int(r.qty or 0),
                    avg_cost=(float(r.avg_cost)
                              if r.avg_cost is not None else None),
                    notional=(float(r.qty) * float(r.avg_cost)
                              if r.qty is not None and r.avg_cost is not None
                              else None),
                    captured_at=r.captured_at.isoformat() if r.captured_at else "",
                )
                for r in rows
            ],
            total=int(total), limit=limit, offset=offset,
            summary={"total_notional": total_notional},
        )

    @get("/funds", guards=[cap_guard("view_audit")])
    async def list_funds(
        self,
        from_date:   Optional[str] = None,
        to_date:     Optional[str] = None,
        accounts:    Optional[str] = None,
    ) -> FundsListResponse:
        """Per-account funds snapshot rows. Funds capture started Jun
        2026 (when the daily_snapshot task gained `_funds_rows`);
        rows before that date don't exist. `earliest_date` hints when
        the tracking began so the UI can show a 'tracking from X' chip."""
        df = _parse_date(from_date)
        dt = _parse_date(to_date)
        if not df or not dt:
            df, dt = _default_range(90)  # funds tab defaults to 90 days

        conditions = [
            DailyBook.kind == "funds",
            DailyBook.date >= df,
            DailyBook.date <= dt,
        ]
        accts = _accounts_filter(accounts)
        if accts:
            conditions.append(DailyBook.account.in_(accts))

        async with async_session() as s:
            rows = (await s.execute(
                select(DailyBook).where(and_(*conditions))
                  .order_by(desc(DailyBook.date), DailyBook.account, DailyBook.segment)
            )).scalars().all()
            # Hint — earliest funds row across ALL accounts (operator
            # wants to know "since when do we have tracking?")
            earliest = (await s.execute(
                select(_func.min(DailyBook.date)).where(
                    DailyBook.kind == "funds",
                )
            )).scalar_one()

        return FundsListResponse(
            rows=[
                _FundsRow(
                    date=r.date.isoformat() if r.date else "",
                    account=r.account,
                    segment=r.segment,
                    cash_available=(float(r.avg_cost)
                                    if r.avg_cost is not None else None),
                    opening_balance=(float(r.ltp)
                                     if r.ltp is not None else None),
                    debits_today=int(r.qty or 0),
                    realised_m2m=(float(r.day_pnl)
                                  if r.day_pnl is not None else None),
                    net=(float(r.total_pnl)
                         if r.total_pnl is not None else None),
                )
                for r in rows
            ],
            total=len(rows),
            earliest_date=(earliest.isoformat() if earliest else None),
        )
