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
        agent = _by_slug('market-preclose-nonmcx')
        assert agent is not None
        assert agent.get('conditions') == {
            "op": "<=", "scope": "funds.total",
            "metric": "minutes_until_nse_close", "value": 30,
        }, f"Unexpected conditions: {agent.get('conditions')}"

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
    firing within 30 minutes of whichever exchange's own close applies
    (2026-10-29 consolidation — operator: "can you create one agent for
    order placing and one agent for alert instead of two" / "why can't
    you have one agent for nfo and mcx")."""

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

    def test_cooldown_is_180_minutes_not_22_hours(self):
        """180 min (3h), not the 22h used elsewhere — a 22h cooldown on
        this single shared metric would let NFO's ~15:00 fire suppress
        MCX's own ~23:00 fire the same day. See the comment above
        _EXPIRY_DAY_AWARENESS_AGENTS for the full reasoning."""
        agent = _by_slug(self.SLUG)
        assert agent is not None
        assert agent.get('cooldown_minutes') == 180, \
            f"expected cooldown_minutes=180, got {agent.get('cooldown_minutes')}"

    def test_events_cover_all_four_channels_with_high_ntfy(self):
        agent = _by_slug(self.SLUG)
        assert agent is not None
        assert _channels(agent) == {"telegram", "email", "log", "ntfy"}
        ntfy = next(e for e in agent["events"] if e["channel"] == "ntfy")
        assert ntfy.get("priority") == "high"


class TestExpiryDayItmAutoCloseAgent:
    """expiry-day-itm-auto-close: ONE order-placing agent covering both
    MCX and NFO, firing 25 minutes before each exchange's own close (5
    min after the alert sibling's 30-min window)."""

    SLUG = "expiry-day-itm-auto-close"

    def test_exists_in_builtin_agents(self):
        assert _by_slug(self.SLUG) is not None, f"{self.SLUG} not found in BUILTIN_AGENTS"

    def test_is_token_driven_not_scheduled(self):
        agent = _by_slug(self.SLUG)
        assert agent is not None
        assert agent.get('fire_at_time') is None

    def test_condition_is_one_flat_leaf_at_25_minutes(self):
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

    def test_cooldown_is_180_minutes(self):
        agent = _by_slug(self.SLUG)
        assert agent is not None
        assert agent.get('cooldown_minutes') == 180

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

    def test_events_have_no_ntfy(self):
        """Critical-tier order-placing agent: telegram+email+log only,
        matching the legacy agents' own channel set — not an ntfy push
        (the held-order-by-default design means this isn't an
        action-required alert the way a live close failure would be)."""
        agent = _by_slug(self.SLUG)
        assert agent is not None
        assert _channels(agent) == {"telegram", "email", "log"}


class TestExpiryDayAgentsSameDayNoSuppression:
    """Regression test for the one real risk the consolidation
    introduced: both agents now share ONE metric name across MCX and
    NFO, so the per-fire latch (keyed on agent_slug+metric+scope+account,
    no exchange dimension) must not let one exchange's fire suppress
    the other's on the same day. cooldown_minutes=180 is the fix —
    this test pins the actual escalation-gate behaviour, not just the
    config value."""

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
        effective = _v2_apply_escalation_gate(agent, nfo_match, now0, 180, store=store)
        assert effective, "NFO's own fire must pass the gate"

        # A SECOND fire 5 minutes later (still well inside the 180-min
        # cooldown) must be suppressed — same exchange's own window
        # should not double-fire every cycle tick.
        now_dup = now0 + timedelta(minutes=5)
        dup_match = [{"metric": metric, "scope": "funds.total", "op": "==",
                      "value": 1.0, "threshold": 1.0, "row": {}}]
        effective_dup = _v2_apply_escalation_gate(agent, dup_match, now_dup, 180, store=store)
        assert not effective_dup, \
            "a re-fire 5 min later (same window) must be suppressed by the cooldown"

        # MCX fires ~7.5h later the SAME day (its own ~23:00 window) —
        # must NOT be suppressed by NFO's 180-min cooldown, which has
        # long since cleared.
        now1 = now0 + timedelta(hours=7.5)
        mcx_match = [{"metric": metric, "scope": "funds.total", "op": "==",
                      "value": 1.0, "threshold": 1.0, "row": {}}]
        effective2 = _v2_apply_escalation_gate(agent, mcx_match, now1, 180, store=store)
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
