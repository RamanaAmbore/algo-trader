"""Unit tests for positions_helpers.py extracted helpers.

Five quality dimensions:
  1. SSOT    — build_summary_from_rows is the single summary builder
  2. Perf    — pure-CPU, no I/O
  3. Stale   — helpers are imported from positions_helpers, not re-defined
  4. Reuse   — same helpers used by _positions_snapshot and paper path
  5. UX      — edge cases: empty rows, zero prev_val, single account
"""

import math
import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_row(
    account="ZG0790",
    pnl=1000.0,
    day_change_val=200.0,
    prev_close=500.0,
    quantity=10,
    unrealised=None,
    realised=0.0,
    prev_settlement_pnl=None,
):
    """Build a synthetic PositionRow.

    `day_change_val` (the legacy per-row field) is kept as a cosmetic
    diagnostic — NOT what drives the account/symbol rollups any more
    (see pnl_math.baseline_diff_day_pnl / positions_helpers.build_summary
    _from_rows). Rollups derive Day P&L from
    `realised + unrealised − prev_settlement_pnl` (falling back to `pnl`
    as the "realised" leg when both realised/unrealised are 0, matching
    the closed-hours snapshot row shape). Defaults: `unrealised=pnl` and
    `prev_settlement_pnl=pnl - day_change_val` so a caller who only sets
    `pnl`/`day_change_val` (the pre-redesign call convention) still drives
    the new formula to the SAME `day_change_val` result — i.e. existing
    callers of this fixture keep their intended Day P&L unless they
    explicitly override the new baseline params.
    """
    from backend.api.schemas import PositionRow
    _unrealised = pnl if unrealised is None else unrealised
    _base = (pnl - day_change_val) if prev_settlement_pnl is None else prev_settlement_pnl
    return PositionRow(
        account=account,
        tradingsymbol="NIFTY25JUNFUT",
        exchange="NFO",
        product="NRML",
        quantity=quantity,
        average_price=490.0,
        prev_close=prev_close,
        last_price=prev_close + day_change_val / quantity,
        pnl=pnl,
        pnl_percentage=0.0,
        unrealised=_unrealised,
        realised=realised,
        day_change_val=day_change_val,
        day_change_percentage=0.0,
        prev_settlement_pnl=_base,
    )


# ---------------------------------------------------------------------------
# 1. SSOT — build_summary_from_rows is importable from positions_helpers
# ---------------------------------------------------------------------------

def test_build_summary_from_rows_importable():
    """SSOT: helper must live in positions_helpers, not inlined in positions.py."""
    from backend.api.routes.positions_helpers import build_summary_from_rows
    assert callable(build_summary_from_rows)


# ---------------------------------------------------------------------------
# 2. Perf — pure-CPU, no DB/broker calls
# ---------------------------------------------------------------------------

def test_build_summary_from_rows_no_io():
    """No awaits / DB calls — sync, pure computation."""
    import inspect
    from backend.api.routes.positions_helpers import build_summary_from_rows
    # Must be a regular (non-async) function
    assert not inspect.iscoroutinefunction(build_summary_from_rows)


# ---------------------------------------------------------------------------
# 3. Correctness — per-account sums + TOTAL row
# ---------------------------------------------------------------------------

def test_build_summary_single_account():
    from backend.api.routes.positions_helpers import build_summary_from_rows

    rows = [
        _make_row("ZG0790", pnl=1000.0, day_change_val=200.0,
                  prev_close=500.0, quantity=10),
        _make_row("ZG0790", pnl=500.0,  day_change_val=100.0,
                  prev_close=250.0, quantity=5),
    ]
    summary = build_summary_from_rows(rows)

    # Two entries: one per-account + TOTAL
    assert len(summary) == 2
    by_acct = {s.account: s for s in summary}

    acct = by_acct["ZG0790"]
    assert math.isclose(acct.pnl, 1500.0, rel_tol=1e-6)
    assert math.isclose(acct.day_change_val, 300.0, rel_tol=1e-6)
    # day_prev_val = |close × qty| summed: |500×10| + |250×5| = 6250
    assert math.isclose(acct.day_prev_val, 6250.0, rel_tol=1e-6)
    # day_change_percentage = 300 / 6250 × 100 ≈ 4.8
    assert math.isclose(acct.day_change_percentage, 300.0 / 6250.0 * 100.0, rel_tol=1e-4)

    total = by_acct["TOTAL"]
    assert math.isclose(total.pnl, 1500.0, rel_tol=1e-6)


def test_build_summary_two_accounts():
    from backend.api.routes.positions_helpers import build_summary_from_rows

    rows = [
        _make_row("ZG0790", pnl=1000.0, day_change_val=200.0,
                  prev_close=500.0, quantity=10),
        _make_row("ZJ6294", pnl=2000.0, day_change_val=400.0,
                  prev_close=200.0, quantity=5),
    ]
    summary = build_summary_from_rows(rows)

    assert len(summary) == 3  # 2 accounts + TOTAL
    by_acct = {s.account: s for s in summary}
    assert math.isclose(by_acct["TOTAL"].pnl, 3000.0, rel_tol=1e-6)
    assert math.isclose(by_acct["TOTAL"].day_change_val, 600.0, rel_tol=1e-6)


def test_build_summary_empty_rows():
    """Empty list → TOTAL row only with zeros."""
    from backend.api.routes.positions_helpers import build_summary_from_rows

    summary = build_summary_from_rows([])
    assert len(summary) == 1
    total = summary[0]
    assert total.account == "TOTAL"
    assert total.pnl == 0.0
    assert total.day_change_val == 0.0
    assert total.day_change_percentage == 0.0


