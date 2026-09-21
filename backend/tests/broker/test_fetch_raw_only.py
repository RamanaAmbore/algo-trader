"""
Tests for raw_only=True parameter in _fetch_holdings_local and _fetch_positions_local.

Verifies:
  - raw_only=True skips enrichment functions (_enrich_holdings, _enrich_positions)
  - raw_only=True returns DataFrame with prev_close populated from broker close_price
  - raw_only=True filters to only ['account', 'tradingsymbol', 'prev_close'] columns
  - raw_only=True works for both holdings and positions
  - raw_only=False (default) runs full enrichment (control test)

Quality dimensions:
  1. SSOT        — raw_only parameter is the gate to skip enrichment
  2. Correctness — prev_close is available without enrichment step
  3. Performance — raw_only path avoids Polars conversions + computed columns
  4. Stale code  — enrichment functions should not be called when raw_only=True
  5. Isolation   — holdings and positions paths behave identically w.r.t. raw_only
"""

from __future__ import annotations

import pytest
import pandas as pd
from unittest.mock import patch, MagicMock, AsyncMock, call


def make_raw_holdings_df() -> pd.DataFrame:
    """Construct a minimal raw holdings DataFrame from broker (before enrichment)."""
    return pd.DataFrame([{
        "tradingsymbol": "HFCL",
        "close_price": 215.71,  # Raw broker field; will be renamed to prev_close
        "quantity": 100,
        "average_price": 200.0,
        "last_price": 215.71,
        "t1_quantity": 0,
        "used_quantity": 0,
        "collateral_quantity": 0,
        "collateral_type": "",
        "isin": "INE544E01019",
        "product": "CNC",
        "exchange": "NSE",
        "pnl": 1571.0,
    }])


def make_raw_positions_df() -> pd.DataFrame:
    """Construct a minimal raw positions DataFrame from broker (before enrichment)."""
    return pd.DataFrame([{
        "tradingsymbol": "RELIANCE",
        "close_price": 2455.50,  # Raw broker field; will be renamed to prev_close
        "quantity": 10,
        "average_price": 2400.0,
        "last_price": 2455.50,
        "overnight_quantity": 10,
        "day_buy_quantity": 0,
        "day_sell_quantity": 0,
        "day_buy_value": 0.0,
        "day_sell_value": 0.0,
        "pnl": 555.0,
        "multiplier": 1,
        "exchange": "NSE",
    }])


def test_fetch_holdings_raw_only_skips_enrich():
    """raw_only=True must NOT call _enrich_holdings.

    Even though the broker DataFrame is returned with close_price renamed to
    prev_close, the enrichment function must be completely bypassed.
    """
    from backend.brokers.broker_apis import _fetch_holdings_local
    raw_df = make_raw_holdings_df()

    with patch("backend.brokers.broker_apis._enrich_holdings") as mock_enrich:
        with patch("backend.brokers.broker_apis._record_fetch"):
            with patch("backend.brokers.broker_apis._record_lkg_frame"):
                # Call the unwrapped function directly (bypass @for_all_accounts)
                # by accessing .__wrapped__
                result = _fetch_holdings_local.__wrapped__(
                    connections=MagicMock(),
                    account="ACC1",
                    kite=MagicMock(**{"holdings.return_value": raw_df.to_dict("records")}),
                    broker=None,
                    raw_only=True,
                )
                # Verify _enrich_holdings was NOT called
                mock_enrich.assert_not_called(), (
                    "raw_only=True must skip _enrich_holdings call"
                )


def test_fetch_holdings_raw_only_returns_prev_close():
    """raw_only=True must return DataFrame with prev_close populated.

    The broker's close_price field is renamed to prev_close, and the value
    should be available without any enrichment or backfill processing.
    """
    from backend.brokers.broker_apis import _fetch_holdings_local
    raw_df = make_raw_holdings_df()

    with patch("backend.brokers.broker_apis._record_fetch"):
        with patch("backend.brokers.broker_apis._record_lkg_frame"):
            result = _fetch_holdings_local.__wrapped__(
                connections=MagicMock(),
                account="ACC1",
                kite=MagicMock(**{"holdings.return_value": raw_df.to_dict("records")}),
                broker=None,
                raw_only=True,
            )
            assert "prev_close" in result.columns, "prev_close column must be present"
            assert len(result) > 0, "result should not be empty"
            prev_close_val = result["prev_close"].iloc[0]
            assert abs(prev_close_val - 215.71) < 0.01, (
                f"prev_close should be 215.71 (from broker close_price), got {prev_close_val}"
            )


