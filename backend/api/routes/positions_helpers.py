"""Pure helper functions extracted from positions.py to reduce cyclomatic complexity.

Three groups:
  1. build_summary_from_rows  — shared summary builder (was duplicated 3×)
  2. Snapshot helpers         — extract_snapshot_extras, resolve_snapshot_day_pnl,
                                build_snapshot_row  (_positions_snapshot seams)
  3. Response shaping         — apply_scope_and_mask, merge_paper_into_live
                                (get_positions seams)
"""

from __future__ import annotations

import json as _json
from typing import Optional

import msgspec

from backend.api.auth_guard import is_admin_request
from backend.api.rbac import (
    normalise_role,
    resolve_role_from_connection,
    user_scope_for_connection,
)
from backend.api.schemas import PositionRow, PositionsResponse, PositionsSummaryRow
from backend.shared.helpers.date_time_utils import timestamp_display
from backend.shared.helpers.utils import mask_account


# ---------------------------------------------------------------------------
# 1. Summary builder — SSOT (was copied verbatim in _positions_snapshot,
#    _build_paper_positions_response, and the mode=both merge in get_positions)
# ---------------------------------------------------------------------------

def build_summary_from_rows(
    rows: list[PositionRow],
) -> list[PositionsSummaryRow]:
    """Aggregate per-account sums + TOTAL row from a list of PositionRow structs.

    `day_prev_val` = Σ |close_price × quantity| per account (denominator for
    day_change_percentage).  This matches the polars expression used by _fetch()
    so the two paths never diverge.
    """
    pnl_by_account: dict[str, float] = {}
    dcv_by_account: dict[str, float] = {}
    prev_by_account: dict[str, float] = {}

    for row in rows:
        acct = row.account
        pnl_by_account[acct]  = pnl_by_account.get(acct, 0.0) + row.pnl
        dcv_by_account[acct]  = dcv_by_account.get(acct, 0.0) + row.day_change_val
        prev_by_account[acct] = (
            prev_by_account.get(acct, 0.0)
            + abs(row.close_price * row.quantity)
        )

    summary: list[PositionsSummaryRow] = []
    total_pnl_sum = 0.0
    total_dcv_sum = 0.0
    for acct, pnl_sum in pnl_by_account.items():
        dcv_sum  = dcv_by_account.get(acct, 0.0)
        prev_sum = prev_by_account.get(acct, 0.0)
        pct = dcv_sum / prev_sum * 100.0 if prev_sum else 0.0
        summary.append(PositionsSummaryRow(
            account=acct,
            pnl=pnl_sum,
            day_change_val=dcv_sum,
            day_change_percentage=pct,
            day_prev_val=prev_sum,
        ))
        total_pnl_sum += pnl_sum
        total_dcv_sum += dcv_sum

    total_prev = sum(prev_by_account.values())
    summary.append(PositionsSummaryRow(
        account="TOTAL",
        pnl=total_pnl_sum,
        day_change_val=total_dcv_sum,
        day_change_percentage=(
            total_dcv_sum / total_prev * 100.0 if total_prev else 0.0
        ),
        day_prev_val=total_prev,
    ))
    return summary


# ---------------------------------------------------------------------------
# 2. Snapshot helpers  (_positions_snapshot seams)
# ---------------------------------------------------------------------------

def extract_snapshot_extras(payload_json: object) -> dict:
    """Return the ``snapshot_extras`` sub-dict from a daily_book payload_json
    column value (dict or JSON string).  Returns {} on any parse failure.
    """
    if not payload_json:
        return {}
    try:
        pj = payload_json if isinstance(payload_json, dict) else _json.loads(payload_json)
        if isinstance(pj, dict):
            extras = pj.get("snapshot_extras")
            if isinstance(extras, dict):
                return extras
    except Exception:
        pass
    return {}


def resolve_snapshot_day_pnl(
    day_pnl_col: object,
    day_pnl_f: float,
    extras: dict,
) -> float:
    """Return the effective day_pnl_f for a snapshot row.

    When the top-level ``day_pnl`` column is NULL (mid-session gate erased it),
    fall back to ``snapshot_extras.day_change_val``.  A legitimate 0.0 from the
    writer always wins over the extras fallback.
    """
    if day_pnl_col is None:
        raw = extras.get("day_change_val")
        if raw is not None:
            try:
                return float(raw)
            except (TypeError, ValueError):
                pass
    return day_pnl_f


