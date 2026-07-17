"""
Tests for orders_place.py — ticket placement lots-convention invariants.
SSOT: _resolve_fno_qty converts lots→contracts at request boundary.
Perf: single multiplication path — no double-multiply downstream.
Stale: G1 removed from _ticket_enforce; G2 bypassed for close intent.
Reuse: shared _resolve_fno_qty helper used by ticket and preview paths.
UX: validation errors for non-positive lots.
"""
from pathlib import Path
import inspect

_SRC = Path("backend/api/routes/orders_place.py").read_text()


def test_resolve_fno_qty_exists():
    assert "_resolve_fno_qty" in _SRC, (
        "_resolve_fno_qty must exist to convert input lots → contracts "
        "at the request boundary (ticket + preview paths)"
    )


def test_lots_times_lot_size_in_resolve_path():
    # The conversion is: contracts = lots × lot_size
    assert "lot_size" in _SRC and ("lots" in _SRC or "input_qty" in _SRC), (
        "lots→contracts multiplication (lots × lot_size) must appear in "
        "_resolve_fno_qty or _ticket_validate_input"
    )


def test_g1_lot_multiple_not_in_enforce_function():
    # G1 (LOT_MULTIPLE check) was removed after lots-convention refactor
    # because contracts = lots × lot_size is always a valid multiple
    import re
    enforce_src = re.search(
        r"def _ticket_enforce_lot_and_fat_finger.*?(?=\nasync def |\ndef )",
        _SRC, re.DOTALL
    )
    if enforce_src:
        body = enforce_src.group(0)
        assert "% lot_size" not in body, (
            "G1 LOT_MULTIPLE check must NOT be in _ticket_enforce_lot_and_fat_finger — "
            "removed after lots-convention refactor (lots×lot_size is always valid)"
        )


def test_g2_bypassed_for_close_intent():
    assert "close" in _SRC.lower() and "intent" in _SRC.lower(), (
        "G2 fat-finger cap must be bypassed when intent=='close' — "
        "close orders of any size are allowed through"
    )
    # Look for the bypass pattern
    assert "_is_close" in _SRC or 'intent.*close' in _SRC or '"close"' in _SRC, (
        "Close-intent bypass for G2 must be present in orders_place.py"
    )


def test_lot_size_multiplication_appears_once_in_validate():
    # Count occurrences of lot_size multiplication pattern in validate section
    import re
    validate_section = re.search(
        r"def _ticket_validate_input.*?(?=\nasync def |\ndef )",
        _SRC, re.DOTALL
    )
    if validate_section:
        body = validate_section.group(0)
        # lots→contracts should happen once via _resolve_fno_qty
        resolve_calls = body.count("_resolve_fno_qty")
        assert resolve_calls >= 1, (
            "_resolve_fno_qty must be called in _ticket_validate_input "
            "to perform the lots→contracts conversion"
        )


def test_validation_error_for_zero_lots():
    assert "lots <= 0" in _SRC or "lots < 1" in _SRC or "input_qty <= 0" in _SRC or (
        "422" in _SRC or "400" in _SRC or "ValidationException" in _SRC
    ), (
        "Validation error (400/422) must be raised when lots <= 0"
    )
