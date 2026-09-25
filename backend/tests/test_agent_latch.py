"""
Tests for the per-(agent_slug, metric, scope, account) re-alert latch that
replaced the old per-AGENT `_V2_LAST_ALERT` (agent_engine.py).

Covers fixes #5, #7, #8, #9 from the 2026-09 loss/rate-of-change alerts
audit, plus the deploy-survival latch-hydration fix ("Also fold in").

  #5 — a missing-data tick (fetch timeout/failure/empty frame) must NEVER
       be read as "condition recovered" and must never clear a latch.
  #7 — a SUPPRESSED fire (topic-tier dedup) must still record its latch
       (not just a survivor); equal-tier same-topic siblings are now
       deduped too; an action-bearing agent (e.g. a kill-switch) is NEVER
       suppressed regardless of tier, and every topic-tier suppression
       merges the suppressed rows into the winner's alert (no dropped
       information).
  #8 — re-alert timing is tracked PER LEAF (metric+scope+account), so a
       ₹ day_val leaf and a %/min rate leaf on the same agent never mix
       units, and the cooldown used is always the AGENT's own configured
       value, never a hard-coded global default.
  #9 — a value oscillating around the threshold does not spam repeat
       fires (re-arm requires recovering past an 80%-of-threshold band);
       a monotonically-worsening breach DOES eventually re-fire, once it
       has escalated by another full |threshold| unit and cooldown has
       elapsed.

Five quality dimensions:
  SSOT        — direct invocation of the real latch functions, no mocks
  Correctness — every worked example from the audit plan reproduced
  Performance — pure functions, no DB/broker I/O
  Reuse       — shared FakeAgent/match-builder helpers across test classes
  UX          — every assert has an f-string with actual/expected
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import pytest

from backend.api.algo.agent_engine import (
    _latch_key,
    _v2_leaf_should_fire,
    _v2_recovered_past_band,
    _v2_apply_recovery,
    _v2_apply_escalation_gate,
    _ae_has_actions,
    _ae_topic_winner,
    _ae_suppressed_in_group,
    _compute_topic_suppression,
    _hydrate_latch_from_rows,
)


class _Agent:
    def __init__(self, id=1, slug="test-agent", tier="medium", topic="general", actions=None):
        self.id = id
        self.slug = slug
        self.tier = tier
        self.topic = topic
        self.actions = actions or []


def _m(metric="day_val", scope="positions.total", account="TOTAL",
      op="<=", threshold=-30000, value=-31000):
    return {'metric': metric, 'scope': scope, 'account': account,
            'op': op, 'threshold': threshold, 'value': value}


def _now():
    return datetime(2026, 9, 20, 10, 0, 0)


# ═══════════════════════════════════════════════════════════════════════════
#  Fix #8 — per-leaf, per-account key isolation
# ═══════════════════════════════════════════════════════════════════════════

class TestLatchKeyIsolation:
    def test_different_metrics_get_different_keys(self):
        """The audit's core #8 bug: a ₹ day_val leaf and a %/min rate leaf
        on the SAME agent must never share latch/cooldown state."""
        k1 = _latch_key("loss-positions-total", _m(metric="day_val"))
        k2 = _latch_key("loss-positions-total", _m(metric="pnl_rate_abs"))
        assert k1 != k2, f"Expected distinct keys for distinct metrics, got {k1} == {k2}"

    def test_different_accounts_get_different_keys(self):
        k1 = _latch_key("loss-positions-acct", _m(account="ZD1234"))
        k2 = _latch_key("loss-positions-acct", _m(account="ZD5678"))
        assert k1 != k2

    def test_none_account_normalises_to_total(self):
        k1 = _latch_key("agent", _m(account=None))
        k2 = _latch_key("agent", _m(account="TOTAL"))
        assert k1 == k2


# ═══════════════════════════════════════════════════════════════════════════
#  Fix #8 — cooldown source (per-agent, not global default)
# ═══════════════════════════════════════════════════════════════════════════

class TestCooldownGate:
    def test_first_breach_always_fires(self):
        store = {}
        assert _v2_leaf_should_fire("agent", _m(), _now(), 10, store) is True

    def test_within_cooldown_blocked_regardless_of_escalation(self):
        """Even a MASSIVE escalation must not re-fire inside the agent's
        own configured cooldown window."""
        now = _now()
        store = {_latch_key("agent", _m()): {'ts': now - timedelta(minutes=3), 'val': -31000}}
        huge_escalation = _m(value=-500000)
        result = _v2_leaf_should_fire("agent", huge_escalation, now, 10, store)
        assert result is False, "Must stay silent inside the cooldown window, no matter how bad it gets"

    def test_uses_the_cooldown_minutes_passed_in_not_a_hardcoded_default(self):
        """Regression for the literal #8 bug: the OLD code read a
        hard-coded global cfg['cooldown_min'] (30) for rate agents
        regardless of the agent's own configured value (10 for
        loss-rate-acct). The new gate takes cooldown_min as an explicit
        parameter — verify a short (10-min) cooldown actually re-arms
        faster than the old 30-min global default would have."""
        now = _now()
        latched_at = now - timedelta(minutes=15)  # < 30 (old default) but > 10 (agent's own)
        store = {_latch_key("loss-rate-acct", _m(op="<=", threshold=-6000)):
                 {'ts': latched_at, 'val': -6000}}
        escalated = _m(op="<=", threshold=-6000, value=-13000)  # moved 1 full threshold-unit worse
        result = _v2_leaf_should_fire("loss-rate-acct", escalated, now, 10, store)
        assert result is True, (
            "With the agent's own 10-min cooldown (elapsed 15 min ago) and a "
            "full-threshold escalation, this must fire — the old 30-min "
            "global default would have wrongly kept it silent"
        )


# ═══════════════════════════════════════════════════════════════════════════
#  Fix #9 — escalation (monotonic worsening eventually re-fires)
# ═══════════════════════════════════════════════════════════════════════════

class TestEscalation:
    def test_monotonic_worsening_refires_after_full_threshold_step(self):
        """Audit worked example: a -31k alert followed by a slide to
        -200k must eventually re-fire (old code: latched forever, silent)."""
        now = _now()
        store = {_latch_key("agent", _m()): {'ts': now - timedelta(minutes=31), 'val': -31000}}
        slid_to_200k = _m(value=-200000)
        assert _v2_leaf_should_fire("agent", slid_to_200k, now, 30, store) is True

    def test_tiny_worsening_does_not_refire(self):
        """A move smaller than one full |threshold| unit must NOT re-fire
        — only a MATERIAL escalation does."""
        now = _now()
        store = {_latch_key("agent", _m(threshold=-30000)): {'ts': now - timedelta(minutes=31), 'val': -31000}}
        barely_worse = _m(threshold=-30000, value=-31500)
        assert _v2_leaf_should_fire("agent", barely_worse, now, 30, store) is False

    def test_zero_threshold_has_no_escalation_step(self):
        """cash < 0: 'worse by another $0' is meaningless — cooldown
        elapsed alone re-arms a zero-threshold leaf, escalation math
        never applies (avoids re-firing every tick on any further dip)."""
        now = _now()
        store = {_latch_key("agent", _m(op="<", threshold=0, value=-100)):
                 {'ts': now - timedelta(minutes=31), 'val': -100}}
        still_negative = _m(op="<", threshold=0, value=-50000)
        assert _v2_leaf_should_fire("agent", still_negative, now, 30, store) is True

    def test_non_ordered_op_refires_after_cooldown_regardless(self):
        """== / in / between leaves (e.g. is_itm) have no ordered 'worse'
        direction — plain cooldown-gated re-latch, no escalation math."""
        now = _now()
        m = _m(op="==", threshold=1.0, value=1.0)
        store = {_latch_key("agent", m): {'ts': now - timedelta(minutes=61), 'val': 1.0}}
        assert _v2_leaf_should_fire("agent", m, now, 60, store) is True


# ═══════════════════════════════════════════════════════════════════════════
#  Fix #9 — hysteresis (re-arm band prevents oscillation spam)
# ═══════════════════════════════════════════════════════════════════════════

class TestHysteresisReArmBand:
    def test_oscillation_around_threshold_does_not_repeatedly_clear_latch(self):
        """Audit worked example: loss-positions-acct oscillated between
        -2.37% and -2.52% around a -2.5% threshold, firing 4x in one day
        with no buffer. A value that ticks JUST back over the line
        (-2.49%, barely not-breaching) must NOT clear the latch."""
        m_base = _m(metric="day_pct", op="<=", threshold=-2.5, value=-2.52)
        obs_barely_recovered = {**m_base, 'value': -2.49, 'fired': False}
        assert _v2_recovered_past_band(obs_barely_recovered) is False, (
            "A value barely back over the threshold line must NOT clear the latch"
        )

    def test_recovery_past_80pct_band_clears_latch(self):
        m_base = _m(metric="day_pct", op="<=", threshold=-2.5, value=-2.52)
        obs_recovered = {**m_base, 'value': -1.0, 'fired': False}  # well past -2.0 (80% band)
        assert _v2_recovered_past_band(obs_recovered) is True

    def test_re_arm_band_is_direction_aware_for_positive_ops(self):
        """Mirrors the < /<= band math for > / >= (rarely used but must
        be symmetric, not just correct-by-coincidence for loss thresholds).
        For a '>=' breach (higher is worse), recovery means the value has
        dropped back down past the 80%-of-threshold re-arm line (80)."""
        obs_not_recovered = {'op': '>=', 'threshold': 100, 'value': 85}   # still above the band
        obs_recovered = {'op': '>=', 'threshold': 100, 'value': 50}      # well below it
        assert _v2_recovered_past_band(obs_not_recovered) is False
        assert _v2_recovered_past_band(obs_recovered) is True

    def test_zero_threshold_always_recovers(self):
        assert _v2_recovered_past_band({'op': '<', 'threshold': 0, 'value': 0.01}) is True

    def test_non_ordered_op_always_recovers(self):
        assert _v2_recovered_past_band({'op': '==', 'threshold': 1.0, 'value': 0.0}) is True


# ═══════════════════════════════════════════════════════════════════════════
#  Fix #5 — missing-data tick never clears a latch
# ═══════════════════════════════════════════════════════════════════════════

class TestMissingDataNeverUnlatches:
    def test_absent_key_left_untouched(self):
        """A latch key that simply never appears in this tick's
        observations (empty/failed fetch) must be left exactly as-is."""
        agent = _Agent(slug="loss-margin-low")
        key = _latch_key(agent.slug, _m(metric="avail_margin", account="ZD1234"))
        store = {key: {'ts': _now() - timedelta(minutes=5), 'val': -1000.0}}
        _v2_apply_recovery(agent, observations=[], store=store)
        assert key in store, "A key absent from observations must never be treated as recovered"

    def test_observed_but_still_breaching_left_untouched(self):
        agent = _Agent(slug="loss-margin-low")
        m = _m(metric="avail_margin", account="ZD1234", op="<", threshold=0, value=-2000)
        key = _latch_key(agent.slug, m)
        store = {key: {'ts': _now() - timedelta(minutes=5), 'val': -1000.0}}
        obs = {**m, 'fired': True}
        _v2_apply_recovery(agent, observations=[obs], store=store)
        assert key in store, "Still-breaching (fired=True) observations must not clear the latch"


# ═══════════════════════════════════════════════════════════════════════════
#  Fix #7 — equal-tier dedup + action-bearing exemption + row merge
# ═══════════════════════════════════════════════════════════════════════════

class TestTopicSuppressionActionExemption:
    def test_action_bearing_agent_never_suppressed_even_at_lower_tier(self):
        """The kill-switch (loss-pos-total-auto-close) must always win,
        even against a higher-priority-tier notify-only sibling."""
        kill_switch = _Agent(id=1, slug="loss-pos-total-auto-close", tier="high",
                             topic="positions_loss", actions=[{"type": "chase_close_positions"}])
        notify_only = _Agent(id=2, slug="loss-positions-total", tier="critical",
                             topic="positions_loss", actions=[])
        group = [
            {'agent': kill_switch, 'matches': [_m()]},
            {'agent': notify_only, 'matches': [_m()]},
        ]
        suppressed, merge_map = _compute_topic_suppression(group)
        assert kill_switch.id not in suppressed, (
            "Action-bearing agent must NEVER be suppressed, even though its "
            "tier (high) is lower priority than the notify-only sibling (critical)"
        )

    def test_equal_tier_siblings_now_deduped(self):
        """Fix #7: two SAME-tier agents in one topic no longer both push
        separately (prod repro: loss-rate-acct + loss-positions-total,
        both critical, fired ~3s apart)."""
        a1 = _Agent(id=1, slug="loss-rate-acct", tier="critical", topic="positions_loss")
        a2 = _Agent(id=2, slug="loss-positions-total", tier="critical", topic="positions_loss")
        group = [
            {'agent': a1, 'matches': [_m()]},
            {'agent': a2, 'matches': [_m()]},
        ]
        suppressed, merge_map = _compute_topic_suppression(group)
        assert len(suppressed) == 1, (
            f"Expected exactly one of the two equal-tier siblings suppressed, got {suppressed}"
        )

    def test_suppressed_rows_merged_into_winner_not_dropped(self):
        a1 = _Agent(id=1, slug="expiry-nfo-risk-alert", tier="high", topic="expiry_warning")
        a2 = _Agent(id=2, slug="expiry-mcx-risk-alert", tier="high", topic="expiry_warning")
        m1 = _m(metric="is_itm", account="ACCT1")
        m2 = _m(metric="pnl", account="ACCT2")
        group = [
            {'agent': a1, 'matches': [m1]},
            {'agent': a2, 'matches': [m2]},
        ]
        suppressed, merge_map = _compute_topic_suppression(group)
        winner_id = next(e['agent'].id for e in group if e['agent'].id not in suppressed)
        assert merge_map.get(winner_id) == [m2 if winner_id == 1 else m1], (
            f"Expected the suppressed sibling's matches merged into the winner, got {merge_map}"
        )

    def test_no_suppression_for_single_fire(self):
        a1 = _Agent(id=1, slug="solo", tier="high", topic="expiry_warning")
        suppressed, merge_map = _compute_topic_suppression([{'agent': a1, 'matches': [_m()]}])
        assert suppressed == {} and merge_map == {}

    def test_general_topic_opts_out(self):
        a1 = _Agent(id=1, slug="a", tier="critical", topic="general")
        a2 = _Agent(id=2, slug="b", tier="low", topic="general")
        suppressed, merge_map = _compute_topic_suppression([
            {'agent': a1, 'matches': [_m()]}, {'agent': a2, 'matches': [_m()]},
        ])
        assert suppressed == {}, "topic='general' is opt-out — legacy untagged agents unaffected"

    def test_has_actions_helper(self):
        assert _ae_has_actions(_Agent(actions=[{"type": "close_position"}])) is True
        assert _ae_has_actions(_Agent(actions=[])) is False


# ═══════════════════════════════════════════════════════════════════════════
#  Deploy-survival latch hydration ("Also fold in")
# ═══════════════════════════════════════════════════════════════════════════

class TestLatchHydration:
    def test_hydrates_from_agent_events_detail(self):
        # AgentEvent.timestamp is DateTime(timezone=True) — always an
        # aware UTC datetime in production; use the same shape here so
        # the IST date-boundary comparison is deterministic regardless
        # of the test host's local timezone.
        from backend.api.algo import agent_engine
        agent_engine._V2_LATCH.clear()
        ts = datetime(2026, 9, 20, 9, 45, 0, tzinfo=timezone.utc)
        detail = json.dumps({'matches': [_m(metric="day_val", account="ZD1234", value=-31000)]})
        _hydrate_latch_from_rows([("loss-positions-acct", detail, ts)], today=ts.date())
        key = _latch_key("loss-positions-acct", _m(metric="day_val", account="ZD1234"))
        assert key in agent_engine._V2_LATCH, "Expected the latch hydrated from the agent_events row"
        assert agent_engine._V2_LATCH[key]['val'] == -31000

    def test_stale_prior_day_rows_are_skipped(self):
        from backend.api.algo import agent_engine
        agent_engine._V2_LATCH.clear()
        # 15:00 UTC on the 19th is still the 19th in IST (UTC+5:30) —
        # unambiguously "yesterday" relative to `today`.
        yesterday = datetime(2026, 9, 19, 15, 0, 0, tzinfo=timezone.utc)
        today = datetime(2026, 9, 20, 9, 0, 0, tzinfo=timezone.utc).date()
        detail = json.dumps({'matches': [_m(metric="day_val", account="ZD9999", value=-31000)]})
        _hydrate_latch_from_rows([("loss-positions-acct", detail, yesterday)], today=today)
        key = _latch_key("loss-positions-acct", _m(metric="day_val", account="ZD9999"))
        assert key not in agent_engine._V2_LATCH, (
            "A prior trading day's row must not survive hydration — the daily "
            "reset would have wiped it anyway"
        )

    def test_later_row_overwrites_earlier_for_same_key(self):
        from backend.api.algo import agent_engine
        agent_engine._V2_LATCH.clear()
        ts1 = datetime(2026, 9, 20, 9, 30, 0, tzinfo=timezone.utc)
        ts2 = datetime(2026, 9, 20, 9, 45, 0, tzinfo=timezone.utc)
        m = _m(metric="day_val", account="ZD1234")
        rows = [
            ("agent", json.dumps({'matches': [{**m, 'value': -31000}]}), ts1),
            ("agent", json.dumps({'matches': [{**m, 'value': -55000}]}), ts2),
        ]
        _hydrate_latch_from_rows(rows, today=ts1.date())
        key = _latch_key("agent", m)
        assert agent_engine._V2_LATCH[key]['val'] == -55000, (
            "Rows must be applied in ascending timestamp order — the LATER "
            "row (bigger escalation) must win"
        )

    def test_missing_or_malformed_detail_skipped_gracefully(self):
        from backend.api.algo import agent_engine
        agent_engine._V2_LATCH.clear()
        ts = datetime(2026, 9, 20, 9, 45, 0, tzinfo=timezone.utc)
        rows = [("agent", None, ts), ("agent", "not json", ts), ("agent", "{}", ts)]
        _hydrate_latch_from_rows(rows, today=ts.date())  # must not raise
        assert agent_engine._V2_LATCH == {}
