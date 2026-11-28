"""
History — multi-day forensic surface for orders, trades, funds, and the
full Kite Console backfill (raw tradebook, P&L, and funds ledger).

GET /api/admin/history/orders       — AlgoOrder table, date + account + symbol + status filters
GET /api/admin/history/trades       — daily_book where kind='trades'
GET /api/admin/history/funds        — daily_book where kind='funds' (per-account ledger)
GET /api/admin/history/realized-pnl — Kite Console P&L reconciliation ground truth
GET /api/admin/history/tradebook    — kite_console_pnl_sync where report_type='tradebook'
GET /api/admin/history/ledger       — kite_console_pnl_sync where report_type='ledger'
GET /api/admin/history/pnl-summary  — kite_console_pnl_sync pnl rows, grouped by
                                       (symbol_root, segment) win/loss aggregate

All seven are cap-gated by `view_audit` — same gate as the audit log;
designated / risk / admin only (broader than "admin-only" in the strict
sense — intentional, matching this file's existing convention for a
read-only surface).

Operator workflow: pick a date range + accounts + status pill, scan
the table, drill via the Audit log for the request_id of any row that
looks suspicious. `realized-pnl`/`tradebook`/`ledger`/`pnl-summary` are
lookup surfaces over the Kite Console backfill/sync's own accumulated
ground truth (see `scripts/kite_console_pnl_sync.py`) — a genuine
reconciliation disagreement reaches the operator separately, through
the standard alert pipeline (`event_agents.PNL_RECONCILIATION_AGENT`),
not through these endpoints.

`tradebook` is the raw per-fill record (one row per Kite trade_id) —
distinct from `trades` above, which is a daily_book settlement
snapshot (one row per account+symbol+day), not the Console tradebook.
`ledger` is the raw Funds Statement voucher feed (margin block/
reversal, net-obligation, etc.) — distinct from `funds` above, which
is a daily_book cash snapshot, not the Console ledger. `pnl-summary`
is a lightweight win/loss aggregate over `realized-pnl`'s own
underlying data, intended as a first-pass input for pattern analysis
(which symbols/segments have been consistently profitable) — it does
NOT do any backtesting or pattern mining itself.

`broker` filter, all four Console-backed endpoints: `kite_console_pnl_sync`
does not yet carry a `broker` column (it is being added concurrently,
by a different change, default 'kite' for existing rows) — see each
endpoint's own docstring for exactly how `broker` degrades until that
column lands. `tradebook`/`ledger` query the table directly and use
`getattr()` so the filter engages for real the moment the column
exists, with no second pass needed here. `realized-pnl`/`pnl-summary`
go through `scripts.kite_console_pnl_sync.get_accumulated_pnl()`,
which has no broker dimension in its grouped output — `broker='kite'`
(or omitted) is a no-op there (every row today is Kite-sourced), and
any other value returns an empty result, until that function itself
gains a `broker` kwarg.
"""

from __future__ import annotations

import asyncio
from datetime import date as _date, datetime, timedelta, timezone
from typing import Optional

import msgspec
from litestar import Controller, get, post
from litestar.exceptions import HTTPException
from sqlalchemy import select, and_, or_ as _sa_or, desc, false as _sa_false, func as _func

from backend.api.database import async_session
from backend.api.models import AlgoOrder, DailyBook, KiteConsolePnlSync
from backend.api.auth_guard import admin_guard
from backend.api.rbac import cap_guard
from backend.shared.helpers.settings import get_int
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


class _RealizedPnlRow(msgspec.Struct):
    account:         str              # masked form, e.g. 'Z1####'
    segment:         str              # 'equity' | 'derivatives' | 'commodities'
    instrument_type: str              # 'equity' | 'future' | 'option'
    symbol_root:     str
    expiry:          Optional[str]    # ISO date, or null for equity
    realized_pnl:    float


class _TradebookRow(msgspec.Struct):
    """One raw Kite Console tradebook fill — `kite_console_pnl_sync`
    where report_type='tradebook'. `expiry` is the typed column (a
    last-Thursday-of-month approximation for MCX options — see
    `KiteConsolePnlSync`'s own model docstring); `console_expiry` is
    the raw Console-reported `expiry_date`, which is the more accurate
    value for MCX (confirmed live: CRUDEOIL/SILVERM options' own
    `expiry_date` lands several days before the typed approximation).
    `price`/`trade_type`/`order_execution_time` come from `raw_row` —
    not typed columns, surfaced here because pattern analysis over
    individual fills needs them."""
    account:               str               # masked form, e.g. 'Z1####'
    broker:                str               # 'kite' until the broker column lands
    segment:               str               # 'equity' | 'derivatives' | 'commodities'
    symbol:                Optional[str]
    exchange:              Optional[str]
    instrument_type:       Optional[str]     # 'equity' | 'future' | 'option'
    symbol_root:           Optional[str]
    expiry:                Optional[str]     # typed column, ISO date — see docstring above
    console_expiry:        Optional[str]     # raw Console expiry_date, ISO-ish string
    trade_date:            Optional[str]
    trade_id:              Optional[str]
    order_id:              Optional[str]
    buy_qty:               Optional[float]
    sell_qty:              Optional[float]
    buy_value:             Optional[float]
    sell_value:            Optional[float]
    price:                 Optional[float]   # raw per-fill price
    trade_type:            Optional[str]     # 'buy' | 'sell', raw
    order_execution_time:  Optional[str]     # raw timestamp string


class TradebookListResponse(msgspec.Struct):
    rows:    list[_TradebookRow]
    total:   int
    limit:   int
    offset:  int


