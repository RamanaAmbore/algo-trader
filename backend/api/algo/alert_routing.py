"""Alert routing tree — the ONE place a `kind='event'` agent's rendered
output resolves to an actual channel set.

Mirrors Prometheus Alertmanager's routing-tree model: specific rules
first, a generic priority-tier default behind them, and a mandatory
catch-all last so a token combination that matches nothing still reaches
a real channel rather than silently dropping. A "detector" agent (one of
`backend/api/algo/event_agents.py`'s `SEEDED_AGENTS`) is only responsible
for matching a log-record shape and picking a renderer — it never names a
channel directly; `resolve_channels()` below is the one function that
does, keyed on the agent's own slug (`name`) first, falling back to a
`priority` tier, falling back to a fixed catch-all.

2026-10 routing-tree plan. Confirmed design decisions (council + operator,
see `.claude/PLAN.md` history for the full record):
  - One `Agent` DB row per producer-tag is preserved (no row-count change,
    no loss of per-agent toggle/last-fired/count on the Automation page) —
    this module only changes WHERE the channel list comes from, never the
    agent-row structure itself.
  - `agent.events`, when explicitly non-empty, still wins over a per-slug
    routing rule — this is what lets an operator-authored `ALERT ...,
    telegram` statement (which DOES compile a real `events` list, by
    design — the standalone ALERT statement is unaffected by this plan)
    keep working, and is also the one place a future operator UI edit to
    a *seeded* agent's channel list (should that feature ever reach
    `kind='event'` rows) would not be silently discarded by this table —
    same "explicit always wins over a default" convention used elsewhere
    in this codebase (e.g. an ntfy channel's own `priority` always beats
    a tier-derived default, see `event_agents.py:_send_channel`).
  - Every one of the 20 migrated `SEEDED_AGENTS` entries gets its own
    per-slug override row, verbatim-identical to its pre-migration
    `events` list — audited against the live DB (prod + dev) before
    writing this table, not assumed from the plan's "a handful of
    deviations" framing, which undersold how bespoke each agent's real
    channel set actually is (different channel SETS, different per-
    channel `priority`, `gate: False` vs capability-gated — see the
    per-slug table below). The generic tier defaults exist for a FUTURE
    seeded or operator-authored event agent that doesn't get its own
    override row, not because today's 20 reduce to a pure function of
    priority.
"""
from __future__ import annotations

from backend.shared.helpers.ramboq_logger import get_logger

_logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Per-slug overrides — one row per migrated seeded agent, verbatim-
# identical to that agent's pre-migration `SEEDED_AGENTS[...]["events"]`
# literal. This is the table `test_event_agents.py`'s resolver-coverage
# test pins byte-for-byte against the frozen pre-migration oracle.
# ─────────────────────────────────────────────────────────────────────
_PER_SLUG_OVERRIDES: dict[str, list[dict]] = {
    "fill-alert": [
        {"channel": "telegram", "enabled": True},
    ],
    "error-alert": [
        {"channel": "telegram", "enabled": True},
        {"channel": "ntfy", "enabled": True},
    ],
    "chase-cancel-alert": [
        {"channel": "telegram", "enabled": True},
        {"channel": "ntfy", "enabled": True, "priority": "urgent"},
    ],
    # 2026-10 channel-routing fix — "every alert goes to Telegram, with
    # no exceptions" (operator requirement #3): this agent used to be
    # ntfy-only. Telegram added; ntfy kept as-is.
    "partial-gtt-alert": [
        {"channel": "telegram", "enabled": True},
        {"channel": "ntfy", "enabled": True, "priority": "urgent"},
    ],
    "template-attach-urgent": [
        {"channel": "telegram", "enabled": True},
        {"channel": "ntfy", "enabled": True, "priority": "urgent"},
    ],
    # 2026-10 channel-routing fix — same as partial-gtt-alert above;
    # this agent used to be ntfy-only.
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
    "mcp-ping-alert": [
        {"channel": "telegram", "enabled": True, "gate": False},
    ],
    "order-correction-alert": [
        {"channel": "telegram", "enabled": True, "gate": False},
        {"channel": "ntfy", "enabled": True, "gate": False, "priority": "urgent"},
    ],
    # 2026-10 channel-routing fix — operator correction: deploy-sync
    # alerts reach "the telegram code deployment alerts channel alone"
    # — a DEDICATED Telegram bot/chat (`telegram_bot_token_deploy`/
    # `telegram_chat_id_deploy`, falling back to the general ops
    # bot/chat when absent — same fallback pair the standalone
    # `webhook/notify_deploy.py` script already uses for its own,
    # separate deploy-OK/FAIL pings), via the `telegram_deploy` channel
    # (`event_agents.py:_send_telegram_deploy_html`). Explicitly NOT the
    # generic `telegram` channel, NOT ntfy, NOT email — a deploy-sync
    # notice is a dev/ops concern, not a phone page, and must not share
    # the operator's main ops Telegram feed either.
    "deploy-sync-alert": [
        {"channel": "telegram_deploy", "enabled": True, "gate": False},
    ],
    # CRITICAL — do not delete this row. `BREACH_AGENT`'s real channels
    # are 100% dynamic (`out[4]` from `_render_breach`, read directly by
    # `_dispatch_agent` BEFORE this table is ever consulted for that
    # record — see event_agents.py). But `_dispatch_agent` resolves
    # `agent_channels` for EVERY active loaded agent up front, once per
    # dispatch call, regardless of whether any record matches that
    # agent's conditions — so without this row, `cycle-breach-alert`
    # (empty `events`, no `priority`) would fall through to the
    # catch-all on EVERY SINGLE `dispatch_rows()` flush, logging a fresh
    # WARNING each time via this module's own logger — which is NOT
    # skip-listed in `log_store.py`'s `_SKIP_PREFIXES`, so that WARNING
    # gets captured and re-enters the next flush, which resolves BREACH
    # again, logs again, forever. Confirmed via code trace, and via a
    # mutation test (temporarily removing this row and watching the
    # dedicated regression test fail) before this fix shipped. An
    # explicit `[]` here matches BREACH's real static shape exactly and
    # short-circuits at step 2 of `resolve_channels()`
    # before ever reaching the catch-all/logging branch.
    "cycle-breach-alert": [],
    "agent-alert-rich": [
        {"channel": "telegram", "enabled": True, "gate": False},
        # No hardcoded priority — this agent fans out EVERY other
        # agent's alerts (a generic multiplexer); ntfy priority is
        # resolved per-record from the REAL triggering agent's tracked
        # tier (`event_agents.py:_rec_tracked_ntfy_priority`), unchanged
        # by this migration.
        {"channel": "ntfy", "enabled": True, "gate": False},
        {"channel": "email", "enabled": True, "gate": False},
    ],
    "market-summary": [
        {"channel": "telegram_info", "enabled": True, "gate": False},
        {"channel": "email", "enabled": True, "gate": False},
    ],
    "gtt-not-accepted-alert": [
        {"channel": "ntfy", "enabled": True, "gate": False, "priority": "urgent"},
    ],
    # The 5 `generic_notice` agents (council item 2's shared table) —
    # channel sets verbatim from `event_agents._GENERIC_NOTICE_SPECS`
    # before this migration moved them here.
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
    "visitor-report-alert": [
        {"channel": "telegram_info", "enabled": True, "gate": False},
    ],
    # 2026-10 operator-reported gap — new-user self-registration alerts
    # were never reaching Telegram/ntfy (see `event_agents.py:
    # USER_REGISTERED_AGENT`'s own comment for the root cause). High,
    # not urgent: needs a look, not a 3x-repeat push.
    "user-registered-alert": [
        {"channel": "telegram", "enabled": True, "gate": False},
        {"channel": "ntfy", "enabled": True, "gate": False, "priority": "high"},
    ],
}


# ─────────────────────────────────────────────────────────────────────
# Generic priority-tier defaults — forward-looking fallback for a FUTURE
# seeded or operator-authored event agent with no per-slug override row
# above. No production agent today falls through to this tier (every one
# of the 20 has its own override), so this is deliberately conservative:
# no `gate: False` escape hatch, meaning a future agent landing here is
# still subject to the normal capability-enabled check
# (`event_agents._channel_enabled`) rather than inheriting the
# `gate: False` convention several of the more-audited recent agents
# above explicitly opted into.
# ─────────────────────────────────────────────────────────────────────
_TIER_DEFAULTS: dict[str, list[dict]] = {
    "critical": [
        {"channel": "telegram", "enabled": True},
        {"channel": "ntfy", "enabled": True, "priority": "urgent"},
        {"channel": "email", "enabled": True},
    ],
    "urgent": [
        {"channel": "telegram", "enabled": True},
        {"channel": "ntfy", "enabled": True, "priority": "urgent"},
    ],
    "high": [
        {"channel": "telegram", "enabled": True},
        {"channel": "ntfy", "enabled": True, "priority": "high"},
    ],
    "medium": [
        {"channel": "telegram", "enabled": True},
    ],
    "info": [
        {"channel": "telegram_info", "enabled": True},
    ],
    "low": [
        {"channel": "telegram_info", "enabled": True},
    ],
}


