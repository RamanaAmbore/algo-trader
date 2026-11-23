"""BUILTIN_AGENTS configuration for the expiry-day and market pre-close agents.

Covers:
  - market-preclose-nonmcx (NSE counterpart of market-preclose-mcx-v2)
  - expiry-day-mcx-v2 / expiry-day-nse-v2 (held F&O expiring today, retimed
    2026-10-29 to fire within 30 min of that exchange's own close, per
    operator instruction — see agent_engine.py's own comment above
    `_EXPIRY_DAY_AWARENESS_AGENTS`)
  - market-preclose-mcx / expiry-day-mcx / expiry-day-nse (retired slugs)
    are absent, so the boot-time `_ae_prune_retired_builtins` sweep has a
    real row to delete for each

The pruning mechanism itself is DB-backed and covered by
test_bracket_agent_unification.py; here we only assert the registry side
(slug not in BUILTIN_AGENTS), which is what the sweep keys on.
"""

from __future__ import annotations

import pytest

from backend.api.algo.agent_engine import BUILTIN_AGENTS


def _by_slug(slug: str) -> dict | None:
    return next((a for a in BUILTIN_AGENTS if a.get('slug') == slug), None)


def _channels(agent: dict) -> set[str]:
    return {e["channel"] for e in agent.get("events", [])}


class TestMarketPrecloseNonMcx:
    """market-preclose-nonmcx: token-driven NSE pre-close, shared info defaults."""

    def test_exists_in_builtin_agents(self):
        assert _by_slug('market-preclose-nonmcx') is not None, \
            "market-preclose-nonmcx not found in BUILTIN_AGENTS"

    def test_is_token_driven_not_scheduled(self):
        agent = _by_slug('market-preclose-nonmcx')
        assert agent is not None
        assert agent.get('fire_at_time') is None, \
            f"Expected fire_at_time=None, got {agent.get('fire_at_time')}"

    def test_condition_is_nse_close_within_30_minutes(self):
        """2026-11 same-window consolidation added a second leaf — this
        generic reminder now also requires NOT being an NSE expiry day,
        since expiry-day-preclose-alert already covers that same moment
        with richer content. See test_expiry_same_window_consolidation.py
        for the suppression-behavior coverage."""
        agent = _by_slug('market-preclose-nonmcx')
        assert agent is not None
        assert agent.get('conditions') == {"all": [
            {"op": "<=", "scope": "funds.total",
             "metric": "minutes_until_nse_close", "value": 30},
            {"op": "==", "scope": "funds.total",
             "metric": "is_nse_expiry_today", "value": 0.0},
        ]}, f"Unexpected conditions: {agent.get('conditions')}"

    def test_name(self):
        agent = _by_slug('market-preclose-nonmcx')
        assert agent is not None
        assert agent.get('name') == 'NSE pre-close', \
            f"Expected name='NSE pre-close', got {agent.get('name')}"

    def test_is_info_tier(self):
        agent = _by_slug('market-preclose-nonmcx')
        assert agent is not None
        assert agent.get('tier') == 'info', f"Expected tier='info', got {agent.get('tier')}"

    def test_is_active_status(self):
        agent = _by_slug('market-preclose-nonmcx')
        assert agent is not None
        assert agent.get('status') == 'active', f"Expected status='active', got {agent.get('status')}"

    def test_events_exclude_email(self):
        """Inherits the shared _INFO_AGENT_DEFAULTS (telegram+ntfy+log).
        Deliberately unlike the MCX sibling, which adds email."""
        agent = _by_slug('market-preclose-nonmcx')
        assert agent is not None
        channels = _channels(agent)
        assert "email" not in channels, f"NSE pre-close must not send email, got {channels}"
        assert channels == {"telegram", "log", "ntfy"}, \
            f"Expected shared info channels {{telegram,log,ntfy}}, got {channels}"


