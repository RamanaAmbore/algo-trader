"""Tests — last-known-good QUOTE (open/close/volume/oi) cache.

Companion to test_last_good_ltp.py.  Verifies the LKG quote cache in
backend/brokers/broker_apis.py stores and retrieves non-LTP snapshot
fields for /api/quote/batch's closed-hours fast path.

Quality dimensions (per project test spec):
  - SSOT — the cache is the SINGLE source for closed-hours non-LTP fields
  - Data integrity — empty payloads never overwrite; TTL respected;
                     symbols independent
  - Concurrency — threading.Lock covers concurrent record + fetch
  - Reuse — mirrors the LTP cache API so callers use one pattern
  - UX — cache read populates every field the frontend reads (open,
         close, volume, oi, change, change_pct, bid, ask)
"""

import threading

import pytest


def _reset_quote_cache():
    """Clear the module-level _LAST_GOOD_QUOTE dict between tests."""
    from backend.brokers import broker_apis
    with broker_apis._LAST_GOOD_QUOTE_LOCK:
        broker_apis._LAST_GOOD_QUOTE.clear()


# ---------------------------------------------------------------------------
# 1. Record + fetch within TTL
# ---------------------------------------------------------------------------

class TestRecordAndFetch:

    def setup_method(self):
        _reset_quote_cache()

    def test_record_then_fetch_returns_payload(self):
        from backend.brokers.broker_apis import record_good_quote, get_last_good_quote
        payload = {
            "open": 2540.0, "close": 2532.0, "volume": 1234567, "oi": 0,
            "change": 8.0, "change_pct": 0.316, "bid": 2540.4, "ask": 2540.6,
        }
        record_good_quote("RELIANCE", payload)
        got = get_last_good_quote("RELIANCE")
        assert got is not None
        assert got["open"]   == pytest.approx(2540.0, abs=0.001)
        assert got["close"]  == pytest.approx(2532.0, abs=0.001)
        assert got["volume"] == 1234567
        assert got["oi"]     == 0
        assert got["change"]     == pytest.approx(8.0,   abs=0.001)
        assert got["change_pct"] == pytest.approx(0.316, abs=0.001)
        assert got["bid"] == pytest.approx(2540.4, abs=0.001)
        assert got["ask"] == pytest.approx(2540.6, abs=0.001)

    def test_unknown_symbol_returns_none(self):
        from backend.brokers.broker_apis import get_last_good_quote
        assert get_last_good_quote("NOSUCHSYM") is None

    def test_empty_symbol_returns_none(self):
        from backend.brokers.broker_apis import record_good_quote, get_last_good_quote
        record_good_quote("", {"open": 100.0, "volume": 10})
        assert get_last_good_quote("") is None

    def test_empty_payload_not_stored(self):
        """No meaningful fields (all None/0) → cache is not written."""
        from backend.brokers.broker_apis import record_good_quote, get_last_good_quote
        record_good_quote("RELIANCE", {"open": None, "close": None,
                                        "volume": 0, "oi": 0})
        assert get_last_good_quote("RELIANCE") is None, \
            "Empty payload must not clobber the cache"

    def test_zero_volume_alone_not_stored(self):
        """volume=0 alone without a meaningful other field → not written."""
        from backend.brokers.broker_apis import record_good_quote, get_last_good_quote
        record_good_quote("SYM", {"volume": 0, "oi": 0})
        assert get_last_good_quote("SYM") is None

    def test_partial_payload_stored(self):
        """A payload with one non-null field (e.g. open only) IS stored."""
        from backend.brokers.broker_apis import record_good_quote, get_last_good_quote
        record_good_quote("INFY", {"open": 1500.0})
        got = get_last_good_quote("INFY")
        assert got is not None
        assert got["open"] == pytest.approx(1500.0, abs=0.001)
        assert got["close"]  is None
        assert got["volume"] is None
        assert got["oi"]     is None

    def test_overwrite_with_newer_value(self):
        from backend.brokers.broker_apis import record_good_quote, get_last_good_quote
        record_good_quote("INFY", {"open": 1500.0, "volume": 100})
        record_good_quote("INFY", {"open": 1510.0, "volume": 200})
        got = get_last_good_quote("INFY")
        assert got["open"]   == pytest.approx(1510.0, abs=0.001)
        assert got["volume"] == 200

    def test_returned_dict_is_copy(self):
        """Callers may mutate the returned dict without corrupting cache."""
        from backend.brokers.broker_apis import record_good_quote, get_last_good_quote
        record_good_quote("TCS", {"open": 3900.0, "volume": 500})
        got = get_last_good_quote("TCS")
        got["open"] = 999.9  # mutate the caller's copy
        # Refetch should still return the ORIGINAL cached value.
        assert get_last_good_quote("TCS")["open"] == pytest.approx(3900.0, abs=0.001)


# ---------------------------------------------------------------------------
# 2. TTL expiry
# ---------------------------------------------------------------------------

