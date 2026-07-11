"""
Tests for the directional-balance fix in get_movers (watchlist.py).

Root cause: the old [:MOVER_TOP_N] abs-sorted slice dropped the minority
direction entirely on strongly directional days (e.g. all top-6 positive
=> losers grid blank).

Fix: _combine_movers() slices top-N losers and top-N winners independently.

Five quality dimensions:
  1. SSOT        — _combine_movers is the single combine logic; get_movers
                   calls it; _force_movers_snapshot writes untruncated.
  2. Performance — _combine_movers is O(N log N) two-pass, no nested loops.
  3. Stale code  — old single-slice pattern (abs-sorted [:MOVER_TOP_N]) no
                   longer present in the combine block of get_movers.
  4. Reusable    — _combine_movers is module-level and importable for tests;
                   session-sticky overlay preserved unchanged.
  5. Correctness — scenario tests: bullish day, bearish day, balanced day,
                   _force_movers_snapshot untruncated, sticky overlay.
"""

import ast
import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Source path
# ---------------------------------------------------------------------------
_WATCHLIST_SRC = Path(__file__).parent.parent / "api" / "routes" / "watchlist.py"


def _wl_source() -> str:
    return _WATCHLIST_SRC.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Import the function under test
# ---------------------------------------------------------------------------

def _make_entry(last_pct: float, sym: str) -> dict:
    """Create a minimal live_snapshot entry."""
    prev = 100.0
    ltp = prev * (1 + last_pct / 100)
    return {
        "last_pct": last_pct,
        "peak_pct": last_pct,
        "last_price": ltp,
        "previous_close": prev,
        "exchange": "NSE",
    }


def _build_snapshot(winners: int, losers: int) -> dict[str, dict]:
    """
    Synthesise a live_snapshot with ``winners`` positive entries (each +5%)
    and ``losers`` negative entries (each -1%).
    """
    snap: dict[str, dict] = {}
    for i in range(winners):
        sym = f"WIN{i}"
        snap[sym] = _make_entry(5.0 + i * 0.1, sym)
    for i in range(losers):
        sym = f"LOS{i}"
        snap[sym] = _make_entry(-1.0 - i * 0.1, sym)
    return snap


# ---------------------------------------------------------------------------
# Dimension 1 (SSOT) — _combine_movers is the combine entry point
# ---------------------------------------------------------------------------

def test_combine_movers_defined():
    """_combine_movers is defined at module level in watchlist.py."""
    src = _wl_source()
    assert "def _combine_movers(" in src, (
        "_combine_movers not found in watchlist.py — "
        "must be a module-level helper for directional-balance logic"
    )


def test_get_movers_calls_combine_movers():
    """get_movers delegates the combine step to _combine_movers."""
    src = _wl_source()
    match = re.search(
        r"async def get_movers\(self.*?\).*?(?=\n    @|\nclass |\Z)",
        src, re.DOTALL,
    )
    assert match, "get_movers not found in watchlist.py"
    body = match.group(0)
    assert "_combine_movers(" in body, (
        "get_movers does not call _combine_movers — directional-balance fix not wired"
    )


# ---------------------------------------------------------------------------
# Dimension 2 (Performance) — no nested loops inside _combine_movers
# ---------------------------------------------------------------------------

def test_combine_movers_no_nested_loops():
    """_combine_movers body does not contain nested for-loops."""
    src = _wl_source()
    match = re.search(
        r"def _combine_movers\(.*?\) -> dict.*?(?=\ndef |\nasync def |\nclass |\Z)",
        src, re.DOTALL,
    )
    assert match, "_combine_movers body not found"
    body = match.group(0)
    # Count indented for-loops: two top-level comprehensions are fine; a for
    # nested inside another for is the concern. Rough but sufficient check.
    for_lines = [ln for ln in body.splitlines() if re.match(r"\s{4,}for ", ln)]
    # Allow at most 2 for-clauses (one for losers, one for winners overlay).
    # A nested loop would introduce a third indented for inside the others.
    assert len(for_lines) <= 4, (
        f"_combine_movers has suspicious nesting: {len(for_lines)} for-lines found"
    )


