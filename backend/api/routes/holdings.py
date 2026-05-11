"""Holdings endpoint — returns per-account rows and summary."""

import pandas as pd
import polars as pl
from litestar import Controller, get
from litestar.exceptions import HTTPException

from litestar import Request

from backend.api.auth_guard import is_admin_request
from backend.api.cache import get_or_fetch, invalidate
from backend.api.schemas import HoldingsResponse, HoldingRow, HoldingsSummaryRow
from backend.shared.helpers import broker_apis
from backend.shared.helpers.date_time_utils import timestamp_display
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import mask_column

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
]

_TTL = 30  # seconds — background task invalidates on each refresh


def _fetch() -> HoldingsResponse:
    raw = pd.concat(broker_apis.fetch_holdings(), ignore_index=True)
    if raw.empty:
        raise Exception("Broker (Kite) returned no holdings data — upstream Bad Gateway / outage")
    # Account masking removed — admin-only pages show real account IDs

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
            resp = await get_or_fetch("holdings", _fetch, ttl_seconds=_TTL)
            if not is_admin_request(request):
                for r in resp.rows:
                    r.account = mask_column(pd.Series([r.account]))[0]
                for s in resp.summary:
                    s.account = mask_column(pd.Series([s.account]))[0]
            return resp
        except Exception as e:
            logger.error(f"Holdings API error: {e}")
            if _is_broker_outage(e):
                raise HTTPException(
                    status_code=503,
                    detail="Broker (Kite) is temporarily unavailable. Try again shortly.",
                )
            raise HTTPException(status_code=500, detail=str(e))
