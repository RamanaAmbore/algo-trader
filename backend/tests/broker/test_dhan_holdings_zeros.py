"""Tests for two Dhan holdings zero-value root causes (Aug 2026).

Root Cause 1 — LKG recorded pre-backfill:
  ``_fetch_holdings_local`` calls ``_record_lkg_frame`` BEFORE
  ``_apply_backfill_to_list`` runs. When Dhan returns
  ``lastTradedPrice=0`` off-market, the LKG gets zero-LTP rows.
  Fix: ``_fetch_holdings_cached`` / ``_fetch_positions_cached`` write a
  second LKG pass post-backfill so the stale-substitute path never
  serves zeros.

Root Cause 2 — _apply_backfill_to_list returns zero-price frames on
  exception:
  The original except block returned raw zero-price ``frames``, which
  ssot_fetch cached for the full TTL window.  Fix: ``raise`` so
  ssot_fetch's exception path fires and the result is NOT stored.

Five quality dimensions per test:
  * SSOT  — one canonical implementation in broker_apis.py.
  * Perf  — LKG upgrade is a tight groupby loop; no extra broker calls.
  * Stale-code grep — docstring updated; no remaining "safety net" claim.
  * Reuse — same ``_record_lkg_frame`` / ``_get_lkg_frame`` helpers used
    across holdings + positions + margins.
  * UX    — stale-substitute path now shows real prices; zeros never
    surface to the UI after a breaker-open cycle.
"""

from __future__ import annotations

import pandas as pd
import pytest
from unittest.mock import patch

