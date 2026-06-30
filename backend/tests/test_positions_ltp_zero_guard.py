"""Tests — LTP=0 guard: positions and holdings preserve last-known-good
LTP when the broker returns zero (rate-limit cool-off or Dhan/Groww
missing field).

Root cause: PriceBroker.quote() raises when all brokers are in their
60s rate-limit cool-off after a 429. `backfill_market_data` caught the
exception and returned 0 (no patches), leaving Dhan/Groww rows at
`last_price=0`. The positions route's `_override_stale_ltp_from_ticker`
should rescue those rows from the live tick, but only when the symbol is
subscribed. Without it the UI alternated between 0 and real values once
per 30s cache cycle.

Fix surfaces tested here:
  1. `backfill_market_data` ticker fallback — when PriceBroker raises,
     the function synthesises LTP values from KiteTicker before giving up.
  2. Holdings `_override_stale_ltp_from_ticker` — patched zero-LTP rows
     to live tick, recomputes day_change_val.
  3. Positions `_override_stale_ltp_from_ticker` — already existed;
     regression guard that it fires on zero-LTP rows.
  4. None returned by ticker → row stays at 0, no crash (safe no-op).

Quality dimensions covered (per project test spec):
  - Correct behaviour (primary)
  - No stale code / name-error regressions
  - Performance: no broker network calls (mocks only)
  - UX: zero never reaches the serialised response when ticker has data
  - Data integrity: non-zero broker LTP is never overwritten
"""

import pandas as pd
import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pos_df(last_price: float = 0.0, close_price: float = 220.0,
             quantity: int = 10, pnl: float = 0.0) -> pd.DataFrame:
    """Minimal positions DataFrame row matching broker_apis shape."""
    return pd.DataFrame([{
        'tradingsymbol': 'CRUDEOIL26JUL6900PE',
        'exchange': 'MCX',
        'last_price': last_price,
        'close_price': close_price,
        'quantity': quantity,
        'overnight_quantity': quantity,
        'day_buy_quantity': 0,
        'day_sell_quantity': 0,
        'day_buy_value': 0.0,
        'day_sell_value': 0.0,
        'average_price': 200.0,
        'realised': 0.0,
        'pnl': pnl,
        'day_change_val': 0.0,
        'day_change': 0.0,
    }])


def _hold_df(last_price: float = 0.0, close_price: float = 1800.0,
              opening_quantity: int = 10) -> pd.DataFrame:
    """Minimal holdings DataFrame row matching broker_apis shape."""
    return pd.DataFrame([{
        'tradingsymbol': 'GOLDBEES',
        'exchange': 'NSE',
        'last_price': last_price,
        'close_price': close_price,
        'opening_quantity': opening_quantity,
        'average_price': 1750.0,
        'pnl': 0.0,
        'day_change_val': 0.0,
        'day_change': 0.0,
    }])


def _mock_ticker(ltp_value: float | None) -> MagicMock:
    m = MagicMock()
    m.get_ltp_by_sym.return_value = ltp_value
    return m


# ---------------------------------------------------------------------------
# 1. backfill_market_data — ticker fallback when PriceBroker raises
# ---------------------------------------------------------------------------

def _clear_ltp_cache():
    from backend.brokers import broker_apis
    with broker_apis._LAST_GOOD_LTP_LOCK:
        broker_apis._LAST_GOOD_LTP.clear()