class TestExpiryDayPrecloseAlertAgent:
    """expiry-day-preclose-alert: ONE agent covering both MCX and NFO,
    firing within 65 minutes of whichever exchange's own close applies
    (2026-10-29 consolidation — operator: "can you create one agent for
    order placing and one agent for alert instead of two" / "why can't
    you have one agent for nfo and mcx"; retimed 30→65 on 2026-11-22,
    operator: "unnetted in the money positions closed... one hour
    before market close")."""

    SLUG = "expiry-day-preclose-alert"

    def test_exists_in_builtin_agents(self):
        assert _by_slug(self.SLUG) is not None, f"{self.SLUG} not found in BUILTIN_AGENTS"

    def test_is_token_driven_not_scheduled(self):
        agent = _by_slug(self.SLUG)
        assert agent is not None
        assert agent.get('fire_at_time') is None

    def test_condition_is_one_flat_leaf_not_a_compound_tree(self):
        """Operator: "let the function that you register identify the
        positions, 30 mins before market close, etc." — the exchange
        loop and lead-time check live in the metric resolver, not a
        per-exchange all[]/any[] DSL tree."""
        agent = _by_slug(self.SLUG)
        assert agent is not None
        assert agent.get('conditions') == {
            "op": "==", "scope": "funds.total",
            "metric": "is_expiry_preclose_alert_window", "value": 1.0,
        }, f"unexpected conditions {agent.get('conditions')}"

    def test_is_high_tier(self):
        agent = _by_slug(self.SLUG)
        assert agent is not None
        assert agent.get('tier') == "high"

    def test_cooldown_is_30_minutes(self):
        """30 min (changed from 180 on 2026-11-22) — short enough that
        the operator is re-notified of a newly-staged leg within the
        65-min window (matching the sibling close-action agent's own
        periodic re-scan cadence), yet short enough that it never
        risks NFO's own fire suppressing MCX's fire hours later the
        same day either. See the comment above
        _EXPIRY_DAY_AWARENESS_AGENTS for the full reasoning."""
        agent = _by_slug(self.SLUG)
        assert agent is not None
        assert agent.get('cooldown_minutes') == 30, \
            f"expected cooldown_minutes=30, got {agent.get('cooldown_minutes')}"

    def test_events_cover_all_four_channels_with_high_ntfy(self):
        agent = _by_slug(self.SLUG)
        assert agent is not None
        assert _channels(agent) == {"telegram", "email", "log", "ntfy"}
        ntfy = next(e for e in agent["events"] if e["channel"] == "ntfy")
        assert ntfy.get("priority") == "high"


