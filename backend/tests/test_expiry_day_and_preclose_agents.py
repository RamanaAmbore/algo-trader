"""BUILTIN_AGENTS configuration for the expiry-day and market pre-close agents.

Covers:
  - market-preclose-nonmcx (NSE counterpart of market-preclose-mcx-v2)
  - expiry-day-{mcx,nse}-preclose-alert / expiry-day-{mcx,nse}-itm-auto-close
    (exchange-pure, split 2026-11-26 from a single combined
    expiry-day-preclose-alert/expiry-day-itm-auto-close pair that existed
    2026-10-29 to 2026-11-26 — see agent_engine.py's own history comment
    above `_EXPIRY_DAY_AWARENESS_AGENTS` for the live incident this fixes:
    a self-contradicting alert body caused by a shared, no-exchange-identity
    condition combined with a day-early false trigger)
  - expiry-day-mcx-morning-preview / expiry-day-nse-morning-preview
    (already exchange-pure since 2026-11-22; unaffected by the split, but
    shares the same day-early-fix infrastructure)
  - market-preclose-mcx / expiry-day-mcx / expiry-day-nse / expiry-day-mcx-v2
    / expiry-day-nse-v2 / expiry-day-equity-itm-auto-close /
    expiry-day-commodity-itm-auto-close / expiry-day-preclose-alert /
    expiry-day-itm-auto-close (all retired slugs) are absent, so the
    boot-time `_ae_prune_retired_builtins` sweep has a real row to delete
    for each

The pruning mechanism itself is DB-backed and covered by
test_bracket_agent_unification.py; here we only assert the registry side
(slug not in BUILTIN_AGENTS), which is what the sweep keys on.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from backend.api.algo.agent_engine import BUILTIN_AGENTS
from backend.api.algo.agent_evaluator import Context


def _by_slug(slug: str) -> dict | None:
    return next((a for a in BUILTIN_AGENTS if a.get('slug') == slug), None)


def _channels(agent: dict) -> set[str]:
    return {e["channel"] for e in agent.get("events", [])}


def _ctx(*, position_rows=None, segment_flags=None, now=None) -> Context:
    return Context(
        sum_positions=pd.DataFrame(),
        position_rows=position_rows or [],
        segment_flags=segment_flags or {},
        now=now or datetime.now(timezone.utc),
    )


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
        since expiry-day-nse-preclose-alert (exchange-pure split of what
        was briefly a combined expiry-day-preclose-alert, 2026-10-29 to
        2026-11-26) already covers that same moment with richer content.
        See test_expiry_same_window_consolidation.py for the
        suppression-behavior coverage."""
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


