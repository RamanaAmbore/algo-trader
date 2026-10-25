"""Alert routing tree — resolver coverage (2026-10 routing-tree plan).

Pins `alert_routing.resolve_channels()` against the frozen pre-migration
oracle for every one of the 20 migrated `SEEDED_AGENTS` entries (captured
from the real `event_agents.py` module BEFORE this migration, cross-
checked against both prod and dev `agents` DB rows before this shipped —
see `.claude/PLAN.md`'s history for the full record), proves the
mandatory catch-all fires for an unmatched combination, and proves a
per-slug override always wins over a matching generic tier rule for the
same agent.

2026-10 channel-routing fix (operator requirement #3 + correction):
four entries below are intentionally amended past the frozen
pre-migration shapes, each noted at its own entry — `partial-gtt-alert`
and `template-attach-high` gained a plain `telegram` channel (both were
ntfy-only); `deploy-sync-alert` moved from zero channels to the
dedicated `telegram_deploy` channel alone (never the generic `telegram`,
never ntfy, never email — operator correction).
"""
import pytest

from backend.api.algo import alert_routing, event_agents


# Frozen pre-migration oracle — exact `events` list every one of these
# 20 agents carried BEFORE channel selection moved to `alert_routing.py`.
# Do NOT regenerate this from the post-migration code; it is the one
# independent ground truth this test exists to protect. The four
# 2026-10 channel-routing amendments (see module docstring) are each
# marked at their own entry below.
_FROZEN_ORACLE: dict[str, list[dict]] = {
    "fill-alert": [{"channel": "telegram", "enabled": True}],
    "error-alert": [{"channel": "telegram", "enabled": True}, {"channel": "ntfy", "enabled": True}],
    "chase-cancel-alert": [
        {"channel": "telegram", "enabled": True},
        {"channel": "ntfy", "enabled": True, "priority": "urgent"},
    ],
    # 2026-10 channel-routing fix — gained `telegram` (was ntfy-only);
    # operator requirement #3, "every alert goes to Telegram".
    "partial-gtt-alert": [
        {"channel": "telegram", "enabled": True},
        {"channel": "ntfy", "enabled": True, "priority": "urgent"},
    ],
    "template-attach-urgent": [
        {"channel": "telegram", "enabled": True},
        {"channel": "ntfy", "enabled": True, "priority": "urgent"},
    ],
    # 2026-10 channel-routing fix — gained `telegram` (was ntfy-only);
    # same rationale as partial-gtt-alert above.
    "template-attach-high": [
        {"channel": "telegram", "enabled": True},
        {"channel": "ntfy", "enabled": True, "priority": "high"},
    ],
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
    # 2026-10 channel-routing fix (operator correction) — moved from
    # zero channels to the DEDICATED `telegram_deploy` channel alone.
    # Explicitly NOT the generic `telegram` channel, NOT ntfy, NOT
    # email — "the telegram code deployment alerts channel alone".
    "deploy-sync-alert": [
        {"channel": "telegram_deploy", "enabled": True, "gate": False},
    ],
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
    # 2026-10-25 alert-channel collapse: 16 of these slugs' own specs
    # are retired from SEEDED_AGENTS (no longer standalone Agent rows)
    # but remain byte-identical module-level dicts
    # (`event_agents._COLLAPSED_DETECTOR_SPECS`), feeding
    # `ALERT_EVENT_DISPATCH`. This test is about `resolve_channels()`'s
    # per-slug table, which still carries every one of their rows
    # unchanged — unaffected by whether the slug still seeds a
    # standalone row — so the search pool is widened to both.
    for spec in event_agents.SEEDED_AGENTS + event_agents._COLLAPSED_DETECTOR_SPECS:
        if spec["slug"] == slug:
            return spec
    raise AssertionError(f"no known spec for slug {slug!r}")


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
    below for its own direct coverage).

    2026-10-25 alert-channel collapse: the comparison pool widens to
    `SEEDED_AGENTS + _COLLAPSED_DETECTOR_SPECS`, since 16 of these 20
    oracle slugs now live only in the latter (see `_spec_by_slug`'s own
    comment above). The 3 new generic channel agents are excluded for
    the same reason `USER_REGISTERED_AGENT` already was — net-new, no
    pre-migration oracle entry to pin."""
    all_specs = event_agents.SEEDED_AGENTS + event_agents._COLLAPSED_DETECTOR_SPECS
    migrated_slugs = {
        spec["slug"] for spec in all_specs
        if spec["slug"] not in (
            event_agents.BREACH_AGENT["slug"], event_agents.UNROUTED_ALERT_AGENT["slug"],
            event_agents.USER_REGISTERED_AGENT["slug"],
            event_agents.TELEGRAM_CHANNEL_AGENT["slug"], event_agents.NTFY_CHANNEL_AGENT["slug"],
            event_agents.EMAIL_CHANNEL_AGENT["slug"],
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


def test_unrouted_alert_agent_events_value_is_a_copy_not_an_alias():
    """Value/identity check only — NOT the real regression guard for
    Step 4 (see the structural test below for that). This proves the
    two are equal in content and never the SAME list/dict objects, but
    that alone does not distinguish a genuine derived copy from a
    hand-written literal that merely happens to match today: a
    hand-duplicated literal was ALSO never aliased to this constant, so
    it would pass every assertion in this function too. Kept as a
    basic sanity check (and because mutating the live constant and
    confirming the agent spec is unaffected is still a meaningful
    "not a lazy/live reference" check), not as proof of derivation."""
    assert alert_routing.CATCH_ALL_CHANNELS == event_agents.UNROUTED_ALERT_AGENT["events"]
    assert alert_routing.CATCH_ALL_CHANNELS is not event_agents.UNROUTED_ALERT_AGENT["events"]
    for shared, owned in zip(alert_routing.CATCH_ALL_CHANNELS, event_agents.UNROUTED_ALERT_AGENT["events"]):
        assert shared is not owned
    sentinel = {"channel": "__sentinel__", "enabled": True}
    alert_routing.CATCH_ALL_CHANNELS.append(sentinel)
    try:
        assert sentinel not in event_agents.UNROUTED_ALERT_AGENT["events"]
    finally:
        alert_routing.CATCH_ALL_CHANNELS.remove(sentinel)


def test_unrouted_alert_agent_events_is_structurally_built_from_catch_all_channels():
    """The real Step 4 regression guard. The value/identity test above
    cannot tell a derived copy apart from a re-typed literal — both
    would pass it, since a hand-duplicated literal is also never
    aliased to the constant it was copied from. This test instead reads
    `event_agents.py`'s own source for the `UNROUTED_ALERT_AGENT` dict
    literal and asserts its `events` value is actually built via a
    comprehension over `alert_routing.CATCH_ALL_CHANNELS`, with no
    re-typed channel-dict literal sitting next to it. Confirmed to FAIL
    against the pre-fix source (a hand-written `events: [{"channel":
    "telegram", ...}, {"channel": "ntfy", ...}]` literal, with no
    `CATCH_ALL_CHANNELS` reference at all)."""
    import inspect

    src = inspect.getsource(event_agents)
    start = src.index("UNROUTED_ALERT_AGENT = {")
    end = src.index("\n}\n", start)
    block = src[start:end]
    assert "alert_routing.CATCH_ALL_CHANNELS" in block, (
        "UNROUTED_ALERT_AGENT must reference the shared constant, not a re-typed literal"
    )
    assert "dict(ch) for ch in alert_routing.CATCH_ALL_CHANNELS" in block, (
        "events must be built via a per-entry copy of CATCH_ALL_CHANNELS, not a bare reference "
        "(a bare `alert_routing.CATCH_ALL_CHANNELS` assignment would alias the shared list)"
    )
    assert '"channel": "telegram"' not in block, "no re-typed channel literal may remain"
    assert '"channel": "ntfy"' not in block, "no re-typed channel literal may remain"


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
    old_level = alert_routing._logger.level
    alert_routing._logger.addHandler(caplog.handler)
    alert_routing._logger.setLevel("WARNING")
    try:
        for _ in range(5):
            resolved = alert_routing.resolve_channels(name="never-seen-before-slug", priority="also-unknown")
            assert resolved == alert_routing.CATCH_ALL_CHANNELS
    finally:
        alert_routing._logger.removeHandler(caplog.handler)
        alert_routing._logger.setLevel(old_level)
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
    UNREGISTERED tag) instead.

    Goes through the REAL pipeline end to end — a real
    `logging.LogRecord` built via `logging.Logger.makeRecord` (exactly
    how a genuine `logger.warning(...)` call builds one), through the
    real `log_store.tags_for()` (which derives the logger-name fallback
    AND exercises `_check_owner_tags()` — a hand-built dict in a prior
    version of this test never touched either), then `row_for()`'s real
    `extra` shape, then `_looks_alert_shaped()`. Proves the insulation
    actually works: the real `_AUTH_INTERNAL_TAGS` extra must never be
    classified alert-shaped, while the SAME record shape built with
    `extra={"tags": ["auth"]}` instead (simulating what would happen
    WITHOUT the fix) IS alert-shaped — confirming this test would have
    caught the regression before it shipped, not passed vacuously."""
    import logging
    from backend.api.algo import event_agents as ea
    from backend.api.routes import auth
    from backend.shared.helpers import log_store

    lg = logging.getLogger("backend.api.routes.auth")

    def _looks_alert_shaped_for(extra: dict) -> bool:
        record = lg.makeRecord(
            lg.name, logging.WARNING, __file__, 0,
            "Auth: watchlist seed failed for 'x': boom", None, None, extra=extra,
        )
        row = log_store.row_for(record)
        return ea._looks_alert_shaped(row)

    assert _looks_alert_shaped_for(auth._AUTH_INTERNAL_TAGS) is False

    # Sanity check the mechanism itself: the SAME real record shape,
    # but tagged "auth" instead, IS alert-shaped — proving the test
    # isn't vacuously passing for some unrelated reason.
    assert _looks_alert_shaped_for({"tags": ["auth"]}) is True

    # Confirm "auth_internal" was deliberately never registered in the
    # known-alert-tag vocabulary (that's what makes the insulation
    # work), while "auth" genuinely is (needed for USER_REGISTERED_AGENT).
    assert "auth_internal" not in ea._known_alert_tags()
    assert "auth" in ea._known_alert_tags()