def test_build_summary_zero_prev_val():
    """close_price=0 → day_change_percentage stays 0 (no div-by-zero)."""
    from backend.api.routes.positions_helpers import build_summary_from_rows

    rows = [_make_row("ZG0790", pnl=500.0, day_change_val=100.0,
                      prev_close=0.0, quantity=5)]
    summary = build_summary_from_rows(rows)
    by_acct = {s.account: s for s in summary}
    assert by_acct["ZG0790"].day_change_percentage == 0.0


# ---------------------------------------------------------------------------
# 4. Reuse — extract_snapshot_extras and resolve_snapshot_day_pnl
# ---------------------------------------------------------------------------

def test_extract_snapshot_extras_dict():
    from backend.api.routes.positions_helpers import extract_snapshot_extras
    import json

    payload = json.dumps({"snapshot_extras": {"day_change_val": 999.9}})
    extras = extract_snapshot_extras(payload)
    assert extras == {"day_change_val": 999.9}


def test_extract_snapshot_extras_none():
    from backend.api.routes.positions_helpers import extract_snapshot_extras
    assert extract_snapshot_extras(None) == {}


def test_extract_snapshot_extras_malformed():
    from backend.api.routes.positions_helpers import extract_snapshot_extras
    assert extract_snapshot_extras("not-valid-json{{") == {}
    assert extract_snapshot_extras("[1,2,3]") == {}


def test_resolve_snapshot_day_pnl_column_wins():
    """When day_pnl_col is not None, column value wins over extras."""
    from backend.api.routes.positions_helpers import resolve_snapshot_day_pnl

    result = resolve_snapshot_day_pnl(
        day_pnl_col=250.0,
        day_pnl_f=250.0,
        extras={"day_change_val": -999.99},
    )
    assert math.isclose(result, 250.0, rel_tol=1e-6)


def test_resolve_snapshot_day_pnl_extras_fallback():
    """When day_pnl_col is None, fall back to extras.day_change_val."""
    from backend.api.routes.positions_helpers import resolve_snapshot_day_pnl

    result = resolve_snapshot_day_pnl(
        day_pnl_col=None,
        day_pnl_f=0.0,
        extras={"day_change_val": 333.3},
    )
    assert math.isclose(result, 333.3, rel_tol=1e-6)


def test_resolve_snapshot_day_pnl_no_extras_no_col():
    """Both None and missing extras → returns original day_pnl_f (0.0)."""
    from backend.api.routes.positions_helpers import resolve_snapshot_day_pnl

    result = resolve_snapshot_day_pnl(
        day_pnl_col=None,
        day_pnl_f=0.0,
        extras={},
    )
    assert result == 0.0


# ---------------------------------------------------------------------------
# 5. UX — build_snapshot_position_row produces well-formed PositionRow
# ---------------------------------------------------------------------------

def test_build_snapshot_position_row_fields():
    from decimal import Decimal
    from backend.api.routes.positions_helpers import build_snapshot_position_row

    row = build_snapshot_position_row(
        account="ZG0790",
        symbol="NIFTY26JULFUT",
        exchange="NFO",
        qty=50,
        avg_cost=Decimal("23000.00"),
        ltp=Decimal("23500.00"),
        day_pnl=Decimal("2500.00"),
        total_pnl=Decimal("7500.00"),
        extras={},
    )
    assert row.account == "ZG0790"
    assert row.tradingsymbol == "NIFTY26JULFUT"
    assert row.quantity == 50
    assert row.overnight_quantity == 50  # SSOT: must match qty
    assert row.is_animating is False
    assert row.price_source == "snapshot_settled"
    assert math.isclose(row.day_change_val, 2500.0, rel_tol=1e-6)
    assert math.isclose(row.pnl, 7500.0, rel_tol=1e-6)


# ---------------------------------------------------------------------------
# 6. extract_snapshot_multiplier removed — daily_book.qty stores CONTRACTS
#    (write-seam converts lots→contracts) so read-seam must NOT multiply again.
#    The function has been deleted from positions_helpers.py.
# ---------------------------------------------------------------------------

def test_snapshot_mcx_qty_contracts_no_double_multiply():
    """Snapshot path: 1-lot CRUDEOIL is stored as 100 contracts in daily_book.qty.
    build_row_from_snapshot_raw must NOT apply multiplier again — doing so caused
    MCX qty to be contracts × lot_size (e.g. 100 × 100 = 10,000) instead of 100.

    The write-seam (_positions_qty_fields) already converts lots → contracts.
    extract_snapshot_multiplier was deprecated and has been removed.
    """
    import json
    from decimal import Decimal
    from backend.api.routes.positions_helpers import (
        build_snapshot_position_row,
    )

    # daily_book.qty stores CONTRACTS after _positions_qty_fields:
    # 1 lot CRUDEOIL (lot_size=100) → qty_contracts = 1 × 100 = 100
    qty_from_db = 100         # daily_book.qty is in contracts, NOT lots
    payload_json = json.dumps({
        "tradingsymbol": "CRUDEOIL26JUL7500CE",
        "exchange": "MCX",
        "multiplier": 100,    # present in payload but must NOT be applied again
    })

    row = build_snapshot_position_row(
        account="ZG0790",
        symbol="CRUDEOIL26JUL7500CE",
        exchange="MCX",
        qty=qty_from_db,
        avg_cost=Decimal("426.30"),
        ltp=Decimal("180.00"),
        day_pnl=Decimal("-24630.00"),
        total_pnl=Decimal("-24630.00"),
        extras={},
    )
    # Must pass through as-is — no double multiplication
    assert row.quantity == 100, (
        f"Expected 100 contracts (already converted by write-seam), got {row.quantity}"
    )
    assert row.overnight_quantity == 100
    # pnl and day_change_val are from DB (absolute ₹) — not scaled by qty
    assert math.isclose(row.pnl, -24630.0, rel_tol=1e-6)


