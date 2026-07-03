"""Funds endpoint — returns margins / cash / available margin per account."""

import pandas as pd
import polars as pl
from litestar import Controller, Request, get
from litestar.exceptions import HTTPException

from backend.api.auth_guard import is_admin_request
from backend.api.rbac import (
    resolve_role_from_connection, user_scope_for_connection, normalise_role,
)
from backend.api.cache import get_or_fetch, invalidate
from backend.api.schemas import FundsResponse, FundsRow
from backend.brokers import broker_apis
from backend.shared.helpers.date_time_utils import timestamp_display
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import mask_account

logger = get_logger(__name__)

_TTL = 30


def _is_broker_outage(err: Exception) -> bool:
    """Detect Kite (Zerodha) upstream HTTP gateway errors so we can
    surface a more specific message than the generic "Server busy".
    Kite returns plain 502/503/504 HTML pages during their periodic
    backend wobbles; the broker_apis helper logs them verbatim, so
    the resulting Exception string carries the marker text."""
    s = str(err).lower()
    return any(needle in s for needle in (
        'bad gateway', '502', '503', '504',
        'service unavailable', 'gateway timeout',
    ))

_COL_MAP = {
    'avail opening_balance': 'cash',
    'avail cash':            'live_cash',       # = avail.live_balance — current cash
    'net':                   'avail_margin',
    'util debits':           'used_margin',
    'util option_premium':   'option_premium',  # cash spent on currently-held long options
    'avail collateral':      'collateral',
}


def _fetch() -> FundsResponse:
    raw = pd.concat(broker_apis.fetch_margins(), ignore_index=True)
    # broker_apis.fetch_margins swallows Kite HTTP errors internally and
    # returns empty per-account frames on outage. An empty concat result
    # means EVERY account's call failed — signal of an upstream outage,
    # not a real "no data" state. Raise with the marker text so the
    # route's _is_broker_outage detector flips us to a 503 + clear msg.
    if raw.empty:
        raise Exception("Broker (Kite) returned no margin data — upstream Bad Gateway / outage")
    # Account masking removed — admin-only pages show real account IDs

    numeric = raw.select_dtypes(include='number').columns
    raw[numeric] = raw[numeric].fillna(0)
    df = pl.from_pandas(raw)

    # Rename broker column names to schema names
    rename = {k: v for k, v in _COL_MAP.items() if k in df.columns}
    df = df.rename(rename)

    numeric = ['cash', 'live_cash', 'avail_margin', 'used_margin', 'option_premium', 'collateral']
    present = [c for c in numeric if c in df.columns]

    totals = df.select(present).sum().with_columns(pl.lit('TOTAL').alias('account'))
    df_all = pl.concat([df.select(['account', *present]), totals], how='diagonal') \
               .fill_nan(0).fill_null(0)

    # Derived columns — computed after TOTAL aggregation so the TOTAL row
    # also carries correct derived values.
    #   available_funds = avail_margin  (broker's "net" — free margin for new trades)
    #   available_cash  = cash − option_premium  (SOD cash net of locked long-option premiums)
    cash_col   = pl.col('cash') if 'cash' in df_all.columns else pl.lit(0.0)
    prem_col   = pl.col('option_premium') if 'option_premium' in df_all.columns else pl.lit(0.0)
    avail_col  = pl.col('avail_margin') if 'avail_margin' in df_all.columns else pl.lit(0.0)
    df_all = df_all.with_columns([
        avail_col.alias('available_funds'),
        (cash_col - prem_col).alias('available_cash'),
    ])

    rows = [FundsRow(**r) for r in df_all.to_dicts()]
    return FundsResponse(rows=rows, refreshed_at=timestamp_display())


class FundsController(Controller):
    path = "/api/funds"

    @get("/")
    async def get_funds(self, request: Request, fresh: bool = False) -> FundsResponse:
        try:
            if fresh:
                invalidate("funds")
                # Also drop the raw-DataFrame cache so the refetch below
                # sees fresh broker state (matches positions / holdings).
                try:
                    from backend.brokers.broker_apis import (
                        _raw_cache_invalidate, dhan_next_poll_clear,
                        _use_conn_service,
                    )
                    _raw_cache_invalidate("margins")
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
            resp = await get_or_fetch("funds", _fetch, ttl_seconds=_TTL)
            # Horizontal scoping (slice 5) — trader sees only their
            # assigned_accounts. Firm-wide roles untouched. TOTAL row
            # is always preserved so the page-level rollup remains
            # meaningful even after a scoped filter.
            role = normalise_role(resolve_role_from_connection(request))
            if role == "trader":
                allowed, _ = await user_scope_for_connection(request)
                allowed_set = {str(a).upper() for a in (allowed or [])}
                import msgspec
                resp = msgspec.structs.replace(
                    resp,
                    rows=[r for r in resp.rows
                          if str(getattr(r, "account", "")).upper() in allowed_set
                          or str(getattr(r, "account", "")).upper() == "TOTAL"],
                )
            # Account-ID masking — admin/designated only see raw codes.
            # Copy-not-mutate so the shared cache doesn't end up holding
            # masked codes (was the demo→signin lag bug).
            if not is_admin_request(request):
                import msgspec
                def _mask(row):
                    if row.account == 'TOTAL':
                        return row
                    return msgspec.structs.replace(
                        row, account=mask_account(row.account)
                    )
                return msgspec.structs.replace(
                    resp,
                    rows=[_mask(r) for r in resp.rows],
                )
            return resp
        except Exception as e:
            logger.error(f"Funds API error: {e}")
            if _is_broker_outage(e):
                raise HTTPException(
                    status_code=503,
                    detail="Broker (Kite) is temporarily unavailable. Try again shortly.",
                )
            raise HTTPException(status_code=500, detail=str(e))
