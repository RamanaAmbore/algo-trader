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
    close_price=500.0,
    quantity=10,
):
    from backend.api.schemas import PositionRow
    return PositionRow(
        account=account,
        tradingsymbol="NIFTY25JUNFUT",
        exchange="NFO",
        product="NRML",
        quantity=quantity,
        average_price=490.0,
        close_price=close_price,
        last_price=close_price + day_change_val / quantity,
        pnl=pnl,
        pnl_percentage=0.0,
        day_change_val=day_change_val,
        day_change_percentage=0.0,
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
                  close_price=500.0, quantity=10),
        _make_row("ZG0790", pnl=500.0,  day_change_val=100.0,
                  close_price=250.0, quantity=5),
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
                  close_price=500.0, quantity=10),
        _make_row("ZJ6294", pnl=2000.0, day_change_val=400.0,
                  close_price=200.0, quantity=5),
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
                      close_price=0.0, quantity=5)]
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
# 6. MCX snapshot multiplier — extract_snapshot_multiplier reads the Kite
#    multiplier field so the snapshot path returns contracts (not lots)
# ---------------------------------------------------------------------------

def test_extract_snapshot_multiplier_mcx():
    """MCX CRUDEOIL: multiplier=100 → returns 100."""
    import json
    from backend.api.routes.positions_helpers import extract_snapshot_multiplier

    pj = json.dumps({"multiplier": 100, "tradingsymbol": "CRUDEOIL26JUL7500CE"})
    assert extract_snapshot_multiplier(pj) == 100


def test_extract_snapshot_multiplier_nfo():
    """NFO NIFTY: multiplier=1 → returns 1 (no-op for contracts)."""
    import json
    from backend.api.routes.positions_helpers import extract_snapshot_multiplier

    pj = json.dumps({"multiplier": 1, "tradingsymbol": "NIFTY26JULFUT"})
    assert extract_snapshot_multiplier(pj) == 1


def test_extract_snapshot_multiplier_missing():
    """Missing multiplier field → returns 1 (safe no-op)."""
    import json
    from backend.api.routes.positions_helpers import extract_snapshot_multiplier

    pj = json.dumps({"tradingsymbol": "SOMESTOCK"})
    assert extract_snapshot_multiplier(pj) == 1


def test_extract_snapshot_multiplier_none_payload():
    """None payload → returns 1 (safe no-op)."""
    from backend.api.routes.positions_helpers import extract_snapshot_multiplier

    assert extract_snapshot_multiplier(None) == 1


def test_snapshot_mcx_qty_contracts_after_multiplier():
    """Snapshot path: 1-lot CRUDEOIL (daily_book.qty=1, multiplier=100)
    must produce quantity=100 contracts — the same value the live path
    (broker_apis.fetch_positions) returns after applying multiplier.
    """
    import json
    from decimal import Decimal
    from backend.api.routes.positions_helpers import (
        build_snapshot_position_row,
        extract_snapshot_multiplier,
    )

    # Simulates the _positions_snapshot loop for one MCX option row.
    payload_json = json.dumps({
        "tradingsymbol": "CRUDEOIL26JUL7500CE",
        "exchange": "MCX",
        "multiplier": 100,
    })
    qty_from_db = 1          # daily_book.qty is in lots for MCX
    multiplier  = extract_snapshot_multiplier(payload_json)
    effective_qty = qty_from_db * multiplier  # → 100 contracts

    row = build_snapshot_position_row(
        account="ZG0790",
        symbol="CRUDEOIL26JUL7500CE",
        exchange="MCX",
        qty=effective_qty,
        avg_cost=Decimal("426.30"),
        ltp=Decimal("180.00"),
        day_pnl=Decimal("-24630.00"),
        total_pnl=Decimal("-24630.00"),
        extras={},
    )
    # After multiplier: 100 contracts, not 1 lot
    assert row.quantity == 100, (
        f"Expected 100 contracts (1 lot × 100), got {row.quantity}"
    )
    assert row.overnight_quantity == 100
    # pnl and day_change_val are from DB (absolute ₹) — not scaled by qty
    assert math.isclose(row.pnl, -24630.0, rel_tol=1e-6)
