"""Tests for compute_firm_nav holdings snapshot path.

_fetch_holdings_from_snapshot delegates to _holdings_snapshot() (the holdings
route SSOT) so firm NAV and the holdings grid always agree after NSE closes.

_fetch_holdings_phase branches on is_exchange_closed_now("NSE"):
  - NSE closed → _fetch_holdings_from_snapshot (daily_book via _holdings_snapshot)
  - NSE open   → live broker fetch (cur_val is current during the session)
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestFetchHoldingsFromSnapshot:
    """_fetch_holdings_from_snapshot delegates to the _holdings_snapshot() SSOT."""

    @pytest.mark.asyncio
    async def test_sums_cur_val_across_accounts(self):
        """Returns sum of cur_val from all snapshot rows and populates accounts."""
        from backend.api.algo.nav import _fetch_holdings_from_snapshot

        row1 = MagicMock(); row1.cur_val = 150000.0; row1.account = "ZG1234"
        row2 = MagicMock(); row2.cur_val = 50000.0;  row2.account = "DH5678"
        mock_snap = MagicMock()
        mock_snap.rows = [row1, row2]

        accounts, errs = [], []
        with patch(
            "backend.api.routes.holdings._holdings_snapshot",
            new=AsyncMock(return_value=mock_snap),
        ):
            total = await _fetch_holdings_from_snapshot(accounts, errs)

        assert total == pytest.approx(200000.0)
        assert set(accounts) == {"ZG1234", "DH5678"}
        assert not errs

    @pytest.mark.asyncio
    async def test_returns_zero_when_snapshot_is_none(self):
        """Returns 0.0 (no error) when _holdings_snapshot returns None."""
        from backend.api.algo.nav import _fetch_holdings_from_snapshot

        accounts, errs = [], []
        with patch(
            "backend.api.routes.holdings._holdings_snapshot",
            new=AsyncMock(return_value=None),
        ):
            total = await _fetch_holdings_from_snapshot(accounts, errs)

        assert total == 0.0
        assert not errs

    @pytest.mark.asyncio
    async def test_appends_error_and_returns_zero_on_exception(self):
        """On exception: appends to errors list with 'holdings_snapshot' tag and returns 0.0."""
        from backend.api.algo.nav import _fetch_holdings_from_snapshot

        accounts, errs = [], []
        with patch(
            "backend.api.routes.holdings._holdings_snapshot",
            new=AsyncMock(side_effect=RuntimeError("DB gone")),
        ):
            total = await _fetch_holdings_from_snapshot(accounts, errs)

        assert total == 0.0
        assert len(errs) == 1
        assert "holdings_snapshot" in errs[0]


class TestFetchHoldingsPhaseNSEClosed:
    """_fetch_holdings_phase branches correctly on NSE state."""

    @pytest.mark.asyncio
    async def test_uses_snapshot_when_nse_closed(self):
        """Snapshot helper is called (not broker) when is_exchange_closed_now('NSE') is True."""
        import pandas as pd
        from backend.api.algo.nav import _fetch_holdings_phase

        accounts, errs = [], []
        with patch(
            "backend.api.helpers.snapshot_gate.is_exchange_closed_now",
            return_value=True,
        ), patch(
            "backend.api.algo.nav._fetch_holdings_from_snapshot",
            new=AsyncMock(return_value=188_000_000.0),
        ) as mock_snap:
            total = await _fetch_holdings_phase(accounts, errs, ticker=MagicMock())

        mock_snap.assert_called_once_with(accounts, errs)
        assert total == pytest.approx(188_000_000.0)

    @pytest.mark.asyncio
    async def test_uses_broker_when_nse_open(self):
        """Broker fetch path runs when is_exchange_closed_now('NSE') is False."""
        import pandas as pd
        from backend.api.algo.nav import _fetch_holdings_phase

        sample_df = pd.DataFrame([{
            "tradingsymbol": "INFY",
            "exchange": "NSE",
            "account": "ZG1234",
            "quantity": 100,
            "cur_val": 150000.0,
            "last_price": 1500.0,
        }])

        mock_ticker = MagicMock()
        mock_ticker.get_ltp_by_sym.return_value = 0

        accounts, errs = [], []
        with patch(
            "backend.api.helpers.snapshot_gate.is_exchange_closed_now",
            return_value=False,
        ), patch(
            "backend.api.algo.nav.asyncio.to_thread",
            new=AsyncMock(return_value=[sample_df]),
        ):
            total = await _fetch_holdings_phase(accounts, errs, ticker=mock_ticker)

        assert total == pytest.approx(150000.0)
        assert "ZG1234" in accounts
