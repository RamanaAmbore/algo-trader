"""Alert routing tree — resolver coverage (2026-10 routing-tree plan).

Pins `alert_routing.resolve_channels()` against the frozen pre-migration
oracle for every one of the 20 migrated `SEEDED_AGENTS` entries (captured
from the real `event_agents.py` module BEFORE this migration, cross-
checked against both prod and dev `agents` DB rows before this shipped —
see `.claude/PLAN.md`'s history for the full record), proves the
mandatory catch-all fires for an unmatched combination, and proves a
per-slug override always wins over a matching generic tier rule for the
same agent.
"""
import pytest

from backend.api.algo import alert_routing, event_agents


# Frozen pre-migration oracle — exact `events` list every one of these
# 20 agents carried BEFORE channel selection moved to `alert_routing.py`.
# Do NOT regenerate this from the post-migration code; it is the one
# independent ground truth this test exists to protect.
_FROZEN_ORACLE: dict[str, list[dict]] = {
    "fill-alert": [{"channel": "telegram", "enabled": True}],
    "error-alert": [{"channel": "telegram", "enabled": True}, {"channel": "ntfy", "enabled": True}],
    "chase-cancel-alert": [
        {"channel": "telegram", "enabled": True},
        {"channel": "ntfy", "enabled": True, "priority": "urgent"},
    ],
    "partial-gtt-alert": [{"channel": "ntfy", "enabled": True, "priority": "urgent"}],
    "template-attach-urgent": [
        {"channel": "telegram", "enabled": True},
        {"channel": "ntfy", "enabled": True, "priority": "urgent"},
    ],
    "template-attach-high": [{"channel": "ntfy", "enabled": True, "priority": "high"}],
    "order-failure-alert": [
        {"channel": "telegram", "enabled": True, "gate": False},
        {"channel": "ntfy", "enabled": True, "gate": False, "priority": "urgent"},
        {"channel": "email", "enabled": True, "gate": False},
    ],
    "template-guard-alert": [
        {"channel": "telegram", "enabled": True, "gate": False},
        {"channel": "ntfy", "enabled": True, "gate": False, "priority": "high"},
        {"channel": "email", "enabled": True, "gate": False},
    ],
    "template-attach-fail-alert": [
        {"channel": "telegram", "enabled": True, "gate": False},
        {"channel": "ntfy", "enabled": True, "gate": False, "priority": "urgent"},
        {"channel": "email", "enabled": True, "gate": False},
    ],
    "mcp-ping-alert": [{"channel": "telegram", "enabled": True, "gate": False}],
    "order-correction-alert": [
        {"channel": "telegram", "enabled": True, "gate": False},
        {"channel": "ntfy", "enabled": True, "gate": False, "priority": "urgent"},
    ],
    "deploy-sync-alert": [],
    "agent-alert-rich": [
        {"channel": "telegram", "enabled": True, "gate": False},
        {"channel": "ntfy", "enabled": True, "gate": False},
        {"channel": "email", "enabled": True, "gate": False},
    ],
    "market-summary": [
        {"channel": "telegram_info", "enabled": True, "gate": False},
        {"channel": "email", "enabled": True, "gate": False},
    ],
    "gtt-not-accepted-alert": [{"channel": "ntfy", "enabled": True, "gate": False, "priority": "urgent"}],
    "gtt-asymmetric-alert": [
        {"channel": "telegram", "enabled": True, "gate": False},
        {"channel": "ntfy", "enabled": True, "gate": False, "priority": "high"},
    ],
    "oco-double-fire-alert": [
        {"channel": "telegram", "enabled": True, "gate": False},
        {"channel": "ntfy", "enabled": True, "gate": False, "priority": "urgent"},
    ],
    "ticker-recovered-alert": [
        {"channel": "telegram", "enabled": True, "gate": False},
        {"channel": "ntfy", "enabled": True, "gate": False, "priority": "high"},
    ],
    "ticker-degraded-alert": [
        {"channel": "telegram", "enabled": True, "gate": False},
        {"channel": "ntfy", "enabled": True, "gate": False, "priority": "urgent"},
    ],
    "visitor-report-alert": [{"channel": "telegram_info", "enabled": True, "gate": False}],
}


