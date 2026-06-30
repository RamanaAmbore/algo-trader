"""Holdings endpoint — returns per-account rows and summary."""

import pandas as pd
import polars as pl
from litestar import Controller, get
from litestar.exceptions import HTTPException
from typing import Optional

from litestar import Request

from backend.api.auth_guard import is_admin_request, is_authenticated_request
from backend.api.rbac import (
    resolve_role_from_connection, user_scope_for_connection, normalise_role,
)
from backend.api.algo.pnl_math import recompute_row_percentages
from backend.api.cache import get_or_fetch, invalidate
from backend.api.helpers.ltp_patch import apply_ltp_patch, holdings_policy
from backend.api.schemas import HoldingsResponse, HoldingRow, HoldingsSummaryRow
from backend.brokers import broker_apis
from backend.shared.helpers.date_time_utils import timestamp_display
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import mask_account, mask_column

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Closed-hours snapshot helpers
# ---------------------------------------------------------------------------

async def _is_all_markets_closed() -> bool:
    """Return True when every configured market segment is currently closed."""
    try:
        from backend.shared.helpers.date_time_utils import is_any_segment_open, timestamp_indian
        return not is_any_segment_open(timestamp_indian())
    except Exception:
        return False  # fail-open: assume market is open so live path runs


async def _holdings_snapshot() -> Optional[HoldingsResponse]:
    """Read the most-recent pre-today daily_book[kind='holdings'] snapshot
    and reconstruct a HoldingsResponse from it.

    Returns None when no snapshot exists or the DB query fails.
    """
    from backend.api.database import async_session
    from sqlalchemy import text as _sql_text
    from backend.shared.helpers.date_time_utils import timestamp_indian

    today_ist_midnight = timestamp_indian().replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    try:
        async with async_session() as session:
            result = await session.execute(_sql_text("""
                SELECT account, symbol, exchange, qty, avg_cost, ltp,
                       day_pnl, total_pnl, captured_at
                FROM daily_book
                WHERE kind = 'holdings' AND ltp IS NOT NULL
                  AND captured_at < :today_open
                ORDER BY captured_at DESC
                LIMIT 5000
            """), {"today_open": today_ist_midnight})
            raw_rows = result.all()
    except Exception as exc:
        logger.warning(f"holdings snapshot query failed: {exc}")
        return None

    if not raw_rows:
        return None

    snap_captured_at: str = raw_rows[0][8].isoformat() if raw_rows[0][8] else ""

    inv_by_account: dict[str, float] = {}
    cur_by_account: dict[str, float] = {}
    pnl_by_account: dict[str, float] = {}
    dcv_by_account: dict[str, float] = {}

    rows: list[HoldingRow] = []
    for (account, symbol, exchange, qty, avg_cost, ltp,
         day_pnl, total_pnl, captured_at) in raw_rows:
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
        )
        rows.append(row)

        acct = str(account)
        inv_by_account[acct] = inv_by_account.get(acct, 0.0) + inv_val
        cur_by_account[acct] = cur_by_account.get(acct, 0.0) + cur_val
        pnl_by_account[acct] = pnl_by_account.get(acct, 0.0) + total_pnl_f
        dcv_by_account[acct] = dcv_by_account.get(acct, 0.0) + day_pnl_f

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


