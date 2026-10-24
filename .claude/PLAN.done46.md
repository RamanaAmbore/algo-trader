# Plan: Two independent initiatives — (1) generalize channel/priority decoupling for event agents, (2) calendar-driven broker/WebSocket connection lifecycle

This plan bundles two separate, unrelated changes into one document per operator
instruction — they touch different files, have no dependency on each other, and can be
implemented/sequenced independently. Part 1 is the event-agent alert work (council-
reviewed). Part 2 is the broker/WebSocket connection lifecycle work (confirmed directly
with the operator, not yet council-reviewed).

---

# Part 1: Generalize channel/priority decoupling for event agents — NOT a channel-keyed restructure

## Context

This follows directly from the DSL grammar unification that just shipped (commits through
`b8ae17ec`). During that work, and in the discussion afterward, the operator proposed a
further step: **"decouple agents from channels"** — collapse the 21 hardcoded alert
definitions in `backend/api/algo/event_agents.py`'s `SEEDED_AGENTS` into roughly 4
channel-keyed `WHEN...DO...` statements (one per channel: telegram, telegram_info, ntfy,
email), with producers emitting tokens (log level, urgency/priority, tags, source) and a
central routing table owning all channel selection — never a literal channel name in
producer/agent code.

**A 6-agent council (architect, risk, UX, perf, devil's advocate, external research) was
dispatched to pressure-test this before writing an implementation plan.** Five of six
reviews — including the one that formally "approved" — converged on the same structural
conclusion, independently, from different angles and different code citations:

- **Architect**: `_render_key(agent)` returns exactly ONE renderer per Agent row today —
  a channel-keyed agent would need ~15 different renderers depending on tag, which the
  engine doesn't support without rewriting `_dispatch_agent`'s core loop. Collapsing to
  channel rows also means the SAME condition (e.g. "wing unprotected") has to be repeated
  inside every channel-agent that should see it — the opposite of DRY, unless the routing
  table (not the Agent row) is the real source of truth, in which case the "4 agents" are
  a thin shell and today's per-tag model is still what matters. Recommendation: keep one
  Agent row per producer-tag; move only the channel/priority mapping into a small
  tag-keyed table extending the pattern `RICH_ALERT_AGENT` already uses.
- **Devil's advocate (BLOCK)**: the stated goal is **already substantially achieved in
  production** — producers already emit semantic tags/`alert_event` via `logger.warning(...,
  extra={"tags":[...], "alert_event": "..."})`, never a channel name; channel resolution
  already runs through a generic `CHANNELS` registry and a dynamic override layer
  (`_rec_tracked_ntfy_priority`, `_TIER_TO_NTFY_PRIORITY`, `_apply_action_required_ntfy_gate`)
  built additively on top of the 21 definitions, not by replacing them — and that layer has
  already fixed real incidents (the rich-alert priority bug, the chase ntfy gate). The
  "21→4" headline is misleading: 5 of the 21 already share one renderer (the only place
  collapse is free); the other 16 each own genuinely bespoke per-channel formatting
  (HTML vs plain vs email-tuple) that doesn't disappear under a routing table, it just
  loses its name and its incident-specific comment explaining why a given severity call
  was made. Also flags sequencing: `event_agents.py` absorbed four separate
  production-incident fixes in the 24–48h before this, plus the DSL unification itself
  (with its own P1 finding), all with zero production soak time.
- **UX (CONCERN)**: today, each of the 21 agents is a real, individually-named,
  individually-toggleable `Agent` DB row with its own last-fired timestamp and count,
  visible on the Automation page's "Alerts" card. Collapsing to ~4 generic rows collapses
  all of that per-type state into one aggregate per channel — the operator loses a working
  answer to "is the visitor-report alert currently enabled" and gains an opaque routing
  table with no decided edit/inspection surface. `RICH_ALERT_AGENT` is cited as live
  precedent for exactly this failure mode already biting the operator (the 3x-urgent-ntfy
  incident). Also surfaces two things worth fixing regardless of this plan's scope: (1) a
  hidden second gate layer (`_apply_action_required_ntfy_gate`) already overrides
  configured channels in code, so "what's configured" is already not "what fires" —
  any new table adds a third place to check unless a trace view covers both; (2)
  `seed_action()`/`SEED_VERSION` silently overwrites an operator's UI edit back to the
  hardcoded spec on the next version bump — a pre-existing bug.
- **Risk (CONCERN)**: `backend/tests/test_alert_golden.py` already exists and is an
  unusually thorough byte-for-byte/semantic golden-render test covering essentially all 21
  agents' exact output — it must stay green, unmodified, as the hard regression bar for
  any change here. Recommends keeping any superseded mechanism dormant-but-reversible
  rather than deleted in the same change, and a fail-loud guard (startup validator +
  runtime "unrouted alertable record" self-alert) so a missing/misnamed token at a future
  call site can never silently suppress a real alert.
- **External research (APPROVE)**: cites Prometheus Alertmanager's routing-tree model as
  the direct, mature precedent — but Alertmanager's own pattern is "**many** rules, each
  emitting labels, routed to a **few** receivers" — it does NOT collapse rule identity down
  to the receiver count. This actually *supports* the architect/devil/UX recommendation
  (keep per-tag rows, centralize only the channel/priority resolution), not the original
  "21→4 agent rows" framing. Also recommends two concrete mechanisms worth adopting
  regardless of scope: a non-null default/catch-all (never silently drop an unmatched
  alert-shaped record) and a synthetic "heartbeat" alert through the same pipeline to
  detect pipeline-wide breakage (dead-man's-switch).
- **Perf (APPROVE)**: no real perf motivation either way (today's cost is dominated by
  per-record `Context()` construction and network sends, not agent count) — but flags one
  precise technical landmine for whichever direction ships: any consolidated
  channel/priority resolution must stay on the existing `dispatch_rows`/`_dispatch_agent`
  event path. Routing it through `run_cycle`'s `_cycle_evaluate_agent`/`_V2_LATCH`
  machinery would wrongly pull cooldown/escalation state into discrete log-event
  dispatch — a repeated identical error within a cooldown window would get latched/
  suppressed instead of always alerting, which is incorrect for this class of event.

**Net: the original "collapse 21 agents into 4 channel-keyed WHEN...DO... statements"
design is not adopted.** The real, already-converged-on principle — "a producer/agent
should emit tokens, never a literal channel name" — is kept, because it's sound and
mostly already built. What changes is the *mechanism*: generalize the token-driven
channel/priority resolution that `RICH_ALERT_AGENT` already proves works, to the other
agents that still hardcode priority/channels directly, while preserving one named,
individually-toggleable Agent row per producer-tag. This is a narrower, safer, reversible
slice than what was originally discussed, consistent with the council's near-unanimous
finding and the devil's advocate's explicit sequencing concern (this file just had four
live-incident fixes land in the last two days; a system-wide restructure right now is the
wrong move regardless of the end-state's merits).

## Design

### 1. Keep one Agent row per producer-tag — do not restructure by channel

No change to `SEEDED_AGENTS`'s row-per-tag structure, `_age_set_status`'s per-slug
activate/deactivate, or the Automation page's per-row Alerts card (name, channel list,
last-fired, count, toggle). This preserves everything the UX and architect reviews
identified as load-bearing: per-event toggle granularity, named/legible identity, no
render-dispatch engine rewrite, no condition-duplication across channels.

### 2. Generalize the token-driven channel/priority resolver

Today, exactly one agent (`RICH_ALERT_AGENT`) resolves its ntfy priority from a token
(`tier`) threaded onto the log record's `extra`, via `_rec_tracked_ntfy_priority()` /
`_TIER_TO_NTFY_PRIORITY` — centralized, working, already fixed a real incident. The other
20 agents still hardcode their channel list and priority directly in their own
`SEEDED_AGENTS` dict literal (e.g. `CHASE_CANCEL_AGENT`'s `{"channel": "ntfy", "priority":
"urgent"}`).

Extend the SAME resolver mechanism so any agent can optionally omit a hardcoded `priority`
on a channel entry and instead have it resolved from the record's own token — reusing the
`log(priority=...)` DSL param that already ships (from the grammar-unification work) as
the producer-side mechanism for setting that token explicitly, falling back to the existing
tier-derived default when absent. This is additive — no existing agent's hardcoded
`priority` is removed or required to change; it becomes optional per channel entry, same
convention as every other "explicit wins, else fall back to today's default" decision made
during the grammar-unification work.

### 3. Collapse only the one place collapse is actually free: the 5 `generic_notice` agents

`GTT_ASYMMETRIC_AGENT`, `OCO_DOUBLE_FIRE_AGENT`, `TICKER_RECOVERED_AGENT`,
`TICKER_DEGRADED_AGENT`, `VISITOR_REPORT_AGENT` already share the identical renderer and
differ only by tag/channel/priority. Per the UX review's explicit guidance: **collapse the
mechanism, not the identity** — these stay 5 separately-named, separately-toggleable Agent
rows (no change to the Automation page, no loss of per-row last-fired/count/toggle), but
their `conditions`/`events` are sourced from one small shared declarative table (tag →
channel-set + priority) instead of 5 near-duplicate Python dict literals, so adding a 6th
`generic_notice`-style alert in the future is a table row, not a new ~15-line dict.

### 4. Fix the two findings that are real bugs regardless of this plan's scope

- **`SEED_VERSION` silently overwrites an operator's UI edit** on the next version bump
  (`seed_action()` in `event_agents.py`) — if an operator ever edits a seeded agent's
  channel list via the UI, it's silently reverted the next time `SEED_VERSION` changes.
  Fix: `seed_action()`'s upsert should not overwrite `events`/`conditions`/`actions` on a
  row the operator has modified since seeding — needs a modified-since-seed marker (e.g.
  compare against a stored hash of the seed spec, or a simple `operator_modified_at`
  timestamp set whenever `update_agent` touches a `kind='event'` row) before deciding
  whether to upsert.
- **A hidden second gate (`_apply_action_required_ntfy_gate`) already overrides configured
  channels in code**, invisible from the declarative definitions alone. Add a per-record
  dispatch trace (debug-level log line, or a small admin-visible field) stating: matched
  agent/tag → resolved channel-set and priority → any code-level gate override applied →
  final send/suppress decision. This directly serves the risk review's fail-loud
  requirement and the UX review's "why didn't I get notified" requirement in one
  mechanism.

### 5. Safety nets (risk + research findings, non-negotiable for this plan)

- **`test_alert_golden.py` must pass unmodified** (same assertions, same exact
  strings/channels/priorities) against every change in this plan. Any test that needs its
  *expected values* changed to pass is evidence of a regression, not a refactor.
- **A non-null default/catch-all** for any log record that looks alert-shaped (carries a
  `tags` list including a known alert-tag vocabulary member, at `WARNING`+ level) but
  matches zero agent conditions — emit one `WARNING`-level self-alert
  ("unrouted alertable record: tag=X level=Y") rather than silently dropping it. Mirrors
  the existing `[CRITICAL]`-log-once-per-process pattern already used for the
  postback-token-absent case.
- **A synthetic heartbeat**: a low-stakes, always-fires-on-schedule log record routed
  through the same dispatch path to a low-priority channel, so a pipeline-wide break (not
  a single agent's condition being wrong) is self-evident from its absence. Confirm
  whether the existing `market-status-report`/scheduled info-tier agents already serve
  this purpose before building a new one.
- **Stay on the existing `dispatch_rows`/`_dispatch_agent` event path** for all of the
  above — never route through `run_cycle`'s `_cycle_evaluate_agent`/`_V2_LATCH` machinery.

## Files

- `backend/api/algo/event_agents.py` — `SEEDED_AGENTS` (no row-count change), the 5
  `generic_notice` agents' condition/channel source moves to a small new table,
  `_rec_tracked_ntfy_priority()` generalized beyond `RICH_ALERT_AGENT`, new dispatch-trace
  logging, `seed_action()`'s modified-since-seed guard.
- `backend/api/routes/agents.py` — if the modified-since-seed marker needs a new column/
  field, same additive-migration convention as every other column added this week
  (`_migrate_agents_preamble`, `_migrate_agents_has_explicit_alert`).
- `backend/tests/test_alert_golden.py` — must stay green unmodified; add new tests for the
  5-agent shared-table sourcing, the generalized priority resolver, the catch-all
  self-alert, and the seed-overwrite fix.
- `docs/specs/ALERT_AGENTS_DESIGN.md` — document the generalized resolver pattern and the
  explicit decision NOT to restructure by channel, citing this council's findings so a
  future revisit starts from where this one landed rather than re-proposing the same
  channel-collapse.

---

# Part 2: Calendar-driven broker/WebSocket connection lifecycle

## Context

Today, the broker WebSocket connection and its health monitoring run on a pure clock
schedule, completely independent of whether the market will actually trade that day.
Confirmed directly in code: `_ds_tick_ticker_restart` (`background.py:3663`) restarts
KiteTicker at 08:00 IST **every single calendar day**, gated only on time-of-day, with no
weekday/holiday/special-session check at all. `_task_ticker_watchdog` runs every 30s
**continuously**, and `BrokerHealthBadge` polls broker auth health every 30s
**continuously** — both with zero market-open awareness. This means a genuinely closed
Saturday/holiday still connects the broker, maintains the WebSocket, and runs the 30s
watchdog heal-check all day, for no reason.

The operator's goal: stop connecting to the broker/WebSocket at all on a day nothing will
trade, while never missing a genuinely open day — including a same-day special session
(e.g. Muhurat trading) that wasn't known at the 05:30 calendar refresh. The design below
was worked out directly with the operator across several corrections; this is the final,
confirmed version.

## Design

**1. 05:30 calendar refresh — unchanged.** `_task_holiday_refresh` continues exactly as
today: fetches the holiday calendar and `market_special_sessions` data. No change to this
job itself.

**2. Cold-start catch-up for the calendar refresh.** On server start, at ANY time of day,
if no calendar record exists for today (fresh deploy, empty DB, or the 05:30 job hadn't
run/failed yet), immediately re-run the 05:30-equivalent refresh — don't wait for the next
scheduled 05:30.

**3. 08:00 connect, normal case.** If, by 08:00, the calendar (weekday + holiday +
`market_special_sessions`) says today is a trading day, and the server is already active,
connect exactly as today's `_ds_tick_ticker_restart` already does — no behavior change for
this case, only the new precondition check in front of it.

**4. Cold-start catch-up for the connect.** If the server starts AFTER 08:00 on a day the
calendar says is open, connect immediately on startup rather than waiting for the next
day's 08:00 tick.

**5. Closed-day recheck loop.** If the calendar says closed at the time of the morning
check, keep re-checking the DB (`market_special_sessions`, cheap — same data source
already fetched at 05:30, no live broker probe) for a market-open record appearing late —
continuously across the full existing active window, **08:00–23:30 IST** (MCX close), not
a short morning slice. If a record appears at any point in that window, pick up the normal
connect process from there, with no further delay. This closes the real gap in today's
`is_trading_day()`'s live-probe fallback, which can't fire before the 09:00–23:30 window
even opens, let alone before 08:00 — see the CLAUDE.md "Pre-open LTP pinning"/market-hours
entries for that existing mechanism's own documented limits.

**6. Never connect if closed all day.** If the calendar never shows an open record for
today (confirmed genuinely closed — weekend/holiday with no special session), never
connect to the broker/WebSocket at all for the rest of the day.

**7. New "idle" broker status.** `BrokerHealthBadge` (and whatever backend status it
reads) needs a new state — **idle** — distinct from connected/stale/failed, shown whenever
the connection was never attempted because the market is closed. Today's badge only models
states that assume a connection attempt happened (green/amber/red); it has no "not even
trying, by design" state yet.

**8. The 08:00–23:30 window itself is unchanged.** This is the SAME window
`_ds_tick_mcx_stop`'s existing MCX-close+1min teardown already uses — confirming today's
evening-disconnect timing stays exactly as-is. Nothing in this plan changes how long an
established connection stays up once connected; it only changes whether the system bothers
connecting in the first place, and adds the catch-up/recheck logic around that one
decision.

## Files

- `backend/api/background.py` — `_ds_tick_ticker_restart`/`_ds_tick_mcx_stop` gain the
  calendar precondition check (reusing `is_market_open()`/`_segment_is_open()` and
  `fetch_special_sessions_async`, already fetched data — no new data source); new
  cold-start catch-up checks at startup; new closed-day recheck loop scoped to 08:00–23:30.
- `backend/api/algo/broker_health.py` (or wherever `BrokerHealthBadge`'s backend status is
  computed/served) — new `idle` state.
- `frontend/src/lib/BrokerHealthBadge.svelte` (confirm exact path) — render the new `idle`
  state distinctly from connected/stale/failed.
- Tests: new coverage for the cold-start catch-up (both the calendar-refresh and the
  connect side), the closed-day recheck loop picking up a late-added special session, and
  the "never connects, shows idle" case for a confirmed-closed day.

## Open items before implementation (not yet resolved, flag before `/impl`)

- This part has NOT yet gone through a council review the way Part 1 did — given it
  touches the live broker connection (real-money relevant: missing the open could mean
  missing real fills/ticks), consider whether it needs the same risk-reviewer scrutiny
  before implementation, particularly around the fail-safe direction (should an uncertain/
  error state during the recheck loop default to "connect anyway" or "stay idle"?) — this
  wasn't explicitly re-confirmed after the original safety discussion earlier in this
  session, which leaned toward "fail open to connecting" as the safer default.
- Exact backend status field/table for `BrokerHealthBadge`'s new `idle` state not yet
  located/confirmed — needs a quick investigation pass before implementation.

---

## Agents

**Part 1** (event agents/alerts):
- **backend**: items 2–4 (generalize the resolver, collapse the 5 `generic_notice` agents
  onto a shared table, dispatch trace, `SEED_VERSION` overwrite fix) — one agent, same
  file family.
- **backend-test**: `test_alert_golden.py` must stay green unmodified; new tests per item
  5's safety nets.
- **doc**: `ALERT_AGENTS_DESIGN.md` update, after backend lands.

**Part 2** (connection lifecycle):
- **backend**: the calendar-gated connect/recheck/catch-up logic in `background.py`, the
  new `idle` status field — one agent, sequenced independently of Part 1 (no shared
  files).
- **frontend**: `BrokerHealthBadge.svelte`'s new `idle` state rendering.
- **backend-test**: cold-start catch-up, closed-day recheck, never-connects-shows-idle.

## Tests
- pytest: yes (Part 1 must include an unmodified-pass run of `test_alert_golden.py`; Part
  2 needs new coverage per its Files section)
- svelte-check: yes (Part 2's frontend badge change)
- playwright: no

## Commit message
Two separate commits, one per part:
- `refactor(alerts): generalize the token-driven channel/priority resolver beyond RICH_ALERT_AGENT, collapse the 5 generic_notice agents onto a shared table, fix SEED_VERSION overwriting operator edits, add a catch-all self-alert and dispatch trace — keep one Agent row per producer-tag, do not restructure by channel`
- `feat(background): gate broker/WebSocket connection on calendar + special-session check instead of connecting unconditionally every day, with cold-start catch-up and a closed-day recheck loop, new idle broker status`

## Done when

**Part 1:**
- `test_alert_golden.py` passes with zero changes to its expected values.
- The 5 `generic_notice` agents remain individually named/toggleable on the Automation
  page with unchanged last-fired/count behavior, while their trigger source is now one
  shared table.
- Any agent can optionally omit a hardcoded per-channel `priority` and have it resolved
  from the record's own token, with every currently-hardcoded agent unaffected if it
  doesn't opt in.
- A log record that looks alert-shaped but matches no agent produces a `WARNING`
  self-alert, not silence — proven by test.
- An operator's UI edit to a seeded agent survives the next `SEED_VERSION` bump — proven
  by test.
- A per-record dispatch trace is visible (log line or admin field) showing match →
  resolved channel/priority → any code-level override → final decision.

**Part 2:**
- A confirmed-closed day never connects to the broker/WebSocket; `BrokerHealthBadge` shows
  `idle`, not a stale/failed state.
- A confirmed-open day connects at 08:00 exactly as today, with no regression.
- A same-day special session added after 05:30 is picked up by the recheck loop within
  the 08:00–23:30 window, and the connect process fires from that point — proven by test
  simulating a late-appearing `market_special_sessions` row.
- A server restart after 08:00 on an open day connects immediately rather than waiting for
  the next day.
- A server restart with no calendar record for today re-runs the calendar refresh
  immediately rather than waiting for the next 05:30.

**Both parts:** pytest green, svelte-check 0 errors, CC gate clean.

---

## Explicitly not done (per the council's findings — revisit only with new evidence)

**The original "collapse 21 agents into ~4 channel-keyed `WHEN...DO...` statements"
design is not implemented.** Five of six council reviews, independently, found this would:
require a render-dispatch engine rewrite (one renderer per agent row today), duplicate
conditions across channel rows instead of sharing them, lose per-event toggle granularity
with no replacement decided, and touch a file that absorbed four live-incident fixes in
the prior 48 hours with zero soak time. Revisit only if a concrete design can show how it
avoids all four problems — not by assertion, by tracing the actual `_dispatch_agent`/
`_render_key` code the way this council did.

**Standalone `alert` statement's own retirement** (replacing its flat `alert log_tag=...`
syntax with an ordinary `WHEN...DO...` agent) is not part of this plan — it was coupled to
the channel-restructure proposal and has no independent justification surfaced by the
council. The standalone statement continues to work exactly as it does today.
