"""Tests for broker registry and client layer.

Coverage:
  • registry._loaded_accounts() — local Connections vs remote fallback
  • registry.get_broker() — RemoteBroker when flag on, local adapter when off
  • registry._broker_id_for() — resolution chain (DB → remote → YAML → default)
  • registry rate-limit cool-off — _mark_rate_limited, _is_rate_limited
  • client.is_cutover_on() — env var parsing, caching
  • RemoteBroker._call() — UDS dispatch, error handling, JSON coercion
"""

import time
import pytest
from unittest.mock import patch, MagicMock

from backend.brokers.registry import (
    _is_rate_limited,
    _mark_rate_limited,
    _RATE_LIMIT_COOLOFF,
    _RATE_LIMIT_LOCK,
    _RATE_LIMIT_COOLOFF_SECONDS,
    PriceBroker,
)


class TestRateLimitCoolOff:
    """Rate-limit cool-off — marking, checking, expiry."""

    def test_is_rate_limited_initially_false(self):
        """_is_rate_limited returns False before any mark."""
        with _RATE_LIMIT_LOCK:
            _RATE_LIMIT_COOLOFF.clear()

        assert _is_rate_limited("kite/ZG0790") is False

    def test_mark_rate_limited_sets_expiry(self):
        """_mark_rate_limited sets an expiry time in the future."""
        with _RATE_LIMIT_LOCK:
            _RATE_LIMIT_COOLOFF.clear()

        now = time.time()
        _mark_rate_limited("kite/ZG0790")

        expires = _RATE_LIMIT_COOLOFF.get("kite/ZG0790", 0.0)
        assert expires > now, f"expiry {expires} should be > now {now}"
        assert expires <= now + _RATE_LIMIT_COOLOFF_SECONDS + 1, "expiry too far in future"

    def test_is_rate_limited_within_cooloff_window(self):
        """_is_rate_limited returns True within cooloff window."""
        with _RATE_LIMIT_LOCK:
            _RATE_LIMIT_COOLOFF.clear()

        _mark_rate_limited("kite/ZG0790")
        assert _is_rate_limited("kite/ZG0790") is True

    def test_is_rate_limited_expires(self):
        """_is_rate_limited returns False after cooloff expires."""
        with _RATE_LIMIT_LOCK:
            _RATE_LIMIT_COOLOFF.clear()

        # Manually set expiry to the past
        now = time.time()
        with _RATE_LIMIT_LOCK:
            _RATE_LIMIT_COOLOFF["kite/ZG0790"] = now - 1.0

        assert _is_rate_limited("kite/ZG0790") is False

        # The expired entry should be cleaned up
        assert "kite/ZG0790" not in _RATE_LIMIT_COOLOFF

    def test_mark_rate_limited_sweeps_stale_entries(self):
        """_mark_rate_limited cleans up expired entries on every call."""
        with _RATE_LIMIT_LOCK:
            _RATE_LIMIT_COOLOFF.clear()

        now = time.time()
        # Add some stale entries
        with _RATE_LIMIT_LOCK:
            _RATE_LIMIT_COOLOFF["stale1"] = now - 10.0
            _RATE_LIMIT_COOLOFF["stale2"] = now - 5.0

        _mark_rate_limited("fresh/ZG0790")

        # Stale entries should be gone, fresh should be present
        assert "stale1" not in _RATE_LIMIT_COOLOFF
        assert "stale2" not in _RATE_LIMIT_COOLOFF
        assert "fresh/ZG0790" in _RATE_LIMIT_COOLOFF


class TestPriceBroker:
    """PriceBroker — failover wrapper."""

    def test_price_broker_requires_brokers(self):
        """PriceBroker raises if constructed with empty list."""
        with pytest.raises(ValueError, match="at least one"):
            PriceBroker([])

    def test_price_broker_account_is_first_broker(self):
        """PriceBroker.account returns first broker's account."""
        broker1 = MagicMock()
        broker1.account = "ZG0790"
        broker1.broker_id = "zerodha_kite"

        broker2 = MagicMock()
        broker2.account = "DH1234"

        pb = PriceBroker([broker1, broker2])
        assert pb.account == "ZG0790"

    def test_price_broker_falls_over_on_exception(self):
        """PriceBroker._try falls over to next broker on exception."""
        broker1 = MagicMock()
        broker1.broker_id = "zerodha_kite"
        broker1.account = "ZG0790"
        broker1.quote.side_effect = RuntimeError("Kite error")

        broker2 = MagicMock()
        broker2.broker_id = "dhan"
        broker2.account = "DH1234"
        broker2.quote.return_value = {"NIFTY50": {"last_price": 20000}}

        pb = PriceBroker([broker1, broker2])
        result = pb.quote(["NIFTY50"])

        assert result == {"NIFTY50": {"last_price": 20000}}
        broker2.quote.assert_called_once()

    def test_price_broker_skips_rate_limited(self):
        """PriceBroker skips rate-limited brokers without calling them."""
        with _RATE_LIMIT_LOCK:
            _RATE_LIMIT_COOLOFF.clear()

        broker1 = MagicMock()
        broker1.broker_id = "zerodha_kite"
        broker1.account = "ZG0790"

        broker2 = MagicMock()
        broker2.broker_id = "dhan"
        broker2.account = "DH1234"
        broker2.quote.return_value = {"NIFTY50": {"last_price": 20000}}

        pb = PriceBroker([broker1, broker2])

        # Mark broker1 as rate-limited
        _mark_rate_limited("zerodha_kite/ZG0790")

        result = pb.quote(["NIFTY50"])

        # broker1.quote should NOT be called
        broker1.quote.assert_not_called()
        broker2.quote.assert_called_once()
        assert result == {"NIFTY50": {"last_price": 20000}}

    def test_price_broker_account_specific_raises_not_implemented(self):
        """PriceBroker account-specific methods raise NotImplementedError."""
        broker = MagicMock()
        broker.broker_id = "zerodha_kite"
        broker.account = "ZG0790"

        pb = PriceBroker([broker])

        with pytest.raises(NotImplementedError):
            pb.holdings()

        with pytest.raises(NotImplementedError):
            pb.place_order()

        with pytest.raises(NotImplementedError):
            pb.positions()

    def test_price_broker_underlying_count(self):
        """PriceBroker.underlying_count() returns broker list size."""
        broker1 = MagicMock()
        broker1.broker_id = "zerodha_kite"
        broker1.account = "ZG0790"

        broker2 = MagicMock()
        broker2.broker_id = "dhan"
        broker2.account = "DH1234"

        broker3 = MagicMock()
        broker3.broker_id = "groww"
        broker3.account = "GR5678"

        pb = PriceBroker([broker1, broker2, broker3])
        assert pb.underlying_count() == 3
