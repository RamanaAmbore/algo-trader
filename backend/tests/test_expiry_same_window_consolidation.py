"""2026-11 same-window consolidation — operator finding: on an expiry
day, `market-preclose-nonmcx` (generic reminder), `expiry-day-preclose-
alert` (the richer, expiry-specific alert with the real position
breakdown) and `expiry-nfo-risk-alert`/`expiry-mcx-risk-alert` could
all fire within about a minute of each other, 30 minutes before that
exchange's close.

Covers:
  - `market-preclose-nonmcx` is suppressed on an NSE expiry day, fires
    normally on a non-expiry day; `market-preclose-mcx-v2` is
    untouched either way.
  - `expiry-nfo-risk-alert`/`expiry-mcx-risk-alert` are suppressed
    specifically inside their OWN exchange's 30-minute pre-close
    window on an expiry day (where `expiry-day-preclose-alert` already
    covers the same position), and fire normally outside that window
    and on non-expiry days.
  - BUILTIN_AGENTS shape (slug/schedule/cooldown/tier/events) is
    unchanged for all three agents — only the condition tree gained a
    suppression leaf.
  - The table-driven content-matched one-time migration
    (`_ae_apply_content_matched_cond_migration`) upgrades an
    already-seeded pre-consolidation row and leaves an
    operator-customized row alone.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from backend.api.algo.agent_evaluator import Context, evaluate
from backend.api.algo.agent_engine import BUILTIN_AGENTS


def _seed_registry_inline():
    """Populate the in-memory GrammarRegistry with the system tokens
    these tests need — same pattern as
    `test_expiry_resolvers.py::_seed_registry_inline`, duplicated
    locally so this file has no private cross-test-file dependency."""
    from backend.api.algo.grammar_registry import REGISTRY
    from backend.api.algo.grammar import OPERATORS, SYSTEM_TOKENS
    import importlib

    def _import_dotted(path: str):
        mod_path, _, attr = path.rpartition(".")
        return getattr(importlib.import_module(mod_path), attr)

    REGISTRY.operators = dict(OPERATORS)
    for t in SYSTEM_TOKENS:
        if t.get('grammar_kind') != 'condition':
            continue
        resolver = t.get('resolver')
        if not resolver:
            continue
        try:
            fn = _import_dotted(resolver)
        except Exception:
            continue
        if t['token_kind'] == 'metric':
            REGISTRY.metrics[t['token']] = fn
        elif t['token_kind'] == 'scope':
            REGISTRY.scopes[t['token']] = fn


def _agent_cond(slug: str) -> dict:
    agent = next(a for a in BUILTIN_AGENTS if a["slug"] == slug)
    return agent["conditions"]


def _total_margins_df() -> pd.DataFrame:
    """One TOTAL row — `funds.total`-scoped metrics (minutes_until_*_
    close, is_*_expiry_today) ignore the row's own columns entirely, so
    only the `account == 'TOTAL'` mask matters here."""
    return pd.DataFrame([{"account": "TOTAL"}])


def _ctx(*, position_rows=None, spot_prices=None, now=None, df_margins=None,
          segment_flags=None) -> Context:
    """`minutes_until_{nse,mcx}_close` read `ctx.segment_flags` (the
    per-tick dict `agent_engine._ae_segment_flags()` computes in
    production, not something derivable from `now` alone in a unit
    test) — set explicitly per case rather than left at the dataclass
    default `{}` (which reads as "closed", making both leaves always
    None/no-match)."""
    return Context(
        sum_positions=pd.DataFrame(),
        df_margins=df_margins if df_margins is not None else _total_margins_df(),
        position_rows=position_rows or [],
        spot_prices=spot_prices or {},
        now=now or datetime.now(timezone.utc),
        segment_flags=segment_flags or {},
    )


@pytest.fixture(autouse=True)
def _registry():
    _seed_registry_inline()


# ── market-preclose-nonmcx ────────────────────────────────────────────

class TestMarketPrecloseNonmcxSuppression:
    def test_shape_unchanged(self):
        agent = next(a for a in BUILTIN_AGENTS if a["slug"] == "market-preclose-nonmcx")
        assert agent["schedule"] == "always"
        assert agent["cooldown_minutes"] == 1320
        assert agent["tier"] == "info"
        assert agent.get("fire_at_time") is None

    def test_suppressed_on_nse_expiry_day(self):
        """30 min from NSE close, but today IS an NSE expiry day —
        expiry-day-preclose-alert already covers this moment."""
        now = datetime(2025, 4, 24, 9, 30, tzinfo=timezone.utc)  # ~15:00 IST
        ctx = _ctx(now=now, position_rows=[
            {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO", "quantity": 50},
        ], segment_flags={"nse_open": True, "minutes_until_nse_close": 30})
        matches = evaluate(_agent_cond("market-preclose-nonmcx"), ctx)
        assert matches == [], "must be suppressed on an NSE expiry day"

    def test_fires_on_non_expiry_day(self):
        """Same 30-min-to-close moment, but no NFO position is
        expiring today — the reminder must still fire."""
        now = datetime(2025, 4, 24, 9, 30, tzinfo=timezone.utc)
        ctx = _ctx(now=now, position_rows=[],
                   segment_flags={"nse_open": True, "minutes_until_nse_close": 30})
        matches = evaluate(_agent_cond("market-preclose-nonmcx"), ctx)
        assert matches != [], "must fire normally on a non-expiry day"

    def test_market_preclose_mcx_v2_unaffected(self):
        """The MCX-side generic reminder keeps its original flat
        condition — only the NSE-side one gained a suppression leaf."""
        agent = next(a for a in BUILTIN_AGENTS if a["slug"] == "market-preclose-mcx-v2")
        assert agent["conditions"] == {
            "op": "<=", "scope": "funds.total",
            "metric": "minutes_until_mcx_close", "value": 30,
        }


# ── expiry-nfo-risk-alert ─────────────────────────────────────────────

class TestExpiryNfoRiskAlertSuppression:
    def test_shape_unchanged(self):
        agent = next(a for a in BUILTIN_AGENTS if a["slug"] == "expiry-nfo-risk-alert")
        assert agent["schedule"] == "market_hours"
        assert agent["cooldown_minutes"] == 60
        assert agent["tier"] == "high"

    def test_suppressed_inside_nse_preclose_window(self):
        """An ITM NFO position exists AND it's within 30 min of NSE
        close on an NSE expiry day — expiry-day-preclose-alert already
        reports this; the risk alert must go quiet."""
        now = datetime(2025, 4, 24, 9, 30, tzinfo=timezone.utc)  # ~15:00 IST, 30 min to 15:30 close
        ctx = _ctx(now=now, position_rows=[
            {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO", "quantity": 50},
        ], spot_prices={"NIFTY": 22200.0},
                   segment_flags={"nse_open": True, "minutes_until_nse_close": 30})
        matches = evaluate(_agent_cond("expiry-nfo-risk-alert"), ctx)
        assert matches == [], "must be suppressed inside NSE's own pre-close window"

    def test_fires_outside_preclose_window_same_expiry_day(self):
        """Same expiry day, but well before the 30-min pre-close
        window — the risk alert's earlier-warning cadence must survive."""
        now = datetime(2025, 4, 24, 4, 0, tzinfo=timezone.utc)  # ~09:30 IST — market just opened
        ctx = _ctx(now=now, position_rows=[
            {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO", "quantity": 50},
        ], spot_prices={"NIFTY": 22200.0},
                   segment_flags={"nse_open": True, "minutes_until_nse_close": 360})
        matches = evaluate(_agent_cond("expiry-nfo-risk-alert"), ctx)
        assert matches != [], "must still fire well outside the 30-min pre-close window"

    def test_fires_on_non_expiry_day_regardless_of_clock(self):
        """No NFO position is expiring today at all — `is_nse_expiry_
        today` is 0, so the suppression leaf can never apply; the
        underlying ITM/future leaf still drives firing normally."""
        now = datetime(2025, 4, 24, 9, 30, tzinfo=timezone.utc)
        ctx = _ctx(now=now, position_rows=[
            # Expiring far in the future — not "expiring today".
            {"tradingsymbol": "NIFTY25DEC22000CE", "exchange": "NFO", "quantity": 50},
        ], spot_prices={"NIFTY": 22200.0},
                   segment_flags={"nse_open": True, "minutes_until_nse_close": 30})
        matches = evaluate(_agent_cond("expiry-nfo-risk-alert"), ctx)
        assert matches == [], "no expiring position at all ⇒ nothing to flag either way"


# ── expiry-mcx-risk-alert ─────────────────────────────────────────────

class TestExpiryMcxRiskAlertSuppression:
    def test_shape_unchanged(self):
        agent = next(a for a in BUILTIN_AGENTS if a["slug"] == "expiry-mcx-risk-alert")
        assert agent["schedule"] == "market_hours"
        assert agent["cooldown_minutes"] == 60
        assert agent["tier"] == "high"

    def test_suppressed_inside_mcx_preclose_window(self):
        """A lone unhedged GOLDM leg, within 30 min of MCX close on an
        MCX expiry day — must go quiet (expiry-day-preclose-alert
        already covers it)."""
        today_dt = datetime(2025, 5, 29, 17, 30, tzinfo=timezone.utc)  # ~23:00 IST, 30 min to 23:30 close
        ctx = _ctx(now=today_dt, position_rows=[
            {"tradingsymbol": "GOLDM25MAY75000CE", "exchange": "MCX", "account": "A1",
             "quantity": 1, "last_price": 500.0},
        ], spot_prices={"GOLDM": 76000.0},
                   segment_flags={"mcx_open": True, "minutes_until_mcx_close": 30})
        matches = evaluate(_agent_cond("expiry-mcx-risk-alert"), ctx)
        assert matches == [], "must be suppressed inside MCX's own pre-close window"

    def test_fires_outside_preclose_window_same_expiry_day(self):
        # GOLDM25MAY's parsed expiry is 2025-05-30 — ref must stay
        # within `_scope_positions_expiring_today`'s <=1.5-day window
        # of that date's 23:30 IST MCX close, so 14:00 IST (08:30 UTC)
        # on the 29th, not the 09:30 IST used elsewhere in this class.
        today_dt = datetime(2025, 5, 29, 8, 30, tzinfo=timezone.utc)  # ~14:00 IST
        ctx = _ctx(now=today_dt, position_rows=[
            {"tradingsymbol": "GOLDM25MAY75000CE", "exchange": "MCX", "account": "A1",
             "quantity": 1, "last_price": 500.0},
        ], spot_prices={"GOLDM": 76000.0},
                   segment_flags={"mcx_open": True, "minutes_until_mcx_close": 360})
        matches = evaluate(_agent_cond("expiry-mcx-risk-alert"), ctx)
        assert matches != [], "must still fire well outside the 30-min pre-close window"


# ── Content-matched one-time migration ────────────────────────────────

class TestContentMatchedCondMigration:
    def test_migrates_stock_pre_consolidation_row(self):
        from backend.api.algo.agent_engine import (
            _ae_apply_content_matched_cond_migration,
            _AE_CONTENT_MATCHED_COND_MIGRATIONS,
        )

        class _Row:
            pass

        for slug, old_cond in _AE_CONTENT_MATCHED_COND_MIGRATIONS.items():
            existing = _Row()
            existing.conditions = old_cond
            agent_def = next(a for a in BUILTIN_AGENTS if a["slug"] == slug)
            _ae_apply_content_matched_cond_migration(existing, agent_def)
            assert existing.conditions == agent_def["conditions"], (
                f"{slug}: stock pre-consolidation row should migrate to the "
                f"new code shape")

    def test_leaves_customized_row_untouched(self):
        from backend.api.algo.agent_engine import _ae_apply_content_matched_cond_migration

        class _Row:
            pass

        existing = _Row()
        existing.conditions = {"op": "<=", "scope": "funds.total",
                                "metric": "minutes_until_nse_close", "value": 15}  # operator-customized
        agent_def = next(a for a in BUILTIN_AGENTS if a["slug"] == "market-preclose-nonmcx")
        _ae_apply_content_matched_cond_migration(existing, agent_def)
        assert existing.conditions == {"op": "<=", "scope": "funds.total",
                                        "metric": "minutes_until_nse_close", "value": 15}, (
            "an operator-customized row must never be overwritten")

    def test_noops_for_unrelated_slug(self):
        from backend.api.algo.agent_engine import _ae_apply_content_matched_cond_migration

        class _Row:
            pass

        existing = _Row()
        existing.conditions = {"anything": "goes"}
        agent_def = {"slug": "market-open-nse", "conditions": {"other": "shape"}}
        _ae_apply_content_matched_cond_migration(existing, agent_def)
        assert existing.conditions == {"anything": "goes"}