class _LedgerRow(msgspec.Struct):
    """One raw Kite Console Funds Statement voucher —
    `kite_console_pnl_sync` where report_type='ledger'. The typed
    `segment` DB column is always the literal 'all' for ledger rows
    (Zerodha's Funds Statement has no per-row segment of its own —
    one shared margin pool across every segment) — `segment` on THIS
    row is instead DERIVED from `raw_row.cost_center` via
    `_ledger_segment_from_cost_center()`; `None` when the cost_center
    is missing or unrecognised, never a guessed value.

    `net_balance` is an account-wide RUNNING total — it is only
    meaningful read across the FULL, unfiltered series for an account;
    a `segment`-narrowed view still shows each row's own real
    `net_balance`, which will jump around once other-segment rows are
    excluded. This is not computed differently per filter; it is the
    raw Console-reported number either way."""
    account:       str               # masked form, e.g. 'Z1####'
    broker:        str               # 'kite' until the broker column lands
    posting_date:  Optional[str]     # == trade_date column; Console's voucher/posting date
    cost_center:   Optional[str]     # raw, e.g. 'MCX-F&O - Z'
    segment:       Optional[str]     # derived from cost_center — see docstring
    voucher_type:  Optional[str]
    particulars:   Optional[str]
    debit:         Optional[float]
    credit:        Optional[float]
    net_balance:   Optional[float]


class LedgerListResponse(msgspec.Struct):
    rows:    list[_LedgerRow]
    total:   int
    limit:   int
    offset:  int


class _PnlSummaryRow(msgspec.Struct):
    """One (symbol_root, segment) bucket, aggregated over
    `get_accumulated_pnl()`'s own already-deduped groups (see that
    function's docstring for how a re-pulled calendar month is
    prevented from double-counting). `trade_count` counts CONTRACT-
    EXPIRY CYCLES (e.g. "GOLDM 26APR140000CE expiring 2026-04-24" is
    one count, however many months of Console pulls covered its
    life) — not raw fills; use `/tradebook` for a fill-level count.
    A cycle with realized_pnl exactly 0 is neither a win nor a loss —
    it is counted only in `flat_count`, folded into `trade_count` but
    excluded from `win_count`/`loss_count`/`avg_win`/`avg_loss`.
    `avg_win`/`avg_loss` are `None` (never 0) when there are no wins/
    losses in the bucket."""
    symbol_root: str
    segment:     str
    trade_count: int
    win_count:   int
    loss_count:  int
    flat_count:  int
    total_pnl:   float
    win_rate:    float              # win_count / trade_count; 0.0 when trade_count == 0
    avg_win:     Optional[float]
    avg_loss:    Optional[float]


class PnlSummaryResponse(msgspec.Struct):
    rows:  list[_PnlSummaryRow]
    total: int


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


def _hist_date_window(
    from_date: Optional[str],
    to_date: Optional[str],
    default_days: int = 30,
) -> tuple[_date, _date]:
    """Parse from_date / to_date strings; fall back to the last
    `default_days` window when either is missing or unparseable."""
    df = _parse_date(from_date)
    dt = _parse_date(to_date)
    if not df or not dt:
        return _default_range(default_days)
    return df, dt


def _hist_order_row(r: "AlgoOrder") -> "_OrderRow":
    """Build an _OrderRow from an AlgoOrder ORM row."""
    return _OrderRow(
        id=r.id,
        created_at=r.created_at.isoformat() if r.created_at else "",
        account=r.account,
        symbol=r.symbol,
        exchange=r.exchange,
        transaction_type=r.transaction_type,
        quantity=int(r.quantity or 0),
        filled_quantity=int(r.filled_quantity or 0),
        initial_price=(float(r.initial_price) if r.initial_price is not None else None),
        fill_price=(float(r.fill_price) if r.fill_price is not None else None),
        slippage=(float(r.slippage) if r.slippage is not None else None),
        status=r.status,
        mode=r.mode,
        engine=r.engine,
        broker_order_id=r.broker_order_id,
        request_id=r.request_id,
        detail=r.detail,
    )


def _hist_build_order_conditions(
    from_dt: datetime,
    to_dt: datetime,
    accounts: Optional[str],
    symbols: Optional[str],
    status: Optional[str],
    mode: Optional[str],
) -> list:
    """Build the SQLAlchemy WHERE conditions list for list_orders."""
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
    return conditions


def _hist_trade_row(r: "DailyBook") -> "_TradeRow":
    """Build a _TradeRow from a DailyBook ORM row."""
    return _TradeRow(
        date=r.date.isoformat() if r.date else "",
        account=r.account,
        segment=r.segment,
        symbol=r.symbol,
        exchange=r.exchange,
        qty=int(r.qty or 0),
        avg_cost=(float(r.avg_cost) if r.avg_cost is not None else None),
        notional=(float(r.qty) * float(r.avg_cost)
                  if r.qty is not None and r.avg_cost is not None else None),
        captured_at=r.captured_at.isoformat() if r.captured_at else "",
    )


def _hist_funds_delta_map(rows: list) -> "dict[int, Optional[float]]":
    """Compute day-over-day cash_available delta per (account, segment) series.

    Walks rows in ascending date order within each series; returns a
    mapping of row.id → delta (None for the first row in each series or
    when the prior row's cash is None).
    """
    from collections import defaultdict
    _series: dict[tuple, list] = defaultdict(list)
    for r in rows:
        _series[(r.account, r.segment)].append(r)
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
    return delta_by_row_id