class TestExpiryDayPrecloseAlertAgents:
    """expiry-day-{mcx,nse}-preclose-alert: exchange-pure pair, split
    2026-11-26 from a single combined expiry-day-preclose-alert (live
    incident: a shared, no-exchange-identity condition let an MCX-only
    fire render "No MCX F&O positions are expiring today" under a title
    naming "MCX or NFO" — confirming via the sibling that the real cause
    was the exchange-pure NFO window not being involved at all that
    evening). Same consolidation history as before (2026-10-29,
    operator: "can you create one agent for order placing and one agent
    for alert instead of two" / "why can't you have one agent for nfo
    and mcx"; retimed 30→65 on 2026-11-22) — the split preserves the
    Python-side exchange-loop/lead-time logic (no DSL compound tree),
    just binds the factory twice instead of once."""

    @pytest.mark.parametrize("slug,metric", [
        ("expiry-day-mcx-preclose-alert", "is_mcx_expiry_preclose_alert_window"),
        ("expiry-day-nse-preclose-alert", "is_nse_expiry_preclose_alert_window"),
    ])
    def test_exists_with_exchange_pure_flat_condition(self, slug, metric):
        agent = _by_slug(slug)
        assert agent is not None, f"{slug} not found in BUILTIN_AGENTS"
        assert agent.get('fire_at_time') is None
        assert agent.get('conditions') == {
            "op": "==", "scope": "funds.total", "metric": metric, "value": 1.0,
        }, f"unexpected conditions {agent.get('conditions')}"

    @pytest.mark.parametrize("slug", [
        "expiry-day-mcx-preclose-alert", "expiry-day-nse-preclose-alert",
    ])
    def test_is_high_tier(self, slug):
        agent = _by_slug(slug)
        assert agent is not None
        assert agent.get('tier') == "high"

    @pytest.mark.parametrize("slug", [
        "expiry-day-mcx-preclose-alert", "expiry-day-nse-preclose-alert",
    ])
    def test_cooldown_is_30_minutes(self, slug):
        """30 min, unchanged from the combined shape's own 2026-11-22
        retime — short enough that the operator is re-notified of a
        newly-staged leg within the 65-min window (matching the sibling
        close-action agent's own periodic re-scan cadence)."""
        agent = _by_slug(slug)
        assert agent is not None
        assert agent.get('cooldown_minutes') == 30, \
            f"expected cooldown_minutes=30, got {agent.get('cooldown_minutes')}"

    @pytest.mark.parametrize("slug", [
        "expiry-day-mcx-preclose-alert", "expiry-day-nse-preclose-alert",
    ])
    def test_events_cover_all_four_channels_with_high_ntfy(self, slug):
        agent = _by_slug(slug)
        assert agent is not None
        assert _channels(agent) == {"telegram", "email", "log", "ntfy"}
        ntfy = next(e for e in agent["events"] if e["channel"] == "ntfy")
        assert ntfy.get("priority") == "high"

    def test_topics_are_distinct_from_each_other_and_from_risk_alerts(self):
        """A shared topic merges same-tick co-fires' match data into one
        winner's alert body (`_compute_topic_suppression`) — exactly the
        cross-exchange conflation this split exists to fix. Each new
        agent's topic must be distinct from its sibling AND from the
        already-split expiry-{nfo,mcx}-risk-alert pair's own topics, and
        from the shared `expiry_warning` the morning-preview/generic
        reminder agents still use."""
        mcx = _by_slug("expiry-day-mcx-preclose-alert")
        nse = _by_slug("expiry-day-nse-preclose-alert")
        reserved = {"expiry_warning", "expiry_warning_mcx", "expiry_warning_nfo"}
        assert mcx["topic"] != nse["topic"]
        assert mcx["topic"] not in reserved
        assert nse["topic"] not in reserved