# ---------------------------------------------------------------------------
# 7. prev_settlement_pnl — new kwarg for the day-P&L Branch A fix
# ---------------------------------------------------------------------------

def test_prev_settlement_pnl_set_when_provided():
    """prev_settlement_pnl kwarg lands on returned PositionRow."""
    from decimal import Decimal
    from backend.api.routes.positions_helpers import build_snapshot_position_row

    row = build_snapshot_position_row(
        account="ZJ6294",
        symbol="CRUDEOIL26JUL7500CE",
        exchange="MCX",
        qty=1,
        avg_cost=Decimal("423.1"),
        ltp=Decimal("205.0"),
        day_pnl=None,
        total_pnl=Decimal("-218.1"),
        extras={},
        previous_close=Decimal("165.3"),
        prev_settlement_pnl=100.0,
    )
    assert row.prev_settlement_pnl == 100.0, \
        f"Expected prev_settlement_pnl=100.0, got {row.prev_settlement_pnl}"


def test_prev_settlement_pnl_none_when_not_provided():
    """Default None — Branch B fires in baseDayPnlForPosition."""
    from decimal import Decimal
    from backend.api.routes.positions_helpers import build_snapshot_position_row

    row = build_snapshot_position_row(
        account="ZJ6294",
        symbol="CRUDEOIL26JUL7500CE",
        exchange="MCX",
        qty=1,
        avg_cost=Decimal("423.1"),
        ltp=Decimal("205.0"),
        day_pnl=None,
        total_pnl=Decimal("-218.1"),
        extras={},
    )
    assert row.prev_settlement_pnl is None, \
        f"Expected prev_settlement_pnl=None, got {row.prev_settlement_pnl}"


def test_branch_a_fires_with_prev_settlement_pnl():
    """When prev_settlement_pnl set, day P&L = total_pnl - prev_settlement_pnl."""
    from decimal import Decimal
    from backend.api.routes.positions_helpers import build_snapshot_position_row

    total_pnl = 2500.0
    prev_pnl = 1000.0
    row = build_snapshot_position_row(
        account="ZJ6294",
        symbol="BHEL26JUL390CE",
        exchange="NFO",
        qty=100,
        avg_cost=Decimal("310.0"),
        ltp=Decimal("335.0"),
        day_pnl=None,
        total_pnl=Decimal(str(total_pnl)),
        extras={},
        previous_close=Decimal("320.0"),
        prev_settlement_pnl=prev_pnl,
    )
    assert row.prev_settlement_pnl == prev_pnl, \
        f"Expected prev_settlement_pnl={prev_pnl}, got {row.prev_settlement_pnl}"
    # Simulate baseDayPnlForPosition Branch A
    day_pnl_branch_a = row.pnl - row.prev_settlement_pnl
    assert math.isclose(day_pnl_branch_a, 1500.0, rel_tol=1e-6), \
        f"Expected day_pnl=1500.0 (2500-1000), got {day_pnl_branch_a}"


def test_branch_b_uses_previous_close_not_ltp():
    """Without prev_settlement_pnl, Branch B: day = pnl - oq*(close-avg)."""
    from decimal import Decimal
    from backend.api.routes.positions_helpers import build_snapshot_position_row

    row = build_snapshot_position_row(
        account="ZJ6294",
        symbol="BHEL",
        exchange="NSE",
        qty=100,
        avg_cost=Decimal("310.0"),
        ltp=Decimal("335.0"),
        day_pnl=None,
        total_pnl=Decimal("2500.0"),
        extras={},
        previous_close=Decimal("320.0"),
    )
    assert row.prev_settlement_pnl is None, \
        f"Expected prev_settlement_pnl=None, got {row.prev_settlement_pnl}"
    # Branch B: close_price must be previous_close (320), not ltp (335)
    assert math.isclose(row.prev_close, 320.0, rel_tol=1e-6), \
        f"Expected close_price=320.0 (previous_close), got {row.prev_close}"
    oq = row.overnight_quantity
    # day-P&L = pnl - oq × (close_price - avg_price)
    # = 2500 - 100 × (320 - 310) = 2500 - 1000 = 1500
    expected_day = row.pnl - oq * (row.prev_close - row.average_price)
    assert math.isclose(expected_day, 1500.0, rel_tol=1e-6), \
        f"Expected day_pnl=1500.0, got {expected_day}"


def test_previous_close_used_when_provided_and_positive():
    """previous_close > 0 → close_price = previous_close (not ltp)."""
    from decimal import Decimal
    from backend.api.routes.positions_helpers import build_snapshot_position_row

    row = build_snapshot_position_row(
        account="ZJ6294",
        symbol="BEL",
        exchange="NSE",
        qty=50,
        avg_cost=Decimal("200.0"),
        ltp=Decimal("215.0"),
        day_pnl=None,
        total_pnl=Decimal("750.0"),
        extras={},
        previous_close=Decimal("210.0"),
    )
    assert math.isclose(row.prev_close, 210.0, rel_tol=1e-6), \
        f"Expected close_price=210.0 (previous_close), got {row.prev_close}"


def test_previous_close_falls_back_to_ltp_when_zero():
    """previous_close=0 → close_price falls back to ltp (old behaviour)."""
    from decimal import Decimal
    from backend.api.routes.positions_helpers import build_snapshot_position_row

    row = build_snapshot_position_row(
        account="ZJ6294",
        symbol="NEWPOS",
        exchange="NFO",
        qty=10,
        avg_cost=Decimal("100.0"),
        ltp=Decimal("105.0"),
        day_pnl=None,
        total_pnl=Decimal("50.0"),
        extras={},
        previous_close=Decimal("0.0"),  # New position, no prior close
    )
    assert math.isclose(row.prev_close, 105.0, rel_tol=1e-6), \
        f"Expected close_price=105.0 (ltp fallback), got {row.prev_close}"


