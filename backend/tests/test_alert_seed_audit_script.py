"""scripts/alert_seed_audit.py — parser/audit-logic unit tests.

Pure-function coverage for the pre-bump audit script (2026-10 routing-tree
activation plan, Step 6). No DB connection is made in these tests — only
`_parse_jsonish`, `_audit`, and `_render` are exercised directly, with a
fake row list and a monkeypatched `SEEDED_AGENTS`/`resolve_channels`
stand-in. `_build_dsn`'s env-var branch is covered without touching a
real DB.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts import alert_seed_audit as audit_mod


# ── _parse_jsonish ──────────────────────────────────────────────────────

class TestParseJsonish:
    def test_none_passes_through(self):
        assert audit_mod._parse_jsonish(None) is None

    def test_dict_passes_through_unchanged(self):
        d = {"a": 1}
        assert audit_mod._parse_jsonish(d) is d

    def test_list_passes_through_unchanged(self):
        lst = [{"channel": "telegram"}]
        assert audit_mod._parse_jsonish(lst) is lst

    def test_json_string_is_decoded(self):
        assert audit_mod._parse_jsonish('{"channel": "telegram", "enabled": true}') == {
            "channel": "telegram", "enabled": True,
        }

    def test_json_array_string_is_decoded(self):
        assert audit_mod._parse_jsonish("[1, 2, 3]") == [1, 2, 3]


# ── _build_dsn ───────────────────────────────────────────────────────────

class TestBuildDsn:
    def test_env_var_overrides_config_and_strips_asyncpg_scheme(self, monkeypatch):
        monkeypatch.setenv(
            "DATABASE_URL", "postgresql+asyncpg://u:p@host:5432/db")
        assert audit_mod._build_dsn() == "postgresql://u:p@host:5432/db"

    def test_env_var_plain_postgresql_scheme_unchanged(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host:5432/db")
        assert audit_mod._build_dsn() == "postgresql://u:p@host:5432/db"


class TestRedactedDsn:
    def test_password_is_redacted(self):
        dsn = "postgresql://rambo_admin:supersecret@localhost:5432/ramboq"
        redacted = audit_mod._redacted_dsn(dsn)
        assert "supersecret" not in redacted
        assert redacted == "postgresql://rambo_admin:***@localhost:5432/ramboq"


# ── _audit ────────────────────────────────────────────────────────────────

def _row(slug, conditions, events, actions, priority, seed_version, seed_spec_hash):
    return SimpleNamespace(
        slug=slug, conditions=conditions, events=events, actions=actions,
        priority=priority, seed_version=seed_version, seed_spec_hash=seed_spec_hash,
    )


class TestAudit:
    def test_hash_match_row_reports_update_and_no_channel_change(self, monkeypatch):
        from backend.api.algo import event_agents as ea

        spec = {"slug": "fill-alert", "name": "Fill alert",
                "conditions": {"log": {"tag": "orders"}}, "actions": [],
                "priority": "medium"}
        monkeypatch.setattr(ea, "SEEDED_AGENTS", (spec,))

        clean_hash = ea.compute_seed_spec_hash(spec["conditions"], [], spec["actions"])
        rows = [_row("fill-alert", spec["conditions"], [], spec["actions"],
                      "medium", 2, clean_hash)]

        report = audit_mod._audit(rows, target_version=3)
        entry = report["per_slug"][0]
        assert entry["slug"] == "fill-alert"
        assert entry["seed_action_at_target"] == "update"
        assert entry["hash_match"] is True
        assert entry["channels_would_change"] is False
        assert report["unseeded_db_rows"] == []

    def test_hash_mismatch_row_reports_modified_skip(self, monkeypatch):
        from backend.api.algo import event_agents as ea

        spec = {"slug": "fill-alert", "name": "Fill alert",
                "conditions": {"log": {"tag": "orders"}}, "actions": [],
                "priority": "medium"}
        monkeypatch.setattr(ea, "SEEDED_AGENTS", (spec,))

        original_hash = ea.compute_seed_spec_hash(spec["conditions"], [], spec["actions"])
        edited_events = [{"channel": "email", "enabled": True}]  # operator edit
        rows = [_row("fill-alert", spec["conditions"], edited_events, spec["actions"],
                      "medium", 2, original_hash)]

        report = audit_mod._audit(rows, target_version=3)
        entry = report["per_slug"][0]
        assert entry["seed_action_at_target"] == "modified_skip"
        assert entry["hash_match"] is False

    def test_missing_db_row_reports_insert(self, monkeypatch):
        from backend.api.algo import event_agents as ea

        spec = {"slug": "brand-new-agent", "name": "Brand new",
                "conditions": {"log": {"tag": "orders"}}, "actions": [],
                "priority": "medium"}
        monkeypatch.setattr(ea, "SEEDED_AGENTS", (spec,))

        report = audit_mod._audit([], target_version=3)
        entry = report["per_slug"][0]
        assert entry["db_row"] == "MISSING (would insert)"

    def test_unseeded_db_row_is_reported_separately(self, monkeypatch):
        from backend.api.algo import event_agents as ea

        monkeypatch.setattr(ea, "SEEDED_AGENTS", ())
        rows = [_row("operator-authored-agent", {}, [], [], None, 1, None)]

        report = audit_mod._audit(rows, target_version=3)
        assert report["per_slug"] == []
        assert report["unseeded_db_rows"] == ["operator-authored-agent"]

    def test_legacy_null_hash_row_is_flagged_hash_is_null_not_mismatch(self, monkeypatch):
        """A pre-seed_spec_hash-column row (or one never seeded) must be
        distinguished from a genuine hash mismatch — `hash_match` is
        `None`, and `hash_is_null` is `True`."""
        from backend.api.algo import event_agents as ea

        spec = {"slug": "legacy-agent", "name": "Legacy", "conditions": {},
                "actions": [], "priority": None}
        monkeypatch.setattr(ea, "SEEDED_AGENTS", (spec,))
        rows = [_row("legacy-agent", {}, [], [], None, 0, None)]

        report = audit_mod._audit(rows, target_version=3)
        entry = report["per_slug"][0]
        assert entry["hash_match"] is None
        assert entry["hash_is_null"] is True

    def test_channel_change_is_detected_when_priority_resolves_differently(self, monkeypatch):
        """A row currently resolving through one priority tier, whose
        spec's priority would resolve to a DIFFERENT tier after a
        version bump, must be flagged `channels_would_change=True` —
        the one thing a bare hash-match check cannot catch."""
        from backend.api.algo import event_agents as ea

        spec = {"slug": "some-agent", "name": "Some agent", "conditions": {},
                "actions": [], "priority": "critical"}  # code's NEW priority
        monkeypatch.setattr(ea, "SEEDED_AGENTS", (spec,))

        clean_hash = ea.compute_seed_spec_hash({}, [], [])
        # Row currently has priority="medium" (stale, pre-bump priority)
        rows = [_row("some-agent", {}, [], [], "medium", 2, clean_hash)]

        report = audit_mod._audit(rows, target_version=3)
        entry = report["per_slug"][0]
        assert entry["channels_would_change"] is True
        assert entry["today_channels"] != entry["post_bump_channels"]


# ── _render ──────────────────────────────────────────────────────────────

class TestRender:
    def test_clean_report_renders_no_risk_found(self):
        report = {
            "per_slug": [{
                "slug": "fill-alert", "seed_action_at_target": "update",
                "hash_match": True, "hash_is_null": False,
                "today_channels": [{"channel": "telegram", "enabled": True}],
                "post_bump_channels": [{"channel": "telegram", "enabled": True}],
                "channels_would_change": False,
            }],
            "unseeded_db_rows": [],
        }
        out = audit_mod._render(report)
        assert "No risk found" in out
        assert "RISK FOUND" not in out
        assert "!!" not in out

    def test_modified_skip_or_channel_change_renders_risk_found(self):
        report = {
            "per_slug": [{
                "slug": "fill-alert", "seed_action_at_target": "modified_skip",
                "hash_match": False, "hash_is_null": False,
                "today_channels": [], "post_bump_channels": [],
                "channels_would_change": False,
            }],
            "unseeded_db_rows": [],
        }
        out = audit_mod._render(report)
        assert "RISK FOUND" in out
        assert "!!" in out

    def test_missing_row_renders_insert_note(self):
        report = {"per_slug": [{"slug": "ghost", "db_row": "MISSING (would insert)"}],
                   "unseeded_db_rows": []}
        out = audit_mod._render(report)
        assert "ghost" in out and "INSERT" in out

    def test_unseeded_rows_are_listed(self):
        report = {"per_slug": [], "unseeded_db_rows": ["manual-agent"]}
        out = audit_mod._render(report)
        assert "manual-agent" in out


# ── main() exit code ────────────────────────────────────────────────────

class TestMainExitCode:
    @pytest.mark.asyncio
    async def test_main_async_returns_1_when_risk_found(self, monkeypatch):
        async def _fake_fetch(dsn):
            return []

        def _fake_audit(rows, target_version):
            return {"per_slug": [{"slug": "x", "seed_action_at_target": "modified_skip",
                                   "hash_match": False, "hash_is_null": False,
                                   "today_channels": [], "post_bump_channels": [],
                                   "channels_would_change": False}],
                    "unseeded_db_rows": []}

        monkeypatch.setattr(audit_mod, "_build_dsn", lambda: "postgresql://x")
        monkeypatch.setattr(audit_mod, "_fetch_event_agent_rows", _fake_fetch)
        monkeypatch.setattr(audit_mod, "_audit", _fake_audit)

        rc = await audit_mod._main_async(target_version=3)
        assert rc == 1

    @pytest.mark.asyncio
    async def test_main_async_returns_0_when_clean(self, monkeypatch):
        async def _fake_fetch(dsn):
            return []

        def _fake_audit(rows, target_version):
            return {"per_slug": [{"slug": "x", "seed_action_at_target": "update",
                                   "hash_match": True, "hash_is_null": False,
                                   "today_channels": [], "post_bump_channels": [],
                                   "channels_would_change": False}],
                    "unseeded_db_rows": []}

        monkeypatch.setattr(audit_mod, "_build_dsn", lambda: "postgresql://x")
        monkeypatch.setattr(audit_mod, "_fetch_event_agent_rows", _fake_fetch)
        monkeypatch.setattr(audit_mod, "_audit", _fake_audit)

        rc = await audit_mod._main_async(target_version=3)
        assert rc == 0