def _hist_funds_row(r: "DailyBook", delta_by_row_id: "dict[int, Optional[float]]") -> "_FundsRow":
    """Build a _FundsRow from a DailyBook ORM row plus pre-computed delta map."""
    return _FundsRow(
        date=r.date.isoformat() if r.date else "",
        account=r.account,
        segment=r.segment,
        cash_available=(float(r.avg_cost) if r.avg_cost is not None else None),
        opening_balance=(float(r.ltp) if r.ltp is not None else None),
        debits_today=int(r.qty or 0),
        realised_m2m=(float(r.day_pnl) if r.day_pnl is not None else None),
        net=(float(r.total_pnl) if r.total_pnl is not None else None),
        cash_delta=delta_by_row_id.get(r.id),
    )


def _hist_backfill_validate(data: "FundsBackfillRequest") -> "tuple[_date, _date]":
    """Validate backfill request dates. Raises HTTPException on bad input.

    Returns (from_date, to_date) as datetime.date objects.
    """
    from datetime import date as _d
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
    return df, dt


def _hist_resolve_loaded_accounts() -> "set[str]":
    """Return the set of currently-loaded broker account codes.

    When conn_service owns sessions the local Connections map is empty;
    consult the remote accounts list in that case.
    """
    from backend.brokers.connections import Connections
    from backend.brokers.client import is_cutover_on
    conns = Connections()
    loaded = set(conns.conn.keys())
    if is_cutover_on() and not loaded:
        from backend.brokers.client.remote_broker import list_remote_accounts
        loaded = {r["account"] for r in list_remote_accounts() if r.get("account")}
    return loaded


async def _hist_fetch_ledger_entries(
    broker, df: "_date", dt: "_date", broker_id: str
) -> list:
    """Call broker.funds_ledger() in the executor; raises HTTPException on failure.

    Returns a (possibly-empty) list of normalised ledger entry dicts.
    """
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
    return entries if isinstance(entries, list) else []


async def _hist_upsert_entry(s, account: str, e: dict, now_utc: datetime) -> None:
    """Upsert a single ledger entry dict into daily_book[kind='funds'].

    Raises on insert error (caller should catch + increment skipped counter).
    """
    from sqlalchemy import text as _text
    import json as _json
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


def _hist_realized_pnl_account_candidates(account: Optional[str]) -> "Optional[set[str]]":
    """Returns the set of strings that should be treated as matching a
    caller-supplied `account` filter, or `None` for "no filter".

    Double-mask guard: `KiteConsolePnlSync.account` (and therefore every
    key `get_accumulated_pnl()` returns) stores the MASKED form — but
    this endpoint has no way to know whether the caller passed a raw
    broker account code or the already-masked form it itself returns in
    every row (a round-tripped display value). `mask_account()` is NOT
    idempotent — re-masking an already-masked code (e.g. 'Z1####')
    produces a DIFFERENT, wrong string ('Z#####') — so this matches
    against BOTH the literal param and its `mask_account()`-translated
    form, never re-masking blindly. `mask_account()` itself is wrapped
    in try/except: it's a process-local registry lookup
    (`register_accounts()`), and a failure there must degrade to
    exact-string matching, never raise."""
    if not account:
        return None
    candidates = {account}
    try:
        from backend.shared.helpers.utils import mask_account
        candidates.add(mask_account(account))
    except Exception:
        pass
    return candidates


def _hist_pnl_group_matches_filters(
    acct: str, seg: str, exp: Optional[_date], *,
    account_candidates: "Optional[set[str]]",
    segment: Optional[str],
    expiry_from: Optional[_date],
    expiry_to: Optional[_date],
) -> bool:
    """Shared group-level filter predicate, used by BOTH
    `_hist_realized_pnl_rows()` (flat listing) and
    `_hist_pnl_summary_rows()` (win/loss aggregate) over
    `get_accumulated_pnl()`'s own `groups` dict — extracted so the two
    surfaces can never silently disagree on which groups a given
    account/segment/expiry-range filter admits.

    A null-expiry (equity) group is excluded whenever `expiry_from` or
    `expiry_to` is set — a range filter implies "I'm asking about a
    specific expiry window", which an equity row has no expiry to
    compare against. With neither bound set, an equity row passes."""
    if account_candidates is not None and acct not in account_candidates:
        return False
    if segment and seg != segment:
        return False
    if expiry_from or expiry_to:
        if exp is None:
            return False
        if expiry_from and exp < expiry_from:
            return False
        if expiry_to and exp > expiry_to:
            return False
    return True


def _hist_realized_pnl_rows(
    result: dict, *,
    account: Optional[str] = None,
    segment: Optional[str] = None,
    expiry_from: Optional[_date] = None,
    expiry_to: Optional[_date] = None,
) -> "list[_RealizedPnlRow]":
    """Pure flattener/filter — reshapes
    `scripts.kite_console_pnl_sync.get_accumulated_pnl()`'s own
    `groups: {(account, segment, instrument_type, symbol_root, expiry): pnl}`
    dict into this endpoint's flat row-list contract, sorted by
    `expiry` descending (most recent cycle first; a null expiry, i.e.
    an equity row, always sorts last).

    `account`/`segment`/`expiry_from`/`expiry_to` are applied HERE, in
    Python, over the already-fetched `groups` — `get_accumulated_pnl()`
    itself has no `segment` or expiry-RANGE kwarg (only an exact-match
    `expiry`), and its own `account` kwarg would double-mask an already-
    masked caller value (see `_hist_realized_pnl_account_candidates()`),
    so this endpoint never passes `account=` to it either."""
    account_candidates = _hist_realized_pnl_account_candidates(account)

    rows: "list[_RealizedPnlRow]" = []
    for (acct, seg, itype, root, exp), pnl in (result.get("groups") or {}).items():
        if not _hist_pnl_group_matches_filters(
            acct, seg, exp, account_candidates=account_candidates,
            segment=segment, expiry_from=expiry_from, expiry_to=expiry_to,
        ):
            continue
        rows.append(_RealizedPnlRow(
            account=acct, segment=seg, instrument_type=itype, symbol_root=root,
            expiry=(exp.isoformat() if exp else None),
            realized_pnl=float(pnl or 0.0),
        ))

    # Descending by expiry; a null expiry ('') sorts last under
    # reverse=True since '' < any real ISO date string. account/
    # symbol_root as a deterministic tiebreak for equal/null expiries —
    # `sorted()` raises on a bare `date | None` mix, so every key here
    # is string-shaped.
    rows.sort(key=lambda r: (r.expiry or "", r.account, r.symbol_root), reverse=True)
    return rows