def test_prev_settlement_pnl_negative_value():
    """prev_settlement_pnl can be negative (a loss from yesterday)."""
    from decimal import Decimal
    from backend.api.routes.positions_helpers import build_snapshot_position_row

    row = build_snapshot_position_row(
        account="ZJ6294",
        symbol="CRUDEOIL26JUL6900PE",
        exchange="MCX",
        qty=10,
        avg_cost=Decimal("200.0"),
        ltp=Decimal("264.5"),
        day_pnl=None,
        total_pnl=Decimal("645.0"),
        extras={},
        previous_close=Decimal("220.0"),
        prev_settlement_pnl=-500.0,
    )
    assert row.prev_settlement_pnl == -500.0, \
        f"Expected prev_settlement_pnl=-500.0, got {row.prev_settlement_pnl}"
    # Branch A: day_pnl = 645 - (-500) = 1145
    day_pnl_branch_a = row.pnl - row.prev_settlement_pnl
    assert math.isclose(day_pnl_branch_a, 1145.0, rel_tol=1e-6), \
        f"Expected day_pnl=1145.0, got {day_pnl_branch_a}"


def test_prev_settlement_pnl_zero_value():
    """prev_settlement_pnl can be 0.0 (break-even yesterday)."""
    from decimal import Decimal
    from backend.api.routes.positions_helpers import build_snapshot_position_row

    row = build_snapshot_position_row(
        account="ZJ6294",
        symbol="CRUDEOIL26JUL6900PE",
        exchange="MCX",
        qty=10,
        avg_cost=Decimal("200.0"),
        ltp=Decimal("264.5"),
        day_pnl=None,
        total_pnl=Decimal("645.0"),
        extras={},
        previous_close=Decimal("220.0"),
        prev_settlement_pnl=0.0,
    )
    assert row.prev_settlement_pnl == 0.0, \
        f"Expected prev_settlement_pnl=0.0, got {row.prev_settlement_pnl}"
    # Branch A: day_pnl = 645 - 0 = 645
    day_pnl_branch_a = row.pnl - row.prev_settlement_pnl
    assert math.isclose(day_pnl_branch_a, 645.0, rel_tol=1e-6), \
        f"Expected day_pnl=645.0, got {day_pnl_branch_a}"


def test_prev_settlement_pnl_coexists_with_close_override():
    """prev_settlement_pnl and previous_close both set → both apply."""
    from decimal import Decimal
    from backend.api.routes.positions_helpers import build_snapshot_position_row

    row = build_snapshot_position_row(
        account="ZJ6294",
        symbol="CRUDEOIL26JUL6900PE",
        exchange="MCX",
        qty=10,
        avg_cost=Decimal("200.0"),
        ltp=Decimal("264.5"),
        day_pnl=None,
        total_pnl=Decimal("645.0"),
        extras={},
        previous_close=Decimal("220.0"),
        prev_settlement_pnl=100.0,
    )
    # Both patches should apply
    assert math.isclose(row.prev_close, 220.0, rel_tol=1e-6), \
        f"Expected close_price=220.0 (previous_close), got {row.prev_close}"
    assert row.prev_settlement_pnl == 100.0, \
        f"Expected prev_settlement_pnl=100.0, got {row.prev_settlement_pnl}"
    # day_pnl = 645 - 100 = 545
    day_pnl_branch_a = row.pnl - row.prev_settlement_pnl
    assert math.isclose(day_pnl_branch_a, 545.0, rel_tol=1e-6), \
        f"Expected day_pnl=545.0, got {day_pnl_branch_a}"


# ---------------------------------------------------------------------------
# Fix 3: closed overnight position must preserve stored day_pnl (not 0.0)
# ---------------------------------------------------------------------------

def test_closed_overnight_position_preserves_day_pnl():
    """Universal formula: when qty=0 (closed overnight position) and no overnight_quantity
    in payload, build_row_from_snapshot_raw falls back to oq=0, so day_pnl = total_pnl.

    With overnight_quantity provided in payload, the universal formula gives the
    correct day_pnl = total_pnl - (prev_close - avg) * oq.

    Column order for build_row_from_snapshot_raw:
    account, symbol, exchange, qty, avg_cost, ltp,
    day_pnl, total_pnl, payload_json, captured_at, previous_close,
    prev_ltp, prev_settlement_pnl
    """
    import json
    from decimal import Decimal
    from backend.api.routes.positions_helpers import build_row_from_snapshot_raw

    # Case 1: No overnight_quantity in payload → fallback to qty=0 → day_pnl = total_pnl
    # avg=195, prev_close=200, total_pnl=-300 (realised from closing 100 shares at 197)
    raw_row_no_oq = (
        "ZG0790",               # account
        "RELIANCE",             # symbol
        "NSE",                  # exchange
        0,                      # qty — closed overnight
        Decimal("195.00"),      # avg_cost
        Decimal("201.50"),      # ltp (exit price after close)
        Decimal("-500.00"),     # day_pnl stored (stale — formula overrides)
        Decimal("-300.00"),     # total_pnl (realised)
        json.dumps({}),         # payload_json — no overnight_quantity
        None,                   # captured_at
        Decimal("200.00"),      # previous_close
        Decimal("200.00"),      # prev_ltp
        None,                   # prev_settlement_pnl
    )
    row_no_oq = build_row_from_snapshot_raw(raw_row_no_oq)
    # Without overnight_quantity, formula: total_pnl - (prev-avg) * 0 = total_pnl
    assert math.isclose(row_no_oq.day_change_val, -300.0, rel_tol=1e-6), (
        f"Without overnight_quantity in payload, day_change_val = total_pnl = -300, "
        f"got {row_no_oq.day_change_val}."
    )

    # Case 2: With overnight_quantity=100 in payload → universal formula gives correct result
    # avg=195, oq=100, prev_close=200, total_pnl=-300
    # day_pnl = -300 - (200-195)*100 = -300 - 500 = -800? No...
    # Let's use consistent data: exit_price=197.5, oq=100, prev_close=200
    # total_pnl = (197.5 - 195)*100 = 250 (realised gain)
    # day_pnl = (exit - prev)*oq = (197.5-200)*100 = -250
    # formula: 250 - (200-195)*100 = 250 - 500 = -250 ✓
    raw_row_with_oq = (
        "ZG0790",               # account
        "RELIANCE",             # symbol
        "NSE",                  # exchange
        0,                      # qty — closed overnight
        Decimal("195.00"),      # avg_cost
        Decimal("197.50"),      # ltp (exit price VWAP)
        Decimal("-250.00"),     # day_pnl stored
        Decimal("250.00"),      # total_pnl = (197.5-195)*100 = 250
        json.dumps({"overnight_quantity": 100}),  # payload with overnight_quantity
        None,                   # captured_at
        Decimal("200.00"),      # previous_close
        Decimal("200.00"),      # prev_ltp
        None,                   # prev_settlement_pnl
    )
    row_with_oq = build_row_from_snapshot_raw(raw_row_with_oq)
    # Formula: 250 - (200-195)*100 = 250 - 500 = -250
    assert math.isclose(row_with_oq.day_change_val, -250.0, rel_tol=1e-6), (
        f"With overnight_quantity=100 in payload, day_change_val = 250 - (200-195)*100 = -250, "
        f"got {row_with_oq.day_change_val}."
    )


