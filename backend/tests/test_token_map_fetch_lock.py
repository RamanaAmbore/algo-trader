"""Regression test for the instruments download storm / OOM (2026-07-24).

On cold cache (startup / midnight IST rollover), _get_today_token_map released
_TOKEN_MAP_LOCK before calling _qt_broker_token_map.  With 50+ concurrent
asyncio.to_thread calls (from sparkline warm token resolution), ALL threads
simultaneously missed the cache and each downloaded 6 exchanges × all broker
accounts → peak memory 6.4 GB → OOM.

Fix: _TOKEN_MAP_FETCH_LOCK serialises the cold download.  Only ONE thread
downloads; the rest wait and then find the cache populated (double-checked
locking pattern).
"""

import threading
import time
from unittest.mock import MagicMock, patch


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_mock_broker(download_delay: float = 0.05) -> MagicMock:
    """Return a broker mock whose instruments() sleeps to simulate network I/O."""
    broker = MagicMock()
    def _instruments(exchange):
        time.sleep(download_delay)
        return [
            {"tradingsymbol": f"SYM_{exchange}", "instrument_token": 123, "exchange": exchange}
        ]
    broker.instruments.side_effect = _instruments
    return broker


# ── serialisation test ────────────────────────────────────────────────────────

class TestTokenMapFetchLock:
    """_get_today_token_map serialises the cold-cache broker download."""

    def test_only_one_download_on_cold_cache_concurrent_calls(self):
        """With N threads all hitting a cold cache simultaneously, instruments()
        must be called exactly once (not N times) thanks to _TOKEN_MAP_FETCH_LOCK.
        """
        from backend.api.routes.quote import (
            _get_today_token_map,
            _TOKEN_MAP_CACHE,
            _TOKEN_MAP_LOCK,
        )

        # Force cold cache for today.
        with _TOKEN_MAP_LOCK:
            _TOKEN_MAP_CACHE.clear()

        broker = _make_mock_broker(download_delay=0.05)
        call_count = 0
        original_side_effect = broker.instruments.side_effect

        def counting_instruments(exchange):
            nonlocal call_count
            call_count += 1
            return original_side_effect(exchange)

        broker.instruments.side_effect = counting_instruments

        _SPARKLINE_EXCHANGES = ("NSE", "NFO", "BSE", "BFO", "MCX", "CDS")

        with patch("backend.api.routes.quote._SPARKLINE_EXCHANGES", _SPARKLINE_EXCHANGES), \
             patch("backend.api.routes.quote._ist_today", return_value="2099-01-01"), \
             patch("backend.api.routes.quote._qt_read_instr_store_tier1", return_value=None):

            # Clear the 2099-01-01 cache key specifically.
            with _TOKEN_MAP_LOCK:
                _TOKEN_MAP_CACHE.pop("2099-01-01", None)

            results = []
            errors = []

            def worker():
                try:
                    m = _get_today_token_map(broker)
                    results.append(m)
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=worker) for _ in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        assert not errors, f"Threads raised exceptions: {errors}"
        assert len(results) == 20, "All threads must return a result"

        # With the fetch lock, instruments() should be called exactly once
        # per exchange (6 exchanges) — not 20 × 6 = 120 times.
        assert call_count == len(_SPARKLINE_EXCHANGES), (
            f"Expected {len(_SPARKLINE_EXCHANGES)} broker.instruments() calls "
            f"(serialised), got {call_count} — fetch lock not working"
        )

    def test_cache_populated_after_first_download(self):
        """After the first cold-cache download, subsequent calls return the
        cached result without calling broker.instruments() again.
        """
        from backend.api.routes.quote import (
            _get_today_token_map,
            _TOKEN_MAP_CACHE,
            _TOKEN_MAP_LOCK,
        )

        broker = _make_mock_broker(download_delay=0.01)
        call_count_box = [0]
        original = broker.instruments.side_effect

        def counting(exchange):
            call_count_box[0] += 1
            return original(exchange)

        broker.instruments.side_effect = counting

        with patch("backend.api.routes.quote._ist_today", return_value="2099-01-02"), \
             patch("backend.api.routes.quote._qt_read_instr_store_tier1", return_value=None):

            with _TOKEN_MAP_LOCK:
                _TOKEN_MAP_CACHE.pop("2099-01-02", None)

            _get_today_token_map(broker)
            calls_after_first = call_count_box[0]

            # Second call — cache should be warm.
            _get_today_token_map(broker)
            calls_after_second = call_count_box[0]

        assert calls_after_second == calls_after_first, (
            "Second call should hit the cache — no additional broker.instruments() calls"
        )

    def test_instr_store_tier1_short_circuits_broker_download(self):
        """When instruments_store Tier-1 is warm, the broker is never called."""
        from backend.api.routes.quote import (
            _get_today_token_map,
            _TOKEN_MAP_CACHE,
            _TOKEN_MAP_LOCK,
        )

        broker = MagicMock()
        warm_map = {("NIFTY", "NSE"): 256265}

        with patch("backend.api.routes.quote._ist_today", return_value="2099-01-03"), \
             patch("backend.api.routes.quote._qt_read_instr_store_tier1", return_value=warm_map):

            with _TOKEN_MAP_LOCK:
                _TOKEN_MAP_CACHE.pop("2099-01-03", None)

            result = _get_today_token_map(broker)

        broker.instruments.assert_not_called()
        assert result == warm_map
