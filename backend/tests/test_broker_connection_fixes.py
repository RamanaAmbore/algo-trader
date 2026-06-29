"""
Regression tests for broker connection issues addressed in Jun 2026.

Issues covered:
  A. Kite rate-limit burst — sparkline warm semaphore concurrency
  B. Dhan DH3747 token dead / re-login loop (config-only fix, partial test)
  C. Dhan instruments CSV 404 — new URL + new schema parsing
  D. PriceBroker unresolved symbols — both Kite accounts rate-limited simultaneously
  E. db_worker intraday date type — str vs datetime.date causing 'toordinal' error

Each test asserts one of the five quality dimensions per project convention:
  SSOT, performance budget, stale-code grep, reusable-component usage, UX color
  consistency. For broker tests the dominant dimensions are SSOT (single code
  path) and correctness (no silent data loss on known failure modes).
"""

from __future__ import annotations

import datetime
import threading
import time
from unittest.mock import MagicMock, patch

import pytest


# ── A. Kite rate-limit cool-off ──────────────────────────────────────────────

class TestRateLimitCooloffDuration:
    """Cool-off is now 60s — long enough for both Kite accounts to clear their
    429 window independently without the second account being hit immediately
    after the first account's 30s cool-off expired."""

    def test_cooloff_is_60s(self):
        from backend.brokers.registry import _RATE_LIMIT_COOLOFF_SECONDS
        assert _RATE_LIMIT_COOLOFF_SECONDS == 60, (
            f"Cool-off should be 60s (was 30s). Got {_RATE_LIMIT_COOLOFF_SECONDS}."
        )

    def test_mark_rate_limited_uses_60s_window(self):
        from backend.brokers.registry import (
            _mark_rate_limited, _RATE_LIMIT_COOLOFF, _RATE_LIMIT_LOCK,
            _RATE_LIMIT_COOLOFF_SECONDS,
        )
        with _RATE_LIMIT_LOCK:
            _RATE_LIMIT_COOLOFF.clear()
        now = time.time()
        _mark_rate_limited("kite/ZG0790")
        expires = _RATE_LIMIT_COOLOFF.get("kite/ZG0790", 0.0)
        # Must be at least 59s from now (60 - 1 tolerance for execution time)
        assert expires >= now + 59, (
            f"Expected expiry ≥ now+59, got {expires - now:.1f}s from now"
        )
        # Must not be absurdly far (e.g., > 300s)
        assert expires <= now + _RATE_LIMIT_COOLOFF_SECONDS + 5

    def test_two_accounts_both_marked_independently(self):
        """Marking ZG0790 rate-limited must not affect ZJ6294 and vice-versa."""
        from backend.brokers.registry import (
            _mark_rate_limited, _is_rate_limited,
            _RATE_LIMIT_COOLOFF, _RATE_LIMIT_LOCK,
        )
        with _RATE_LIMIT_LOCK:
            _RATE_LIMIT_COOLOFF.clear()

        _mark_rate_limited("kite/ZG0790")
        assert _is_rate_limited("kite/ZG0790") is True
        # Other account must NOT be marked
        assert _is_rate_limited("kite/ZJ6294") is False, (
            "Marking ZG0790 must not affect ZJ6294"
        )

    def test_price_broker_falls_through_to_second_when_first_rate_limited(self):
        """When the first broker is rate-limited, _try falls through to the second."""
        from backend.brokers.registry import (
            PriceBroker, _mark_rate_limited, _RATE_LIMIT_COOLOFF, _RATE_LIMIT_LOCK,
        )
        with _RATE_LIMIT_LOCK:
            _RATE_LIMIT_COOLOFF.clear()

        b1 = MagicMock()
        b1.broker_id = "zerodha_kite"
        b1.account = "ZG0790"
        b1.quote = MagicMock(return_value={"NSE:RELIANCE": {"last_price": 2500.0}})

        b2 = MagicMock()
        b2.broker_id = "zerodha_kite"
        b2.account = "ZJ6294"
        b2.quote = MagicMock(return_value={"NSE:RELIANCE": {"last_price": 2500.0}})

        pb = PriceBroker([b1, b2])
        # Mark b1 rate-limited
        _mark_rate_limited("zerodha_kite/ZG0790")

        result = pb.quote(["NSE:RELIANCE"])
        # b1.quote must NOT have been called (it was rate-limited)
        b1.quote.assert_not_called()
        # b2.quote must have been called
        b2.quote.assert_called_once()
        assert result.get("NSE:RELIANCE", {}).get("last_price") == 2500.0


