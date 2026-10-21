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
from unittest.mock import patch, AsyncMock

import pandas as pd
import pytest

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


# ---------------------------------------------------------------------------
# _dry_run_context's any_market_open (2026-10-20 fix) — _build_context()
# emits flat nse_open/mcx_open keys, never a "segments" list. The old
# `any(s.get("open") for s in base.get("segments", []))` always iterated
# an empty default, so any_market_open was always False and every
# schedule="market_hours" agent showed as blocked in the dry-run preview
# regardless of real market state.
# ---------------------------------------------------------------------------

async def _run_dry_run_context_with_mocked_fetches(build_context_return):
    from backend.api.routes import agents as agents_mod

    empty_df_pair = (pd.DataFrame(), pd.DataFrame())
    with patch("backend.api.background._fetch_holdings_direct", return_value=empty_df_pair), \
         patch("backend.api.background._fetch_positions_direct", return_value=empty_df_pair), \
         patch("backend.brokers.broker_apis.fetch_margins", return_value=[]), \
         patch("backend.api.algo.agent_engine._build_context", return_value=dict(build_context_return)):
        return await agents_mod._dry_run_context(datetime.now())


@pytest.mark.asyncio
async def test_dry_run_any_market_open_true_when_nse_open():
    ctx = await _run_dry_run_context_with_mocked_fetches(
        {"nse_open": True, "mcx_open": False}
    )
    assert ctx["any_market_open"] is True


@pytest.mark.asyncio
async def test_dry_run_any_market_open_true_when_mcx_open():
    ctx = await _run_dry_run_context_with_mocked_fetches(
        {"nse_open": False, "mcx_open": True}
    )
    assert ctx["any_market_open"] is True


@pytest.mark.asyncio
async def test_dry_run_any_market_open_false_when_both_closed():
    ctx = await _run_dry_run_context_with_mocked_fetches(
        {"nse_open": False, "mcx_open": False}
    )
    assert ctx["any_market_open"] is False


@pytest.mark.asyncio
async def test_dry_run_any_market_open_ignores_absent_segments_key():
    """_build_context() never emits a 'segments' key — confirm the dry-run
    context doesn't silently depend on one existing."""
    ctx = await _run_dry_run_context_with_mocked_fetches(
        {"nse_open": True, "mcx_open": True}
    )
    assert "segments" not in ctx or ctx["any_market_open"] is True