class TestBackfillMarketDataTickerFallback:
    """When PriceBroker.quote() raises (all brokers rate-limited), the
    function must fall back to KiteTicker to populate `last_price`.
    The previously-zero rows must be patched to the ticker's value."""

    def setup_method(self):
        _clear_ltp_cache()

    def test_ticker_fallback_patches_zero_ltp(self):
        from backend.brokers.broker_apis import backfill_market_data

        df = _pos_df(last_price=0.0, close_price=220.0)

        mock_ticker = _mock_ticker(264.5)

        # get_price_broker is imported inside backfill_market_data via
        # `from backend.brokers.registry import get_price_broker`, so
        # we patch it at the registry module level.
        with patch('backend.brokers.registry.get_price_broker',
                   side_effect=RuntimeError("rate-limited")), \
             patch('backend.brokers.kite_ticker.get_ticker',
                   return_value=mock_ticker):
            backfill_market_data(df)

        assert float(df.at[0, 'last_price']) == pytest.approx(264.5, abs=0.01), \
            f"last_price should be patched from ticker, got {df.at[0, 'last_price']}"
        # day_change_val must be recomputed on the patched row
        expected_dcv = (264.5 - 220.0) * 10  # (ltp - close) × qty
        assert float(df.at[0, 'day_change_val']) == pytest.approx(expected_dcv, abs=0.01), \
            f"day_change_val not recomputed: expected {expected_dcv}"

    def test_ticker_fallback_none_leaves_row_at_zero(self):
        """When ticker also returns None, the row stays at 0 — no crash."""
        from backend.brokers.broker_apis import backfill_market_data

        df = _pos_df(last_price=0.0)
        mock_ticker = _mock_ticker(None)

        with patch('backend.brokers.registry.get_price_broker',
                   side_effect=RuntimeError("rate-limited")), \
             patch('backend.brokers.kite_ticker.get_ticker',
                   return_value=mock_ticker):
            backfill_market_data(df)

        assert float(df.at[0, 'last_price']) == pytest.approx(0.0), \
            "Row must stay at 0 when ticker also has no data"

    def test_nonzero_broker_ltp_not_overwritten(self):
        """When the source broker already supplied a valid LTP, backfill
        must not overwrite it even if the ticker has a different value."""
        from backend.brokers.broker_apis import backfill_market_data

        df = _pos_df(last_price=260.0, close_price=220.0)
        # Both close and last_price are > 0 → backfill sees no missing rows.
        mock_ticker = _mock_ticker(999.0)

        with patch('backend.brokers.registry.get_price_broker',
                   side_effect=RuntimeError("rate-limited")), \
             patch('backend.brokers.kite_ticker.get_ticker',
                   return_value=mock_ticker):
            backfill_market_data(df)

        # No missing rows → no ticker call should have been needed
        assert float(df.at[0, 'last_price']) == pytest.approx(260.0), \
            "Valid broker LTP must not be overwritten"

    def test_ticker_fallback_zero_ltp_is_skipped(self):
        """Ticker returning 0.0 is treated the same as None — not patched."""
        from backend.brokers.broker_apis import backfill_market_data

        df = _pos_df(last_price=0.0)
        mock_ticker = _mock_ticker(0.0)

        with patch('backend.brokers.registry.get_price_broker',
                   side_effect=RuntimeError("rate-limited")), \
             patch('backend.brokers.kite_ticker.get_ticker',
                   return_value=mock_ticker):
            backfill_market_data(df)

        assert float(df.at[0, 'last_price']) == pytest.approx(0.0), \
            "Ticker LTP=0 must not be patched in — still missing data"


# ---------------------------------------------------------------------------
# 2. Holdings _override_stale_ltp_from_ticker
# ---------------------------------------------------------------------------