class TestExpiryDayItmAutoCloseAgents:
    """expiry-day-{mcx,nse}-itm-auto-close: exchange-pure order-placing
    pair, split 2026-11-26 from a single combined expiry-day-itm-auto-
    close, firing starting 60 minutes before each exchange's own close
    (5 min after the alert sibling's 65-min window), then re-firing
    every 30 minutes while still in that window."""

    @pytest.mark.parametrize("slug,metric,exchange", [
        ("expiry-day-mcx-itm-auto-close", "is_mcx_expiry_preclose_close_window", "MCX"),
        ("expiry-day-nse-itm-auto-close", "is_nse_expiry_preclose_close_window", "NFO"),
    ])
    def test_condition_and_action_exchange_are_the_same_exchange(self, slug, metric, exchange):
        """The condition's own exchange and the action's params.exchange
        must name the SAME exchange — a drift between the two would
        silently reproduce the cross-exchange conflation this split
        fixes (e.g. an MCX-triggered agent whose action closes NFO)."""
        agent = _by_slug(slug)
        assert agent is not None, f"{slug} not found in BUILTIN_AGENTS"
        assert agent.get('fire_at_time') is None
        assert agent.get('conditions') == {
            "op": "==", "scope": "funds.total", "metric": metric, "value": 1.0,
        }, f"unexpected conditions {agent.get('conditions')}"
        actions = agent.get('actions') or []
        assert len(actions) == 1
        assert actions[0]["type"] == "expiry_auto_close"
        assert actions[0]["params"] == {"exchange": exchange}, (
            f"{slug}: action params must narrow to exactly {exchange!r} — "
            f"got {actions[0]['params']!r}. _action_live_expiry_auto_close "
            "already supported this narrowing before the split; only the "
            "agent definitions needed to set it."
        )

    @pytest.mark.parametrize("slug", [
        "expiry-day-mcx-itm-auto-close", "expiry-day-nse-itm-auto-close",
    ])
    def test_is_critical_tier(self, slug):
        agent = _by_slug(slug)
        assert agent is not None
        assert agent.get('tier') == "critical"

    @pytest.mark.parametrize("slug", [
        "expiry-day-mcx-itm-auto-close", "expiry-day-nse-itm-auto-close",
    ])
    def test_cooldown_is_30_minutes(self, slug):
        agent = _by_slug(slug)
        assert agent is not None
        assert agent.get('cooldown_minutes') == 30

    @pytest.mark.parametrize("slug", [
        "expiry-day-mcx-itm-auto-close", "expiry-day-nse-itm-auto-close",
    ])
    def test_status_is_active(self, slug):
        """New slugs, not bound by the _ae_guard_seed_status fail-safe
        (which only triggers on a literal 'Ships INACTIVE' phrase in the
        description — this agent's real safety net is the order-level
        hold-by-default in ExpiryEngine.close_positions(), not agent
        deactivation)."""
        agent = _by_slug(slug)
        assert agent is not None
        assert agent.get('status') == "active"
        assert "Ships INACTIVE" not in (agent.get('description') or ""), \
            "this phrase would force status back to inactive on every restart " \
            "via _ae_guard_seed_status — must not appear here"

    @pytest.mark.parametrize("slug", [
        "expiry-day-mcx-itm-auto-close", "expiry-day-nse-itm-auto-close",
    ])
    def test_events_include_ntfy_high(self, slug):
        agent = _by_slug(slug)
        assert agent is not None
        assert _channels(agent) == {"telegram", "email", "log", "ntfy"}
        ntfy = next(e for e in agent["events"] if e["channel"] == "ntfy")
        assert ntfy.get("priority") == "high"

    @pytest.mark.parametrize("slug", [
        "expiry-day-mcx-itm-auto-close", "expiry-day-nse-itm-auto-close",
    ])
    def test_trade_mode_is_live(self, slug):
        """2026-10-31 live-incident fix, preserved across the split:
        without an explicit trade_mode here, this agent silently
        inherits execution.default_agent_trade_mode ('paper') and the
        real hold-gate path (HoldCategory.EXPIRY_CLOSE, held by default)
        never engages."""
        agent = _by_slug(slug)
        assert agent is not None
        assert agent.get('trade_mode') == "live", \
            "must explicitly set trade_mode='live' — omitting it silently " \
            "falls back to the paper default and the hold-gate never engages"

    def test_topics_are_distinct_from_each_other_and_from_the_alert_pair(self):
        mcx_close = _by_slug("expiry-day-mcx-itm-auto-close")
        nse_close = _by_slug("expiry-day-nse-itm-auto-close")
        mcx_alert = _by_slug("expiry-day-mcx-preclose-alert")
        nse_alert = _by_slug("expiry-day-nse-preclose-alert")
        topics = {mcx_close["topic"], nse_close["topic"], mcx_alert["topic"], nse_alert["topic"]}
        assert len(topics) == 4, f"all 4 topics must be pairwise distinct, got {topics}"