def _fetch() -> HoldingsResponse:
    per_acct = broker_apis.fetch_holdings()
    # Outage detection: only raise when every per-account call failed
    # (`fetch_failed` flag set in broker_apis.py). Empty without the
    # flag = legitimate "no holdings" state — operator who hasn't
    # taken delivery yet, or holds only F&O positions. Returning
    # outage on this produced a false "Holdings unavailable" banner
    # on /performance.
    if per_acct and all(df.attrs.get('fetch_failed', False) for df in per_acct):
        raise Exception("Broker (Kite) returned no holdings data — upstream Bad Gateway / outage")
    raw = pd.concat(per_acct, ignore_index=True) if per_acct else pd.DataFrame()
    if raw.empty:
        return HoldingsResponse(rows=[], summary=[], refreshed_at=timestamp_display())
    # Account masking removed — admin-only pages show real account IDs

    # Backfill missing market data (close_price + last_price) for
    # adapters that don't populate them. Source brokers keep their
    # account-specific facts (avg_price, qty, opening_qty, realised);
    # market data routes through PriceBroker.quote (prefers Kite) so
    # Dhan + Groww rows match Kite's Day P&L / Day % / Prev Close
    # downstream. Single batched round-trip across every missing row.
    broker_apis.backfill_market_data(raw)

    # Patch any rows that still have last_price=0 after backfill
    # (PriceBroker rate-limit cool-off, or symbol not in quote cache).
    # Same live-ticker override pattern used by the positions route.
    _override_stale_ltp_from_ticker(raw)

    numeric = raw.select_dtypes(include='number').columns
    raw[numeric] = raw[numeric].fillna(0)
    df = pl.from_pandas(raw)

    row_cols = [c for c in _ROW_COLS if c in df.columns]
    df_rows = df.select(row_cols)

    sum_cols = [c for c in ['inv_val', 'cur_val', 'pnl', 'day_change_val'] if c in df.columns]
    grouped = (
        df.group_by('account')
          .agg([pl.col(c).sum() for c in sum_cols])
    )
    # day_change_percentage uses YESTERDAY's value (cur_val - day_change_val)
    # as the denominator — Kite's convention for "today moved X% off the
    # previous close". Using cur_val (which already includes today's gain)
    # would understate the move on positive days and overstate on negative.
    grouped = grouped.with_columns([
        (pl.col('pnl')            / pl.col('inv_val')                                * 100).alias('pnl_percentage'),
        (pl.col('day_change_val') / (pl.col('cur_val') - pl.col('day_change_val'))   * 100).alias('day_change_percentage'),
    ])

    totals = grouped.select(sum_cols).sum().with_columns([
        pl.lit('TOTAL').alias('account'),
        (pl.col('pnl')            / pl.col('inv_val')                                * 100).alias('pnl_percentage'),
        (pl.col('day_change_val') / (pl.col('cur_val') - pl.col('day_change_val'))   * 100).alias('day_change_percentage'),
    ])
    summary_df = pl.concat([grouped, totals], how='diagonal').fill_nan(0).fill_null(0)

    rows = [
        HoldingRow(**{k: (v if v is not None else 0) for k, v in r.items()})
        for r in df_rows.to_dicts()
    ]
    summary = [
        HoldingsSummaryRow(**{k: (v if v is not None else 0) for k, v in r.items()})
        for r in summary_df.to_dicts()
    ]
    return HoldingsResponse(rows=rows, summary=summary, refreshed_at=timestamp_display())


class HoldingsController(Controller):
    path = "/api/holdings"

    @get("/")
    async def get_holdings(self, request: Request, fresh: bool = False) -> HoldingsResponse:
        try:
            # ── Closed-hours fast-path ──────────────────────────────────────
            # Holdings are long-dated (days–weeks) so their LTP doesn't
            # change between sessions.  When all segments are closed, return
            # the persisted daily_book[kind='holdings'] snapshot instead of
            # calling the broker.  `?fresh=1` bypasses the guard.
            if not fresh and await _is_all_markets_closed():
                snap = await _holdings_snapshot()
                if snap is not None:
                    logger.info("holdings: market closed — serving daily_book snapshot")
                    role = normalise_role(resolve_role_from_connection(request))
                    if role == "trader":
                        allowed, _ = await user_scope_for_connection(request)
                        allowed_set = {str(a).upper() for a in (allowed or [])}
                        import msgspec
                        snap = msgspec.structs.replace(
                            snap,
                            rows=[r for r in snap.rows
                                  if str(getattr(r, "account", "")).upper() in allowed_set],
                            summary=[s for s in snap.summary
                                     if str(getattr(s, "account", "")).upper() in allowed_set
                                     or str(getattr(s, "account", "")).upper() == "TOTAL"],
                        )
                    if not is_admin_request(request):
                        import msgspec
                        def _mask_snap(row):
                            return msgspec.structs.replace(row, account=mask_account(row.account))
                        snap = msgspec.structs.replace(
                            snap,
                            rows=[_mask_snap(r) for r in snap.rows],
                            summary=[_mask_snap(s) for s in snap.summary],
                        )
                    return snap

            # `?fresh=1` — Refresh button bypasses the TTL cache and
            # forces a live broker fetch. The cache's per-key lock still
            # coalesces multiple simultaneous refresh clicks into one
            # broker call. Demo + public flows share this path — real
            # data, accounts masked for non-admin callers below.
            if fresh:
                invalidate("holdings")
                # Also drop the raw-DataFrame cache so the refetch
                # below sees fresh broker state (matches positions.py).
                try:
                    from backend.brokers.broker_apis import _raw_cache_invalidate
                    _raw_cache_invalidate("holdings")
                except Exception:
                    pass
            resp = await get_or_fetch("holdings", _fetch, ttl_seconds=_TTL)
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