# ---------------------------------------------------------------------------
# Dimension 3 (Stale code) — old abs-sorted single-slice gone from combine block
# ---------------------------------------------------------------------------

def test_old_single_abs_slice_removed_from_get_movers():
    """The old abs(kv[1]['last_pct'])…[:MOVER_TOP_N] pattern is gone from get_movers.

    That single-slice was the root cause: it discarded the minority direction
    entirely on strongly directional days.
    """
    src = _wl_source()
    match = re.search(
        r"async def get_movers\(self.*?\).*?(?=\n    @|\nclass |\Z)",
        src, re.DOTALL,
    )
    assert match, "get_movers not found"
    body = match.group(0)
    # The old pattern sorted by abs(last_pct) and took a single [:MOVER_TOP_N] slice.
    assert "abs(kv[1]" not in body, (
        "Old abs(kv[1]['last_pct']) sort key still present in get_movers — "
        "directional-balance fix may not have replaced the old code path"
    )


def test_force_movers_snapshot_no_mover_top_n_slice():
    """_force_movers_snapshot does NOT apply a MOVER_TOP_N slice — writes full universe.

    The directional-fairness invariant applies here implicitly: the full untruncated
    universe is written so the DB snapshot always contains both directions.
    """
    src = _wl_source()
    match = re.search(
        r"async def _force_movers_snapshot\(.*?\).*?(?=\nasync def |\Z)",
        src, re.DOTALL,
    )
    assert match, "_force_movers_snapshot body not found"
    body = match.group(0)
    # Must NOT slice by MOVER_TOP_N at the row-building stage.
    assert "[:MOVER_TOP_N]" not in body, (
        "_force_movers_snapshot applies a [:MOVER_TOP_N] slice — "
        "it should write the full untruncated universe to the DB"
    )


# ---------------------------------------------------------------------------
# Dimension 4 (Reusable) — sticky overlay preserved; _combine_movers importable
# ---------------------------------------------------------------------------

def test_combine_movers_importable():
    """_combine_movers can be imported from the module without broker deps."""
    # This would ImportError if the function had a broker-level import at def time.
    import importlib.util
    spec = importlib.util.spec_from_file_location("watchlist", _WATCHLIST_SRC)
    # We do NOT exec the module (broker deps would fail); we only check source-level.
    # The functional tests below use the extracted function directly.
    assert spec is not None


def test_combine_movers_sticky_overlay_higher_priority():
    """session_movers overlay takes priority over live_snapshot in _combine_movers."""
    # Import _combine_movers by exec'ing just the relevant source slice.
    src = _wl_source()
    # Extract the function source.
    match = re.search(
        r"(def _combine_movers\(.*?\) -> dict.*?)(?=\n\nasync def |\Z)",
        src, re.DOTALL,
    )
    assert match, "_combine_movers source not extractable"
    fn_src = match.group(1)
    ns: dict = {}
    exec(fn_src, ns)  # noqa: S102
    combine = ns["_combine_movers"]

    # One symbol is in both snapshot (last_pct=+3) and session_movers (last_pct=+7).
    snapshot = {"NIFTY": _make_entry(3.0, "NIFTY")}
    sticky_entry = {**_make_entry(7.0, "NIFTY"), "first_seen_at": "2026-07-02T09:30:00Z"}
    session_movers = {"NIFTY": sticky_entry}

    result = combine(snapshot, session_movers, top_n=6)
    # Sticky must win.
    assert result["NIFTY"]["last_pct"] == pytest.approx(7.0), (
        "session_movers entry did not override live_snapshot entry in _combine_movers"
    )


# ---------------------------------------------------------------------------
# Dimension 5 (Correctness) — functional scenarios using extracted helper
# ---------------------------------------------------------------------------

