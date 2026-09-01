"""
Tests for structured diagnostic logging added to chain_quotes handler and
_chain_quotes_sym_lookup in backend/api/routes/options.py.

Coverage:
  1. Cold cache (both instruments_chain and instruments return None) ->
     handler returns empty expiries and emits a WARNING (not just debug).
  2. instruments_chain_expiries cold (None) -> fast-path miss taken,
     sym_lookup called, "exp_index cold" debug message emitted.
  3. instruments_chain_expiries warm but underlying absent -> fast-path miss,
     sym_lookup called, "und not in index" debug message emitted.
  4. Entry-level debug log always fires with expected fields (und, exp,
     inst_chain_warm, exp_index_size, und_in_index).
  5. Fast-path hit (underlying in warm index) -> "fast-path: returning N expiries"
     debug log emitted, sym_lookup NOT called.
  6. sym_lookup cache-miss path emits cache-miss entry log and timing completion log.

Patch target for cache peek: backend.api.cache.peek
(NOT backend.api.routes.options._cache_peek -- that name is only a local
binding inside the handler body and does not exist at module scope.)
"""

from __future__ import annotations

import logging
import pytest
from unittest.mock import patch, AsyncMock

from backend.api.routes.instruments import Instrument, InstrumentsResponse
from backend.api.routes.options import (
    ChainQuotesResponse,
    _chain_quotes_sym_lookup,
    _chain_sym_cache_clear,
    _chain_quotes_closed_cache_clear,
)


@pytest.fixture(autouse=True)
def _clear_caches():
    """Clear both chain-quote caches before and after each test."""
    _chain_sym_cache_clear()
    _chain_quotes_closed_cache_clear()
    yield
    _chain_sym_cache_clear()
    _chain_quotes_closed_cache_clear()


def _make_inst_resp() -> InstrumentsResponse:
    """Minimal NIFTY instruments fixture with two expiries, two strikes."""
    instruments = [
        Instrument(s="NIFTY27AUG24000CE", e="NFO", t="CE",
                   ls=25, ts=0.05, u="NIFTY", x="2027-08-14", k=24000.0),
        Instrument(s="NIFTY27AUG24000PE", e="NFO", t="PE",
                   ls=25, ts=0.05, u="NIFTY", x="2027-08-14", k=24000.0),
        Instrument(s="NIFTY27AUG21000CE", e="NFO", t="CE",
                   ls=25, ts=0.05, u="NIFTY", x="2027-08-21", k=24000.0),
        Instrument(s="NIFTY27AUG21000PE", e="NFO", t="PE",
                   ls=25, ts=0.05, u="NIFTY", x="2027-08-21", k=24000.0),
    ]
    return InstrumentsResponse(cycle_date="2027-08-11", count=len(instruments), items=instruments)


# ---------------------------------------------------------------------------
# Log capture helper
# ---------------------------------------------------------------------------

class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self, level: int | None = None) -> list[str]:
        return [
            r.getMessage()
            for r in self.records
            if level is None or r.levelno == level
        ]


def _capture_options_logger():
    """Context manager: capture all options-module log records."""
    cap = _LogCapture()
    log = logging.getLogger("backend.api.routes.options")
    old_level = log.level
    log.setLevel(logging.DEBUG)
    log.addHandler(cap)

    class _Ctx:
        def __enter__(self):
            return cap

        def __exit__(self, *_):
            log.removeHandler(cap)
            log.setLevel(old_level)

    return _Ctx()


# ---------------------------------------------------------------------------
# Direct handler invocation helper (avoids Litestar test client overhead)
# ---------------------------------------------------------------------------

async def _invoke_handler(
    peek_side_effect,
    underlying: str = "NIFTY",
    expiry: str = "",
    prices: bool = False,
    mock_sym_lookup: bool = True,
):
    """
    Invoke OptionsController.chain_quotes directly with a patched cache.peek.
    If mock_sym_lookup=True, _chain_quotes_sym_lookup is replaced with an
    AsyncMock returning ({}, []) so tests do not spin up threads.
    Returns (response, log_capture, sym_lookup_mock_or_None).
    """
    from backend.api.routes.options import OptionsController

    # Access the raw coroutine function via .fn — the @get decorator wraps it in
    # an HTTPRouteHandler whose __call__ does not accept handler keyword args directly.
    fn = OptionsController.chain_quotes.fn

    with _capture_options_logger() as cap:
        with patch("backend.api.cache.peek", side_effect=peek_side_effect):
            if mock_sym_lookup:
                mock = AsyncMock(return_value=({}, []))
                with patch("backend.api.routes.options._chain_quotes_sym_lookup", mock):
                    resp = await fn(None, underlying=underlying, expiry=expiry, prices=prices)
                return resp, cap, mock
            else:
                resp = await fn(None, underlying=underlying, expiry=expiry, prices=prices)
                return resp, cap, None


# ---------------------------------------------------------------------------
# Test 1: cold cache -> empty response + WARNING level log
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cold_cache_returns_empty_and_logs_warning():
    """Both instruments_chain and instruments None -> empty response + WARNING."""
    resp, cap, mock = await _invoke_handler(lambda key: None)

    assert isinstance(resp, ChainQuotesResponse)
    assert resp.expiries == []
    assert resp.rows == []
    assert resp.underlying == "NIFTY"

    # sym_lookup must NOT be called
    mock.assert_not_called()

    # At least one WARNING with expected text
    warn_msgs = cap.messages(logging.WARNING)
    assert any(
        "instruments cache cold" in m and "NIFTY" in m
        for m in warn_msgs
    ), f"Expected WARNING about cold cache; got WARNING msgs: {warn_msgs}"


