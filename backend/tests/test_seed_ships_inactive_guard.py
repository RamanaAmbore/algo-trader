"""Tests for the seed-status/description consistency guard.

Covers:
  - Regression: the two real expiry auto-close seed dicts ship with
    status='inactive' in the raw BUILTIN_AGENTS list (checked on the raw
    dict, not through the guard — so a future regression where someone
    flips status back to 'active' is caught even if the guard itself were
    ever weakened).
  - Sweep: no entry in BUILTIN_AGENTS contains both the "Ships INACTIVE"
    phrase in its description and status='active' — catches this whole
    bug class at test time for ANY current or future seeded agent.
  - `_ae_guard_seed_status`: forces status='inactive' + logs ERROR when
    description says "Ships INACTIVE" but status is 'active' (including
    when status is entirely absent and would default to 'active'); passes
    a normal agent's stated status through unchanged with no log.
  - `_ae_build_agent_row` (insert path): a synthetic mismatched seed dict
    is inserted with status='inactive', never 'active'.
  - `_ae_sync_existing_builtin` (sync path): an existing inactive row is
    NOT flipped back to active by a mismatched code seed dict.

Five quality dimensions:
  SSOT        — direct invocation of the guard + both call sites, not a
                re-implementation of the check
  Correctness — mismatch forced inactive + logged; non-mismatch untouched
  Performance — pure functions / in-memory ORM construct; no DB, no I/O
  Reuse       — reuses the SimpleNamespace existing-row shape already
                established in test_agent_engine_baseline.py
  UX          — every assert has an f-string with actual/expected
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.api.algo.agent_engine import (
    BUILTIN_AGENTS,
    _SEED_SHIPS_INACTIVE_PHRASE,
    _ae_build_agent_row,
    _ae_guard_seed_status,
    _ae_sync_existing_builtin,
)

EQUITY_SLUG = "expiry-day-equity-itm-auto-close"
COMMODITY_SLUG = "expiry-day-commodity-itm-auto-close"


def _by_slug(slug: str) -> dict:
    agent = next((a for a in BUILTIN_AGENTS if a.get("slug") == slug), None)
    assert agent is not None, f"{slug} not found in BUILTIN_AGENTS"
    return agent


class TestRealExpiryAgentsShipInactive:
    """Direct regression on the raw seed dicts — bypasses the guard
    entirely so a future regression is caught even if the guard itself
    were ever weakened or removed."""

    @pytest.mark.parametrize("slug", [EQUITY_SLUG, COMMODITY_SLUG])
    def test_status_is_inactive_on_raw_dict(self, slug):
        agent_def = _by_slug(slug)
        assert agent_def["status"] == "inactive", (
            f"{slug}: status must be 'inactive' to match its own "
            f"description's 'Ships INACTIVE' claim, got "
            f"{agent_def['status']!r}"
        )

    @pytest.mark.parametrize("slug", [EQUITY_SLUG, COMMODITY_SLUG])
    def test_description_still_claims_ships_inactive(self, slug):
        """Sanity-check the fixture assumption itself: if a future edit
        removes the phrase from the description, this test (not the
        status test above) should be the one that fails, surfacing the
        intent clearly."""
        agent_def = _by_slug(slug)
        assert _SEED_SHIPS_INACTIVE_PHRASE in agent_def.get("description", ""), (
            f"{slug}: expected description to contain "
            f"{_SEED_SHIPS_INACTIVE_PHRASE!r}"
        )


class TestNoBuiltinAgentContradictsItsOwnDescription:
    """Sweep: catches this whole bug class for ANY seeded agent, current
    or future — not just the two slugs fixed here."""

    def test_no_active_agent_claims_ships_inactive(self):
        offenders = [
            a.get("slug")
            for a in BUILTIN_AGENTS
            if a.get("status") == "active"
            and _SEED_SHIPS_INACTIVE_PHRASE in (a.get("description") or "")
        ]
        assert offenders == [], (
            f"BUILTIN_AGENTS seed dicts contradict their own description "
            f"('{_SEED_SHIPS_INACTIVE_PHRASE}' + status='active'): {offenders}"
        )


class TestAeGuardSeedStatus:
    """Unit tests for the guard function itself."""

    def test_mismatch_forces_inactive_and_logs_error(self, monkeypatch):
        import backend.api.algo.agent_engine as agent_engine_mod
        mock_calls: list = []
        mock_logger = SimpleNamespace(error=lambda *a, **k: mock_calls.append((a, k)))
        monkeypatch.setattr(agent_engine_mod, "logger", mock_logger)

        seed = {
            "slug": "synthetic-mismatched-agent",
            "status": "active",
            "description": "Does destructive things. Ships INACTIVE (destructive).",
        }
        result = _ae_guard_seed_status(seed)
        assert result == "inactive", f"expected forced 'inactive', got {result!r}"
        assert len(mock_calls) == 1, f"expected exactly one ERROR log call, got {len(mock_calls)}"
        logged_args = mock_calls[0][0]
        assert "synthetic-mismatched-agent" in logged_args, (
            f"expected slug named in the ERROR log args, got {logged_args}"
        )

    def test_mismatch_with_missing_status_key_defaults_to_active_then_forced(self, monkeypatch):
        """A seed dict with NO status key at all would otherwise default
        to 'active' (the insert-path default) — the guard must still
        catch it."""
        import backend.api.algo.agent_engine as agent_engine_mod
        mock_calls: list = []
        mock_logger = SimpleNamespace(error=lambda *a, **k: mock_calls.append((a, k)))
        monkeypatch.setattr(agent_engine_mod, "logger", mock_logger)

        seed = {
            "slug": "synthetic-no-status-key",
            "description": "Ships INACTIVE until reviewed.",
            # no "status" key at all
        }
        result = _ae_guard_seed_status(seed, default="active")
        assert result == "inactive", f"expected forced 'inactive', got {result!r}"
        assert len(mock_calls) == 1, "expected one ERROR log for the missing-status mismatch case"

    def test_normal_agent_passes_through_unchanged_no_log(self, monkeypatch):
        """A normal seed dict whose description does NOT contain the
        phrase must seed with its stated status unchanged, with no log
        call at all — the guard must not affect every other agent."""
        import backend.api.algo.agent_engine as agent_engine_mod
        mock_calls: list = []
        mock_logger = SimpleNamespace(error=lambda *a, **k: mock_calls.append((a, k)))
        monkeypatch.setattr(agent_engine_mod, "logger", mock_logger)

        seed = {
            "slug": "synthetic-normal-agent",
            "status": "active",
            "description": "A perfectly ordinary active notify-only agent.",
        }
        result = _ae_guard_seed_status(seed)
        assert result == "active", f"expected 'active' passthrough, got {result!r}"
        assert mock_calls == [], f"expected no log calls for a non-mismatched agent, got {mock_calls}"

    def test_inactive_status_with_phrase_is_not_a_mismatch(self):
        """status='inactive' + the phrase is the CORRECT, consistent
        state — not something the guard should touch or complain about."""
        seed = {
            "slug": "synthetic-correct-agent",
            "status": "inactive",
            "description": "Ships INACTIVE (destructive).",
        }
        result = _ae_guard_seed_status(seed)
        assert result == "inactive", f"expected 'inactive' passthrough, got {result!r}"

    def test_default_none_used_by_sync_path_when_status_key_absent(self):
        """The sync-path default (None) must not falsely trigger the
        mismatch branch when status is genuinely absent (no desired
        status to force) and no phrase mismatch is even present."""
        seed = {
            "slug": "synthetic-sync-no-status",
            "description": "A normal agent with no explicit status override.",
        }
        result = _ae_guard_seed_status(seed, default=None)
        assert result is None, f"expected None passthrough, got {result!r}"


class TestAeBuildAgentRowUsesGuard:
    """Insert path: a synthetic mismatched seed dict must never construct
    an Agent row with status='active'."""

    def _minimal_def(self, **overrides):
        base = {
            "slug": "synthetic-insert-mismatch",
            "name": "Synthetic insert mismatch",
            "conditions": {},
            "events": [],
            "actions": [],
            "status": "active",
            "description": "Does something destructive. Ships INACTIVE (destructive).",
        }
        base.update(overrides)
        return base

    def test_mismatched_seed_inserts_as_inactive(self):
        agent_def = self._minimal_def()
        row = _ae_build_agent_row(agent_def)
        assert row.status == "inactive", (
            f"expected inserted row status='inactive' for a mismatched "
            f"seed dict, got {row.status!r}"
        )

    def test_mismatched_seed_with_no_status_key_inserts_as_inactive(self):
        """Confirms the insert-path default ('active') is also guarded,
        not just an explicit status='active'."""
        agent_def = self._minimal_def()
        del agent_def["status"]
        row = _ae_build_agent_row(agent_def)
        assert row.status == "inactive", (
            f"expected default-active insert to be forced 'inactive' for "
            f"a mismatched seed dict, got {row.status!r}"
        )

    def test_normal_seed_inserts_with_its_stated_status(self):
        """Control case: an ordinary agent with no phrase in its
        description inserts with whatever status it declares."""
        agent_def = self._minimal_def(
            description="A perfectly ordinary active agent.", status="active",
        )
        row = _ae_build_agent_row(agent_def)
        assert row.status == "active", (
            f"expected normal seed to insert as 'active' unchanged, got "
            f"{row.status!r}"
        )


class TestAeSyncExistingBuiltinUsesGuard:
    """Sync path: an existing inactive row must not be flipped back to
    active by a mismatched code seed dict (the exact bug this fix
    closes — a hand-patched-inactive prod row must survive re-sync)."""

    def _make_existing(self, status="inactive"):
        return SimpleNamespace(
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

    def test_existing_inactive_row_stays_inactive_against_mismatched_def(self):
        existing = self._make_existing(status="inactive")
        agent_def = {
            "slug": "synthetic-sync-mismatch",
            "status": "active",
            "description": "Destructive. Ships INACTIVE (destructive).",
            "schedule": "market_hours",
        }
        _ae_sync_existing_builtin(existing, agent_def)
        assert existing.status == "inactive", (
            f"a hand-patched-inactive row must not be resurrected active "
            f"by a mismatched seed dict, got {existing.status!r}"
        )

    def test_existing_active_row_still_flips_inactive_against_mismatched_def(self):
        """The bidirectional sync still converges an 'active' existing
        row to the (guard-corrected) code default of 'inactive' — the
        guard forces the code-side value, it doesn't freeze the row."""
        existing = self._make_existing(status="active")
        agent_def = {
            "slug": "synthetic-sync-mismatch-2",
            "status": "active",
            "description": "Destructive. Ships INACTIVE (destructive).",
            "schedule": "market_hours",
        }
        _ae_sync_existing_builtin(existing, agent_def)
        assert existing.status == "inactive", (
            f"expected existing active row to converge to the "
            f"guard-corrected 'inactive', got {existing.status!r}"
        )

    def test_normal_def_sync_still_converges_status_unaffected_by_guard(self):
        """Control case: a normal (non-mismatched) seed dict's status
        sync behavior is completely unaffected by the guard."""
        existing = self._make_existing(status="inactive")
        agent_def = {
            "slug": "synthetic-sync-normal",
            "status": "active",
            "description": "A perfectly ordinary agent.",
            "schedule": "market_hours",
        }
        _ae_sync_existing_builtin(existing, agent_def)
        assert existing.status == "active", (
            f"expected normal def's status to sync through unaffected by "
            f"the guard, got {existing.status!r}"
        )