# ── B. Dhan DH3747 — token dead + re-login (partial, no real credentials) ───

class TestDhanTokenRateLimit:
    """Confirm that the re-login rate-limit guard prevents hammering Dhan's
    2-min auth endpoint. The guard is in DhanConnection.get_dhan_conn — when
    _login_blocked_until is in the future and test_conn=True, it raises
    RuntimeError immediately instead of calling _do_login."""

    def test_login_blocked_raises_immediately_when_token_dead(self):
        """When _login_blocked_until is set and test_conn=True, the connection
        must raise RuntimeError immediately (not retry _do_login)."""
        from backend.brokers.connections import DhanConnection

        conn = DhanConnection.__new__(DhanConnection)
        conn.account = "DH3747"
        conn._login_lock = threading.Lock()
        conn._login_blocked_until = time.time() + 100.0  # blocked for 100s
        conn._access_token = "dead_token"
        conn._conn_created_at = None
        conn._dhan = MagicMock()

        with pytest.raises(RuntimeError, match="rate-limited"):
            conn.get_dhan_conn(test_conn=True)

    def test_login_not_blocked_returns_client_when_test_conn_false(self):
        """When the cool-off is active but test_conn=False (routine poll),
        the cached client is returned so position/holdings fetches still work."""
        from backend.brokers.connections import DhanConnection

        mock_sdk = MagicMock()
        conn = DhanConnection.__new__(DhanConnection)
        conn.account = "DH3747"
        conn._login_lock = threading.Lock()
        conn._login_blocked_until = time.time() + 100.0
        conn._access_token = "cached_token"
        conn._conn_created_at = datetime.datetime.now(datetime.timezone.utc)
        conn._dhan = mock_sdk

        result = conn.get_dhan_conn(test_conn=False)
        assert result is mock_sdk, "Should return the cached SDK handle"


# ── C. Dhan instruments — new CSV URL + schema ───────────────────────────────

class TestDhanInstrumentsUrl:
    """The instruments URL changed to images.dhan.co/api-data/api-scrip-master.csv.
    The module constant must reflect the new URL."""

    def test_instruments_url_is_images_dhan(self):
        from backend.brokers.adapters.dhan import _DHAN_INSTRUMENTS_URL
        assert "images.dhan.co" in _DHAN_INSTRUMENTS_URL, (
            f"URL must point to images.dhan.co (new location). Got: {_DHAN_INSTRUMENTS_URL}"
        )
        assert "api-scrip-master.csv" in _DHAN_INSTRUMENTS_URL


