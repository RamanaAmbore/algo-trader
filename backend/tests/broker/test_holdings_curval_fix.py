"""Tests for holdings pnl + cur_val fix (Aug 2026).

Covers two broker-layer fixes:

  1. `_override_stale_ltp_from_ticker` in holdings.py now recomputes pnl
     and cur_val after patching last_price from the live KiteTicker. Without
     this, the DataFrame would have a patched last_price but stale pnl/cur_val
     values, making the API response internally inconsistent.

  2. `_build_holdings_pnl_expr` in broker_apis.py no longer blindly trusts
     broker pnl=0.0 when valid prices exist. The broker sends pnl=0.0 explicitly
     (not null) during the pre-market window when last_price=0. The old logic
     trusted that zero, incorrectly setting cur_val = inv_val. The new logic
     guards with `(_broker_pnl != 0.0)` so zero is treated as "no data",
     and the formula (ltp-avg)*qty is used instead. At true breakeven
     (ltp==avg) the formula also gives 0, so there is no regression.

Five quality dimensions per fix:
  * SSOT — canonical implementations in holdings.py and broker_apis.py.
  * Perf — no extra I/O; recompute runs in pandas once per row.
  * Stale-code grep — docstrings updated; no silent behaviour changes.
  * Reuse — standard LTP-patch + enrichment patterns.
  * UX — positions show correct current value immediately after LTP patch;
    breakeven positions still show zero pnl correctly.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest
import pandas as pd

from backend.api.routes.holdings import _override_stale_ltp_from_ticker
from backend.brokers.broker_apis import _enrich_holdings


class TestLTPOverrideRecomputesPnlAndCurval:
    """After ltp-override patch, pnl and cur_val must be recalculated
    against the new LTP, not left at the stale zero-LTP values.
    """

    def test_ltp_override_recomputes_pnl_and_curval(self, monkeypatch):
        """After LTP patch from zero to valid price, pnl and cur_val must
        reflect the new price — not remain at their stale zero-price values.

        Scenario: holdings row with zero LTP (pre-market), pnl=-10000, cur_val=0.
        After LTP is patched to 150 (from ticker), recompute should give:
          pnl = (150-100)*100 = 5000
          cur_val = inv_val + pnl = 10000 + 5000 = 15000
        """
        # Build a minimal DataFrame with one row — last_price already patched
        # by the monkeypatch (simulate what apply_ltp_patch would do).
        df = pd.DataFrame({
            "last_price": [150.0],           # patched (was 0, now valid)
            "average_price": [100.0],
            "quantity": [100],
            "opening_quantity": [100],
            "inv_val": [10000.0],
            "pnl": [-10000.0],               # stale: computed when last_price=0
            "cur_val": [0.0],                # stale
            "close_price": [98.0],
            "day_change_val": [0.0],
            "day_change": [0.0],
            "day_change_percentage": [0.0],
        })

        # Monkeypatch apply_ltp_patch to return a fake result where row 0 was patched.
        fake_patch_result = MagicMock()
        fake_patch_result.any_patched = True
        fake_patch_result.patched_idx = [0]
        fake_patch_result.stale_idx = []

        monkeypatch.setattr(
            "backend.api.routes.holdings.apply_ltp_patch",
            lambda df, policy: fake_patch_result,
        )

        # Call the function.
        _override_stale_ltp_from_ticker(df)

        # Assert: pnl was recomputed.
        assert df.iloc[0]["pnl"] == pytest.approx(5000.0), \
            f"expected pnl=5000 but got {df.iloc[0]['pnl']}"

        # Assert: cur_val was recomputed.
        assert df.iloc[0]["cur_val"] == pytest.approx(15000.0), \
            f"expected cur_val=15000 but got {df.iloc[0]['cur_val']}"

    def test_ltp_override_no_patch_leaves_values_unchanged(self, monkeypatch):
        """When apply_ltp_patch returns any_patched=False (no rows were patched),
        the function should return without modifying pnl/cur_val.
        """
        df = pd.DataFrame({
            "last_price": [150.0],
            "average_price": [100.0],
            "quantity": [100],
            "opening_quantity": [100],
            "inv_val": [10000.0],
            "pnl": [5000.0],
            "cur_val": [15000.0],
            "close_price": [98.0],
            "day_change_val": [0.0],
            "day_change": [0.0],
            "day_change_percentage": [0.0],
        })

        fake_patch_result = MagicMock()
        fake_patch_result.any_patched = False
        fake_patch_result.patched_idx = []
        fake_patch_result.stale_idx = []

        monkeypatch.setattr(
            "backend.api.routes.holdings.apply_ltp_patch",
            lambda df, policy: fake_patch_result,
        )

        # Store original values to verify they don't change.
        original_pnl = df.iloc[0]["pnl"]
        original_cur_val = df.iloc[0]["cur_val"]

        _override_stale_ltp_from_ticker(df)

        # Assert: values unchanged.
        assert df.iloc[0]["pnl"] == original_pnl
        assert df.iloc[0]["cur_val"] == original_cur_val


class TestEnrichHoldingsComputedPnlWhenBrokerSendsZero:
    """When broker sends pnl=0.0 (not null) but valid prices exist,
    _enrich_holdings must compute pnl from (ltp-avg)*qty.
    """

    def test_enrich_holdings_uses_computed_pnl_when_broker_sends_zero(self):
        """When broker sends pnl=0.0 (not null) but ltp and avg are valid,
        the function should compute pnl from (ltp-avg)*qty instead of trusting
        the zero.

        Scenario: holdings row from Kite pre-market window where broker sent
        pnl=0.0 because last_price=0, but now last_price has been patched
        to 150 from the ticker. Formula gives (150-100)*100 = 5000.
        """
        df = pd.DataFrame({
            "last_price": [150.0],
            "average_price": [100.0],
            "quantity": [100],
            "opening_quantity": [100],
            "pnl": [0.0],                   # broker explicitly sends zero
            "close_price": [98.0],
        })

        # Call _enrich_holdings to enrich the frame.
        result = _enrich_holdings(df)

        # Assert: pnl was computed from (ltp-avg)*qty.
        assert result.iloc[0]["pnl"] == pytest.approx(5000.0), \
            f"expected pnl=5000 but got {result.iloc[0]['pnl']}"

        # Assert: cur_val = inv_val + pnl = 10000 + 5000 = 15000.
        assert result.iloc[0]["cur_val"] == pytest.approx(15000.0), \
            f"expected cur_val=15000 but got {result.iloc[0]['cur_val']}"

    def test_enrich_holdings_trusts_broker_pnl_when_nonzero(self):
        """Regression guard: when broker sends non-zero pnl, use it.

        Scenario: broker_pnl = 8000 (trusted). Formula would give 5000,
        but we use broker's 8000 instead.
        """
        df = pd.DataFrame({
            "last_price": [150.0],
            "average_price": [100.0],
            "quantity": [100],
            "opening_quantity": [100],
            "pnl": [8000.0],                # broker sends non-zero, trust it
            "close_price": [98.0],
        })

        result = _enrich_holdings(df)

        # Assert: broker pnl was trusted (not recomputed).
        assert result.iloc[0]["pnl"] == pytest.approx(8000.0), \
            f"expected pnl=8000 (broker value) but got {result.iloc[0]['pnl']}"

        # Assert: cur_val = inv_val + broker_pnl = 10000 + 8000 = 18000.
        assert result.iloc[0]["cur_val"] == pytest.approx(18000.0), \
            f"expected cur_val=18000 but got {result.iloc[0]['cur_val']}"

    def test_enrich_holdings_trusts_broker_pnl_when_null(self):
        """Regression guard: when broker sends null pnl, compute from formula.

        Scenario: broker_pnl = None (missing data). Compute as (150-100)*100 = 5000.
        """
        df = pd.DataFrame({
            "last_price": [150.0],
            "average_price": [100.0],
            "quantity": [100],
            "opening_quantity": [100],
            "pnl": [None],                  # broker sends null
            "close_price": [98.0],
        })

        result = _enrich_holdings(df)

        # Assert: pnl was computed from formula.
        assert result.iloc[0]["pnl"] == pytest.approx(5000.0), \
            f"expected pnl=5000 (computed) but got {result.iloc[0]['pnl']}"


class TestEnrichHoldingsPnlZeroAtBreakeven:
    """At true breakeven (ltp==avg), pnl=0 is correct — no regression.

    When broker sends pnl=0 at breakeven, the computed formula (ltp-avg)*qty
    also gives 0, so there is no risk of computing a spurious non-zero value.
    """

    def test_enrich_holdings_pnl_zero_at_breakeven_broker_sends_zero(self):
        """At true breakeven (ltp==avg==100), broker sends pnl=0.
        Formula also gives 0, so we compute the correct value (not a regression).
        """
        df = pd.DataFrame({
            "last_price": [100.0],
            "average_price": [100.0],
            "quantity": [100],
            "opening_quantity": [100],
            "pnl": [0.0],                   # broker sends 0 at breakeven
            "close_price": [98.0],
        })

        result = _enrich_holdings(df)

        # Assert: pnl is 0 (computed formula also gives 0).
        assert result.iloc[0]["pnl"] == pytest.approx(0.0), \
            f"expected pnl=0 at breakeven but got {result.iloc[0]['pnl']}"

        # Assert: cur_val = inv_val + 0 = 10000 (100 shares × 100 price).
        assert result.iloc[0]["cur_val"] == pytest.approx(10000.0), \
            f"expected cur_val=10000 at breakeven but got {result.iloc[0]['cur_val']}"

    def test_enrich_holdings_pnl_zero_at_breakeven_broker_sends_null(self):
        """At true breakeven, broker sends null pnl (missing data).
        Formula gives 0, which is correct.
        """
        df = pd.DataFrame({
            "last_price": [100.0],
            "average_price": [100.0],
            "quantity": [100],
            "opening_quantity": [100],
            "pnl": [None],                  # broker sends null at breakeven
            "close_price": [98.0],
        })

        result = _enrich_holdings(df)

        # Assert: pnl is 0 (computed formula).
        assert result.iloc[0]["pnl"] == pytest.approx(0.0), \
            f"expected pnl=0 at breakeven but got {result.iloc[0]['pnl']}"

        # Assert: cur_val = inv_val = 10000.
        assert result.iloc[0]["cur_val"] == pytest.approx(10000.0), \
            f"expected cur_val=10000 at breakeven but got {result.iloc[0]['cur_val']}"