# ── `Agent.priority` column + its migration ───────────────────────────

class _FakeTxConn:
    def __init__(self):
        self.executed: list[str] = []

    async def execute(self, stmt, *a, **k):
        self.executed.append(str(stmt))
        return None


def test_migrate_agents_priority_statement_shape():
    """Same pattern as every other additive `agents` column migration
    (`_migrate_agents_seed_spec_hash`/`_migrate_agents_given`/etc. in
    `test_agent_preamble_execution.py`): plain nullable `ADD COLUMN IF
    NOT EXISTS`, `lock_timeout` set, no `CONCURRENTLY` (that pattern is
    reserved for index creation, which runs outside the transaction)."""
    import asyncio
    from backend.api import database as dbmod
    conn = _FakeTxConn()
    asyncio.run(dbmod._migrate_agents_priority(conn))
    joined = "\n".join(conn.executed)
    assert "ALTER TABLE agents ADD COLUMN IF NOT EXISTS priority VARCHAR(16)" in joined
    assert any("lock_timeout" in s for s in conn.executed)
    assert not any("CONCURRENTLY" in s for s in conn.executed)


def test_init_db_calls_priority_migration_after_seed_spec_hash():
    import inspect
    from backend.api import database as dbmod
    src = inspect.getsource(dbmod.init_db)
    lines = src.splitlines()
    begin_idx = next(i for i, l in enumerate(lines) if "async with engine.begin() as conn:" in l)
    begin_indent = len(lines[begin_idx]) - len(lines[begin_idx].lstrip())

    def _idx(marker):
        i = next(j for j, l in enumerate(lines) if marker in l and "async def" not in l)
        return i, len(lines[i]) - len(lines[i].lstrip())

    hash_idx, _ = _idx("_migrate_agents_seed_spec_hash(conn)")
    priority_idx, priority_indent = _idx("_migrate_agents_priority(conn)")

    assert priority_indent > begin_indent, (
        "_migrate_agents_priority(conn) must run INSIDE the engine.begin() transactional block"
    )
    assert priority_idx > hash_idx, (
        "_migrate_agents_priority(conn) must run after _migrate_agents_seed_spec_hash(conn)"
    )