class TestExpiryDayItmAutoCloseAgent:
    """expiry-day-itm-auto-close: ONE order-placing agent covering both
    MCX and NFO, firing starting 60 minutes before each exchange's own
    close (5 min after the alert sibling's 65-min window), then
    re-firing every 30 minutes while still in that window (2026-11-22
    retime — closes the gap where a leg could turn unnetted-ITM in the
    last hour with no periodic re-scan, matching NFO's own pre-existing
    re-scan cadence)."""

    SLUG = "expiry-day-itm-auto-close"

    def test_exists_in_builtin_agents(self):
        assert _by_slug(self.SLUG) is not None, f"{self.SLUG} not found in BUILTIN_AGENTS"

    def test_is_token_driven_not_scheduled(self):
        agent = _by_slug(self.SLUG)
        assert agent is not None
        assert agent.get('fire_at_time') is None

    def test_condition_is_one_flat_leaf_at_60_minutes(self):
        agent = _by_slug(self.SLUG)
        assert agent is not None
        assert agent.get('conditions') == {
            "op": "==", "scope": "funds.total",
            "metric": "is_expiry_preclose_close_window", "value": 1.0,
        }, f"unexpected conditions {agent.get('conditions')}"

    def test_is_critical_tier(self):
        agent = _by_slug(self.SLUG)
        assert agent is not None
        assert agent.get('tier') == "critical"

    def test_cooldown_is_30_minutes(self):
        """30 min (changed from 180 on 2026-11-22) — lets this agent
        re-fire ~2x inside its own 60-min window (T-60, T-30),
        re-running ExpiryEngine.scan_positions() each time. Safe to
        repeat: record_held_order()'s per-leg dedup guard returns the
        existing HELD row's id for an already-staged leg instead of
        duplicating it."""
        agent = _by_slug(self.SLUG)
        assert agent is not None
        assert agent.get('cooldown_minutes') == 30

    def test_status_is_active(self):
        """New slug, not bound by the _ae_guard_seed_status fail-safe
        (which only triggers on a literal 'Ships INACTIVE' phrase in the
        description — this agent's real safety net is the order-level
        hold-by-default in ExpiryEngine.close_positions(), not agent
        deactivation)."""
        agent = _by_slug(self.SLUG)
        assert agent is not None
        assert agent.get('status') == "active"
        assert "Ships INACTIVE" not in (agent.get('description') or ""), \
            "this phrase would force status back to inactive on every restart " \
            "via _ae_guard_seed_status — must not appear here"

    def test_action_is_expiry_auto_close_with_no_exchange_param(self):
        agent = _by_slug(self.SLUG)
        assert agent is not None
        actions = agent.get('actions') or []
        assert len(actions) == 1
        assert actions[0]["type"] == "expiry_auto_close"
        assert actions[0]["params"] == {}, \
            "params must be empty — the action processes both exchanges from one scan"

    def test_events_include_ntfy_high(self):
        """ntfy(high) added 2026-11-22 (operator: a real-money milestone
        — positions staged HELD and waiting for release need an urgent
        mobile push), matching the sibling alert agent's own channel
        set."""
        agent = _by_slug(self.SLUG)
        assert agent is not None
        assert _channels(agent) == {"telegram", "email", "log", "ntfy"}
        ntfy = next(e for e in agent["events"] if e["channel"] == "ntfy")
        assert ntfy.get("priority") == "high"

    def test_trade_mode_is_live(self):
        """2026-10-31 live-incident fix: without an explicit trade_mode
        here, this agent silently inherited execution.default_agent_
        trade_mode ('paper') and every real close on its first live
        trading day ran through PaperTradeEngine instead of the real
        hold-gate path — confirmed via prod algo_orders rows written
        mode='paper'/engine='paper' for a genuine ExpiryEngine-identified
        close. The real safety net (HoldCategory.EXPIRY_CLOSE, held by
        default) only engages once this reaches the live action path at
        all."""
        agent = _by_slug(self.SLUG)
        assert agent is not None
        assert agent.get('trade_mode') == "live", \
            "must explicitly set trade_mode='live' — omitting it silently " \
            "falls back to the paper default and the hold-gate never engages"


class TestExpiryDayAgentsSameDayNoSuppression:
    """Regression test for the one real risk the consolidation
    introduced: both agents now share ONE metric name across MCX and
    NFO, so the per-fire latch (keyed on agent_slug+metric+scope+account,
    no exchange dimension) must not let one exchange's fire suppress
    the other's on the same day. cooldown_minutes=30 (changed from 180
    on 2026-11-22, to enable periodic re-scanning within each
    exchange's own window) is short enough that this was never a risk
    to begin with — this test pins both the periodic-re-fire behaviour
    AND the cross-exchange independence, not just the config value."""

    @pytest.mark.parametrize("slug,metric", [
        ("expiry-day-preclose-alert", "is_expiry_preclose_alert_window"),
        ("expiry-day-itm-auto-close", "is_expiry_preclose_close_window"),
    ])
    def test_nfo_fire_then_mcx_fire_same_day_both_pass_escalation_gate(self, slug, metric):
        from datetime import datetime, timedelta, timezone
        from backend.api.algo.agent_engine import _v2_apply_escalation_gate

        class _Agent:
            pass
        agent = _Agent()
        agent.slug = slug

        store: dict = {}  # isolated — never the shared module-level _V2_LATCH
        now0 = datetime(2026, 10, 30, 9, 30, 0, tzinfo=timezone.utc)  # ~15:00 IST
        nfo_match = [{"metric": metric, "scope": "funds.total", "op": "==",
                      "value": 1.0, "threshold": 1.0, "row": {}}]
        # NFO fires at t=0 (its own ~15:00 window).
        effective = _v2_apply_escalation_gate(agent, nfo_match, now0, 30, store=store)
        assert effective, "NFO's own fire must pass the gate"

        # A SECOND fire 5 minutes later (still well inside the 30-min
        # cooldown) must be suppressed.
        now_dup = now0 + timedelta(minutes=5)
        dup_match = [{"metric": metric, "scope": "funds.total", "op": "==",
                      "value": 1.0, "threshold": 1.0, "row": {}}]
        effective_dup = _v2_apply_escalation_gate(agent, dup_match, now_dup, 30, store=store)
        assert not effective_dup, \
            "a re-fire 5 min later (same window) must be suppressed by the cooldown"

        # A THIRD fire 31 minutes after the FIRST (cooldown elapsed,
        # still well inside NFO's own ~65-min window) must now be
        # ALLOWED — this IS the periodic re-scan this retime exists to
        # provide (a leg that turns unnetted-ITM in the last hour must
        # get a fresh chance to be flagged/staged, not wait for
        # next-day review).
        now_rescan = now0 + timedelta(minutes=31)
        rescan_match = [{"metric": metric, "scope": "funds.total", "op": "==",
                         "value": 1.0, "threshold": 1.0, "row": {}}]
        effective_rescan = _v2_apply_escalation_gate(agent, rescan_match, now_rescan, 30, store=store)
        assert effective_rescan, \
            "a re-fire >30 min later, still inside the same window, must be " \
            "ALLOWED — this is the periodic re-scan the 2026-11-22 retime exists for"

        # MCX fires ~7.5h later the SAME day (its own ~23:00 window) —
        # must NOT be suppressed by NFO's 30-min cooldown, which has
        # long since cleared (trivially, given it's only 30 min).
        now1 = now0 + timedelta(hours=7.5)
        mcx_match = [{"metric": metric, "scope": "funds.total", "op": "==",
                      "value": 1.0, "threshold": 1.0, "row": {}}]
        effective2 = _v2_apply_escalation_gate(agent, mcx_match, now1, 30, store=store)
        assert effective2, \
            "MCX's fire 7.5h later the same day must not be suppressed by NFO's cooldown"


