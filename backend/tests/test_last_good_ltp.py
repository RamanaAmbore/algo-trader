"""Tests — last-known-good LTP cache.

Verifies that `record_good_ltp` / `get_last_good_ltp` in broker_apis:
  1. Store and retrieve a valid LTP within the TTL window.
  2. Expire entries older than max_age_s (TTL bypass via monkeypatching time).
  3. Store multiple symbols independently.
  4. Ignore zero / negative LTP writes.
  5. Are safe under concurrent access (lock works, no data corruption).

Quality dimensions (per project test spec):
  - Correct behaviour (primary — cache semantics)
  - Performance: no broker / network calls
  - Data integrity: zero never stored, TTL respected, symbols independent
  - Concurrency: threading.Lock covers concurrent record + fetch
"""

import threading
import time
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset_cache():
    """Clear the module-level _LAST_GOOD_LTP dict between tests."""
    from backend.brokers import broker_apis
    with broker_apis._LAST_GOOD_LTP_LOCK:
        broker_apis._LAST_GOOD_LTP.clear()


# ---------------------------------------------------------------------------
# 1. Basic record + fetch within TTL
# ---------------------------------------------------------------------------

class TestRecordAndFetch:

    def setup_method(self):
        _reset_cache()

    def test_record_then_fetch_returns_value(self):
        from backend.brokers.broker_apis import record_good_ltp, get_last_good_ltp

        record_good_ltp("RELIANCE", 2850.5)
        result = get_last_good_ltp("RELIANCE")
        assert result == pytest.approx(2850.5, abs=0.001), \
            f"Expected 2850.5, got {result}"

    def test_unknown_symbol_returns_none(self):
        from backend.brokers.broker_apis import get_last_good_ltp

        result = get_last_good_ltp("NOSUCHSYM")
        assert result is None

    def test_empty_symbol_returns_none(self):
        from backend.brokers.broker_apis import record_good_ltp, get_last_good_ltp

        record_good_ltp("", 100.0)
        assert get_last_good_ltp("") is None

    def test_zero_ltp_not_stored(self):
        from backend.brokers.broker_apis import record_good_ltp, get_last_good_ltp

        record_good_ltp("RELIANCE", 0.0)
        assert get_last_good_ltp("RELIANCE") is None, \
            "Zero LTP must not be written to the cache"

    def test_negative_ltp_not_stored(self):
        from backend.brokers.broker_apis import record_good_ltp, get_last_good_ltp

        record_good_ltp("RELIANCE", -10.0)
        assert get_last_good_ltp("RELIANCE") is None, \
            "Negative LTP must not be written to the cache"

    def test_overwrite_with_newer_value(self):
        from backend.brokers.broker_apis import record_good_ltp, get_last_good_ltp

        record_good_ltp("INFY", 1500.0)
        record_good_ltp("INFY", 1510.0)
        assert get_last_good_ltp("INFY") == pytest.approx(1510.0, abs=0.001), \
            "Second write must overwrite the first"


# ---------------------------------------------------------------------------
# 2. TTL expiry
# ---------------------------------------------------------------------------

