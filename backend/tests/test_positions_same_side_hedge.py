"""
Tests for _auto_pair_same_side_hedges() — "S-group" same-side CE+PE hedge
detection (backend/api/routes/positions.py), added 2026-10-18 per operator:
"sell pe, sell ce are week hedges... implement it."

_auto_pair_positions() only matches OPPOSITE-sign legs within the same
(account, root_symbol, option_type) — it cannot detect a same-side hedge
(e.g. sell CE + sell PE, a short strangle). This function runs AFTER
_auto_pair_positions on its own output, and ONLY touches rows that came
out as pure orphans (pair_group_key is None, is_orphan is True) — a row
even partially P-matched is left alone.

Coverage:
1. Short strangle (sell CE + sell PE, same qty) -> S1, fully matched.
2. Long straddle (buy CE + buy PE) -> S1, independent of same-side bucketing.
3. Lot-split: sell CE 3 lots + sell PE 2 lots -> S1 matches 2, CE leg keeps
   orphan_qty=1 (still is_orphan=False since partially matched).
4. A row already P-matched (even partially) is never touched by the S-pass.
5. CE/PE legs in DIFFERENT accounts never match.
6. option_type is None (future/equity) rows are never eligible.
7. S-counter is global/independent of the P-counter — "P1" and "S1" can
   coexist in one response without collision.
8. A genuinely single-sided orphan (sell CE, no PE at all) stays a plain
   orphan, not forced into a fake S-group.
9. Opposite-sign CE/PE (e.g. buy CE + sell PE) never match as same-side
   (that's a directional combo, not a hedge).
"""

from __future__ import annotations


def _make_row(account: str, tradingsymbol: str, exchange: str, quantity: int):
    from backend.api.schemas import PositionRow
    return PositionRow(
        account=account,
        tradingsymbol=tradingsymbol,
        exchange=exchange,
        product="NRML",
        quantity=quantity,
        average_price=100.0,
        prev_close=100.0,
        pnl=0.0,
    )


def _rows_by_symbol(rows):
    return {r.tradingsymbol: r for r in rows}


# ---------------------------------------------------------------------------
# 1. Short strangle
# ---------------------------------------------------------------------------

def test_short_strangle_matches_into_s1():
    from backend.api.routes.positions import _auto_pair_positions, _auto_pair_same_side_hedges

    rows = [
        _make_row("ZG0790", "NIFTY24AUG24000CE", "NFO", -5),
        _make_row("ZG0790", "NIFTY24AUG23000PE", "NFO", -5),
    ]
    rows = _auto_pair_positions(rows)
    rows = _auto_pair_same_side_hedges(rows)
    by_sym = _rows_by_symbol(rows)

    ce = by_sym["NIFTY24AUG24000CE"]
    pe = by_sym["NIFTY24AUG23000PE"]
    assert ce.pair_group_key == "S1"
    assert pe.pair_group_key == "S1"
    assert ce.is_orphan is False
    assert pe.is_orphan is False
    assert ce.paired_qty == 5
    assert pe.paired_qty == 5
    assert ce.orphan_qty == 0
    assert pe.orphan_qty == 0


# ---------------------------------------------------------------------------
# 2. Long straddle
# ---------------------------------------------------------------------------

def test_long_straddle_matches_into_s1():
    from backend.api.routes.positions import _auto_pair_positions, _auto_pair_same_side_hedges

    rows = [
        _make_row("ZG0790", "NIFTY24AUG24000CE", "NFO", 3),
        _make_row("ZG0790", "NIFTY24AUG24000PE", "NFO", 3),
    ]
    rows = _auto_pair_positions(rows)
    rows = _auto_pair_same_side_hedges(rows)
    by_sym = _rows_by_symbol(rows)

    assert by_sym["NIFTY24AUG24000CE"].pair_group_key == "S1"
    assert by_sym["NIFTY24AUG24000PE"].pair_group_key == "S1"


# ---------------------------------------------------------------------------
# 3. Lot-split same-side match
# ---------------------------------------------------------------------------