def _hist_pnl_summary_rows(
    result: dict, *,
    account: Optional[str] = None,
    segment: Optional[str] = None,
    expiry_from: Optional[_date] = None,
    expiry_to: Optional[_date] = None,
) -> "list[_PnlSummaryRow]":
    """Groups `get_accumulated_pnl()`'s own already-deduped `groups`
    dict — keyed by (account, segment, instrument_type, symbol_root,
    expiry), each value already the correctly-summed total across any
    re-pulled calendar months for that one contract-expiry cycle (see
    `get_accumulated_pnl()`'s own docstring for why a bare re-sum would
    double-count) — by (symbol_root, segment) ONLY, collapsing account,
    instrument_type and expiry. Filtering uses the identical predicate
    `_hist_realized_pnl_rows()` uses, so the two surfaces can never
    disagree about which underlying contract-cycles are in scope.

    Each bucketed value is one CONTRACT-EXPIRY CYCLE's realized P&L
    (e.g. "GOLDM 26APR140000CE expiring 2026-04-24", summed across
    however many months of Console pulls covered its life) — NOT a
    single fill. `trade_count` therefore counts cycles; use
    `/tradebook` for a fill-level count. A cycle whose realized_pnl is
    exactly 0 is counted in `flat_count` only — not a win, not a loss.

    Sorted by `total_pnl` descending (most profitable bucket first) —
    the natural reading order for "which symbols/segments have been
    consistently profitable"."""
    account_candidates = _hist_realized_pnl_account_candidates(account)
    buckets: "dict[tuple[str, str], list[float]]" = {}
    for (acct, seg, _itype, root, exp), pnl in (result.get("groups") or {}).items():
        if not _hist_pnl_group_matches_filters(
            acct, seg, exp, account_candidates=account_candidates,
            segment=segment, expiry_from=expiry_from, expiry_to=expiry_to,
        ):
            continue
        buckets.setdefault((root, seg), []).append(float(pnl or 0.0))

    rows: "list[_PnlSummaryRow]" = []
    for (root, seg), pnls in buckets.items():
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        flat = [p for p in pnls if p == 0]
        trade_count = len(pnls)
        win_count = len(wins)
        loss_count = len(losses)
        rows.append(_PnlSummaryRow(
            symbol_root=root, segment=seg,
            trade_count=trade_count, win_count=win_count,
            loss_count=loss_count, flat_count=len(flat),
            total_pnl=sum(pnls),
            win_rate=(win_count / trade_count) if trade_count else 0.0,
            avg_win=(sum(wins) / win_count) if win_count else None,
            avg_loss=(sum(losses) / loss_count) if loss_count else None,
        ))

    rows.sort(key=lambda r: (-r.total_pnl, r.symbol_root, r.segment))
    return rows


def _hist_broker_param_matches_all(broker: Optional[str]) -> bool:
    """True when `broker` doesn't need to narrow a `get_accumulated_pnl()`-
    backed result — that function's grouped output carries no per-row
    broker dimension (see this module's own docstring). Until it gains
    a `broker` kwarg, 'kite' (or omitted) matches every row (every row
    ingested so far is Kite-sourced); any other value matches none."""
    return not broker or broker.strip().lower() == "kite"