def test_agent_priority_column_is_nullable_with_no_server_default():
    """`Agent.priority` is nullable with no DB-level default — unlike
    `has_explicit_alert` (which requires `server_default` for a
    `NOT NULL` column on an already-populated table), a nullable column
    needs neither; `None` is itself the correct "no override" value the
    resolver's own fallback chain already handles."""
    from backend.api.models import Agent
    col = Agent.__table__.columns["priority"]
    assert col.nullable is True
    assert col.server_default is None


# ── 2026-10 Done-when regression guard: no seeded agent may resolve to
#    an empty channel list any more ───────────────────────────────────

def test_no_seeded_event_agent_resolves_to_an_empty_channel_list():
    """Plan 'Done when': every event agent resolves at least one real
    channel. `BREACH_AGENT` is the sole, documented exception — its
    real channels are 100% dynamic (`out[4]` from `_render_breach`,
    read directly by `_dispatch_agent` before this table is ever
    consulted for that record); its own static `[]` override exists
    only to short-circuit the catch-all-warning log loop (see that
    override's own comment), never to leave it genuinely unrouted."""
    for spec in event_agents.SEEDED_AGENTS:
        if spec["slug"] == event_agents.BREACH_AGENT["slug"]:
            continue
        resolved = alert_routing.resolve_channels(
            name=spec["slug"], priority=spec.get("priority"),
            agent_events=spec.get("events", []),
        )
        assert resolved != [], f"{spec['slug']!r} resolved to an empty channel list"