# ─────────────────────────────────────────────────────────────────────
# Mandatory catch-all — "nothing silently drops" (plan Design item 1 /
# Done-when). Deliberately the SAME channel shape `UNROUTED_ALERT_AGENT`
# (event_agents.py) already uses for its own, condition-level catch-all
# (a record matching NO agent's conditions at all) — this is a DIFFERENT,
# narrower case: an agent's conditions DID match and it DID render, but
# neither a per-slug override nor a recognised `priority` tier exists for
# it. This is now the SOLE source — `UNROUTED_ALERT_AGENT["events"]`
# (event_agents.py) is built directly from this constant at import time
# (`[dict(ch) for ch in CATCH_ALL_CHANNELS]`, a deep copy, never an
# aliased reference), not a hand-duplicated literal. The import runs in
# the direction `event_agents.py` → `alert_routing.py` (this module is
# imported BY event_agents.py, never the reverse), so building it here
# and reading it there carries no circular-import risk.
# `test_alert_routing.py` carries two checks, deliberately NOT just one:
# a value/identity check (equal content, but never the SAME list/dict
# objects — rules out an aliased reference) and a SEPARATE structural
# check that reads `event_agents.py`'s own source and confirms the
# `events` key is actually built via a comprehension over this constant,
# not a re-typed literal. The value/identity check alone cannot tell a
# derived copy apart from a hand-written literal that merely happens to
# match today — a hand-duplicated literal would pass it too, since it
# was never aliased to this constant either. Only the structural check
# is a real regression guard against a future refactor silently
# reintroducing a second, driftable copy.
# ─────────────────────────────────────────────────────────────────────
CATCH_ALL_CHANNELS: list[dict] = [
    {"channel": "telegram", "enabled": True, "gate": False},
    {"channel": "ntfy", "enabled": True, "priority": "high", "gate": False},
]

# Public — the set of `priority` tokens `_TIER_DEFAULTS` actually
# recognises. `event_agents.py:validate_seed_spec()` checks a seeded
# agent's own `priority` field against this, the same
# DSL-token-reuse-first convention `validate_seed_spec` already applies
# to `min_level`/renderer/channel names (never a second hardcoded
# vocabulary that could drift from this module's own table).
KNOWN_PRIORITIES: frozenset[str] = frozenset(_TIER_DEFAULTS.keys())


def resolve_channels(
    *, name: "str | None" = None, priority: "str | None" = None,
    level: "str | None" = None, tags: "list | None" = None,
    agent_events: "list | None" = None,
) -> list[dict]:
    """Resolve the channel-entry list a dispatched record should send to.

    Precedence (confirmed, do not reorder without a fresh operator
    decision — this exact order is what keeps a future operator edit to
    a seeded agent's own `events` from being silently discarded by this
    table, the same failure class `seed_spec_hash`/`modified_skip` exists
    to prevent for `conditions`/`actions`):

      1. `agent_events`, if non-empty — an explicitly-set `events` list
         (an operator-authored `ALERT ...` statement, or — in principle —
         an operator edit to a seeded row) always wins outright.
      2. A per-slug override in `_PER_SLUG_OVERRIDES`, keyed on `name`
         (the agent's own `slug`).
      3. A generic tier default in `_TIER_DEFAULTS`, keyed on `priority`.
      4. `CATCH_ALL_CHANNELS` — the mandatory last rule. Logged once at
         WARNING (this module's own logger, not a recursive alert) so an
         unmatched combination is visible without being silent.

    `level`/`tags` are accepted now (matching the plan's matcher surface
    — Alertmanager-style matchers key on more than just a severity label)
    but not yet consulted by any rule; no current agent's channel set
    depends on them. Kept as real parameters, not dropped, so a future
    per-tag or per-level rule can be added without changing every call
    site's signature.
    """
    if agent_events:
        return agent_events
    if name and name in _PER_SLUG_OVERRIDES:
        return _PER_SLUG_OVERRIDES[name]
    if priority and priority in _TIER_DEFAULTS:
        return _TIER_DEFAULTS[priority]
    _warn_catch_all_once(name, priority, level, tags)
    return CATCH_ALL_CHANNELS


# `_dispatch_agent` resolves every active loaded agent's channels once
# per `dispatch()` call, regardless of whether any record matches that
# agent's conditions (see `cycle-breach-alert`'s own comment in
# `_PER_SLUG_OVERRIDES` above for the confirmed incident this caused).
# A misconfigured FUTURE agent (unrecognised slug, unrecognised/absent
# priority) would otherwise log a fresh WARNING on every single flush
# forever, re-entering the next flush's own log batch. Second layer of
# defense behind the `cycle-breach-alert` fix above: warn at most ONCE
# per unique `(name, priority)` combination per process, not once per
# flush. Unbounded by design (the realistic cardinality is "number of
# distinct misconfigured agents," not "number of flushes") — if this
# ever needs an eviction policy, that's a sign the real fix is adding
# the missing per-slug override, not tuning this cache.
_WARNED_COMBOS: set[tuple] = set()


def _warn_catch_all_once(name, priority, level, tags) -> None:
    key = (name, priority)
    if key in _WARNED_COMBOS:
        return
    _WARNED_COMBOS.add(key)
    _logger.warning(
        "alert_routing: no rule matched name=%s priority=%s level=%s tags=%s — "
        "falling back to the catch-all channel set (further repeats of this "
        "exact name/priority combination are suppressed, not re-logged, for "
        "the rest of this process)",
        name, priority, level, tags,
    )