def _safe_float(v) -> Optional[float]:
    """Parses a `raw_row` JSONB numeric-looking string (e.g.
    '1416190.500000') into a float. An absent/empty/unparseable value
    is `None`, never a coerced `0.0` — same missing-vs-zero convention
    as every other margin/funds field in this codebase."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _hist_console_account_condition(column, account: Optional[str]):
    """SQLAlchemy WHERE condition for an `account` filter against a
    masked `KiteConsolePnlSync.account`-shaped column — reuses the same
    double-mask-safe candidate matching as
    `_hist_realized_pnl_account_candidates()`. Returns `None` for "no
    filter" (never an always-true condition, so callers can `if cond
    is not None: conditions.append(cond)`)."""
    candidates = _hist_realized_pnl_account_candidates(account)
    if candidates is None:
        return None
    return column.in_(candidates)


def _hist_console_broker_value(r) -> str:
    """Per-row broker identifier for a `KiteConsolePnlSync` row. The
    `broker` column is being added concurrently (see this module's own
    docstring) — `getattr()` degrades to the literal 'kite' default,
    correct for every row ingested before that column exists (the Kite
    Console scraper is the only producer so far). Once the column
    lands, the real stored value is read with no code change here."""
    return getattr(r, "broker", None) or "kite"


def _hist_console_broker_condition(broker: Optional[str]):
    """SQLAlchemy WHERE condition for an optional `broker` filter
    against `KiteConsolePnlSync` directly (the `/tradebook` and
    `/ledger` routes, which query the table themselves rather than
    going through `get_accumulated_pnl()`).

    While the `broker` column doesn't exist yet (`getattr()` returns
    `None`): 'kite' (or omitted) is a no-op — every existing row is
    Kite-sourced — any other value returns a condition matching
    nothing, via `sqlalchemy.false()` (never silently ignored as if it
    matched everything). Once the column lands, this filters on it for
    real with no code change needed here.

    Returns `None` for "no filter" (the same convention as
    `_hist_console_account_condition()`)."""
    if not broker:
        return None
    col = getattr(KiteConsolePnlSync, "broker", None)
    if col is None:
        return None if broker.strip().lower() == "kite" else _sa_false()
    return col == broker.strip().lower()


_LEDGER_COST_CENTER_SEGMENT_MAP: "dict[str, str]" = {
    "NSE-EQ":   "equity",
    "BSE-EQ":   "equity",
    "NSE-F&O":  "derivatives",
    "BSE-F&O":  "derivatives",
    "MCX-F&O":  "commodities",
}


def _ledger_segment_from_cost_center(cost_center: Optional[str]) -> Optional[str]:
    """Maps a Kite Console ledger `cost_center` label to this app's
    segment vocabulary ('equity' | 'derivatives' | 'commodities').
    Confirmed-live values on prod: 'NSE-EQ - Z', 'NSE-F&O - Z',
    'MCX-F&O - Z' (the ' - Z' suffix is a fixed Zerodha-side label,
    not account-specific — observed identical across both of the two
    live accounts). The typed `KiteConsolePnlSync.segment` DB column is
    always the literal 'all' for ledger rows, so a `segment` filter on
    the `/ledger` endpoint is resolved against THIS derived value, not
    the stored column. An unrecognised or missing cost_center returns
    `None` — never a guessed segment.

    `_hist_ledger_segment_condition()` below is the SQL-pushdown
    equivalent of this exact mapping (same prefixes, same case-folding)
    — the two must never silently disagree on which cost_center labels
    belong to which segment; see that function's own docstring."""
    if not cost_center:
        return None
    prefix = cost_center.split(" - ")[0].strip().upper()
    return _LEDGER_COST_CENTER_SEGMENT_MAP.get(prefix)


def _hist_ledger_segment_prefixes(segment: str) -> "list[str]":
    """Every cost_center prefix mapping to `segment` — built by
    INVERTING `_LEDGER_COST_CENTER_SEGMENT_MAP`, never a second,
    independently hand-typed list, so this can never silently drift
    from `_ledger_segment_from_cost_center()`'s own mapping."""
    seg = (segment or "").strip().lower()
    return [prefix for prefix, s in _LEDGER_COST_CENTER_SEGMENT_MAP.items() if s == seg]


def _hist_ledger_segment_condition(segment: Optional[str]):
    """SQL-pushdown WHERE condition for `/ledger`'s derived `segment`
    filter, so the DB does the filtering instead of a Python fetch-
    everything-then-filter pass (P2 fix — the derived value lives in
    `raw_row->>'cost_center'`, not an indexed column, so this can't be
    a plain `column == value` equality the way `/tradebook`'s own
    `segment` filter is).

    `KiteConsolePnlSync.raw_row['cost_center'].astext` compiles to the
    dialect's native JSON path-extraction operator (`->>'` on both
    Postgres/JSONB and SQLite 3.38+'s JSON1 extension — confirmed
    working against both dialects, including the hand-built sqlite
    fixture `console_session_factory` already uses for this file's own
    tests), so this is portable with no dialect branch needed.

    Matches on `'{prefix} - %'` (case-insensitively, via `.ilike()`,
    which SQLAlchemy compiles portably — `ILIKE` on Postgres,
    `lower(...) LIKE lower(...)` elsewhere) — the exact Zerodha label
    shape `_ledger_segment_from_cost_center()` parses (`cost_center.
    split(' - ')[0]`), never a bare substring/contains match that could
    false-positive on an unrelated cost_center sharing the same prefix
    text with no ' - ' separator.

    Returns `None` for "no filter" (same convention as every other
    `_hist_*_condition()` helper in this module). An unrecognised
    segment value (no matching prefixes) returns `sqlalchemy.false()`
    — matches nothing, never silently treated as "no filter"."""
    if not segment:
        return None
    prefixes = _hist_ledger_segment_prefixes(segment)
    if not prefixes:
        return _sa_false()
    extracted = KiteConsolePnlSync.raw_row["cost_center"].astext
    return _sa_or(*[extracted.ilike(f"{p} - %") for p in prefixes])


def _hist_tradebook_row(r: "KiteConsolePnlSync") -> "_TradebookRow":
    """Builds a `_TradebookRow` from a `KiteConsolePnlSync` ORM row
    (report_type='tradebook'). `raw_row` fields are surfaced for the
    ones no typed column carries — see `_TradebookRow`'s own
    docstring for why `console_expiry` differs from `expiry`."""
    raw = r.raw_row or {}
    console_expiry = raw.get("expiry_date")
    return _TradebookRow(
        account=r.account,
        broker=_hist_console_broker_value(r),
        segment=r.segment,
        symbol=r.symbol,
        exchange=r.exchange,
        instrument_type=r.instrument_type,
        symbol_root=r.symbol_root,
        expiry=(r.expiry.isoformat() if r.expiry else None),
        console_expiry=(str(console_expiry) if console_expiry else None),
        trade_date=(r.trade_date.isoformat() if r.trade_date else None),
        trade_id=r.trade_id,
        order_id=r.order_id,
        buy_qty=(float(r.buy_qty) if r.buy_qty is not None else None),
        sell_qty=(float(r.sell_qty) if r.sell_qty is not None else None),
        buy_value=(float(r.buy_value) if r.buy_value is not None else None),
        sell_value=(float(r.sell_value) if r.sell_value is not None else None),
        price=_safe_float(raw.get("price")),
        trade_type=raw.get("trade_type"),
        order_execution_time=raw.get("order_execution_time"),
    )


def _hist_ledger_row(r: "KiteConsolePnlSync") -> "_LedgerRow":
    """Builds a `_LedgerRow` from a `KiteConsolePnlSync` ORM row
    (report_type='ledger'). Every displayed field but `account`/
    `broker`/`posting_date` comes from `raw_row` — see that model's
    own docstring (`KiteConsolePnlSync`, 2026-11-26 ledger addition)
    for why ledger rows carry no typed symbol/exchange/qty columns."""
    raw = r.raw_row or {}
    cost_center = raw.get("cost_center")
    return _LedgerRow(
        account=r.account,
        broker=_hist_console_broker_value(r),
        posting_date=(r.trade_date.isoformat() if r.trade_date else None),
        cost_center=cost_center,
        segment=_ledger_segment_from_cost_center(cost_center),
        voucher_type=raw.get("voucher_type"),
        particulars=raw.get("particulars"),
        debit=_safe_float(raw.get("debit")),
        credit=_safe_float(raw.get("credit")),
        net_balance=_safe_float(raw.get("net_balance")),
    )


def _hist_build_console_conditions(
    report_type: str, *,
    from_date: Optional[_date],
    to_date: Optional[_date],
    account: Optional[str],
    broker: Optional[str],
) -> list:
    """Shared WHERE-condition builder for `/tradebook` and `/ledger` —
    both filter on `report_type`, an optional `trade_date` range (NO
    default window — this is a full-history surface, unlike `/trades`/
    `/orders`/`/funds`, which default to a recent window), an optional
    masked-account match, and an optional broker match."""
    conditions: list = [KiteConsolePnlSync.report_type == report_type]
    if from_date:
        conditions.append(KiteConsolePnlSync.trade_date >= from_date)
    if to_date:
        conditions.append(KiteConsolePnlSync.trade_date <= to_date)
    acct_cond = _hist_console_account_condition(KiteConsolePnlSync.account, account)
    if acct_cond is not None:
        conditions.append(acct_cond)
    broker_cond = _hist_console_broker_condition(broker)
    if broker_cond is not None:
        conditions.append(broker_cond)
    return conditions


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
        df, dt = _hist_date_window(from_date, to_date, default_days=30)
        # Convert to UTC datetimes spanning the inclusive IST range.
        # Approximation — date filter doesn't have to be ms-precise.
        from_dt = datetime.combine(df, datetime.min.time(), tzinfo=timezone.utc)
        to_dt   = datetime.combine(dt + timedelta(days=1),
                                   datetime.min.time(), tzinfo=timezone.utc)

        conditions = _hist_build_order_conditions(
            from_dt, to_dt, accounts, symbols, status, mode,
        )

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
            rows=[_hist_order_row(r) for r in rows],
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
        df, dt = _hist_date_window(from_date, to_date, default_days=30)

        conditions: list = [
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
            rows=[_hist_trade_row(r) for r in rows],
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
        df, dt = _hist_date_window(from_date, to_date, default_days=90)

        conditions: list = [
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
            hard_cap = get_int("retention.list_funds_hard_cap", 1000)
            async with async_session() as s_rows:
                result = (await s_rows.execute(
                    select(DailyBook).where(and_(*conditions))
                      .order_by(desc(DailyBook.date),
                                DailyBook.account, DailyBook.segment)
                      .limit(hard_cap + 1)  # fetch one extra to detect truncation
                )).scalars().all()
            if len(result) > hard_cap:
                logger.warning(
                    "list_funds: response truncated at %d rows (hard_cap=%d); "
                    "operator should narrow the date range.",
                    hard_cap, hard_cap,
                )
                result = result[:hard_cap]
            return result

        async def _fetch_earliest():
            async with async_session() as s_min:
                return (await s_min.execute(
                    select(_func.min(DailyBook.date)).where(
                        DailyBook.kind == "funds",
                    )
                )).scalar_one()

        rows, earliest = await asyncio.gather(_fetch_rows(), _fetch_earliest())

        # Cashbook lens — compute day-over-day Δ on cash_available
        # within each (account, segment) series.
        delta_by_row_id = _hist_funds_delta_map(rows)

        return FundsListResponse(
            rows=[_hist_funds_row(r, delta_by_row_id) for r in rows],
            total=len(rows),
            earliest_date=(earliest.isoformat() if earliest else None),
        )

    @get("/realized-pnl", guards=[cap_guard("view_audit")])
    async def list_realized_pnl(
        self,
        account:         Optional[str] = None,
        segment:         Optional[str] = None,
        instrument_type: Optional[str] = None,
        symbol_root:     Optional[str] = None,
        expiry_from:     Optional[str] = None,
        expiry_to:       Optional[str] = None,
        broker:          Optional[str] = None,
    ) -> "list[_RealizedPnlRow]":
        """Kite Console P&L reconciliation ground truth — wraps
        `scripts.kite_console_pnl_sync.get_accumulated_pnl()`. A bare
        list of rows (not the `{rows, total, ...}` wrapper shape the
        other three endpoints in this file use) — this surface has no
        pagination need today (the underlying table is grouped/summed
        server-side, not a raw per-trade feed).

        `account`/`instrument_type`/`symbol_root` map onto the
        underlying function's own filter kwargs. `segment` and an
        expiry RANGE (`expiry_from`/`expiry_to`, inclusive, 'YYYY-MM-DD')
        have no equivalent there, so they're applied in Python over its
        already-grouped output — see `_hist_realized_pnl_rows()`.
        `account` is deliberately NEVER forwarded to
        `get_accumulated_pnl()` itself (which would double-mask an
        already-masked caller value); it's matched post-hoc against
        both the raw and `mask_account()`-translated form instead.

        `broker`: `get_accumulated_pnl()`'s grouped output carries no
        broker dimension today (see this module's own docstring) —
        'kite' (or omitted) is a no-op (every row is Kite-sourced so
        far); any other value returns an empty list. This param exists
        now so no second pass is needed once that function gains real
        broker support.

        Sorted by `expiry` descending by default (most recent cycle
        first); a null expiry (equity) sorts last.

        Read-only — a genuine reconciliation disagreement reaches the
        operator separately, through the Alerts tab
        (`event_agents.PNL_RECONCILIATION_AGENT`), not through this
        endpoint."""
        if broker is not None and not _hist_broker_param_matches_all(broker):
            return []
        from scripts.kite_console_pnl_sync import get_accumulated_pnl

        ef = _parse_date(expiry_from)
        et = _parse_date(expiry_to)
        result = await get_accumulated_pnl(
            instrument_type=instrument_type, symbol_root=symbol_root,
        )
        return _hist_realized_pnl_rows(
            result, account=account, segment=segment, expiry_from=ef, expiry_to=et,
        )

    @get("/tradebook", guards=[cap_guard("view_audit")])
    async def list_tradebook(
        self,
        from_date:   Optional[str] = None,
        to_date:     Optional[str] = None,
        account:     Optional[str] = None,
        segment:     Optional[str] = None,
        instrument_type: Optional[str] = None,
        symbol_root: Optional[str] = None,
        exchange:    Optional[str] = None,
        broker:      Optional[str] = None,
        limit:       int = 50,
        offset:      int = 0,
    ) -> TradebookListResponse:
        """Raw Kite Console tradebook — `kite_console_pnl_sync` where
        report_type='tradebook'. One row per broker trade_id — the
        actual per-fill record, NOT `/trades` (a daily_book settlement
        snapshot, one row per account+symbol+day).

        Date filter is on `trade_date`. UNLIKE `/orders`/`/trades`/
        `/funds`, there is no default window — omit both `from_date`
        and `to_date` to get the entire backfilled history.

        `account` matches either the raw broker code or its already-
        masked display form (same double-mask-safe rule as
        `/realized-pnl`). `segment`/`instrument_type` are exact-match
        against the stored lowercase values ('equity'/'derivatives'/
        'commodities', 'equity'/'future'/'option'); `symbol_root`/
        `exchange` are exact-match against the stored uppercase form.

        `broker`: see this module's own docstring for how this
        degrades until `kite_console_pnl_sync.broker` lands — on this
        endpoint it filters FOR REAL the moment that column exists, no
        second pass needed.

        Sorted by `trade_date` descending, `id` descending as a
        deterministic tiebreak."""
        limit  = max(1, min(int(limit or 50), 500))
        offset = max(0, int(offset or 0))
        conditions = _hist_build_console_conditions(
            "tradebook",
            from_date=_parse_date(from_date), to_date=_parse_date(to_date),
            account=account, broker=broker,
        )
        if segment:
            conditions.append(KiteConsolePnlSync.segment == segment.strip().lower())
        if instrument_type:
            conditions.append(
                KiteConsolePnlSync.instrument_type == instrument_type.strip().lower()
            )
        if symbol_root:
            conditions.append(KiteConsolePnlSync.symbol_root == symbol_root.strip().upper())
        if exchange:
            conditions.append(KiteConsolePnlSync.exchange == exchange.strip().upper())

        async with async_session() as s:
            total = (await s.execute(
                select(_func.count(KiteConsolePnlSync.id)).where(and_(*conditions))
            )).scalar_one()
            rows = (await s.execute(
                select(KiteConsolePnlSync).where(and_(*conditions))
                  .order_by(desc(KiteConsolePnlSync.trade_date), desc(KiteConsolePnlSync.id))
                  .limit(limit).offset(offset)
            )).scalars().all()

        return TradebookListResponse(
            rows=[_hist_tradebook_row(r) for r in rows],
            total=int(total or 0), limit=limit, offset=offset,
        )

    @get("/ledger", guards=[cap_guard("view_audit")])
    async def list_ledger(
        self,
        from_date: Optional[str] = None,
        to_date:   Optional[str] = None,
        account:   Optional[str] = None,
        segment:   Optional[str] = None,
        broker:    Optional[str] = None,
        limit:     int = 50,
        offset:    int = 0,
    ) -> LedgerListResponse:
        """Raw Kite Console Funds Statement ledger —
        `kite_console_pnl_sync` where report_type='ledger'. One row
        per voucher (margin block/reversal, net-obligation, etc.) —
        distinct from `/funds` (a daily_book cash SNAPSHOT, one row
        per account+segment+day, not a voucher-level feed).

        Date filter is on `trade_date` (the voucher's own posting
        date, surfaced as `posting_date` in the response). No default
        window, same as `/tradebook` — omit both to get everything.

        `segment` filters on a DERIVED value (`raw_row.cost_center`
        mapped via `_ledger_segment_from_cost_center()`) — the typed
        `KiteConsolePnlSync.segment` column is always 'all' for ledger
        rows, so it is never used for this filter. The derivation lives
        in JSONB, not an indexed column, so this is pushed into the SQL
        WHERE clause via `_hist_ledger_segment_condition()` (a dialect-
        portable `raw_row->>'cost_center' ILIKE '{prefix} - %'`, built
        by inverting `_LEDGER_COST_CENTER_SEGMENT_MAP` — never a second,
        independently-filtered Python pass over the full table) — the
        DB does the filtering AND the paging, same as every other
        endpoint in this module (P2 fix, 2026-11-27: this used to fetch
        every date/account/broker-matching row, filter by segment in
        Python, THEN slice `[offset:offset+limit]` — full table load
        + full in-process JSON parse on every page of every request).

        Sorted by `trade_date` descending, `id` descending — `id`
        preserves the per-pull insertion order, so same-day vouchers
        read in a coherent running-balance order."""
        limit  = max(1, min(int(limit or 50), 500))
        offset = max(0, int(offset or 0))
        conditions = _hist_build_console_conditions(
            "ledger",
            from_date=_parse_date(from_date), to_date=_parse_date(to_date),
            account=account, broker=broker,
        )
        seg_cond = _hist_ledger_segment_condition(segment)
        if seg_cond is not None:
            conditions.append(seg_cond)

        async with async_session() as s:
            total = (await s.execute(
                select(_func.count(KiteConsolePnlSync.id)).where(and_(*conditions))
            )).scalar_one()
            rows = (await s.execute(
                select(KiteConsolePnlSync).where(and_(*conditions))
                  .order_by(desc(KiteConsolePnlSync.trade_date), desc(KiteConsolePnlSync.id))
                  .limit(limit).offset(offset)
            )).scalars().all()

        return LedgerListResponse(
            rows=[_hist_ledger_row(r) for r in rows],
            total=int(total or 0), limit=limit, offset=offset,
        )

    @get("/pnl-summary", guards=[cap_guard("view_audit")])
    async def pnl_summary(
        self,
        account:     Optional[str] = None,
        segment:     Optional[str] = None,
        symbol_root: Optional[str] = None,
        broker:      Optional[str] = None,
        expiry_from: Optional[str] = None,
        expiry_to:   Optional[str] = None,
    ) -> PnlSummaryResponse:
        """Win/loss aggregate over the Kite Console P&L reconciliation
        data (`report_type='pnl'`), grouped by (symbol_root, segment) —
        a first-pass input for "which symbols/segments have been
        consistently profitable", NOT a backtester or pattern-mining
        engine. See `_hist_pnl_summary_rows()` for the exact grouping
        and dedup rule (reuses `get_accumulated_pnl()`'s own already-
        deduped per-contract-cycle totals, same as `/realized-pnl`).

        Date range here is `expiry_from`/`expiry_to` (contract expiry),
        NOT a trade-date range — every `report_type='pnl'` row has a
        null `trade_date` (confirmed live; a P&L-statement row is a
        per-symbol-per-pull aggregate, not a single dated event).

        `account`/`segment`/`expiry_from`/`expiry_to` behave exactly
        as on `/realized-pnl`. `symbol_root` is forwarded to
        `get_accumulated_pnl()` itself (its own exact-match kwarg).
        `broker`: see this module's own docstring — a no-op for 'kite'
        (or omitted), an empty result for any other value, until
        `get_accumulated_pnl()` gains real broker support."""
        if broker is not None and not _hist_broker_param_matches_all(broker):
            return PnlSummaryResponse(rows=[], total=0)
        from scripts.kite_console_pnl_sync import get_accumulated_pnl

        ef = _parse_date(expiry_from)
        et = _parse_date(expiry_to)
        result = await get_accumulated_pnl(symbol_root=symbol_root)
        rows = _hist_pnl_summary_rows(
            result, account=account, segment=segment, expiry_from=ef, expiry_to=et,
        )
        return PnlSummaryResponse(rows=rows, total=len(rows))

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
        from backend.brokers.registry import get_broker
        from backend.shared.helpers.utils import mask_account

        df, dt = _hist_backfill_validate(data)
        account = (data.account or "").strip()

        loaded = _hist_resolve_loaded_accounts()
        if account not in loaded:
            raise HTTPException(status_code=404,
                                detail=f"account {mask_account(account)} not loaded")

        broker = get_broker(account)
        broker_id = getattr(broker, "broker_id", "unknown")

        # Pull the ledger via the broker adapter. Raises 501 if not implemented,
        # 502 on fetch failure. Sync call offloaded to the executor.
        entries = await _hist_fetch_ledger_entries(broker, df, dt, broker_id)

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
        added = 0
        skipped = 0
        now_utc = datetime.now(timezone.utc)
        async with async_session() as s:
            for e in entries:
                try:
                    # Each row gets its own SAVEPOINT (session.begin_nested()).
                    # In Postgres, once ANY statement in a transaction fails,
                    # the whole transaction is aborted and every later
                    # statement silently fails too (InFailedSqlTransaction) —
                    # the per-row try/except alone does NOT protect later
                    # rows the way it looks like it should (P2 fix,
                    # 2026-11-27). A nested transaction rolls back to its
                    # own savepoint on the row's own exception, leaving the
                    # OUTER transaction (and every row after it) unaffected.
                    async with s.begin_nested():
                        await _hist_upsert_entry(s, account, e, now_utc)
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
