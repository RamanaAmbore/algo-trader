"""Tests for _auto_pair_positions() lot-waterfall auto-pairing logic.

The lot-waterfall algorithm pairs longs vs shorts largest-first within each
(account, root_symbol) group. Each paired lot gets a sequential key ("P1", "P2", etc.).
Unmatched remainder gets is_orphan=True.

A regression existed where longs.pop(0) was used instead of longs_q.pop(0),
which would cause an infinite loop. These tests verify the fix holds.
"""

import pytest
from backend.api.routes.positions import _auto_pair_positions, _root_symbol
from backend.api.schemas import PositionRow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_position(
    account: str = "ZG0790",
    tradingsymbol: str = "NIFTY26AUG24500CE",
    quantity: int = 1,
    **kwargs,
) -> PositionRow:
    """Build a minimal PositionRow for testing."""
    defaults = dict(
        account=account,
        tradingsymbol=tradingsymbol,
        exchange="NFO",
        product="MIS",
        quantity=quantity,
        average_price=100.0,
        close_price=100.0,
        pnl=0.0,
        last_price=100.0,
        unrealised=0.0,
        realised=0.0,
    )
    return PositionRow(**{**defaults, **kwargs})


# ---------------------------------------------------------------------------
# Root symbol extraction
# ---------------------------------------------------------------------------

def test_root_symbol_strips_expiry_month_code_suffix():
    """BANKNIFTY24AUGFUT → BANKNIFTY (month-code expiry)."""
    assert _root_symbol("BANKNIFTY24AUGFUT") == "BANKNIFTY"


def test_root_symbol_strips_numeric_strike_and_type():
    """NIFTY24800CE → NIFTY (numeric strike + option type)."""
    assert _root_symbol("NIFTY24800CE") == "NIFTY"


def test_root_symbol_strips_crudeoil_futures():
    """CRUDEOIL24AUGFUT → CRUDEOIL."""
    assert _root_symbol("CRUDEOIL24AUGFUT") == "CRUDEOIL"


def test_root_symbol_strips_goldm_futures():
    """GOLDM24AUGFUT → GOLDM."""
    assert _root_symbol("GOLDM24AUGFUT") == "GOLDM"


def test_root_symbol_passthrough_for_equity():
    """INFY (no expiry/strike) → INFY."""
    assert _root_symbol("INFY") == "INFY"


# ---------------------------------------------------------------------------
# Test 1: Long + Short portfolio — waterfall terminates
# ---------------------------------------------------------------------------

def test_auto_pair_long_short_matching_terminates():
    """Long + short equal qty for same root → both paired, no infinite loop."""
    positions = [
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=1),
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=-1),
    ]

    result = _auto_pair_positions(positions)

    assert len(result) == 2, "result length must match input"
    assert result[0].pair_group_key == "P1", "long should have pair_group_key P1"
    assert result[1].pair_group_key == "P1", "short should have pair_group_key P1"
    assert result[0].is_orphan is False, "long should not be orphan"
    assert result[1].is_orphan is False, "short should not be orphan"
    assert result[0].paired_qty == 1, "long paired_qty should be 1"
    assert result[1].paired_qty == 1, "short paired_qty should be 1"
    assert result[0].orphan_qty == 0, "long orphan_qty should be 0"
    assert result[1].orphan_qty == 0, "short orphan_qty should be 0"


def test_auto_pair_multiple_pairs_waterfall():
    """Multiple long+short pairs: largest-first waterfall, multiple P keys.

    Positions: longs=[3, 2], shorts=[-2, -2]
    Waterfall:
      P1: long(3) matches short(-2) → pair 2, long rem 1, short exhausted
      P2: long(3 rem=1) matches short(-2) → pair 1, long exhausted, short rem -1
      P3: long(2) matches short(rem=-1) → pair 1, long rem 1, short exhausted

    After: long(2) has 1 remaining, but it's been paired (pair_key=P3), so
    is_orphan stays False (only never-paired entries become orphan).
    """
    positions = [
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=3),
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=2),
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=-2),
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=-2),
    ]

    result = _auto_pair_positions(positions)

    assert len(result) == 4, "result length must match input"
    # All 4 rows should be touched (paired, never fully orphan)
    all_paired = [r for r in result if r.pair_group_key is not None]
    assert len(all_paired) == 4, "all 4 rows should have a pair_group_key (all touched by waterfall)"

    orphan_rows = [r for r in result if r.is_orphan]
    assert len(orphan_rows) == 0, "no rows should be fully orphan (all were paired at some point)"


