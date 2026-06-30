"""Holdings endpoint — returns per-account rows and summary."""

import pandas as pd
import polars as pl
from litestar import Controller, get
from litestar.exceptions import HTTPException

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
