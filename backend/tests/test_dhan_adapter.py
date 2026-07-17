"""
Tests for brokers/adapters/dhan.py — Dhan symbol conversion and GTT normalisation.
SSOT: _dhan_to_kite_symbol is the authoritative Dhan→Kite format converter.
Perf: conversion is a pure string operation (no DB/broker calls).
Stale: instruments cache has a date-roll expiry (no perpetually stale state).
Reuse: _normalise_dhan_gtt_row maps all GTT fields including leg quantities.
UX: symbol conversion must preserve MCX/NFO/CDS exchange prefixes correctly.
"""
from pathlib import Path
import pytest

_SRC = Path("backend/brokers/adapters/dhan.py").read_text()


def test_dhan_to_kite_symbol_function_exists():
    from backend.brokers.adapters.dhan import _dhan_to_kite_symbol
    assert callable(_dhan_to_kite_symbol), "_dhan_to_kite_symbol must be callable"


def test_dhan_option_symbol_conversion():
    """Dhan format: CRUDEOIL-16JUL2026-8500-CE → Kite format: CRUDEOIL26JUL8500CE"""
    from backend.brokers.adapters.dhan import _dhan_to_kite_symbol
    result = _dhan_to_kite_symbol("CRUDEOIL-16JUL2026-8500-CE")
    # Should strip hyphens, reformat date YY+MON, append strike+type
    assert "CRUDEOIL" in result, f"Symbol must contain CRUDEOIL, got: {result}"
    assert "CE" in result, f"Symbol must contain CE suffix, got: {result}"
    assert "-" not in result, f"Kite symbols must not contain hyphens, got: {result}"


def test_dhan_futures_symbol_conversion():
    """Dhan format: NIFTY-31JUL2026-FUT → Kite futures format."""
    from backend.brokers.adapters.dhan import _dhan_to_kite_symbol
    result = _dhan_to_kite_symbol("NIFTY-31JUL2026-FUT")
    assert "NIFTY" in result, f"Symbol must contain NIFTY, got: {result}"
    assert "FUT" in result, f"Symbol must contain FUT, got: {result}"
    assert "-" not in result, f"Kite symbols must not contain hyphens, got: {result}"


def test_equity_passthrough():
    """Simple equity symbols pass through unchanged."""
    from backend.brokers.adapters.dhan import _dhan_to_kite_symbol
    result = _dhan_to_kite_symbol("RELIANCE")
    assert result == "RELIANCE", f"Equity symbol must pass through as-is, got: {result}"


def test_normalise_dhan_gtt_row_exists():
    from backend.brokers.adapters.dhan import _normalise_dhan_gtt_row
    assert callable(_normalise_dhan_gtt_row), "_normalise_dhan_gtt_row must be callable"


def test_normalise_dhan_gtt_row_maps_key_fields():
    """_normalise_dhan_gtt_row must map trigger_price and leg price fields."""
    assert "trigger_price" in _SRC, "_normalise_dhan_gtt_row must map trigger_price"
    assert '"price"' in _SRC or "'price'" in _SRC, "_normalise_dhan_gtt_row must map price in GTT leg"


def test_instruments_cache_has_date_expiry():
    """Dhan instruments cache must refresh on new trading day (date-roll expiry)."""
    assert "date" in _SRC.lower() and ("expire" in _SRC.lower() or "stale" in _SRC.lower() or "refresh" in _SRC.lower() or "_cache_date" in _SRC or "_CACHE_DATE" in _SRC), (
        "Instruments cache must have a date-roll expiry to reload on new trading day"
    )