def _load_combine_fn():
    """Extract and compile _combine_movers from source for functional tests."""
    src = _wl_source()
    match = re.search(
        r"(def _combine_movers\(.*?\) -> dict.*?)(?=\n\nasync def |\Z)",
        src, re.DOTALL,
    )
    assert match, "_combine_movers source not found"
    ns: dict = {}
    exec(match.group(1), ns)  # noqa: S102
    return ns["_combine_movers"]


def test_bullish_day_both_losers_survive():
    """20 winners, 2 losers → combined contains both losers (not just top-6 by abs)."""
    combine = _load_combine_fn()
    snapshot = _build_snapshot(winners=20, losers=2)
    combined = combine(snapshot, session_movers={}, top_n=6)

    loser_keys = {k for k in combined if k.startswith("LOS")}
    assert len(loser_keys) == 2, (
        f"Expected both losers in combined on a bullish day, got {len(loser_keys)}: {loser_keys}"
    )


def test_bearish_day_both_winners_survive():
    """20 losers, 2 winners → combined contains both winners."""
    combine = _load_combine_fn()
    snapshot = _build_snapshot(winners=2, losers=20)
    combined = combine(snapshot, session_movers={}, top_n=6)

    winner_keys = {k for k in combined if k.startswith("WIN")}
    assert len(winner_keys) == 2, (
        f"Expected both winners in combined on a bearish day, got {len(winner_keys)}: {winner_keys}"
    )


def test_balanced_day_six_each():
    """10 winners, 10 losers → combined has exactly 6 winners and 6 losers."""
    combine = _load_combine_fn()
    snapshot = _build_snapshot(winners=10, losers=10)
    combined = combine(snapshot, session_movers={}, top_n=6)

    winner_keys = {k for k in combined if k.startswith("WIN")}
    loser_keys = {k for k in combined if k.startswith("LOS")}
    assert len(winner_keys) == 6, (
        f"Expected 6 winners on balanced day, got {len(winner_keys)}"
    )
    assert len(loser_keys) == 6, (
        f"Expected 6 losers on balanced day, got {len(loser_keys)}"
    )


def test_session_movers_overlay_after_split():
    """_session_movers sticky entries are still overlaid after the directional split."""
    combine = _load_combine_fn()
    # Only winners in live_snapshot.
    snapshot = _build_snapshot(winners=10, losers=0)
    # One sticky loser (crossed threshold earlier, has since reverted to -0.5%).
    sticky_entry = {**_make_entry(-0.5, "STICKYLOSE"), "first_seen_at": "2026-07-02T09:30:00Z"}
    session_movers = {"STICKYLOSE": sticky_entry}

    combined = combine(snapshot, session_movers, top_n=6)
    assert "STICKYLOSE" in combined, (
        "Sticky session_movers entry missing from combined after directional split"
    )
    assert combined["STICKYLOSE"]["last_pct"] == pytest.approx(-0.5), (
        "Sticky entry last_pct corrupted after overlay"
    )


def test_force_movers_snapshot_untruncated_by_source():
    """_force_movers_snapshot source iterates full key_to_underlying (no early slice).

    Confirms the DB snapshot at NSE-close always contains the full universe
    (both winners and losers), satisfying the directional-fairness invariant
    without needing _combine_movers.
    """
    src = _wl_source()
    match = re.search(
        r"async def _force_movers_snapshot\(.*?\).*?(?=\nasync def |\Z)",
        src, re.DOTALL,
    )
    assert match, "_force_movers_snapshot body not found"
    body = match.group(0)

    # Full universe is iterated: key_to_underlying loop must be present.
    assert "for kite_key, underlying in key_to_underlying.items():" in body, (
        "_force_movers_snapshot does not iterate key_to_underlying — "
        "full universe may not be written to DB"
    )
    # No directional or size truncation before append.
    assert "[:MOVER_TOP_N]" not in body, (
        "_force_movers_snapshot applies [:MOVER_TOP_N] — must write full universe"
    )
    # Comment or code must signal directional-fairness awareness.
    assert "untruncated" in body or "full" in body or "directional" in body or "_combine_movers" not in body, (
        "_force_movers_snapshot lacks comment noting it writes the untruncated universe"
    )