class TestTtlExpiry:

    def setup_method(self):
        _reset_cache()

    def test_expired_entry_returns_none(self):
        """Entry recorded in the past beyond max_age_s must return None."""
        from backend.brokers.broker_apis import record_good_ltp, get_last_good_ltp, _LAST_GOOD_LTP_LOCK
        import backend.brokers.broker_apis as bapi

        # Write a real entry first so the key exists.
        record_good_ltp("GOLDM", 6500.0)

        # Simulate time advancing 3601 seconds by patching the stored
        # timestamp backwards via direct dict manipulation (avoids
        # monkey-patching time.time globally which can interfere with
        # other threading primitives).
        with _LAST_GOOD_LTP_LOCK:
            old_ts, old_ltp = bapi._LAST_GOOD_LTP["GOLDM"]
            bapi._LAST_GOOD_LTP["GOLDM"] = (old_ts - 3601.0, old_ltp)

        result = get_last_good_ltp("GOLDM", max_age_s=3600.0)
        assert result is None, \
            f"Expired entry must return None, got {result}"

    def test_not_yet_expired_entry_returned(self):
        """Entry recorded 59 minutes ago (< 1 hour) must still be returned."""
        from backend.brokers.broker_apis import record_good_ltp, get_last_good_ltp, _LAST_GOOD_LTP_LOCK
        import backend.brokers.broker_apis as bapi

        record_good_ltp("SILVER", 92000.0)

        # Simulate 59 minutes elapsed.
        with _LAST_GOOD_LTP_LOCK:
            old_ts, old_ltp = bapi._LAST_GOOD_LTP["SILVER"]
            bapi._LAST_GOOD_LTP["SILVER"] = (old_ts - 3540.0, old_ltp)

        result = get_last_good_ltp("SILVER", max_age_s=3600.0)
        assert result == pytest.approx(92000.0, abs=0.001), \
            f"59-minute-old entry must still be returned, got {result}"

    def test_custom_max_age_respected(self):
        """Caller can pass a shorter max_age_s (e.g. 60 s) to tighten TTL."""
        from backend.brokers.broker_apis import record_good_ltp, get_last_good_ltp, _LAST_GOOD_LTP_LOCK
        import backend.brokers.broker_apis as bapi

        record_good_ltp("CRUDEOIL", 6200.0)

        with _LAST_GOOD_LTP_LOCK:
            old_ts, old_ltp = bapi._LAST_GOOD_LTP["CRUDEOIL"]
            bapi._LAST_GOOD_LTP["CRUDEOIL"] = (old_ts - 61.0, old_ltp)

        result = get_last_good_ltp("CRUDEOIL", max_age_s=60.0)
        assert result is None, \
            f"Entry must be expired with 60s TTL after 61s, got {result}"


# ---------------------------------------------------------------------------
# 3. Multiple symbols stored independently
# ---------------------------------------------------------------------------

class TestMultipleSymbols:

    def setup_method(self):
        _reset_cache()

    def test_independent_storage(self):
        from backend.brokers.broker_apis import record_good_ltp, get_last_good_ltp

        record_good_ltp("RELIANCE", 2850.0)
        record_good_ltp("INFY",    1500.0)
        record_good_ltp("TCS",     3900.0)

        assert get_last_good_ltp("RELIANCE") == pytest.approx(2850.0, abs=0.001)
        assert get_last_good_ltp("INFY")     == pytest.approx(1500.0, abs=0.001)
        assert get_last_good_ltp("TCS")      == pytest.approx(3900.0, abs=0.001)

    def test_update_one_does_not_affect_others(self):
        from backend.brokers.broker_apis import record_good_ltp, get_last_good_ltp

        record_good_ltp("RELIANCE", 2850.0)
        record_good_ltp("INFY",    1500.0)

        record_good_ltp("RELIANCE", 2900.0)  # update RELIANCE only

        assert get_last_good_ltp("RELIANCE") == pytest.approx(2900.0, abs=0.001), \
            "RELIANCE must reflect the updated value"
        assert get_last_good_ltp("INFY") == pytest.approx(1500.0, abs=0.001), \
            "INFY must be unchanged"


# ---------------------------------------------------------------------------
# 4. Concurrent record + fetch — lock correctness
# ---------------------------------------------------------------------------

