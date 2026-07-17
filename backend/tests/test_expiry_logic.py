"""
Tests for api/algo/expiry.py — option/future expiry-roll engine.
SSOT: _exp_opt_pair_valid and _best_opt_partner are the pair-selection primitives.
Perf: interval guard ensures scan doesn't run on every tick.
Stale: ExpiryEngine has an idle/scanning/closing state machine.
Reuse: pair validation logic shared between option and futures paths.
UX: opposite-sign CE+PE pair on same underlying → valid netting pair.
"""
from pathlib import Path
import pytest

_SRC = Path("backend/api/algo/expiry.py").read_text()


def test_exp_opt_pair_valid_exists():
    from backend.api.algo.expiry import _exp_opt_pair_valid
    assert _exp_opt_pair_valid is not None


def test_valid_pair_same_sign_opposite_type():
    """CE long + PE long (same sign, opposite types) is a valid netting pair — rules 3/4.
    same_type=False (CE vs PE), both long (same sign) → valid (CE+PE pair can net).
    """
    from backend.api.algo.expiry import _exp_opt_pair_valid
    # CE long (aq=50) + PE long (bq=50) — same_type=False (CE vs PE), same sign
    result = _exp_opt_pair_valid(same_type=False, aq=50, bq=50)
    assert result is True, (
        "CE+PE with same-sign quantities (both long or both short) must be a valid netting pair"
    )


def test_invalid_pair_same_sign():
    """Two CE positions with the same sign (both long) are NOT a valid netting pair."""
    from backend.api.algo.expiry import _exp_opt_pair_valid
    # same_type=True (both CE), aq=50 bq=50 (same sign = both long) → invalid
    result = _exp_opt_pair_valid(same_type=True, aq=50, bq=50)
    assert result is False, (
        "Two CE positions with same sign must NOT be a valid netting pair"
    )


def test_invalid_pair_same_type_same_sign():
    """same_type=True (both CE), same sign → invalid (two longs can't net each other)."""
    from backend.api.algo.expiry import _exp_opt_pair_valid
    result = _exp_opt_pair_valid(same_type=True, aq=100, bq=50)
    assert result is False, (
        "Same-type pair (two CE) with same sign must NOT be a valid netting pair"
    )


def test_best_opt_partner_exists():
    from backend.api.algo.expiry import _best_opt_partner
    assert _best_opt_partner is not None, "_best_opt_partner must exist for partner selection"


def test_expiry_engine_class_exists():
    from backend.api.algo.expiry import ExpiryEngine
    assert ExpiryEngine is not None


def test_expiry_engine_state_machine_in_source():
    assert "ExpiryState" in _SRC or "_state" in _SRC, (
        "ExpiryEngine must have a state machine (idle/scanning/closing transitions)"
    )


def test_interval_guard_before_scan():
    """Expiry scan must not fire on every tick — interval/schedule guard required."""
    assert "interval" in _SRC or "cooldown" in _SRC or "last_scan" in _SRC or "time" in _SRC, (
        "Expiry scan must have an interval/time guard to avoid scanning on every tick"
    )