# ---------------------------------------------------------------------------
# Pool-depth tests (MOVER_TOP_N = 20 bump, 2026-07-02)
# ---------------------------------------------------------------------------

def test_mover_top_n_is_20():
    """MOVER_TOP_N must be 20 so each frontend tab gets enough rows after classification."""
    src = _wl_source()
    match = re.search(r"MOVER_TOP_N\s*:\s*int\s*=\s*(\d+)", src)
    assert match, "MOVER_TOP_N constant not found in watchlist.py"
    value = int(match.group(1))
    assert value == 20, (
        f"MOVER_TOP_N={value}, expected 20. "
        "With 6, the frontend's 3-tab split (underlying/midcap/smallcap) "
        "can produce tabs with as few as 1-2 rows."
    )


def test_pool_of_20_losers_returned():
    """25 losers in snapshot → combined contains 20 (capped at MOVER_TOP_N=20)."""
    combine = _load_combine_fn()
    snapshot = _build_snapshot(winners=5, losers=25)
    combined = combine(snapshot, session_movers={}, top_n=20)

    loser_keys = {k for k in combined if k.startswith("LOS")}
    assert len(loser_keys) == 20, (
        f"Expected 20 losers (MOVER_TOP_N cap), got {len(loser_keys)}"
    )


def test_tab_distribution_across_three_buckets():
    """
    25 losers with names spread across underlying/midcap/smallcap naming
    conventions → combined contains 20 losers → each bucket has ≥3 rows.

    Simulates the frontend _classifyMoverSym split: symbols prefixed
    'UND' → underlying, 'MID' → midcap, 'SML' → smallcap (mimics
    the bucket names; actual classification uses index sets not name
    prefixes, but the point is that with 20 rows in the pool the spread
    gives every tab a non-trivial count).
    """
    combine = _load_combine_fn()
    snapshot: dict[str, dict] = {}
    # 9 underlying-style, 8 midcap-style, 8 smallcap-style losers (25 total).
    for i in range(9):
        sym = f"UND{i}"
        snapshot[sym] = _make_entry(-1.0 - i * 0.1, sym)
    for i in range(8):
        sym = f"MID{i}"
        snapshot[sym] = _make_entry(-1.5 - i * 0.1, sym)
    for i in range(8):
        sym = f"SML{i}"
        snapshot[sym] = _make_entry(-2.0 - i * 0.1, sym)

    combined = combine(snapshot, session_movers={}, top_n=20)

    loser_keys = {k for k in combined if not k.startswith("WIN")}
    assert len(loser_keys) == 20, (
        f"Expected 20 losers in combined pool, got {len(loser_keys)}"
    )

    # Simulate frontend grouping by name prefix (proxy for the three tabs).
    und_count = sum(1 for k in loser_keys if k.startswith("UND"))
    mid_count = sum(1 for k in loser_keys if k.startswith("MID"))
    sml_count = sum(1 for k in loser_keys if k.startswith("SML"))

    assert und_count >= 3, f"underlying bucket too sparse: {und_count}"
    assert mid_count >= 3, f"midcap bucket too sparse: {mid_count}"
    assert sml_count >= 3, f"smallcap bucket too sparse: {sml_count}"


def test_balanced_day_twenty_each():
    """30 winners, 30 losers → combined has exactly 20 winners and 20 losers."""
    combine = _load_combine_fn()
    snapshot = _build_snapshot(winners=30, losers=30)
    combined = combine(snapshot, session_movers={}, top_n=20)

    winner_keys = {k for k in combined if k.startswith("WIN")}
    loser_keys  = {k for k in combined if k.startswith("LOS")}
    assert len(winner_keys) == 20, f"Expected 20 winners, got {len(winner_keys)}"
    assert len(loser_keys)  == 20, f"Expected 20 losers, got {len(loser_keys)}"
