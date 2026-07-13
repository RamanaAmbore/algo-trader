"""
Unit tests for hedge proxy β regression math.

Pure numpy math tests — no DB, no broker, no mocks needed.
Tests the regression formulas in backend/api/routes/hedge_proxies.py:
  - _beta_r2_sigmas_from_returns (β, R², σ computation)
  - _regression_window_config (window tuning per asset class)
"""

import numpy as np
import pytest

from backend.api.routes.hedge_proxies import (
    _beta_r2_sigmas_from_returns,
    _regression_window_config,
)


class TestBetaFormula:
    """Test β = Cov(proxy, target) / Var(target) computation."""

    def test_beta_formula_basic(self):
        """Perfectly correlated series (1:1) should yield β ≈ 1.0."""
        # proxy = target (perfect correlation)
        target_ret = np.array([0.01, 0.02, -0.01, 0.005, 0.015, 0.008])
        proxy_ret = target_ret.copy()

        beta, r2, sigma_t, sigma_p = _beta_r2_sigmas_from_returns(
            proxy_ret, target_ret, "TESTPROXY", "TESTTARGET"
        )

        assert beta is not None, "beta should not be None for correlated series"
        assert abs(beta - 1.0) < 1e-6, f"expected β ≈ 1.0, got {beta}"

    def test_beta_formula_scaled(self):
        """Proxy = 2× target should yield β ≈ 2.0."""
        # If proxy = 2 * target, then β should be 2.0
        target_ret = np.array([0.01, 0.02, -0.01, 0.005, 0.015, 0.008])
        proxy_ret = 2.0 * target_ret

        beta, r2, sigma_t, sigma_p = _beta_r2_sigmas_from_returns(
            proxy_ret, target_ret, "TESTPROXY", "TESTTARGET"
        )

        assert beta is not None, "beta should not be None for scaled series"
        assert abs(beta - 2.0) < 1e-6, f"expected β ≈ 2.0, got {beta}"

    def test_beta_negative(self):
        """Proxy = -1× target should yield β ≈ -1.0 (negative β is valid)."""
        target_ret = np.array([0.01, 0.02, -0.01, 0.005, 0.015, 0.008])
        proxy_ret = -1.0 * target_ret

        beta, r2, sigma_t, sigma_p = _beta_r2_sigmas_from_returns(
            proxy_ret, target_ret, "TESTPROXY", "TESTTARGET"
        )

        assert beta is not None, "beta should not be None for negatively correlated series"
        assert abs(beta - (-1.0)) < 1e-6, f"expected β ≈ -1.0, got {beta}"

    def test_beta_large_value_allowed(self):
        """β > 2.0 is allowed (no exception raised), result returned."""
        # Proxy with very high variance relative to target → large β
        # Use correlated data to avoid hitting the β > 5.0 guard
        target_ret = np.array([0.01, 0.02, -0.01, 0.005, 0.015, 0.008] * 3)
        proxy_ret = 3.0 * target_ret  # β should be exactly 3.0

        beta, r2, sigma_t, sigma_p = _beta_r2_sigmas_from_returns(
            proxy_ret, target_ret, "TESTPROXY", "TESTTARGET"
        )

        # Should not raise; β > 2.0 is allowed (just not > 5.0)
        assert beta is not None, "beta should not be None for high-variance proxy"
        assert abs(beta - 3.0) < 1e-6, f"expected β ≈ 3.0, got {beta}"
        assert beta <= 5.0, "beta should be ≤ 5.0 (within plausibility guard)"


class TestR2Correlation:
    """Test R² = correlation² computation."""

    def test_r2_perfect_correlation(self):
        """Perfectly correlated series should yield r² ≈ 1.0."""
        target_ret = np.array([0.01, 0.02, -0.01, 0.005, 0.015, 0.008])
        proxy_ret = target_ret.copy()

        beta, r2, sigma_t, sigma_p = _beta_r2_sigmas_from_returns(
            proxy_ret, target_ret, "TESTPROXY", "TESTTARGET"
        )

        assert r2 is not None, "r2 should not be None for correlated series"
        assert abs(r2 - 1.0) < 1e-6, f"expected R² ≈ 1.0, got {r2}"

    def test_r2_zero_correlation(self):
        """Uncorrelated random series should yield r² close to 0."""
        # Fixed seed for determinism
        np.random.seed(42)
        target_ret = np.random.normal(0, 0.01, 20)
        # Independent random proxy
        proxy_ret = np.random.normal(0, 0.01, 20)

        beta, r2, sigma_t, sigma_p = _beta_r2_sigmas_from_returns(
            proxy_ret, target_ret, "TESTPROXY", "TESTTARGET"
        )

        # With a fixed seed, the random series should have near-zero correlation
        assert r2 is not None, "r2 should not be None"
        # Allowing r2 up to 0.3 for random noise (won't be exactly 0)
        assert r2 < 0.3, f"expected R² close to 0 for uncorrelated series, got {r2}"