# ---------------------------------------------------------------------------
# Test 2: All-long portfolio — unmatched gets orphan
# ---------------------------------------------------------------------------

def test_auto_pair_all_longs_all_orphan():
    """All-long portfolio: no shorts to match → all orphan, no pair_group_key."""
    positions = [
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=1),
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=2),
    ]

    result = _auto_pair_positions(positions)

    assert len(result) == 2, "result length must match input"
    assert all(r.is_orphan for r in result), "all longs should be orphan"
    assert all(r.pair_group_key is None for r in result), "no pair_group_key for orphans"
    assert all(r.paired_qty == 0 for r in result), "all paired_qty should be 0"
    assert result[0].orphan_qty == 1, "first long orphan_qty should be 1"
    assert result[1].orphan_qty == 2, "second long orphan_qty should be 2"


def test_auto_pair_all_shorts_all_orphan():
    """All-short portfolio: no longs to match → all orphan."""
    positions = [
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=-1),
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=-3),
    ]

    result = _auto_pair_positions(positions)

    assert len(result) == 2, "result length must match input"
    assert all(r.is_orphan for r in result), "all shorts should be orphan"
    assert all(r.pair_group_key is None for r in result), "no pair_group_key for orphans"
    assert result[0].orphan_qty == 1, "first short orphan_qty should be 1"
    assert result[1].orphan_qty == 3, "second short orphan_qty should be 3"


# ---------------------------------------------------------------------------
# Test 3: Mixed portfolio — larger long partially matched
# ---------------------------------------------------------------------------

def test_auto_pair_larger_long_partial_match():
    """Long qty=2 + short qty=-1 → qty=1 left matched, 1 orphan."""
    positions = [
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=2),
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=-1),
    ]

    result = _auto_pair_positions(positions)

    assert len(result) == 2, "result length must match input"
    # Both should be touched (one pair key, one orphan marker)
    long_row = next(r for r in result if r.quantity == 2)
    short_row = next(r for r in result if r.quantity == -1)

    assert long_row.pair_group_key == "P1", "matched long should have pair_group_key"
    assert short_row.pair_group_key == "P1", "matched short should have pair_group_key"
    assert long_row.paired_qty == 1, "long paired_qty should be 1"
    assert short_row.paired_qty == 1, "short paired_qty should be 1"
    assert long_row.orphan_qty == 1, "long orphan_qty should be 1 (2 - 1)"
    assert short_row.orphan_qty == 0, "short orphan_qty should be 0 (1 - 1)"
    assert long_row.is_orphan is False, "partially-matched long is not orphan"
    assert short_row.is_orphan is False, "fully-matched short is not orphan"


def test_auto_pair_larger_short_partial_match():
    """Long qty=1 + short qty=-3 → qty=1 matched, short qty=2 orphan."""
    positions = [
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=1),
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=-3),
    ]

    result = _auto_pair_positions(positions)

    assert len(result) == 2, "result length must match input"
    long_row = next(r for r in result if r.quantity == 1)
    short_row = next(r for r in result if r.quantity == -3)

    assert long_row.pair_group_key == "P1", "matched long should have pair_group_key"
    assert short_row.pair_group_key == "P1", "matched short should have pair_group_key"
    assert long_row.paired_qty == 1, "long paired_qty should be 1"
    assert short_row.paired_qty == 1, "short paired_qty should be 1"
    assert long_row.orphan_qty == 0, "long orphan_qty should be 0 (1 - 1)"
    assert short_row.orphan_qty == 2, "short orphan_qty should be 2 (3 - 1)"
    assert long_row.is_orphan is False, "fully-matched long is not orphan"
    assert short_row.is_orphan is False, "partially-matched short is not orphan"


# ---------------------------------------------------------------------------
# Test 4: Largest-first waterfall
# ---------------------------------------------------------------------------