class TestRetiredSlugs:
    """Old slugs must be gone from the registry so _ae_prune_retired_builtins
    deletes the stale DB rows instead of leaving them stuck on a condition
    shape the sync-on-restart logic can't detect as stale."""

    @pytest.mark.parametrize("slug", [
        "market-preclose-mcx", "expiry-day-mcx", "expiry-day-nse",
        "expiry-day-mcx-v2", "expiry-day-nse-v2",
        "expiry-day-equity-itm-auto-close", "expiry-day-commodity-itm-auto-close",
    ])
    def test_old_slug_absent_from_builtin_agents(self, slug):
        assert _by_slug(slug) is None, \
            f"{slug} is retired; it must not appear in BUILTIN_AGENTS " \
            "or _ae_prune_retired_builtins will not delete the stale DB row"


class TestReminderOnlyLine:
    """_v2_reminder_only_line: clear text for calendar/state reminder
    matches, replacing the generic per-account kind/threshold table that
    previously rendered a meaningless 'TOTAL'/numeric row for these
    agents (operator: "why it is showing total values for pre close.
    expiry pre close should clearly say it is expiry pre close")."""

    def test_plain_preclose_uses_agent_name(self):
        from backend.api.algo.agent_engine import _v2_reminder_only_line

        class _Agent:
            name = "MCX pre-close"

        line = _v2_reminder_only_line(
            _Agent(), [{"metric": "minutes_until_mcx_close"}],
        )
        assert line == "MCX pre-close — MCX closes in 30 minutes"

    def test_expiry_leaf_alone_says_expiry_pre_close(self):
        from backend.api.algo.agent_engine import _v2_reminder_only_line

        class _Agent:
            name = "MCX expiry day"

        line = _v2_reminder_only_line(
            _Agent(), [{"metric": "is_mcx_expiry_today"}],
        )
        assert line == "Expiry pre-close — a held MCX position expires today"

    def test_compound_expiry_and_close_leaves_says_expiry_pre_close(self):
        from backend.api.algo.agent_engine import _v2_reminder_only_line

        class _Agent:
            name = "MCX expiry day"

        line = _v2_reminder_only_line(
            _Agent(), [
                {"metric": "is_mcx_expiry_today"},
                {"metric": "minutes_until_mcx_close"},
            ],
        )
        assert line.startswith("Expiry pre-close — ")
        assert "a held MCX position expires today" in line
        assert "MCX closes in 30 minutes" in line

    def test_real_breach_metric_returns_none(self):
        from backend.api.algo.agent_engine import _v2_reminder_only_line

        class _Agent:
            name = "loss-positions-total"

        assert _v2_reminder_only_line(
            _Agent(), [{"metric": "pnl_rate_abs"}],
        ) is None

    def test_mixed_breach_and_reminder_returns_none(self):
        """A real breach leaf anywhere in the match set must still render
        the full table — never silently drop the breach info because a
        reminder-shaped metric happened to be in the same match list."""
        from backend.api.algo.agent_engine import _v2_reminder_only_line

        class _Agent:
            name = "mixed"

        assert _v2_reminder_only_line(
            _Agent(), [
                {"metric": "is_mcx_expiry_today"},
                {"metric": "pnl_rate_abs"},
            ],
        ) is None

    def test_empty_matches_returns_none(self):
        from backend.api.algo.agent_engine import _v2_reminder_only_line

        class _Agent:
            name = "whatever"

        assert _v2_reminder_only_line(_Agent(), []) is None