class TestDhanInstrumentsNewSchema:
    """New CSV schema: SEM_EXM_EXCH_ID carries 'NSE'/'BSE'/'MCX', SEM_SEGMENT
    carries 'D'/'M'/'E'/'C'/'I'. _load_dhan_instruments must resolve both into
    the correct Kite exchange string."""

    def _make_csv_new_schema(self, rows: list[tuple]) -> str:
        """Build a minimal new-schema CSV from (exch, seg, sid, sym, lot, tick) tuples."""
        header = ("SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,"
                  "SEM_TRADING_SYMBOL,SEM_LOT_UNITS,SEM_TICK_SIZE,SM_SYMBOL_NAME")
        lines = [header]
        for exch, seg, sid, sym, lot, tick in rows:
            lines.append(f"{exch},{seg},{sid},{sym},{lot},{tick},TESTNAME")
        return "\n".join(lines)

    def _run_load(self, csv_text: str):
        """Patch urlopen to return csv_text, then call _load_dhan_instruments."""
        from backend.brokers.adapters import dhan as _dhan_mod
        # Clear existing cache so the load runs unconditionally.
        _dhan_mod._DHAN_INSTRUMENTS_DATE = ""
        _dhan_mod._DHAN_BY_EXCHANGE = {}
        _dhan_mod._DHAN_BY_SYMBOL = {}

        from io import BytesIO
        from unittest.mock import patch as _patch, MagicMock

        mock_resp = MagicMock()
        mock_resp.read.return_value = csv_text.encode("utf-8")
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with _patch("backend.brokers.adapters.dhan.urlopen", return_value=mock_resp):
            _dhan_mod._load_dhan_instruments()

        return _dhan_mod._DHAN_BY_EXCHANGE, _dhan_mod._DHAN_BY_SYMBOL

    def test_nse_equity_resolves_to_nse(self):
        csv = self._make_csv_new_schema([("NSE", "E", "100", "RELIANCE", "1", "0.05")])
        by_exch, by_sym = self._run_load(csv)
        assert "NSE" in by_exch, "NSE_E should map to Kite exchange 'NSE'"
        assert ("NSE", "RELIANCE") in by_sym

    def test_nse_derivatives_resolves_to_nfo(self):
        # New format: no day-prefix in equity options (ROOT-MonYYYY-STRIKE-CE|PE)
        csv = self._make_csv_new_schema([
            ("NSE", "D", "200", "RELIANCE-Jun2026-2900-CE", "250", "0.05"),
        ])
        by_exch, by_sym = self._run_load(csv)
        assert "NFO" in by_exch, "NSE_D should map to Kite exchange 'NFO'"
        # "RELIANCE-Jun2026-2900-CE" → no day prefix → "RELIANCE26JUN2900CE"
        sym_key = ("NFO", "RELIANCE26JUN2900CE")
        assert sym_key in by_sym, (
            f"Expected key {sym_key} in by_sym. Got: {list(by_sym.keys())}"
        )

    def test_mcx_commodity_resolves_to_mcx(self):
        csv = self._make_csv_new_schema([
            ("MCX", "M", "300", "CRUDEOIL-10Aug2026-6150-CE", "100", "1.0"),
        ])
        by_exch, by_sym = self._run_load(csv)
        assert "MCX" in by_exch, "MCX_M should map to Kite exchange 'MCX'"

    def test_lot_size_read_from_sem_lot_units(self):
        """SEM_LOT_UNITS (new schema) must be read as lot_size."""
        csv = self._make_csv_new_schema([("NSE", "D", "400", "NIFTY-26Jun2026-23500-CE", "50", "0.05")])
        by_exch, _ = self._run_load(csv)
        nfo_rows = by_exch.get("NFO", [])
        assert nfo_rows, "Expected NFO rows"
        assert nfo_rows[0]["lot_size"] == 50, (
            f"lot_size should be 50, got {nfo_rows[0]['lot_size']}"
        )

    def test_unknown_segment_skipped_silently(self):
        """Rows with an unmapped (exchange, segment) combo are skipped without error."""
        csv = self._make_csv_new_schema([
            ("NSE", "X", "999", "UNKNOWN", "1", "0.05"),  # 'X' is not in the map
        ])
        by_exch, by_sym = self._run_load(csv)
        # No rows should land in any exchange
        total = sum(len(v) for v in by_exch.values())
        assert total == 0, f"Unknown segment rows should be dropped, got {total}"


class TestDhanExchSegMapping:
    """Unit-test the _DHAN_EXCH_SEG_TO_EXCHANGE constant directly."""

    def test_all_required_mappings_present(self):
        from backend.brokers.adapters.dhan import _DHAN_EXCH_SEG_TO_EXCHANGE
        required = {
            ("NSE", "E"): "NSE",
            ("BSE", "E"): "BSE",
            ("NSE", "D"): "NFO",
            ("BSE", "D"): "BFO",
            ("MCX", "M"): "MCX",
            ("NSE", "C"): "CDS",
        }
        for key, expected in required.items():
            got = _DHAN_EXCH_SEG_TO_EXCHANGE.get(key)
            assert got == expected, (
                f"_DHAN_EXCH_SEG_TO_EXCHANGE[{key}] = {got!r}, expected {expected!r}"
            )


# ── D. PriceBroker unresolved symbols (both Kite accounts rate-limited) ──────

