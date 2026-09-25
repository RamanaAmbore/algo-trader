"""Broker-layer tests for A4's R1 fix: last-known-good substitution on ANY
per-account positions fetch failure, not only when the circuit breaker is
open.

Pre-fix: `_fetch_positions_local`'s except / `net_rows is None` branches
returned a bare empty `fetch_failed=True` DataFrame, bypassing
`_stale_substitute_frame` entirely — only the `_is_circuit_open` short-
circuit (BEFORE any fetch attempt) called it. An account that hadn't
opted into the circuit breaker (or hadn't yet tripped it) got zero
substitution on a single failed fetch, so a healthy sibling account's
success let the route's "not all failed" check pass while this account's
rows silently vanished from an otherwise-'live' response.

Post-fix: both failure branches call `_stale_substitute_frame("positions",
account)`, reusing the exact same LKG mechanism the breaker-open path
already used.

Also covers:
  - `_stale_substitute_frame` tags `attrs['account']` on every return path
    (including the no-LKG empty fallback) so a zero-row failure frame is
    still attributable to an account.
  - The "flat, then fails, must not resurrect" invariant: a successful-
    but-empty fetch must overwrite a prior non-empty LKG (moved
    `_record_lkg_frame` above the old empty-frame early-return), so a
    SUBSEQUENT failed fetch substitutes the correct (empty/flat) state,
    not stale pre-flat positions.
"""

from __future__ import annotations

import pandas as pd
import pytest


ACCOUNT = "R1_TEST_ACCT"


def _reset(account: str = ACCOUNT) -> None:
    from backend.brokers import broker_apis
    broker_apis._LKG_FRAME_BY_ACCT.pop(("positions", account), None)
    broker_apis._DB_LKG_CACHE.pop(("positions", account), None)
    broker_apis._FETCH_HEALTH.pop(account, None)


@pytest.fixture(autouse=True)
def _clean():
    _reset()
    yield
    _reset()


# ---------------------------------------------------------------------------
# _stale_substitute_frame — attrs['account'] tagging (all return paths)
# ---------------------------------------------------------------------------

class TestStaleSubstituteFrameAccountTag:
    def test_no_lkg_empty_frame_still_tagged_with_account(self):
        from backend.brokers.broker_apis import _stale_substitute_frame
        df = _stale_substitute_frame("positions", ACCOUNT)
        assert df.empty
        assert df.attrs.get("fetch_failed") is True
        assert df.attrs.get("account") == ACCOUNT

    def test_found_lkg_frame_tagged_with_account(self):
        from backend.brokers.broker_apis import _record_lkg_frame, _stale_substitute_frame
        seed = pd.DataFrame([{"tradingsymbol": "TCS", "quantity": 1, "account": ACCOUNT}])
        _record_lkg_frame("positions", ACCOUNT, seed)
        df = _stale_substitute_frame("positions", ACCOUNT)
        assert not df.empty
        assert df.attrs.get("account") == ACCOUNT
        assert df.attrs.get("fetch_failed") is not True


# ---------------------------------------------------------------------------
# _fetch_positions_local — substitution fires on ANY failure, not just
# breaker-open, and regardless of circuit-breaker opt-in status.
# ---------------------------------------------------------------------------