class TestConcurrentAccess:

    def setup_method(self):
        _reset_cache()

    def test_concurrent_writes_no_data_corruption(self):
        """100 threads each write a distinct symbol; all values must be
        readable afterwards without any KeyError or data corruption."""
        from backend.brokers.broker_apis import record_good_ltp, get_last_good_ltp

        n = 100
        symbols = [f"SYM{i:03d}" for i in range(n)]
        prices  = [float(1000 + i) for i in range(n)]
        errors: list[str] = []

        def _write(sym, price):
            try:
                record_good_ltp(sym, price)
            except Exception as e:
                errors.append(f"{sym}: {e}")

        threads = [threading.Thread(target=_write, args=(s, p))
                   for s, p in zip(symbols, prices)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors during concurrent writes: {errors}"
        for sym, price in zip(symbols, prices):
            result = get_last_good_ltp(sym)
            assert result == pytest.approx(price, abs=0.001), \
                f"{sym}: expected {price}, got {result}"

    def test_concurrent_read_write_no_crash(self):
        """One writer thread records repeatedly while 10 reader threads
        fetch concurrently. Must not raise any exception."""
        from backend.brokers.broker_apis import record_good_ltp, get_last_good_ltp

        errors: list[str] = []

        def _writer():
            for i in range(200):
                try:
                    record_good_ltp("CONCURRENT_SYM", float(1000 + i))
                except Exception as e:
                    errors.append(f"writer: {e}")

        def _reader():
            for _ in range(200):
                try:
                    get_last_good_ltp("CONCURRENT_SYM")
                except Exception as e:
                    errors.append(f"reader: {e}")

        threads = ([threading.Thread(target=_writer)]
                   + [threading.Thread(target=_reader) for _ in range(10)])
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors during concurrent read/write: {errors}"


# ---------------------------------------------------------------------------
# 5. Integration: all broker + ticker paths fail → last-good used, not 0
# ---------------------------------------------------------------------------

class TestLastGoodFallbackIntegration:
    """When PriceBroker.quote raises AND KiteTicker.get_ltp_by_sym returns
    None, the position / holding row must NOT be 0 — it must use the
    last-known-good value previously recorded in the cache."""

    def setup_method(self):
        _reset_cache()

    def _pos_df(self, last_price: float = 0.0) -> "pd.DataFrame":
        import pandas as pd
        return pd.DataFrame([{
            'tradingsymbol': 'CRUDEOIL26JUL6900PE',
            'exchange': 'MCX',
            'last_price': last_price,
            'close_price': 220.0,
            'quantity': 10,
            'overnight_quantity': 10,
            'day_buy_quantity': 0,
            'day_sell_quantity': 0,
            'day_buy_value': 0.0,
            'day_sell_value': 0.0,
            'average_price': 200.0,
            'realised': 0.0,
            'pnl': 0.0,
            'day_change_val': 0.0,
            'day_change': 0.0,
        }])

    def _hold_df(self, last_price: float = 0.0) -> "pd.DataFrame":
        import pandas as pd
        return pd.DataFrame([{
            'tradingsymbol': 'GOLDBEES',
            'exchange': 'NSE',
            'last_price': last_price,
            'close_price': 1800.0,
            'opening_quantity': 10,
            'average_price': 1750.0,
            'pnl': 0.0,
            'day_change_val': 0.0,
            'day_change': 0.0,
        }])

    def test_positions_ticker_fallback_uses_last_good(self):
        """After a successful fetch records a good LTP, a subsequent failed
        fetch (ticker=None) must return the cached value, not 0."""
        from backend.brokers.broker_apis import record_good_ltp
        from backend.api.routes.positions import _override_stale_ltp_from_ticker
        from unittest.mock import MagicMock, patch

        # Simulate a prior successful fetch having recorded the LTP.
        record_good_ltp("CRUDEOIL26JUL6900PE", 264.5)

        df = self._pos_df(last_price=0.0)

        mock_ticker = MagicMock()
        mock_ticker.get_ltp_by_sym.return_value = None  # ticker has no data now

        with patch('backend.brokers.kite_ticker.get_ticker', return_value=mock_ticker):
            _override_stale_ltp_from_ticker(df)

        assert float(df.at[0, 'last_price']) == pytest.approx(264.5, abs=0.001), \
            f"Expected last-good LTP 264.5, got {df.at[0, 'last_price']}"
        assert bool(df.at[0, 'last_price_stale']) is True, \
            "Row must be flagged as stale when sourced from last-known-good cache"

    def test_holdings_ticker_fallback_uses_last_good(self):
        """Same scenario for the holdings route."""
        from backend.brokers.broker_apis import record_good_ltp
        from backend.api.routes.holdings import _override_stale_ltp_from_ticker
        from unittest.mock import MagicMock, patch

        record_good_ltp("GOLDBEES", 1870.0)

        df = self._hold_df(last_price=0.0)

        mock_ticker = MagicMock()
        mock_ticker.get_ltp_by_sym.return_value = None

        with patch('backend.brokers.kite_ticker.get_ticker', return_value=mock_ticker):
            _override_stale_ltp_from_ticker(df)

        assert float(df.at[0, 'last_price']) == pytest.approx(1870.0, abs=0.001), \
            f"Expected last-good LTP 1870.0, got {df.at[0, 'last_price']}"
        assert bool(df.at[0, 'last_price_stale']) is True, \
            "Row must be flagged as stale when sourced from last-known-good cache"

    def test_positions_no_prior_cache_stays_zero(self):
        """When there is no prior cached LTP and all sources fail, the row
        must stay at 0 — genuine missing-data, not rescued."""
        from backend.api.routes.positions import _override_stale_ltp_from_ticker
        from unittest.mock import MagicMock, patch

        df = self._pos_df(last_price=0.0)

        mock_ticker = MagicMock()
        mock_ticker.get_ltp_by_sym.return_value = None

        with patch('backend.brokers.kite_ticker.get_ticker', return_value=mock_ticker):
            _override_stale_ltp_from_ticker(df)

        assert float(df.at[0, 'last_price']) == pytest.approx(0.0), \
            "With no cached LTP and no live source, row must stay at 0"
        assert 'last_price_stale' not in df.columns or not bool(df.at[0, 'last_price_stale']), \
            "No stale flag when nothing was rescued"

    def test_holdings_no_prior_cache_stays_zero(self):
        from backend.api.routes.holdings import _override_stale_ltp_from_ticker
        from unittest.mock import MagicMock, patch

        df = self._hold_df(last_price=0.0)

        mock_ticker = MagicMock()
        mock_ticker.get_ltp_by_sym.return_value = None

        with patch('backend.brokers.kite_ticker.get_ticker', return_value=mock_ticker):
            _override_stale_ltp_from_ticker(df)

        assert float(df.at[0, 'last_price']) == pytest.approx(0.0), \
            "With no cached LTP and no live source, row must stay at 0"

    def test_backfill_last_good_marks_stale(self):
        """backfill_market_data must use the last-known-good cache for a
        symbol when both PriceBroker and KiteTicker fail, and must set
        the `last_price_stale` column on that row."""
        from backend.brokers.broker_apis import record_good_ltp, backfill_market_data
        from unittest.mock import patch, MagicMock

        record_good_ltp("CRUDEOIL26JUL6900PE", 264.5)

        import pandas as pd
        df = pd.DataFrame([{
            'tradingsymbol': 'CRUDEOIL26JUL6900PE',
            'exchange': 'MCX',
            'last_price': 0.0,
            'close_price': 220.0,
            'quantity': 10,
            'day_change_val': 0.0,
        }])

        mock_ticker = MagicMock()
        mock_ticker.get_ltp_by_sym.return_value = None  # ticker unavailable

        with patch('backend.brokers.registry.get_price_broker',
                   side_effect=RuntimeError("rate-limited")), \
             patch('backend.brokers.kite_ticker.get_ticker',
                   return_value=mock_ticker):
            backfill_market_data(df)

        assert float(df.at[0, 'last_price']) == pytest.approx(264.5, abs=0.001), \
            f"backfill must rescue from last-good cache, got {df.at[0, 'last_price']}"
        stale_val = df.get('last_price_stale', pd.Series([False])).iloc[0]
        assert bool(stale_val) is True, \
            "backfill must set last_price_stale=True on rescued rows"