# ---------------------------------------------------------------------------
# Test 2: instruments warm, expiry index cold -> fast-path miss + sym_lookup called
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_expiry_index_cold_fast_path_miss():
    """instruments warm but instruments_chain_expiries None -> miss + sym_lookup."""
    inst_resp = _make_inst_resp()

    def _peek(key: str):
        if key == "instruments_chain_expiries":
            return None
        return inst_resp  # instruments_chain and instruments both warm

    resp, cap, mock = await _invoke_handler(_peek, underlying="NIFTY", expiry="")

    # sym_lookup must be called (fast path missed)
    mock.assert_called_once()

    # "exp_index cold" debug message must appear
    debug_msgs = cap.messages(logging.DEBUG)
    assert any(
        "fast-path miss" in m and "exp_index cold" in m
        for m in debug_msgs
    ), f"Expected 'fast-path miss ... exp_index cold' debug; got: {debug_msgs}"


# ---------------------------------------------------------------------------
# Test 3: instruments warm, underlying absent from warm index -> miss + sym_lookup
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_und_not_in_expiry_index_fast_path_miss():
    """Expiry index warm but NIFTY absent -> miss branch + sym_lookup called."""
    inst_resp = _make_inst_resp()
    exp_index = {"BANKNIFTY": ["2027-09-01"]}  # NIFTY absent

    def _peek(key: str):
        if key == "instruments_chain_expiries":
            return exp_index
        return inst_resp

    resp, cap, mock = await _invoke_handler(_peek, underlying="NIFTY", expiry="")

    mock.assert_called_once()

    debug_msgs = cap.messages(logging.DEBUG)
    assert any(
        "fast-path miss" in m and "und not in index" in m
        for m in debug_msgs
    ), f"Expected 'fast-path miss ... und not in index' debug; got: {debug_msgs}"


# ---------------------------------------------------------------------------
# Test 4: entry debug log fires with required fields
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_entry_debug_log_contains_required_fields():
    """Handler emits entry debug with und, exp, inst_chain_warm, exp_index_size, und_in_index."""
    inst_resp = _make_inst_resp()

    def _peek(key: str):
        if key in ("instruments_chain", "instruments"):
            return inst_resp
        return None

    resp, cap, _mock = await _invoke_handler(
        _peek, underlying="NIFTY", expiry="2027-08-14"
    )

    debug_msgs = cap.messages(logging.DEBUG)
    entry_msgs = [m for m in debug_msgs if "und=NIFTY" in m]
    assert entry_msgs, f"Expected entry debug log with 'und=NIFTY'; got debug msgs: {debug_msgs}"

    msg = entry_msgs[0]
    assert "exp=" in msg
    assert "inst_chain_warm=" in msg
    assert "exp_index_size=" in msg
    assert "und_in_index=" in msg


# ---------------------------------------------------------------------------
# Test 5: fast-path hit -> "fast-path: returning N expiries" + sym_lookup NOT called
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fast_path_hit_logs_count_and_skips_sym_lookup():
    """When underlying is in warm index, fast path fires and logs expiry count."""
    inst_resp = _make_inst_resp()
    exp_index = {"NIFTY": ["2027-08-14", "2027-08-21"]}

    def _peek(key: str):
        if key == "instruments_chain_expiries":
            return exp_index
        return inst_resp

    resp, cap, mock = await _invoke_handler(_peek, underlying="NIFTY", expiry="")

    # Fast path: sym_lookup NOT called
    mock.assert_not_called()

    # Response carries the fast-path expiries
    assert set(resp.expiries) == {"2027-08-14", "2027-08-21"}

    # Debug log with "fast-path: returning"
    debug_msgs = cap.messages(logging.DEBUG)
    assert any(
        "fast-path: returning" in m and "expiries" in m
        for m in debug_msgs
    ), f"Expected 'fast-path: returning N expiries' debug; got: {debug_msgs}"


# ---------------------------------------------------------------------------
# Test 6: sym_lookup cache-miss path emits timing log
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sym_lookup_emits_cache_miss_and_timing_logs():
    """_chain_quotes_sym_lookup emits cache-miss entry + timing completion logs."""
    _chain_sym_cache_clear()
    inst_resp = _make_inst_resp()

    with _capture_options_logger() as cap:
        sym_by_strike, all_expiries = await _chain_quotes_sym_lookup(
            "NIFTY", "2027-08-14", inst_resp
        )

    debug_msgs = cap.messages(logging.DEBUG)

    # Cache-miss entry log
    assert any(
        "sym_lookup: cache miss" in m and "NIFTY" in m
        for m in debug_msgs
    ), f"Expected cache-miss entry log; got: {debug_msgs}"

    # Timing completion log
    assert any(
        "sym_lookup(NIFTY,2027-08-14)" in m and "took" in m and "strikes" in m
        for m in debug_msgs
    ), f"Expected timing completion log; got: {debug_msgs}"