class TestExpiryDayAgentsExchangePurity:
    """Regression coverage for the real design property the split
    establishes: the 4 new agents' conditions each name a DIFFERENT
    metric, so there is no shared latch key, no cross-exchange
    suppression risk, and no DSL-level way for one exchange's fire to
    render or act on the other's data — structurally, not just by
    convention. (The combined shape's old same-metric cross-exchange
    no-suppression regression test is now moot: there is no shared
    metric left to test for collision.)"""

    def test_all_four_metrics_are_pairwise_distinct(self):
        slugs = [
            "expiry-day-mcx-preclose-alert", "expiry-day-nse-preclose-alert",
            "expiry-day-mcx-itm-auto-close", "expiry-day-nse-itm-auto-close",
        ]
        metrics = {_by_slug(s)["conditions"]["metric"] for s in slugs}
        assert len(metrics) == 4, f"all 4 metrics must be pairwise distinct, got {metrics}"

    @pytest.mark.parametrize("slug,metric", [
        ("expiry-day-mcx-preclose-alert", "is_mcx_expiry_preclose_alert_window"),
        ("expiry-day-nse-preclose-alert", "is_nse_expiry_preclose_alert_window"),
        ("expiry-day-mcx-itm-auto-close", "is_mcx_expiry_preclose_close_window"),
        ("expiry-day-nse-itm-auto-close", "is_nse_expiry_preclose_close_window"),
    ])
    def test_periodic_refire_within_one_exchanges_own_window(self, slug, metric):
        """Single-exchange version of the old cross-exchange latch
        test — 30-min cooldown blocks an immediate re-fire but still
        allows the periodic re-scan (>30 min later, same window) the
        2026-11-22 retime exists for. Nothing cross-exchange to test
        here any more — each metric's own latch is exchange-scoped by
        construction."""
        from datetime import timedelta
        from backend.api.algo.agent_engine import _v2_apply_escalation_gate

        class _Agent:
            pass
        agent = _Agent()
        agent.slug = slug

        store: dict = {}  # isolated — never the shared module-level _V2_LATCH
        now0 = datetime(2026, 10, 30, 9, 30, 0, tzinfo=timezone.utc)
        match = [{"metric": metric, "scope": "funds.total", "op": "==",
                  "value": 1.0, "threshold": 1.0, "row": {}}]
        assert _v2_apply_escalation_gate(agent, match, now0, 30, store=store), \
            "the first fire must pass the gate"

        now_dup = now0 + timedelta(minutes=5)
        assert not _v2_apply_escalation_gate(agent, match, now_dup, 30, store=store), \
            "a re-fire 5 min later (same window) must be suppressed by the cooldown"

        now_rescan = now0 + timedelta(minutes=31)
        assert _v2_apply_escalation_gate(agent, match, now_rescan, 30, store=store), \
            "a re-fire >30 min later, still inside the same window, must be " \
            "ALLOWED — this is the periodic re-scan the 2026-11-22 retime exists for"


