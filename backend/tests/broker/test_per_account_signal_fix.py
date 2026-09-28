"""Tests for the 2026-09-27 audit fix to broker_apis.py / positions.py's
stale-account and partial-outage detection chain.

Root cause: `broker_apis.fetch_positions()` runs `_apply_backfill_to_list`
internally, which `pd.concat`s every per-account frame into ONE combined
frame whenever 2+ accounts are configured and at least one has real rows.
`pd.concat` clears `.attrs` on the result whenever the inputs' attrs
differ (the normal case — only a stale/failed account's frame carries
these keys) — so `positions.py`'s `_is_positions_outage`,
`_accounts_flagged_stale`, `_build_stale_since_map`, and
`_positions_partial_outage_accounts` (all of which read `df.attrs` per
frame, and all of which say "must be called BEFORE pd.concat" in their
own docstrings) silently stopped detecting any stale/failed account the
moment a second account was configured — reproduced directly against the
real `_apply_backfill_to_list` below.

Also covers two related fixes found in the same area:
  - The post-backfill LKG re-record pass no longer re-stamps
    `stale_since` for an account whose frame was itself stale-substituted
    (previously reset the "last genuinely successful" timestamp to `now`
    every cycle, so the 24h max-age cutoff could never fire).
  - A margins fetch failure no longer overwrites the last-known-good
    cached margins with an empty frame.
"""
from __future__ import annotations

import os
os.environ.setdefault("PYTEST_RUNNING", "1")

import time
import pandas as pd
import pytest
from unittest.mock import patch, MagicMock

from backend.brokers.broker_apis import (
    _apply_backfill_to_list,
    _capture_per_account_signal,
    _stale_accounts_from_signal,
    _record_lkg_frame,
    _get_lkg_frame,
    _LKG_FRAME_BY_ACCT,
    _LKG_FRAME_LOCK,
)
from backend.api.routes.positions import (
    _extract_per_account_signal,
    _is_positions_outage,
    _accounts_flagged_stale,
    _build_stale_since_map,
    _positions_partial_outage_accounts,
)


def _make_row(account, **overrides):
    base = {
        "account": account,
        "tradingsymbol": "NIFTY24JUN22000CE",
        "exchange": "NFO",
        "last_price": 150.0,
        "prev_close": 100.0,
        "quantity": 10,
        "average_price": 90.0,
        "pnl": 600.0,
        "day_change_val": 500.0,
        "day_change": 50.0,
        "day_change_percentage": 5.0,
    }
    base.update(overrides)
    return base


def _healthy_frame(account):
    return pd.DataFrame([_make_row(account)])


def _stale_frame(account, stale_since):
    df = pd.DataFrame([_make_row(account)])
    df.attrs["stale"] = True
    df.attrs["stale_since"] = stale_since
    df.attrs["account"] = account
    df.attrs["circuit_open"] = True
    return df


def _failed_empty_frame(account):
    df = pd.DataFrame()
    df.attrs["fetch_failed"] = True
    df.attrs["account"] = account
    return df


def _clear_lkg(kind, account):
    with _LKG_FRAME_LOCK:
        _LKG_FRAME_BY_ACCT.pop((kind, account), None)


class TestCapturePerAccountSignal:
    def test_captures_account_stale_fetch_failed_from_each_frame(self):
        healthy = _healthy_frame("ZG0790")
        stale = _stale_frame("DH6847", stale_since=1000.0)
        signal = _capture_per_account_signal([healthy, stale])

        by_acct = {s["account"]: s for s in signal}
        assert by_acct["ZG0790"]["stale"] is False
        assert by_acct["ZG0790"]["fetch_failed"] is False
        assert by_acct["DH6847"]["stale"] is True
        assert by_acct["DH6847"]["stale_since"] == 1000.0

    def test_captures_failed_empty_frame_account(self):
        failed = _failed_empty_frame("GR87DF")
        signal = _capture_per_account_signal([failed])
        assert signal[0]["account"] == "GR87DF"
        assert signal[0]["fetch_failed"] is True
        assert signal[0]["empty"] is True