def test_lot_split_same_side_partial_match():
    from backend.api.routes.positions import _auto_pair_positions, _auto_pair_same_side_hedges

    rows = [
        _make_row("ZG0790", "NIFTY24AUG24000CE", "NFO", -3),
        _make_row("ZG0790", "NIFTY24AUG23000PE", "NFO", -2),
    ]
    rows = _auto_pair_positions(rows)
    rows = _auto_pair_same_side_hedges(rows)
    by_sym = _rows_by_symbol(rows)

    ce = by_sym["NIFTY24AUG24000CE"]
    pe = by_sym["NIFTY24AUG23000PE"]
    assert ce.pair_group_key == "S1"
    assert pe.pair_group_key == "S1"
    assert ce.paired_qty == 2
    assert ce.orphan_qty == 1
    assert ce.is_orphan is False
    assert pe.paired_qty == 2
    assert pe.orphan_qty == 0


# ---------------------------------------------------------------------------
# 4. Already P-matched rows are untouched by the S-pass
# ---------------------------------------------------------------------------

def test_p_matched_row_never_touched_by_s_pass():
    from backend.api.routes.positions import _auto_pair_positions, _auto_pair_same_side_hedges

    rows = [
        # CE spread: these two P-match each other (opposite sign, same type).
        _make_row("ZG0790", "NIFTY24AUG24000CE", "NFO", 4),
        _make_row("ZG0790", "NIFTY24AUG25000CE", "NFO", -4),
        # A lone PE orphan that could otherwise bridge with a CE orphan —
        # but there IS no CE orphan left (both CE rows are fully P-matched).
        _make_row("ZG0790", "NIFTY24AUG23000PE", "NFO", -4),
    ]
    rows = _auto_pair_positions(rows)
    before = {r.tradingsymbol: (r.pair_group_key, r.is_orphan) for r in rows}
    rows = _auto_pair_same_side_hedges(rows)
    after = {r.tradingsymbol: (r.pair_group_key, r.is_orphan) for r in rows}

    # Both CE legs are P1 both before and after — the S-pass must not alter them.
    assert before["NIFTY24AUG24000CE"] == ("P1", False)
    assert after["NIFTY24AUG24000CE"] == ("P1", False)
    assert before["NIFTY24AUG25000CE"] == ("P1", False)
    assert after["NIFTY24AUG25000CE"] == ("P1", False)
    # The PE leg has no CE orphan counterpart left, so it stays a plain orphan.
    assert after["NIFTY24AUG23000PE"] == (None, True)


def test_partially_p_matched_row_not_eligible_for_s_pass():
    from backend.api.routes.positions import _auto_pair_positions, _auto_pair_same_side_hedges

    rows = [
        # Long 5 vs short 2 -> P1 matches 2, long leg has orphan_qty=3 but
        # pair_group_key="P1" (not None) -> NOT eligible for the S-pass,
        # even though it has leftover unmatched quantity.
        _make_row("ZG0790", "NIFTY24AUG24000CE", "NFO", 5),
        _make_row("ZG0790", "NIFTY24AUG25000CE", "NFO", -2),
        _make_row("ZG0790", "NIFTY24AUG23000PE", "NFO", 3),
    ]
    rows = _auto_pair_positions(rows)
    rows = _auto_pair_same_side_hedges(rows)
    by_sym = _rows_by_symbol(rows)

    long_ce = by_sym["NIFTY24AUG24000CE"]
    assert long_ce.pair_group_key == "P1"
    assert long_ce.orphan_qty == 3
    # The PE leg remains a plain orphan — it never gets a chance to bridge
    # with the CE's leftover 3 lots, since that CE row isn't a pure orphan.
    pe = by_sym["NIFTY24AUG23000PE"]
    assert pe.pair_group_key is None
    assert pe.is_orphan is True


# ---------------------------------------------------------------------------
# 5. Different accounts never match
# ---------------------------------------------------------------------------

def test_different_accounts_never_match():
    from backend.api.routes.positions import _auto_pair_positions, _auto_pair_same_side_hedges

    rows = [
        _make_row("ZG0790", "NIFTY24AUG24000CE", "NFO", -5),
        _make_row("ZJ6294", "NIFTY24AUG23000PE", "NFO", -5),
    ]
    rows = _auto_pair_positions(rows)
    rows = _auto_pair_same_side_hedges(rows)
    by_sym = _rows_by_symbol(rows)

    assert by_sym["NIFTY24AUG24000CE"].pair_group_key is None
    assert by_sym["NIFTY24AUG24000CE"].is_orphan is True
    assert by_sym["NIFTY24AUG23000PE"].pair_group_key is None
    assert by_sym["NIFTY24AUG23000PE"].is_orphan is True