class TestHoldingsTickerOverride:
    """The holdings route's `_override_stale_ltp_from_ticker` patches
    zero-LTP rows from the live KiteTicker. Holdings never had this
    guard before the fix."""

    def setup_method(self):
        _clear_ltp_cache()

    def test_zero_ltp_patched_from_ticker(self):
        from backend.api.routes.holdings import _override_stale_ltp_from_ticker

        df = _hold_df(last_price=0.0, close_price=1800.0)
        mock_ticker = _mock_ticker(1870.0)

        with patch('backend.brokers.kite_ticker.get_ticker',
                   return_value=mock_ticker):
            _override_stale_ltp_from_ticker(df)

        assert float(df.at[0, 'last_price']) == pytest.approx(1870.0, abs=0.01)
        expected_dcv = (1870.0 - 1800.0) * 10
        assert float(df.at[0, 'day_change_val']) == pytest.approx(expected_dcv, abs=0.01)

    def test_nonzero_ltp_not_overwritten(self):
        """If holdings already has a valid LTP, the ticker override
        must not touch it."""
        from backend.api.routes.holdings import _override_stale_ltp_from_ticker

        df = _hold_df(last_price=1850.0, close_price=1800.0)
        mock_ticker = _mock_ticker(9999.0)

        with patch('backend.brokers.kite_ticker.get_ticker',
                   return_value=mock_ticker):
            _override_stale_ltp_from_ticker(df)

        assert float(df.at[0, 'last_price']) == pytest.approx(1850.0), \
            "Valid holdings LTP must not be overwritten"

    def test_ticker_unavailable_no_crash(self):
        """If get_ticker() raises, the function exits gracefully."""
        from backend.api.routes.holdings import _override_stale_ltp_from_ticker

        df = _hold_df(last_price=0.0)
        with patch('backend.brokers.kite_ticker.get_ticker',
                   side_effect=RuntimeError("ticker not started")):
            _override_stale_ltp_from_ticker(df)  # must not raise

        assert float(df.at[0, 'last_price']) == pytest.approx(0.0)

    def test_ticker_returns_none_no_patch(self):
        from backend.api.routes.holdings import _override_stale_ltp_from_ticker

        df = _hold_df(last_price=0.0)
        mock_ticker = _mock_ticker(None)

        with patch('backend.brokers.kite_ticker.get_ticker',
                   return_value=mock_ticker):
            _override_stale_ltp_from_ticker(df)

        assert float(df.at[0, 'last_price']) == pytest.approx(0.0)

    def test_empty_df_no_crash(self):
        from backend.api.routes.holdings import _override_stale_ltp_from_ticker

        _override_stale_ltp_from_ticker(pd.DataFrame())  # must not raise


# ---------------------------------------------------------------------------
# 3. Positions _override_stale_ltp_from_ticker — zero-LTP guard regression
# ---------------------------------------------------------------------------

class TestPositionsTickerOverrideZeroLtp:
    """The positions ticker override already existed but was gated on
    `abs(tick - current) > 0.005`. When current=0 and tick>0 the
    condition fires — verify this guards the zero-LTP case too."""

    def setup_method(self):
        _clear_ltp_cache()

    def test_zero_ltp_patched(self):
        from backend.api.routes.positions import _override_stale_ltp_from_ticker

        df = _pos_df(last_price=0.0, close_price=220.0, pnl=-2000.0)
        mock_ticker = _mock_ticker(264.5)

        with patch('backend.brokers.kite_ticker.get_ticker',
                   return_value=mock_ticker):
            _override_stale_ltp_from_ticker(df)

        assert float(df.at[0, 'last_price']) == pytest.approx(264.5, abs=0.01)
        # pnl additive patch: pnl_new = pnl_old + (new_ltp - old_ltp) * qty
        expected_pnl = -2000.0 + (264.5 - 0.0) * 10
        assert float(df.at[0, 'pnl']) == pytest.approx(expected_pnl, abs=0.01)

    def test_ticker_returns_zero_no_patch(self):
        """Ticker returning 0.0 must not be written to last_price."""
        from backend.api.routes.positions import _override_stale_ltp_from_ticker

        df = _pos_df(last_price=0.0, close_price=220.0)
        mock_ticker = _mock_ticker(0.0)

        with patch('backend.brokers.kite_ticker.get_ticker',
                   return_value=mock_ticker):
            _override_stale_ltp_from_ticker(df)

        assert float(df.at[0, 'last_price']) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 4. last-known-good LTP rescue via positions + holdings override
# ---------------------------------------------------------------------------

