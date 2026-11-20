"""Broker-layer tests for the holdings freeze-to-last-known-good fix
(sibling of `_fetch_positions_local`'s / `_fetch_margins_local`'s existing
R1 substitution — see `test_positions_r1_substitution.py`).

Pre-fix: `_fetch_holdings_local`'s plain-exception branch and its
`rows is None` branch both returned a bare empty `fetch_failed=True`
DataFrame, bypassing `_stale_substitute_frame` entirely — only the
`_is_circuit_open` short-circuit (BEFORE any fetch attempt) called it.
Positions and margins already substituted last-known-good on ANY
per-account fetch failure (not only breaker-open); holdings was the one
kind still falling through to a bare empty frame on a plain exception.

Post-fix: both failure branches call `_stale_substitute_frame("holdings",
account)`, matching positions/margins and the holdings breaker-open path.

Also covers a second, independent failure point in the same function:
`_enrich_holdings` raising for a malformed row shape must ALSO freeze to
last-known-good instead of propagating past the per-account boundary
`@for_all_accounts` relies on (see `backend/shared/helpers/decorators.py`
`_per_account` — an uncaught exception there breaks the whole multi-
account `ThreadPoolExecutor.map` fan-out, not just the one account).
"""

from __future__ import annotations

import pandas as pd
import pytest


ACCOUNT = "R1_HOLD_TEST_ACCT"


def _reset(account: str = ACCOUNT) -> None:
    from backend.brokers import broker_apis
    broker_apis._LKG_FRAME_BY_ACCT.pop(("holdings", account), None)
    broker_apis._DB_LKG_CACHE.pop(("holdings", account), None)
    broker_apis._FETCH_HEALTH.pop(account, None)


@pytest.fixture(autouse=True)
def _clean():
    _reset()
    yield
    _reset()


def _call(broker_holdings_side_effect=None, broker_holdings_return=None):
    """Invoke `_fetch_holdings_local` for ACCOUNT, bypassing
    `@for_all_accounts`'s own account iteration by calling the undecorated
    function directly with account=/broker=/kite= kwargs — same pattern
    `test_positions_r1_substitution.py` uses."""
    from backend.brokers.broker_apis import _fetch_holdings_local
    from unittest.mock import MagicMock

    mock_broker = MagicMock()
    if broker_holdings_side_effect is not None:
        mock_broker.holdings.side_effect = broker_holdings_side_effect
    else:
        mock_broker.holdings.return_value = broker_holdings_return

    undecorated = _fetch_holdings_local.__wrapped__
    return undecorated(account=ACCOUNT, broker=mock_broker, kite=None)


# ---------------------------------------------------------------------------
# Plain exception from broker.holdings() — must substitute LKG, not just
# the breaker-open short-circuit.
# ---------------------------------------------------------------------------