class TestSigmas:
    """Test annualised volatility (σ_p, σ_t) computation."""

    def test_sigma_proxy(self):
        """Proxy series with known std → sigma_proxy ≈ std(returns) × √252."""
        # Create a returns series with known standard deviation
        target_ret = np.array([0.01, 0.02, -0.01, 0.005, 0.015, 0.008, 0.012, -0.005])
        # Proxy with same std as target (should also be std(target) × √252)
        proxy_ret = target_ret.copy()

        beta, r2, sigma_t, sigma_p = _beta_r2_sigmas_from_returns(
            proxy_ret, target_ret, "TESTPROXY", "TESTTARGET"
        )

        # sigma = std(returns) × √252
        expected_sigma = np.std(target_ret, ddof=1) * np.sqrt(252.0)

        assert sigma_p is not None, "sigma_p should not be None"
        assert abs(sigma_p - expected_sigma) < 1e-6, (
            f"expected sigma_p ≈ {expected_sigma:.6f}, got {sigma_p:.6f}"
        )

    def test_sigma_target(self):
        """Target series annualised σ_t computed correctly."""
        target_ret = np.array([0.01, 0.02, -0.01, 0.005, 0.015, 0.008, 0.012, -0.005])
        proxy_ret = target_ret.copy()

        beta, r2, sigma_t, sigma_p = _beta_r2_sigmas_from_returns(
            proxy_ret, target_ret, "TESTPROXY", "TESTTARGET"
        )

        expected_sigma_t = np.std(target_ret, ddof=1) * np.sqrt(252.0)

        assert sigma_t is not None, "sigma_t should not be None"
        assert abs(sigma_t - expected_sigma_t) < 1e-6, (
            f"expected sigma_t ≈ {expected_sigma_t:.6f}, got {sigma_t:.6f}"
        )


class TestZeroVarianceGuard:
    """Test handling of zero-variance edge cases."""

    def test_zero_variance_target_does_not_raise(self):
        """Target with zero variance should not raise ZeroDivisionError."""
        # Constant target (zero variance)
        target_ret = np.array([0.0] * 20)
        proxy_ret = np.array([0.01, 0.02, -0.01, 0.005] * 5)

        # Should not raise ZeroDivisionError
        beta, r2, sigma_t, sigma_p = _beta_r2_sigmas_from_returns(
            proxy_ret, target_ret, "TESTPROXY", "TESTTARGET"
        )

        # With zero variance, β cannot be computed → all should be None
        assert beta is None, f"expected beta=None for zero-variance target, got {beta}"
        assert r2 is None, f"expected r2=None for zero-variance target, got {r2}"
        assert sigma_t is None, f"expected sigma_t=None for zero-variance target, got {sigma_t}"
        assert sigma_p is None, f"expected sigma_p=None for zero-variance target, got {sigma_p}"

    def test_implausible_beta_above_5_rejected(self):
        """β > 5.0 should be rejected (logged + all returned as None)."""
        # Craft returns such that β > 5.0
        # This can happen with split-day outliers or bad ticks
        target_ret = np.array([0.001] * 15)  # minimal variance
        # Make proxy spike heavily → high β
        proxy_ret = np.array([0.01] * 15)
        proxy_ret[7] = 10.0  # Large outlier

        beta, r2, sigma_t, sigma_p = _beta_r2_sigmas_from_returns(
            proxy_ret, target_ret, "TESTPROXY", "TESTTARGET"
        )

        # With β > 5.0, the function should reject and return None
        assert beta is None, f"expected beta=None for implausible β > 5.0, got {beta}"
        assert r2 is None, f"expected r2=None for implausible β > 5.0, got {r2}"