class TestCooldownMigrationOnSync:
    """2026-11-22 retime regression: `cooldown_minutes` is an
    operator-editable field and is NEVER force-synced on an ordinary
    restart (see `_ae_sync_existing_builtin`'s own docstring) — so
    shipping the new 30-min value in BUILTIN_AGENTS alone left an
    already-seeded prod/dev row stuck at its old 180-min value,
    confirmed live on dev right after deploying the lead-time retime
    (the conditions migration for the sibling expiry-nfo-risk-alert
    correctly ran; cooldown silently did not, because nothing in
    `_ae_sync_existing_builtin` touched it for these two slugs). This
    pins the dedicated, value-gated migration added to fix that gap."""

    @pytest.mark.parametrize("slug", [
        "expiry-day-preclose-alert", "expiry-day-itm-auto-close",
    ])
    def test_stale_180_row_migrates_to_30(self, slug):
        from types import SimpleNamespace
        from backend.api.algo.agent_engine import _ae_sync_existing_builtin

        agent_def = _by_slug(slug)
        existing = SimpleNamespace(
            conditions=agent_def["conditions"], long_name=None,
            schedule="always", tier=agent_def["tier"], topic="expiry_warning",
            status="active", events=list(agent_def["events"]), actions=[],
            cooldown_minutes=180, fire_at_time=None, last_fired=None,
        )
        _ae_sync_existing_builtin(existing, agent_def)
        assert existing.cooldown_minutes == 30, (
            f"{slug}: a stock row still at the old 180-min cooldown must "
            f"migrate to 30 on restart, got {existing.cooldown_minutes}"
        )

    @pytest.mark.parametrize("slug", [
        "expiry-day-preclose-alert", "expiry-day-itm-auto-close",
    ])
    def test_operator_customized_cooldown_is_left_alone(self, slug):
        """Value-gated, not slug-only — an operator who already set a
        different cooldown (not the exact old stock 180) must never be
        silently overwritten."""
        from types import SimpleNamespace
        from backend.api.algo.agent_engine import _ae_sync_existing_builtin

        agent_def = _by_slug(slug)
        existing = SimpleNamespace(
            conditions=agent_def["conditions"], long_name=None,
            schedule="always", tier=agent_def["tier"], topic="expiry_warning",
            status="active", events=list(agent_def["events"]), actions=[],
            cooldown_minutes=45, fire_at_time=None, last_fired=None,
        )
        _ae_sync_existing_builtin(existing, agent_def)
        assert existing.cooldown_minutes == 45, (
            "an operator-customized cooldown (not the exact old stock "
            "180 value) must never be overwritten by this migration"
        )

    @pytest.mark.parametrize("slug", [
        "expiry-day-preclose-alert", "expiry-day-itm-auto-close",
    ])
    def test_already_migrated_row_is_a_noop(self, slug):
        from types import SimpleNamespace
        from backend.api.algo.agent_engine import _ae_sync_existing_builtin

        agent_def = _by_slug(slug)
        existing = SimpleNamespace(
            conditions=agent_def["conditions"], long_name=None,
            schedule="always", tier=agent_def["tier"], topic="expiry_warning",
            status="active", events=list(agent_def["events"]), actions=[],
            cooldown_minutes=30, fire_at_time=None, last_fired=None,
        )
        _ae_sync_existing_builtin(existing, agent_def)
        assert existing.cooldown_minutes == 30