def resolve_snapshot_day_pct(
    day_pnl_col: object,
    day_pnl_f: float,
    ltp_f: float,
    qty_i: int,
    inv_val: float,
    extras: dict,
) -> float:
    """Return the effective day_change_percentage for a snapshot row.

    Prefers ``snapshot_extras.day_change_pct`` when the column was NULL
    (same condition as ``resolve_snapshot_day_pnl``).  Falls back to
    computed value when extras don't have the key.
    """
    ex_pct = extras.get("day_change_pct") if day_pnl_col is None else None
    if ex_pct is not None:
        try:
            return float(ex_pct)
        except (TypeError, ValueError):
            pass
    close_notional = abs(ltp_f * qty_i)
    if close_notional:
        return day_pnl_f / close_notional * 100.0
    return day_pnl_f / inv_val * 100.0 if inv_val else 0.0


def build_snapshot_position_row(
    account: object,
    symbol: object,
    exchange: object,
    qty: object,
    avg_cost: object,
    ltp: object,
    day_pnl: object,
    total_pnl: object,
    extras: dict,
) -> PositionRow:
    """Construct a PositionRow from raw daily_book snapshot columns.

    All financial calculations here mirror the writer logic in
    ``daily_snapshot.py`` so closed-hours readers are always consistent
    with what was persisted.
    """
    avg_cost_f  = float(avg_cost)  if avg_cost  is not None else 0.0
    ltp_f       = float(ltp)       if ltp       is not None else 0.0
    total_pnl_f = float(total_pnl) if total_pnl is not None else 0.0
    day_pnl_f   = float(day_pnl)   if day_pnl   is not None else 0.0
    qty_i       = int(qty)         if qty       is not None else 0

    day_pnl_f = resolve_snapshot_day_pnl(day_pnl, day_pnl_f, extras)

    inv_val = abs(avg_cost_f * qty_i)
    pnl_pct = (total_pnl_f / inv_val * 100.0) if inv_val else 0.0
    day_pct = resolve_snapshot_day_pct(day_pnl, day_pnl_f, ltp_f, qty_i, inv_val, extras)

    return PositionRow(
        account=str(account),
        tradingsymbol=str(symbol),
        exchange=str(exchange or ""),
        product="NRML",
        quantity=qty_i,
        average_price=avg_cost_f,
        close_price=ltp_f,
        last_price=ltp_f,
        pnl=total_pnl_f,
        pnl_percentage=pnl_pct,
        day_change_val=day_pnl_f,
        day_change_percentage=day_pct,
        overnight_quantity=qty_i,
        last_price_stale=True,
        price_source="snapshot_settled",
        current_price=ltp_f,
        is_animating=False,
    )


# ---------------------------------------------------------------------------
# 3. Response shaping (get_positions seams)
# ---------------------------------------------------------------------------

async def apply_scope_and_mask(
    resp: PositionsResponse,
    request: object,
) -> PositionsResponse:
    """Apply trader-role account scoping then admin/non-admin account masking.

    IMPORTANT: always builds new lists via msgspec.structs.replace so the
    cached PositionsResponse object is never mutated in place (the cache
    returns the same object reference across requests — mutation would
    poison subsequent callers).

    Returns a possibly-narrowed / masked PositionsResponse.
    """
    role = normalise_role(resolve_role_from_connection(request))
    if role == "trader":
        allowed, _ = await user_scope_for_connection(request)
        allowed_set = {str(a).upper() for a in (allowed or [])}
        resp = msgspec.structs.replace(
            resp,
            rows=[r for r in resp.rows
                  if str(getattr(r, "account", "")).upper() in allowed_set],
            summary=[s for s in resp.summary
                     if str(getattr(s, "account", "")).upper() in allowed_set
                     or str(getattr(s, "account", "")).upper() == "TOTAL"],
        )

    if not is_admin_request(request):
        def _mask(row: object) -> object:
            return msgspec.structs.replace(row, account=mask_account(row.account))  # type: ignore[attr-defined]

        resp = msgspec.structs.replace(
            resp,
            rows=[_mask(r) for r in resp.rows],
            summary=[_mask(s) for s in resp.summary],
        )
    return resp


def merge_paper_into_live(
    live_resp: PositionsResponse,
    paper_resp: PositionsResponse,
) -> PositionsResponse:
    """Union live + paper rows and recompute summary over the merged set.

    Live rows are tagged mode='live'; paper rows already carry mode='paper'.
    Returns a new PositionsResponse (does not mutate either input).
    """
    if not paper_resp.rows:
        return live_resp

    live_rows_tagged = [
        msgspec.structs.replace(r, mode="live") for r in live_resp.rows
    ]
    merged_rows = live_rows_tagged + list(paper_resp.rows)
    merged_summary = build_summary_from_rows(merged_rows)  # type: ignore[arg-type]
    return msgspec.structs.replace(
        live_resp,
        rows=merged_rows,
        summary=merged_summary,
    )