def test_auto_pair_largest_first_waterfall():
    """Waterfall prioritizes by quantity size (largest first).

    Positions: longs=[5, 1], shorts=[-4, -2]
    Waterfall:
      P1: long(5) with short(-4) → pair 4, long rem 1, short exhausted
      P2: long(5 rem=1) with short(-2) → pair 1, long exhausted, short rem -1
      P3: long(1) with short(rem=-1) → pair 1, both exhausted

    All rows are touched by the waterfall, so none are orphan (is_orphan=False for all).
    """
    positions = [
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=1),  # smallest long
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=5),  # largest long
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=-2),  # smaller short
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=-4),  # larger short
    ]

    result = _auto_pair_positions(positions)

    assert len(result) == 4, "result length must match input"

    # All rows should have pair_group_keys from the waterfall
    paired_rows = [r for r in result if r.pair_group_key is not None]
    assert len(paired_rows) == 4, "all 4 rows should be paired (touched by waterfall)"

    # No row should be fully orphan (all were matched at least once)
    orphan_rows = [r for r in result if r.is_orphan]
    assert len(orphan_rows) == 0, "no rows should be fully orphan"

    # Largest short should be in P1 with paired_qty=4
    largest_short = next((r for r in result if r.quantity == -4), None)
    assert largest_short is not None, "largest short should exist"
    assert largest_short.pair_group_key == "P1", "largest short should be in P1 (first pair)"
    assert largest_short.paired_qty == 4, "largest short paired_qty should be 4"


# ---------------------------------------------------------------------------
# Test 5: Multiple accounts — grouped separately
# ---------------------------------------------------------------------------

def test_auto_pair_different_accounts_separate_groups():
    """Positions from different accounts should not cross-pair."""
    positions = [
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=1),
        _make_position(account="ZJ6294", tradingsymbol="NIFTY26AUG24500CE", quantity=-1),
    ]

    result = _auto_pair_positions(positions)

    assert len(result) == 2, "result length must match input"
    zg_row = next(r for r in result if r.account == "ZG0790")
    zj_row = next(r for r in result if r.account == "ZJ6294")

    assert zg_row.is_orphan is True, "ZG0790 long should be orphan (no matching short in same account)"
    assert zj_row.is_orphan is True, "ZJ6294 short should be orphan (no matching long in same account)"
    assert zg_row.pair_group_key is None, "ZG0790 row should have no pair_group_key"
    assert zj_row.pair_group_key is None, "ZJ6294 row should have no pair_group_key"


def test_auto_pair_different_symbols_separate_groups():
    """Positions with different root symbols should not cross-pair."""
    positions = [
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=1),
        _make_position(account="ZG0790", tradingsymbol="BANKNIFTY26AUG45000CE", quantity=-1),
    ]

    result = _auto_pair_positions(positions)

    assert len(result) == 2, "result length must match input"
    nifty_row = next(r for r in result if "NIFTY26" in r.tradingsymbol and "BANKNIFTY" not in r.tradingsymbol)
    banknifty_row = next(r for r in result if "BANKNIFTY" in r.tradingsymbol)

    assert nifty_row.is_orphan is True, "NIFTY long should be orphan (different root symbol)"
    assert banknifty_row.is_orphan is True, "BANKNIFTY short should be orphan (different root symbol)"


# ---------------------------------------------------------------------------
# Test 6: Flat positions (qty == 0)
# ---------------------------------------------------------------------------

def test_auto_pair_flat_position_stays_default():
    """Flat position (qty=0) stays with defaults (is_orphan=False, pair_group_key=None)."""
    positions = [
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=0),
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=1),
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=-1),
    ]

    result = _auto_pair_positions(positions)

    assert len(result) == 3, "result length must match input"
    flat_row = next(r for r in result if r.quantity == 0)
    long_row = next(r for r in result if r.quantity == 1)
    short_row = next(r for r in result if r.quantity == -1)

    assert flat_row.is_orphan is False, "flat position should have is_orphan=False (default)"
    assert flat_row.pair_group_key is None, "flat position should have pair_group_key=None (default)"
    assert flat_row.paired_qty == 0, "flat position should have paired_qty=0"
    assert flat_row.orphan_qty == 0, "flat position should have orphan_qty=0"

    # Long and short should still pair (ignoring flat)
    assert long_row.pair_group_key == "P1", "long should pair with short"
    assert short_row.pair_group_key == "P1", "short should pair with long"


# ---------------------------------------------------------------------------
# Test 7: Empty input
# ---------------------------------------------------------------------------

def test_auto_pair_empty_input():
    """Empty position list should return empty list."""
    result = _auto_pair_positions([])

    assert result == [], "empty input should return empty list"


# ---------------------------------------------------------------------------
# Test 8: Complex waterfall with many positions
# ---------------------------------------------------------------------------

