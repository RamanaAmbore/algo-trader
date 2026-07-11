"""
Characterization tests for _v2_match_to_alertrow().

Goal: push branch coverage to ≥80% before refactoring.

This function converts a v2 evaluator match (from agent_evaluator.py)
into an alert-row dict suitable for Telegram + email rendering. It handles:
  - section derivation from scope token prefix (holdings/positions/funds)
  - kind (alert style) from metric suffix or special tokens
  - optional enrichment: underlying breakdown, rate-of-loss in static alerts
  - row data extraction (pnl, pct, rate_val, threshold formatting)
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch


class TestV2MatchToAlertrowSimpleLeaves:
    """Test simple metric → kind → section mappings."""

    def test_holdings_scope_with_cash_metric_yields_negative_cash_kind(self):
        """
        Scope token starts with 'holdings', metric is 'cash' → kind='negative_cash'.
        """
        from backend.api.algo.agent_engine import _v2_match_to_alertrow

        match = {
            'metric': 'cash',
            'scope': 'holdings_any_acct',
            'op': 'lt',
            'threshold': -10000,
            'value': -15000,
            'row': {'account': 'TOTAL', 'day_change_val': -100},
        }
        result = _v2_match_to_alertrow(match)
        assert result['kind'] == 'negative_cash', (
            "holdings + cash metric must yield negative_cash kind"
        )
        assert result['section'] == 'Holdings', (
            "holdings_* scope must map to Holdings section"
        )

    def test_positions_scope_with_pnl_metric_yields_static_abs_kind(self):
        """
        Scope token starts with 'positions', metric is 'pnl' (not ending in _pct)
        → kind='static_abs'.
        """
        from backend.api.algo.agent_engine import _v2_match_to_alertrow

        match = {
            'metric': 'pnl',
            'scope': 'positions_any_acct',
            'threshold': -5000,
            'value': -7500,
            'row': {'account': 'ACC1', 'pnl': -7500},
        }
        result = _v2_match_to_alertrow(match)
        assert result['kind'] == 'static_abs', (
            "positions + pnl metric must yield static_abs kind"
        )
        assert result['section'] == 'Positions', (
            "positions_* scope must map to Positions section"
        )

    def test_positions_scope_with_pnl_pct_metric_yields_static_pct_kind(self):
        """
        Metric='pnl_pct' → kind='static_pct'.
        """
        from backend.api.algo.agent_engine import _v2_match_to_alertrow

        match = {
            'metric': 'pnl_pct',
            'scope': 'positions_any_acct',
            'threshold': -10,
            'value': -15.5,
            'row': {'account': 'ACC1', 'pnl': -5000, 'pnl_percentage': -15.5},
        }
        result = _v2_match_to_alertrow(match)
        assert result['kind'] == 'static_pct', (
            "pnl_pct metric must yield static_pct kind"
        )

    def test_rate_abs_metric_yields_rate_abs_kind(self):
        """
        Metric contains '_rate_abs' → kind='rate_abs'.
        """
        from backend.api.algo.agent_engine import _v2_match_to_alertrow

        match = {
            'metric': 'pnl_rate_abs',
            'scope': 'positions_TOTAL',
            'threshold': -1000,
            'value': -1500,
            'row': {'account': 'TOTAL', 'pnl': -7500},
        }
        result = _v2_match_to_alertrow(match)
        assert result['kind'] == 'rate_abs', (
            "_rate_abs in metric name must yield rate_abs kind"
        )
        assert result['rate_val'] == -1500, (
            "rate_abs kind must populate rate_val from match value"
        )

    def test_rate_pct_metric_yields_rate_pct_kind(self):
        """
        Metric contains '_rate_pct' → kind='rate_pct'.
        """
        from backend.api.algo.agent_engine import _v2_match_to_alertrow

        match = {
            'metric': 'pnl_rate_pct',
            'scope': 'positions_TOTAL',
            'threshold': -0.5,
            'value': -0.75,
            'row': {'account': 'TOTAL'},
        }
        result = _v2_match_to_alertrow(match)
        assert result['kind'] == 'rate_pct', (
            "_rate_pct in metric name must yield rate_pct kind"
        )
        assert result['rate_val'] == -0.75

    def test_avail_margin_metric_yields_negative_margin_kind(self):
        """
        Metric='avail_margin' → kind='negative_margin'.
        """
        from backend.api.algo.agent_engine import _v2_match_to_alertrow

        match = {
            'metric': 'avail_margin',
            'scope': 'funds_any_acct',
            'threshold': -50000,
            'value': -75000,
            'row': {'account': 'TOTAL', 'net': -75000},
        }
        result = _v2_match_to_alertrow(match)
        assert result['kind'] == 'negative_margin', (
            "avail_margin metric must yield negative_margin kind"
        )

    def test_unknown_scope_prefix_yields_funds_section(self):
        """
        Scope token that doesn't start with 'holdings' or 'positions' → section='Funds'.
        """
        from backend.api.algo.agent_engine import _v2_match_to_alertrow

        match = {
            'metric': 'cash',
            'scope': 'unknown_scope',
            'threshold': -10000,
            'value': -15000,
            'row': {'account': 'TOTAL'},
        }
        result = _v2_match_to_alertrow(match)
        assert result['section'] == 'Funds', (
            "unknown scope prefix must default to Funds section"
        )

    def test_none_or_empty_scope_defaults_to_funds(self):
        """
        Scope is None or empty string → section='Funds'.
        """
        from backend.api.algo.agent_engine import _v2_match_to_alertrow

        # Test with None
        match = {
            'metric': 'cash',
            'scope': None,
            'threshold': -1000,
            'value': -2000,
            'row': {'account': 'TOTAL'},
        }
        result = _v2_match_to_alertrow(match)
        assert result['section'] == 'Funds'

        # Test with empty string
        match['scope'] = ''
        result = _v2_match_to_alertrow(match)
        assert result['section'] == 'Funds'


class TestV2MatchToAlertrowPnLExtraction:
    """Test PnL and percentage value extraction from rows."""

    def test_holdings_section_extracts_day_change_val(self):
        """
        Holdings section → pnl = day_change_val from row.
        """
        from backend.api.algo.agent_engine import _v2_match_to_alertrow

        match = {
            'metric': 'day_pct',
            'scope': 'holdings_any_acct',
            'threshold': 5,
            'value': 8.5,
            'row': {
                'account': 'ACC1',
                'day_change_val': 12500,
                'day_change_percentage': 8.5,
            },
        }
        result = _v2_match_to_alertrow(match)
        assert result['pnl'] == 12500, (
            "Holdings section must extract pnl from day_change_val"
        )
        assert result['pct'] == 8.5, (
            "Holdings section must extract pct from day_change_percentage"
        )

    def test_holdings_section_handles_missing_percentage(self):
        """
        When day_change_percentage is None or missing → pct=None.
        """
        from backend.api.algo.agent_engine import _v2_match_to_alertrow

        match = {
            'metric': 'day_val',
            'scope': 'holdings_any_acct',
            'threshold': 1000,
            'value': 5000,
            'row': {
                'account': 'ACC1',
                'day_change_val': 5000,
                'day_change_percentage': None,
            },
        }
        result = _v2_match_to_alertrow(match)
        assert result['pct'] is None, (
            "Holdings with None percentage must set pct to None"
        )

    def test_holdings_section_with_zero_percentage(self):
        """
        When day_change_percentage is 0 → pct=0.0 (falsy but convertible).
        """
        from backend.api.algo.agent_engine import _v2_match_to_alertrow

        match = {
            'metric': 'day_val',
            'scope': 'holdings_any_acct',
            'threshold': 1000,
            'value': 5000,
            'row': {
                'account': 'ACC1',
                'day_change_val': 5000,
                'day_change_percentage': 0,
            },
        }
        result = _v2_match_to_alertrow(match)
        assert result['pct'] == 0.0

    def test_positions_section_extracts_pnl_field(self):
        """
        Positions section → pnl = pnl from row, pct=None (computed later).
        """
        from backend.api.algo.agent_engine import _v2_match_to_alertrow

        match = {
            'metric': 'pnl',
            'scope': 'positions_any_acct',
            'threshold': -5000,
            'value': -8000,
            'row': {'account': 'ACC1', 'pnl': -8000, 'used_margin': 100000},
        }
        result = _v2_match_to_alertrow(match)
        assert result['pnl'] == -8000
        assert result['pct'] is None, (
            "Positions section does not compute pct from row"
        )

    def test_funds_section_with_cash_metric_uses_opening_balance(self):
        """
        Funds section + metric='cash' → pnl = 'avail opening_balance' from row.
        """
        from backend.api.algo.agent_engine import _v2_match_to_alertrow

        match = {
            'metric': 'cash',
            'scope': 'funds_margin',
            'threshold': -50000,
            'value': -100000,
            'row': {'account': 'TOTAL', 'avail opening_balance': -100000},
        }
        result = _v2_match_to_alertrow(match)
        assert result['pnl'] == -100000

    def test_funds_section_with_margin_metric_uses_net(self):
        """
        Funds section + metric='avail_margin' → pnl = 'net' from row.
        """
        from backend.api.algo.agent_engine import _v2_match_to_alertrow

        match = {
            'metric': 'avail_margin',
            'scope': 'funds_margin',
            'threshold': -50000,
            'value': -75000,
            'row': {'account': 'TOTAL', 'net': -75000},
        }
        result = _v2_match_to_alertrow(match)
        assert result['pnl'] == -75000

    def test_funds_section_fallback_uses_match_value(self):
        """
        Funds section + unmapped metric → pnl = match['value'].
        """
        from backend.api.algo.agent_engine import _v2_match_to_alertrow

        match = {
            'metric': 'some_custom_metric',
            'scope': 'funds_other',
            'threshold': 1000,
            'value': 2500,
            'row': {'account': 'TOTAL'},
        }
        result = _v2_match_to_alertrow(match)
        assert result['pnl'] == 2500


class TestV2MatchToAlertrowThresholdFormatting:
    """Test threshold string formatting by kind."""

    def test_static_pct_kind_formats_threshold_with_percent_suffix(self):
        """
        kind='static_pct' → threshold_str = "{value:.2f}%"
        """
        from backend.api.algo.agent_engine import _v2_match_to_alertrow

        match = {
            'metric': 'pnl_pct',
            'scope': 'positions_any_acct',
            'threshold': -10.567,
            'value': -15.2,
            'row': {'account': 'ACC1'},
        }
        result = _v2_match_to_alertrow(match)
        assert result['threshold'] == '-10.57%', (
            "static_pct kind must format threshold as '{:.2f}%'"
        )

    def test_rate_pct_kind_formats_threshold_with_percent_min_suffix(self):
        """
        kind='rate_pct' → threshold_str = "{value:.2f}%/min"
        """
        from backend.api.algo.agent_engine import _v2_match_to_alertrow

        match = {
            'metric': 'pnl_rate_pct',
            'scope': 'positions_TOTAL',
            'threshold': -0.5,
            'value': -0.75,
            'row': {'account': 'TOTAL'},
        }
        result = _v2_match_to_alertrow(match)
        assert result['threshold'] == '-0.50%/min', (
            "rate_pct kind must format threshold as '{:.2f}%/min'"
        )

    def test_static_abs_kind_formats_threshold_as_rupee_amount(self):
        """
        kind='static_abs' → threshold_str = "-₹{abs(value):,.0f}"
        """
        from backend.api.algo.agent_engine import _v2_match_to_alertrow

        match = {
            'metric': 'pnl',
            'scope': 'positions_any_acct',
            'threshold': -5000,
            'value': -8750,
            'row': {'account': 'ACC1', 'pnl': -8750},
        }
        result = _v2_match_to_alertrow(match)
        assert result['threshold'] == '-₹5,000', (
            "static_abs kind must format threshold as '-₹{:,.0f}'"
        )

    def test_rate_abs_kind_formats_threshold_as_rupee_per_min(self):
        """
        kind='rate_abs' → threshold_str = "-₹{abs(value):,.0f}/min"
        """
        from backend.api.algo.agent_engine import _v2_match_to_alertrow

        match = {
            'metric': 'pnl_rate_abs',
            'scope': 'positions_TOTAL',
            'threshold': -1000,
            'value': -1500.5,
            'row': {'account': 'TOTAL'},
        }
        result = _v2_match_to_alertrow(match)
        assert result['threshold'] == '-₹1,000/min', (
            "rate_abs kind must format threshold as '-₹{:,.0f}/min'"
        )

    def test_threshold_format_handles_invalid_threshold_gracefully(self):
        """
        When threshold cannot be converted to float → threshold_str = str(threshold).
        """
        from backend.api.algo.agent_engine import _v2_match_to_alertrow

        match = {
            'metric': 'pnl',
            'scope': 'positions_any_acct',
            'threshold': 'invalid_number',
            'value': -5000,
            'row': {'account': 'ACC1', 'pnl': -5000},
        }
        result = _v2_match_to_alertrow(match)
        assert result['threshold'] == 'invalid_number', (
            "Invalid threshold must fall back to str()"
        )

    def test_threshold_format_unknown_metric_defaults_to_static_abs(self):
        """
        When metric is unknown (doesn't match any special patterns),
        kind defaults to 'static_abs' and threshold_str = "-₹{abs(threshold):,.0f}"
        """
        from backend.api.algo.agent_engine import _v2_match_to_alertrow

        match = {
            'metric': 'unknown_metric',
            'scope': 'unknown_scope',
            'threshold': -42,
            'value': 99,
            'row': {'account': 'TOTAL'},
        }
        result = _v2_match_to_alertrow(match)
        # Unknown metric → kind='static_abs' → format with rupee
        assert result['threshold'] == '-₹42'


class TestV2MatchToAlertrowScopeLabel:
    """Test scope_label extraction from row account field."""

    def test_scope_label_from_row_account_field(self):
        """
        scope_label is extracted from row['account'].
        """
        from backend.api.algo.agent_engine import _v2_match_to_alertrow

        match = {
            'metric': 'pnl',
            'scope': 'positions_any_acct',
            'threshold': -5000,
            'value': -8000,
            'row': {'account': 'ZG0790', 'pnl': -8000},
        }
        result = _v2_match_to_alertrow(match)
        assert result['scope'] == 'ZG0790', (
            "scope_label must be extracted from row['account']"
        )

    def test_scope_label_defaults_to_total_when_account_missing(self):
        """
        When row['account'] is missing → scope_label='TOTAL'.
        """
        from backend.api.algo.agent_engine import _v2_match_to_alertrow

        match = {
            'metric': 'pnl',
            'scope': 'positions_any_acct',
            'threshold': -5000,
            'value': -8000,
            'row': {'pnl': -8000},
        }
        result = _v2_match_to_alertrow(match)
        assert result['scope'] == 'TOTAL'


class TestV2MatchToAlertrowUnderlyingBreakdown:
    """Test optional enrichment: per-underlying breakdown."""

    def test_underlying_breakdown_when_positions_and_df_provided(self):
        """
        When section='Positions' and df_positions is provided and the setting
        is enabled → underlyings_breakdown is populated via the helper function.
        """
        from backend.api.algo.agent_engine import _v2_match_to_alertrow
        import pandas as pd

        # Mock df_positions with some underlying data
        df_positions = pd.DataFrame({
            'account': ['TOTAL', 'TOTAL', 'TOTAL'],
            'symbol': ['NIFTY', 'BANKNIFTY', 'FINNIFTY'],
            'pnl': [-15000, -8000, -3000],
        })

        match = {
            'metric': 'pnl',
            'scope': 'positions_TOTAL',
            'threshold': -5000,
            'value': -25000,
            'row': {'account': 'TOTAL', 'pnl': -25000},
        }

        with patch('backend.shared.helpers.settings.get_bool', return_value=True), \
             patch('backend.shared.helpers.summarise.breakdown_positions_by_underlying',
                   return_value=[
                       {'symbol': 'NIFTY', 'pnl': -15000},
                       {'symbol': 'BANKNIFTY', 'pnl': -8000},
                   ]):
            result = _v2_match_to_alertrow(
                match, df_positions=df_positions, alert_state={}
            )
        assert len(result['underlyings_breakdown']) == 2
        assert result['underlyings_breakdown'][0]['symbol'] == 'NIFTY'

    def test_no_breakdown_when_section_not_positions(self):
        """
        When section != 'Positions' → underlyings_breakdown is empty.
        """
        from backend.api.algo.agent_engine import _v2_match_to_alertrow

        match = {
            'metric': 'day_val',
            'scope': 'holdings_TOTAL',
            'threshold': 1000,
            'value': 5000,
            'row': {'account': 'TOTAL', 'day_change_val': 5000},
        }
        result = _v2_match_to_alertrow(match)
        assert result['underlyings_breakdown'] == [], (
            "Non-Positions sections must have empty breakdown"
        )

    def test_no_breakdown_when_df_positions_is_none(self):
        """
        When df_positions=None → underlyings_breakdown is empty.
        """
        from backend.api.algo.agent_engine import _v2_match_to_alertrow

        match = {
            'metric': 'pnl',
            'scope': 'positions_TOTAL',
            'threshold': -5000,
            'value': -8000,
            'row': {'account': 'TOTAL', 'pnl': -8000},
        }
        result = _v2_match_to_alertrow(match, df_positions=None)
        assert result['underlyings_breakdown'] == []

    def test_breakdown_helper_exception_logged_gracefully(self):
        """
        When the breakdown helper raises an exception → logged and
        underlyings_breakdown remains empty (not re-raised).
        """
        from backend.api.algo.agent_engine import _v2_match_to_alertrow
        import pandas as pd

        df_positions = pd.DataFrame({'symbol': ['TEST']})

        match = {
            'metric': 'pnl',
            'scope': 'positions_TOTAL',
            'threshold': -5000,
            'value': -8000,
            'row': {'account': 'TOTAL', 'pnl': -8000},
        }

        with patch('backend.shared.helpers.settings.get_bool', return_value=True), \
             patch('backend.shared.helpers.summarise.breakdown_positions_by_underlying',
                   side_effect=ValueError("Breakdown failed")):
            result = _v2_match_to_alertrow(match, df_positions=df_positions)
        assert result['underlyings_breakdown'] == []


class TestV2MatchToAlertrowRateEnrichment:
    """Test optional enrichment: rate-of-loss in static alerts."""

    def test_rate_enrichment_for_static_pct_when_alert_state_available(self):
        """
        When section='Positions', kind='static_pct', and alert_state has
        sufficient pnl_history → rate_val is computed and populated.
        """
        from backend.api.algo.agent_engine import _v2_match_to_alertrow

        now = datetime(2026, 7, 11, 10, 30, 0, tzinfo=timezone.utc)
        old_ts = now - timedelta(minutes=5)

        alert_state = {
            'pnl_history': {
                ('positions', 'TOTAL'): [
                    (old_ts, -100000, -5.0),  # (timestamp, pnl_abs, pnl_pct)
                    (now, -110000, -5.5),
                ]
            }
        }

        match = {
            'metric': 'pnl_pct',
            'scope': 'positions_TOTAL',
            'threshold': -10,
            'value': -5.5,
            'row': {'account': 'TOTAL', 'pnl': -110000},
        }

        with patch('backend.shared.helpers.settings.get_bool', return_value=True):
            result = _v2_match_to_alertrow(
                match,
                alert_state=alert_state,
                rate_window_min=10,
            )
        # rate_val = (latest_pnl - oldest_pnl) / minutes
        # = (-110000 - -100000) / 5 = -10000 / 5 = -2000/min
        assert result['rate_val'] == -2000.0, (
            "rate_val must be computed as (latest - oldest) / minutes"
        )

    def test_no_rate_enrichment_when_rate_already_populated(self):
        """
        When kind='rate_pct' or 'rate_abs' → rate_val is already set,
        so no enrichment computation happens.
        """
        from backend.api.algo.agent_engine import _v2_match_to_alertrow

        alert_state = {
            'pnl_history': {
                ('positions', 'TOTAL'): [
                    (datetime.now(timezone.utc), -100000, -5.0),
                    (datetime.now(timezone.utc) + timedelta(minutes=5), -110000, -5.5),
                ]
            }
        }

        match = {
            'metric': 'pnl_rate_pct',
            'scope': 'positions_TOTAL',
            'threshold': -0.5,
            'value': -0.1,  # rate value already set
            'row': {'account': 'TOTAL'},
        }

        with patch('backend.shared.helpers.settings.get_bool', return_value=True):
            result = _v2_match_to_alertrow(match, alert_state=alert_state)
        assert result['rate_val'] == -0.1, (
            "rate_val must not be recomputed when already set from rate metric"
        )

    def test_no_rate_enrichment_when_settings_disabled(self):
        """
        When alerts.show_rate_in_static_alerts setting is False → no enrichment.
        """
        from backend.api.algo.agent_engine import _v2_match_to_alertrow

        now = datetime(2026, 7, 11, 10, 30, 0, tzinfo=timezone.utc)
        old_ts = now - timedelta(minutes=5)

        alert_state = {
            'pnl_history': {
                ('positions', 'TOTAL'): [
                    (old_ts, -100000, -5.0),
                    (now, -110000, -5.5),
                ]
            }
        }

        match = {
            'metric': 'pnl_pct',
            'scope': 'positions_TOTAL',
            'threshold': -10,
            'value': -5.5,
            'row': {'account': 'TOTAL', 'pnl': -110000},
        }

        with patch('backend.shared.helpers.settings.get_bool', return_value=False):
            result = _v2_match_to_alertrow(
                match,
                alert_state=alert_state,
                rate_window_min=10,
            )
        assert result['rate_val'] is None, (
            "rate_val must remain None when setting is disabled"
        )

    def test_no_rate_enrichment_with_insufficient_history(self):
        """
        When alert_state has < 2 samples → rate_val stays None.
        """
        from backend.api.algo.agent_engine import _v2_match_to_alertrow

        alert_state = {
            'pnl_history': {
                ('positions', 'TOTAL'): [
                    (datetime.now(timezone.utc), -100000, -5.0),
                ]
            }
        }

        match = {
            'metric': 'pnl_pct',
            'scope': 'positions_TOTAL',
            'threshold': -10,
            'value': -5.5,
            'row': {'account': 'TOTAL', 'pnl': -110000},
        }

        with patch('backend.shared.helpers.settings.get_bool', return_value=True):
            result = _v2_match_to_alertrow(
                match,
                alert_state=alert_state,
                rate_window_min=10,
            )
        assert result['rate_val'] is None, (
            "rate_val must be None with < 2 history samples"
        )

    def test_rate_enrichment_exception_logged_gracefully(self):
        """
        When rate computation raises an exception → logged and rate_val stays None.
        """
        from backend.api.algo.agent_engine import _v2_match_to_alertrow

        alert_state = {
            'pnl_history': {
                ('positions', 'TOTAL'): [
                    ('invalid', -100000, -5.0),  # Invalid timestamp
                    ('also_invalid', -110000, -5.5),
                ]
            }
        }

        match = {
            'metric': 'pnl_pct',
            'scope': 'positions_TOTAL',
            'threshold': -10,
            'value': -5.5,
            'row': {'account': 'TOTAL', 'pnl': -110000},
        }

        with patch('backend.shared.helpers.settings.get_bool', return_value=True):
            result = _v2_match_to_alertrow(
                match,
                alert_state=alert_state,
                rate_window_min=10,
            )
        assert result['rate_val'] is None
