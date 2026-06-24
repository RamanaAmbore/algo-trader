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

import asyncio
from datetime import date as _date, datetime, timedelta, timezone
from typing import Optional

import msgspec
from litestar import Controller, get, post
from litestar.exceptions import HTTPException
from sqlalchemy import select, and_, desc, func as _func

from backend.api.database import async_session
from backend.api.models import AlgoOrder, DailyBook
from backend.api.auth_guard import admin_guard
from backend.api.rbac import cap_guard
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


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
    request_id:       Optional[str]   # /admin/audit drill-through
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
    # Day-over-day Δ on cash_available within the same (account,
    # segment) series. None for the first row in the series (no
    # prior reference) or when prior row's cash is null. The
    # 'cashbook' lens — running balance is implicit (cash_available
    # IS the running balance); delta makes the daily move explicit.
    cash_delta:     Optional[float]


class FundsListResponse(msgspec.Struct):
    rows:           list[_FundsRow]
    total:          int
    earliest_date:  Optional[str]  # 'tracking started X days ago' hint


class FundsBackfillRequest(msgspec.Struct):
    account:   str          # broker account code (e.g. 'DH3747')
    from_date: str          # 'YYYY-MM-DD'
    to_date:   str          # 'YYYY-MM-DD' (inclusive)


