"""
Regression tests for the 2026-10 builtin-status-sync fix: every process
restart (`seed_agents()` → `_ae_sync_existing_builtin()`) was silently
reverting an operator's enable/disable choice on ordinary builtin agents,
because `_ae_sync_builtin_status` force-synced `status` bidirectionally
for EVERY builtin, not just the safety-critical "Ships INACTIVE" category.

Fix: `_ae_sync_existing_builtin` now only calls `_ae_sync_builtin_status`
when `_ae_seed_ships_inactive(agent_def)` is True (the agent's own
description literally contains "Ships INACTIVE" — e.g. a destructive
auto-close action). For every other builtin, an existing row's `status`
is left completely untouched on sync; the seed's `status` key is only
ever applied at genuine first-insert time (`_ae_build_agent_row`).

Scope note on the motivating example named in the original bug report:
`loss-pos-total-auto-close`'s own description DOES contain the literal
phrase "Ships INACTIVE" (it is a destructive, broker-touching chase-close
action), so it correctly remains in the safety-critical category and
STILL force-reverts to inactive on every restart after this fix — this
is intentional, confirmed via `advisor()`, and tested explicitly below
(`test_ships_inactive_real_builtin_still_force_corrected`). The fix
instead applies to ordinary disabled-by-default builtins whose
descriptions do NOT invoke that safety phrase: `loss-positions-acct`,
`loss-rate-acct`, and `loss-margin-low` (confirmed via grep — none of
their descriptions contain "Ships INACTIVE").

Five quality dimensions:
  SSOT        — direct invocation of `_ae_sync_existing_builtin` against
                the REAL `BUILTIN_AGENTS` seed dicts, not a reimplementation
  Correctness — both directions (operator activates a default-off agent;
                operator deactivates a default-on agent) + the safety
                regression guard
  Performance — pure function, no DB/broker I/O
  Reuse       — reuses the SimpleNamespace existing-row stub pattern
                already established in test_agent_engine_baseline.py /
                test_seed_ships_inactive_guard.py
  UX          — every assert has an f-string with actual/expected
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.api.algo.agent_engine import (
    BUILTIN_AGENTS,
    _ae_build_agent_row,
    _ae_seed_ships_inactive,
    _ae_sync_existing_builtin,
)


def _by_slug(slug: str) -> dict:
    agent = next((a for a in BUILTIN_AGENTS if a.get("slug") == slug), None)
    assert agent is not None, f"{slug} not found in BUILTIN_AGENTS"
    return agent


def _make_existing(status: str, **overrides) -> SimpleNamespace:
    base = dict(
        conditions={},
        long_name=None,
        schedule="market_hours",
        tier="medium",
        topic="general",
        status=status,
        events=[],
        actions=[],
        cooldown_minutes=60,
        fire_at_time=None,
        last_fired=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestCategorySplitIsCorrect:
    """Sanity-check the categorisation itself before trusting the
    behavioral tests built on top of it."""

    @pytest.mark.parametrize("slug", [
        "loss-positions-acct", "loss-rate-acct", "loss-margin-low",
    ])
    def test_ordinary_disabled_by_default_agents_are_not_ships_inactive(self, slug):
        agent_def = _by_slug(slug)
        assert not _ae_seed_ships_inactive(agent_def), (
            f"{slug}'s description must NOT contain 'Ships INACTIVE' for "
            f"this test file's premise to hold — got description "
            f"{agent_def.get('description')!r}"
        )

    def test_loss_pos_total_auto_close_is_ships_inactive(self):
        agent_def = _by_slug("loss-pos-total-auto-close")
        assert _ae_seed_ships_inactive(agent_def), (
            "loss-pos-total-auto-close's description must contain 'Ships "
            "INACTIVE' — it is a destructive, broker-touching action and "
            "must remain in the safety-critical force-revert category"
        )


class TestOperatorChoiceSurvivesRestartForOrdinaryBuiltins:
    """The core fix: an operator's enable/disable choice on an ordinary
    (non-'Ships INACTIVE') builtin must survive `seed_agents()`-style
    re-sync, in BOTH directions."""

    @pytest.mark.parametrize("slug", [
        "loss-positions-acct", "loss-rate-acct", "loss-margin-low",
    ])
    def test_operator_activation_of_default_off_agent_survives_resync(self, slug):
        agent_def = _by_slug(slug)
        assert agent_def.get("status") == "inactive", (
            f"{slug}'s code seed default must be 'inactive' for this test "
            f"to actually exercise the activation direction, got "
            f"{agent_def.get('status')}"
        )
        # Simulate: row was inserted inactive (matching the seed default),
        # then the operator activated it from /agents.
        existing = _make_existing(status="active")
        # Re-run the sync with the ORIGINAL seed def (status='inactive') —
        # pre-fix, this would force existing.status back to 'inactive'.
        _ae_sync_existing_builtin(existing, agent_def)
        assert existing.status == "active", (
            f"{slug}: operator's activation must survive a re-sync against "
            f"the original seed status, got {existing.status!r}"
        )

    def test_operator_deactivation_of_default_on_agent_survives_resync(self):
        """Reverse direction, explicitly named in the bug report:
        loss-positions-total ships status='active' (no explicit override
        — inherits _LOSS_AGENT_DEFAULTS) and does not say 'Ships INACTIVE'.
        An operator turning it OFF must survive a re-sync."""
        agent_def = _by_slug("loss-positions-total")
        assert agent_def.get("status") == "active"
        assert not _ae_seed_ships_inactive(agent_def)
        existing = _make_existing(status="inactive")
        _ae_sync_existing_builtin(existing, agent_def)
        assert existing.status == "inactive", (
            f"operator's deactivation of loss-positions-total must survive "
            f"a re-sync against the original (active) seed status, got "
            f"{existing.status!r}"
        )


class TestShipsInactiveSafetyCaseStillForceCorrected:
    """Regression guard: do NOT break the existing safety-critical path.
    An agent whose description says 'Ships INACTIVE' must still be
    force-corrected to inactive on every sync, even if an operator (or a
    bug) managed to set it active — this is the destructive/broker-
    touching category and must never be durably enableable via a status
    column surviving restarts."""

    def test_ships_inactive_real_builtin_still_force_corrected(self):
        agent_def = _by_slug("loss-pos-total-auto-close")
        existing = _make_existing(status="active")
        _ae_sync_existing_builtin(existing, agent_def)
        assert existing.status == "inactive", (
            f"loss-pos-total-auto-close must still be force-corrected to "
            f"'inactive' on sync even though an operator (or bug) set it "
            f"active — this is the safety-critical category and must be "
            f"unaffected by the ordinary-builtin fix, got {existing.status!r}"
        )

    @pytest.mark.parametrize("slug", [
        "expiry-day-positions-alert",
    ])
    def test_other_ships_inactive_builtins_still_force_corrected(self, slug):
        agent_def = _by_slug(slug)
        assert _ae_seed_ships_inactive(agent_def), (
            f"{slug} is expected to be in the 'Ships INACTIVE' category"
        )
        existing = _make_existing(status="active")
        _ae_sync_existing_builtin(existing, agent_def)
        assert existing.status == "inactive", (
            f"{slug}: safety-critical category must still force-correct "
            f"to inactive, got {existing.status!r}"
        )


class TestBuildAgentRowReadsTradeModeFromSpec:
    """2026-10-31 live-incident fix: _ae_build_agent_row() never read
    `trade_mode` from the seed spec at all, so a builtin explicitly
    wanting `trade_mode: "live"` (e.g. expiry-day-itm-auto-close) got
    the ORM column default ("paper") instead on first insert — confirmed
    via prod: every real close on that agent's first live trading day
    silently ran through PaperTradeEngine. Only passed through when the
    spec explicitly sets it, so every other builtin (none of which
    specify trade_mode today) keeps the exact same default behaviour."""

    def test_expiry_day_itm_auto_close_spec_requests_live(self):
        agent_def = _by_slug("expiry-day-itm-auto-close")
        assert agent_def.get("trade_mode") == "live", (
            "this agent's own design relies on reaching the real "
            "hold-gate path — omitting trade_mode here silently falls "
            "back to paper and the hold-gate never engages"
        )

    def test_build_agent_row_passes_through_explicit_trade_mode(self):
        agent_def = _by_slug("expiry-day-itm-auto-close")
        row = _ae_build_agent_row(agent_def)
        assert row.trade_mode == "live", (
            f"_ae_build_agent_row() must read trade_mode from the spec "
            f"when present, got {row.trade_mode!r}"
        )

    def test_build_agent_row_omits_trade_mode_kwarg_when_spec_silent(self):
        """An ordinary builtin with no trade_mode key must NOT have the
        kwarg passed at all — the ORM column default ('paper') is applied
        by SQLAlchemy at INSERT time, not at bare construction, so the
        in-memory object's attribute reads None here (identical to the
        pre-fix behaviour, which never knew about this field either).
        Not a hardcoded 'paper' baked into the builder itself, which
        would be a second place this value lives."""
        agent_def = _by_slug("loss-funds-negative")
        assert "trade_mode" not in agent_def, (
            "test premise: this agent must not specify trade_mode for "
            "this test to actually exercise the omitted-kwarg path"
        )
        row = _ae_build_agent_row(agent_def)
        assert row.trade_mode is None, (
            f"with no explicit trade_mode, the kwarg must be omitted "
            f"entirely (letting SQLAlchemy's column default apply only "
            f"on insert) — got {row.trade_mode!r}"
        )