class TestFetchPositionsLocalR1Substitution:
    def _call(self, broker_positions_side_effect=None, broker_positions_return=None):
        """Invoke `_fetch_positions_local` for ACCOUNT with a mock broker
        whose `.positions()` either raises or returns a given payload.
        Bypasses `@for_all_accounts`'s account-iteration by calling the
        wrapped function directly with account= + broker= kwargs (the
        decorator's single-account fast path)."""
        from backend.brokers.broker_apis import _fetch_positions_local
        from unittest.mock import MagicMock

        mock_broker = MagicMock()
        if broker_positions_side_effect is not None:
            mock_broker.positions.side_effect = broker_positions_side_effect
        else:
            mock_broker.positions.return_value = broker_positions_return

        # _fetch_positions_local is decorated with @for_all_accounts, whose
        # Case 1 (single `account=` kwarg supplied) calls the wrapped
        # function directly with `broker=` injected — but we bypass the
        # decorator's own broker resolution by calling through with an
        # explicit `broker=` kwarg via the decorator's normal call path
        # is awkward to mock; instead call the undecorated function.
        undecorated = _fetch_positions_local.__wrapped__
        return undecorated(account=ACCOUNT, broker=mock_broker, kite=None)

    def test_broker_exception_with_lkg_substitutes_not_fetch_failed(self):
        """Circuit breaker NOT open, NOT opted-in — a bare exception from
        broker.positions() must still substitute last-known-good when one
        exists, rather than returning empty+fetch_failed."""
        from backend.brokers.broker_apis import _record_lkg_frame

        seed = pd.DataFrame([
            {"tradingsymbol": "TCS", "quantity": 10, "average_price": 3400.0,
             "last_price": 3500.0, "prev_close": 3480.0, "account": ACCOUNT,
             "exchange": "NSE", "product": "CNC", "pnl": 1000.0,
             "multiplier": 1},
        ])
        _record_lkg_frame("positions", ACCOUNT, seed)

        df = self._call(broker_positions_side_effect=RuntimeError("Kite connection timeout"))

        assert not df.empty, "R1: must substitute LKG rows instead of returning empty"
        assert df.attrs.get("fetch_failed") is not True, (
            "Substituted frame counts as success-with-stale-data, not a fetch failure"
        )
        assert df.attrs.get("stale") is True
        assert bool(df["account_stale"].iloc[0]) is True
        assert set(df["tradingsymbol"]) == {"TCS"}

    def test_broker_exception_without_lkg_falls_back_to_fetch_failed(self):
        """No LKG available (cold start) — must still fall back to the
        pre-fix empty+fetch_failed shape (nothing to substitute)."""
        df = self._call(broker_positions_side_effect=RuntimeError("Kite connection timeout"))
        assert df.empty
        assert df.attrs.get("fetch_failed") is True
        assert df.attrs.get("account") == ACCOUNT

    def test_none_net_rows_with_lkg_substitutes(self):
        """broker.positions() returning None (not an exception) must ALSO
        trigger substitution when LKG exists."""
        from backend.brokers.broker_apis import _record_lkg_frame

        seed = pd.DataFrame([
            {"tradingsymbol": "INFY", "quantity": 5, "average_price": 2400.0,
             "last_price": 2500.0, "prev_close": 2480.0, "account": ACCOUNT,
             "exchange": "NSE", "product": "MIS", "pnl": 500.0,
             "multiplier": 1},
        ])
        _record_lkg_frame("positions", ACCOUNT, seed)

        df = self._call(broker_positions_return=None)

        assert not df.empty
        assert df.attrs.get("fetch_failed") is not True
        assert bool(df["account_stale"].iloc[0]) is True


# ---------------------------------------------------------------------------
# "Flat, then fails, must not resurrect" — LKG recorded for empty
# successful fetches too, so a later failure substitutes the correct
# (empty) state instead of resurrecting stale pre-flat positions.
# ---------------------------------------------------------------------------

class TestFlatThenFailsDoesNotResurrect:
    def test_empty_successful_fetch_overwrites_prior_nonempty_lkg(self):
        """Direct unit test of the LKG-recording-ordering fix: a real
        (successful) empty DataFrame passed to `_record_lkg_frame` must
        overwrite a prior non-empty entry — `_stale_substitute_frame`
        afterwards must return empty, not the old positions."""
        from backend.brokers.broker_apis import _record_lkg_frame, _stale_substitute_frame

        # Prior session: account had an open TCS position.
        prior = pd.DataFrame([
            {"tradingsymbol": "TCS", "quantity": 10, "account": ACCOUNT},
        ])
        _record_lkg_frame("positions", ACCOUNT, prior)
        assert not _stale_substitute_frame("positions", ACCOUNT).empty

        # Account goes flat — a genuinely empty successful fetch.
        _record_lkg_frame("positions", ACCOUNT, pd.DataFrame())

        # A subsequent failed fetch must substitute the FLAT (empty)
        # state, not resurrect the old TCS position.
        df = _stale_substitute_frame("positions", ACCOUNT)
        assert df.empty, (
            "Flat-then-fails must not resurrect stale pre-flat positions — "
            "the empty successful fetch must have overwritten the LKG"
        )

    def test_fetch_positions_local_records_lkg_for_empty_successful_fetch(self):
        """End-to-end via `_fetch_positions_local`: a successful fetch that
        returns zero net_rows must still call `_record_lkg_frame` (moved
        above the old early-return) so a LATER failure correctly
        substitutes 'flat', not stale prior rows."""
        from backend.brokers.broker_apis import _fetch_positions_local, _stale_substitute_frame
        from unittest.mock import MagicMock

        undecorated = _fetch_positions_local.__wrapped__

        # Seed a prior non-empty LKG (simulating "had positions last poll").
        from backend.brokers.broker_apis import _record_lkg_frame
        _record_lkg_frame("positions", ACCOUNT, pd.DataFrame([
            {"tradingsymbol": "TCS", "quantity": 10, "account": ACCOUNT},
        ]))

        # This poll: broker reports zero open positions (flat) — a
        # successful fetch with net=[] (empty list, not None).
        mock_broker = MagicMock()
        mock_broker.positions.return_value = {"net": []}
        result = undecorated(account=ACCOUNT, broker=mock_broker, kite=None)
        assert result.empty

        # LKG must now reflect the flat state.
        substitute = _stale_substitute_frame("positions", ACCOUNT)
        assert substitute.empty, (
            "The flat successful fetch must have overwritten the prior "
            "non-empty LKG entry"
        )