def test_mcx_settlement_snapshot_uses_previous_close_directly():
    """MCX settlement snapshot: previous_close from daily_book is used directly.

    Simplified pipeline: no corruption guard (pc ≈ ltp detection removed).
    At session open ltp == previous_close is valid — no intraday movement yet.
    fix_daily_book_prev_close(settlement_map=...) at 08:00 IST sets the correct
    settlement reference from BHAV-confirmed broker data.

    With previous_close = settlement_price: day_pnl = total_pnl - (pc - avg) * oq
    = 180 - (1280 - 1100) * 1 = 0. This is correct for an at-settlement position.
    """
    import json
    from decimal import Decimal
    from backend.api.routes.positions_helpers import build_row_from_snapshot_raw

    settlement_price = Decimal("1280.00")   # MCX settlement = ltp
    avg_cost         = Decimal("1100.00")
    oq               = 1
    total_pnl        = Decimal("180.00")    # (1280 - 1100) * 1 = 180
    raw_row = (
        "ZJ6294",
        "GOLDM26SEP160000CE",
        "MCX",
        oq,                        # qty
        avg_cost,
        settlement_price,          # ltp = settlement price
        Decimal("0.00"),           # day_pnl stored (0 from UPSERT)
        total_pnl,
        json.dumps({"overnight_quantity": oq}),
        None,                      # captured_at
        settlement_price,          # previous_close = settlement (valid: equals ltp at open)
        Decimal("1200.00"),        # prev_ltp (not used in simplified pipeline)
        None,                      # prev_settlement_pnl
    )
    row = build_row_from_snapshot_raw(raw_row)

    # With simplified pipeline: previous_close = settlement_price = 1280
    # day_pnl = total_pnl - (prev_close - avg) * oq = 180 - (1280-1100)*1 = 0
    expected = float(total_pnl) - (float(settlement_price) - float(avg_cost)) * oq
    assert math.isclose(row.day_change_val, expected, rel_tol=1e-6), (
        f"MCX settlement: expected day_pnl={expected} (using previous_close directly), "
        f"got {row.day_change_val}"
    )


# ---------------------------------------------------------------------------
# 8. _compute_snapshot_day_pnl — extracted CC-reduction helper
# ---------------------------------------------------------------------------

class TestComputeSnapshotDayPnl:
    """Unit tests for _compute_snapshot_day_pnl helper."""

    def test_with_valid_prev_close(self):
        """When actual_pc > 0, formula = total_pnl - (prev_close - avg) * oq."""
        from backend.api.routes.positions_helpers import _compute_snapshot_day_pnl

        # Overnight position: total=2500, avg=195, oq=100, prev_close=200
        # day = 2500 - (200-195)*100 = 2500 - 500 = 2000
        result = _compute_snapshot_day_pnl(
            actual_pc=200.0, total_pnl=2500.0, avg=195.0, oq=100.0, day_pnl_raw=9999.0
        )
        assert math.isclose(result, 2000.0, rel_tol=1e-6), (
            f"Expected 2000.0, got {result}"
        )

    def test_fallback_to_day_pnl_raw_when_no_prev_close(self):
        """When actual_pc is None, return day_pnl_raw unchanged."""
        from backend.api.routes.positions_helpers import _compute_snapshot_day_pnl

        result = _compute_snapshot_day_pnl(
            actual_pc=None, total_pnl=1000.0, avg=100.0, oq=10.0, day_pnl_raw=42.0
        )
        assert result == 42.0, f"Expected day_pnl_raw=42.0, got {result}"

    def test_fallback_when_prev_close_zero(self):
        """When actual_pc=0.0, return day_pnl_raw (zero is falsy guard)."""
        from backend.api.routes.positions_helpers import _compute_snapshot_day_pnl

        result = _compute_snapshot_day_pnl(
            actual_pc=0.0, total_pnl=1000.0, avg=100.0, oq=10.0, day_pnl_raw=-55.0
        )
        assert result == -55.0, f"Expected day_pnl_raw=-55.0, got {result}"

    def test_new_position_oq_zero(self):
        """New-position case (oq=0): day = total_pnl - (prev-avg)*0 = total_pnl."""
        from backend.api.routes.positions_helpers import _compute_snapshot_day_pnl

        result = _compute_snapshot_day_pnl(
            actual_pc=200.0, total_pnl=500.0, avg=195.0, oq=0.0, day_pnl_raw=0.0
        )
        assert math.isclose(result, 500.0, rel_tol=1e-6), (
            f"New position (oq=0): day should equal total_pnl=500.0, got {result}"
        )

    def test_closed_overnight_position(self):
        """Closed overnight (oq>0, qty=0): day = total_pnl - (prev-avg)*oq."""
        from backend.api.routes.positions_helpers import _compute_snapshot_day_pnl

        # avg=195, oq=100, prev_close=200, total_pnl=250 (exit at 197.5)
        # day = 250 - (200-195)*100 = 250 - 500 = -250
        result = _compute_snapshot_day_pnl(
            actual_pc=200.0, total_pnl=250.0, avg=195.0, oq=100.0, day_pnl_raw=0.0
        )
        assert math.isclose(result, -250.0, rel_tol=1e-6), (
            f"Closed overnight: expected -250.0, got {result}"
        )

    def test_is_importable_from_positions_helpers(self):
        """SSOT: helper must be importable from positions_helpers, not inline."""
        from backend.api.routes.positions_helpers import _compute_snapshot_day_pnl
        import inspect
        assert callable(_compute_snapshot_day_pnl)
        assert not inspect.iscoroutinefunction(_compute_snapshot_day_pnl)