def _spec_by_slug(slug: str) -> dict:
    for spec in event_agents.SEEDED_AGENTS:
        if spec["slug"] == slug:
            return spec
    raise AssertionError(f"no SEEDED_AGENTS entry for slug {slug!r}")


@pytest.mark.parametrize("slug", sorted(_FROZEN_ORACLE))
def test_resolve_channels_matches_frozen_pre_migration_oracle(slug):
    """For every one of the 20 migrated agents, the RESOLVED channel
    list is byte-identical to what that agent's own `events` dict
    literal used to carry directly, before this migration moved channel
    selection into this module's per-slug override table."""
    spec = _spec_by_slug(slug)
    resolved = alert_routing.resolve_channels(
        name=spec["slug"], priority=spec.get("priority"),
        agent_events=spec.get("events", []),
    )
    assert resolved == _FROZEN_ORACLE[slug]


def test_frozen_oracle_covers_every_migrated_agent_exactly():
    """Guard against a slug silently falling out of (or an extra one
    sneaking into) the oracle above — compares against `SEEDED_AGENTS`
    itself, excluding `BREACH_AGENT` (dynamic per-record channels via
    `out[4]`, never resolved through this table), `UNROUTED_ALERT_AGENT`
    (the catch-all's own infrastructure, not migrated — see
    `alert_routing.CATCH_ALL_CHANNELS`), and `USER_REGISTERED_AGENT`
    (added NET-NEW alongside this plan, not migrated from a pre-existing
    `events` literal — it has no pre-migration oracle entry to pin; see
    `test_user_registered_agent_resolves_to_telegram_and_ntfy_high`
    below for its own direct coverage)."""
    migrated_slugs = {
        spec["slug"] for spec in event_agents.SEEDED_AGENTS
        if spec["slug"] not in (
            event_agents.BREACH_AGENT["slug"], event_agents.UNROUTED_ALERT_AGENT["slug"],
            event_agents.USER_REGISTERED_AGENT["slug"],
        )
    }
    assert migrated_slugs == set(_FROZEN_ORACLE)


def test_user_registered_agent_resolves_to_telegram_and_ntfy_high():
    """2026-10 operator-reported gap: new-user self-registration alerts
    never reached a real channel because the producing log line
    (`routes/auth.py`) carried no alert-tag metadata at all. Confirms
    the new agent's own per-slug override resolves correctly — high
    priority, telegram + ntfy, not urgent (not time-critical)."""
    spec = event_agents.USER_REGISTERED_AGENT
    resolved = alert_routing.resolve_channels(
        name=spec["slug"], priority=spec.get("priority"), agent_events=spec.get("events", []))
    assert resolved == [
        {"channel": "telegram", "enabled": True, "gate": False},
        {"channel": "ntfy", "enabled": True, "gate": False, "priority": "high"},
    ]


def test_unrouted_alert_agent_events_matches_catch_all_channels():
    """`alert_routing.CATCH_ALL_CHANNELS` is a deliberate, documented
    duplication of `UNROUTED_ALERT_AGENT["events"]` (can't import from
    `event_agents.py` without a circular import — see that constant's
    own comment) — this test is what keeps the two from silently
    drifting apart."""
    assert alert_routing.CATCH_ALL_CHANNELS == event_agents.UNROUTED_ALERT_AGENT["events"]


def test_resolve_channels_falls_through_to_catch_all_when_nothing_matches():
    """An unrecognised slug + an unrecognised/absent priority resolves
    to the mandatory catch-all, never an empty list — 'nothing silently
    drops' (plan Design item 1 / Done-when)."""
    resolved = alert_routing.resolve_channels(name="totally-unknown-slug", priority="not-a-real-tier")
    assert resolved == alert_routing.CATCH_ALL_CHANNELS


