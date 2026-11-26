"""2026-11 same-window consolidation — operator finding: on an expiry
day, `market-preclose-nonmcx` (generic reminder), `expiry-day-preclose-
alert` (the richer, expiry-specific alert with the real position
breakdown) and the expiry risk alert could all fire within about a
minute of each other, 30 minutes before that exchange's close.
`expiry-nfo-risk-alert`'s own suppression leaf was retimed to 65 min on
2026-11-22, alongside `expiry-day-preclose-alert`/`expiry-day-itm-
auto-close`'s own retime from 30/25 to 65/60 (operator: "unnetted in
the money positions closed... one hour before market close").
`market-preclose-nonmcx`/`market-preclose-mcx-v2`'s own 30-min
condition is untouched — a different, unrelated agent pair.

2026-11-19 — `expiry-nfo-risk-alert` and `expiry-mcx-risk-alert` were
merged into ONE agent (`expiry-nfo-risk-alert`, edited in place — see
`agent_engine.py`'s own comment above `_EXPIRY_RISK_AGENTS`). Its
`conditions` became a top-level `{"any": [<NFO all[] branch>, <MCX
all[] branch>]}`; each branch kept its OWN exchange-scoped pre-close
suppression leaf. `expiry-mcx-risk-alert`'s own slug no longer existed
in `BUILTIN_AGENTS` — it was pruned (deleted) by
`_ae_prune_retired_builtins()`, not migrated.

2026-11-26 — SPLIT BACK, not just retimed. Live operator report: "i
don't have any nfo positions. i got nfo expiration alerts with mcx
positions." Confirmed via `agent_events` on prod: every real fire of
the merged agent for hours matched only the MCX branch, yet the row's
own (never force-synced) `name` field still read "NFO expiry risk..."
with no mention of MCX at all. `expiry-nfo-risk-alert` is now
NFO-pure (`{"all": [<NFO leaf>, <NFO suppression leaf>]}` — no `any[]`,
no MCX branch at all), and `expiry-mcx-risk-alert` is reintroduced as
its own fully independent agent (`{"all": [<MCX leaf>, <MCX suppression
leaf>]}`), each on its OWN distinct topic (`expiry_warning_nfo` /
`expiry_warning_mcx`, not the shared `expiry_warning` the generic-
reminder/preclose/morning-preview family uses) — see `_EXPIRY_RISK_
AGENTS`'s own comment for why a shared topic would have left the same
cross-exchange-merge risk in place via `_compute_topic_suppression()`.

Covers:
  - `market-preclose-nonmcx` is suppressed on an NSE expiry day, fires
    normally on a non-expiry day; `market-preclose-mcx-v2` is
    untouched either way.
  - `expiry-nfo-risk-alert` and `expiry-mcx-risk-alert` are two fully
    independent agents: each fires on its own exchange's condition
    only, is suppressed specifically inside its OWN exchange's
    65-minute pre-close window on an expiry day (where `expiry-day-
    preclose-alert` already covers the same position), and NEVER
    reacts to the other exchange's data or pre-close window at all —
    structurally, not just behaviorally, since each agent's condition
    tree contains no reference whatsoever to the other exchange.
  - BUILTIN_AGENTS shape (slug/schedule/cooldown/tier/events) is
    unchanged for `market-preclose-nonmcx`; the two risk-alert agents'
    schedule/cooldown/tier/events are unchanged from the merged row's,
    only `conditions`/`name`/`description`/`topic` differ per agent.
  - The table-driven content-matched one-time migration
    (`_ae_apply_content_matched_cond_migration`) upgrades an
    already-seeded merged-era `expiry-nfo-risk-alert` row to the new
    NFO-only shape and leaves an operator-customized row alone;
    `expiry-mcx-risk-alert` needs no migration (fresh insert, 0 rows
    existed under that slug on either prod or dev at split time).
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


# ── expiry-nfo-risk-alert / expiry-mcx-risk-alert (split back into
#    two exchange-pure agents, 2026-11-26) ─────────────────────────────

class TestExpiryRiskAlertsSplit:
    def test_nfo_shape(self):
        agent = next(a for a in BUILTIN_AGENTS if a["slug"] == "expiry-nfo-risk-alert")
        assert agent["schedule"] == "market_hours"
        # Raised 60->240 on 2026-11-26 (operator: "too many alerts on
        # the previous day of expiry of mcx. need to reduce it.").
        assert agent["cooldown_minutes"] == 240
        assert agent["tier"] == "high"
        assert agent["topic"] == "expiry_warning_nfo"

    def test_mcx_shape(self):
        agent = next(a for a in BUILTIN_AGENTS if a["slug"] == "expiry-mcx-risk-alert")
        assert agent["schedule"] == "market_hours"
        assert agent["cooldown_minutes"] == 240
        assert agent["tier"] == "high"
        assert agent["topic"] == "expiry_warning_mcx"

    def test_mcx_slug_reintroduced_as_separate_agent(self):
        """`expiry-mcx-risk-alert` is back as its own BUILTIN_AGENTS
        entry (reversing the 2026-11-19 merge) — live operator report:
        an alert literally named "NFO" was firing purely on real MCX
        positions. Two separate agents is the only way to guarantee
        each alert's name/slug can never disagree with the exchange
        whose data actually triggered it."""
        assert any(a["slug"] == "expiry-mcx-risk-alert" for a in BUILTIN_AGENTS)

    def test_nfo_and_mcx_use_distinct_topics_not_the_shared_one(self):
        """Confirmed during the split's own planning: `_compute_topic_
        suppression()` merges/suppresses same-topic co-fires within one
        run_cycle tick. Sharing the generic-reminder family's
        `expiry_warning` topic between these two agents would silently
        reintroduce the exact cross-exchange-merge bug this split
        exists to fix, the first time both genuinely co-fire in one
        tick. Each must have its OWN topic, and neither may be the
        shared one the preclose/auto-close/morning-preview agents use."""
        nfo = next(a for a in BUILTIN_AGENTS if a["slug"] == "expiry-nfo-risk-alert")
        mcx = next(a for a in BUILTIN_AGENTS if a["slug"] == "expiry-mcx-risk-alert")
        assert nfo["topic"] != mcx["topic"]
        assert nfo["topic"] != "expiry_warning"
        assert mcx["topic"] != "expiry_warning"

    def test_nfo_condition_is_exchange_pure(self):
        """Structural, not just behavioral: the NFO agent's condition
        tree contains no reference to MCX at all — a top-level `all[]`
        of exactly the NFO leaf + the NFO-scoped suppression leaf, no
        `any[]`, no MCX branch to accidentally match on."""
        cond = _agent_cond("expiry-nfo-risk-alert")
        assert set(cond.keys()) == {"all"}
        assert len(cond["all"]) == 2
        assert "mcx" not in str(cond).lower()

    def test_mcx_condition_is_exchange_pure(self):
        cond = _agent_cond("expiry-mcx-risk-alert")
        assert set(cond.keys()) == {"all"}
        assert len(cond["all"]) == 2
        assert "nfo" not in str(cond).lower()
        assert "nse" not in str(cond).lower()

    # ── NFO agent ─────────────────────────────────────────────────────

    def test_suppressed_inside_nse_preclose_window(self):
        """An ITM NFO position exists AND it's within 65 min of NSE
        close on an NSE expiry day — expiry-day-preclose-alert already
        reports this; this agent must go quiet."""
        now = datetime(2025, 4, 24, 9, 30, tzinfo=timezone.utc)  # ~15:00 IST, 65 min to 15:30 close (approx)
        ctx = _ctx(now=now, position_rows=[
            {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO", "quantity": 50},
        ], spot_prices={"NIFTY": 22200.0},
                   segment_flags={"nse_open": True, "minutes_until_nse_close": 65})
        matches = evaluate(_agent_cond("expiry-nfo-risk-alert"), ctx)
        assert matches == [], "must be suppressed inside NSE's own pre-close window"

    def test_fires_outside_nse_preclose_window_same_expiry_day(self):
        """Same expiry day, but well before the 65-min pre-close
        window — the risk alert's earlier-warning cadence must survive."""
        now = datetime(2025, 4, 24, 4, 0, tzinfo=timezone.utc)  # ~09:30 IST — market just opened
        ctx = _ctx(now=now, position_rows=[
            {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO", "quantity": 50},
        ], spot_prices={"NIFTY": 22200.0},
                   segment_flags={"nse_open": True, "minutes_until_nse_close": 360})
        matches = evaluate(_agent_cond("expiry-nfo-risk-alert"), ctx)
        assert matches != [], "must still fire well outside the 65-min pre-close window"

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

    def test_nfo_agent_never_matches_a_mcx_only_position(self):
        """The exact live-reported bug, as a regression test: an
        account holding only an unhedged MCX leg, zero NFO positions,
        must never produce a match from the NFO agent's condition."""
        today_dt = datetime(2025, 5, 29, 8, 30, tzinfo=timezone.utc)  # ~14:00 IST
        ctx = _ctx(now=today_dt, position_rows=[
            {"tradingsymbol": "GOLDM25MAY75000CE", "exchange": "MCX", "account": "A1",
             "quantity": 1, "last_price": 500.0},
        ], spot_prices={"GOLDM": 76000.0},
                   segment_flags={"mcx_open": True, "minutes_until_mcx_close": 360})
        matches = evaluate(_agent_cond("expiry-nfo-risk-alert"), ctx)
        assert matches == [], (
            "zero NFO positions ⇒ the NFO agent must never fire, "
            "regardless of any MCX data present"
        )

    # ── MCX agent ─────────────────────────────────────────────────────

    def test_suppressed_inside_mcx_preclose_window(self):
        """A lone unhedged GOLDM leg, within 65 min of MCX close on an
        MCX expiry day — must go quiet (expiry-day-preclose-alert
        already covers it)."""
        today_dt = datetime(2025, 5, 29, 17, 30, tzinfo=timezone.utc)  # ~23:00 IST, 65 min to 23:30 close (approx)
        ctx = _ctx(now=today_dt, position_rows=[
            {"tradingsymbol": "GOLDM25MAY75000CE", "exchange": "MCX", "account": "A1",
             "quantity": 1, "last_price": 500.0},
        ], spot_prices={"GOLDM": 76000.0},
                   segment_flags={"mcx_open": True, "minutes_until_mcx_close": 65})
        matches = evaluate(_agent_cond("expiry-mcx-risk-alert"), ctx)
        assert matches == [], "must be suppressed inside MCX's own pre-close window"

    def test_fires_outside_mcx_preclose_window_same_expiry_day(self):
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
        assert matches != [], "must still fire well outside the 65-min pre-close window"

    def test_mcx_agent_never_matches_a_nfo_only_position(self):
        """The mirror of the live-reported bug: an account holding only
        an ITM NFO leg, zero MCX positions, must never produce a match
        from the MCX agent's condition."""
        now = datetime(2025, 4, 24, 4, 0, tzinfo=timezone.utc)  # ~09:30 IST — market just opened
        ctx = _ctx(now=now, position_rows=[
            {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO", "quantity": 50},
        ], spot_prices={"NIFTY": 22200.0},
                   segment_flags={"nse_open": True, "minutes_until_nse_close": 360})
        matches = evaluate(_agent_cond("expiry-mcx-risk-alert"), ctx)
        assert matches == [], (
            "zero MCX positions ⇒ the MCX agent must never fire, "
            "regardless of any NFO data present"
        )

    # ── Cross-agent independence — the actual risk of the old merge ──

    def test_nse_preclose_window_does_not_suppress_mcx_agent(self):
        """Inside NSE's own 65-min pre-close window, a lone unhedged
        MCX leg that is NOT inside ITS OWN pre-close window must still
        fire from the MCX agent — NSE's suppression leaf lives only in
        the NFO agent's condition tree and cannot reach the MCX agent
        at all, let alone suppress it. GOLDM25APR's parsed expiry must
        stay within `_scope_positions_expiring_today`'s <=1.5-day
        window of this same 2025-04-24 "today", matching the NFO-side
        fixtures' own April expiry date above."""
        now = datetime(2025, 4, 24, 9, 30, tzinfo=timezone.utc)  # ~15:00 IST — NSE 65 min to close (approx)
        ctx = _ctx(now=now, position_rows=[
            {"tradingsymbol": "GOLDM25APR75000CE", "exchange": "MCX", "account": "A1",
             "quantity": 1, "last_price": 500.0},
        ], spot_prices={"GOLDM": 76000.0},
                   segment_flags={"nse_open": True, "minutes_until_nse_close": 65,
                                  "mcx_open": True, "minutes_until_mcx_close": 360})
        matches = evaluate(_agent_cond("expiry-mcx-risk-alert"), ctx)
        assert matches != [], (
            "the MCX agent must fire even while NSE's own pre-close window is active"
        )

    def test_mcx_preclose_window_does_not_suppress_nfo_agent(self):
        """Inside MCX's own 65-min pre-close window, an ITM NFO
        position that is NOT inside ITS OWN pre-close window must still
        fire from the NFO agent — MCX's suppression leaf lives only in
        the MCX agent's condition tree and cannot reach the NFO agent
        at all."""
        today_dt = datetime(2025, 4, 24, 17, 0, tzinfo=timezone.utc)  # ~22:30 IST — MCX 65 min to close (approx), NSE long closed
        ctx = _ctx(now=today_dt, position_rows=[
            {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO", "quantity": 50},
        ], spot_prices={"NIFTY": 22200.0},
                   segment_flags={"nse_open": False, "minutes_until_nse_close": 360,
                                  "mcx_open": True, "minutes_until_mcx_close": 65})
        matches = evaluate(_agent_cond("expiry-nfo-risk-alert"), ctx)
        assert matches != [], (
            "the NFO agent must fire even while MCX's own pre-close window is active"
        )

    def test_both_agents_can_fire_independently_in_one_evaluation(self):
        """Both an ITM NFO leg and an unhedged MCX leg are held
        simultaneously, neither inside its own pre-close window — each
        agent's OWN condition must independently match, confirming
        neither was accidentally scoped to require the other's data.

        14:00 IST (08:30 UTC) on 2025-04-24, not the 09:30 IST used by
        several single-exchange tests above: GOLDM25APR75000CE expires
        2025-04-25 23:30 IST, and `_scope_positions_expiring_today`'s
        own <=1.5-day window requires `days_to_expiry <= 1.5` AT the
        instant `now` is evaluated — confirmed numerically (not
        guessed) that 09:30 IST yields d≈1.58 for this leg (just past
        the window, silently dropping the MCX row from the shared base
        scope and masking the real assertion this test exists to make),
        while 14:00 IST yields d≈1.40 (safely inside it) with NIFTY's
        own d≈0.06 still comfortably non-zero and both exchanges well
        outside their own 65-min pre-close windows (NSE close 15:30
        IST ⇒ ~90 min away; MCX close 23:30 IST ⇒ ~570 min away)."""
        now = datetime(2025, 4, 24, 8, 30, tzinfo=timezone.utc)  # ~14:00 IST — both exchanges well open
        ctx = _ctx(now=now, position_rows=[
            {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO", "quantity": 50},
            {"tradingsymbol": "GOLDM25APR75000CE", "exchange": "MCX", "account": "A1",
             "quantity": 1, "last_price": 500.0},
        ], spot_prices={"NIFTY": 22200.0, "GOLDM": 76000.0},
                   segment_flags={"nse_open": True, "minutes_until_nse_close": 90,
                                  "mcx_open": True, "minutes_until_mcx_close": 570})
        nfo_matches = evaluate(_agent_cond("expiry-nfo-risk-alert"), ctx)
        mcx_matches = evaluate(_agent_cond("expiry-mcx-risk-alert"), ctx)
        assert nfo_matches != [], "NFO leg held and not suppressed ⇒ NFO agent must fire"
        assert mcx_matches != [], "MCX leg held and not suppressed ⇒ MCX agent must fire"


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

    def test_nfo_risk_alert_migrates_from_merged_shape_to_nfo_pure_split(self):
        """2026-11-26 split: an already-seeded row still at the
        pre-split merged `any[nfo, mcx]` shape (confirmed byte-for-byte
        identical on both prod and dev before this migration key was
        written) must force-upgrade to the new, NFO-pure `all[]` shape —
        shipping the new condition in code alone is inert against a
        live DB row otherwise."""
        from backend.api.algo.agent_engine import (
            _ae_apply_content_matched_cond_migration,
            _AE_CONTENT_MATCHED_COND_MIGRATIONS,
        )

        class _Row:
            pass

        existing = _Row()
        existing.conditions = _AE_CONTENT_MATCHED_COND_MIGRATIONS["expiry-nfo-risk-alert"]
        agent_def = next(a for a in BUILTIN_AGENTS if a["slug"] == "expiry-nfo-risk-alert")
        _ae_apply_content_matched_cond_migration(existing, agent_def)

        assert set(existing.conditions.keys()) == {"all"}, (
            "the migrated row must be the new NFO-pure all[] shape, "
            "with no top-level any[] left over from the merge"
        )
        nfo_leaf = existing.conditions["all"][0]
        assert nfo_leaf == {
            "op": ">=", "scope": "positions.expiring_today.nfo",
            "metric": "pnl", "value": -999999999,
        }, "the NFO leaf must be the tautological pnl sentinel"
        assert "mcx" not in str(existing.conditions).lower(), (
            "the migrated row must contain no MCX reference at all"
        )

    def test_mcx_risk_alert_needs_no_migration_entry_fresh_insert(self):
        """`expiry-mcx-risk-alert` is a fresh insert on both prod and
        dev (confirmed 0 existing rows with that slug at split time),
        so it correctly has no entry in `_AE_CONTENT_MATCHED_COND_
        MIGRATIONS` — there is no stale row for a migration to upgrade."""
        from backend.api.algo.agent_engine import _AE_CONTENT_MATCHED_COND_MIGRATIONS

        assert "expiry-mcx-risk-alert" not in _AE_CONTENT_MATCHED_COND_MIGRATIONS


# ── name/description/topic force-sync for the split (2026-11-26) ─────
# `_ae_apply_content_matched_cond_migration` only ever touches
# `conditions`. Without a separate sync, an already-seeded row would
# keep its stale merged-era `name` ("NFO or MCX expiry risk...") and
# shared `expiry_warning` topic forever — exactly the confusion the
# split exists to fix. `_ae_sync_expiry_risk_alert_split()` closes
# that gap; these tests exercise it directly, the same way
# `_ae_sync_expiry_awareness_cooldown` (the cooldown-migration
# precedent this function's own docstring cites) has its own tests.

class TestExpiryRiskAlertSplitFieldSync:
    def test_stock_pre_split_row_is_force_synced(self):
        from backend.api.algo.agent_engine import (
            _ae_sync_expiry_risk_alert_split,
            _AE_EXPIRY_NFO_RISK_ALERT_PRE_SPLIT_NAME,
            _AE_EXPIRY_NFO_RISK_ALERT_PRE_SPLIT_TOPIC,
        )

        class _Row:
            pass

        existing = _Row()
        existing.name = _AE_EXPIRY_NFO_RISK_ALERT_PRE_SPLIT_NAME
        existing.topic = _AE_EXPIRY_NFO_RISK_ALERT_PRE_SPLIT_TOPIC
        existing.description = "old merged description"
        agent_def = next(a for a in BUILTIN_AGENTS if a["slug"] == "expiry-nfo-risk-alert")

        _ae_sync_expiry_risk_alert_split(existing, agent_def)

        assert existing.name == agent_def["name"]
        assert existing.topic == agent_def["topic"] == "expiry_warning_nfo"
        assert existing.description == agent_def["description"]

    def test_operator_renamed_row_is_left_alone(self):
        """Value-gated against the EXACT pre-split stock name/topic —
        an operator who already renamed or re-topicked this agent (in
        either direction) must never have that choice silently
        overwritten on the next restart."""
        from backend.api.algo.agent_engine import _ae_sync_expiry_risk_alert_split

        class _Row:
            pass

        existing = _Row()
        existing.name = "My custom expiry alert name"
        existing.topic = "expiry_warning"
        existing.description = "my custom description"
        agent_def = next(a for a in BUILTIN_AGENTS if a["slug"] == "expiry-nfo-risk-alert")

        _ae_sync_expiry_risk_alert_split(existing, agent_def)

        assert existing.name == "My custom expiry alert name"
        assert existing.description == "my custom description"

    def test_operator_retopicked_row_is_left_alone_even_with_stock_name(self):
        """Both the name AND topic must match the pre-split stock
        values — a row with the stock name but a since-customized
        topic (or vice versa) is NOT force-synced, since that combo is
        evidence of at least one deliberate operator edit."""
        from backend.api.algo.agent_engine import (
            _ae_sync_expiry_risk_alert_split,
            _AE_EXPIRY_NFO_RISK_ALERT_PRE_SPLIT_NAME,
        )

        class _Row:
            pass

        existing = _Row()
        existing.name = _AE_EXPIRY_NFO_RISK_ALERT_PRE_SPLIT_NAME
        existing.topic = "my_custom_topic"
        existing.description = "stock-name but operator re-topicked"
        agent_def = next(a for a in BUILTIN_AGENTS if a["slug"] == "expiry-nfo-risk-alert")

        _ae_sync_expiry_risk_alert_split(existing, agent_def)

        assert existing.topic == "my_custom_topic"

    def test_unrelated_slug_is_a_no_op(self):
        from backend.api.algo.agent_engine import (
            _ae_sync_expiry_risk_alert_split,
            _AE_EXPIRY_NFO_RISK_ALERT_PRE_SPLIT_NAME,
            _AE_EXPIRY_NFO_RISK_ALERT_PRE_SPLIT_TOPIC,
        )

        class _Row:
            pass

        existing = _Row()
        existing.name = _AE_EXPIRY_NFO_RISK_ALERT_PRE_SPLIT_NAME
        existing.topic = _AE_EXPIRY_NFO_RISK_ALERT_PRE_SPLIT_TOPIC
        existing.description = "unrelated agent, coincidentally same name/topic"
        agent_def = {"slug": "market-open-nse", "name": "should never apply",
                     "topic": "general", "description": "should never apply"}

        _ae_sync_expiry_risk_alert_split(existing, agent_def)

        assert existing.name == _AE_EXPIRY_NFO_RISK_ALERT_PRE_SPLIT_NAME
        assert existing.topic == _AE_EXPIRY_NFO_RISK_ALERT_PRE_SPLIT_TOPIC

    def test_called_from_sync_existing_builtin_end_to_end(self):
        """Confirms the function is actually wired into `_ae_sync_
        existing_builtin`'s call chain, not just unit-testable in
        isolation — drives the full sync path a real restart takes."""
        from backend.api.algo.agent_engine import (
            _ae_sync_existing_builtin,
            _AE_EXPIRY_NFO_RISK_ALERT_PRE_SPLIT_NAME,
            _AE_EXPIRY_NFO_RISK_ALERT_PRE_SPLIT_TOPIC,
        )

        class _Row:
            long_name = None
            schedule = "market_hours"
            tier = "medium"
            topic = _AE_EXPIRY_NFO_RISK_ALERT_PRE_SPLIT_TOPIC
            fire_at_time = None
            conditions = None
            cooldown_minutes = 60
            status = "active"
            require_release = False
            trade_mode = "paper"
            events = []

        existing = _Row()
        existing.name = _AE_EXPIRY_NFO_RISK_ALERT_PRE_SPLIT_NAME
        existing.description = "old merged description"
        agent_def = next(a for a in BUILTIN_AGENTS if a["slug"] == "expiry-nfo-risk-alert")

        _ae_sync_existing_builtin(existing, agent_def)

        assert existing.name == agent_def["name"]
        assert existing.topic == "expiry_warning_nfo"