# ---------------------------------------------------------------------------
# 9. _compute_holding_day_change — extracted holdings CC-reduction helper
# ---------------------------------------------------------------------------

class TestComputeHoldingDayChange:
    """Unit tests for _compute_holding_day_change helper."""

    def test_day_pnl_beats_prev_ltp(self):
        """day_pnl (Kite session value) is Priority 1; prev_ltp is only a last-resort fallback."""
        from backend.api.routes.holdings import _compute_holding_day_change

        result = _compute_holding_day_change(
            day_pnl_f=500.0, ltp_f=2100.0, prev_close_f=2050.0,
            prev_ltp_f=2040.0, qty_i=10
        )
        # day_pnl wins: prev_ltp only fires when day_pnl=0 AND previous_close=0
        assert result == 500.0, f"day_pnl=500 should win over prev_ltp, got {result}"

    def test_day_pnl_is_priority_one(self):
        """day_pnl is Priority 1: returned whenever non-zero, even when previous_close=0."""
        from backend.api.routes.holdings import _compute_holding_day_change

        result = _compute_holding_day_change(
            day_pnl_f=500.0, ltp_f=2100.0, prev_close_f=0.0,
            prev_ltp_f=None, qty_i=10
        )
        assert result == 500.0, f"day_pnl should be Priority 1, got {result}"

    def test_price_recompute_using_previous_close(self):
        """day_pnl=0, previous_close valid: use (ltp - previous_close) * qty."""
        from backend.api.routes.holdings import _compute_holding_day_change

        result = _compute_holding_day_change(
            day_pnl_f=0.0, ltp_f=2100.0, prev_close_f=2050.0,
            prev_ltp_f=None, qty_i=10
        )
        expected = (2100.0 - 2050.0) * 10  # 500.0
        assert math.isclose(result, expected, rel_tol=1e-6), (
            f"Expected (ltp-pc)*qty={expected}, got {result}"
        )

    def test_prev_ltp_fallback_when_day_pnl_zero_and_no_close(self):
        """prev_ltp is Priority 3 fallback: (ltp-prev_ltp)*qty when day_pnl=0 and previous_close=0."""
        from backend.api.routes.holdings import _compute_holding_day_change

        result = _compute_holding_day_change(
            day_pnl_f=0.0, ltp_f=2100.0, prev_close_f=0.0,
            prev_ltp_f=2040.0, qty_i=10
        )
        expected = (2100.0 - 2040.0) * 10  # 600.0
        assert math.isclose(result, expected, rel_tol=1e-6), (
            f"Expected (ltp-prev_ltp)*qty={expected}, got {result}"
        )

    def test_all_zero_returns_zero(self):
        """No reference available: returns 0.0."""
        from backend.api.routes.holdings import _compute_holding_day_change

        result = _compute_holding_day_change(
            day_pnl_f=0.0, ltp_f=2100.0, prev_close_f=0.0,
            prev_ltp_f=None, qty_i=10
        )
        assert result == 0.0, f"Expected 0.0 when no reference available, got {result}"

    def test_negative_day_pnl_returned_directly(self):
        """Negative stored day_pnl is returned as-is (loss day)."""
        from backend.api.routes.holdings import _compute_holding_day_change

        result = _compute_holding_day_change(
            day_pnl_f=-800.0, ltp_f=1900.0, prev_close_f=1980.0,
            prev_ltp_f=None, qty_i=10
        )
        assert result == -800.0, f"Expected -800.0, got {result}"

    def test_is_importable_from_holdings(self):
        """SSOT: helper must be importable from holdings module."""
        from backend.api.routes.holdings import _compute_holding_day_change
        import inspect
        assert callable(_compute_holding_day_change)
        assert not inspect.iscoroutinefunction(_compute_holding_day_change)


# ---------------------------------------------------------------------------
# 10. _resolve_prev_settlement_pnl — holdings-phantom-baseline gate/pro-ration
#     (2026-09 Day P&L audit items #4 and #5)
# ---------------------------------------------------------------------------

