"""BUILTIN_AGENTS configuration for the expiry-day and market pre-close agents.

Covers:
  - market-preclose-nonmcx (NSE counterpart of market-preclose-mcx-v2)
  - expiry-day-mcx / expiry-day-nse (held F&O expiring today reminders)
  - market-preclose-mcx (retired slug) is absent, so the boot-time
    `_ae_prune_retired_builtins` sweep has a real row to delete

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
    """expiry-day-mcx / expiry-day-nse: held F&O expiring today."""

    @pytest.mark.parametrize("slug,metric", [
        ("expiry-day-mcx", "is_mcx_expiry_today"),
        ("expiry-day-nse", "is_nse_expiry_today"),
    ])
    def test_exists_in_builtin_agents(self, slug, metric):
        assert _by_slug(slug) is not None, f"{slug} not found in BUILTIN_AGENTS"

    @pytest.mark.parametrize("slug,metric", [
        ("expiry-day-mcx", "is_mcx_expiry_today"),
        ("expiry-day-nse", "is_nse_expiry_today"),
    ])
    def test_is_token_driven_not_scheduled(self, slug, metric):
        agent = _by_slug(slug)
        assert agent is not None
        assert agent.get('fire_at_time') is None, \
            f"{slug}: expected fire_at_time=None, got {agent.get('fire_at_time')}"

    @pytest.mark.parametrize("slug,metric", [
        ("expiry-day-mcx", "is_mcx_expiry_today"),
        ("expiry-day-nse", "is_nse_expiry_today"),
    ])
    def test_condition_matches_expiry_token_equals_one(self, slug, metric):
        agent = _by_slug(slug)
        assert agent is not None
        assert agent.get('conditions') == {
            "op": "==", "scope": "funds.total", "metric": metric, "value": 1.0,
        }, f"{slug}: unexpected conditions {agent.get('conditions')}"

    @pytest.mark.parametrize("slug", ["expiry-day-mcx", "expiry-day-nse"])
    def test_is_high_tier(self, slug):
        agent = _by_slug(slug)
        assert agent is not None
        assert agent.get('tier') == "high", f"{slug}: expected tier='high', got {agent.get('tier')}"

    @pytest.mark.parametrize("slug", ["expiry-day-mcx", "expiry-day-nse"])
    def test_cooldown_is_22_hours(self, slug):
        agent = _by_slug(slug)
        assert agent is not None
        assert agent.get('cooldown_minutes') == 1320, \
            f"{slug}: expected cooldown_minutes=1320, got {agent.get('cooldown_minutes')}"

    @pytest.mark.parametrize("slug", ["expiry-day-mcx", "expiry-day-nse"])
    def test_events_cover_all_four_channels_with_high_ntfy(self, slug):
        agent = _by_slug(slug)
        assert agent is not None
        assert _channels(agent) == {"telegram", "email", "log", "ntfy"}, \
            f"{slug}: expected all 4 channels, got {_channels(agent)}"
        ntfy = next(e for e in agent["events"] if e["channel"] == "ntfy")
        assert ntfy.get("priority") == "high", \
            f"{slug}: ntfy priority must be 'high' (not default/urgent), got {ntfy.get('priority')}"


class TestRetiredPrecloseSlug:
    """market-preclose-mcx (old slug) must be gone from the registry."""

    def test_old_slug_absent_from_builtin_agents(self):
        assert _by_slug('market-preclose-mcx') is None, \
            "market-preclose-mcx is retired; it must not appear in BUILTIN_AGENTS " \
            "or _ae_prune_retired_builtins will not delete the stale DB row"
