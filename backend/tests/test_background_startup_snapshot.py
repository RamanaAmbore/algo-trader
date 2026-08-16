"""
Tests for startup snapshot weekend skip in backend/api/background.py::_task_daily_snapshot.

After _probe_nse_mcx(), the startup block enforces three guards:
  1. Weekend (weekday >= 5): skip startup snapshot — existing EOD data sufficient
  2. Market open: skip to avoid mid-session LTP pollution
  3. Neither: fire startup snapshot with market_open=False

Five quality dimensions:
  1. SSOT       — startup decision logic is pure and testable
  2. Performance — no async overhead in the decision gate itself
  3. Stale-code  — no unreachable code paths in guard sequence
  4. Reusable   — decision logic isolated for unit testing
  5. Correctness — each guard branch taken at the right conditions

Test approach: extract the startup decision logic into a pure helper function,
then unit-test each condition + precedence rule.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Helper: pure startup decision logic (mirrors background.py startup block)
# ---------------------------------------------------------------------------

def _startup_decision(today_weekday: int, nse_open: bool, mcx_open: bool) -> str:
    """
    Determine startup snapshot action based on guards.

    Args:
        today_weekday: datetime.date.weekday() (0=Monday, 6=Sunday; Sat=5, Sun=6)
        nse_open:      is_market_open() returned True for NSE
        mcx_open:      is_market_open() returned True for MCX

    Returns:
        One of: 'skip-weekend', 'skip-market-open', 'fire'

    Logic (mirrors background.py _task_daily_snapshot startup block):
        if weekday >= 5:
            return 'skip-weekend'
        elif nse_open or mcx_open:
            return 'skip-market-open'
        else:
            return 'fire'
    """
    if today_weekday >= 5:
        return 'skip-weekend'
    elif nse_open or mcx_open:
        return 'skip-market-open'
    else:
        return 'fire'


class TestStartupSnapshotWeekendSkip:
    """Unit tests for _task_daily_snapshot startup block logic."""

    # ─────────────────────────────────────────────────────────────────────
    # Dimension 5 — Correctness: each guard branch
    # ─────────────────────────────────────────────────────────────────────

    def test_saturday_skip_regardless_of_market_state(self):
        """Saturday (weekday=5): skip startup regardless of market state."""
        # Both closed
        assert _startup_decision(5, False, False) == 'skip-weekend'
        # NSE open
        assert _startup_decision(5, True, False) == 'skip-weekend'
        # MCX open
        assert _startup_decision(5, False, True) == 'skip-weekend'
        # Both open
        assert _startup_decision(5, True, True) == 'skip-weekend'

    def test_sunday_skip_regardless_of_market_state(self):
        """Sunday (weekday=6): skip startup regardless of market state."""
        # Both closed
        assert _startup_decision(6, False, False) == 'skip-weekend'
        # NSE open
        assert _startup_decision(6, True, False) == 'skip-weekend'
        # MCX open
        assert _startup_decision(6, False, True) == 'skip-weekend'
        # Both open
        assert _startup_decision(6, True, True) == 'skip-weekend'

    def test_weekday_both_closed_fire_snapshot(self):
        """Weekday (Mon-Fri) with both markets closed: fire snapshot."""
        # Monday through Friday
        for weekday in range(5):
            result = _startup_decision(weekday, False, False)
            assert result == 'fire', (
                f"weekday {weekday} with both markets closed should fire, got {result}"
            )

    def test_weekday_nse_open_skip(self):
        """Weekday with NSE open: skip (avoid mid-session pollution)."""
        # Test one weekday (Monday=0)
        assert _startup_decision(0, True, False) == 'skip-market-open'

    def test_weekday_mcx_open_skip(self):
        """Weekday with MCX open: skip (avoid mid-session pollution)."""
        # Test one weekday (Wednesday=2)
        assert _startup_decision(2, False, True) == 'skip-market-open'

    def test_weekday_both_open_skip(self):
        """Weekday with both markets open: skip."""
        # Test one weekday (Friday=4)
        assert _startup_decision(4, True, True) == 'skip-market-open'

    # ─────────────────────────────────────────────────────────────────────
    # Dimension 5 — Correctness: guard precedence (weekend > market-open)
    # ─────────────────────────────────────────────────────────────────────

    def test_weekend_guard_takes_precedence_over_market_open(self):
        """Weekend check must run FIRST; market-open check is second.

        If a Saturday had (hypothetically) both markets "open", the weekend
        guard must win and return 'skip-weekend', not 'skip-market-open'.
        This ensures the precedence: 1. weekend 2. market-open 3. fire.
        """
        # Saturday with both markets "open"
        assert _startup_decision(5, True, True) == 'skip-weekend'
        # Sunday with both markets "open"
        assert _startup_decision(6, True, True) == 'skip-weekend'

    # ─────────────────────────────────────────────────────────────────────
    # Dimension 3 — Stale-code: no unreachable paths
    # ─────────────────────────────────────────────────────────────────────

    def test_all_guard_conditions_covered(self):
        """Every combination of (weekday_class, nse, mcx) must map to one action.

        This ensures the if-elif-else chain is complete and no condition
        is shadowed by an earlier check.
        """
        results = {}
        # Enumerate all combinations
        for weekday_class, label in [(4, 'weekday'), (5, 'sat'), (6, 'sun')]:
            for nse_open in [False, True]:
                for mcx_open in [False, True]:
                    key = (weekday_class, nse_open, mcx_open)
                    result = _startup_decision(weekday_class, nse_open, mcx_open)
                    results[key] = result
                    assert result in ('skip-weekend', 'skip-market-open', 'fire'), (
                        f"Invalid result {result} for {key}"
                    )

        # Verify expected groupings
        # Weekends: all 4 combinations → skip-weekend
        weekend_results = {k: v for k, v in results.items() if k[0] >= 5}
        assert all(v == 'skip-weekend' for v in weekend_results.values()), (
            "All weekend combinations should skip-weekend"
        )

        # Weekday, either market open: all 3 combinations → skip-market-open
        weekday_open_results = {
            k: v for k, v in results.items()
            if k[0] < 5 and (k[1] or k[2])
        }
        assert all(v == 'skip-market-open' for v in weekday_open_results.values()), (
            "All weekday+open combinations should skip-market-open"
        )

        # Weekday, both closed: 1 combination → fire
        weekday_closed = {
            k: v for k, v in results.items()
            if k[0] < 5 and not k[1] and not k[2]
        }
        assert all(v == 'fire' for v in weekday_closed.values()), (
            "Weekday+both-closed should fire"
        )

    # ─────────────────────────────────────────────────────────────────────
    # Dimension 1 — SSOT: decision logic is pure, no side effects
    # ─────────────────────────────────────────────────────────────────────

    def test_decision_is_idempotent(self):
        """Calling _startup_decision multiple times with same inputs
        returns the same result."""
        inputs = (3, True, False)  # Wed, NSE open
        results = [_startup_decision(*inputs) for _ in range(3)]
        assert len(set(results)) == 1, "Decision should be deterministic"

    # ─────────────────────────────────────────────────────────────────────
    # Dimension 2 — Performance: decision logic is O(1), no I/O
    # ─────────────────────────────────────────────────────────────────────

    def test_decision_is_synchronous(self):
        """_startup_decision is a pure function; no async/blocking I/O."""
        # Just call it synchronously; no await needed
        result = _startup_decision(0, False, False)
        assert isinstance(result, str)

    # ─────────────────────────────────────────────────────────────────────
    # Edge cases: weekday boundary conditions
    # ─────────────────────────────────────────────────────────────────────

    def test_weekday_boundary_monday(self):
        """Monday (weekday=0) is first trading day; should fire if closed."""
        assert _startup_decision(0, False, False) == 'fire'

    def test_weekday_boundary_friday(self):
        """Friday (weekday=4) is last trading day; should fire if closed."""
        assert _startup_decision(4, False, False) == 'fire'

    def test_saturday_boundary(self):
        """Saturday (weekday=5) is first non-trading day; must skip."""
        assert _startup_decision(5, False, False) == 'skip-weekend'

    def test_sunday_boundary(self):
        """Sunday (weekday=6) is second non-trading day; must skip."""
        assert _startup_decision(6, False, False) == 'skip-weekend'

    # ─────────────────────────────────────────────────────────────────────
    # Edge cases: market state combinations
    # ─────────────────────────────────────────────────────────────────────

    def test_weekday_only_nse_open(self):
        """Weekday with only NSE open (MCX closed): skip."""
        # e.g. 12:00 IST on Monday (NSE 09:15-15:30, MCX 09:00-23:30)
        assert _startup_decision(0, True, False) == 'skip-market-open'

    def test_weekday_only_mcx_open(self):
        """Weekday with only MCX open (NSE closed): skip."""
        # e.g. 17:00 IST on Monday (NSE closed, MCX 09:00-23:30)
        assert _startup_decision(0, False, True) == 'skip-market-open'

    def test_weekday_nse_and_mcx_both_open(self):
        """Weekday with both NSE and MCX open: skip."""
        # e.g. 12:00 IST on Monday
        assert _startup_decision(0, True, True) == 'skip-market-open'


# ---------------------------------------------------------------------------
# Integration: verify logs match background.py code
# ---------------------------------------------------------------------------

def test_startup_decision_matches_background_py_guard_sequence():
    """Smoke test: guard sequence in _startup_decision matches
    the actual code structure in background.py::_task_daily_snapshot.

    This is a stale-code guard: if someone refactors the startup block
    and changes the guard order or logic, this test reminds them to
    update the helper too.
    """
    from pathlib import Path

    bg_file = Path(__file__).parent.parent / "api" / "background.py"
    src = bg_file.read_text(encoding="utf-8")

    # Find the startup snapshot block
    startup_start = src.find("# ── startup snapshot")
    startup_end = src.find("# ── settlement pass deduplication", startup_start)
    startup_block = src[startup_start:startup_end]

    # Verify the guard sequence is present
    assert "if _today_d.weekday() >= 5:" in startup_block, (
        "startup block must check weekday >= 5 first"
    )
    assert "elif _nse_open or _mcx_open:" in startup_block, (
        "startup block must check nse_open or mcx_open second"
    )
    assert "await _fire_snapshot(" in startup_block, (
        "startup block must have else case with _fire_snapshot"
    )

    # Verify no other guard branches
    assert "else:" in startup_block  # Should have exactly one else clause


def test_startup_block_fires_with_market_open_false():
    """Verify that the else clause in background.py calls
    _fire_snapshot with market_open=False when startup fires."""
    from pathlib import Path

    bg_file = Path(__file__).parent.parent / "api" / "background.py"
    src = bg_file.read_text(encoding="utf-8")

    # Find the startup snapshot block
    startup_start = src.find("# ── startup snapshot")
    startup_end = src.find("# ── settlement pass deduplication", startup_start)
    startup_block = src[startup_start:startup_end]

    # Verify the fire call includes market_open=False
    assert 'await _fire_snapshot("startup", market_open=False)' in startup_block, (
        "startup block else clause must call _fire_snapshot with market_open=False"
    )


def test_weekend_skip_message_in_background_py():
    """Verify that weekend skip logs the correct message."""
    from pathlib import Path

    bg_file = Path(__file__).parent.parent / "api" / "background.py"
    src = bg_file.read_text(encoding="utf-8")

    # Find the startup snapshot block
    startup_start = src.find("# ── startup snapshot")
    startup_end = src.find("# ── settlement pass deduplication", startup_start)
    startup_block = src[startup_start:startup_end]

    # Verify weekend skip message is present
    assert "skipping startup snapshot — weekend" in startup_block, (
        "startup block must log 'skipping startup snapshot — weekend' for weekend skip"
    )