def test_auto_pair_complex_waterfall_with_remainder():
    """
    Complex case: multiple long+short pairs with remainder.
    Longs:  [10, 8, 5]
    Shorts: [-7, -6]

    Waterfall:
      P1: long(10) with short(-7) → pair 7, long rem 3, short exhausted
      P2: long(10 rem=3) with short(-6) → pair 3, long exhausted, short rem -3
      P3: long(8) with short(rem=-3) → pair 3, long rem 5, short exhausted
      P4: long(8 rem=5) cannot pair (shorts exhausted)
      Remaining: long(5) never paired → orphan

    So the final state: qty=10 touched twice (P1 then P2), qty=8 touched twice (P3 then continues),
    qty=5 never touched, shorts all touched.
    """
    positions = [
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=10),
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=8),
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=5),
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=-7),
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=-6),
    ]

    result = _auto_pair_positions(positions)

    assert len(result) == 5, "result length must match input"

    # Check that both shorts were touched (paired)
    short_rows = [r for r in result if r.quantity < 0]
    assert all(r.pair_group_key is not None for r in short_rows), "all shorts should be paired"

    # Check that qty=5 long is orphan (never paired)
    qty5_row = next((r for r in result if r.quantity == 5), None)
    assert qty5_row is not None, "qty=5 row should exist"
    assert qty5_row.is_orphan is True, "qty=5 long should be orphan (never paired)"
    assert qty5_row.pair_group_key is None, "orphan should have no pair_group_key"
    assert qty5_row.orphan_qty == 5, "orphan_qty should match quantity"

    # Other longs should have pair keys (even if partially matched)
    qty10_row = next((r for r in result if r.quantity == 10), None)
    qty8_row = next((r for r in result if r.quantity == 8), None)
    assert qty10_row is not None, "qty=10 row should exist"
    assert qty8_row is not None, "qty=8 row should exist"
    assert qty10_row.pair_group_key is not None, "qty=10 should be paired"
    assert qty8_row.pair_group_key is not None, "qty=8 should be paired"


# ---------------------------------------------------------------------------
# Test 9: No infinite loop on longs_q.pop(0) vs longs.pop(0) bug
# ---------------------------------------------------------------------------

def test_auto_pair_no_infinite_loop_waterfall():
    """
    Regression test: ensure longs_q.pop(0) (mutable waterfall list) is used,
    not longs.pop(0) (original static list). Original bug would cause
    an infinite loop because the original list never shrinks, so the
    while loop never exits.

    This test uses a moderate-sized case that would clearly time-out
    if the bug (infinite loop) were present.
    """
    positions = [
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=10),
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=-5),
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=-4),
    ]

    # This should complete without hanging
    result = _auto_pair_positions(positions)

    assert len(result) == 3, "result length must match input"
    # Verify the waterfall actually happened (no infinite loop)
    paired_rows = [r for r in result if r.pair_group_key is not None]
    assert len(paired_rows) >= 2, "at least 2 rows should be paired"


# ---------------------------------------------------------------------------
# Test 10: Same position appears multiple times (multiple entries for same symbol)
# ---------------------------------------------------------------------------

def test_auto_pair_multiple_entries_same_symbol():
    """Same symbol, same account, multiple qty entries → waterfall matches them.

    Longs: [3, 2], Shorts: [-2, -1]
    Waterfall:
      P1: long(3) with short(-2) → pair 2, long rem 1, short exhausted
      P2: long(3 rem=1) with short(-1) → pair 1, long exhausted, short exhausted

    Remaining: long(2) never paired in the main waterfall → orphan
    """
    positions = [
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=3),
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=2),
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=-1),
        _make_position(account="ZG0790", tradingsymbol="NIFTY26AUG24500CE", quantity=-2),
    ]

    result = _auto_pair_positions(positions)

    assert len(result) == 4, "result length must match input"

    # qty=3 and both shorts should be paired
    qty3_row = next((r for r in result if r.quantity == 3), None)
    short_rows = [r for r in result if r.quantity < 0]
    assert qty3_row is not None, "qty=3 row should exist"
    assert qty3_row.pair_group_key is not None, "qty=3 should be paired"
    assert all(r.pair_group_key is not None for r in short_rows), "both shorts should be paired"

    # qty=2 was never paired (both shorts exhausted before it got its turn)
    qty2_row = next((r for r in result if r.quantity == 2), None)
    assert qty2_row is not None, "qty=2 row should exist"
    assert qty2_row.is_orphan is True, "qty=2 should be orphan (never paired)"
    assert qty2_row.pair_group_key is None, "orphan qty=2 should have no pair_group_key"
    assert qty2_row.orphan_qty == 2, "orphan qty=2 should have orphan_qty=2"