def test_resolve_channels_falls_through_to_catch_all_with_no_args_at_all():
    """Every argument is optional and absent — still resolves to the
    catch-all, never raises and never returns an empty list."""
    assert alert_routing.resolve_channels() == alert_routing.CATCH_ALL_CHANNELS


def test_resolve_channels_tier_default_used_when_no_per_slug_override():
    """An unrecognised slug but a REAL priority tier resolves to that
    tier's generic default, not the catch-all."""
    resolved = alert_routing.resolve_channels(name="totally-unknown-slug", priority="high")
    assert resolved == alert_routing._TIER_DEFAULTS["high"]


def test_per_slug_override_wins_over_matching_generic_tier_rule():
    """`order-failure-alert`'s own priority is 'urgent', and its
    per-slug override channel set DIFFERS from the generic 'urgent' tier
    default (the override adds an email channel and `gate: False` on
    every entry; the tier default has neither) — proves the per-slug
    table is consulted BEFORE falling back to the tier default for the
    SAME agent, not that the two happen to coincidentally agree."""
    override = alert_routing._PER_SLUG_OVERRIDES["order-failure-alert"]
    tier_default = alert_routing._TIER_DEFAULTS["urgent"]
    assert override != tier_default, "fixture sanity: the two must genuinely differ for this test to prove anything"
    resolved = alert_routing.resolve_channels(name="order-failure-alert", priority="urgent", agent_events=[])
    assert resolved == override


def test_explicit_agent_events_wins_over_per_slug_override():
    """Confirmed precedence (coordinator decision, 2026-10): an
    explicitly non-empty `agent.events` — e.g. an operator-authored
    `ALERT ...` statement, or a future operator edit to a seeded row —
    always wins over this module's own per-slug override, so this table
    can never silently discard an explicit, real configuration. Same
    protection class `seed_spec_hash`/`modified_skip` already provides
    for `conditions`/`actions`."""
    explicit = [{"channel": "email", "enabled": True}]
    resolved = alert_routing.resolve_channels(
        name="chase-cancel-alert", priority="urgent", agent_events=explicit)
    assert resolved == explicit


def test_known_priorities_matches_tier_defaults_keys():
    """`KNOWN_PRIORITIES` (consulted by
    `event_agents.validate_seed_spec()`) is exactly the set of tiers
    `_TIER_DEFAULTS` actually recognises — never a second, driftable
    vocabulary."""
    assert alert_routing.KNOWN_PRIORITIES == frozenset(alert_routing._TIER_DEFAULTS.keys())


# ── Confirmed-incident regression: BREACH_AGENT catch-all log loop ───────

def test_breach_agent_resolves_to_empty_list_not_the_catch_all(caplog):
    """`_dispatch_agent` resolves EVERY active loaded agent's channels
    once per `dispatch()` call, regardless of whether any record
    matches that agent's conditions. `cycle-breach-alert` (empty
    `events`, no `priority`) would otherwise fall through to the
    catch-all on every single flush, logging a fresh WARNING each time
    via this module's own logger — which is NOT skip-listed in
    `log_store.py`'s `_SKIP_PREFIXES`, so that WARNING would be captured
    and re-enter the next flush, forever. Confirmed via code trace
    before this test was written. The explicit `[]` override for this
    slug (see `_PER_SLUG_OVERRIDES`'s own comment) must short-circuit
    before ever reaching the catch-all/logging branch."""
    with caplog.at_level("WARNING", logger=alert_routing._logger.name):
        alert_routing._logger.addHandler(caplog.handler)
        try:
            resolved = alert_routing.resolve_channels(
                name=event_agents.BREACH_AGENT["slug"],
                priority=event_agents.BREACH_AGENT.get("priority"),
                agent_events=event_agents.BREACH_AGENT.get("events") or [],
            )
        finally:
            alert_routing._logger.removeHandler(caplog.handler)
    assert resolved == []
    assert caplog.records == [], "must not hit the catch-all/warning branch at all"


