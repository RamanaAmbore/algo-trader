"""Broker-agnostic P&L/trade history row shaping + accumulation.

Extracted (2026-11-28) out of `scripts/kite_console_pnl_sync.py` so that
`scripts/dhan_pnl_sync.py` no longer has to import its shared row-building
and accumulation functions from the Kite-specific scraper script. This
module has NO Playwright, NO browser automation, and NO Kite-Console-page
scraping logic — it is pure row-shaping (`build_sync_row`/`build_dedup_key`)
plus the async DB upsert (`ingest_rows`) and the read-side accumulation
query (`get_accumulated_pnl`), all operating on the `BrokerPnlHistory`
table (renamed from `KiteConsolePnlSync`/`kite_console_pnl_sync` in the
same change — see that model's own docstring in `backend/api/models.py`
for the full table/row-shape design).

`scripts/kite_console_pnl_sync.py` imports every function below and
re-exports them under its own module namespace (`from
backend.api.algo.pnl_history import ...`), so every existing
`from scripts.kite_console_pnl_sync import build_sync_row` /
`ingest_rows` / `build_dedup_key` / `get_accumulated_pnl` call site, and
every existing `patch("scripts.kite_console_pnl_sync.<name>", ...)` test
mock, keeps working unchanged — Python binds a `from X import Y` name
into the IMPORTING module's own namespace, so a function living inside
`scripts/kite_console_pnl_sync.py`'s namespace (e.g. `build_ledger_sync_row`,
which stays there — it is ledger-specific and not part of this extraction)
can still call `build_sync_row`/`build_dedup_key` through a plain global
lookup exactly as before, because those names ARE present in that
module's globals too, just bound via import instead of `def`.

`scripts/dhan_pnl_sync.py` imports `ingest_rows` directly from THIS module
(not from `scripts.kite_console_pnl_sync`) — the one piece the audit that
led to this change called out specifically ("Dhan importing from Kite's
script"). It still imports the genuinely Kite-Console-scraper-specific
helpers (`alert_critical`, `alert_pnl_reconciliation_disagreement`,
`build_ledger_sync_row`, `chunk_by_calendar_month`, `ingest_ledger_rows`,
`validate_ledger_rows`, `ReportValidationError`) from
`scripts.kite_console_pnl_sync` — those were not named by the task that
produced this change and are not required for correctness here; moving
them is a separate, independent decision.

SCOPE NOTE (2026-11-28): this table/model rename, and the `broker` kwarg
on `get_accumulated_pnl()` below, cover Kite and Dhan only, per explicit
operator scope confirmation. Groww is deliberately excluded — do not add
Groww-specific filtering/logic here. `broker` is left as a plain string
rather than a `Literal["kite", "dhan"]` so a future Groww addition needs
no second migration/signature change, but no Groww value is validated or
special-cased today.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import-cycle-free type hints only
    pass


# ---------------------------------------------------------------------------
# Instrument classification (pure, no I/O) — reuses derivatives.py's
# existing F&O symbol parser, never a bespoke regex.
# ---------------------------------------------------------------------------

def classify_instrument(symbol: "str | None") -> "tuple[str, str]":
    """Returns `(instrument_type, symbol_root)` for a Console-reported
    tradingsymbol. `instrument_type` is 'future' | 'option' | 'equity'.

    Reuses `derivatives.parse_tradingsymbol()` — the SAME parser
    `build_sync_row()`'s own expiry derivation already uses — rather than
    a second, independent classification rule. Deliberately NEVER uses
    `symbol.endswith('CE'/'PE')`: that would misclassify an equity
    ticker like RELIANCE or BAJFINANCE as an option (the exact defect
    class this codebase has already fixed elsewhere — see
    derivativesMath.js's `isFOSymbol()`/`rollupByUnderlying` fix in
    CLAUDE.md). A symbol that fails to parse as F&O (equity, or an
    unparseable F&O-looking string) is classified 'equity', with its
    root defaulting to the symbol itself."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return "equity", ""
    try:
        from backend.api.algo.derivatives import parse_tradingsymbol
        parsed = parse_tradingsymbol(sym)
    except Exception:
        parsed = None
    if parsed:
        kind = parsed.get("kind")
        root = parsed.get("root") or sym
        if kind == "fut":
            return "future", root
        if kind == "opt":
            return "option", root
    return "equity", sym


def _derive_expiry_from_symbol(symbol: "str | None") -> "date | None":
    """Pure — wraps `parse_tradingsymbol()` with the same best-effort
    try/except `build_sync_row()` always used, extracted so that
    function's own branching stays under the complexity gate."""
    try:
        from backend.api.algo.derivatives import parse_tradingsymbol
        parsed = parse_tradingsymbol(symbol or "")
    except Exception:
        parsed = None
    return parsed.get("expiry") if parsed else None


