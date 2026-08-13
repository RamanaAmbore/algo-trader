"""Smoke tests: positions route and helpers import cleanly after stale-import removal."""

import importlib


def test_positions_imports_cleanly():
    """positions.py must import without NameError or ImportError.

    Covers P3-B: build_snapshot_position_row, extract_snapshot_extras, and
    extract_snapshot_multiplier were removed from the import block because
    they had zero references in the file body.
    """
    mod = importlib.import_module("backend.api.routes.positions")
    # Verify the names that SHOULD be present after refactor
    assert hasattr(mod, "logger")


def test_positions_helpers_imports_cleanly():
    """positions_helpers.py must import without error after docstring edit.

    Covers P3-A: prev_settlement_pnl docstring no longer references
    frontend Branch A / Branch B internal names.
    """
    mod = importlib.import_module("backend.api.routes.positions_helpers")
    assert hasattr(mod, "build_row_from_snapshot_raw")


def test_stale_names_not_re_exported():
    """The three removed names must NOT be importable from positions.py.

    If any of them accidentally re-appear in the import block, this test
    will catch it.
    """
    import backend.api.routes.positions as pos_mod
    for name in ("build_snapshot_position_row", "extract_snapshot_extras",
                 "extract_snapshot_multiplier"):
        assert not hasattr(pos_mod, name), (
            f"{name!r} should have been removed from positions.py imports"
        )
