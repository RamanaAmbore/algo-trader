"""
Tests for the partial-outage guard (2026-09 alerts audit item 5).

Bug: `_fetch_positions_direct` (background.py) already raised on a FULL
outage (`_is_positions_outage` — all accounts failed) via `_is_positions_outage`,
which is correct and untouched. The gap was the PARTIAL case: some (not
all) accounts' frames carry `attrs['fetch_failed']=True` — `pd.concat`
silently drops that account's (non-)contribution from the resulting
`raw`/`summary` totals, so the TOTAL row understates the real P&L as if
the missing account had simply never existed. Two consumers read this
distorted TOTAL:
  1. `grammar._scope_positions_total` — feeds the `loss-positions-total`
     static day_val leaf straight from `sum_positions`'s TOTAL row.
  2. `agent_engine._update_pnl_history` — records that TOTAL row into
     `alert_state['pnl_history'][('positions', 'TOTAL')]`, corrupting a
     FUTURE tick's rate-of-change window.

Fix: `_positions_partial_outage_accounts()` (positions.py) detects the
partial shape; `_fetch_positions_direct` / `_rebuild_positions_summary`
carry it forward as a `.attrs['partial_outage']` flag (never a tuple-
shape change) on both the raw and summary frames. Both consumers above
check the attr and skip/freeze the TOTAL scope for that tick, while
leaving healthy per-account rows/buckets completely unaffected.

Five quality dimensions:
  SSOT        — single `_positions_partial_outage_accounts` helper,
                reused (not re-implemented) by both consumers via the
                shared `.attrs` flag
  Correctness — full-outage still raises; partial sets the flag; clean
                case carries no flag at all
  Reachability — the real chain (fetch -> rebuild -> scope / pnl_history)
                is exercised, not a hand-set attrs shortcut
  Reuse       — per-account (non-TOTAL) rows/buckets are proven
                unaffected — the regression guard that shows only TOTAL
                was frozen
  UX          — a fully-recovered/absent observation this tick is never
                misread as "condition recovered" (fix #5's existing
                contract), confirmed by asserting NO observation is
                recorded for the positions.total scope
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest


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
# grammar._scope_positions_total
# ---------------------------------------------------------------------------

class TestScopePositionsTotalPartialOutageGuard:
    @pytest.fixture(autouse=True)
    def _wire_registry(self, monkeypatch):
        """REGISTRY is normally populated from the DB at app startup
        (`REGISTRY.reload()`); wire the real grammar.py resolvers in
        directly (auto-restored after each test) so `evaluate()` is
        exercised end-to-end without a DB session — same pattern as
        test_agent_evaluator.py."""
        from backend.api.algo.grammar_registry import REGISTRY
        from backend.api.algo.grammar import _metric_day_val, _scope_positions_total, OPERATORS

        monkeypatch.setitem(REGISTRY.metrics, 'day_val', _metric_day_val)
        monkeypatch.setitem(REGISTRY.scopes, 'positions.total', _scope_positions_total)
        monkeypatch.setitem(REGISTRY.operators, '<=', OPERATORS['<='])

    def test_returns_empty_when_partial_outage_flagged(self):
        from backend.api.algo.grammar import _scope_positions_total

        class _Ctx:
            sum_positions = _partial_outage_summary()

        assert _scope_positions_total(_Ctx()) == [], (
            "positions.total scope must return no rows when the summary "
            "carries a partial_outage flag — a distorted TOTAL must never "
            "be evaluated against any leaf this tick"
        )

    def test_returns_total_row_when_clean(self):
        from backend.api.algo.grammar import _scope_positions_total

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
# grammar._scope_positions_total — rate-limited visibility log (audit
# fix #3): a persistent single-account failure silently disabled EVERY
# positions.total loss/ROC alert for as long as it lasted, with nothing
# logged anywhere. Must log, but rate-limited (not every tick).
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
        from backend.api.algo.grammar import _scope_positions_total

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
        from backend.api.algo.grammar import _scope_positions_total

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
        from backend.api.algo.grammar import _scope_positions_total

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
        from backend.api.algo.grammar import _scope_positions_total

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
        from backend.api.algo.grammar import _scope_positions_total

        self._reset_log_state()
        records, target_logger, handler = self._collect_logs()
        try:
            _scope_positions_total(type("Ctx", (), {"sum_positions": _clean_summary()})())
            assert records == []
        finally:
            target_logger.removeHandler(handler)
            self._reset_log_state()


# ---------------------------------------------------------------------------
# agent_engine._update_pnl_history
# ---------------------------------------------------------------------------

class TestUpdatePnlHistoryPartialOutageGuard:
    def test_total_sample_skipped_but_healthy_account_sample_kept(self):
        from backend.api.algo.agent_engine import _update_pnl_history

        alert_state: dict = {}
        now = datetime(2026, 9, 26, 10, 0, 0, tzinfo=timezone.utc)
        market_state = {"nse_open": True, "mcx_open": False}

        _update_pnl_history(
            alert_state, now, _partial_outage_summary(), None,
            market_state=market_state,
        )

        hist = alert_state.get('pnl_history', {})
        assert ('positions', 'TOTAL') not in hist, (
            "TOTAL bucket must get NO sample this tick when partial_outage "
            "is flagged — recording it would poison a future rate-of-change "
            "window with a distorted (missing-account) total"
        )
        assert ('positions', 'ACCT_A') in hist, (
            "The healthy account's own bucket must still get its sample — "
            "only TOTAL is frozen, not the whole positions section"
        )
        assert hist[('positions', 'ACCT_A')][-1][1] == pytest.approx(500.0)

    def test_total_sample_recorded_when_clean(self):
        """Regression guard — TOTAL must still get its sample in the
        normal (no partial outage) case."""
        from backend.api.algo.agent_engine import _update_pnl_history

        alert_state: dict = {}
        now = datetime(2026, 9, 26, 10, 0, 0, tzinfo=timezone.utc)
        market_state = {"nse_open": True, "mcx_open": False}

        _update_pnl_history(
            alert_state, now, _clean_summary(), None,
            market_state=market_state,
        )

        hist = alert_state.get('pnl_history', {})
        assert ('positions', 'TOTAL') in hist
        assert hist[('positions', 'TOTAL')][-1][1] == pytest.approx(500.0)


# ---------------------------------------------------------------------------
# Audit follow-up: 3 additional consumers of the understated TOTAL that
# the original fix (this file's earlier tests) did NOT reach.
#   1. summarise_positions (shared/helpers/summarise.py) — used by the
#      manual/dry-run agent-fire path (routes/agents.py) AND the
#      close-summary rebuild (background.py) — dropped .attrs via
#      pd.concat with a freshly-built TOTAL row.
#   2. background._perf_append_intraday_equity — feeds
#      auth._compute_firm_nav's NavCard Day/Cum P&L via the
#      _intraday_equity deque.
#   3. alert_utils.send_summary — open/close Telegram/email summaries.
# ---------------------------------------------------------------------------

class TestSummarisePositionsPreservesPartialOutageAttr:
    def test_attrs_survive_the_concat(self):
        from backend.shared.helpers.summarise import summarise_positions

        raw = pd.DataFrame([
            {'account': 'ACCT_A', 'pnl': 1000.0},
        ])
        raw.attrs['partial_outage'] = ['ACCT_FAILED']

        result = summarise_positions(raw)

        assert result.attrs.get('partial_outage') == ['ACCT_FAILED'], (
            "summarise_positions must propagate the source frame's "
            ".attrs onto its returned TOTAL-appended frame — pd.concat "
            "with a freshly-built TOTAL row (empty attrs) silently wipes "
            "attrs unless explicitly re-applied"
        )
        # Regression guard — TOTAL row + real aggregation still correct.
        total_row = result.loc[result['account'] == 'TOTAL'].iloc[0]
        assert total_row['pnl'] == pytest.approx(1000.0)

    def test_no_attrs_when_clean(self):
        from backend.shared.helpers.summarise import summarise_positions

        raw = pd.DataFrame([{'account': 'ACCT_A', 'pnl': 1000.0}])
        result = summarise_positions(raw)
        assert not result.attrs.get('partial_outage')


class TestPerfAppendIntradayEquitySkipsOnPartialOutage:
    def test_skips_append_and_logs_when_flagged(self):
        import logging
        from backend.api import background as bg

        bg._intraday_equity.clear()
        bg._intraday_equity_date = None
        sentinel = ("2026-09-25T10:00:00+05:30", 5000.0, 20000.0, 15000.0, 5000.0, 5000.0, 5000.0)
        bg._intraday_equity.append(sentinel)
        bg._intraday_equity_date = datetime(2026, 9, 26).date()

        # background.py's logger (ramboq_logger) sets propagate=False and
        # routes records through an async QueueHandler — caplog's default
        # root-logger capture never sees it. Attach a plain in-memory
        # handler directly to the named logger instead.
        records: list = []

        class _CollectHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        target_logger = logging.getLogger("backend.api.background")
        handler = _CollectHandler()
        target_logger.addHandler(handler)
        try:
            bg._perf_append_intraday_equity(
                _clean_summary(), _partial_outage_summary(),
                datetime(2026, 9, 26, 10, 5, 0), datetime(2026, 9, 26).date(),
            )

            assert list(bg._intraday_equity) == [sentinel], (
                "A partial-outage tick must NOT append a new (understated) "
                "point — the deque must freeze at the last good point, "
                "since auth._compute_firm_nav prefers this deque for "
                "NavCard's Day/Cum P&L"
            )
            joined = " ".join(r.getMessage() for r in records)
            assert "PARTIAL-OUTAGE" in joined, (
                f"Expected a [PARTIAL-OUTAGE] warning log when the append "
                f"is skipped, got: {joined!r}"
            )
        finally:
            target_logger.removeHandler(handler)
            bg._intraday_equity.clear()
            bg._intraday_equity_date = None

    def test_appends_normally_when_clean(self):
        from backend.api import background as bg

        bg._intraday_equity.clear()
        bg._intraday_equity_date = datetime(2026, 9, 26).date()
        try:
            bg._perf_append_intraday_equity(
                _clean_summary(), _clean_summary(),
                datetime(2026, 9, 26, 10, 5, 0), datetime(2026, 9, 26).date(),
            )
            assert len(bg._intraday_equity) == 1, (
                "Regression guard — the clean (no partial outage) case "
                "must still append normally"
            )
        finally:
            bg._intraday_equity.clear()
            bg._intraday_equity_date = None


class TestSendSummaryWarnsOnPartialOutage:
    def test_telegram_and_email_carry_warning_when_flagged(self):
        from backend.shared.helpers.alert_utils import send_summary

        captured = {}

        def _fake_dispatch(msg_type, ist_display, tg_table, email_html, subject_detail, **kw):
            captured['tg_table'] = tg_table
            captured['email_html'] = email_html

        import backend.shared.helpers.alert_utils as au
        from unittest.mock import patch as _patch

        with _patch.object(au, "_dispatch", side_effect=_fake_dispatch):
            send_summary(
                pd.DataFrame(), _partial_outage_summary(), "26-Sep-26 10:00",
                "open", label="Equity",
            )

        assert "PARTIAL OUTAGE" in captured['tg_table']
        assert "ACCT_FAILED" in captured['tg_table']
        assert "PARTIAL OUTAGE" in captured['email_html']

    def test_no_warning_when_clean(self):
        from backend.shared.helpers.alert_utils import send_summary
        import backend.shared.helpers.alert_utils as au
        from unittest.mock import patch as _patch

        captured = {}

        def _fake_dispatch(msg_type, ist_display, tg_table, email_html, subject_detail, **kw):
            captured['tg_table'] = tg_table
            captured['email_html'] = email_html

        with _patch.object(au, "_dispatch", side_effect=_fake_dispatch):
            send_summary(
                pd.DataFrame(), _clean_summary(), "26-Sep-26 10:00",
                "open", label="Equity",
            )

        assert "PARTIAL OUTAGE" not in captured['tg_table']
        assert "PARTIAL OUTAGE" not in captured['email_html']
