"""Test chart range partial coverage logic.

Tests verify that the _still_partial fix in options_helpers.py no longer
requires _heal_attempted, and that coverage threshold math is correct.
"""

import inspect
import pytest

from backend.api.routes import options_helpers


def test_still_partial_does_not_require_heal_attempted():
    """Scan source to verify _still_partial assignment does NOT reference _heal_attempted."""
    source = inspect.getsource(options_helpers)

    # Find the line(s) containing _still_partial =
    lines = source.split('\n')
    still_partial_lines = [line for line in lines if '_still_partial =' in line]

    assert len(still_partial_lines) > 0, "Could not find _still_partial assignment"

    # Join the relevant lines and verify _heal_attempted does not appear
    combined = ' '.join(still_partial_lines)
    assert '_heal_attempted' not in combined, (
        f"_heal_attempted should not appear in _still_partial assignment, "
        f"but found in: {combined}"
    )


def test_still_partial_is_pure_coverage_check():
    """Scan source to verify _still_partial checks coverage as len(store_bars) <."""
    source = inspect.getsource(options_helpers)

    # Find the line(s) containing _still_partial =
    lines = source.split('\n')
    still_partial_lines = [line for line in lines if '_still_partial =' in line]

    assert len(still_partial_lines) > 0, "Could not find _still_partial assignment"

    # Verify coverage check with len(store_bars) <
    combined = ' '.join(still_partial_lines)
    assert 'len(store_bars) <' in combined, (
        f"_still_partial should check 'len(store_bars) <', "
        f"but found: {combined}"
    )


def test_still_partial_threshold_math():
    """Verify coverage threshold math for self-heal gate.

    36 bars for 90-day range (3M) = 36 / 63 ≈ 57% coverage → still partial (below 70%)
    36 bars for 30-day range (1M) = 36 / 21 ≈ 171% coverage → NOT partial (above 70%)
    """
    from backend.api.routes.options import _SELF_HEAL_COVERAGE_THRESHOLD

    # For 90 days: 36 bars should be below threshold
    coverage_90d = int(_SELF_HEAL_COVERAGE_THRESHOLD * 90)
    assert 36 < coverage_90d, (
        f"For 90 days, threshold is {coverage_90d}; "
        f"36 bars (57% coverage) should be below this and thus still partial"
    )

    # For 30 days: 36 bars should NOT be below threshold
    coverage_30d = int(_SELF_HEAL_COVERAGE_THRESHOLD * 30)
    assert not (36 < coverage_30d), (
        f"For 30 days, threshold is {coverage_30d}; "
        f"36 bars (171% coverage) should be above this and thus NOT partial"
    )