class TestFetchHoldingsLocalPlainExceptionFreezesToLKG:
    def test_broker_exception_with_lkg_substitutes_not_fetch_failed(self):
        """A bare exception from broker.holdings() (breaker NOT open, NOT
        opted-in) must substitute last-known-good when one exists, rather
        than returning empty+fetch_failed."""
        from backend.brokers.broker_apis import _record_lkg_frame

        seed = pd.DataFrame([
            {"tradingsymbol": "TCS", "quantity": 10, "average_price": 3400.0,
             "last_price": 3500.0, "prev_close": 3480.0, "account": ACCOUNT,
             "pnl": 1000.0},
        ])
        _record_lkg_frame("holdings", ACCOUNT, seed)

        df = _call(broker_holdings_side_effect=RuntimeError("Kite connection timeout"))

        assert not df.empty, "Must substitute LKG rows instead of returning empty"
        assert df.attrs.get("fetch_failed") is not True, (
            "Substituted frame counts as success-with-stale-data, not a fetch failure"
        )
        assert df.attrs.get("stale") is True
        assert bool(df["account_stale"].iloc[0]) is True
        assert set(df["tradingsymbol"]) == {"TCS"}

    def test_broker_exception_without_lkg_falls_back_to_fetch_failed(self):
        """No LKG available (cold start) — must still fall back to the
        empty+fetch_failed shape (nothing to substitute)."""
        df = _call(broker_holdings_side_effect=RuntimeError("Kite connection timeout"))
        assert df.empty
        assert df.attrs.get("fetch_failed") is True
        assert df.attrs.get("account") == ACCOUNT

    def test_rows_none_with_lkg_substitutes(self):
        """broker.holdings() returning None (not an exception) must ALSO
        trigger substitution when LKG exists."""
        from backend.brokers.broker_apis import _record_lkg_frame

        seed = pd.DataFrame([
            {"tradingsymbol": "INFY", "quantity": 5, "average_price": 2400.0,
             "last_price": 2500.0, "prev_close": 2480.0, "account": ACCOUNT,
             "pnl": 500.0},
        ])
        _record_lkg_frame("holdings", ACCOUNT, seed)

        df = _call(broker_holdings_return=None)

        assert not df.empty
        assert df.attrs.get("fetch_failed") is not True
        assert bool(df["account_stale"].iloc[0]) is True

    def test_rows_none_without_lkg_falls_back_to_fetch_failed(self):
        df = _call(broker_holdings_return=None)
        assert df.empty
        assert df.attrs.get("fetch_failed") is True
        assert df.attrs.get("account") == ACCOUNT


# ---------------------------------------------------------------------------
# Enrichment-stage failure — a malformed row shape for this account must
# also freeze to LKG rather than raising past the per-account function.
# ---------------------------------------------------------------------------

class TestFetchHoldingsLocalEnrichmentFailureFreezesToLKG:
    def test_enrich_holdings_exception_with_lkg_substitutes(self):
        from backend.brokers.broker_apis import _record_lkg_frame
        from unittest.mock import patch

        seed = pd.DataFrame([
            {"tradingsymbol": "WIPRO", "quantity": 20, "average_price": 400.0,
             "last_price": 420.0, "prev_close": 410.0, "account": ACCOUNT,
             "pnl": 400.0},
        ])
        _record_lkg_frame("holdings", ACCOUNT, seed)

        with patch(
            "backend.brokers.broker_apis._enrich_holdings",
            side_effect=RuntimeError("malformed row shape"),
        ):
            df = _call(broker_holdings_return=[
                {"tradingsymbol": "WIPRO", "quantity": 20, "average_price": 400.0,
                 "last_price": 420.0, "close_price": 410.0},
            ])

        assert not df.empty, (
            "An enrichment failure for ONE account must freeze to LKG, not "
            "propagate past the per-account boundary"
        )
        assert df.attrs.get("fetch_failed") is not True
        assert df.attrs.get("stale") is True
        assert set(df["tradingsymbol"]) == {"WIPRO"}

    def test_enrich_holdings_exception_without_lkg_falls_back_to_fetch_failed(self):
        from unittest.mock import patch

        with patch(
            "backend.brokers.broker_apis._enrich_holdings",
            side_effect=RuntimeError("malformed row shape"),
        ):
            df = _call(broker_holdings_return=[
                {"tradingsymbol": "WIPRO", "quantity": 20, "average_price": 400.0,
                 "last_price": 420.0, "close_price": 410.0},
            ])

        assert df.empty
        assert df.attrs.get("fetch_failed") is True
        assert df.attrs.get("account") == ACCOUNT

    def test_enrich_holdings_exception_does_not_raise(self):
        """The enrichment exception must never propagate out of
        `_fetch_holdings_local` — it must always be caught and converted
        into a (possibly empty) DataFrame."""
        from unittest.mock import patch

        with patch(
            "backend.brokers.broker_apis._enrich_holdings",
            side_effect=ValueError("boom"),
        ):
            df = _call(broker_holdings_return=[
                {"tradingsymbol": "WIPRO", "quantity": 20, "average_price": 400.0,
                 "last_price": 420.0, "close_price": 410.0},
            ])

        assert isinstance(df, pd.DataFrame)