class TestTtlExpiry:

    def setup_method(self):
        _reset_quote_cache()

    def test_expired_entry_returns_none(self):
        """Entry older than max_age_s must return None."""
        from backend.brokers.broker_apis import (
            record_good_quote, get_last_good_quote, _LAST_GOOD_QUOTE_LOCK,
        )
        import backend.brokers.broker_apis as bapi

        record_good_quote("GOLDM", {"open": 6500.0, "volume": 100})
        # Simulate 25h elapsed
        with _LAST_GOOD_QUOTE_LOCK:
            old_ts, payload = bapi._LAST_GOOD_QUOTE["GOLDM"]
            bapi._LAST_GOOD_QUOTE["GOLDM"] = (old_ts - 90000.0, payload)

        assert get_last_good_quote("GOLDM", max_age_s=86400.0) is None

    def test_not_yet_expired(self):
        """Entry recorded 23h ago (< 24h TTL) is still returned."""
        from backend.brokers.broker_apis import (
            record_good_quote, get_last_good_quote, _LAST_GOOD_QUOTE_LOCK,
        )
        import backend.brokers.broker_apis as bapi

        record_good_quote("SILVER", {"open": 92000.0, "close": 91500.0})
        with _LAST_GOOD_QUOTE_LOCK:
            old_ts, payload = bapi._LAST_GOOD_QUOTE["SILVER"]
            bapi._LAST_GOOD_QUOTE["SILVER"] = (old_ts - 82800.0, payload)  # 23h

        got = get_last_good_quote("SILVER", max_age_s=86400.0)
        assert got is not None
        assert got["open"] == pytest.approx(92000.0, abs=0.001)


# ---------------------------------------------------------------------------
# 3. Multiple symbols independent
# ---------------------------------------------------------------------------

class TestMultipleSymbols:

    def setup_method(self):
        _reset_quote_cache()

    def test_independent_storage(self):
        from backend.brokers.broker_apis import record_good_quote, get_last_good_quote
        record_good_quote("RELIANCE", {"open": 2540.0, "volume": 100})
        record_good_quote("INFY",     {"open": 1500.0, "volume": 200})
        record_good_quote("TCS",      {"open": 3900.0, "volume": 300})

        assert get_last_good_quote("RELIANCE")["open"] == pytest.approx(2540.0, abs=0.001)
        assert get_last_good_quote("INFY")["open"]     == pytest.approx(1500.0, abs=0.001)
        assert get_last_good_quote("TCS")["open"]      == pytest.approx(3900.0, abs=0.001)


# ---------------------------------------------------------------------------
# 4. Concurrency
# ---------------------------------------------------------------------------

class TestConcurrentAccess:

    def setup_method(self):
        _reset_quote_cache()

    def test_concurrent_writes_no_corruption(self):
        from backend.brokers.broker_apis import record_good_quote, get_last_good_quote
        n = 100
        symbols = [f"SYM{i:03d}" for i in range(n)]
        opens   = [float(1000 + i) for i in range(n)]
        errors: list[str] = []

        def _write(sym, o):
            try:
                record_good_quote(sym, {"open": o, "volume": 10})
            except Exception as e:
                errors.append(f"{sym}: {e}")

        threads = [threading.Thread(target=_write, args=(s, o))
                   for s, o in zip(symbols, opens)]
        for t in threads: t.start()
        for t in threads: t.join()

        assert not errors, f"Errors: {errors}"
        for sym, o in zip(symbols, opens):
            got = get_last_good_quote(sym)
            assert got is not None
            assert got["open"] == pytest.approx(o, abs=0.001)


# ---------------------------------------------------------------------------
# 5. End-to-end closed-hours branch integration
# ---------------------------------------------------------------------------

class TestClosedHoursBranchIntegration:
    """Closed-hours fast-path returns open/close/volume/oi from LKG cache
    instead of dropping every non-LTP field to null."""

    def setup_method(self):
        _reset_quote_cache()
        # Clear the LTP cache too so we start clean.
        from backend.brokers import broker_apis
        with broker_apis._LAST_GOOD_LTP_LOCK:
            broker_apis._LAST_GOOD_LTP.clear()

    def test_closed_hours_row_carries_ohlc_volume_oi(self):
        """After a live-path write for RELIANCE, a subsequent closed-hours
        call must return a BatchQuoteRow with the same open/close/volume/oi
        that were recorded — not zeros/nulls.  This is the operator-report
        regression: on a Saturday (market closed), /pulse showed empty
        Open / Vol / OI columns because the closed-hours branch only
        returned {ltp, stale}."""
        from backend.brokers.broker_apis import record_good_ltp, record_good_quote
        record_good_ltp("RELIANCE", 2540.5)
        record_good_quote("RELIANCE", {
            "open": 2530.0, "close": 2532.0, "volume": 1_234_567, "oi": 0,
            "change": 8.5, "change_pct": 0.336,
        })

        # Simulate closed-hours row construction (same logic as
        # /api/quote/batch's `if market_closed:` branch).
        from backend.brokers.broker_apis import get_last_good_ltp, get_last_good_quote
        ltp = get_last_good_ltp("RELIANCE", max_age_s=86400.0) or 0.0
        snap = get_last_good_quote("RELIANCE", max_age_s=86400.0) or {}

        assert ltp             == pytest.approx(2540.5, abs=0.001)
        assert snap["open"]    == pytest.approx(2530.0, abs=0.001)
        assert snap["close"]   == pytest.approx(2532.0, abs=0.001)
        assert snap["volume"]  == 1_234_567
        assert snap["oi"]      == 0
        assert snap["change"]  == pytest.approx(8.5,   abs=0.001)

    def test_closed_hours_no_prior_cache_returns_empty(self):
        """No prior LKG write → closed-hours read returns None for the
        snapshot dict, and the row would carry null open/close/volume/oi.
        (Cold-start warm helper handles this case at the endpoint level;
        this test verifies the cache honestly returns "no data" without
        fabricating values.)"""
        from backend.brokers.broker_apis import get_last_good_quote
        assert get_last_good_quote("UNSEEN_SYMBOL", max_age_s=86400.0) is None