class FundsBackfillResponse(msgspec.Struct):
    account:     str
    from_date:   str
    to_date:     str
    rows_added:  int
    rows_skipped:int
    broker_id:   str
    detail:      str       # short status / error string


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
            # GROUP BY status returns a complete histogram of the filtered
            # set; total = sum(counts.values()). Saves a separate COUNT(*)
            # round-trip on every page load.
            counts_q = (
                select(AlgoOrder.status, _func.count(AlgoOrder.id))
                  .where(and_(*conditions))
                  .group_by(AlgoOrder.status)
            )
            counts: dict[str, int] = {}
            for st, c in (await s.execute(counts_q)).all():
                counts[str(st)] = int(c)
            total = sum(counts.values())

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
                    request_id=r.request_id,
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
            # Fold COUNT(*) and SUM(qty*avg_cost) into a single round-trip;
            # the two scalars are computed by the same scan. Saves one
            # round-trip per request without affecting the result shape.
            agg_q = select(
                _func.count(DailyBook.id),
                _func.coalesce(_func.sum(DailyBook.qty * DailyBook.avg_cost), 0.0),
            ).where(and_(*conditions))
            total_int, notional_val = (await s.execute(agg_q)).one()
            total = int(total_int or 0)
            total_notional = float(notional_val or 0.0)

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

        # Run the filtered SELECT and the unfiltered "earliest" probe
        # concurrently across two short-lived sessions. SQLAlchemy async
        # sessions can't multiplex statements; two sessions + gather
        # halves wall-time when the filtered query scans many rows.
        # (Slice M3.)
        async def _fetch_rows():
            async with async_session() as s_rows:
                return (await s_rows.execute(
                    select(DailyBook).where(and_(*conditions))
                      .order_by(desc(DailyBook.date),
                                DailyBook.account, DailyBook.segment)
                )).scalars().all()

        async def _fetch_earliest():
            async with async_session() as s_min:
                return (await s_min.execute(
                    select(_func.min(DailyBook.date)).where(
                        DailyBook.kind == "funds",
                    )
                )).scalar_one()

        rows, earliest = await asyncio.gather(_fetch_rows(), _fetch_earliest())

        # Cashbook lens — compute day-over-day Δ on cash_available
        # within each (account, segment) series. Walk rows sorted by
        # ascending date per series; the response order is reversed
        # back to DESC at the end so the UI still reads top-down
        # newest-first. Single pass; O(N) where N = rows in range.
        from collections import defaultdict
        _series: dict[tuple, list] = defaultdict(list)
        for r in rows:
            _series[(r.account, r.segment)].append(r)
        # Per-series order from DB query is DESC; flip to ASC for the
        # delta walk so prior = previous row's cash.
        delta_by_row_id: dict[int, Optional[float]] = {}
        for (_acct, _seg), series in _series.items():
            series_asc = sorted(series, key=lambda x: x.date or _date.min)
            prior_cash: Optional[float] = None
            for r in series_asc:
                cur = (float(r.avg_cost) if r.avg_cost is not None else None)
                if prior_cash is not None and cur is not None:
                    delta_by_row_id[r.id] = cur - prior_cash
                else:
                    delta_by_row_id[r.id] = None
                if cur is not None:
                    prior_cash = cur

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
                    cash_delta=delta_by_row_id.get(r.id),
                )
                for r in rows
            ],
            total=len(rows),
            earliest_date=(earliest.isoformat() if earliest else None),
        )

    @post("/funds/backfill", guards=[admin_guard])
    async def backfill_funds(self, data: FundsBackfillRequest) -> FundsBackfillResponse:
        """Pull historical funds ledger from the broker and seed
        daily_book[kind='funds'] for the date range. Idempotent on
        the unique key (date, account, kind, symbol) — re-running
        with a wider range OVERWRITES existing rows with the
        canonical broker-ledger numbers (intentional — the voucher-
        aggregated backfill data is more accurate than a single
        broker.margins() snapshot taken at 15:35 IST).

        Admin-only (slice L7): this is a WRITE endpoint that
        clobbers persisted ledger rows. The view_audit cap admits
        risk/ops which is appropriate for reads but not for
        overwriting financial history. Operator
        edits to a backfilled row will be clobbered on the next
        backfill; treat the table as read-only for funds rows.

        Broker support matrix:
        - Kite (zerodha_kite): NO programmatic ledger. Console
          download only. Returns 501 with guidance.
        - Dhan (dhan): has `/v2/statement/ledger` REST endpoint.
          Adapter wiring is a follow-up — endpoint structure is in
          place + returns 501 until the broker adapter implements
          `funds_ledger(from_date, to_date)`.
        - Groww: unknown SDK support.

        Operator workflow once the adapter lands:
        1. /admin/history Funds tab → click Backfill on a Dhan
           row → date picker → fires this endpoint.
        2. Endpoint pulls ledger entries from broker, maps each
           date to a `daily_book` row, INSERT ... ON CONFLICT DO
           NOTHING.
        3. Funds tab re-fetches → historical data appears.
        """
        from datetime import date as _d
        from backend.shared.helpers.connections import Connections
        from backend.shared.brokers.registry import get_broker
        from backend.shared.helpers.utils import mask_account

        account = (data.account or "").strip()
        if not account:
            raise HTTPException(status_code=400, detail="account is required")
        try:
            df = _d.fromisoformat(data.from_date)
            dt = _d.fromisoformat(data.to_date)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="from_date / to_date must be YYYY-MM-DD",
            )
        if df > dt:
            raise HTTPException(
                status_code=400,
                detail="from_date must be <= to_date",
            )

        conns = Connections()
        if account not in conns.conn:
            raise HTTPException(status_code=404,
                                detail=f"account {mask_account(account)} not loaded")

        broker = get_broker(account)
        broker_id = getattr(broker, "broker_id", "unknown")

        # Adapter contract: optional `funds_ledger(from_date, to_date)`
        # method returning a list of normalised dicts (see
        # backend/shared/brokers/dhan.py::DhanBroker.funds_ledger
        # for the canonical shape). Kite has no programmatic ledger;
        # Groww adapter wiring is still pending. Both 501 here.
        if not hasattr(broker, "funds_ledger"):
            raise HTTPException(
                status_code=501,
                detail=(
                    f"Funds backfill not implemented for broker "
                    f"'{broker_id}'. Kite has no programmatic ledger "
                    f"(Zerodha Console download only). Groww adapter "
                    f"support is pending."
                ),
            )

        # Pull the ledger via the broker adapter. Sync call; offload
        # to the executor so the route stays async-clean.
        import asyncio
        loop = asyncio.get_running_loop()
        try:
            entries = await loop.run_in_executor(
                None,
                lambda: broker.funds_ledger(df.isoformat(), dt.isoformat()),
            )
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail=f"Ledger fetch failed: {e}",
            )
        if not isinstance(entries, list):
            entries = []

        # Idempotent upsert into daily_book[kind='funds']. Re-use the
        # same column mapping as the live snapshot path so the Funds
        # tab reads back uniformly:
        #   qty       = debits
        #   avg_cost  = cash_available
        #   ltp       = opening_balance
        #   day_pnl   = realised_m2m
        #   total_pnl = net
        # ON CONFLICT clause matches the existing unique constraint
        # (date, account, kind, symbol) — backfill DOES overwrite
        # any prior row for the same (date, account, segment) so an
        # operator re-running with a wider date range gets the
        # canonical Dhan numbers rather than a stale partial.
        from sqlalchemy import text as _text
        import json as _json
        added = 0
        skipped = 0
        now_utc = datetime.now(timezone.utc)
        async with async_session() as s:
            for e in entries:
                seg = (e.get("segment") or "equity").lower()
                params = {
                    "date":         e.get("date"),
                    "account":      account,
                    "segment":      seg,
                    "kind":         "funds",
                    "symbol":       "__seg__",
                    "exchange":     seg.upper(),
                    "qty":          int(e.get("debits") or 0),
                    "avg_cost":     e.get("cash_available"),
                    "ltp":          e.get("opening_balance"),
                    "day_pnl":      e.get("realised_m2m"),
                    "total_pnl":    e.get("net"),
                    "payload_json": _json.dumps(e.get("payload") or {}, default=str),
                    "captured_at":  now_utc,
                }
                try:
                    await s.execute(_text("""
                        INSERT INTO daily_book
                            (date, account, segment, kind, symbol, exchange,
                             qty, avg_cost, ltp, day_pnl, total_pnl,
                             payload_json, captured_at)
                        VALUES
                            (:date, :account, :segment, :kind, :symbol, :exchange,
                             :qty, :avg_cost, :ltp, :day_pnl, :total_pnl,
                             :payload_json, :captured_at)
                        ON CONFLICT (date, account, kind, symbol) DO UPDATE SET
                            segment      = EXCLUDED.segment,
                            exchange     = EXCLUDED.exchange,
                            qty          = EXCLUDED.qty,
                            avg_cost     = EXCLUDED.avg_cost,
                            ltp          = EXCLUDED.ltp,
                            day_pnl      = EXCLUDED.day_pnl,
                            total_pnl    = EXCLUDED.total_pnl,
                            payload_json = EXCLUDED.payload_json,
                            captured_at  = EXCLUDED.captured_at
                    """), params)
                    added += 1
                except Exception as _row_err:
                    skipped += 1
                    logger.debug(f"backfill skip row {e!r}: {_row_err}")
            await s.commit()

        return FundsBackfillResponse(
            account=account,
            from_date=df.isoformat(), to_date=dt.isoformat(),
            rows_added=added, rows_skipped=skipped, broker_id=broker_id,
            detail=(f"{added} rows upserted from {broker_id} ledger"
                    if added else "no ledger entries in range"),
        )