def _parse_date_cell(value) -> "date | None":
    if not value:
        return None
    value = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _parse_float_cell(value) -> "float | None":
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _fill_fields_for_row(report_type: str, row: dict, qty: "float | None",
                          price: "float | None", trade_type: str) -> dict:
    """Returns the report-type-dependent fields
    (`buy_qty`/`sell_qty`/`buy_value`/`sell_value`/`realized_pnl`) for one
    row. Extracted out of `build_sync_row()` to keep that function's own
    branching under the complexity gate (`radon cc ... -s -n D`).

    buy/sell qty/value are Tradebook-only fields — a P&L-statement row is
    a symbol-level aggregate, not a per-trade BUY/SELL row, so these
    deliberately stay null for report_type='pnl' even if a future,
    verified export happens to carry similarly-named columns."""
    if report_type == "pnl":
        return {
            "buy_qty": None, "sell_qty": None,
            "buy_value": None, "sell_value": None,
            "realized_pnl": _parse_float_cell(row.get("realized_pnl")),
        }
    has_qty_price = qty is not None and price is not None
    return {
        "buy_qty": qty if trade_type == "BUY" else None,
        "sell_qty": qty if trade_type == "SELL" else None,
        "buy_value": (qty * price) if (trade_type == "BUY" and has_qty_price) else None,
        "sell_value": (qty * price) if (trade_type == "SELL" and has_qty_price) else None,
        "realized_pnl": None,
    }


# ---------------------------------------------------------------------------
# Ingestion (pure dedup-key/row-building + the async upsert)
# ---------------------------------------------------------------------------

def build_dedup_key(report_type: str, *, row: dict,
                     pull_start: "date | None" = None,
                     pull_end: "date | None" = None,
                     seq: "int | None" = None) -> str:
    """Stable idempotency key per report_type — see
    `BrokerPnlHistory`'s own docstring (backend/api/models.py) for the
    full reasoning behind why tradebook and pnl need DIFFERENT key
    shapes.

    `seq` is ledger-only: a ledger entry carries no natural stable
    identifier (no trade_id/order_id, and `net_balance` is a RUNNING
    total that shifts on re-pull whenever the broker posts a back-dated
    voucher — see `ingest_ledger_rows()`'s own docstring for why that
    rules out a content-addressed key). `seq` is the row's 0-based
    position within THIS pull's own parsed list, which only needs to
    be unique WITHIN one ingest call — `ingest_ledger_rows()` always
    deletes the whole (account, chunk) bucket before inserting, so
    this key's stability ACROSS pulls is irrelevant, only its
    uniqueness WITHIN one."""
    if report_type == "tradebook":
        trade_id = (row.get("trade_id") or "").strip()
        order_id = (row.get("order_id") or "").strip()
        exchange = (row.get("exchange") or "").strip().upper()
        if trade_id:
            return f"{exchange}:{order_id}:{trade_id}"
        # No trade_id at all — degrade to a composite of every field that
        # identifies a single fill rather than silently dropping the row.
        return (
            f"{exchange}:{order_id}:{row.get('symbol')}:{row.get('trade_date')}:"
            f"{row.get('quantity')}:{row.get('price')}"
        )
    if report_type == "pnl":
        return f"{row.get('symbol')}:{row.get('expiry')}:{pull_start}:{pull_end}"
    if report_type == "ledger":
        return f"{pull_start}:{pull_end}:{seq}"
    raise ValueError(f"unknown report_type: {report_type!r}")


def build_sync_row(masked_account: str, segment: str, report_type: str, row: dict, *,
                    pull_start: "date | None" = None,
                    pull_end: "date | None" = None,
                    broker: str = "kite") -> dict:
    """Builds one `broker_pnl_history` row payload from one parsed
    report row. Pure — no DB/session access.

    `broker` (added 2026-11-27, default `"kite"` so every pre-existing
    call site in `scripts/kite_console_pnl_sync.py` is byte-identical)
    — reused by `scripts/dhan_pnl_sync.py` to write `broker='dhan'` rows
    into the same table/row-shape, after mapping its own broker-specific
    response fields into this function's Kite-CSV-shaped `row` dict
    first (see that script's own field-mapping docstring)."""
    symbol = row.get("symbol")
    trade_date = _parse_date_cell(row.get("trade_date"))
    expiry = _derive_expiry_from_symbol(symbol)
    instrument_type, symbol_root = classify_instrument(symbol)
    trade_id = (row.get("trade_id") or "").strip() or None
    order_id = (row.get("order_id") or "").strip() or None
    exchange = (row.get("exchange") or "").strip().upper() or None
    qty = _parse_float_cell(row.get("quantity"))
    price = _parse_float_cell(row.get("price"))
    trade_type = (row.get("trade_type") or "").strip().upper()
    fill_fields = _fill_fields_for_row(report_type, row, qty, price, trade_type)
    return {
        "broker": broker,
        "account": masked_account,
        "segment": segment,
        "report_type": report_type,
        "dedup_key": build_dedup_key(report_type, row=row, pull_start=pull_start, pull_end=pull_end),
        "symbol": symbol,
        "exchange": exchange,
        "instrument_type": instrument_type,
        "symbol_root": symbol_root,
        "expiry": expiry,
        "trade_date": trade_date,
        "trade_id": trade_id,
        "order_id": order_id,
        "pull_range_start": pull_start,
        "pull_range_end": pull_end,
        **fill_fields,
        "raw_row": row,
        "updated_at": datetime.now(timezone.utc),
    }