class TestPriceBrokerAllRateLimited:
    """When all brokers in the PriceBroker chain are rate-limited, _try
    raises the last RuntimeError so the caller gets a real diagnostic."""

    def test_raises_when_all_rate_limited(self):
        from backend.brokers.registry import (
            PriceBroker, _mark_rate_limited,
            _RATE_LIMIT_COOLOFF, _RATE_LIMIT_LOCK,
        )
        with _RATE_LIMIT_LOCK:
            _RATE_LIMIT_COOLOFF.clear()

        b1 = MagicMock()
        b1.broker_id = "zerodha_kite"
        b1.account = "ZG0790"

        b2 = MagicMock()
        b2.broker_id = "zerodha_kite"
        b2.account = "ZJ6294"

        pb = PriceBroker([b1, b2])
        _mark_rate_limited("zerodha_kite/ZG0790")
        _mark_rate_limited("zerodha_kite/ZJ6294")

        with pytest.raises(RuntimeError):
            pb.quote(["NSE:RELIANCE"])

        # Neither broker's quote method should have been called
        b1.quote.assert_not_called()
        b2.quote.assert_not_called()

    def test_recovers_after_cooloff_expires(self):
        """After the rate-limit expires, the broker is used again."""
        from backend.brokers.registry import (
            PriceBroker, _mark_rate_limited, _is_rate_limited,
            _RATE_LIMIT_COOLOFF, _RATE_LIMIT_LOCK,
        )
        with _RATE_LIMIT_LOCK:
            _RATE_LIMIT_COOLOFF.clear()

        b1 = MagicMock()
        b1.broker_id = "zerodha_kite"
        b1.account = "ZG0790"
        b1.quote = MagicMock(return_value={"NSE:RELIANCE": {"last_price": 2500.0}})

        pb = PriceBroker([b1])

        # Force expiry into the past
        with _RATE_LIMIT_LOCK:
            _RATE_LIMIT_COOLOFF["zerodha_kite/ZG0790"] = time.time() - 1.0

        # Should NOT be rate-limited (expired)
        assert _is_rate_limited("zerodha_kite/ZG0790") is False
        result = pb.quote(["NSE:RELIANCE"])
        assert result.get("NSE:RELIANCE", {}).get("last_price") == 2500.0


# ── E. db_worker intraday date type fix ──────────────────────────────────────

class TestDbWorkerIntradayDateType:
    """_coalesce_intraday must convert the date string to datetime.date so
    asyncpg doesn't raise 'str' object has no attribute 'toordinal'."""

    def _make_payload(self, date_val) -> dict:
        return {
            "kind":     "intraday_bars",
            "symbol":   "RELIANCE",
            "exchange": "NSE",
            "date":     date_val,
            "interval": "30minute",
            "bars": [
                {"bar_ts": "2026-06-29T04:00:00+00:00",
                 "open": 2500.0, "high": 2510.0, "low": 2490.0,
                 "close": 2505.0, "volume": 10000},
            ],
        }

    def test_date_string_is_converted_to_date_object(self):
        from backend.api.persistence.db_worker import _coalesce_intraday
        rows = _coalesce_intraday([self._make_payload("2026-06-29")])
        assert len(rows) == 1
        d = rows[0]["date"]
        assert isinstance(d, datetime.date), (
            f"Expected datetime.date, got {type(d).__name__}: {d!r}"
        )
        assert d == datetime.date(2026, 6, 29)

    def test_date_object_passes_through_unchanged(self):
        from backend.api.persistence.db_worker import _coalesce_intraday
        d_obj = datetime.date(2026, 6, 29)
        rows = _coalesce_intraday([self._make_payload(d_obj)])
        assert len(rows) == 1
        assert rows[0]["date"] == d_obj

    def test_unparseable_date_drops_payload_not_raises(self):
        from backend.api.persistence.db_worker import _coalesce_intraday
        # Should not raise; bad date silently drops the payload
        rows = _coalesce_intraday([self._make_payload("not-a-date")])
        assert rows == [], "Unparseable date should drop the payload"

    def test_multiple_payloads_all_dates_converted(self):
        from backend.api.persistence.db_worker import _coalesce_intraday
        payloads = [
            self._make_payload("2026-06-27"),
            self._make_payload("2026-06-28"),
            self._make_payload("2026-06-29"),
        ]
        # Give each bar a distinct timestamp so they're not deduplicated
        payloads[0]["bars"][0]["bar_ts"] = "2026-06-27T04:00:00+00:00"
        payloads[1]["bars"][0]["bar_ts"] = "2026-06-28T04:00:00+00:00"
        payloads[2]["bars"][0]["bar_ts"] = "2026-06-29T04:00:00+00:00"

        rows = _coalesce_intraday(payloads)
        assert len(rows) == 3
        for row in rows:
            assert isinstance(row["date"], datetime.date), (
                f"All rows must have datetime.date, got {type(row['date'])}"
            )

    def test_ohlcv_date_conversion_also_produces_date_object(self):
        """Regression: _coalesce_ohlcv already has the fix — assert it still holds."""
        from backend.api.persistence.db_worker import _coalesce_ohlcv
        payload = {
            "symbol":   "NIFTY50",
            "exchange": "NSE",
            "bars": [{"date": "2026-06-29", "open": 24000.0, "high": 24100.0,
                      "low": 23900.0, "close": 24050.0, "volume": 5000000}],
        }
        rows = _coalesce_ohlcv([payload])
        assert len(rows) == 1
        assert isinstance(rows[0]["date"], datetime.date), (
            f"ohlcv date must be datetime.date, got {type(rows[0]['date'])}"
        )