def test_fetch_holdings_raw_only_filters_columns():
    """raw_only=True must return only ['account', 'tradingsymbol', 'prev_close'].

    This ensures callers can use the minimal set without triggering full
    enrichment on large DataFrames.
    """
    from backend.brokers.broker_apis import _fetch_holdings_local
    raw_df = make_raw_holdings_df()

    with patch("backend.brokers.broker_apis._record_fetch"):
        with patch("backend.brokers.broker_apis._record_lkg_frame"):
            result = _fetch_holdings_local.__wrapped__(
                connections=MagicMock(),
                account="ACC1",
                kite=MagicMock(**{"holdings.return_value": raw_df.to_dict("records")}),
                broker=None,
                raw_only=True,
            )
            expected_cols = {"account", "tradingsymbol", "prev_close"}
            actual_cols = set(result.columns)
            assert actual_cols == expected_cols, (
                f"Expected columns {expected_cols}, got {actual_cols}"
            )


def test_fetch_positions_raw_only_skips_enrich():
    """raw_only=True must NOT call _enrich_positions.

    Same gate applied to the positions path — enrichment is bypassed
    completely.
    """
    from backend.brokers.broker_apis import _fetch_positions_local
    raw_df = make_raw_positions_df()

    with patch("backend.brokers.broker_apis._enrich_positions") as mock_enrich:
        with patch("backend.brokers.broker_apis._record_fetch"):
            with patch("backend.brokers.broker_apis._record_lkg_frame"):
                with patch("backend.brokers.broker_apis._annotate_lot_size"):
                    result = _fetch_positions_local.__wrapped__(
                        connections=MagicMock(),
                        account="ACC1",
                        kite=MagicMock(**{
                            "positions.return_value": {
                                "net": raw_df.to_dict("records")
                            }
                        }),
                        broker=None,
                        raw_only=True,
                    )
                    mock_enrich.assert_not_called(), (
                        "raw_only=True must skip _enrich_positions call"
                    )


def test_fetch_positions_raw_only_returns_prev_close():
    """raw_only=True must return positions DataFrame with prev_close populated."""
    from backend.brokers.broker_apis import _fetch_positions_local
    raw_df = make_raw_positions_df()

    with patch("backend.brokers.broker_apis._record_fetch"):
        with patch("backend.brokers.broker_apis._record_lkg_frame"):
            with patch("backend.brokers.broker_apis._annotate_lot_size"):
                result = _fetch_positions_local.__wrapped__(
                    connections=MagicMock(),
                    account="ACC1",
                    kite=MagicMock(**{
                        "positions.return_value": {
                            "net": raw_df.to_dict("records")
                        }
                    }),
                    broker=None,
                    raw_only=True,
                )
                assert "prev_close" in result.columns, "prev_close column must be present"
                assert len(result) > 0, "result should not be empty"
                prev_close_val = result["prev_close"].iloc[0]
                assert abs(prev_close_val - 2455.50) < 0.01, (
                    f"prev_close should be 2455.50 (from broker close_price), got {prev_close_val}"
                )


def test_fetch_positions_raw_only_filters_columns():
    """raw_only=True must return only ['account', 'tradingsymbol', 'prev_close']."""
    from backend.brokers.broker_apis import _fetch_positions_local
    raw_df = make_raw_positions_df()

    with patch("backend.brokers.broker_apis._record_fetch"):
        with patch("backend.brokers.broker_apis._record_lkg_frame"):
            with patch("backend.brokers.broker_apis._annotate_lot_size"):
                result = _fetch_positions_local.__wrapped__(
                    connections=MagicMock(),
                    account="ACC1",
                    kite=MagicMock(**{
                        "positions.return_value": {
                            "net": raw_df.to_dict("records")
                        }
                    }),
                    broker=None,
                    raw_only=True,
                )
                expected_cols = {"account", "tradingsymbol", "prev_close"}
                actual_cols = set(result.columns)
                assert actual_cols == expected_cols, (
                    f"Expected columns {expected_cols}, got {actual_cols}"
                )