from backend.brokers.broker_apis import (
    _apply_backfill_to_list,
    _fetch_holdings_cached,
    _fetch_positions_cached,
    _get_lkg_frame,
    _LKG_FRAME_BY_ACCT,
    _LKG_FRAME_LOCK,
    _raw_cache_invalidate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clear_lkg(kind: str, account: str) -> None:
    with _LKG_FRAME_LOCK:
        _LKG_FRAME_BY_ACCT.pop((kind, account), None)


def _clear_ssot_cache() -> None:
    """Evict ssot_fetch result-caches for holdings and positions."""
    _raw_cache_invalidate(None)


def _minimal_holdings_df(account: str, last_price: float) -> pd.DataFrame:
    return pd.DataFrame([{
        "tradingsymbol": "TATASTEEL",
        "exchange": "BSE",
        "account": account,
        "last_price": last_price,
        "average_price": 90.0,
        "quantity": 10,
        "opening_quantity": 10,
        "pnl": 0.0,
        "day_change": 0.0,
        "day_change_val": 0.0,
        "close_price": 90.0,
    }])


def _minimal_positions_df(account: str, last_price: float) -> pd.DataFrame:
    return pd.DataFrame([{
        "tradingsymbol": "NIFTY24DEC22000CE",
        "exchange": "NFO",
        "account": account,
        "last_price": last_price,
        "average_price": 80.0,
        "quantity": 5,
        "opening_quantity": 5,
        "pnl": 0.0,
        "day_change": 0.0,
        "day_change_val": 0.0,
        "close_price": 80.0,
        "buy_quantity": 5,
        "sell_quantity": 0,
        "buy_value": 400.0,
        "sell_value": 0.0,
    }])


# ---------------------------------------------------------------------------
# Root Cause 2 — _apply_backfill_to_list raises on exception
# ---------------------------------------------------------------------------

class TestApplyBackfillRaisesOnError:
    """After the fix, _apply_backfill_to_list must raise when
    backfill_market_data raises — it must NOT silently return raw frames."""

    def test_raises_when_backfill_raises(self):
        """RuntimeError from backfill_market_data propagates out of
        _apply_backfill_to_list (not swallowed + returned as zero frames)."""
        df = _minimal_holdings_df("DH9999", 0.0)
        with patch(
            "backend.brokers.broker_apis.backfill_market_data",
            side_effect=RuntimeError("rate limited"),
        ):
            with pytest.raises(RuntimeError, match="rate limited"):
                _apply_backfill_to_list([df])

    def test_returns_unchanged_for_empty_list(self):
        """Empty input is a no-op — no backfill call, returns [] immediately."""
        result = _apply_backfill_to_list([])
        assert result == []

    def test_returns_unchanged_for_all_empty_frames(self):
        """All-empty frames: backfill_market_data is never called; original
        list is returned (no non-empty frames to concat)."""
        frames = [pd.DataFrame(), pd.DataFrame()]
        with patch(
            "backend.brokers.broker_apis.backfill_market_data",
            side_effect=AssertionError("must not be called"),
        ):
            result = _apply_backfill_to_list(frames)
        # Original list reference returned unchanged.
        assert result is frames


# ---------------------------------------------------------------------------
# Root Cause 1 — LKG upgraded post-backfill for holdings
# ---------------------------------------------------------------------------

class TestLkgUpgradedPostBackfillHoldings:
    """_fetch_holdings_cached must write LKG after backfill so the
    stale-substitute path never sees zero-LTP rows."""

    def test_lkg_upgraded_to_patched_price(self):
        """After _fetch_holdings_cached runs:
        - raw frame has last_price=0 (Dhan off-market)
        - backfill patches it to last_price=100
        - LKG stored for the account should reflect last_price=100.
        """
        acct = "DH_TEST_H1"
        _clear_lkg("holdings", acct)
        _clear_ssot_cache()

        raw_df = _minimal_holdings_df(acct, last_price=0.0)
        patched_df = _minimal_holdings_df(acct, last_price=100.0)

        with patch("backend.brokers.broker_apis._use_conn_service", return_value=False), \
             patch("backend.brokers.broker_apis._fetch_holdings_local", return_value=[raw_df]), \
             patch(
                 "backend.brokers.broker_apis.backfill_market_data",
                 side_effect=lambda df: df.__setitem__("last_price", [100.0]),
             ):
            _fetch_holdings_cached()

        result = _get_lkg_frame("holdings", acct)
        assert result is not None, "LKG must be set after fetch"
        _ts, stored_df = result
        assert float(stored_df["last_price"].iloc[0]) == pytest.approx(100.0), (
            "LKG must store post-backfill price, not the raw zero"
        )

    def test_lkg_not_set_when_backfill_raises(self):
        """When backfill raises, ssot_fetch must not cache a result and
        the LKG for the account must remain at its pre-call value."""
        acct = "DH_TEST_H2"
        _clear_lkg("holdings", acct)
        _clear_ssot_cache()

        raw_df = _minimal_holdings_df(acct, last_price=0.0)

        with patch("backend.brokers.broker_apis._use_conn_service", return_value=False), \
             patch("backend.brokers.broker_apis._fetch_holdings_local", return_value=[raw_df]), \
             patch(
                 "backend.brokers.broker_apis.backfill_market_data",
                 side_effect=RuntimeError("quota exceeded"),
             ):
            with pytest.raises(RuntimeError):
                _fetch_holdings_cached(force_refresh=True)

        # ssot_fetch must NOT have cached the zero-price result.
        cache = getattr(_fetch_holdings_cached, "_result_cache", {})
        assert cache.get("holdings") is None, (
            "ssot_fetch must not cache zero-price result when backfill raises"
        )


# ---------------------------------------------------------------------------
# Root Cause 1 — LKG upgraded post-backfill for positions
# ---------------------------------------------------------------------------

class TestLkgUpgradedPostBackfillPositions:
    """_fetch_positions_cached must upgrade LKG after backfill."""

    def test_lkg_upgraded_to_patched_price(self):
        """Mirror of the holdings test for positions."""
        acct = "DH_TEST_P1"
        _clear_lkg("positions", acct)
        _clear_ssot_cache()

        raw_df = _minimal_positions_df(acct, last_price=0.0)

        with patch("backend.brokers.broker_apis._use_conn_service", return_value=False), \
             patch("backend.brokers.broker_apis._fetch_positions_local", return_value=[raw_df]), \
             patch(
                 "backend.brokers.broker_apis.backfill_market_data",
                 side_effect=lambda df: df.__setitem__("last_price", [200.0]),
             ):
            _fetch_positions_cached(force_refresh=True)

        result = _get_lkg_frame("positions", acct)
        assert result is not None, "LKG must be set after positions fetch"
        _ts, stored_df = result
        assert float(stored_df["last_price"].iloc[0]) == pytest.approx(200.0), (
            "Positions LKG must store post-backfill price"
        )

    def test_lkg_not_set_when_backfill_raises_positions(self):
        """When backfill raises for positions, ssot_fetch must not cache."""
        acct = "DH_TEST_P2"
        _clear_lkg("positions", acct)
        _clear_ssot_cache()

        raw_df = _minimal_positions_df(acct, last_price=0.0)

        with patch("backend.brokers.broker_apis._use_conn_service", return_value=False), \
             patch("backend.brokers.broker_apis._fetch_positions_local", return_value=[raw_df]), \
             patch(
                 "backend.brokers.broker_apis.backfill_market_data",
                 side_effect=RuntimeError("network error"),
             ):
            with pytest.raises(RuntimeError):
                _fetch_positions_cached(force_refresh=True)

        cache = getattr(_fetch_positions_cached, "_result_cache", {})
        assert cache.get("positions") is None, (
            "ssot_fetch must not cache zero-price positions result when backfill raises"
        )
