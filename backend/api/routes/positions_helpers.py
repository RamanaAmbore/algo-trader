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
# Shared previous-close resolution helpers (used by both holdings and positions)
# ---------------------------------------------------------------------------

def _resolve_previous_close(
    pc_f: float,
    ltp_f: float,
    backup_f: float,
    prev_ltp_f: "float | None" = None,
) -> float:
    """Return a corrected previous_close, falling back when NULL or ≈ ltp.

    When `pc_f` is zero/missing or equal to `ltp_f` within 0.01 (rolling-shift
    corruption where previous_close was overwritten by the current LTP), try
    `backup_f` first, then `prev_ltp_f` (prior snapshot batch LTP). Returns
    the original `pc_f` when no correction is needed.
    """
    if pc_f <= 0 or (ltp_f > 0 and abs(pc_f - ltp_f) < 0.01):
        if backup_f > 0 and abs(backup_f - ltp_f) >= 0.01:
            return backup_f
        if prev_ltp_f is not None and prev_ltp_f > 0:
            return prev_ltp_f
    return pc_f


def _parse_overnight_qty(payload_json, fallback_qty: float) -> float:
    """Return overnight_quantity from payload_json, falling back to fallback_qty."""
    try:
        pj = _json.loads(payload_json) if isinstance(payload_json, str) else payload_json
        pj = pj if isinstance(pj, dict) else {}
    except (_json.JSONDecodeError, ValueError, TypeError):
        pj = {}
    oq_raw = pj.get("overnight_quantity")
    return float(oq_raw) if oq_raw is not None else float(fallback_qty)


def _compute_snapshot_day_pnl(
    actual_pc: "float | None",
    total_pnl: float,
    avg: float,
    oq: float,
    day_pnl_raw,
) -> "float | None":
    """Compute day_pnl from the universal formula when prev_close is available.

    Formula: day_pnl = total_pnl - (prev_close - avg) * oq
      Overnight open (oq=qty):  (ltp-avg)*oq - (prev-avg)*oq = (ltp-prev)*oq ✓
      New today (oq=0):         total_pnl - 0 = realised ✓
      Closed overnight (oq>0):  realised - (prev-avg)*oq = (exit-prev)*oq ✓

    Falls back to `day_pnl_raw` when prev_close is unavailable.
    """
    if actual_pc and actual_pc > 0:
        return total_pnl - (actual_pc - avg) * oq
    return day_pnl_raw


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
    close_price_f: float | None = None,
) -> float:
    """Return the effective day_change_percentage for a snapshot row.

    Prefers ``snapshot_extras.day_change_pct`` when the column was NULL
    (same condition as ``resolve_snapshot_day_pnl``).  Falls back to
    computed value when extras don't have the key.

    ``close_price_f`` — the prior-session settlement price to use as the
    denominator for day-change percentage.  When provided and > 0, used
    instead of ``ltp_f`` so the denominator is the correct opening value
    (not today's LTP, which would understate the move).  Falls back to
    ``ltp_f`` when close_price_f is None or zero (same-day buys / cold-boot).
    """
    ex_pct = extras.get("day_change_pct") if day_pnl_col is None else None
    if ex_pct is not None:
        try:
            return float(ex_pct)
        except (TypeError, ValueError):
            pass
    # Use the prior-session close as denominator when available (prevents
    # understating the move when LTP has moved significantly from close).
    denom_price = (
        close_price_f
        if close_price_f is not None and close_price_f > 0
        else ltp_f
    )
    close_notional = abs(denom_price * qty_i)
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
    *,
    previous_close: float | None = None,
    prev_settlement_pnl: float | None = None,
    product: str = "NRML",
    overnight_quantity: int | None = None,
) -> PositionRow:
    """Construct a PositionRow from raw daily_book snapshot columns.

    All financial calculations here mirror the writer logic in
    ``daily_snapshot.py`` so closed-hours readers are always consistent
    with what was persisted.

    ``previous_close`` — when provided and > 0, used as ``close_price``
    instead of LTP. This is the prior-session official settlement captured
    at the first snapshot of the day and frozen via COALESCE in the UPSERT.
    The frontend's ``baseDayPnlForPosition`` reads ``close_price`` to
    compute day-P&L; without this fix overnight positions always show 0.

    ``prev_settlement_pnl`` — frozen yesterday's total_pnl from the
    most-recent daily_book snapshot captured before midnight IST. When set,
    day P&L = total_pnl − prev_settlement_pnl (authoritative). When None,
    the frontend falls back to reconstructing day P&L from price data.

    ``product`` — the Kite product type (NRML / MIS / CNC). Extracted from
    ``payload_json.get("product", "NRML")`` at the call site. Defaults to
    "NRML" so existing callers without payload_json access are unaffected.
    """
    avg_cost_f  = float(avg_cost)  if avg_cost  is not None else 0.0
    ltp_f       = float(ltp)       if ltp       is not None else 0.0
    total_pnl_f = float(total_pnl) if total_pnl is not None else 0.0
    day_pnl_f   = float(day_pnl)   if day_pnl   is not None else 0.0
    qty_i       = int(qty)         if qty       is not None else 0

    day_pnl_f = resolve_snapshot_day_pnl(day_pnl, day_pnl_f, extras)

    inv_val = abs(avg_cost_f * qty_i)
    pnl_pct = (total_pnl_f / inv_val * 100.0) if inv_val else 0.0

    # Use the frozen prior-session settlement as close_price when available.
    # Must be computed BEFORE resolve_snapshot_day_pct so the correct
    # denominator (prior-session close × qty, not LTP × qty) is used.
    # Without this, close_price = LTP and baseDayPnlForPosition computes
    # total_pnl - oq×(ltp-ltp) = total_pnl - 0 which collapses correctly
    # only for new positions; for overnight positions the day-P&L becomes 0.
    close_price_f = (
        float(previous_close)
        if previous_close is not None and float(previous_close) > 0
        else ltp_f
    )

    day_pct = resolve_snapshot_day_pct(
        day_pnl, day_pnl_f, ltp_f, qty_i, inv_val, extras,
        close_price_f=close_price_f,
    )

    # overnight_quantity: use the value from payload_json when provided by the
    # caller (via build_row_from_snapshot_raw); fall back to qty_i so existing
    # call sites that don't pass overnight_quantity are unaffected.
    oq_i = int(overnight_quantity) if overnight_quantity is not None else qty_i

    return PositionRow(
        account=str(account),
        tradingsymbol=str(symbol),
        exchange=str(exchange or ""),
        product=product,
        quantity=qty_i,
        average_price=avg_cost_f,
        close_price=close_price_f,
        last_price=ltp_f,
        pnl=total_pnl_f,
        pnl_percentage=pnl_pct,
        day_change_val=day_pnl_f,
        day_change_percentage=day_pct,
        overnight_quantity=oq_i,
        last_price_stale=True,
        price_source="snapshot_settled",
        current_price=ltp_f,
        is_animating=False,
        prev_settlement_pnl=prev_settlement_pnl,
    )


