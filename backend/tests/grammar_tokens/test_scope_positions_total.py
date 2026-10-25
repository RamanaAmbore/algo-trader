"""Tests for grammar_tokens/scope_positions_total.py —
`positions.total` scope.

Moved out of test_positions_partial_outage_guard.py's
TestScopePositionsTotalPartialOutageGuard and
TestPartialOutageSuppressionLogging (Wave 1, scope batch C of the DSL
token-unification plan) — pure resolver-level tests for
`_scope_positions_total` and its shared `_log_partial_outage_suppression`
helper (which stays in grammar.py). The other consumers of the
partial-outage `.attrs` flag (agent_engine._update_pnl_history,
summarise.py, background.py, alert_utils.py) stay covered by
test_positions_partial_outage_guard.py — this file is scoped to the
resolver itself only.

See the module docstring in test_positions_partial_outage_guard.py for
the full bug/fix narrative (2026-09 alerts audit item 5).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from backend.api.algo.grammar_tokens.scope_positions_total import (
    _scope_positions_total,
)


def _partial_outage_summary() -> "pd.DataFrame":
    """A positions summary frame shaped like the real post-
    `_rebuild_positions_summary` output, flagged with partial_outage —
    ACCT_A is healthy, ACCT_FAILED is missing (never contributed a row)."""
    df = pd.DataFrame([
        {'account': 'ACCT_A', 'pnl': 1000.0, 'day_change_val': 500.0,
         'day_change_percentage': 5.0},
        {'account': 'TOTAL', 'pnl': 1000.0, 'day_change_val': 500.0,
         'day_change_percentage': 5.0},
    ])
    df.attrs['partial_outage'] = ['ACCT_FAILED']
    return df


def _clean_summary() -> "pd.DataFrame":
    df = pd.DataFrame([
        {'account': 'ACCT_A', 'pnl': 1000.0, 'day_change_val': 500.0,
         'day_change_percentage': 5.0},
        {'account': 'TOTAL', 'pnl': 1000.0, 'day_change_val': 500.0,
         'day_change_percentage': 5.0},
    ])
    return df


# ---------------------------------------------------------------------------
# grammar_tokens.scope_positions_total._scope_positions_total
# ---------------------------------------------------------------------------

class TestScopePositionsTotalPartialOutageGuard:
    @pytest.fixture(autouse=True)
    def _wire_registry(self, monkeypatch):
        """REGISTRY is normally populated from the DB at app startup
        (`REGISTRY.reload()`); wire the real resolver in directly
        (auto-restored after each test) so `evaluate()` is exercised
        end-to-end without a DB session — same pattern as
        test_agent_evaluator.py."""
        from backend.api.algo.grammar_registry import REGISTRY
        from backend.api.algo.grammar import OPERATORS
        from backend.api.algo.grammar_tokens.metric_day_val import _metric_day_val

        monkeypatch.setitem(REGISTRY.metrics, 'day_val', _metric_day_val)
        monkeypatch.setitem(REGISTRY.scopes, 'positions.total', _scope_positions_total)
        monkeypatch.setitem(REGISTRY.operators, '<=', OPERATORS['<='])

    def test_returns_empty_when_partial_outage_flagged(self):
        class _Ctx:
            sum_positions = _partial_outage_summary()

        assert _scope_positions_total(_Ctx()) == [], (
            "positions.total scope must return no rows when the summary "
            "carries a partial_outage flag — a distorted TOTAL must never "
            "be evaluated against any leaf this tick"
        )

    def test_returns_total_row_when_clean(self):
        class _Ctx:
            sum_positions = _clean_summary()

        rows = _scope_positions_total(_Ctx())
        assert len(rows) == 1
        assert rows[0]['account'] == 'TOTAL'

    def test_no_observation_recorded_when_partial_outage(self):
        """End-to-end through the real evaluator: a leaf scoped to
        positions.total must produce ZERO observations when partial
        outage is flagged — confirming fix #5's 'absent from
        observations = untouched, never recovered' contract actually
        applies here (not just that the scope returns [])."""
        from backend.api.algo.agent_evaluator import Context, evaluate

        ctx = Context(sum_positions=_partial_outage_summary(), now=datetime.now(timezone.utc))
        cond = {"metric": "day_val", "scope": "positions.total", "op": "<=", "value": -30000}
        matches = evaluate(cond, ctx)

        assert matches == []
        assert ctx.observations == [], (
            f"Expected zero observations for positions.total during partial "
            f"outage, got {ctx.observations}"
        )

    def test_observation_recorded_when_clean(self):
        """Regression guard — the clean case must still record an
        observation (proving the partial-outage test above is actually
        exercising the guard, not some unrelated evaluator no-op)."""
        from backend.api.algo.agent_evaluator import Context, evaluate

        ctx = Context(sum_positions=_clean_summary(), now=datetime.now(timezone.utc))
        cond = {"metric": "day_val", "scope": "positions.total", "op": "<=", "value": -30000}
        evaluate(cond, ctx)

        assert len(ctx.observations) == 1
        assert ctx.observations[0]['account'] == 'TOTAL'


# ---------------------------------------------------------------------------
# grammar._log_partial_outage_suppression — rate-limited visibility log
# (audit fix #3): a persistent single-account failure silently disabled
# EVERY positions.total loss/ROC alert for as long as it lasted, with
# nothing logged anywhere. Must log, but rate-limited (not every tick).
# ---------------------------------------------------------------------------

class TestPartialOutageSuppressionLogging:
    def _reset_log_state(self):
        from backend.api.algo import grammar
        grammar._last_partial_outage_log["ts"] = None
        grammar._last_partial_outage_log["accounts"] = None

    def _collect_logs(self):
        import logging
        records: list = []

        class _CollectHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        target_logger = logging.getLogger("backend.api.algo.grammar")
        handler = _CollectHandler()
        target_logger.addHandler(handler)
        return records, target_logger, handler

    def test_logs_on_first_suppression(self):
        self._reset_log_state()
        records, target_logger, handler = self._collect_logs()
        try:
            _scope_positions_total(type("Ctx", (), {"sum_positions": _partial_outage_summary()})())
            joined = " ".join(r.getMessage() for r in records)
            assert "PARTIAL-OUTAGE" in joined
            assert "ACCT_FAILED" in joined
        finally:
            target_logger.removeHandler(handler)
            self._reset_log_state()

    def test_does_not_log_again_within_rate_limit_window(self):
        self._reset_log_state()
        records, target_logger, handler = self._collect_logs()
        try:
            _scope_positions_total(type("Ctx", (), {"sum_positions": _partial_outage_summary()})())
            assert len(records) == 1

            # Same tick, same failed accounts, well within the 15-min
            # rate-limit window — must NOT log again.
            _scope_positions_total(type("Ctx", (), {"sum_positions": _partial_outage_summary()})())
            _scope_positions_total(type("Ctx", (), {"sum_positions": _partial_outage_summary()})())
            assert len(records) == 1, (
                f"Expected exactly 1 log line across 3 consecutive "
                f"suppressed cycles (rate-limited), got {len(records)}"
            )
        finally:
            target_logger.removeHandler(handler)
            self._reset_log_state()

    def test_logs_again_after_rate_limit_interval_elapses(self):
        from backend.api.algo import grammar

        self._reset_log_state()
        records, target_logger, handler = self._collect_logs()
        try:
            _scope_positions_total(type("Ctx", (), {"sum_positions": _partial_outage_summary()})())
            assert len(records) == 1

            # Fast-forward past the rate-limit interval.
            grammar._last_partial_outage_log["ts"] -= (
                grammar._PARTIAL_OUTAGE_LOG_INTERVAL_MIN * 60 + 1
            )
            _scope_positions_total(type("Ctx", (), {"sum_positions": _partial_outage_summary()})())
            assert len(records) == 2
        finally:
            target_logger.removeHandler(handler)
            self._reset_log_state()

    def test_logs_immediately_when_failed_account_set_changes(self):
        """A NEW failure must never be masked by a still-cooling-down
        rate limit left over from an OLDER failure."""
        self._reset_log_state()
        records, target_logger, handler = self._collect_logs()
        try:
            _scope_positions_total(type("Ctx", (), {"sum_positions": _partial_outage_summary()})())
            assert len(records) == 1

            df2 = pd.DataFrame([
                {'account': 'ACCT_A', 'pnl': 1000.0},
                {'account': 'TOTAL', 'pnl': 1000.0},
            ])
            df2.attrs['partial_outage'] = ['ACCT_DIFFERENT']
            _scope_positions_total(type("Ctx", (), {"sum_positions": df2})())
            assert len(records) == 2, (
                "A different failed-account set must log immediately, "
                "not wait out the rate-limit window from the prior failure"
            )
        finally:
            target_logger.removeHandler(handler)
            self._reset_log_state()

    def test_no_log_when_clean(self):
        self._reset_log_state()
        records, target_logger, handler = self._collect_logs()
        try:
            _scope_positions_total(type("Ctx", (), {"sum_positions": _clean_summary()})())
            assert records == []
        finally:
            target_logger.removeHandler(handler)
            self._reset_log_state()
