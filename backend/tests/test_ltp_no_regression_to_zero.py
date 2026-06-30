"""Regression contract: LTP must never regress to 0 once positive.

Sleep audit Jun 2026 — definitive LTP-flicker fix. Extends the existing
`test_last_good_ltp.py` coverage by adding WebSocket-ticker and SSE-bus
paths to the zero-LTP guard. The root cause of the operator-visible
flicker was that Kite occasionally emits `last_price: 0` for freshly-
subscribed instruments before the first real trade lands; those zeros
landed in `_tick_map`, were broadcast over SSE, and then poisoned the
frontend symbolStore's per-symbol timestamp arbitration (a 0-stamped-
fresh entry rejected every subsequent positive poll).

Quality dimensions (per project test spec):
  - Correct behaviour (primary — zero-LTP never leaks past ticker)
  - SSOT: same guard at ticker level + last-known-good cache + override paths
  - Performance: pure in-memory; no broker / network calls
  - Stale-code grep: ensures the fix in `_on_ticks` stays applied
  - Reusable-component usage: covers `kite_ticker.TickerManager` directly

Five regression checks:
  1. `_on_ticks` drops `lp == 0` frames before writing `_tick_map`
  2. `_on_ticks` drops `lp < 0` frames (negative is also invalid)
  3. `_on_ticks` does NOT publish 0-LTP frames to the SSE bus
  4. `snapshot()` filters non-positive entries (belt + suspenders)
  5. After a positive write, a subsequent 0-frame must NOT overwrite
     the positive value (the original flicker scenario in the field)
"""

from __future__ import annotations

from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Helpers — direct construction of a TickerManager without starting the
# WebSocket. The unit under test is the _on_ticks callback semantics.
# ---------------------------------------------------------------------------


def _fresh_ticker():
    """Build a minimal TickerManager instance with no live KiteTicker.

    `_on_ticks` and `snapshot` only touch self._lock, self._tick_map,
    self._tick_age, self._token_to_sym, self._tick_buffer, and self._bus.
    The bus is a real BroadcastBus (no-op without subscribers); the
    tick_buffer is stubbed to None so we don't need /dev/shm.
    """
    from backend.brokers.kite_ticker import TickerManager
    tm = TickerManager()
    # Pre-seed token → sym map so the published payloads carry a sym
    # (mirrors what subscribe_with_sym() would populate at runtime).
    tm._token_to_sym = {12345: "RELIANCE", 67890: "INFY", 11111: "CRUDEOILX"}
    return tm


def _capture_bus_publishes(tm):
    """Replace tm._bus.publish with a list-appending stub and return the
    list so the test can assert which payloads were published."""
    published: list = []
    tm._bus.publish = lambda payload: published.append(payload)
    return published


# ---------------------------------------------------------------------------
# 1. Zero-LTP guard — single tick frame
# ---------------------------------------------------------------------------


class TestTickerOnTicksZeroGuard:

    def test_zero_ltp_not_written_to_tick_map(self):
        tm = _fresh_ticker()
        _capture_bus_publishes(tm)

        tm._on_ticks(None, [{"instrument_token": 12345, "last_price": 0}])

        assert 12345 not in tm._tick_map, \
            "Zero LTP must NOT land in _tick_map"

    def test_negative_ltp_not_written_to_tick_map(self):
        tm = _fresh_ticker()
        _capture_bus_publishes(tm)

        tm._on_ticks(None, [{"instrument_token": 12345, "last_price": -10.5}])

        assert 12345 not in tm._tick_map, \
            "Negative LTP must NOT land in _tick_map"

    def test_positive_ltp_is_written(self):
        tm = _fresh_ticker()
        _capture_bus_publishes(tm)

        tm._on_ticks(None, [{"instrument_token": 12345, "last_price": 2850.50}])

        assert tm._tick_map.get(12345) == 2850.50

    def test_zero_ltp_not_published_to_bus(self):
        tm = _fresh_ticker()
        published = _capture_bus_publishes(tm)

        tm._on_ticks(None, [
            {"instrument_token": 12345, "last_price": 0},
            {"instrument_token": 67890, "last_price": 1500.0},
        ])

        # Only the positive INFY tick should have been published.
        syms = [p["sym"] for p in published]
        assert "RELIANCE" not in syms, \
            "Zero-LTP RELIANCE tick must NOT be broadcast over SSE bus"
        assert "INFY" in syms, \
            "Positive INFY tick must be broadcast"

    def test_mixed_frame_filters_only_zeros(self):
        """A real Kite tick frame can carry 50+ tokens — make sure the
        zero-guard filters PER-TICK, not all-or-nothing."""
        tm = _fresh_ticker()
        published = _capture_bus_publishes(tm)

        tm._on_ticks(None, [
            {"instrument_token": 12345, "last_price": 2850.50},
            {"instrument_token": 67890, "last_price": 0},          # drop
            {"instrument_token": 11111, "last_price": 6200.0},
        ])

        assert tm._tick_map == {12345: 2850.50, 11111: 6200.0}
        assert len(published) == 2
        published_toks = sorted(p["tok"] for p in published)
        assert published_toks == [11111, 12345]

    def test_missing_fields_skipped(self):
        """Defence against malformed Kite payloads — no token or no
        last_price means we skip the tick silently."""
        tm = _fresh_ticker()
        _capture_bus_publishes(tm)

        tm._on_ticks(None, [
            {"last_price": 100.0},                       # no token
            {"instrument_token": 12345},                 # no last_price
            {"instrument_token": 67890, "last_price": None},  # None LP
        ])

        assert tm._tick_map == {}, \
            "No malformed payload should land in _tick_map"