def extract_snapshot_product(payload_json: object) -> str:
    """Return the Kite product type (NRML/MIS/CNC) from payload_json."""
    if not payload_json:
        return "NRML"
    try:
        pj = payload_json if isinstance(payload_json, dict) else _json.loads(payload_json)
        if isinstance(pj, dict):
            return pj.get("product", "NRML") or "NRML"
    except Exception:
        pass
    return "NRML"


def build_row_from_snapshot_raw(raw_row: tuple) -> PositionRow:
    """Build a PositionRow from a 14-column daily_book raw snapshot tuple.

    Column order: account, symbol, exchange, qty, avg_cost, ltp,
    day_pnl, total_pnl, payload_json, captured_at, previous_close,
    prev_ltp, prev_settlement_pnl, previous_close_backup.

    Extracted from ``_positions_snapshot`` to reduce that function's CC.
    """
    (account, symbol, exchange, qty, avg_cost, ltp,
     day_pnl, total_pnl, payload_json, _captured_at, previous_close,
     prev_ltp, prev_settlement_pnl) = raw_row[:13]
    previous_close_backup = raw_row[13] if len(raw_row) > 13 else None

    extras = extract_snapshot_extras(payload_json)
    # daily_book.qty is already in CONTRACTS — _positions_qty_fields converted
    # lots × lot_size before the row was written.  No multiplier needed here.
    effective_qty = qty or 0

    # `previous_close` is frozen by COALESCE on the first daily UPSERT and
    # never overwritten — it is the official prior-session settlement price.
    # `prev_ltp` is the most-recent batch LTP and converges toward the current
    # LTP during a session, which would make day_change ≈ 0.  Use
    # `actual_previous_close` as the primary reference and fall back to
    # `prev_ltp` only when `previous_close` is absent or zero.
    # Safety net: when previous_close was corrupted by the rolling-shift UPSERT
    # (i.e. previous_close == ltp), fall back to previous_close_backup (saved
    # before fix_daily_book_prev_close overwrote previous_close).
    _pc_raw = float(previous_close) if previous_close and float(previous_close) > 0 else 0.0
    _ltp_f  = float(ltp) if ltp else 0.0
    backup_f = float(previous_close_backup) if previous_close_backup else 0.0

    # Use the corruption-detection helper to resolve the final previous_close value.
    # prev_ltp is NOT passed here — positions falls back to prev_ltp only when
    # previous_close is completely absent (None/zero), not as a corruption fallback.
    # Corruption fallback (pc ≈ ltp) tries backup_f only; if no backup, keeps pc as-is.
    resolved_pc = _resolve_previous_close(_pc_raw, _ltp_f, backup_f)
    actual_previous_close = resolved_pc if resolved_pc > 0 else None
    prev_pnl_val = float(prev_settlement_pnl) if prev_settlement_pnl is not None else None
    # Universal day_pnl formula using overnight_quantity from payload_json.
    # Handles all position states (overnight open, new today, partial close,
    # fully closed intraday, fully closed overnight) when prev_close is known.
    # Formula: day_pnl = total_pnl - (prev_close - avg) * oq
    #   Overnight open (oq=qty):  (ltp-avg)*oq - (prev-avg)*oq = (ltp-prev)*oq ✓
    #   New today (oq=0):         (ltp-avg)*qty - 0 = (ltp-entry)*qty ✓
    #   Partial close (oq>qty>0): (ltp-avg)*rem + realised - (prev-avg)*oq ✓
    #   Closed intraday (oq=0):   realised - 0 = realised ✓
    #   Closed overnight (oq>0):  realised - (prev-avg)*oq = (exit-prev)*oq ✓
    _avg = float(avg_cost) if avg_cost else 0.0
    _total = float(total_pnl) if total_pnl is not None else 0.0
    # Snapshots written before this fix have no overnight_quantity field — fall
    # back to effective_qty (the old behavior: all held qty = overnight qty).
    _oq = _parse_overnight_qty(payload_json, effective_qty)

    computed_day_pnl = _compute_snapshot_day_pnl(actual_previous_close, _total, _avg, _oq, day_pnl)

    return build_snapshot_position_row(
        account, symbol, exchange, effective_qty, avg_cost, ltp,
        computed_day_pnl, total_pnl, extras,
        previous_close=actual_previous_close,
        prev_settlement_pnl=prev_pnl_val,
        product=extract_snapshot_product(payload_json),
        overnight_quantity=int(_oq),
    )