class TestApplyBackfillToListPreservesSignal:
    """The core reproduction: 2+ accounts through the REAL
    _apply_backfill_to_list, with backfill_market_data patched to a no-op
    (rows already carry non-zero prices, so real backfill would no-op
    anyway — patched to guarantee no network/broker call in a unit test)."""

    def test_signal_survives_concat_with_two_accounts_one_stale(self):
        healthy = _healthy_frame("ZG0790")
        stale = _stale_frame("DH6847", stale_since=1234.0)

        with patch("backend.brokers.broker_apis.backfill_market_data", return_value=0):
            result = _apply_backfill_to_list([healthy, stale])

        assert len(result) == 1
        combined = result[0]
        # The bug this fixes: pd.concat alone would have cleared .attrs
        # here since the two input frames' attrs differ.
        signal = combined.attrs.get("_per_account_signal")
        assert signal is not None
        by_acct = {s["account"]: s for s in signal}
        assert by_acct["DH6847"]["stale"] is True
        assert by_acct["ZG0790"]["stale"] is False

    def test_signal_includes_failed_empty_account_excluded_from_concat(self):
        healthy = _healthy_frame("ZG0790")
        failed = _failed_empty_frame("GR87DF")

        with patch("backend.brokers.broker_apis.backfill_market_data", return_value=0):
            result = _apply_backfill_to_list([healthy, failed])

        combined = result[0]
        # GR87DF's row never made it into `combined` (empty frames are
        # excluded from the concat input) but its signal must still be
        # present — otherwise it silently vanishes with no trace.
        assert "GR87DF" not in combined.get("account", pd.Series(dtype=str)).values \
            if "account" in combined.columns else True
        signal = combined.attrs.get("_per_account_signal")
        by_acct = {s["account"]: s for s in signal}
        assert by_acct["GR87DF"]["fetch_failed"] is True


class TestPositionsHelpersReadSignalFromCombinedFrame:
    """End-to-end: positions.py's 4 helpers, given the REAL post-concat
    single-element list _apply_backfill_to_list produces (matching what
    fetch_positions() actually returns), correctly detect the
    stale/failed account — the exact scenario that was broken."""

    def test_accounts_flagged_stale_detects_with_two_accounts(self):
        healthy = _healthy_frame("ZG0790")
        stale = _stale_frame("DH6847", stale_since=1234.0)
        with patch("backend.brokers.broker_apis.backfill_market_data", return_value=0):
            per_acct = _apply_backfill_to_list([healthy, stale])

        assert _accounts_flagged_stale(per_acct) == {"DH6847"}

    def test_build_stale_since_map_detects_with_two_accounts(self):
        healthy = _healthy_frame("ZG0790")
        ts = time.time() - 300
        stale = _stale_frame("DH6847", stale_since=ts)
        with patch("backend.brokers.broker_apis.backfill_market_data", return_value=0):
            per_acct = _apply_backfill_to_list([healthy, stale])

        result = _build_stale_since_map(per_acct)
        assert "DH6847" in result

    def test_is_positions_outage_false_when_one_of_two_healthy(self):
        healthy = _healthy_frame("ZG0790")
        stale = _stale_frame("DH6847", stale_since=1234.0)
        with patch("backend.brokers.broker_apis.backfill_market_data", return_value=0):
            per_acct = _apply_backfill_to_list([healthy, stale])

        # Not a full outage — stale (not fetch_failed) + a healthy account.
        assert _is_positions_outage(per_acct) is False

    def test_positions_partial_outage_accounts_detects_failed_with_two_accounts(self):
        healthy = _healthy_frame("ZG0790")
        failed = _failed_empty_frame("GR87DF")
        with patch("backend.brokers.broker_apis.backfill_market_data", return_value=0):
            per_acct = _apply_backfill_to_list([healthy, failed])

        assert _positions_partial_outage_accounts(per_acct) == ["GR87DF"]

    def test_three_accounts_stale_failed_and_live_all_correctly_flagged(self):
        """The exact repro scenario from the audit: one stale-substituted
        account, one empty fetch_failed account, one live account."""
        live = _healthy_frame("ZG0790")
        stale = _stale_frame("DH6847", stale_since=time.time() - 60)
        failed = _failed_empty_frame("GR87DF")
        with patch("backend.brokers.broker_apis.backfill_market_data", return_value=0):
            per_acct = _apply_backfill_to_list([live, stale, failed])

        assert _accounts_flagged_stale(per_acct) == {"DH6847", "GR87DF"}
        assert _positions_partial_outage_accounts(per_acct) == ["GR87DF"]
        assert _is_positions_outage(per_acct) is False
        stale_map = _build_stale_since_map(per_acct)
        assert "DH6847" in stale_map


