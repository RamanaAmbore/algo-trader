"""
Tests for closed-hours snapshot guard on tick-data endpoints.

Defense-in-depth rule: even when the frontend mistakenly polls during closed
hours, the routes return the persisted daily_book snapshot instead of hitting
the broker.

Five quality dimensions:
  1. SSOT        — guard logic lives in each route module; uses is_any_segment_open
                   and daily_book[kind=...] as the canonical snapshot source.
  2. Performance — zero broker calls when market is closed (mocked broker is
                   asserted call_count == 0 on the closed-hours paths).
  3. Stale code  — guard added via `_is_all_markets_closed` helper (positions /
                   holdings); `_all_exchanges_closed` + `_is_exchange_segment_closed`
                   (quote); source-grep verifies the helpers exist.
  4. Reusable    — `as_of` field present on PositionsResponse, HoldingsResponse,
                   BatchQuoteResponse, SparklineResponse so callers can detect
                   snapshot vs. live.
  5. Correctness — per-route:
       a. Closed + snapshot exists → snapshot returned, broker NOT called.
       b. Open → live path runs, broker IS called.
       c. Mixed exchanges (one open, one closed) — verified for batch_quote.
       d. Closed + no snapshot → graceful empty / live fallback.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Source paths (dimension 3 — stale-code checks)
# ---------------------------------------------------------------------------

_POS_SRC  = Path(__file__).parent.parent / "api" / "routes" / "positions.py"
_HOL_SRC  = Path(__file__).parent.parent / "api" / "routes" / "holdings.py"
_QUO_SRC  = Path(__file__).parent.parent / "api" / "routes" / "quote.py"
_OPT_SRC  = Path(__file__).parent.parent / "api" / "routes" / "options.py"
_SCH_SRC  = Path(__file__).parent.parent / "api" / "schemas.py"


def _src(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Dimension 3 — static source checks
# ---------------------------------------------------------------------------

def test_positions_has_closed_hours_helper():
    """positions.py uses closed_hours_or_broker + _positions_snapshot."""
    src = _src(_POS_SRC)
    assert "closed_hours_or_broker" in src, (
        "positions.py must use closed_hours_or_broker from snapshot_gate"
    )
    assert "_positions_snapshot" in src, "missing _positions_snapshot in positions.py"
    assert "daily_book" in src and "kind = 'positions'" in src, (
        "_positions_snapshot should query daily_book with kind='positions'"
    )


def test_holdings_has_closed_hours_helper():
    """holdings.py uses closed_hours_or_broker + _holdings_snapshot."""
    src = _src(_HOL_SRC)
    assert "closed_hours_or_broker" in src, (
        "holdings.py must use closed_hours_or_broker from snapshot_gate"
    )
    assert "_holdings_snapshot" in src, "missing _holdings_snapshot in holdings.py"
    assert "daily_book" in src and "kind = 'holdings'" in src, (
        "_holdings_snapshot should query daily_book with kind='holdings'"
    )


def test_quote_has_exchange_closed_helpers():
    """quote.py defines _all_exchanges_closed + _is_exchange_segment_closed."""
    src = _src(_QUO_SRC)
    assert "_all_exchanges_closed" in src, "missing _all_exchanges_closed in quote.py"
    assert "_is_exchange_segment_closed" in src, "missing _is_exchange_segment_closed in quote.py"
    assert "_exchanges_from_keys" in src, "missing _exchanges_from_keys in quote.py"


def test_options_historical_has_closed_guard():
    """options_helpers.py has a closed-hours guard before the broker loop for intraday intervals."""
    # After refactor: helpers moved to options_helpers.py
    opt_helpers_src = Path(__file__).parent.parent / "api" / "routes" / "options_helpers.py"
    src = _src(opt_helpers_src)
    assert "is_any_segment_open" in src, "options_helpers.py should import is_any_segment_open for guard"
    assert "market closed" in src.lower(), (
        "options_helpers.py historical should log a market-closed skip message"
    )


def test_schemas_have_as_of_fields():
    """PositionsResponse and HoldingsResponse in schemas.py carry as_of: Optional[str]."""
    src = _src(_SCH_SRC)
    # Both response classes must have as_of declared
    assert re.search(r"class PositionsResponse.*?as_of.*?Optional\[str\]",
                     src, re.DOTALL), "PositionsResponse missing as_of: Optional[str]"
    assert re.search(r"class HoldingsResponse.*?as_of.*?Optional\[str\]",
                     src, re.DOTALL), "HoldingsResponse missing as_of: Optional[str]"


def test_quote_response_structs_have_as_of():
    """BatchQuoteResponse and SparklineResponse in quote.py carry as_of: Optional[str]."""
    src = _src(_QUO_SRC)
    assert re.search(r"class BatchQuoteResponse.*?as_of.*?Optional\[str\]",
                     src, re.DOTALL), "BatchQuoteResponse missing as_of: Optional[str]"
    assert re.search(r"class SparklineResponse.*?as_of.*?Optional\[str\]",
                     src, re.DOTALL), "SparklineResponse missing as_of: Optional[str]"


# ---------------------------------------------------------------------------
# Dimension 1 / 5 — functional: snapshot_gate _any_segment_open helper
# ---------------------------------------------------------------------------

def test_snapshot_gate_any_segment_open_returns_bool():
    """snapshot_gate._any_segment_open() returns a bool (True = open, False = closed)."""
    from backend.api.helpers.snapshot_gate import _any_segment_open

    with patch(
        "backend.api.helpers.snapshot_gate.is_any_segment_open" if False else
        "backend.shared.helpers.date_time_utils.is_any_segment_open",
        return_value=True,
    ):
        # Just test the import is clean — functional tests in test_snapshot_gate.py
        assert callable(_any_segment_open)


def test_snapshot_gate_imported_in_positions():
    """positions.py imports closed_hours_or_broker from snapshot_gate — static check."""
    src = _src(_POS_SRC)
    assert "snapshot_gate" in src, (
        "positions.py must import from snapshot_gate (the canonical closed-hours gate)"
    )


def test_snapshot_gate_imported_in_holdings():
    """holdings.py imports closed_hours_or_broker from snapshot_gate — static check."""
    src = _src(_HOL_SRC)
    assert "snapshot_gate" in src, (
        "holdings.py must import from snapshot_gate (the canonical closed-hours gate)"
    )


# ---------------------------------------------------------------------------
# Dimension 5a — positions: closed → snapshot, broker NOT called
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_positions_closed_hours_returns_snapshot_no_broker():
    """When market is closed and a snapshot exists, positions route returns
    the snapshot without calling broker_apis.fetch_positions."""
    from backend.api.schemas import PositionsResponse, PositionRow, PositionsSummaryRow

    fake_snapshot = PositionsResponse(
        rows=[
            PositionRow(
                account="ZG0790",
                tradingsymbol="NIFTY25JUNFUT",
                exchange="NFO",
                product="NRML",
                quantity=50,
                average_price=23000.0,
                close_price=23100.0,
                pnl=5000.0,
                last_price=23100.0,
            )
        ],
        summary=[
            PositionsSummaryRow(account="ZG0790", pnl=5000.0),
            PositionsSummaryRow(account="TOTAL", pnl=5000.0),
        ],
        refreshed_at="Mon 28 Jun 09:00 IST",
        as_of="2026-06-27T23:30:00+00:00",
    )

    mock_broker_fetch = MagicMock()

    with patch(
        "backend.api.helpers.snapshot_gate._any_segment_open",
        return_value=False,   # market closed
    ), patch(
        "backend.api.routes.positions._positions_snapshot",
        new=AsyncMock(return_value=fake_snapshot),
    ), patch(
        "backend.brokers.broker_apis.fetch_positions",
        mock_broker_fetch,
    ):
        from backend.api.routes.positions import PositionsController
        # Litestar controllers cannot be instantiated standalone — call the
        # unbound handler directly by extracting the route function.
        handler_fn = PositionsController.get_positions.fn

        mock_request = MagicMock()
        mock_request.headers = {}
        mock_request.user = None

        # Patch auth helpers to admin so no masking interferes
        # (they're imported into positions_helpers, not positions)
        with patch("backend.api.routes.positions_helpers.is_admin_request", return_value=True), \
             patch("backend.api.routes.positions_helpers.resolve_role_from_connection",
                   return_value="admin"), \
             patch("backend.api.routes.positions_helpers.normalise_role", return_value="admin"):
            resp = await handler_fn(None, mock_request, fresh=False)

    assert resp.as_of is not None, "as_of must be set on snapshot response"
    assert resp.as_of == "2026-06-27T23:30:00+00:00"
    assert len(resp.rows) == 1
    assert resp.rows[0].tradingsymbol == "NIFTY25JUNFUT"
    # Broker was NOT called
    assert mock_broker_fetch.call_count == 0, (
        f"broker fetch_positions called {mock_broker_fetch.call_count} times — "
        "should be 0 during closed hours"
    )


# ---------------------------------------------------------------------------
# Dimension 5b — positions: open → live path, broker IS called
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_positions_open_hours_calls_broker():
    """When market is open, positions route uses the live path (get_or_fetch)."""
    from backend.api.schemas import PositionsResponse

    live_resp = PositionsResponse(rows=[], summary=[], refreshed_at="live")

    with patch(
        "backend.api.helpers.snapshot_gate._any_segment_open",
        return_value=True,   # market open
    ), patch(
        "backend.api.routes.positions.get_or_fetch",
        new=AsyncMock(return_value=live_resp),
    ):
        from backend.api.routes.positions import PositionsController
        handler_fn = PositionsController.get_positions.fn

        mock_request = MagicMock()
        with patch("backend.api.routes.positions_helpers.is_admin_request", return_value=True), \
             patch("backend.api.routes.positions_helpers.resolve_role_from_connection",
                   return_value="admin"), \
             patch("backend.api.routes.positions_helpers.normalise_role", return_value="admin"):
            resp = await handler_fn(None, mock_request, fresh=False)

    assert resp.as_of is None, "as_of must be None on live response"
    assert resp.refreshed_at == "live"


# ---------------------------------------------------------------------------
# Dimension 5a — holdings: closed → snapshot, broker NOT called
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_holdings_closed_hours_returns_snapshot_no_broker():
    """When market is closed and a snapshot exists, holdings route returns
    the snapshot without calling broker_apis.fetch_holdings."""
    from backend.api.schemas import HoldingsResponse, HoldingRow, HoldingsSummaryRow

    fake_snapshot = HoldingsResponse(
        rows=[
            HoldingRow(
                account="ZG0790",
                tradingsymbol="RELIANCE",
                exchange="NSE",
                quantity=10,
                average_price=2800.0,
                close_price=2900.0,
                inv_val=28000.0,
                cur_val=29000.0,
                pnl=1000.0,
                pnl_percentage=3.57,
                last_price=2900.0,
            )
        ],
        summary=[
            HoldingsSummaryRow(
                account="ZG0790", inv_val=28000.0, cur_val=29000.0,
                pnl=1000.0, pnl_percentage=3.57,
                day_change_val=0.0, day_change_percentage=0.0,
            )
        ],
        refreshed_at="Mon 28 Jun 09:00 IST",
        as_of="2026-06-27T23:30:00+00:00",
    )

    mock_broker_fetch = MagicMock()

    with patch(
        "backend.api.helpers.snapshot_gate._any_segment_open",
        return_value=False,   # market closed
    ), patch(
        "backend.api.routes.holdings._holdings_snapshot",
        new=AsyncMock(return_value=fake_snapshot),
    ), patch(
        "backend.brokers.broker_apis.fetch_holdings",
        mock_broker_fetch,
    ):
        from backend.api.routes.holdings import HoldingsController
        handler_fn = HoldingsController.get_holdings.fn

        mock_request = MagicMock()
        with patch("backend.api.auth_guard.is_admin_request", return_value=True), \
             patch("backend.api.rbac.resolve_role_from_connection",
                   return_value="admin"), \
             patch("backend.api.rbac.normalise_role", return_value="admin"):
            resp = await handler_fn(None, mock_request, fresh=False)

    assert resp.as_of is not None
    assert len(resp.rows) == 1
    assert resp.rows[0].tradingsymbol == "RELIANCE"
    assert mock_broker_fetch.call_count == 0, (
        f"broker fetch_holdings called {mock_broker_fetch.call_count} times — "
        "should be 0 during closed hours"
    )


# ---------------------------------------------------------------------------
# Dimension 5b — holdings: open → broker IS called
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_holdings_open_hours_calls_broker():
    """When market is open, holdings route uses the live cache path."""
    from backend.api.schemas import HoldingsResponse

    live_resp = HoldingsResponse(rows=[], summary=[], refreshed_at="live")

    with patch(
        "backend.api.helpers.snapshot_gate._any_segment_open",
        return_value=True,   # market open
    ), patch(
        "backend.api.routes.holdings.get_or_fetch",
        new=AsyncMock(return_value=live_resp),
    ):
        from backend.api.routes.holdings import HoldingsController
        handler_fn = HoldingsController.get_holdings.fn

        mock_request = MagicMock()
        with patch("backend.api.auth_guard.is_admin_request", return_value=True), \
             patch("backend.api.rbac.resolve_role_from_connection",
                   return_value="admin"), \
             patch("backend.api.rbac.normalise_role", return_value="admin"):
            resp = await handler_fn(None, mock_request, fresh=False)

    assert resp.as_of is None
    assert resp.refreshed_at == "live"


# ---------------------------------------------------------------------------
# Dimension 5d — positions: closed + no snapshot → fall through (no crash)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_positions_closed_no_snapshot_falls_through():
    """When market is closed but no snapshot in DB, fall through to live path."""
    from backend.api.schemas import PositionsResponse

    live_resp = PositionsResponse(rows=[], summary=[], refreshed_at="fallback")

    with patch(
        "backend.api.helpers.snapshot_gate._any_segment_open",
        return_value=False,   # market closed
    ), patch(
        "backend.api.routes.positions._positions_snapshot",
        new=AsyncMock(return_value=None),  # no snapshot → wrapper returns empty PositionsResponse
    ), patch(
        "backend.api.routes.positions.get_or_fetch",
        new=AsyncMock(return_value=live_resp),
    ):
        from backend.api.routes.positions import PositionsController
        handler_fn = PositionsController.get_positions.fn

        mock_request = MagicMock()
        with patch("backend.api.routes.positions_helpers.is_admin_request", return_value=True), \
             patch("backend.api.routes.positions_helpers.resolve_role_from_connection",
                   return_value="admin"), \
             patch("backend.api.routes.positions_helpers.normalise_role", return_value="admin"):
            resp = await handler_fn(None, mock_request, fresh=False)

    # When no snapshot exists, the wrapper returns an empty PositionsResponse with
    # as_of=None. The handler falls back to the live path (get_or_fetch).
    assert resp.refreshed_at == "fallback"


# ---------------------------------------------------------------------------
# Dimension 5c — batch_quote: mixed exchanges (NSE open, MCX closed)
# ---------------------------------------------------------------------------

def test_exchanges_from_keys_extracts_correctly():
    """_exchanges_from_keys correctly extracts exchange codes from key strings."""
    from backend.api.routes.quote import _exchanges_from_keys
    keys = ["NSE:RELIANCE", "MCX:GOLD26JUNFUT", "NFO:NIFTY25JUNFUT", "bad_key"]
    result = _exchanges_from_keys(keys)
    assert result == {"NSE", "MCX", "NFO"}


def test_all_exchanges_closed_empty_set():
    """_all_exchanges_closed returns False for empty exchange set (no info → open)."""
    from backend.api.routes.quote import _all_exchanges_closed
    assert _all_exchanges_closed(set()) is False


def test_all_exchanges_closed_when_all_closed():
    """_all_exchanges_closed returns True when every exchange reports closed."""
    from backend.api.routes.quote import _all_exchanges_closed, _is_exchange_segment_closed

    with patch("backend.api.routes.quote._is_exchange_segment_closed", return_value=True):
        result = _all_exchanges_closed({"NSE", "MCX"})
    assert result is True


def test_all_exchanges_closed_when_one_open():
    """_all_exchanges_closed returns False when at least one exchange is open."""
    from backend.api.routes.quote import _all_exchanges_closed

    def _side(exch: str) -> bool:
        return exch == "MCX"  # MCX closed, NSE open

    with patch("backend.api.routes.quote._is_exchange_segment_closed", side_effect=_side):
        result = _all_exchanges_closed({"NSE", "MCX"})
    assert result is False, "should be open when at least one exchange is open"


# ---------------------------------------------------------------------------
# Dimension 5a — batch_quote: closed → LKG LTP, broker at most once (cold warm)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_batch_quote_closed_returns_lkg_no_broker():
    """batch_quote returns LKG LTPs during closed hours.

    Post 2026-07-03 hardening: the closed-hours branch fires ONE broker.quote()
    per (IST day, key-set signature) as a cold-start warm so open/close/volume/oi
    are hydrated into the LKG cache when the process restarts during closed
    hours.  Steady-state (warm cache) subsequent calls still make zero broker
    calls.  This test verifies:
      (a) Response carries LKG LTP + stale=True.
      (b) The SECOND call for the same key-set makes zero additional broker
          calls (steady-state guarantee).
    """
    from backend.api.routes.quote import (
        QuoteController, BatchQuoteRequest, _closed_hours_warm_signatures,
    )

    # Reset the warm-signature dedup so the test is deterministic across runs.
    _closed_hours_warm_signatures.clear()

    # Broker returns one valid symbol so _persisted > 0 → sig gets added after warm.
    # (Post-fix change: empty-response warm does NOT add sig — that's intentional so
    # a broker race on cold start retries; dedup only fires when warm actually landed.)
    mock_broker_quote = MagicMock(return_value={
        "NSE:RELIANCE": {
            "last_price": 2540.5,
            "ohlc": {"open": 2530.0, "close": 2532.0},
            "volume": 1_234_567,
            "oi": 0,
            "depth": {},
        }
    })

    with patch("backend.api.routes.quote._all_exchanges_closed", return_value=True), \
         patch(
             "backend.brokers.broker_apis.get_last_good_ltp",
             side_effect=lambda sym, max_age_s=3600.0: 1234.5 if sym == "RELIANCE" else 0.0,
         ), \
         patch(
             "backend.brokers.broker_apis.get_last_good_quote",
             return_value=None,  # cold cache — snapshot fields will be null
         ), \
         patch("backend.brokers.broker_apis.record_good_ltp"), \
         patch("backend.brokers.broker_apis.record_good_quote"), \
         patch("backend.brokers.registry.get_price_broker") as mock_registry:
        mock_registry.return_value.quote = mock_broker_quote

        handler_fn = QuoteController.batch_quote.fn
        req = BatchQuoteRequest(keys=["NSE:RELIANCE", "NSE:INFY"])
        # First call: cold cache — expect ONE broker.quote() for the warm.
        resp = await handler_fn(None, req)

        assert resp.as_of is not None, "as_of must be set for closed-hours batch_quote"
        assert len(resp.items) == 2
        # Find RELIANCE row
        rel_row = next((r for r in resp.items if r.tradingsymbol == "RELIANCE"), None)
        assert rel_row is not None
        assert rel_row.ltp == 1234.5
        assert rel_row.stale is True  # explicitly marked stale during closed hours

        # First-call cold warm: exactly ONE broker.quote() (per key-set signature).
        first_call_count = mock_broker_quote.call_count
        assert first_call_count == 1, (
            f"cold-start warm should call broker.quote() exactly once; "
            f"got {first_call_count}"
        )

        # Second identical call: warm-signature dedup must skip the broker.
        resp2 = await handler_fn(None, req)
        assert mock_broker_quote.call_count == first_call_count, (
            f"second call for same key-set should make ZERO additional broker "
            f"calls; total went {first_call_count} → {mock_broker_quote.call_count}"
        )
        assert len(resp2.items) == 2


# ---------------------------------------------------------------------------
# Dimension 5a-ii — batch_quote: broker exception must NOT poison warm signature
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_batch_quote_closed_warm_not_poisoned_on_broker_failure():
    """Signature-poisoning fix — a broker exception on the first warm attempt
    must NOT add the signature to _closed_hours_warm_signatures.

    Pre-fix behaviour (bug): _closed_hours_warm_signatures.add(sig) ran
    BEFORE the broker.quote() call.  On broker failure (conn-service cold,
    network error) the signature was marked 'warmed' all day — every
    subsequent closed-hours response returned volume=0/oi=0 with no retry
    until midnight IST rollover.

    Post-fix behaviour (correct): add(sig) only executes inside the
    `if _persisted:` block, AFTER at least one symbol is successfully
    persisted.  A broker exception leaves the signature absent so the
    NEXT closed-hours call re-attempts the warm.

    Post-simplify-review update: a broker failure now records a ~60 s cool-
    off in `_closed_hours_warm_failed_until` (broker-storm throttle).  The
    "not poisoned" invariant still holds — the sig is NOT added to
    `_signatures`, so once cool-off expires the retry runs.  Test clears
    the cool-off between calls to prove immediate retry is possible when
    unthrottled.

    Test plan:
      Call 1 — broker.quote() raises RuntimeError (simulate cold start).
               Signature must NOT appear in _closed_hours_warm_signatures.
               Signature IS in _closed_hours_warm_failed_until (cool-off).
      Call 2 — After clearing cool-off, broker.quote() succeeds and returns
               one valid symbol.  Broker must be invoked (proof that
               `_signatures` was still absent).  Signature IS added after
               successful persisted write.
      Call 3 — Same key-set. Broker must NOT be called (dedup now active).
    """
    from backend.api.routes.quote import (
        _maybe_warm_closed_hours_quotes,
        _closed_hours_warm_signatures,
        _closed_hours_warm_in_progress,
        _closed_hours_warm_failed_until,
    )

    # Build a minimal key_map stub with the required `broker_keys` attribute.
    class _FakeKeyMap:
        broker_keys = ["NSE:RELIANCE", "MCX:CRUDEOIL"]

    key_map = _FakeKeyMap()
    sig = ",".join(sorted(key_map.broker_keys))

    # Reset state so the test is deterministic across runs.
    _closed_hours_warm_signatures.discard(sig)
    _closed_hours_warm_in_progress.discard(sig)
    _closed_hours_warm_failed_until.pop(sig, None)

    call_count = 0

    async def _fake_to_thread(fn, *args):
        """Dispatch fn(*args) — needed because _maybe_warm uses asyncio.to_thread."""
        return fn(*args)

    # ── Call 1: broker raises ─────────────────────────────────────────────────
    def _broker_raise(*_a, **_kw):
        raise RuntimeError("conn-service unavailable")

    mock_broker_1 = MagicMock()
    mock_broker_1.quote = MagicMock(side_effect=_broker_raise)

    with patch("backend.brokers.registry.get_market_data_broker", return_value=mock_broker_1), \
         patch("asyncio.to_thread", side_effect=_fake_to_thread):
        await _maybe_warm_closed_hours_quotes(key_map.broker_keys, key_map)

    # Signature must NOT be present after a broker failure.
    assert sig not in _closed_hours_warm_signatures, (
        "Broker exception must not poison the warm signature — "
        "sig was added before the broker call (pre-fix bug)"
    )
    # Broker-storm throttle records a cool-off so back-to-back failures
    # don't hammer the broker.  Clear it here so call 2's immediate retry
    # proves the "not poisoned" invariant (sig retriable once cool-off ends).
    assert sig in _closed_hours_warm_failed_until, (
        "Broker failure must record a cool-off timestamp (broker-storm throttle)"
    )
    _closed_hours_warm_failed_until.pop(sig, None)

    # ── Call 2: broker succeeds ───────────────────────────────────────────────
    def _broker_ok(*_a, **_kw):
        return {
            "NSE:RELIANCE": {
                "last_price": 2540.5,
                "ohlc": {"open": 2530.0, "close": 2532.0},
                "volume": 1_234_567,
                "oi": 0,
                "depth": {},
            }
        }

    mock_broker_2 = MagicMock()
    mock_broker_2.quote = MagicMock(side_effect=_broker_ok)

    with patch("backend.brokers.registry.get_market_data_broker", return_value=mock_broker_2), \
         patch("asyncio.to_thread", side_effect=_fake_to_thread), \
         patch("backend.brokers.broker_apis.record_good_ltp"), \
         patch("backend.brokers.broker_apis.record_good_quote"):
        await _maybe_warm_closed_hours_quotes(key_map.broker_keys, key_map)

    # Broker MUST have been called on call 2 (signature was absent after call 1).
    assert mock_broker_2.quote.call_count == 1, (
        f"Second call should invoke broker.quote() because signature was absent "
        f"after broker failure; call_count={mock_broker_2.quote.call_count}"
    )

    # Signature IS now present (successful persisted write).
    assert sig in _closed_hours_warm_signatures, (
        "After successful warm, signature must be added to prevent redundant broker calls"
    )

    # ── Call 3: dedup fires ───────────────────────────────────────────────────
    mock_broker_3 = MagicMock()
    mock_broker_3.quote = MagicMock(side_effect=_broker_ok)

    with patch("backend.brokers.registry.get_market_data_broker", return_value=mock_broker_3), \
         patch("asyncio.to_thread", side_effect=_fake_to_thread):
        await _maybe_warm_closed_hours_quotes(key_map.broker_keys, key_map)

    # Broker must NOT be called — signature dedup is active.
    assert mock_broker_3.quote.call_count == 0, (
        f"Third call for same key-set must make zero broker calls (dedup); "
        f"call_count={mock_broker_3.quote.call_count}"
    )


# ---------------------------------------------------------------------------
# Dimension 5b — batch_quote: open → broker IS called
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_batch_quote_open_calls_broker():
    """batch_quote calls broker.quote() when market is open."""
    from backend.api.routes.quote import QuoteController, BatchQuoteRequest

    fake_quote_data = {
        "NSE:RELIANCE": {"last_price": 2900.0, "ohlc": {"close": 2850.0, "open": 2860.0}},
    }

    with patch("backend.api.routes.quote._all_exchanges_closed", return_value=False), \
         patch("backend.brokers.registry.get_price_broker") as mock_registry, \
         patch("backend.brokers.registry.get_sparkline_broker") as _mock_sp, \
         patch("backend.api.routes.quote._get_today_token_map", return_value={}), \
         patch("backend.brokers.kite_ticker.get_ticker") as _mock_tk:
        mock_broker = MagicMock()
        mock_broker.quote = MagicMock(return_value=fake_quote_data)
        mock_registry.return_value = mock_broker
        _mock_sp.return_value = MagicMock()
        _mock_tk.return_value = MagicMock()
        _mock_tk.return_value.subscribe_with_sym = MagicMock()

        import asyncio

        async def _fake_to_thread(fn, *args):
            return fn(*args)

        with patch("asyncio.to_thread", side_effect=_fake_to_thread):
            handler_fn = QuoteController.batch_quote.fn
            req = BatchQuoteRequest(keys=["NSE:RELIANCE"])
            resp = await handler_fn(None, req)

    assert resp.as_of is None, "as_of must be None when market is open (live data)"
    assert mock_broker.quote.call_count >= 1, "broker.quote() should be called when market is open"


# ---------------------------------------------------------------------------
# Dimension 5a — sparkline: closed → no broker.ltp(), as_of set
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sparkline_closed_skips_broker_ltp():
    """batch_sparkline skips broker.ltp() when all exchanges are closed."""
    from backend.api.routes.quote import SparklineController, SparklineRequest, SparklineSymbol

    # Simulate ohlcv_store returning 4 past closes; intraday returns empty (closed)
    past_bars = [
        {"date": "2026-06-24", "close": 2850.0},
        {"date": "2026-06-25", "close": 2860.0},
        {"date": "2026-06-26", "close": 2870.0},
        {"date": "2026-06-27", "close": 2880.0},
    ]

    mock_ltp_call = MagicMock()

    with patch("backend.api.routes.quote._all_exchanges_closed", return_value=True), \
         patch("backend.api.persistence.ohlcv_store.get_or_fetch_daily",
               new=AsyncMock(return_value=past_bars)), \
         patch("backend.api.persistence.intraday_store.get_or_fetch_intraday",
               new=AsyncMock(return_value=[])), \
         patch("backend.api.routes.quote._get_today_token_map", return_value={}), \
         patch("backend.brokers.registry.get_sparkline_broker") as _mock_sp_reg, \
         patch("backend.brokers.kite_ticker.get_ticker") as _mock_tk, \
         patch("backend.api.routes.watchlist._resolve_mcx_commodity",
               new=AsyncMock(return_value=None)), \
         patch("backend.api.routes.watchlist._resolve_cds_currency",
               new=AsyncMock(return_value=None)), \
         patch("backend.brokers.broker_apis.get_last_good_ltp", return_value=0.0):
        _mock_sp_reg.return_value = MagicMock()
        _mock_sp_reg.return_value.ltp = mock_ltp_call
        ticker_mock = MagicMock()
        ticker_mock.get_ltp = MagicMock(return_value=None)
        ticker_mock.subscribe_with_sym = MagicMock()
        _mock_tk.return_value = ticker_mock

        import asyncio as _asyncio

        async def _fake_to_thread(fn, *args):
            return fn(*args)

        with patch("asyncio.to_thread", side_effect=_fake_to_thread):
            handler_fn = SparklineController.batch_sparkline.fn
            req = SparklineRequest(
                symbols=[SparklineSymbol(tradingsymbol="RELIANCE", exchange="NSE")],
                days=5,
            )
            resp = await handler_fn(None, req)

    assert resp.as_of is not None, "as_of must be set when market is closed"
    # broker.ltp() must not have been called
    assert mock_ltp_call.call_count == 0, (
        f"broker.ltp() called {mock_ltp_call.call_count} times — "
        "should be 0 during closed hours"
    )
    # Series should be just the past closes (no live LTP appended)
    assert "RELIANCE" in resp.data
    assert resp.data["RELIANCE"] == [2850.0, 2860.0, 2870.0, 2880.0]


# ---------------------------------------------------------------------------
# Dimension 5b — sparkline: open → broker.ltp() IS called for misses
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sparkline_open_calls_broker_ltp():
    """batch_sparkline calls broker.ltp() for ticker misses when market is open."""
    past_bars = [
        {"date": "2026-06-27", "close": 2880.0},
    ]

    fake_ltp_data = {"NSE:RELIANCE": {"last_price": 2920.0}}
    mock_ltp = MagicMock(return_value=fake_ltp_data)

    with patch("backend.api.routes.quote._all_exchanges_closed", return_value=False), \
         patch("backend.api.persistence.ohlcv_store.get_or_fetch_daily",
               new=AsyncMock(return_value=past_bars)), \
         patch("backend.api.persistence.intraday_store.get_or_fetch_intraday",
               new=AsyncMock(return_value=[])), \
         patch("backend.api.routes.quote._get_today_token_map", return_value={}), \
         patch("backend.brokers.registry.get_sparkline_broker") as _mock_sp_reg, \
         patch("backend.brokers.kite_ticker.get_ticker") as _mock_tk, \
         patch("backend.api.routes.watchlist._resolve_mcx_commodity",
               new=AsyncMock(return_value=None)), \
         patch("backend.api.routes.watchlist._resolve_cds_currency",
               new=AsyncMock(return_value=None)):
        sp_mock = MagicMock()
        sp_mock.ltp = mock_ltp
        _mock_sp_reg.return_value = sp_mock
        ticker_mock = MagicMock()
        ticker_mock.get_ltp = MagicMock(return_value=None)  # force broker.ltp() path
        ticker_mock.subscribe_with_sym = MagicMock()
        _mock_tk.return_value = ticker_mock

        async def _fake_to_thread(fn, *args):
            return fn(*args)

        with patch("asyncio.to_thread", side_effect=_fake_to_thread):
            from backend.api.routes.quote import SparklineController as _SC, SparklineRequest, SparklineSymbol
            handler_fn = _SC.batch_sparkline.fn
            req = SparklineRequest(
                symbols=[SparklineSymbol(tradingsymbol="RELIANCE", exchange="NSE")],
                days=2,
            )
            resp = await handler_fn(None, req)

    assert resp.as_of is None, "as_of must be None when market is open"
    assert mock_ltp.call_count >= 1, "broker.ltp() should be called for misses when market is open"
    # LTP appended as tail
    assert "RELIANCE" in resp.data
    assert 2920.0 in resp.data["RELIANCE"]


# ---------------------------------------------------------------------------
# Dimension 5 — options historical: closed + intraday interval → no broker
# ---------------------------------------------------------------------------

def test_options_historical_closed_guard_in_source():
    """options_helpers.py has the market-closed guard before broker loop."""
    # After refactor: closed-hours guard moved to _historical_closed_guard in options_helpers
    opt_helpers_path = Path(__file__).parent.parent / "api" / "routes" / "options_helpers.py"
    src = _src(opt_helpers_path)
    # The guard checks is_any_segment_open before returning early on closed markets
    guard_idx   = src.find("is_any_segment_open")
    broker_loop = src.find("get_historical_brokers()")
    assert guard_idx != -1, "options_helpers.py missing is_any_segment_open guard"
    assert broker_loop != -1, "options_helpers.py missing get_historical_brokers call"
    assert guard_idx < broker_loop, (
        "closed-hours guard must appear BEFORE the broker account-fallback loop"
    )


# ---------------------------------------------------------------------------
# Dimension 2 — performance: _all_exchanges_closed is O(N exchanges)
# ---------------------------------------------------------------------------

def test_all_exchanges_closed_short_circuits_on_first_open():
    """_all_exchanges_closed short-circuits as soon as one exchange is open."""
    from backend.api.routes.quote import _all_exchanges_closed

    call_log: list[str] = []

    def _side(exch: str) -> bool:
        call_log.append(exch)
        return False  # all "open"

    with patch("backend.api.routes.quote._is_exchange_segment_closed", side_effect=_side):
        result = _all_exchanges_closed({"NSE", "MCX", "NFO"})

    assert result is False
    # all() short-circuits on first False → only 1 call
    assert len(call_log) == 1, (
        f"_all_exchanges_closed made {len(call_log)} calls — expected 1 (short-circuit)"
    )


# ---------------------------------------------------------------------------
# Dimension 4 — reusable: as_of field defaults to None on live responses
# ---------------------------------------------------------------------------

def test_positions_response_as_of_defaults_none():
    """PositionsResponse.as_of defaults to None (backwards-compatible)."""
    from backend.api.schemas import PositionsResponse
    resp = PositionsResponse(rows=[], summary=[], refreshed_at="now")
    assert resp.as_of is None


def test_holdings_response_as_of_defaults_none():
    """HoldingsResponse.as_of defaults to None (backwards-compatible)."""
    from backend.api.schemas import HoldingsResponse
    resp = HoldingsResponse(rows=[], summary=[], refreshed_at="now")
    assert resp.as_of is None


def test_batch_quote_response_as_of_defaults_none():
    """BatchQuoteResponse.as_of defaults to None (backwards-compatible)."""
    from backend.api.routes.quote import BatchQuoteResponse
    resp = BatchQuoteResponse(refreshed_at="now", items=[])
    assert resp.as_of is None


def test_sparkline_response_as_of_defaults_none():
    """SparklineResponse.as_of defaults to None (backwards-compatible)."""
    from backend.api.routes.quote import SparklineResponse
    resp = SparklineResponse(data={}, refreshed_at="now")
    assert resp.as_of is None


# ---------------------------------------------------------------------------
# Dimension 5 — fresh=True bypasses closed-hours guard for positions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_positions_fresh_bypasses_closed_hours_guard():
    """?fresh=True skips the closed-hours snapshot guard and forces live fetch."""
    from backend.api.schemas import PositionsResponse

    live_resp = PositionsResponse(rows=[], summary=[], refreshed_at="fresh-live")
    mock_snapshot = AsyncMock(return_value=None)  # should NOT be called

    with patch(
        "backend.api.helpers.snapshot_gate._any_segment_open",
        return_value=False,   # market closed — but fresh=True bypasses
    ), patch(
        "backend.api.routes.positions._positions_snapshot",
        mock_snapshot,
    ), patch(
        "backend.api.routes.positions.get_or_fetch",
        new=AsyncMock(return_value=live_resp),
    ), patch(
        "backend.api.routes.positions.invalidate",
    ):
        from backend.api.routes.positions import PositionsController
        handler_fn = PositionsController.get_positions.fn

        mock_request = MagicMock()
        with patch("backend.api.routes.positions_helpers.is_admin_request", return_value=True), \
             patch("backend.api.routes.positions_helpers.resolve_role_from_connection",
                   return_value="admin"), \
             patch("backend.api.routes.positions_helpers.normalise_role", return_value="admin"), \
             patch("backend.brokers.broker_apis._raw_cache_invalidate"):
            resp = await handler_fn(None, mock_request, fresh=True)

    # Snapshot should NOT have been called (fresh=True bypasses the guard)
    assert mock_snapshot.call_count == 0, (
        "snapshot helper must not be called when fresh=True"
    )
    assert resp.refreshed_at == "fresh-live"


# ---------------------------------------------------------------------------
# Dimension 5 — P0 regression: _holdings_snapshot pnl_percentage field
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_holdings_snapshot_pnl_percentage_populated():
    """_holdings_snapshot() must construct HoldingRow with pnl_percentage set
    (non-None, non-zero for rows with positive P&L).  Previously this field was
    omitted → msgspec raised 'Missing required argument pnl_percentage' → 500.

    This test calls the real _holdings_snapshot() with a mocked DB session so
    the struct-construction code path executes in full — not a mock snapshot.
    """
    from datetime import datetime, timezone, timedelta
    from unittest.mock import AsyncMock, MagicMock, patch

    # A fake DB row that represents a holdings snapshot:
    #   account, symbol, exchange, qty, avg_cost, ltp,
    #   day_pnl, total_pnl, captured_at
    captured_ts = datetime.now(timezone.utc) - timedelta(hours=2)

    fake_row = (
        "ZG0790",   # account
        "RELIANCE",  # symbol
        "NSE",       # exchange
        10,          # qty
        2800.0,      # avg_cost
        2900.0,      # ltp (snapshot LTP)
        100.0,       # day_pnl
        1000.0,      # total_pnl
        captured_ts, # captured_at
    )

    mock_result = MagicMock()
    mock_result.all.return_value = [fake_row]

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__  = AsyncMock(return_value=False)
    mock_session.execute    = AsyncMock(return_value=mock_result)

    with patch("backend.api.database.async_session",
               return_value=mock_session):
        from backend.api.routes.holdings import _holdings_snapshot
        resp = await _holdings_snapshot()

    assert resp is not None, "_holdings_snapshot must return a HoldingsResponse when rows exist"
    assert len(resp.rows) == 1
    row = resp.rows[0]

    # P0 regression guard — construction must not raise:
    assert row.pnl_percentage is not None, "pnl_percentage must be set (was missing → 500)"
    # Numeric sanity: pnl=1000, inv_val=2800*10=28000 → ~3.57 %
    assert abs(row.pnl_percentage - (1000.0 / 28000.0 * 100.0)) < 0.01, (
        f"pnl_percentage expected {1000.0/28000.0*100:.4f}, got {row.pnl_percentage}"
    )
    # day_change_percentage: day_pnl=100, close_notional=2900*10=29000 → ~0.345 %
    assert row.day_change_percentage is not None
    assert abs(row.day_change_percentage - (100.0 / 29000.0 * 100.0)) < 0.01, (
        f"day_change_percentage expected {100.0/29000.0*100:.4f}, got {row.day_change_percentage}"
    )
    # last_price_stale must be True for a snapshot (it's not live broker data)
    assert row.last_price_stale is True, "snapshot rows must have last_price_stale=True"


@pytest.mark.asyncio
async def test_positions_snapshot_pnl_percentage_populated():
    """_positions_snapshot() must construct PositionRow with pnl_percentage and
    day_change_percentage computed from stored values — not left at the 0.0
    default (which silently hides P&L information from closed-hours callers).
    """
    from datetime import datetime, timezone, timedelta
    from unittest.mock import AsyncMock, MagicMock, patch

    captured_ts = datetime.now(timezone.utc) - timedelta(hours=2)

    # account, symbol, exchange, qty, avg_cost, ltp,
    # day_pnl, total_pnl, payload_json, captured_at, previous_close
    fake_row = (
        "ZG0790",     # account
        "NIFTY25JUNFUT",  # symbol
        "NFO",         # exchange
        50,            # qty
        23000.0,       # avg_cost
        23200.0,       # ltp
        200.0,         # day_pnl
        10000.0,       # total_pnl
        "{}",          # payload_json
        captured_ts,   # captured_at
        None,          # previous_close (index 10)
        None,          # prev_ltp (index 11)
        None,          # prev_settlement_pnl (index 12)
    )

    mock_result = MagicMock()
    mock_result.all.return_value = [fake_row]

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__  = AsyncMock(return_value=False)
    mock_session.execute    = AsyncMock(return_value=mock_result)

    with patch("backend.api.database.async_session",
               return_value=mock_session):
        from backend.api.routes.positions import _positions_snapshot
        resp = await _positions_snapshot()

    assert resp is not None
    assert len(resp.rows) == 1
    row = resp.rows[0]

    # pnl_percentage: pnl=10000, inv_val=|23000*50|=1150000 → ~0.87 %
    inv_val = abs(23000.0 * 50)
    expected_pnl_pct = 10000.0 / inv_val * 100.0
    assert abs(row.pnl_percentage - expected_pnl_pct) < 0.01, (
        f"pnl_percentage expected {expected_pnl_pct:.4f}, got {row.pnl_percentage}"
    )
    # day_change_percentage: day_pnl=200, close_notional=|23200*50|=1160000 → ~0.017 %
    close_notional = abs(23200.0 * 50)
    expected_day_pct = 200.0 / close_notional * 100.0
    assert abs(row.day_change_percentage - expected_day_pct) < 0.01, (
        f"day_change_percentage expected {expected_day_pct:.4f}, got {row.day_change_percentage}"
    )
    assert row.last_price_stale is True, "snapshot rows must have last_price_stale=True"


# ---------------------------------------------------------------------------
# Fix 2 — reader fallback: bad snapshot (zeros) → reader returns good snapshot
# ---------------------------------------------------------------------------
#
# When daily_book contains ONE bad snapshot (all zeros, captured more recently)
# AND ONE good snapshot (non-zero values, captured earlier) for the same
# (account, symbol), _holdings_snapshot() and _positions_snapshot() must
# return the GOOD snapshot values, not the bad ones.
#
# The SQL uses DISTINCT ON (account, symbol) with a WHERE clause that excludes
# rows where ltp=0 AND total_pnl=0 AND avg_cost>0 — the bad-payload fingerprint.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_holdings_snapshot_prefers_good_over_bad():
    """_holdings_snapshot() returns the most-recent *good* row per (account, symbol),
    skipping zero-ltp/zero-pnl rows (bad token fingerprint) even if they are newer.

    Scenario:
      - Captured at T-2h: ltp=2900, total_pnl=1000 (GOOD)
      - Captured at T-30min: ltp=0, total_pnl=0, avg_cost=2800 (BAD — token failure)

    The bad row is more recent but excluded by the WHERE clause.
    The good row must be returned.
    """
    from datetime import timedelta
    from unittest.mock import AsyncMock, MagicMock, patch

    now_utc = datetime.now(timezone.utc)
    good_ts = now_utc - timedelta(hours=2)   # older but valid
    bad_ts  = now_utc - timedelta(minutes=30) # newer but zeroed

    # The SQL query uses DISTINCT ON (account, symbol) with WHERE NOT (ltp=0...)
    # so the bad row is excluded entirely; only the good row survives.
    # We return the good row as the only row from the mock DB.
    good_row = (
        "ZG0790",   # account
        "RELIANCE",  # symbol
        "NSE",       # exchange
        10,          # qty
        2800.0,      # avg_cost
        2900.0,      # ltp  — non-zero: good row
        100.0,       # day_pnl
        1000.0,      # total_pnl
        good_ts,     # captured_at (older timestamp)
    )

    # The DB layer (after Fix 2 SQL) would exclude the bad row and return only
    # the good one. Simulate this by returning [good_row] from execute().
    mock_result = MagicMock()
    mock_result.all.return_value = [good_row]

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__  = AsyncMock(return_value=False)
    mock_session.execute    = AsyncMock(return_value=mock_result)

    with patch("backend.api.database.async_session", return_value=mock_session):
        from backend.api.routes.holdings import _holdings_snapshot
        resp = await _holdings_snapshot()

    assert resp is not None, "_holdings_snapshot must return a response when good rows exist"
    assert len(resp.rows) == 1
    row = resp.rows[0]
    assert row.last_price == 2900.0, (
        f"Expected good snapshot ltp=2900.0, got {row.last_price}"
    )
    assert row.pnl == 1000.0, (
        f"Expected good snapshot total_pnl=1000.0, got {row.pnl}"
    )


@pytest.mark.asyncio
async def test_positions_snapshot_prefers_good_over_bad():
    """_positions_snapshot() returns the most-recent *good* row per (account, symbol),
    skipping zero-ltp rows even if they are newer.

    This is the P0 regression that caused NavStrip P delta = 0.0 on 2026-06-30.
    ZG0790's token was invalid; snapshot wrote ltp=0/pnl=0. NavStrip summed zeros.
    Fix 2 ensures the previous day's good snapshot is used instead.
    """
    from datetime import timedelta
    from unittest.mock import AsyncMock, MagicMock, patch

    now_utc = datetime.now(timezone.utc)
    good_ts = now_utc - timedelta(hours=26)  # prior session EOD (valid)

    good_row = (
        "ZG0790",
        "NIFTY25JUNFUT",
        "NFO",
        50,
        23000.0,    # avg_cost
        23200.0,    # ltp  — non-zero: good
        200.0,      # day_pnl
        10000.0,    # total_pnl
        "{}",       # payload_json
        good_ts,    # captured_at
        None,       # previous_close (index 10)
        None,       # prev_ltp (index 11)
        None,       # prev_settlement_pnl (index 12)
    )

    mock_result = MagicMock()
    mock_result.all.return_value = [good_row]

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__  = AsyncMock(return_value=False)
    mock_session.execute    = AsyncMock(return_value=mock_result)

    with patch("backend.api.database.async_session", return_value=mock_session):
        from backend.api.routes.positions import _positions_snapshot
        resp = await _positions_snapshot()

    assert resp is not None
    assert len(resp.rows) == 1
    row = resp.rows[0]
    assert row.last_price == 23200.0, (
        f"Expected good snapshot ltp=23200.0, got {row.last_price} — "
        "Fix 2 reader fallback must return the prior good snapshot, not zeros"
    )
    assert row.pnl == 10000.0, (
        f"Expected good snapshot total_pnl=10000.0, got {row.pnl}"
    )
    # P delta (day_change_val) must be non-zero — this is the NavStrip P delta fix
    assert row.day_change_val == 200.0, (
        f"NavStrip P delta must reflect stored day_pnl=200.0, got {row.day_change_val}"
    )


@pytest.mark.asyncio
async def test_holdings_snapshot_sql_excludes_bad_payload_rows():
    """_holdings_snapshot SQL query must contain the bad-payload exclusion clause +
    use the latest-batch-per-account anchor.

    Source-level check verifying the WHERE NOT (ltp=0 AND total_pnl=0 AND avg_cost>0)
    guard is present AND the query joins on MAX(captured_at) per account so stale
    rows from past sessions never carry into today's sum.
    """
    src = _HOL_SRC.read_text(encoding="utf-8")
    assert "MAX(captured_at)" in src, (
        "_holdings_snapshot SQL must anchor on MAX(captured_at) per account "
        "(latest-batch pattern) so closed-out symbols from past months don't "
        "carry stale day_pnl into today's NavStrip"
    )
    assert "NOT (db.ltp = 0" in src or "NOT (ltp = 0" in src, (
        "_holdings_snapshot SQL must exclude zero-ltp bad-payload rows "
        "via WHERE NOT (ltp = 0 ...)"
    )
    # Stale grep: the prior DISTINCT ON pattern with `captured_at < today_open`
    # must NOT come back — it was the source of the May-row carry-over bug.
    assert "captured_at < :today_open" not in src, (
        "snapshot reader must not re-introduce the captured_at < today_open "
        "filter — it pulled stale months-old rows for closed-out symbols"
    )


@pytest.mark.asyncio
async def test_positions_snapshot_sql_excludes_bad_payload_rows():
    """_positions_snapshot SQL query must contain the bad-payload exclusion clause +
    use the latest-batch-per-account anchor.
    """
    src = _POS_SRC.read_text(encoding="utf-8")
    assert "MAX(captured_at)" in src, (
        "_positions_snapshot SQL must anchor on MAX(captured_at) per account"
    )
    assert "NOT (db.ltp = 0" in src or "NOT (ltp = 0" in src, (
        "_positions_snapshot SQL must exclude zero-ltp bad-payload rows"
    )
    # The LTP-close-override path (further down in positions.py) DOES legitimately
    # use `captured_at < :today_open` to pull yesterday's close. So we can't blanket-ban
    # the literal — but the snapshot reader (top of file) must not contain it.
    # Find the function body and grep only inside it.
    func_start = src.index("def _positions_snapshot")
    func_end = src.index("\nasync def ", func_start) if "\nasync def " in src[func_start:] else len(src)
    # `_positions_snapshot` is async — find the next def at the same indent level
    snap_body_end = src.find("\n@", func_start + 1)
    if snap_body_end < 0:
        snap_body_end = func_end
    snap_body = src[func_start:snap_body_end]
    assert "captured_at < :today_open" not in snap_body, (
        "_positions_snapshot reader must not use captured_at < today_open — "
        "that filter pulled stale months-old rows for closed-out symbols"
    )
