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


class TestExpiryDayAgents:
    """expiry-day-mcx-v2 / expiry-day-nse-v2: held F&O expiring today,
    firing within 30 minutes of that exchange's own close (2026-10-29
    retime — operator: "it should be 30 mins before market close")."""

    @pytest.mark.parametrize("slug,metric,close_metric", [
        ("expiry-day-mcx-v2", "is_mcx_expiry_today", "minutes_until_mcx_close"),
        ("expiry-day-nse-v2", "is_nse_expiry_today", "minutes_until_nse_close"),
    ])
    def test_exists_in_builtin_agents(self, slug, metric, close_metric):
        assert _by_slug(slug) is not None, f"{slug} not found in BUILTIN_AGENTS"

    @pytest.mark.parametrize("slug,metric,close_metric", [
        ("expiry-day-mcx-v2", "is_mcx_expiry_today", "minutes_until_mcx_close"),
        ("expiry-day-nse-v2", "is_nse_expiry_today", "minutes_until_nse_close"),
    ])
    def test_is_token_driven_not_scheduled(self, slug, metric, close_metric):
        agent = _by_slug(slug)
        assert agent is not None
        assert agent.get('fire_at_time') is None, \
            f"{slug}: expected fire_at_time=None, got {agent.get('fire_at_time')}"

    @pytest.mark.parametrize("slug,metric,close_metric", [
        ("expiry-day-mcx-v2", "is_mcx_expiry_today", "minutes_until_mcx_close"),
        ("expiry-day-nse-v2", "is_nse_expiry_today", "minutes_until_nse_close"),
    ])
    def test_condition_is_expiry_today_and_within_30min_of_close(self, slug, metric, close_metric):
        agent = _by_slug(slug)
        assert agent is not None
        assert agent.get('conditions') == {"all": [
            {"op": "==", "scope": "funds.total", "metric": metric, "value": 1.0},
            {"op": "<=", "scope": "funds.total", "metric": close_metric, "value": 30},
        ]}, f"{slug}: unexpected conditions {agent.get('conditions')}"

    @pytest.mark.parametrize("slug", ["expiry-day-mcx-v2", "expiry-day-nse-v2"])
    def test_is_high_tier(self, slug):
        agent = _by_slug(slug)
        assert agent is not None
        assert agent.get('tier') == "high", f"{slug}: expected tier='high', got {agent.get('tier')}"

    @pytest.mark.parametrize("slug", ["expiry-day-mcx-v2", "expiry-day-nse-v2"])
    def test_cooldown_is_22_hours(self, slug):
        agent = _by_slug(slug)
        assert agent is not None
        assert agent.get('cooldown_minutes') == 1320, \
            f"{slug}: expected cooldown_minutes=1320, got {agent.get('cooldown_minutes')}"

    @pytest.mark.parametrize("slug", ["expiry-day-mcx-v2", "expiry-day-nse-v2"])
    def test_events_cover_all_four_channels_with_high_ntfy(self, slug):
        agent = _by_slug(slug)
        assert agent is not None
        assert _channels(agent) == {"telegram", "email", "log", "ntfy"}, \
            f"{slug}: expected all 4 channels, got {_channels(agent)}"
        ntfy = next(e for e in agent["events"] if e["channel"] == "ntfy")
        assert ntfy.get("priority") == "high", \
            f"{slug}: ntfy priority must be 'high' (not default/urgent), got {ntfy.get('priority')}"


class TestRetiredSlugs:
    """Old slugs must be gone from the registry so _ae_prune_retired_builtins
    deletes the stale DB rows instead of leaving them stuck on a condition
    shape the sync-on-restart logic can't detect as stale."""

    @pytest.mark.parametrize("slug", [
        "market-preclose-mcx", "expiry-day-mcx", "expiry-day-nse",
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