def test_fetch_holdings_raw_false_calls_enrich():
    """raw_only=False (default) must call _enrich_holdings (control test).

    Verify that the enrichment path still activates when raw_only is not set
    or is explicitly False.
    """
    from backend.brokers.broker_apis import _fetch_holdings_local
    raw_df = make_raw_holdings_df()

    # Mock _enrich_holdings to return the input unchanged (just verify it's called)
    mock_enriched = raw_df.copy()
    mock_enriched = mock_enriched.rename(columns={'close_price': 'prev_close'})

    with patch("backend.brokers.broker_apis._enrich_holdings", return_value=mock_enriched) as mock_enrich:
        with patch("backend.brokers.broker_apis._record_fetch"):
            with patch("backend.brokers.broker_apis._record_lkg_frame"):
                result = _fetch_holdings_local.__wrapped__(
                    connections=MagicMock(),
                    account="ACC1",
                    kite=MagicMock(**{"holdings.return_value": raw_df.to_dict("records")}),
                    broker=None,
                    raw_only=False,  # Explicitly False
                )
                # Verify _enrich_holdings WAS called
                mock_enrich.assert_called_once()


def test_fetch_positions_raw_false_calls_enrich():
    """raw_only=False (default) must call _enrich_positions (control test)."""
    from backend.brokers.broker_apis import _fetch_positions_local
    raw_df = make_raw_positions_df()

    # Mock _enrich_positions to return the input unchanged
    mock_enriched = raw_df.copy()
    mock_enriched = mock_enriched.rename(columns={'close_price': 'prev_close'})

    with patch("backend.brokers.broker_apis._enrich_positions", return_value=mock_enriched) as mock_enrich:
        with patch("backend.brokers.broker_apis._record_fetch"):
            with patch("backend.brokers.broker_apis._record_lkg_frame"):
                with patch("backend.brokers.broker_apis._annotate_lot_size"):
                    result = _fetch_positions_local.__wrapped__(
                        connections=MagicMock(),
                        account="ACC1",
                        kite=MagicMock(**{
                            "positions.return_value": {
                                "net": raw_df.to_dict("records")
                            }
                        }),
                        broker=None,
                        raw_only=False,  # Explicitly False
                    )
                    # Verify _enrich_positions WAS called
                    mock_enrich.assert_called_once()


def test_fetch_holdings_raw_only_empty_frame():
    """raw_only=True on empty holdings frame should return empty DataFrame."""
    from backend.brokers.broker_apis import _fetch_holdings_local
    empty_df = pd.DataFrame()

    with patch("backend.brokers.broker_apis._record_fetch"):
        with patch("backend.brokers.broker_apis._record_lkg_frame"):
            result = _fetch_holdings_local.__wrapped__(
                connections=MagicMock(),
                account="ACC1",
                kite=MagicMock(**{"holdings.return_value": []}),
                broker=None,
                raw_only=True,
            )
            assert result.empty, "Empty frame should stay empty"


def test_fetch_positions_raw_only_empty_frame():
    """raw_only=True on empty positions frame should return empty DataFrame."""
    from backend.brokers.broker_apis import _fetch_positions_local

    with patch("backend.brokers.broker_apis._record_fetch"):
        with patch("backend.brokers.broker_apis._record_lkg_frame"):
            with patch("backend.brokers.broker_apis._annotate_lot_size"):
                result = _fetch_positions_local.__wrapped__(
                    connections=MagicMock(),
                    account="ACC1",
                    kite=MagicMock(**{
                        "positions.return_value": {
                            "net": []
                        }
                    }),
                    broker=None,
                    raw_only=True,
                )
                assert result.empty, "Empty frame should stay empty"