# ---------------------------------------------------------------------------
# 2. Flicker scenario — positive then 0 must NOT overwrite
# ---------------------------------------------------------------------------


class TestFlickerScenario:
    """The operator-visible flicker pattern:
       T0 — Kite emits last_price=2850 — cell shows ₹2850
       T1 — Kite emits last_price=0 (cold sub frame / boundary glitch)
       T2 — Cell flickers to 0 / "—" — OPERATOR ESCALATION

    The fix at `_on_ticks` drops the T1 frame so the cell stays at ₹2850.
    """

    def test_positive_then_zero_preserves_positive(self):
        tm = _fresh_ticker()
        _capture_bus_publishes(tm)

        # T0 — positive tick
        tm._on_ticks(None, [{"instrument_token": 12345, "last_price": 2850.0}])
        # T1 — flicker-frame
        tm._on_ticks(None, [{"instrument_token": 12345, "last_price": 0}])

        assert tm._tick_map.get(12345) == 2850.0, \
            "T1 zero frame must NOT overwrite T0 positive LTP"

    def test_get_ltp_by_sym_returns_positive_after_zero_frame(self):
        tm = _fresh_ticker()
        _capture_bus_publishes(tm)

        tm._on_ticks(None, [{"instrument_token": 12345, "last_price": 2850.0}])
        tm._on_ticks(None, [{"instrument_token": 12345, "last_price": 0}])
        tm._sym_to_token = {"RELIANCE": 12345}  # subscribe_with_sym() does this

        assert tm.get_ltp_by_sym("RELIANCE") == 2850.0, \
            "get_ltp_by_sym must return the surviving positive value"


# ---------------------------------------------------------------------------
# 3. snapshot() — defensive filter on read
# ---------------------------------------------------------------------------


class TestSnapshotFiltersNonPositive:

    def test_snapshot_excludes_zero_entries_if_any_leaked(self):
        """Belt + suspenders: even if a 0 somehow landed in _tick_map
        (legacy entry from before the fix, or unit-test scaffolding),
        snapshot() must filter it out so new SSE clients never see it
        on their initial-snapshot event."""
        tm = _fresh_ticker()

        # Bypass the _on_ticks guard to plant a legacy 0 directly.
        # This simulates a process state that pre-dates the fix.
        with tm._lock:
            tm._tick_map[12345] = 2850.0
            tm._tick_map[67890] = 0.0       # legacy bad entry
            tm._tick_map[11111] = -100.0    # negative — equally invalid

        snap = tm.snapshot()

        assert 12345 in snap and snap[12345]["ltp"] == 2850.0
        assert 67890 not in snap, "snapshot() must filter 0 entries"
        assert 11111 not in snap, "snapshot() must filter negative entries"


# ---------------------------------------------------------------------------
# 4. End-to-end — ticker-override path on positions never leaks 0 to row
# ---------------------------------------------------------------------------