# ---------------------------------------------------------------------------
# 3. Response shaping (get_positions seams)
# ---------------------------------------------------------------------------

async def _apply_trader_scope(
    resp: PositionsResponse,
    request: object,
) -> PositionsResponse:
    """Narrow `resp` to only the accounts the trader role is allowed to see."""
    allowed, _ = await user_scope_for_connection(request)
    allowed_set = {str(a).upper() for a in (allowed or [])}
    return msgspec.structs.replace(
        resp,
        rows=[r for r in resp.rows
              if str(getattr(r, "account", "")).upper() in allowed_set],
        summary=[s for s in resp.summary
                 if str(getattr(s, "account", "")).upper() in allowed_set
                 or str(getattr(s, "account", "")).upper() == "TOTAL"],
    )


def _apply_account_mask(resp: PositionsResponse) -> PositionsResponse:
    """Mask account identifiers for non-admin callers."""
    def _mask(row: object) -> object:
        return msgspec.structs.replace(row, account=mask_account(row.account))  # type: ignore[attr-defined]

    return msgspec.structs.replace(
        resp,
        rows=[_mask(r) for r in resp.rows],
        summary=[_mask(s) for s in resp.summary],
    )


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
        resp = await _apply_trader_scope(resp, request)

    if not is_admin_request(request):
        resp = _apply_account_mask(resp)

    return resp


def _is_broker_outage(err: Exception) -> bool:
    """Detect Kite (Zerodha) upstream HTTP gateway errors.

    Returns True when the error message contains any of the well-known
    upstream-gateway strings.  Used as a guard before falling back to a
    stale snapshot so transient 502/503/504 conditions don't surface blank
    data to the frontend.

    SSOT: imported by positions.py, holdings.py, and funds.py — do NOT
    define locally in those modules.
    """
    s = str(err).lower()
    return any(needle in s for needle in (
        "bad gateway", "502", "503", "504",
        "service unavailable", "gateway timeout",
    ))


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