def test_catch_all_warning_logs_at_most_once_per_name_priority_combo(monkeypatch, caplog):
    """Second layer of defense behind the `cycle-breach-alert` fix above
    — ANY future agent that falls through to the catch-all (unrecognised
    slug + unrecognised/absent priority) must warn at most once per
    unique `(name, priority)` combination per process, not once per
    `resolve_channels()` call, so a repeatedly-dispatched misconfigured
    agent cannot amplify into a runaway log loop the way BREACH's own
    incident did."""
    monkeypatch.setattr(alert_routing, "_WARNED_COMBOS", set())
    alert_routing._logger.addHandler(caplog.handler)
    alert_routing._logger.setLevel("WARNING")
    try:
        for _ in range(5):
            resolved = alert_routing.resolve_channels(name="never-seen-before-slug", priority="also-unknown")
            assert resolved == alert_routing.CATCH_ALL_CHANNELS
    finally:
        alert_routing._logger.removeHandler(caplog.handler)
    assert len(caplog.records) == 1, "must warn exactly once across 5 identical calls"
    # A genuinely DIFFERENT combination still gets its own one-time warning.
    alert_routing._logger.addHandler(caplog.handler)
    try:
        alert_routing.resolve_channels(name="a-different-never-seen-slug", priority="also-unknown")
    finally:
        alert_routing._logger.removeHandler(caplog.handler)
    assert len(caplog.records) == 2


# ── Confirmed-incident regression: the new `auth` tag's blast radius ─────

def test_auth_internal_tagged_records_never_reach_the_catch_all():
    """2026-10 — adding `"auth"` to `LOG_TAG_TOKENS` (so
    `USER_REGISTERED_AGENT` can match the real registration record) was
    found, via advisor review, to retroactively expose every OTHER
    pre-existing bare `logger.warning(...)` call in `routes/auth.py` to
    `UNROUTED_ALERT_AGENT`'s catch-all — `log_store.py:tags_for()` auto-
    tags any untagged record with the last dotted component of its
    logger's name (`"auth"` for this whole module), and `"auth"` is now
    a known alert tag. `routes/auth.py` insulates those 12 call sites
    with `extra={"tags": ["auth_internal"]}` (a deliberately
    UNREGISTERED tag) instead. This test proves the insulation actually
    works: a WARNING-level record tagged only `"auth_internal"` must
    never be classified alert-shaped, while the SAME record tagged
    `"auth"` instead (simulating what would happen WITHOUT the fix) IS
    alert-shaped — confirming this test would have caught the
    regression before it shipped."""
    from backend.api.algo import event_agents as ea

    insulated = {"ts": None, "level": "WARNING", "logger": "backend.api.routes.auth",
                 "message": "Auth: watchlist seed failed for 'x': boom",
                 "tags": ["warning", "auth_internal"], "extra": {}}
    assert ea._looks_alert_shaped(insulated) is False

    # Sanity check the mechanism itself: the SAME shape, but tagged
    # "auth" instead, IS alert-shaped — proving the test isn't
    # vacuously passing for some unrelated reason (e.g. a typo'd field
    # name that would make _looks_alert_shaped always return False).
    unfixed = {**insulated, "tags": ["warning", "auth"]}
    assert ea._looks_alert_shaped(unfixed) is True

    # Confirm "auth_internal" was deliberately never registered in the
    # known-alert-tag vocabulary (that's what makes the insulation
    # work), while "auth" genuinely is (needed for USER_REGISTERED_AGENT).
    assert "auth_internal" not in ea._known_alert_tags()
    assert "auth" in ea._known_alert_tags()