class TestPositionsRouteNeverEmitsZeroLtp:
    """The positions route layered three guards:
        a) `_override_stale_ltp_from_ticker` only writes from ticker
            when `tick_ltp is not None and tick_ltp > 0`
        b) Otherwise falls back to `_LAST_GOOD_LTP` cache (`> 0` recorded)
        c) Otherwise leaves the row's broker-reported value untouched.

    After the Jun-2026 fix the ticker itself never returns 0, but the
    route-level guards still need to hold against a hypothetical future
    regression upstream. Verify both legs.
    """

    def test_ticker_zero_does_not_overwrite_positive_broker_ltp(self):
        from backend.api.routes.positions import _override_stale_ltp_from_ticker
        from unittest.mock import patch
        import pandas as pd

        df = pd.DataFrame([{
            'tradingsymbol': 'CRUDEOILX',
            'exchange': 'MCX',
            'last_price': 264.5,      # broker provided a real value
            'close_price': 220.0,
            'quantity': 10,
            'overnight_quantity': 10,
            'day_buy_quantity': 0, 'day_sell_quantity': 0,
            'day_buy_value': 0.0,   'day_sell_value': 0.0,
            'average_price': 200.0, 'realised': 0.0,
            'pnl': 644.0,
            'day_change_val': 444.0, 'day_change': 44.5,
        }])

        # Mock ticker returns ZERO — simulates a Kite flicker frame
        # surviving past the source filter for any reason.
        mock_ticker = MagicMock()
        mock_ticker.get_ltp_by_sym.return_value = 0.0

        with patch('backend.brokers.kite_ticker.get_ticker',
                   return_value=mock_ticker):
            _override_stale_ltp_from_ticker(df)

        assert float(df.at[0, 'last_price']) == 264.5, \
            "Ticker 0 must NOT overwrite a positive broker LTP"

    def test_ticker_none_falls_back_to_broker_value(self):
        from backend.api.routes.positions import _override_stale_ltp_from_ticker
        from unittest.mock import patch
        import pandas as pd

        df = pd.DataFrame([{
            'tradingsymbol': 'CRUDEOILX',
            'exchange': 'MCX',
            'last_price': 264.5,      # broker value
            'close_price': 220.0,
            'quantity': 10,
            'overnight_quantity': 10,
            'day_buy_quantity': 0, 'day_sell_quantity': 0,
            'day_buy_value': 0.0,   'day_sell_value': 0.0,
            'average_price': 200.0, 'realised': 0.0,
            'pnl': 644.0,
            'day_change_val': 444.0, 'day_change': 44.5,
        }])

        mock_ticker = MagicMock()
        mock_ticker.get_ltp_by_sym.return_value = None

        with patch('backend.brokers.kite_ticker.get_ticker',
                   return_value=mock_ticker):
            _override_stale_ltp_from_ticker(df)

        assert float(df.at[0, 'last_price']) == 264.5, \
            "Ticker None must leave broker value untouched"


# ---------------------------------------------------------------------------
# 5. Same coverage on holdings route — symmetry check
# ---------------------------------------------------------------------------


class TestHoldingsRouteNeverEmitsZeroLtp:

    def test_ticker_zero_does_not_overwrite_positive_broker_ltp(self):
        from backend.api.routes.holdings import _override_stale_ltp_from_ticker
        from unittest.mock import patch
        import pandas as pd

        df = pd.DataFrame([{
            'tradingsymbol': 'GOLDBEES',
            'exchange': 'NSE',
            'last_price': 1870.0,
            'close_price': 1800.0,
            'opening_quantity': 10,
            'average_price': 1750.0,
            'pnl': 700.0,
            'day_change_val': 700.0, 'day_change': 70.0,
        }])

        mock_ticker = MagicMock()
        mock_ticker.get_ltp_by_sym.return_value = 0.0

        with patch('backend.brokers.kite_ticker.get_ticker',
                   return_value=mock_ticker):
            _override_stale_ltp_from_ticker(df)

        # Holdings _override_stale_ltp_from_ticker only TOUCHES rows
        # whose last_price is already 0 — but the cache fallback path
        # could still mis-fire if the cache contained a 0 (it can't,
        # by record_good_ltp's `> 0` guard, but the test exercises the
        # full code path defensively).
        assert float(df.at[0, 'last_price']) == 1870.0, \
            "Holdings ticker 0 must NOT overwrite a positive broker LTP"