class TestLastGoodRescueViaPositionsOverride:
    """Regression: when ALL live sources (broker + ticker) return 0 or raise,
    the row must use the last-known-good cache value rather than propagating 0."""

    def setup_method(self):
        """Clear the cache before each test to isolate state."""
        from backend.brokers import broker_apis
        with broker_apis._LAST_GOOD_LTP_LOCK:
            broker_apis._LAST_GOOD_LTP.clear()

    def test_positions_last_good_rescue_when_ticker_none(self):
        from backend.brokers.broker_apis import record_good_ltp
        from backend.api.routes.positions import _override_stale_ltp_from_ticker

        # Simulate a prior successful fetch having warmed the cache.
        record_good_ltp("CRUDEOIL26JUL6900PE", 264.5)

        df = _pos_df(last_price=0.0, close_price=220.0, pnl=0.0)
        mock_ticker = _mock_ticker(None)  # ticker has no data

        with patch('backend.brokers.kite_ticker.get_ticker',
                   return_value=mock_ticker):
            _override_stale_ltp_from_ticker(df)

        assert float(df.at[0, 'last_price']) == pytest.approx(264.5, abs=0.001), \
            f"Expected last-good 264.5, got {df.at[0, 'last_price']}"
        assert bool(df.at[0, 'last_price_stale']) is True, \
            "last_price_stale must be True for cache-rescued rows"

    def test_positions_no_cache_stays_zero(self):
        """No cache entry: row stays at 0, no crash, no stale flag."""
        from backend.api.routes.positions import _override_stale_ltp_from_ticker

        df = _pos_df(last_price=0.0, close_price=220.0)
        mock_ticker = _mock_ticker(None)

        with patch('backend.brokers.kite_ticker.get_ticker',
                   return_value=mock_ticker):
            _override_stale_ltp_from_ticker(df)

        assert float(df.at[0, 'last_price']) == pytest.approx(0.0)
        assert 'last_price_stale' not in df.columns or not bool(df.at[0, 'last_price_stale'])

    def test_positions_ticker_ltp_recorded_as_good(self):
        """When ticker returns a valid LTP, it must be recorded into the
        last-known-good cache for future fallback."""
        from backend.brokers.broker_apis import get_last_good_ltp
        from backend.api.routes.positions import _override_stale_ltp_from_ticker

        df = _pos_df(last_price=100.0, close_price=220.0)  # existing non-zero LTP
        # Ticker returns a different value — override fires.
        mock_ticker = _mock_ticker(264.5)

        with patch('backend.brokers.kite_ticker.get_ticker',
                   return_value=mock_ticker):
            _override_stale_ltp_from_ticker(df)

        cached = get_last_good_ltp("CRUDEOIL26JUL6900PE")
        assert cached == pytest.approx(264.5, abs=0.001), \
            f"Ticker LTP must be recorded in cache, got {cached}"

    def test_holdings_last_good_rescue_when_ticker_none(self):
        from backend.brokers.broker_apis import record_good_ltp
        from backend.api.routes.holdings import _override_stale_ltp_from_ticker as _h_override

        record_good_ltp("GOLDBEES", 1870.0)

        df = _hold_df(last_price=0.0, close_price=1800.0)
        mock_ticker = _mock_ticker(None)

        with patch('backend.brokers.kite_ticker.get_ticker',
                   return_value=mock_ticker):
            _h_override(df)

        assert float(df.at[0, 'last_price']) == pytest.approx(1870.0, abs=0.001), \
            f"Expected last-good 1870.0, got {df.at[0, 'last_price']}"
        assert bool(df.at[0, 'last_price_stale']) is True, \
            "last_price_stale must be True for cache-rescued holdings rows"

    def test_holdings_live_ticker_not_marked_stale(self):
        """When the ticker provides a live value, the row must NOT be stale."""
        from backend.api.routes.holdings import _override_stale_ltp_from_ticker as _h_override

        df = _hold_df(last_price=0.0, close_price=1800.0)
        mock_ticker = _mock_ticker(1870.0)

        with patch('backend.brokers.kite_ticker.get_ticker',
                   return_value=mock_ticker):
            _h_override(df)

        assert float(df.at[0, 'last_price']) == pytest.approx(1870.0, abs=0.001)
        assert 'last_price_stale' not in df.columns or not bool(df.at[0, 'last_price_stale']), \
            "Live ticker row must not be flagged as stale"
