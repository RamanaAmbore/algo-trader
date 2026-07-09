"""Holdings endpoint — returns per-account rows and summary."""

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import msgspec
import pandas as pd
import polars as pl
from litestar import Controller, Request, get
from litestar.exceptions import HTTPException

from backend.api.auth_guard import is_admin_request
from backend.api.rbac import (
    resolve_role_from_connection, user_scope_for_connection, normalise_role,
)
from backend.api.algo.pnl_math import recompute_row_percentages
from backend.api.cache import get_or_fetch, invalidate
from backend.api.helpers.ltp_patch import apply_ltp_patch, holdings_policy
from backend.api.helpers.price_resolver import resolve_current_price
from backend.api.helpers.snapshot_gate import (
    closed_hours_or_broker, is_exchange_closed_now, latest_snapshot_ltp_map,
)
from backend.api.schemas import HoldingsResponse, HoldingRow, HoldingsSummaryRow
from backend.brokers import broker_apis
from backend.shared.helpers.date_time_utils import timestamp_display
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import mask_account

logger = get_logger(__name__)

_IST = ZoneInfo("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Closed-hours snapshot helpers
# ---------------------------------------------------------------------------

_HOLDINGS_SNAPSHOT_SQL = """
    WITH latest_batch AS (
        SELECT account, MAX(captured_at) AS max_at
        FROM daily_book
        WHERE kind = 'holdings' AND ltp IS NOT NULL
        GROUP BY account
    )
    SELECT db.account, db.symbol, db.exchange, db.qty, db.avg_cost,
           db.ltp, db.day_pnl, db.total_pnl, db.captured_at
    FROM daily_book db
    JOIN latest_batch lb
      ON db.account = lb.account AND db.captured_at = lb.max_at
    WHERE db.kind = 'holdings'
      AND db.ltp IS NOT NULL
      AND NOT (db.ltp = 0 AND (db.total_pnl = 0 OR db.total_pnl IS NULL)
               AND db.avg_cost IS NOT NULL AND db.avg_cost > 0)
    ORDER BY db.account, db.symbol
"""


async def _query_holdings_snapshot_rows():
    """Latest snapshot BATCH per account — pull every (account, symbol)
    row written in the most-recent captured_at for that account.
    The prior `DISTINCT ON (account, symbol) ORDER BY captured_at DESC`
    pattern picked the newest non-zero row per symbol regardless of
    date. For symbols closed weeks ago, that's a months-old row
    whose day_pnl was real on its capture date but is summed today
    as nonsense (NavStrip showed ₹14k vs the real ₹30k holdings P∆).
    Batch-anchoring guarantees we ONLY surface the broker's current
    book, never carry-over from prior sessions. Zero-payload guard
    still applies inside the batch in case the writer slipped one
    through.
    """
    from backend.api.database import async_session
    from sqlalchemy import text as _sql_text

    try:
        async with async_session() as session:
            result = await session.execute(_sql_text(_HOLDINGS_SNAPSHOT_SQL))
            return result.all()
    except Exception as exc:
        logger.warning(f"holdings snapshot query failed: {exc}")
        return None


def _build_holding_row_from_snapshot(raw_row) -> tuple[HoldingRow, float, float, float, float]:
    """Convert one raw snapshot tuple into a HoldingRow + the four
    per-account sums (inv, cur, total_pnl, day_pnl) that the caller
    aggregates into HoldingsSummaryRow.
    """
    (account, symbol, exchange, qty, avg_cost, ltp,
     day_pnl, total_pnl, _captured_at) = raw_row

    avg_cost_f  = float(avg_cost)  if avg_cost  is not None else 0.0
    ltp_f       = float(ltp)       if ltp        is not None else 0.0
    total_pnl_f = float(total_pnl) if total_pnl  is not None else 0.0
    day_pnl_f   = float(day_pnl)   if day_pnl    is not None else 0.0
    qty_i       = int(qty)         if qty         is not None else 0
    inv_val     = avg_cost_f * qty_i
    cur_val     = ltp_f      * qty_i

    # pnl_percentage: pnl / |avg × qty| × 100
    # (inv_val = avg_cost_f × qty_i, so use that directly)
    pnl_pct = (total_pnl_f / inv_val * 100.0) if inv_val else 0.0
    # day_change_percentage: day_change_val / |close × qty| × 100
    # close_price for a holdings snapshot is the last stored LTP.
    # Use |avg × qty| (inv_val) as the fallback when ltp is zero.
    close_notional = abs(ltp_f * qty_i)
    day_pct = (day_pnl_f / close_notional * 100.0) if close_notional else (
        day_pnl_f / inv_val * 100.0 if inv_val else 0.0
    )
    row = HoldingRow(
        account=str(account),
        tradingsymbol=str(symbol),
        exchange=str(exchange or ""),
        quantity=qty_i,
        opening_quantity=qty_i,
        average_price=avg_cost_f,
        close_price=ltp_f,
        last_price=ltp_f,
        inv_val=inv_val,
        cur_val=cur_val,
        pnl=total_pnl_f,
        pnl_percentage=pnl_pct,
        day_change_val=day_pnl_f,
        day_change_percentage=day_pct,
        last_price_stale=True,
        price_source="snapshot_settled",
        current_price=ltp_f,
        is_animating=False,
    )
    return row, inv_val, cur_val, total_pnl_f, day_pnl_f


def _build_holdings_summary(
    inv_by_account: dict[str, float],
    cur_by_account: dict[str, float],
    pnl_by_account: dict[str, float],
    dcv_by_account: dict[str, float],
) -> list[HoldingsSummaryRow]:
    """Per-account HoldingsSummaryRow list + a TOTAL row."""
    summary: list[HoldingsSummaryRow] = []
    total_inv = total_cur = total_pnl_s = total_dcv = 0.0
    for acct in pnl_by_account:
        inv  = inv_by_account.get(acct, 0.0)
        cur  = cur_by_account.get(acct, 0.0)
        pnl  = pnl_by_account.get(acct, 0.0)
        dcv  = dcv_by_account.get(acct, 0.0)
        prev = cur - dcv
        summary.append(HoldingsSummaryRow(
            account=acct,
            inv_val=inv,
            cur_val=cur,
            pnl=pnl,
            pnl_percentage=pnl / inv * 100.0 if inv else 0.0,
            day_change_val=dcv,
            day_change_percentage=dcv / prev * 100.0 if prev else 0.0,
        ))
        total_inv += inv; total_cur += cur
        total_pnl_s += pnl; total_dcv += dcv
    total_prev = total_cur - total_dcv
    summary.append(HoldingsSummaryRow(
        account="TOTAL",
        inv_val=total_inv,
        cur_val=total_cur,
        pnl=total_pnl_s,
        pnl_percentage=total_pnl_s / total_inv * 100.0 if total_inv else 0.0,
        day_change_val=total_dcv,
        day_change_percentage=total_dcv / total_prev * 100.0 if total_prev else 0.0,
    ))
    return summary


async def _holdings_snapshot() -> Optional[HoldingsResponse]:
    """Read the most-recent pre-today daily_book[kind='holdings'] snapshot
    and reconstruct a HoldingsResponse from it.

    Returns None when no snapshot exists or the DB query fails.
    """
    raw_rows = await _query_holdings_snapshot_rows()
    if not raw_rows:
        return None

    snap_captured_at: str = raw_rows[0][8].isoformat() if raw_rows[0][8] else ""

    inv_by_account: dict[str, float] = {}
    cur_by_account: dict[str, float] = {}
    pnl_by_account: dict[str, float] = {}
    dcv_by_account: dict[str, float] = {}

    rows: list[HoldingRow] = []
    for raw_row in raw_rows:
        row, inv_val, cur_val, total_pnl_f, day_pnl_f = (
            _build_holding_row_from_snapshot(raw_row)
        )
        rows.append(row)

        acct = row.account
        inv_by_account[acct] = inv_by_account.get(acct, 0.0) + inv_val
        cur_by_account[acct] = cur_by_account.get(acct, 0.0) + cur_val
        pnl_by_account[acct] = pnl_by_account.get(acct, 0.0) + total_pnl_f
        dcv_by_account[acct] = dcv_by_account.get(acct, 0.0) + day_pnl_f

    summary = _build_holdings_summary(
        inv_by_account, cur_by_account, pnl_by_account, dcv_by_account
    )

    return HoldingsResponse(
        rows=rows,
        summary=summary,
        refreshed_at=timestamp_display(),
        as_of=snap_captured_at,
    )


def _is_broker_outage(err: Exception) -> bool:
    """Detect Kite (Zerodha) upstream HTTP gateway errors. See
    funds.py for the rationale — same helper, same patterns."""
    s = str(err).lower()
    return any(needle in s for needle in (
        'bad gateway', '502', '503', '504',
        'service unavailable', 'gateway timeout',
    ))

_ROW_COLS = [
    'account', 'tradingsymbol', 'exchange', 'quantity', 'opening_quantity',
    'average_price', 'close_price', 'last_price', 'inv_val', 'cur_val',
    'pnl', 'pnl_percentage', 'day_change', 'day_change_val', 'day_change_percentage',
    # Staleness flag — True when last_price came from the last-known-good
    # cache rather than a live broker or ticker source.
    'last_price_stale',
    # Account-level staleness — True when the entire row was substituted
    # from broker_apis' LKG frame cache because the account's circuit
    # breaker was OPEN. Preserves DH6847 rows across breaker-open cycles.
    'account_stale',
]

_TTL = 30  # seconds — background task invalidates on each refresh


def _override_stale_ltp_from_ticker(raw: pd.DataFrame) -> None:
    """Patch `last_price` from the live KiteTicker tick_map for any
    holdings row whose last_price is still zero or missing after
    `backfill_market_data`. Holdings brokers (Dhan, Groww) sometimes
    return zero LTP for equity symbols; the ticker always has the
    freshest streamed value for subscribed holdings.

    Only patches rows where the broker / backfill delivered a zero or
    missing LTP — never overwrites a valid non-zero broker value (same
    guard as positions route). Recomputes `day_change_val` + `day_change`
    on patched rows using (LTP - close) × opening_qty so the Day P&L
    column reflects the fresh tick immediately.

    Bookkeeping (ticker pull + LKG fallback + stale flag) is owned by
    `helpers/ltp_patch.apply_ltp_patch`. This route only owns the
    naive (LTP - close) × opening_qty recompute (no decomposed
    intraday — holdings don't have buy/sell decomposition columns).
    """
    res = apply_ltp_patch(raw, holdings_policy)
    if res is None or not res.any_patched:
        return

    _sel = pd.Index(res.patched_idx)
    _qty_col = 'opening_quantity' if 'opening_quantity' in raw.columns else 'quantity'
    _ltp_p = pd.to_numeric(raw.loc[_sel, 'last_price'], errors='coerce').fillna(0)
    _cls_p = pd.to_numeric(raw.loc[_sel, 'close_price'], errors='coerce').fillna(0) \
             if 'close_price' in raw.columns else pd.Series(0.0, index=_sel)
    _qty_p = pd.to_numeric(raw.loc[_sel, _qty_col], errors='coerce').fillna(0)
    _dcv = (_ltp_p - _cls_p) * _qty_p
    if 'day_change_val' in raw.columns:
        raw.loc[_sel, 'day_change_val'] = _dcv.where(_ltp_p > 0,
                                                      raw.loc[_sel, 'day_change_val'])
    if 'day_change' in raw.columns:
        raw.loc[_sel, 'day_change'] = _ltp_p - _cls_p
    # Recompute day_change_percentage + pnl_percentage on patched rows.
    # day_change_val and pnl were updated by backfill_market_data for
    # holdings rows that had last_price patched — but pnl and cur_val
    # may also have been patched (see backfill_market_data inv_val/cur_val
    # chain). The percentage columns lag without this step.
    recompute_row_percentages(raw, _sel)
    n_stale = len(res.stale_idx)
    logger.info(
        f"holdings: ltp-override patched {len(res.patched_idx)}/{len(raw)} "
        f"zero-LTP rows from KiteTicker"
        + (f" ({n_stale} via last-known-good cache)" if n_stale else "")
    )


async def _overlay_snapshot_for_closed_exchanges(rows: list) -> list:
    """Per-exchange close-snapshot overlay for holdings rows under the
    unified animation model (Jul 2026 refactor). Delegates the per-row
    triad decision to `price_resolver.resolve_current_price` so
    positions + holdings + movers all share ONE branch matrix.

    Holdings-specific concern: cur_val is derived from ltp × qty
    (unlike positions where broker owns pnl). When the snapshot LTP
    wins, we recompute cur_val to match — otherwise the frontend TOTAL
    row rolls up stale broker cur_val against fresh snapshot LTP.

    Holdings are eq-only (NSE/BSE) so the closed-exchange gate almost
    always resolves to "NSE closed" during 15:30-23:30 IST. MCX holdings
    don't exist (delivery is by contract expiry). Kept generic so a
    future BSE-only session (or a corporate-action holiday split) still
    tags rows correctly.
    """
    if not rows:
        return rows
    exchange_closed: dict[str, bool] = {}
    def _closed(exch: str) -> bool:
        e = (exch or "").upper()
        if e not in exchange_closed:
            exchange_closed[e] = is_exchange_closed_now(e)
        return exchange_closed[e]

    import msgspec as _msc

    # Fast path — every exchange currently open. Resolver call per row.
    if not any(_closed(getattr(r, "exchange", "")) for r in rows):
        out: list = []
        for r in rows:
            live_ltp = float(getattr(r, "last_price", 0.0) or 0.0)
            price, source, animating = resolve_current_price(
                exchange_open=True, live_ltp=live_ltp,
            )
            out.append(_msc.structs.replace(
                r, price_source=source,
                current_price=price if price is not None else live_ltp,
                is_animating=animating,
            ))
        return out

    snap_map = await latest_snapshot_ltp_map("holdings")
    out = []
    for r in rows:
        exch = getattr(r, "exchange", "")
        broker_ltp = float(getattr(r, "last_price", 0.0) or 0.0)
        if not _closed(exch):
            price, source, animating = resolve_current_price(
                exchange_open=True, live_ltp=broker_ltp,
            )
            out.append(_msc.structs.replace(
                r, price_source=source,
                current_price=price if price is not None else broker_ltp,
                is_animating=animating,
            ))
            continue

        key = (getattr(r, "account", ""), getattr(r, "tradingsymbol", ""))
        snap_ltp = snap_map.get(key)
        has_snapshot = snap_ltp is not None and snap_ltp > 0

        price, source, animating = resolve_current_price(
            exchange_open=False,
            live_ltp=broker_ltp,
            snapshot_close=(float(snap_ltp) if has_snapshot else None),
            snapshot_last_ltp=broker_ltp,
            settled=has_snapshot,
        )
        replace_kwargs: dict = {
            "price_source": source,
            "current_price": price if price is not None else broker_ltp,
            "is_animating": animating,
        }
        # On settled path — overlay last_price + close_price + recompute
        # cur_val (holdings-specific: cur_val is derived from ltp × qty).
        if has_snapshot and price is not None:
            qty = int(
                getattr(r, "quantity", 0) or getattr(r, "opening_quantity", 0)
            )
            replace_kwargs["last_price"] = float(price)
            replace_kwargs["close_price"] = float(price)
            replace_kwargs["cur_val"] = float(price) * qty
        out.append(_msc.structs.replace(r, **replace_kwargs))
    return out


def _is_full_outage(per_acct: list) -> bool:
    """Every per-account frame carries fetch_failed = a true outage.

    Empty per_acct or ANY successful frame → legitimate 'no holdings' state
    (operator who hasn't taken delivery, or holds only F&O).
    """
    return bool(per_acct) and all(
        df.attrs.get("fetch_failed", False) for df in per_acct
    )


def _stale_since_map(per_acct: list) -> dict[str, str]:
    """Build {account → 'HH:MM IST'} for stale-substituted frames BEFORE
    concat (which drops all DataFrame.attrs)."""
    out: dict[str, str] = {}
    if not per_acct:
        return out
    for _df in per_acct:
        _ss = _df.attrs.get("stale_since")
        if not (_ss and not _df.empty and "account" in _df.columns):
            continue
        _acct = str(_df["account"].iloc[0])
        try:
            out[_acct] = datetime.fromtimestamp(
                float(_ss), tz=_IST
            ).strftime("%H:%M IST")
        except Exception:
            pass
    return out


def _prepare_raw_frame(per_acct: list) -> pd.DataFrame:
    """Concat + backfill + LTP-override + numeric-fillna. Returns the
    fully-hydrated pandas DataFrame ready for polars conversion."""
    raw = pd.concat(per_acct, ignore_index=True) if per_acct else pd.DataFrame()
    if raw.empty:
        return raw
    # Backfill missing market data (close_price + last_price) for adapters
    # that don't populate them (Dhan / Groww). Market data routes through
    # PriceBroker.quote (prefers Kite) so cross-broker rows agree on Day
    # P&L / Day % / Prev Close.
    broker_apis.backfill_market_data(raw)
    # Rows still at last_price=0 (rate-limit cool-off or missing quote) get
    # patched from the live KiteTicker snapshot — same pattern as positions.
    _override_stale_ltp_from_ticker(raw)
    numeric = raw.select_dtypes(include="number").columns
    raw[numeric] = raw[numeric].fillna(0)
    return raw


def _compute_summary_df(df: pl.DataFrame) -> pl.DataFrame:
    """Group by account, add derived %s, append TOTAL row.

    day_change_percentage uses YESTERDAY's value (cur_val - day_change_val)
    as the denominator — Kite's convention for "today moved X% off the
    previous close". Using cur_val (which already includes today's gain)
    would understate on positive days and overstate on negative.
    """
    sum_cols = [c for c in ["inv_val", "cur_val", "pnl", "day_change_val"] if c in df.columns]
    grouped = df.group_by("account").agg([pl.col(c).sum() for c in sum_cols])
    derived = [
        (pl.col("pnl") / pl.col("inv_val") * 100).alias("pnl_percentage"),
        (pl.col("day_change_val") / (pl.col("cur_val") - pl.col("day_change_val")) * 100)
            .alias("day_change_percentage"),
    ]
    grouped = grouped.with_columns(derived)
    totals = grouped.select(sum_cols).sum().with_columns([
        pl.lit("TOTAL").alias("account"), *derived
    ])
    return pl.concat([grouped, totals], how="diagonal").fill_nan(0).fill_null(0)


def _apply_stale_since_map(
    rows: list[HoldingRow], stale_since: dict[str, str],
) -> list[HoldingRow]:
    """Thread account_stale_since into rows where the account was
    LKG-substituted (breaker-open cache path). See positions.py."""
    if not stale_since:
        return rows
    return [
        msgspec.structs.replace(r, account_stale_since=stale_since[r.account])
        if r.account_stale and r.account in stale_since
        else r
        for r in rows
    ]


def _fetch() -> HoldingsResponse:
    per_acct = broker_apis.fetch_holdings()
    # Outage detection: fetch_failed flag set on every frame. Empty per_acct
    # alone is a legitimate "no holdings" state — not an outage.
    if _is_full_outage(per_acct):
        raise Exception(
            "Broker (Kite) returned no holdings data — upstream Bad Gateway / outage"
        )
    stale_since_by_acct = _stale_since_map(per_acct)
    raw = _prepare_raw_frame(per_acct)
    if raw.empty:
        return HoldingsResponse(rows=[], summary=[], refreshed_at=timestamp_display())

    df = pl.from_pandas(raw)
    row_cols = [c for c in _ROW_COLS if c in df.columns]
    df_rows = df.select(row_cols)
    summary_df = _compute_summary_df(df)

    rows = [
        HoldingRow(**{k: (v if v is not None else 0) for k, v in r.items()})
        for r in df_rows.to_dicts()
    ]
    rows = _apply_stale_since_map(rows, stale_since_by_acct)
    summary = [
        HoldingsSummaryRow(**{k: (v if v is not None else 0) for k, v in r.items()})
        for r in summary_df.to_dicts()
    ]
    # Response-level stale_accounts — see PositionsResponse.stale_accounts
    # (breaker-open account rows served from LKG cache).
    stale_accts = sorted({r.account for r in rows if r.account_stale})
    return HoldingsResponse(
        rows=rows,
        summary=summary,
        refreshed_at=timestamp_display(),
        stale_accounts=stale_accts,
    )


class HoldingsController(Controller):
    path = "/api/holdings"

    @get("/")
    async def get_holdings(
        self, request: Request, fresh: bool = False, skip_ltp: bool = False,
    ) -> HoldingsResponse:
        try:
            # ── Closed-hours gate via canonical helper ──────────────────────
            # Holdings are long-dated (days–weeks) so their LTP doesn't
            # change between sessions.  closed_hours_or_broker decides
            # whether to call the broker or serve the daily_book snapshot.
            # `?fresh=1` bypasses the gate.
            # `?skip_ltp=1` — force snapshot path even when a segment is open.
            # RefreshButton sends this during both-markets-closed clicks so
            # the operator can still refresh cash/margins/holdings-metadata
            # without hitting the broker for LTPs.

            async def _snapshot_fn() -> HoldingsResponse:
                snap = await _holdings_snapshot()
                if snap is None:
                    return HoldingsResponse(rows=[], summary=[], refreshed_at=timestamp_display())
                return snap

            async def _broker_fn() -> HoldingsResponse:
                if fresh:
                    invalidate("holdings")
                    try:
                        from backend.brokers.broker_apis import (
                            _raw_cache_invalidate, dhan_next_poll_clear,
                            _use_conn_service,
                        )
                        _raw_cache_invalidate("holdings")
                        # Reset the Dhan interval gate so ?fresh=1 bypasses
                        # cold/warm cadence and always hits the broker.
                        # Under conn-service the _dhan_next_poll dict lives in
                        # conn_service's process — proxy the reset over UDS.
                        if _use_conn_service():
                            from backend.brokers.client.api import dhan_poll_reset_remote
                            await dhan_poll_reset_remote()
                        else:
                            dhan_next_poll_clear()
                    except Exception:
                        pass
                _resp = await get_or_fetch("holdings", _fetch, ttl_seconds=_TTL)
                # Per-exchange overlay — closed exchanges get snapshot LTP
                # + price_source="snapshot_*" + is_animating=False. Runs
                # on the broker path so NSE-closed / MCX-open windows serve
                # mixed live+snap.
                _new_rows = await _overlay_snapshot_for_closed_exchanges(list(_resp.rows))
                import msgspec as _msc
                return _msc.structs.replace(_resp, rows=_new_rows)

            # ?skip_ltp=1 — RefreshButton's both-closed click. Runs the
            # normal broker path so holdings metadata refreshes (qty +
            # avg_cost change on corporate actions, dividend credits,
            # delivery-to-holdings transitions); the row-level overlay
            # in _broker_fn tags every closed-exchange row with
            # price_source='snapshot_*' and freezes its last_price to
            # the daily_book close_settled value. Funds stays on its own
            # broker path in parallel.
            if skip_ltp:
                resp = await _broker_fn()
            elif not fresh:
                resp, source = await closed_hours_or_broker(
                    exchange="NSE",
                    snapshot_fn=_snapshot_fn,
                    broker_fn=_broker_fn,
                    fallback_to_snapshot_on_broker_error=True,
                    route_key="holdings",
                )
                if source not in ("live", "stale-live") and getattr(resp, "as_of", None):
                    logger.info(
                        f"holdings: market closed ({source}) — serving daily_book snapshot"
                    )
                    role = normalise_role(resolve_role_from_connection(request))
                    if role == "trader":
                        allowed, _ = await user_scope_for_connection(request)
                        allowed_set = {str(a).upper() for a in (allowed or [])}
                        import msgspec
                        resp = msgspec.structs.replace(
                            resp,
                            rows=[r for r in resp.rows
                                  if str(getattr(r, "account", "")).upper() in allowed_set],
                            summary=[s for s in resp.summary
                                     if str(getattr(s, "account", "")).upper() in allowed_set
                                     or str(getattr(s, "account", "")).upper() == "TOTAL"],
                        )
                    if not is_admin_request(request):
                        import msgspec
                        def _mask_snap(row):
                            return msgspec.structs.replace(row, account=mask_account(row.account))
                        resp = msgspec.structs.replace(
                            resp,
                            rows=[_mask_snap(r) for r in resp.rows],
                            summary=[_mask_snap(s) for s in resp.summary],
                        )
                    return resp
                # Market is open (or stale-live), or no snapshot exists yet —
                # continue to live path (resp already holds broker or stale-live).
                if source not in ("live", "stale-live"):
                    resp = await _broker_fn()
            else:
                # ?fresh=1 — bypass closed-hours gate entirely
                resp = await _broker_fn()
            # Horizontal scoping (slice 5) — trader sees only their
            # assigned_accounts. Firm-wide roles untouched. Filter
            # BEFORE masking so the trader's assigned-list match
            # works against unmasked codes.
            role = normalise_role(resolve_role_from_connection(request))
            if role == "trader":
                allowed, _ = await user_scope_for_connection(request)
                allowed_set = {str(a).upper() for a in (allowed or [])}
                import msgspec
                resp = msgspec.structs.replace(
                    resp,
                    rows=[r for r in resp.rows
                          if str(getattr(r, "account", "")).upper() in allowed_set],
                    summary=[s for s in resp.summary
                             if str(getattr(s, "account", "")).upper() in allowed_set
                             or str(getattr(s, "account", "")).upper() == "TOTAL"],
                )
            # Account-ID masking — admin/designated only see raw codes.
            # Copy-not-mutate so the shared cache doesn't end up holding
            # masked codes (was the demo→signin lag bug).
            if not is_admin_request(request):
                import msgspec
                def _mask(row):
                    return msgspec.structs.replace(
                        row, account=mask_account(row.account)
                    )
                return msgspec.structs.replace(
                    resp,
                    rows=[_mask(r) for r in resp.rows],
                    summary=[_mask(s) for s in resp.summary],
                )
            return resp
        except Exception as e:
            logger.error(f"Holdings API error: {e}")
            if _is_broker_outage(e):
                raise HTTPException(
                    status_code=503,
                    detail="Broker (Kite) is temporarily unavailable. Try again shortly.",
                )
            raise HTTPException(status_code=500, detail=str(e))