class TestRetiredSlugs:
    """Old slugs must be gone from the registry so _ae_prune_retired_builtins
    deletes the stale DB rows instead of leaving them stuck on a condition
    shape the sync-on-restart logic can't detect as stale."""

    @pytest.mark.parametrize("slug", [
        "market-preclose-mcx", "expiry-day-mcx", "expiry-day-nse",
        "expiry-day-mcx-v2", "expiry-day-nse-v2",
        "expiry-day-equity-itm-auto-close", "expiry-day-commodity-itm-auto-close",
        # 2026-11-26 exchange-pure split — the combined shape is retired.
        "expiry-day-preclose-alert", "expiry-day-itm-auto-close",
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

    @pytest.mark.parametrize("metric,expected_exchange_word,unexpected_word", [
        ("is_mcx_expiry_preclose_alert_window", "MCX", "NFO"),
        ("is_nse_expiry_preclose_alert_window", "NSE", None),
        ("is_mcx_expiry_preclose_close_window", "MCX", "NFO"),
        ("is_nse_expiry_preclose_close_window", "NSE", None),
    ])
    def test_new_per_exchange_labels_name_only_their_own_exchange(
        self, metric, expected_exchange_word, unexpected_word,
    ):
        """2026-11-26 split regression — the whole point: the rendered
        reminder line for a per-exchange token must name ONLY that
        exchange, never the other one, unlike the old combined labels
        ("a held MCX or NFO position expires today...") this split
        replaces at the agent level."""
        from backend.api.algo.agent_engine import _v2_reminder_only_line

        class _Agent:
            name = "whatever"

        line = _v2_reminder_only_line(_Agent(), [{"metric": metric}])
        assert line is not None
        assert line.startswith("Expiry pre-close — ")
        assert expected_exchange_word in line
        if unexpected_word:
            assert unexpected_word not in line, (
                f"{metric}'s label must never name {unexpected_word} — "
                f"got: {line!r}"
            )


class TestExpiryMorningPreviewAgents:
    """expiry-day-mcx-morning-preview / expiry-day-nse-morning-preview —
    TWO separate per-exchange agents added 2026-11-22 (operator: "the
    morning mcx or nfo message, instead of just showing it is market
    close day, it should show the positions that need to be closed at
    that time"). Deliberately NOT one shared agent like the pre-close
    pair used to be — see `metric_is_expiry_morning_preview_window.py`'s
    own module docstring: MCX opens 09:00, NSE 09:15 (only ~15 min
    apart), and `_cycle_in_cooldown` gates at the AGENT level, so a
    single shared agent firing for one exchange would block it from
    ever evaluating the other exchange's own branch on a dual-expiry
    day. Unaffected by the 2026-11-26 preclose-pair split; shares only
    the exact-today fix infrastructure."""

    @pytest.mark.parametrize("slug,metric,name", [
        ("expiry-day-mcx-morning-preview", "is_mcx_expiry_morning_preview_window", "MCX morning preview"),
        ("expiry-day-nse-morning-preview", "is_nse_expiry_morning_preview_window", "NSE morning preview"),
    ])
    def test_exists_with_flat_condition(self, slug, metric, name):
        agent = _by_slug(slug)
        assert agent is not None, f"{slug} not found in BUILTIN_AGENTS"
        assert agent.get('name') == name
        assert agent.get('conditions') == {
            "op": "==", "scope": "funds.total", "metric": metric, "value": 1.0,
        }, f"unexpected conditions {agent.get('conditions')}"
        assert agent.get('fire_at_time') is None

    @pytest.mark.parametrize("slug", [
        "expiry-day-mcx-morning-preview", "expiry-day-nse-morning-preview",
    ])
    def test_cooldown_is_1320_minutes_once_per_day(self, slug):
        """1320 min (22h) is SAFE here, unlike the pre-close pair's own
        30-min design — each agent's own metric is exchange-specific,
        so there is no cross-exchange collision to design around.
        Matches the exact market-preclose-mcx-v2/market-preclose-nonmcx
        precedent (two agents, each 1320)."""
        agent = _by_slug(slug)
        assert agent is not None
        assert agent.get('cooldown_minutes') == 1320

    @pytest.mark.parametrize("slug", [
        "expiry-day-mcx-morning-preview", "expiry-day-nse-morning-preview",
    ])
    def test_is_high_tier_no_action(self, slug):
        agent = _by_slug(slug)
        assert agent is not None
        assert agent.get('tier') == "high"
        assert agent.get('actions') == []

    @pytest.mark.parametrize("slug", [
        "expiry-day-mcx-morning-preview", "expiry-day-nse-morning-preview",
    ])
    def test_events_cover_all_four_channels_with_high_ntfy(self, slug):
        agent = _by_slug(slug)
        assert agent is not None
        assert _channels(agent) == {"telegram", "email", "log", "ntfy"}
        ntfy = next(e for e in agent["events"] if e["channel"] == "ntfy")
        assert ntfy.get("priority") == "high"

    def test_the_two_metrics_are_distinct_strings(self):
        """The whole point of the per-exchange design: different metric
        names mean different latch keys, so the two agents' cooldowns
        can never collide with each other regardless of how close
        MCX's and NSE's own open times are."""
        mcx_agent = _by_slug("expiry-day-mcx-morning-preview")
        nse_agent = _by_slug("expiry-day-nse-morning-preview")
        assert mcx_agent["conditions"]["metric"] != nse_agent["conditions"]["metric"]


class TestExpiryMorningPreviewFactory:
    """`_metric_factory_is_expiry_morning_preview_window` — the shared
    factory both agents' tokens are bound from (operator: "the time
    should be a parameter in the function in the DSL grammar to make
    it generic")."""

    def test_factory_boundary_logic_with_fake_resolvers(self, monkeypatch):
        """Tests the factory's own gating logic directly — not expiring
        EXACTLY today (2026-11-26 fix: `_is_exchange_expiring_exactly_
        today`, not the old ≤1.5-day `is_{mcx,nse}_expiry_today`) AND
        open long enough, independent of the real symbol-parsing/
        calendar machinery (which would make a test depend on today's
        real date). Monkeypatches the factory module's own `_is_
        exchange_expiring_exactly_today` name (imported into this
        module's namespace) so a FRESH factory call (made here, after
        patching) binds against the fake, leaving the two real
        module-level bindings (already bound at import time against the
        real helper) untouched."""
        import backend.api.algo.grammar_tokens.metric_is_expiry_morning_preview_window as mod

        cases = [
            # (expiring_exactly_today, minutes_since_open, expected)
            (False, 10, 0.0),  # not expiring exactly today -> never fires regardless of open time
            (True, 2, 0.0),    # expiring exactly today, but open <5 min -> too early
            (True, None, 0.0),  # expiring exactly today, but segment closed (None) -> not in window
            (True, 5, 1.0),    # expiring exactly today, open exactly at the buffer -> fires
            (True, 30, 1.0),   # expiring exactly today, well past the buffer -> fires
        ]
        for expiring_today, minutes_since_open, expected in cases:
            monkeypatch.setattr(
                mod, "_is_exchange_expiring_exactly_today",
                lambda ctx, exchange: expiring_today,
            )
            monkeypatch.setitem(mod._MINUTES_SINCE_OPEN_RESOLVERS, "MCX", lambda ctx, row: minutes_since_open)
            resolver = mod._metric_factory_is_expiry_morning_preview_window("MCX", 5)
            result = resolver(None, None)
            assert result == expected, (
                f"expiring_today={expiring_today}, minutes_since_open={minutes_since_open}: "
                f"expected {expected}, got {result}"
            )

    def test_two_bindings_are_independent_closures(self):
        """Confirms the factory genuinely produces two distinct resolver
        functions (not one shared mutable closure state) — calling the
        MCX binding must never affect the NSE binding's own result."""
        from backend.api.algo.grammar_tokens.metric_is_expiry_morning_preview_window import (
            _metric_is_mcx_expiry_morning_preview_window,
            _metric_is_nse_expiry_morning_preview_window,
        )
        assert _metric_is_mcx_expiry_morning_preview_window is not _metric_is_nse_expiry_morning_preview_window


class TestIsExchangeExpiringExactlyToday:
    """`_is_exchange_expiring_exactly_today` — the 2026-11-26 root-cause
    fix. Live incident: the pre-close window gate used to read `is_mcx_
    expiry_today`/`is_nse_expiry_today` (a ≤1.5-day lookahead, correct
    for THEIR other callers), so it could go true for an exchange purely
    because a position expires TOMORROW, while `ExpiryEngine.scan_
    positions()`'s own exact `expiry == today` filter then found nothing
    — producing a self-contradicting alert. This fixes it by requiring
    `days_to_expiry < 1.0`, reusing the SAME NFO symbol/expiry pair
    `test_scope_positions_expiring_today.py` already confirms parses to
    2025-04-24, only varying `now` — the day before (tomorrow case) vs.
    the day of (exactly-today case)."""

    SYMBOL_ROW = {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO", "quantity": 50}

    def test_position_expiring_tomorrow_is_not_exactly_today(self):
        from backend.api.algo.grammar_tokens.metric_is_expiry_preclose_alert_window import (
            _is_exchange_expiring_exactly_today,
        )
        # 2025-04-23 10:00 IST = 04:30 UTC -- one day before the real
        # 2025-04-24 15:30 IST NSE close. days_to_expiry ~= 1.229, which
        # satisfies the OLD <=1.5-day buffer (the live incident's actual
        # cause) but must NOT satisfy this tighter <1.0 check.
        now = datetime(2025, 4, 23, 4, 30, 0, tzinfo=timezone.utc)
        ctx = _ctx(now=now, position_rows=[self.SYMBOL_ROW])
        assert _is_exchange_expiring_exactly_today(ctx, "NFO") is False, (
            "a position expiring TOMORROW must not read as expiring exactly "
            "today -- this is the exact live-incident false trigger"
        )

    def test_position_expiring_today_is_exactly_today(self):
        from backend.api.algo.grammar_tokens.metric_is_expiry_preclose_alert_window import (
            _is_exchange_expiring_exactly_today,
        )
        # 2025-04-24 10:00 IST = 04:30 UTC -- the expiry day itself,
        # hours before the 15:30 IST close. days_to_expiry ~= 0.229.
        now = datetime(2025, 4, 24, 4, 30, 0, tzinfo=timezone.utc)
        ctx = _ctx(now=now, position_rows=[self.SYMBOL_ROW])
        assert _is_exchange_expiring_exactly_today(ctx, "NFO") is True

    def test_wrong_exchange_never_matches(self):
        from backend.api.algo.grammar_tokens.metric_is_expiry_preclose_alert_window import (
            _is_exchange_expiring_exactly_today,
        )
        now = datetime(2025, 4, 24, 4, 30, 0, tzinfo=timezone.utc)
        ctx = _ctx(now=now, position_rows=[self.SYMBOL_ROW])
        assert _is_exchange_expiring_exactly_today(ctx, "MCX") is False, (
            "an NFO position must never make the MCX check true -- this is "
            "the cross-exchange-conflation half of the live incident"
        )

    def test_empty_position_rows_is_false(self):
        from backend.api.algo.grammar_tokens.metric_is_expiry_preclose_alert_window import (
            _is_exchange_expiring_exactly_today,
        )
        ctx = _ctx(position_rows=[])
        assert _is_exchange_expiring_exactly_today(ctx, "NFO") is False

    def test_unparseable_symbol_is_skipped_not_raised(self):
        from backend.api.algo.grammar_tokens.metric_is_expiry_preclose_alert_window import (
            _is_exchange_expiring_exactly_today,
        )
        ctx = _ctx(position_rows=[{"tradingsymbol": "RELIANCE", "exchange": "NSE", "quantity": 10}])
        assert _is_exchange_expiring_exactly_today(ctx, "NSE") is False


class TestExpiryPrecloseExchangePurityAtTokenLevel:
    """Direct regression for the cross-exchange-conflation half of the
    live incident, at the resolver level (not just the agent-config
    level above): with ONLY a genuinely in-window NFO position, the
    MCX-named per-exchange token must stay 0.0, and vice versa."""

    def test_nfo_only_in_window_mcx_token_stays_zero(self):
        from backend.api.algo.grammar_tokens.metric_is_expiry_preclose_alert_window import (
            _metric_is_mcx_expiry_preclose_alert_window,
            _metric_is_nse_expiry_preclose_alert_window,
        )
        # 2025-04-24 14:30 IST = 09:00 UTC -- 60 min before the 15:30 IST
        # NSE close, well inside the 65-min alert lead time. segment_
        # flags is set directly (minutes_until_nse_close is flag-driven,
        # not derived from days_to_expiry) so this test is deterministic
        # regardless of real market-hours machinery.
        now = datetime(2025, 4, 24, 9, 0, 0, tzinfo=timezone.utc)
        ctx = _ctx(
            now=now,
            position_rows=[{"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO", "quantity": 50}],
            segment_flags={"nse_open": True, "minutes_until_nse_close": 60,
                           "mcx_open": False},
        )
        assert _metric_is_nse_expiry_preclose_alert_window(ctx, None) == 1.0
        assert _metric_is_mcx_expiry_preclose_alert_window(ctx, None) == 0.0, (
            "an NFO-only breach must never make the MCX-named token read "
            "1.0 -- this is the exact live incident (an alert titled about "
            "MCX firing on nothing but NFO activity, or vice versa)"
        )

    def test_mcx_only_in_window_nse_token_stays_zero(self):
        from backend.api.algo.grammar_tokens.metric_is_expiry_preclose_alert_window import (
            _metric_is_mcx_expiry_preclose_alert_window,
            _metric_is_nse_expiry_preclose_alert_window,
        )
        # GOLD26NOV123000PE's real expiry is 2026-11-27 (confirmed by
        # test_scope_positions_expiring_today.py's own DST-aware-close
        # test against the same symbol) -- "now" must be ON that date,
        # close to its 23:55 IST winter close, for days_to_expiry to
        # read "exactly today".
        now = datetime(2026, 11, 27, 18, 0, 0, tzinfo=timezone.utc)  # 23:30 IST, 25 min before close
        ctx = _ctx(
            now=now,
            position_rows=[{"tradingsymbol": "GOLD26NOV123000PE", "exchange": "MCX", "quantity": -100}],
            segment_flags={"mcx_open": True, "minutes_until_mcx_close": 25,
                           "nse_open": False},
        )
        assert _metric_is_mcx_expiry_preclose_alert_window(ctx, None) == 1.0
        assert _metric_is_nse_expiry_preclose_alert_window(ctx, None) == 0.0