# ---------------------------------------------------------------------------
# 6. Futures/equity rows (option_type is None) never eligible
# ---------------------------------------------------------------------------

def test_future_rows_never_eligible_for_s_pass():
    from backend.api.routes.positions import _auto_pair_positions, _auto_pair_same_side_hedges

    rows = [
        _make_row("ZG0790", "NIFTY24AUGFUT", "NFO", -5),
        _make_row("ZG0790", "NIFTY24AUG23000PE", "NFO", -5),
    ]
    rows = _auto_pair_positions(rows)
    rows = _auto_pair_same_side_hedges(rows)
    by_sym = _rows_by_symbol(rows)

    assert by_sym["NIFTY24AUGFUT"].pair_group_key is None
    assert by_sym["NIFTY24AUGFUT"].is_orphan is True
    assert by_sym["NIFTY24AUG23000PE"].pair_group_key is None
    assert by_sym["NIFTY24AUG23000PE"].is_orphan is True


# ---------------------------------------------------------------------------
# 7. S-counter is global/independent of the P-counter
# ---------------------------------------------------------------------------

def test_s_counter_independent_of_p_counter_no_collision():
    from backend.api.routes.positions import _auto_pair_positions, _auto_pair_same_side_hedges

    rows = [
        # P1: NIFTY CE spread.
        _make_row("ZG0790", "NIFTY24AUG24000CE", "NFO", 2),
        _make_row("ZG0790", "NIFTY24AUG25000CE", "NFO", -2),
        # S1: BANKNIFTY short strangle (different root -> independent group).
        _make_row("ZG0790", "BANKNIFTY24AUG50000CE", "NFO", -3),
        _make_row("ZG0790", "BANKNIFTY24AUG49000PE", "NFO", -3),
    ]
    rows = _auto_pair_positions(rows)
    rows = _auto_pair_same_side_hedges(rows)
    by_sym = _rows_by_symbol(rows)

    assert by_sym["NIFTY24AUG24000CE"].pair_group_key == "P1"
    assert by_sym["NIFTY24AUG25000CE"].pair_group_key == "P1"
    assert by_sym["BANKNIFTY24AUG50000CE"].pair_group_key == "S1"
    assert by_sym["BANKNIFTY24AUG49000PE"].pair_group_key == "S1"


# ---------------------------------------------------------------------------
# 8. Single-sided orphan stays a plain orphan
# ---------------------------------------------------------------------------

def test_single_sided_orphan_not_forced_into_fake_s_group():
    from backend.api.routes.positions import _auto_pair_positions, _auto_pair_same_side_hedges

    rows = [_make_row("ZG0790", "NIFTY24AUG24000CE", "NFO", -5)]
    rows = _auto_pair_positions(rows)
    rows = _auto_pair_same_side_hedges(rows)

    assert rows[0].pair_group_key is None
    assert rows[0].is_orphan is True


# ---------------------------------------------------------------------------
# 9. Opposite-sign CE/PE is a directional combo, not a same-side hedge
# ---------------------------------------------------------------------------

def test_opposite_sign_ce_pe_never_matches_as_same_side():
    from backend.api.routes.positions import _auto_pair_positions, _auto_pair_same_side_hedges

    rows = [
        _make_row("ZG0790", "NIFTY24AUG24000CE", "NFO", 5),   # long CE
        _make_row("ZG0790", "NIFTY24AUG23000PE", "NFO", -5),  # short PE
    ]
    rows = _auto_pair_positions(rows)
    rows = _auto_pair_same_side_hedges(rows)
    by_sym = _rows_by_symbol(rows)

    assert by_sym["NIFTY24AUG24000CE"].pair_group_key is None
    assert by_sym["NIFTY24AUG24000CE"].is_orphan is True
    assert by_sym["NIFTY24AUG23000PE"].pair_group_key is None
    assert by_sym["NIFTY24AUG23000PE"].is_orphan is True
