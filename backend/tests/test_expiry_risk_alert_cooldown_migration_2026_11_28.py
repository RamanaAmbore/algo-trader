"""Tests for the 2026-11-28 expiry-risk-alert cooldown MIGRATION fix.

Background investigation (operator-reported): `expiry-mcx-risk-alert` fired
14 separate Telegram/email/ntfy sends on prod on 2026-11-27. A direct read
of prod `agent_events` for that agent on that date showed:

  - 14 distinct `triggered` rows, timestamps ~60-70 minutes apart, spanning
    the FULL trading day (03:34 UTC / 09:04 IST through 16:54 UTC / 22:24
    IST) — NOT confined to a 65-minute pre-close window.
  - The live `agents.cooldown_minutes` value for both `expiry-mcx-risk-
    alert` and `expiry-nfo-risk-alert` was 60 on prod, even though
    `_EXPIRY_RISK_AGENT_DEFAULTS["cooldown_minutes"]` in code is 240 (it
    was raised 60 -> 240 on 2026-11-26, see
    `test_expiry_risk_alert_cooldown_and_enrichment.py`'s own
    `TestCooldownRaisedTo240`).
  - Each individual fire's own `agent_events.detail` JSON (`matches`/
    `latched_matches`) already carried EVERY currently-unhedged leg,
    grouped by account via `_v2_expiry_risk_alert_enrichment()` — i.e. the
    per-tick dispatch path was ALREADY batching correctly. There was no
    per-leaf fan-out bug at the dispatch/send layer.

Root cause: the 60->240 cooldown bump was shipped in code on 2026-11-26,
but no migration function existed to force-sync it onto the two
already-seeded DB rows (both of which were created/populated with the OLD
60-min default before or at the moment of that bump). `cooldown_minutes`
is never force-synced on an ordinary restart (`_ae_sync_existing_builtin`'s
own docstring), so the fix was structurally inert against the live rows.

This file tests the new migration function,
`_ae_sync_expiry_risk_alert_cooldown()`, which closes that gap — mirroring
the exact safety contract `_ae_sync_divergence_cooldown` already
established for its own sibling migration (value-gated to the precise
stale value, slug-scoped, never touching an operator-customized row).
"""
from __future__ import annotations

import inspect

import pytest

from backend.api.algo.agent_engine import (
    BUILTIN_AGENTS,
    _ae_sync_existing_builtin,
    _ae_sync_expiry_risk_alert_cooldown,
)


def _agent(slug: str) -> dict:
    return next(a for a in BUILTIN_AGENTS if a["slug"] == slug)


class _Row:
    """Minimal stand-in for an `Agent` ORM row — only the attribute this
    migration reads/writes is needed."""
    pass


class TestExpiryRiskAlertCooldownMigration:
    """Direct unit tests of `_ae_sync_expiry_risk_alert_cooldown`."""

    @pytest.mark.parametrize("slug", ["expiry-mcx-risk-alert", "expiry-nfo-risk-alert"])
    def test_sync_force_upgrades_a_stale_60min_row(self, slug):
        existing = _Row()
        existing.cooldown_minutes = 60
        agent_def = _agent(slug)

        _ae_sync_expiry_risk_alert_cooldown(existing, agent_def)

        assert existing.cooldown_minutes == 240

    @pytest.mark.parametrize("slug", ["expiry-mcx-risk-alert", "expiry-nfo-risk-alert"])
    def test_sync_is_a_no_op_once_already_at_240(self, slug):
        """A row already migrated (or freshly seeded post-bump) must not
        be touched a second time — this is a value-gate, not an
        unconditional overwrite."""
        existing = _Row()
        existing.cooldown_minutes = 240
        agent_def = _agent(slug)

        _ae_sync_expiry_risk_alert_cooldown(existing, agent_def)

        assert existing.cooldown_minutes == 240

    @pytest.mark.parametrize("slug", ["expiry-mcx-risk-alert", "expiry-nfo-risk-alert"])
    def test_sync_leaves_an_operator_customized_cooldown_alone(self, slug):
        """An operator who deliberately set a DIFFERENT cooldown (not the
        stale stock 60) is never force-upgraded — the migration can only
        ever fire against the exact pre-bump stock value."""
        existing = _Row()
        existing.cooldown_minutes = 90  # operator's own custom value
        agent_def = _agent(slug)

        _ae_sync_expiry_risk_alert_cooldown(existing, agent_def)

        assert existing.cooldown_minutes == 90

    def test_sync_is_slug_gated_and_no_op_for_other_agents(self):
        """A 60-min cooldown on an unrelated agent must never be migrated
        by this expiry-risk-alert-only function."""
        existing = _Row()
        existing.cooldown_minutes = 60
        agent_def = _agent("loss-funds-negative")  # unrelated, also happens to be nonzero

        _ae_sync_expiry_risk_alert_cooldown(existing, agent_def)

        assert existing.cooldown_minutes == 60, (
            "a 60-min cooldown on an unrelated agent must never be "
            "force-upgraded by this expiry-risk-alert-only migration"
        )

    def test_wiring_present_in_sync_existing_builtin(self):
        """`_ae_sync_existing_builtin` must actually call this migration
        — a correct migration function that's never invoked on restart
        is exactly as inert as having none at all."""
        src = inspect.getsource(_ae_sync_existing_builtin)
        assert "_ae_sync_expiry_risk_alert_cooldown(existing, agent_def)" in src

    def test_code_default_is_still_240_not_regressed_back_to_60(self):
        """Guard against a future edit silently reverting the
        `_EXPIRY_RISK_AGENT_DEFAULTS` bump this migration exists to
        propagate — if the code default ever drops back to 60, this
        migration's own value-gate (`== 60`) would become a permanent
        no-op forever (agent_def["cooldown_minutes"] would also read 60,
        so the assignment would be harmless but the whole fix would be
        pointless)."""
        nfo = _agent("expiry-nfo-risk-alert")
        mcx = _agent("expiry-mcx-risk-alert")
        assert nfo["cooldown_minutes"] == 240
        assert mcx["cooldown_minutes"] == 240


class TestFixOneDoesNotTouchDispatchBatching:
    """Confirms (by source inspection) that this fix is scoped to the
    cooldown VALUE only — it must never introduce a new cross-tick
    batching/coalescing mechanism, per the explicit constraint that only
    genuinely-simultaneous-in-one-cycle fires may ever consolidate into
    one message, and legs becoming eligible on different cycles must
    always send as separate messages."""

    def test_v2_apply_escalation_gate_unchanged_signature(self):
        from backend.api.algo.agent_engine import _v2_apply_escalation_gate
        sig = inspect.signature(_v2_apply_escalation_gate)
        assert list(sig.parameters) == ["agent", "matches", "now", "cooldown_min", "store"]

    def test_v2_leaf_should_fire_unchanged_signature(self):
        from backend.api.algo.agent_engine import _v2_leaf_should_fire
        sig = inspect.signature(_v2_leaf_should_fire)
        assert list(sig.parameters) == ["agent_slug", "m", "now", "cooldown_min", "store"]