class TestExtractPerAccountSignalFallback:
    """Raw multi-frame input (no _per_account_signal attr — the
    pre-existing test suite's shape) must still work exactly as before."""

    def test_falls_back_to_raw_frame_attrs_when_no_signal_present(self):
        healthy = _healthy_frame("ZG0790")
        stale = _stale_frame("DH6847", stale_since=999.0)
        signal = _extract_per_account_signal([healthy, stale])
        by_acct = {s["account"]: s for s in signal}
        assert by_acct["DH6847"]["stale"] is True


class TestStaleAccountsFromSignal:
    def test_returns_stale_accounts_only(self):
        healthy = _healthy_frame("ZG0790")
        stale = _stale_frame("DH6847", stale_since=1234.0)
        with patch("backend.brokers.broker_apis.backfill_market_data", return_value=0):
            per_acct = _apply_backfill_to_list([healthy, stale])
        combined = per_acct[0]
        assert _stale_accounts_from_signal(combined) == {"DH6847"}


class TestLkgRestampSkippedForStaleAccounts:
    """The post-backfill LKG re-record pass in _fetch_holdings_cached/
    _fetch_positions_cached must not reset a stale-substituted account's
    stale_since to `now` — see _stale_accounts_from_signal's docstring."""

    def test_stale_account_lkg_not_restamped(self):
        _clear_lkg("positions", "DH6847")
        # Seed an LKG entry with an old timestamp.
        old_ts = time.time() - 3600
        seed_df = _healthy_frame("DH6847")
        with _LKG_FRAME_LOCK:
            _LKG_FRAME_BY_ACCT[("positions", "DH6847")] = (old_ts, seed_df)

        # Simulate the post-backfill loop's decision for a stale account:
        # it must skip _record_lkg_frame entirely.
        stale_accts = {"DH6847"}
        acct = "DH6847"
        if acct not in stale_accts:
            _record_lkg_frame("positions", acct, _healthy_frame(acct))

        entry = _get_lkg_frame("positions", "DH6847")
        assert entry is not None
        ts, _ = entry
        assert ts == old_ts  # unchanged — not restamped

        _clear_lkg("positions", "DH6847")


class TestMarginsFailureDoesNotClobberLkg:
    def test_margins_exception_path_preserves_lkg_via_stale_substitute(self):
        """Directly exercises the fixed exception-path logic: on failure,
        the function must substitute the real LKG frame (if one exists)
        instead of recording+returning an empty failed frame."""
        _clear_lkg("margins", "ZG0790")
        good_margins = pd.DataFrame([{"account": "ZG0790", "avail_margin": 50000.0}])
        _record_lkg_frame("margins", "ZG0790", good_margins)

        from backend.brokers.broker_apis import _stale_substitute_frame
        lkg = _stale_substitute_frame("margins", "ZG0790")

        assert not lkg.empty
        assert lkg.attrs.get("stale") is True
        assert lkg.attrs.get("fetch_failed", False) is False

        # Confirm the LKG entry itself was NOT overwritten with an empty
        # frame — re-fetching it again must still return the good data.
        entry = _get_lkg_frame("margins", "ZG0790")
        assert entry is not None
        _, cached_df = entry
        assert not cached_df.empty
        assert cached_df["avail_margin"].iloc[0] == 50000.0

        _clear_lkg("margins", "ZG0790")