class TestRegressionWindowConfig:
    """Test window tuning per asset class."""

    def test_regression_window_nse_equity(self):
        """NSE equity targets use 60-day window."""
        days_out, min_overlap, min_returns = _regression_window_config("RELIANCE", 60)

        assert days_out == 60, f"expected 60-day window for NSE equity, got {days_out}"
        assert min_overlap == 20, f"expected min_overlap=20 for NSE, got {min_overlap}"
        assert min_returns == 15, f"expected min_returns=15 for NSE, got {min_returns}"

    def test_regression_window_nse_index_nifty(self):
        """NSE index target (NIFTY) uses 60-day window."""
        days_out, min_overlap, min_returns = _regression_window_config("NIFTY", 60)

        assert days_out == 60, f"expected 60-day window for NIFTY, got {days_out}"
        assert min_overlap == 20, f"expected min_overlap=20 for NSE index, got {min_overlap}"
        assert min_returns == 15, f"expected min_returns=15 for NSE index, got {min_returns}"

    def test_regression_window_nse_index_banknifty(self):
        """NSE index target (BANKNIFTY) uses 60-day window."""
        days_out, min_overlap, min_returns = _regression_window_config("BANKNIFTY", 60)

        assert days_out == 60, f"expected 60-day window for BANKNIFTY, got {days_out}"

    def test_regression_window_mcx_gold(self):
        """MCX commodity target (GOLD) uses min(60, 30)=30-day window."""
        days_out, min_overlap, min_returns = _regression_window_config("GOLD", 60)

        assert days_out == 30, f"expected 30-day window for MCX GOLD, got {days_out}"
        assert min_overlap == 12, f"expected min_overlap=12 for MCX, got {min_overlap}"
        assert min_returns == 8, f"expected min_returns=8 for MCX, got {min_returns}"

    def test_regression_window_mcx_silver(self):
        """MCX commodity target (SILVER) uses 30-day window."""
        days_out, min_overlap, min_returns = _regression_window_config("SILVER", 60)

        assert days_out == 30, f"expected 30-day window for MCX SILVER, got {days_out}"

    def test_regression_window_mcx_goldm(self):
        """MCX commodity target (GOLDM mini) uses 30-day window."""
        days_out, min_overlap, min_returns = _regression_window_config("GOLDM", 60)

        assert days_out == 30, f"expected 30-day window for MCX GOLDM, got {days_out}"

    def test_regression_window_mcx_silverm(self):
        """MCX commodity target (SILVERM mini) uses 30-day window."""
        days_out, min_overlap, min_returns = _regression_window_config("SILVERM", 60)

        assert days_out == 30, f"expected 30-day window for MCX SILVERM, got {days_out}"

    def test_regression_window_mcx_crudeoil(self):
        """MCX commodity target (CRUDEOIL) uses 30-day window."""
        days_out, min_overlap, min_returns = _regression_window_config("CRUDEOIL", 60)

        assert days_out == 30, f"expected 30-day window for MCX CRUDEOIL, got {days_out}"

    def test_regression_window_mcx_capped_when_days_below_30(self):
        """When input days < 30, MCX should return the input (min of input, 30)."""
        # Request 20-day window for MCX target
        days_out, min_overlap, min_returns = _regression_window_config("GOLD", 20)

        # Should get min(20, 30) = 20
        assert days_out == 20, f"expected 20-day window (min of 20, 30), got {days_out}"

    def test_regression_window_nse_not_capped(self):
        """NSE targets should not be capped by the MCX 30-day ceiling."""
        # Request 90-day window for NSE
        days_out, min_overlap, min_returns = _regression_window_config("RELIANCE", 90)

        assert days_out == 90, f"expected 90-day window for NSE (not capped), got {days_out}"


class TestRegressionMathIntegration:
    """Integration tests combining β, r², σ in realistic scenarios."""

    def test_realistic_etf_tracking(self):
        """ETF (e.g., NIFTYBEES) tracking index (NIFTY) should have β ≈ 0.1, r² ≈ 1.0."""
        # Simulate NIFTY returns
        np.random.seed(123)
        nifty_ret = np.random.normal(0.0005, 0.015, 60)

        # NIFTYBEES = 1/10 NIFTY (plus minimal tracking error to preserve high correlation)
        tracking_error = np.random.normal(0, 0.0001, 60)  # 100x smaller error
        niftybees_ret = (nifty_ret / 10.0) + tracking_error

        beta, r2, sigma_t, sigma_p = _beta_r2_sigmas_from_returns(
            niftybees_ret, nifty_ret, "NIFTYBEES", "NIFTY"
        )

        # β should be close to 0.1 (1/10 leverage)
        assert beta is not None, "beta should not be None for ETF tracking"
        assert 0.05 < beta < 0.15, f"expected β ≈ 0.1 for 1:10 ETF, got {beta}"

        # r² should be very high (close to 1.0) with minimal tracking error
        assert r2 is not None, "r2 should not be None for ETF tracking"
        assert r2 > 0.99, f"expected R² > 0.99 for tight ETF tracking, got {r2}"

    def test_uncorrelated_pairs_produce_low_r2(self):
        """Unrelated assets should have low R²."""
        np.random.seed(456)
        asset1_ret = np.random.normal(0.0005, 0.02, 60)
        asset2_ret = np.random.normal(0.0005, 0.01, 60)

        beta, r2, sigma_t, sigma_p = _beta_r2_sigmas_from_returns(
            asset1_ret, asset2_ret, "ASSET1", "ASSET2"
        )

        # Uncorrelated random series should have low R²
        assert r2 is not None, "r2 should not be None"
        assert r2 < 0.5, f"expected R² < 0.5 for uncorrelated assets, got {r2}"

    def test_annualised_sigma_scaling(self):
        """Annualised σ should be daily σ × √252."""
        np.random.seed(789)
        daily_returns = np.random.normal(0, 0.01, 60)
        target_ret = daily_returns.copy()
        proxy_ret = daily_returns.copy()

        beta, r2, sigma_t, sigma_p = _beta_r2_sigmas_from_returns(
            proxy_ret, target_ret, "PROXY", "TARGET"
        )

        # Compute expected annualised sigma
        daily_std = np.std(target_ret, ddof=1)
        expected_annualised = daily_std * np.sqrt(252.0)

        assert sigma_t is not None, "sigma_t should not be None"
        assert abs(sigma_t - expected_annualised) < 1e-10, (
            f"expected σ_t ≈ {expected_annualised}, got {sigma_t}"
        )