class TestResolvePrevSettlementPnl:
    """Unit tests for _resolve_prev_settlement_pnl.

    Bug #4 repro: a symbol exists in holdings (100 shares, lifetime
    unrealised = ₹50,000) with no positions-kind row in yesterday's batch.
    A fresh, UNRELATED same-day MIS trade on that symbol today must NOT
    inherit the holding's full lifetime gain as its baseline — it should
    get no baseline at all (base_pnl=0 downstream).

    Bug #5 repro: a position was fully closed yesterday (qty=0,
    total_pnl=realised) and the row survives in the latest batch. A fresh
    re-entry today must not inherit yesterday's flat/closed total_pnl as
    its baseline — the SQL layer excludes qty=0 rows entirely (tested via
    `_BASELINE_PNL_CTE_SQL`'s `qty != 0` filter in test_baseline_pnl_map.py);
    this class covers the Python-side kind/qty gating that complements it.
    """

    def test_positions_kind_baseline_passes_through_unchanged(self):
        """A 'positions'-kind baseline (genuine continuing position) is
        used verbatim — no gating applies."""
        from backend.api.routes.positions_helpers import _resolve_prev_settlement_pnl

        result = _resolve_prev_settlement_pnl(
            4500.0, "positions", 10.0,
            product="MIS", day_sell_qty=0.0,
        )
        assert result == 4500.0

    def test_none_kind_passes_through_unchanged(self):
        """Older callers that haven't threaded kind/qty through (kind=None)
        keep the pre-fix unconditional-use behaviour — backward compatible
        with 13/14-column daily_book snapshot tuples."""
        from backend.api.routes.positions_helpers import _resolve_prev_settlement_pnl

        result = _resolve_prev_settlement_pnl(
            4500.0, None, None, product=None, day_sell_qty=None,
        )
        assert result == 4500.0

    def test_holdings_baseline_rejected_for_unrelated_mis_trade(self):
        """Bug #4 core repro: holdings total_pnl=50000 (lifetime gain on
        100 shares held) must NOT become the baseline for a fresh,
        unrelated same-day MIS trade on the same symbol (product='MIS',
        day_sell_qty=0 — no holding was sold). Returns None (no baseline,
        base_pnl=0 downstream) instead of the phantom ₹50,000."""
        from backend.api.routes.positions_helpers import _resolve_prev_settlement_pnl

        result = _resolve_prev_settlement_pnl(
            50000.0, "holdings", 100.0,
            product="MIS", day_sell_qty=0.0,
        )
        assert result is None, (
            f"MIS trade must not inherit the holding's lifetime gain as a "
            f"baseline; got {result} instead of None"
        )

    def test_holdings_baseline_rejected_for_cnc_topup_no_sale(self):
        """A same-day CNC top-up (buying MORE of a held stock, not selling)
        is also NOT a valid holdings-baseline case — day_sell_qty=0 means
        nothing was actually drawn down from the holding."""
        from backend.api.routes.positions_helpers import _resolve_prev_settlement_pnl

        result = _resolve_prev_settlement_pnl(
            50000.0, "holdings", 100.0,
            product="CNC", day_sell_qty=0.0,
        )
        assert result is None

    def test_holdings_baseline_prorated_for_genuine_cnc_sale(self):
        """Bug #4 legitimate case: holding sold_qty=10 out of hold_qty=100,
        holding total_pnl=50000 (lifetime gain on the WHOLE 100-share
        holding). Baseline must be pro-rated to the SOLD fraction:
        50000 * min(10,100)/100 = 5000 — not the full 50000."""
        from backend.api.routes.positions_helpers import _resolve_prev_settlement_pnl

        result = _resolve_prev_settlement_pnl(
            50000.0, "holdings", 100.0,
            product="CNC", day_sell_qty=10.0,
        )
        assert result == pytest.approx(5000.0), (
            f"Expected pro-rated baseline 50000*10/100=5000.0, got {result}"
        )

    def test_holdings_baseline_full_when_entire_holding_sold(self):
        """Selling the ENTIRE holding (sold_qty == hold_qty) uses the full
        lifetime total_pnl as the baseline — the pro-ration ratio is 1.0."""
        from backend.api.routes.positions_helpers import _resolve_prev_settlement_pnl

        result = _resolve_prev_settlement_pnl(
            50000.0, "holdings", 100.0,
            product="CNC", day_sell_qty=100.0,
        )
        assert result == pytest.approx(50000.0)

    def test_holdings_baseline_clamped_when_sold_exceeds_hold_qty(self):
        """Defensive clamp: if day_sell_qty somehow exceeds hold_qty (stale
        data edge case), the ratio is capped at 1.0 (min(sold, hold_qty)),
        never inflating the baseline beyond the holding's own total_pnl."""
        from backend.api.routes.positions_helpers import _resolve_prev_settlement_pnl

        result = _resolve_prev_settlement_pnl(
            50000.0, "holdings", 100.0,
            product="CNC", day_sell_qty=150.0,
        )
        assert result == pytest.approx(50000.0)

    def test_holdings_baseline_rejected_when_hold_qty_missing(self):
        """No hold_qty (None) → can't pro-rate → reject rather than guess."""
        from backend.api.routes.positions_helpers import _resolve_prev_settlement_pnl

        result = _resolve_prev_settlement_pnl(
            50000.0, "holdings", None,
            product="CNC", day_sell_qty=10.0,
        )
        assert result is None

    def test_none_raw_value_returns_none(self):
        """No baseline row at all → None passes through regardless of kind."""
        from backend.api.routes.positions_helpers import _resolve_prev_settlement_pnl

        assert _resolve_prev_settlement_pnl(
            None, "holdings", 100.0, product="CNC", day_sell_qty=10.0,
        ) is None
        assert _resolve_prev_settlement_pnl(
            None, "positions", 10.0, product="MIS", day_sell_qty=0.0,
        ) is None

    def test_end_to_end_day_pnl_with_corrected_baseline(self):
        """Full pipeline: today_pnl=1200 (fresh MIS trade), old (buggy)
        baseline=50000 (holding's lifetime gain) would give Day P&L =
        1200-50000 = -48800 (nonsensical). Corrected: baseline=None ->
        baseline_diff_day_pnl treats it as 0 -> Day P&L = 1200 (correct,
        matches a brand-new position with no prior-day continuity)."""
        from backend.api.routes.positions_helpers import _resolve_prev_settlement_pnl
        from backend.api.algo.pnl_math import baseline_diff_day_pnl

        resolved = _resolve_prev_settlement_pnl(
            50000.0, "holdings", 100.0, product="MIS", day_sell_qty=0.0,
        )
        assert resolved is None
        day_pnl = baseline_diff_day_pnl(1200.0, 0.0, resolved or 0.0)
        assert day_pnl == pytest.approx(1200.0), (
            f"Day P&L must be 1200.0 (today's fresh trade only), got {day_pnl} "
            f"— the pre-fix bug would have produced -48800.0"
        )


