"""Positions endpoint — returns per-account rows and summary."""

import pandas as pd
import polars as pl
from litestar import Controller, Request, get
from litestar.exceptions import HTTPException

from backend.api.auth_guard import is_admin_request, is_authenticated_request
from backend.api.cache import get_or_fetch, invalidate
from backend.api.schemas import PositionsResponse, PositionRow, PositionsSummaryRow
from backend.shared.helpers import broker_apis
from backend.shared.helpers.date_time_utils import timestamp_display
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import mask_column

logger = get_logger(__name__)

_ROW_COLS = [
    'account', 'tradingsymbol', 'exchange', 'product',
    'quantity', 'average_price', 'close_price', 'last_price',
    'pnl', 'pnl_percentage', 'unrealised', 'realised',
    'day_change', 'day_change_val', 'day_change_percentage',
]

_TTL = 30


def _is_broker_outage(err: Exception) -> bool:
    """Detect Kite (Zerodha) upstream HTTP gateway errors. See
    funds.py for the rationale — same helper, same patterns."""
    s = str(err).lower()
    return any(needle in s for needle in (
        'bad gateway', '502', '503', '504',
        'service unavailable', 'gateway timeout',
    ))


def _fetch() -> PositionsResponse:
    raw = pd.concat(broker_apis.fetch_positions(), ignore_index=True)
    if raw.empty:
        raise Exception("Broker (Kite) returned no positions data — upstream Bad Gateway / outage")
    # Account masking removed — admin-only pages show real account IDs

    numeric = raw.select_dtypes(include='number').columns
    raw[numeric] = raw[numeric].fillna(0)
    df = pl.from_pandas(raw)

    row_cols = [c for c in _ROW_COLS if c in df.columns]
    df_rows = df.select(row_cols)

    # Compute prev_val per row so we can sum it per account and derive a
    # meaningful day_change_percentage on the summary (sum Δ / sum prev).
    df = df.with_columns(
        (pl.col('close_price') * pl.col('quantity')).abs().alias('_prev_val')
    )
    sum_cols = [c for c in ('pnl', 'day_change_val', '_prev_val') if c in df.columns]
    if sum_cols:
        grouped = df.group_by('account').agg([pl.col(c).sum() for c in sum_cols])
    else:
        grouped = pl.DataFrame({'account': []})
    # Ensure all sum columns exist even when absent from the broker frame
    for col in ('pnl', 'day_change_val', '_prev_val'):
        if col not in grouped.columns:
            grouped = grouped.with_columns(pl.lit(0.0).alias(col))
    totals = pl.DataFrame([{
        'account': 'TOTAL',
        'pnl': grouped['pnl'].sum(),
        'day_change_val': grouped['day_change_val'].sum(),
        '_prev_val': grouped['_prev_val'].sum(),
    }])
    summary_df = pl.concat([grouped, totals], how='diagonal').fill_nan(0).fill_null(0)
    # day_change_percentage = Σ day_change_val / Σ |close × qty|, per-row's
    # absolute denominator captured above. Drop the helper column afterwards.
    summary_df = summary_df.with_columns(
        (pl.col('day_change_val') / pl.col('_prev_val').replace(0, None) * 100)
        .fill_nan(0).fill_null(0)
        .alias('day_change_percentage')
    ).drop('_prev_val')

    rows = [
        PositionRow(**{k: (v if v is not None else 0) for k, v in r.items()})
        for r in df_rows.to_dicts()
    ]
    summary = [
        PositionsSummaryRow(**{k: (v if v is not None else 0) for k, v in r.items()})
        for r in summary_df.to_dicts()
    ]
    return PositionsResponse(rows=rows, summary=summary, refreshed_at=timestamp_display())


class PositionsController(Controller):
    path = "/api/positions"

    @get("/")
    async def get_positions(self, request: Request, fresh: bool = False) -> PositionsResponse:
        try:
            # Demo + public flow share one path: real broker data via
            # the cached fetch, with accounts masked for non-admin
            # callers (existing behaviour from the public /performance
            # page). No synthetic data — demo visitors see real
            # positions with `ZG####` style masks.
            if fresh:
                invalidate("positions")
            resp = await get_or_fetch("positions", _fetch, ttl_seconds=_TTL)
            # Mask account IDs for everyone who is NOT admin/designated.
            # Partner-tier authenticated users see masked codes (ZG####)
            # just like demo visitors — they have no need to see raw
            # broker account codes. is_admin_request() returns True only
            # for role ∈ {admin, designated}.
            if not is_admin_request(request):
                for r in resp.rows:
                    r.account = mask_column(pd.Series([r.account]))[0]
                for s in resp.summary:
                    s.account = mask_column(pd.Series([s.account]))[0]
            return resp
        except Exception as e:
            logger.error(f"Positions API error: {e}")
            if _is_broker_outage(e):
                raise HTTPException(
                    status_code=503,
                    detail="Broker (Kite) is temporarily unavailable. Try again shortly.",
                )
            raise HTTPException(status_code=500, detail=str(e))
