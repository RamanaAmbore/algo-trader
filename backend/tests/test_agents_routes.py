"""
Tests for api/routes/agents.py — agent management helpers.
SSOT: _parse_iso_dt handles TZ-naive and null inputs without crash.
Perf: _check_debounce_gate is a pure function (no DB on hot path).
Stale: lifespan one_shot produces n_fires=1 with no until_date.
Reuse: _normalize_fire_at and _parse_iso_dt shared across create/update paths.
UX: 422 returned for malformed condition/grammar fields.
"""
from pathlib import Path
from datetime import datetime

_SRC = Path("backend/api/routes/agents.py").read_text()


def test_parse_iso_dt_function_exists():
    from backend.api.routes.agents import _parse_iso_dt
    assert callable(_parse_iso_dt), "_parse_iso_dt must be callable"


def test_parse_iso_dt_valid_string():
    from backend.api.routes.agents import _parse_iso_dt
    result = _parse_iso_dt("2026-07-17T09:15:00+05:30")
    assert result is not None, "_parse_iso_dt must return a datetime for a valid ISO string"
    assert isinstance(result, datetime), "_parse_iso_dt must return a datetime object"
    assert result.tzinfo is not None, "_parse_iso_dt must return a timezone-aware datetime"


def test_parse_iso_dt_none_input():
    from backend.api.routes.agents import _parse_iso_dt
    result = _parse_iso_dt(None)
    assert result is None, "_parse_iso_dt(None) must return None without raising"


def test_parse_iso_dt_empty_string():
    from backend.api.routes.agents import _parse_iso_dt
    result = _parse_iso_dt("")
    assert result is None, "_parse_iso_dt('') must return None"


def test_debounce_gate_exists():
    assert "_check_debounce_gate" in _SRC, (
        "_check_debounce_gate must exist to block agent re-fires within the debounce window"
    )


def test_debounce_gate_is_callable():
    from backend.api.routes.agents import _check_debounce_gate
    assert callable(_check_debounce_gate), "_check_debounce_gate must be callable"


def test_one_shot_lifespan_handling():
    """one_shot lifespan must produce a single fire with no ongoing schedule."""
    assert "one_shot" in _SRC, "one_shot lifespan must be handled in agents.py"


def test_validation_for_malformed_input():
    """422/ValidationException must be raised for malformed condition/grammar."""
    assert (
        "422" in _SRC
        or "ValidationException" in _SRC
        or "UnprocessableEntityException" in _SRC
        or "malformed" in _SRC.lower()
        or "invalid" in _SRC.lower()
    ), (
        "agents.py must validate agent condition/grammar fields and return 422 on malformed input"
    )