# ---------------------------------------------------------------------------
# 11. build_snapshot_position_row — realised/unrealised split
#     (2026-09 Day P&L audit item #1)
# ---------------------------------------------------------------------------

class TestSnapshotRowRealisedUnrealisedSplit:
    """build_snapshot_position_row must populate real realised/unrealised
    values (derived from mark-to-market on qty/avg/ltp) rather than leaving
    them at the PositionRow struct default of 0.0 — otherwise
    `_row_baseline_diff_day_pnl`'s "both zero -> fall back to pnl" trigger
    and any consumer using `realised+unrealised` directly (frontend
    `currentTotalProfit`) silently compute 0 instead of the real total.

    Auditor repro: `baseDayPnlForPosition({realised:0, unrealised:0,
    pnl:500, prev_settlement_pnl:400})` returned -400 (wrong) instead of
    100 — because both fields were left at the struct default. These tests
    prove the backend now populates real values so that repro can't recur.
    """

    def test_open_row_splits_realised_and_unrealised(self):
        """Open row (qty>0): unrealised = (ltp-avg)*qty; realised absorbs
        the remainder of total_pnl.

        qty=10, avg=100, ltp=150 -> unrealised=(150-100)*10=500.
        total_pnl=500 -> realised=500-500=0.
        """
        from backend.api.routes.positions_helpers import build_snapshot_position_row

        row = build_snapshot_position_row(
            account="ZG0790", symbol="RELIANCE", exchange="NSE",
            qty=10, avg_cost=100.0, ltp=150.0,
            day_pnl=0.0, total_pnl=500.0, extras={},
        )
        assert row.unrealised == pytest.approx(500.0)
        assert row.realised == pytest.approx(0.0)
        assert row.realised + row.unrealised == pytest.approx(row.pnl)

    def test_closed_row_all_realised_no_phantom_zero(self):
        """Auditor repro: closed/flat snapshot row (qty=0) must NOT leave
        realised=unrealised=0 while pnl=500 — that combination is exactly
        what caused `baseDayPnlForPosition` to fall through to the wrong
        branch. unrealised must be 0 (no open qty) and realised must
        absorb the FULL total_pnl (500), so realised+unrealised=pnl=500
        (never both-zero-with-nonzero-pnl)."""
        from backend.api.routes.positions_helpers import build_snapshot_position_row

        row = build_snapshot_position_row(
            account="ZG0790", symbol="RELIANCE", exchange="NSE",
            qty=0, avg_cost=100.0, ltp=150.0,
            day_pnl=0.0, total_pnl=500.0, extras={},
        )
        assert row.unrealised == 0.0
        assert row.realised == pytest.approx(500.0)
        assert not (row.realised == 0.0 and row.unrealised == 0.0), (
            "closed row must not leave realised=unrealised=0 while pnl=500 "
            "(the exact combination that broke the frontend's != null "
            "fallback in the audited bug)"
        )

    def test_end_to_end_day_pnl_matches_auditor_expectation(self):
        """Full pipeline proof of the auditor's repro, using the SSOT
        baseline_diff_day_pnl formula the backend and frontend both use:
        prev_settlement_pnl=400, total_pnl=500 (closed row) -> Day P&L
        must be 100 (500-400), NOT -400 (the pre-fix bug)."""
        from backend.api.routes.positions_helpers import (
            build_snapshot_position_row, _row_baseline_diff_day_pnl,
        )

        row = build_snapshot_position_row(
            account="ZG0790", symbol="RELIANCE", exchange="NSE",
            qty=0, avg_cost=100.0, ltp=150.0,
            day_pnl=0.0, total_pnl=500.0, extras={},
            prev_settlement_pnl=400.0,
        )
        day_pnl = _row_baseline_diff_day_pnl(row)
        assert day_pnl == pytest.approx(100.0), (
            f"Day P&L must be 100.0 (500-400); the pre-fix bug produced "
            f"-400.0 because realised=unrealised=0 with pnl=500 fell "
            f"through to the frontend's own-pnl fallback incorrectly. Got {day_pnl}"
        )

    def test_zero_qty_zero_avg_falls_back_to_pnl_via_ssot_rule(self):
        """Degenerate case: avg_cost=0 (no cost basis info at all) ->
        unrealised=0, realised=total_pnl -- still satisfies the SSOT
        "both zero -> use pnl" rule harmlessly when total_pnl is itself 0,
        and correctly attributes all P&L to realised otherwise."""
        from backend.api.routes.positions_helpers import build_snapshot_position_row

        row = build_snapshot_position_row(
            account="ZG0790", symbol="NEW_SYM", exchange="NSE",
            qty=5, avg_cost=0.0, ltp=0.0,
            day_pnl=0.0, total_pnl=0.0, extras={},
        )
        assert row.unrealised == 0.0
        assert row.realised == 0.0
        # pnl is also 0 here, so the SSOT fallback is a harmless no-op.
        assert row.pnl == 0.0