async def ingest_rows(session, masked_account: str, segment: str, report_type: str,
                       rows: "list[dict]", *, pull_start: "date | None" = None,
                       pull_end: "date | None" = None,
                       broker: str = "kite") -> int:
    """Idempotent upsert of parsed report rows into `broker_pnl_history`
    — `ON CONFLICT (account, report_type, dedup_key) DO UPDATE`, mirroring
    this codebase's existing UPSERT-on-conflict convention (see
    `daily_snapshot.py`). Re-ingesting an overlapping pull never creates a
    duplicate row for the same real trade/aggregate.

    `broker` (added 2026-11-27, default `"kite"`) — see `build_sync_row()`'s
    own docstring. Deliberately NOT in `update_cols` below: `broker` can
    never legitimately change for an already-existing `(account,
    report_type, dedup_key)` row (the masked `account` form alone already
    disambiguates Kite from Dhan — different broker-letter mask prefix,
    see `BrokerPnlHistory.broker`'s own model-column comment), so there
    is nothing to update it TO on a conflict.

    Shared by `scripts/kite_console_pnl_sync.py` (Kite) and
    `scripts/dhan_pnl_sync.py` (Dhan, imports this function DIRECTLY
    from this module, not from the Kite script)."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from backend.api.models import BrokerPnlHistory

    if not rows:
        return 0
    payloads = [
        build_sync_row(masked_account, segment, report_type, r,
                        pull_start=pull_start, pull_end=pull_end, broker=broker)
        for r in rows
    ]
    stmt = pg_insert(BrokerPnlHistory).values(payloads)
    update_cols = {
        c: getattr(stmt.excluded, c) for c in (
            "symbol", "exchange", "instrument_type", "symbol_root",
            "expiry", "trade_date", "order_id",
            "buy_qty", "sell_qty", "buy_value", "sell_value",
            "realized_pnl", "raw_row", "updated_at",
        )
    }
    stmt = stmt.on_conflict_do_update(
        index_elements=["account", "report_type", "dedup_key"],
        set_=update_cols,
    )
    await session.execute(stmt)
    await session.commit()
    return len(payloads)


# ---------------------------------------------------------------------------
# Accumulation — query-time aggregation over the deduped base rows, NEVER
# an incrementally-mutated running counter (that would double-count every
# time an overlapping range gets re-ingested). Correct by construction
# regardless of how many times a range is re-pulled, because
# `ingest_rows()` upserts each dedup_key in place, never duplicating it.
# ---------------------------------------------------------------------------

async def get_accumulated_pnl(
    account: "str | None" = None,
    instrument_type: "str | None" = None,
    symbol_root: "str | None" = None,
    expiry: "date | None" = None,
    broker: "str | None" = None,
) -> dict:
    """Accumulated realized P&L over `broker_pnl_history`
    (`report_type='pnl'` rows only), grouped by every requested dimension
    left ungrouped by the filters above.

    2026-11-26 redesign (see `BrokerPnlHistory`'s model docstring for
    the fuller reasoning) — real-world constraint discovered before any
    real data existed in this table (confirmed 0 rows in prod at the
    time): Kite Console's Tradebook/P&L-statement CSV export does not
    reliably support a requested date range wider than ~1 month, so
    every real pull is now forced through `chunk_by_calendar_month()`
    into non-overlapping calendar-month-aligned chunks.

    This is NOT a bare `SUM(realized_pnl)` across every matching row —
    an earlier version of this redesign was, and that double-counts on
    two realistic paths: (a) re-running the SAME month (a retried or
    re-triggered pull) produces a DIFFERENT `pull_range_end` (today's
    date moves), hence a DIFFERENT `dedup_key`, hence a SECOND row for
    the same month rather than an upsert in place; (b) the FIRST ongoing
    sync after a one-time backfill can genuinely overlap the backfill's
    own last month (an expiry essentially never lands exactly on a
    calendar-month boundary), again with a different `pull_range_end`
    for that shared month. A bare SUM would double-count both.

    The actual rule: per (account, segment, symbol, expiry,
    `pull_range_start`) — `sync_account()` floors every PNL chunk's own
    `pull_range_start` to the 1st of its calendar month, so this key is
    stable and shared by every pull that ever touched "this symbol, this
    expiry, this calendar month", regardless of which caller/trigger
    produced it — pick only the row with the LATEST `pull_range_end`
    (via a `ROW_NUMBER() OVER (PARTITION BY ... ORDER BY pull_range_end
    DESC, updated_at DESC)` window, `rn=1`). That is the single most
    complete picture of that one month. Summing is then correct, and
    happens, ACROSS the resulting per-month "latest" rows — both across
    DIFFERENT symbols within one requested grouping (e.g. multiple
    strikes under one underlying+expiry) and across DIFFERENT months for
    the same symbol+expiry (a position spanning more than one calendar
    month), since those are genuinely non-overlapping facts once each
    month's own latest-pull value has been picked.

    `broker` (added 2026-11-28) — optional exact-match filter against the
    stored, lowercase `broker` column ('kite' | 'dhan'). `None` (the
    default) preserves the exact pre-2026-11-28 behaviour: every broker's
    rows combined, with no filter applied at all — every existing caller
    (`history.py`'s `/realized-pnl`/`/pnl-summary` routes with no
    `broker` query param, `_reconcile_ground_truth_against_daily_book()`)
    is therefore unaffected by this parameter's addition. A caller that
    DOES pass `broker` gets ONLY that broker's rows in the returned
    `groups`/`by_account_root_expiry`/`overall_total` — there is
    deliberately no Groww value handled specially here; see this
    module's own docstring for the scope decision.

    Returns:
        {
          "groups": {(account, segment, instrument_type, symbol_root, expiry): pnl, ...},
          "by_account_root_expiry": {(account, symbol_root, expiry): pnl, ...},
          "overall_total": float,
        }

    `by_account_root_expiry` collapses `segment`/`instrument_type` out of
    the detailed `groups` key (summed in Python over the already-fetched
    rows — no second query) — this is the shape
    `reconcile_against_daily_book()` needs, since `daily_book` has
    no `instrument_type` column to join against.

    `account`, when given, is the RAW account code (matching every other
    function in this script) — masked internally before filtering, since
    `BrokerPnlHistory.account` stores the masked form by deliberate,
    already-tested design (see that model's own docstring). This is a
    flagged "worth a second look" item, not silently changed: the join
    is only as stable as whatever account roster `register_accounts()`
    was last called with in this process — the same precondition every
    other `mask_account()` consumer in this codebase already has.
    """
    from sqlalchemy import select, func, and_
    from backend.api.models import BrokerPnlHistory
    from backend.api.database import async_session
    from backend.shared.helpers.utils import mask_account

    T = BrokerPnlHistory
    filters = [T.report_type == "pnl"]
    if account:
        filters.append(T.account == mask_account(account))
    if instrument_type:
        filters.append(T.instrument_type == instrument_type)
    if symbol_root:
        filters.append(T.symbol_root == symbol_root)
    if expiry:
        filters.append(T.expiry == expiry)
    if broker:
        filters.append(T.broker == broker.strip().lower())

    rn = func.row_number().over(
        partition_by=(T.account, T.segment, T.symbol, T.expiry, T.pull_range_start),
        order_by=(T.pull_range_end.desc(), T.updated_at.desc()),
    ).label("rn")

    ranked = (
        select(
            T.account, T.segment, T.instrument_type, T.symbol_root,
            T.symbol, T.expiry, T.pull_range_start, T.realized_pnl, rn,
        )
        .where(and_(*filters))
        .subquery()
    )
    latest_per_month = select(ranked).where(ranked.c.rn == 1).subquery()

    group_cols = (
        latest_per_month.c.account, latest_per_month.c.segment,
        latest_per_month.c.instrument_type, latest_per_month.c.symbol_root,
        latest_per_month.c.expiry,
    )
    stmt = (
        select(*group_cols, func.sum(latest_per_month.c.realized_pnl).label("total_pnl"))
        .group_by(*group_cols)
    )

    async with async_session() as session:
        result = await session.execute(stmt)
        rows = result.all()

    groups: dict = {}
    by_account_root_expiry: dict = {}
    overall_total = 0.0
    for acct, seg, itype, root, exp, total in rows:
        total_f = float(total) if total is not None else 0.0
        groups[(acct, seg, itype, root, exp)] = total_f
        collapsed_key = (acct, root, exp)
        by_account_root_expiry[collapsed_key] = (
            by_account_root_expiry.get(collapsed_key, 0.0) + total_f
        )
        overall_total += total_f

    return {
        "groups": groups,
        "by_account_root_expiry": by_account_root_expiry,
        "overall_total": overall_total,
    }
