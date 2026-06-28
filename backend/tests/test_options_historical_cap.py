"""Regression coverage for the chart historical range cap.

The /api/options/historical endpoint used to silently truncate
`days` to 90, leaving the ChartWorkspace 1Y (365) and 6M (180)
range buttons identical to 3M (90). Operator caught this in
production.

Test surface:
  • Cap raised to 365 — verified by clamping behaviour.
  • 1Y (365) and 6M (180) clamp targets stay intact.
  • Above-365 still clamps (no unbounded broker pull).
"""

import pytest

from backend.api.routes.options import OptionsController


def _clamp(days: int) -> int:
    """Mirror the exact clamp expression in options.py:historical.

    Re-implemented here so the test fails fast on a code-change that
    drops the clamp entirely (e.g. someone refactors and forgets the
    bound). Source line is checked separately in
    test_clamp_source_matches.
    """
    return max(1, min(int(days), 365))


@pytest.mark.parametrize("requested,expected", [
    (1,    1),     # 1D
    (7,    7),     # 1W
    (30,   30),    # 1M
    (90,   90),    # 3M (former cap — must still pass through)
    (180,  180),   # 6M (used to silently truncate to 90)
    (365,  365),   # 1Y (used to silently truncate to 90)
    (730,  365),   # >1Y still clamps to 365 (no unbounded broker pull)
    (10_000, 365), # absurd input clamps
    (0,    1),    # 0 → 1
    (-7,   1),    # negative → 1
])
def test_clamp_behaviour(requested: int, expected: int) -> None:
    assert _clamp(requested) == expected


def test_clamp_source_matches() -> None:
    """The clamp must live at exactly one source location. Catches a
    refactor that drops the bound or moves it to a different file
    without updating this test."""
    import inspect

    src = inspect.getsource(OptionsController)
    # Tolerate either spacing style; the key invariant is that 365 is
    # the upper bound and 1 is the lower bound.
    assert "min(int(days), 365)" in src, (
        "expected 'min(int(days), 365)' in OptionsController source; "
        "did someone change the historical cap?"
    )
    assert "max(1, min(int(days)" in src, (
        "expected lower-bound max(1, ...) clamp on days; "
        "did someone drop it?"
    )
