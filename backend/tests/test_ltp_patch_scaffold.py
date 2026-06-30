"""Tier 2 / A3 — Tests for the LTP-patch scaffold.

`backend/api/helpers/ltp_patch.py` owns the common bookkeeping (ticker
pull, last-known-good fallback, stale-flag write) used by both
positions.py and holdings.py. Each route passes its own policy
callback to govern per-row "should I patch this?" decisions.

Asserts:
  1. SSOT — only one module owns the ticker-pull + LKG-fallback loop
     (grep guard: route files no longer iterate raw.index for LTP
     patching).
  2. Built-in policies behave correctly (positions vs holdings).
  3. Apply_ltp_patch integrates ticker + cache + stale flag end-to-end.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from backend.api.helpers.ltp_patch import (
    apply_ltp_patch,
    holdings_policy,
    positions_policy,
    Decision,
)


# ---------------------------------------------------------------------------
# Built-in policies — unit tests
# ---------------------------------------------------------------------------

class TestPositionsPolicy:
    def test_fresh_tick_above_epsilon_patches(self):
        d = positions_policy(current=100.0, tick_ltp=105.0)
        assert d.new_ltp == 105.0
        assert d.consider_cache is False

    def test_fresh_tick_within_epsilon_skips(self):
        d = positions_policy(current=100.0, tick_ltp=100.003)
        assert d.new_ltp is None
        assert d.consider_cache is False

    def test_no_tick_zero_broker_asks_for_cache(self):
        d = positions_policy(current=0.0, tick_ltp=None)
        assert d.new_ltp is None
        assert d.consider_cache is True

    def test_no_tick_nonzero_broker_skips(self):
        # broker has a valid LTP, no fresh tick — leave it alone
        d = positions_policy(current=100.0, tick_ltp=None)
        assert d.new_ltp is None
        assert d.consider_cache is False


class TestHoldingsPolicy:
    def test_nonzero_broker_never_overwritten(self):
        # Holdings policy never touches a positive broker value even
        # if the ticker has a value.
        d = holdings_policy(current=100.0, tick_ltp=999.0)
        assert d.new_ltp is None
        assert d.consider_cache is False

    def test_zero_broker_uses_fresh_tick(self):
        d = holdings_policy(current=0.0, tick_ltp=42.5)
        assert d.new_ltp == 42.5
        assert d.consider_cache is False

    def test_zero_broker_no_tick_asks_for_cache(self):
        d = holdings_policy(current=0.0, tick_ltp=None)
        assert d.new_ltp is None
        assert d.consider_cache is True


# ---------------------------------------------------------------------------
# Apply_ltp_patch — end-to-end integration
# ---------------------------------------------------------------------------

def _fake_ticker(ltp_map: dict):
    """Build a stub ticker with a get_ltp_by_sym method."""
    t = MagicMock()
    t.get_ltp_by_sym = lambda sym: ltp_map.get(sym)
    return t


class TestApplyLtpPatch:
    def test_empty_df_returns_none(self):
        df = pd.DataFrame()
        with patch("backend.api.helpers.ltp_patch.get_last_good_ltp"), \
             patch("backend.api.helpers.ltp_patch.record_good_ltp"):
            assert apply_ltp_patch(df, positions_policy) is None

    def test_missing_tradingsymbol_returns_none(self):
        df = pd.DataFrame([{'last_price': 0.0}])
        with patch("backend.api.helpers.ltp_patch.get_last_good_ltp"), \
             patch("backend.api.helpers.ltp_patch.record_good_ltp"):
            assert apply_ltp_patch(df, positions_policy) is None

    def test_positions_policy_patches_drift(self):
        df = pd.DataFrame([{
            'tradingsymbol': 'NIFTY26JUL25000CE',
            'last_price': 100.0,
        }])
        ticker = _fake_ticker({'NIFTY26JUL25000CE': 105.0})
        with patch("backend.api.helpers.ltp_patch.record_good_ltp"), \
             patch("backend.brokers.kite_ticker.get_ticker", return_value=ticker):
            res = apply_ltp_patch(df, positions_policy)
        assert res is not None
        assert res.any_patched
        assert df.at[0, 'last_price'] == 105.0
        assert res.stale_idx == []
        assert res.patched_old_ltp[0] == 100.0

    def test_holdings_policy_skips_valid_broker(self):
        df = pd.DataFrame([{
            'tradingsymbol': 'TCS',
            'last_price': 3500.0,
        }])
        ticker = _fake_ticker({'TCS': 9999.0})  # ticker has wildly different
        with patch("backend.api.helpers.ltp_patch.record_good_ltp"), \
             patch("backend.brokers.kite_ticker.get_ticker", return_value=ticker):
            res = apply_ltp_patch(df, holdings_policy)
        assert res is not None
        assert not res.any_patched
        assert df.at[0, 'last_price'] == 3500.0  # untouched

    def test_holdings_policy_patches_zero_from_ticker(self):
        df = pd.DataFrame([{
            'tradingsymbol': 'TCS',
            'last_price': 0.0,
        }])
        ticker = _fake_ticker({'TCS': 3500.0})
        with patch("backend.api.helpers.ltp_patch.record_good_ltp"), \
             patch("backend.brokers.kite_ticker.get_ticker", return_value=ticker):
            res = apply_ltp_patch(df, holdings_policy)
        assert res is not None
        assert res.any_patched
        assert df.at[0, 'last_price'] == 3500.0

    def test_consider_cache_writes_stale_flag(self):
        df = pd.DataFrame([{
            'tradingsymbol': 'TCS',
            'last_price': 0.0,
        }])
        ticker = _fake_ticker({})  # no ticker data
        with patch("backend.api.helpers.ltp_patch.record_good_ltp"), \
             patch("backend.api.helpers.ltp_patch.get_last_good_ltp", return_value=3490.0), \
             patch("backend.brokers.kite_ticker.get_ticker", return_value=ticker):
            res = apply_ltp_patch(df, holdings_policy)
        assert res is not None
        assert res.any_patched
        assert df.at[0, 'last_price'] == 3490.0
        assert 'last_price_stale' in df.columns
        assert bool(df.at[0, 'last_price_stale']) is True


# ---------------------------------------------------------------------------
# SSOT grep — routes no longer inline the ticker-pull + LKG loop
# ---------------------------------------------------------------------------

BACKEND_ROOT = Path(__file__).resolve().parent.parent


class TestLtpPatchSSOT:
    def test_positions_route_uses_scaffold(self):
        src = (BACKEND_ROOT / "api" / "routes" / "positions.py").read_text(
            encoding="utf-8"
        )
        assert "apply_ltp_patch" in src
        # The old inline `for idx in raw.index:` ticker loop is gone.
        # (Other index iterations may exist for unrelated reasons; the
        # specific bookkeeping signature was `patched_idx: list = []`.)
        assert "patched_old_ltp: dict = {}" not in src

    def test_holdings_route_uses_scaffold(self):
        src = (BACKEND_ROOT / "api" / "routes" / "holdings.py").read_text(
            encoding="utf-8"
        )
        assert "apply_ltp_patch" in src
        assert "_zero_mask = _ltp_s <= 0" not in src

    def test_scaffold_is_imported_in_both_routes(self):
        for fname in ("positions.py", "holdings.py"):
            src = (BACKEND_ROOT / "api" / "routes" / fname).read_text(
                encoding="utf-8"
            )
            assert "from backend.api.helpers.ltp_patch import" in src, (
                f"{fname} should import from backend.api.helpers.ltp_patch"
            )
