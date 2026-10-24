# Alert Agents: Design, Before and After

Status: implemented on `main` as of `9323f9d3`; gap status in section 5 is
current as of `e37fab01` (G14 fully fixed). Source of truth for the alert
pipeline. Companion to `docs/proposals/AGENT_RULES_PROPOSAL.md`, which holds
the wider agent grammar roadmap.

## 1. Purpose

Every operator-facing alert (Telegram, ntfy, email) should come from one
place, be configured as data, and be extendable without touching the code
that produces the event. Order automation will later reuse the same
mechanism.

## 2. Before

Alerts were sent directly from the code that detected the event.

| Area | How it worked |
|---|---|
| Error alerts | `ErrorAlertHandler` on the logging queue, with its own repeat gate (3 in 15 min, 15 min cooldown, `alert_now` bypass). Sent from its own thread. |
| Fills | `fill_notify.notify_fills` called the sender directly after each FILLED transition. |
| Order failures | `send_order_failure_alert` called from ~8 sites. Redis cooldown (`ramboq:order_alert:*`) and an in-process fallback. Telegram, ntfy, email. |
| Template attach | Direct `_alert_route` and `send_ntfy_alert` calls from `template_attach.py` and `orders_place.py`. |
| Chase | Direct ntfy in `chase.py` for cancel-unconfirmed. |
| Rich agent alerts | `_v2_send_rich_alert` built rows, then called `_dispatch` in a thread. |
| Summaries | `send_summary` called `_dispatch` directly. Callers in `background.py`, `actions_live.py`, `actions.py`. |
| MCP audit pings | Direct `_send_telegram` calls in `lab.py` (six sites). |
| Routing | `alert_routing` table in `backend_config.yaml` mapped each event key to telegram (ops/info), ntfy priority, and email flag. |
| Cycle agents | `agent_engine` evaluated agents each tick, then `events.dispatch` sent channels directly (telegram, email, ntfy, websocket, inapp, log). |

Problems with this layout: formatting and routing were duplicated per site,
each site chose its own cooldown, and adding an alert meant editing the
sender code.

## 3. After

Every alert is now a **tagged log record** that a **seeded event agent**
matches and sends. The producer decides what happened. The agent decides who
gets told and how.

```
producer --logger.<level>(msg, extra={tags, event, ...})--> LogRecord
   |                                                          |
   |   QueueHandler (tags origin via OriginFilter)            |
   v                                                          v
log_store.LogStoreHandler (bounded queue, wakes writer)   [file/console]
   |
   v
writer task (batches, 2s or on wake)
   |-- insert log_events (INFO+, 7-day retention)
   |-- event_agents.dispatch_rows (API and conn; main branch only)
          |-- match agent.conditions (log leaf)
          |-- repeat gate (if action.gate)
          |-- render (registered renderer by name)
          |-- channels (telegram, telegram_info, ntfy, email)
```

### 3.1 Components

| Component | File | Role |
|---|---|---|
| Log store | `backend/shared/helpers/log_store.py` | Tag rules, row shape, bounded queue, batch writer, retention, shutdown drain, dropped-record report, origin stamping |
| Origin | same file, `ORIGIN_BRANCH`, `OriginFilter` | Carries the caller's branch into the record |
| Log feed | `backend/api/algo/log_feed.py` | High-water cursor for the cycle engine's log leaf |
| Event agents | `backend/api/algo/event_agents.py` | Seeded agents, renderers, channel senders, dispatch |
| Repeat gate | `backend/shared/helpers/error_alerts.py` | `RepeatGate` (local) and `SharedRepeatGate` (Redis) |
| Message builders | `backend/shared/helpers/alert_utils.py` | `dispatch_payload`, `_email_message`, `order_failure_messages` (pure) |
| Tag catalog | `backend/api/algo/grammar.py`, `LOG_TAG_TOKENS` | Tags agents may match, seeded into `grammar_tokens` |
| Storage | `backend/api/models.py` `LogEvent`; migrations in `database.py` | `log_events` table, GIN index on `tags` |

### 3.2 Record contract

A record is written with `logger.<level>(message, extra={...})`.

- `tags`: list of owner tags. The level tag is always added, and the logger's
  last name is added when `tags` is absent. Tags must exist in the catalog;
  unknown tags are stored and reported once on stderr.
- `event`: the match key used by agents (for example `filled`, `order_failure`,
  `rich_alert`, `summary`).
- Other fields: the data a renderer needs. Stored in `extra` (JSON).

Stored rows: `ts`, `process` (`api` or `conn`), `level`, `logger`,
`message`, `tags`, `extra`. Retention and minimum level are settings
(`log.retention_days`, `log.db_min_level`).

### 3.2a Order-failure alert origin labels (2026-10)

Order-failure alerts (`order_failure` records) include an origin label in
Telegram body, ntfy text, email subject, and email HTML Origin row. Label
is determined by `_classify_order_origin_label(source, agent_id)` at record
time and survives log→event-agent replay via `agent_id` in `extra` dict.

| Condition | Label | Meaning |
|---|---|---|
| `agent_id is None` + `source not in {"template_wing", "template_wing_chase"}` | Manual | Operator-placed order (no agent) |
| `agent_id is None` + `source in {"template_wing", "template_wing_chase"}` | Manual Bracket | Template/Bracket exit on operator-placed parent |
| `agent_id is not None` + `source not in {"template_wing", "template_wing_chase"}` | Agent | Agent-fired order |
| `agent_id is not None` + `source in {"template_wing", "template_wing_chase"}` | Agent Bracket | Bracket exit on agent-fired parent |

**Threading**: `agent_id` parameter passes through `send_order_failure_alert()`
kwarg (call sites: chase.py, template_attach.py, order_hold_gate.py,
ExpiryEngine, background.py recovery sweep) and reaches the renderer via
`event_agents._render_order_failure()` reading it back from the replayed
record's `extra` dict.

### 3.3 Event agents (seeded)

Defined as constants in `event_agents.py` and upserted at API startup.

| Slug | Match (`tag`, level, `event`) | Renderer | Channels |
|---|---|---|---|
| `fill-alert` | orders, INFO, filled, `mode=live` | fill | telegram, ntfy |
| `error-alert` | error, ERROR, any | error (gate) | telegram, ntfy |
| `chase-cancel-alert` | chase, CRITICAL, cancel_unconfirmed | chase_cancel | ntfy urgent |
| `partial-gtt-alert` | gtt, CRITICAL, partial_gtt | partial_gtt | ntfy urgent |
| `template-attach-urgent` | gtt, any of wing_unprotected, wing_hard_reject | template_attach | ntfy urgent |
| `template-attach-high` | gtt, any of wing_skip, wing_offset_skip | template_attach | ntfy high |
| `order-failure-alert` | orders, WARNING, order_failure | order_failure | telegram, ntfy urgent, email |
| `template-guard-alert` | orders, INFO, template_guard | template_guard | telegram, ntfy high |
| `template-attach-fail-alert` | orders, WARNING, template_attach_fail | template_attach_fail | telegram, ntfy urgent |
| `mcp-ping-alert` | mcp, INFO, mcp_ping | mcp_ping | telegram |
| `deploy-sync-alert` | deploy, WARNING, deploy_out_of_sync | deploy_sync | none (2026-10, see 3.14; was ntfy high) |
| `agent-alert-rich` | agent, INFO, rich_alert | rich_alert | telegram, ntfy (dynamic priority), email |
| `market-summary` | summary, INFO, summary | summary | telegram_info, email |

Channel entries may set `priority` (ntfy) and `gate` (default true). `gate:
false` skips the capability flag check, to keep the same behaviour as the
direct code it replaced.

**`agent-alert-rich`'s ntfy priority is dynamic, not a static table entry
(2026-10-16 fix)** — every other row's `ntfy urgent`/`ntfy high` above IS a
static per-channel priority, correct for that one specific event. This row
is different: `agent-alert-rich` is a generic multiplexer that renders
EVERY cycle agent's alert (any tier), so a static priority here would be
wrong for most of them. Before this fix it was hardcoded `ntfy urgent`
unconditionally — since `send_ntfy_alert` sends an urgent-priority push 3x
in a row, every single cycle-agent alert (including routine info-tier
scheduled notifications like `market-open-nse`) got that treatment. Live
incident: operator saw 3 ntfy pushes for one "NSE market open" alert. Fix:
`_v2_send_rich_alert` (`agent_engine.py`) now threads the REAL triggering
agent's `tier` into the log record's `extra["tier"]`; `agent-alert-rich`'s
own channel entry carries no priority at all; `event_agents.py:
_dispatch_agent()` resolves priority per-record from that tracked tier via
`_TIER_TO_NTFY_PRIORITY` (`critical→urgent, high→high, medium→default,
low→low, info→low`), falling back to the old `agent.tier`-based resolution
only when a record carries no tier (e.g. any other renderer, none of which
are generic multiplexers).

### 3.4 Channels

| Key | Sender | Capability |
|---|---|---|
| `telegram` | `alert_utils._send_telegram` (ops) | `telegram` unless `gate: false` |
| `telegram_info` | `alert_utils._send_telegram_info` | none (gate off) |
| `ntfy` | `alert_utils.send_ntfy_alert` with priority | `ntfy` unless `gate: false` |
| `email` | `mail_utils.send_email` to `get_alert_recipients()` | none (gate off) |

### 3.5 Repeat gate

Only the error agent uses a gate (`actions: [{"render": ..., "gate": true}]`).
Rule: alert when the same message repeats more than 3 times in 15 minutes, or
when the record sets `alert_now`. Then suppress for 15 minutes per message and
report the suppressed count on the next alert.

`SharedRepeatGate` keeps the same rule in Redis (`ramboq:err_gate:*`). The
window is fixed rather than sliding. If Redis is unavailable, `RepeatGate`
(in-process) is used.

### 3.6 Origin and branch guard

- Event dispatch runs only when `deploy_branch == main`. Dev never sends.
- Conn requests carry `X-Ramboq-Branch`. The conn service stamps it on each
  record as `origin`. Records whose origin is not `main` are dropped before
  dispatch. The header default is `main` (see gap G6).

### 3.7 Cycle agents (unchanged)

Breach rules that need hysteresis, cooldown, or schedules stay in
`agent_engine`. They use `events.dispatch` for their channels. A log leaf
(`{"log": {...}}`) can be used in cycle conditions, fed by `log_feed`, which
keeps an in-memory high-water mark.

### 3.8 DEBUG exemption, unified grammar, and explicit-alert mode (2026-10-23)

**DEBUG exemption (invariant).** `event_agents.py:_dispatch_agent` drops any log record whose level
is `DEBUG` before it evaluates the agent's conditions. This is structural: it applies to every
`kind='event'` agent, seeded or operator-authored, and no per-agent list is involved. A DEBUG record
cannot reach a notification channel. `help(<name>)` is the canonical DEBUG-tier producer. It logs at
DEBUG and is never alert-eligible. A bare `debug(x)` is the same tier. `log(...)` at any other level
is not exempt, and it can carry a `priority` argument that a matching event agent uses as its
ntfy priority override.

**Unified grammar framing.** The Console grammar has no `ALERT` clause keyword and no `GIVEN`
keyword. `alert(...)` is an ordinary call, so an alert channel list is written the same way
wherever a call is allowed. There is no distinct agent-clause syntax: a statement is a preamble of
calls, with an optional `WHEN … DO …` pair wrapping the shared vocabulary. The compiler still tags a
compiled WHEN/DO statement's output as `kind:'agent'`, so that tag is a compiled-shape detail, not a
separate statement form. The standalone event statement (`ALERT log_tag=…`, which subscribes to the
log stream) is the only remaining `ALERT`-led form, and it is unchanged.

**Explicit-alert mode (item 0.C).** A cycle agent's `has_explicit_alert` flag is true only when
its `DO` clause contains a real `alert(...)` call. When the flag is true and `events` is non-empty,
the breach path in `agent_engine.py` skips `_v2_send_rich_alert()`. Delivery then goes through the
normal `events.dispatch()` path, with a generic body or the operator's `msg=` text. A preamble
`alert(...)` does not set the flag. It is an unconditional notice, rate-limited by `cooldown_minutes`.

Round-trip caveat. The decompiler renders any populated `events` list as `DO alert(...)`. Without a
guard, saving a seed-style agent (`has_explicit_alert=false` with a populated `events`) from the
Console would silently switch it to explicit mode. `updateConsoleAgent` therefore keeps the flag
sticky-false: it sends `true` only when the loaded row was already `true`. Consequence: an existing
seed or form-built agent cannot be converted to explicit mode through the Console. A dedicated
convert action would be a separate feature. Details are in
[CLAUDE.md](../../CLAUDE.md), entry "`has_explicit_alert`".

### 3.9 One Agent row per producer tag: the collapse was rejected (2026-10)

Landed on `workshop` in `65de9319` and `5db3f7a8`. The file holds 21 seeded agents
(`SEEDED_AGENTS`,
[event_agents.py:1223](../../backend/api/algo/event_agents.py#L1223)). The table in 3.3 is not
exhaustive.

A 6-agent council reviewed a proposal to collapse the 21 agents into 4 channel-keyed agents, and
rejected it. Findings, so a revisit starts here:

- **The engine maps one renderer per row.** `_render_key(agent)`
  ([event_agents.py:383](../../backend/api/algo/event_agents.py#L383)) reads the single `render`
  action on an Agent row. A channel-keyed row carries ~15 tags, and each tag needs its own renderer
  because the message text differs. Supporting that means an engine rewrite, not a data change.
- **Conditions would be duplicated.** Each channel agent would need every tag's condition. One
  condition change would then mean N edits, which is the drift this design exists to prevent.
- **Per-event toggle would be lost.** Each of the 21 agents is activated or deactivated on its own
  on the Automation page. A channel row turns off every event on that channel at once.
- **No soak time.** The file had just absorbed four production-incident fixes: rich-alert priority,
  the action-required ntfy gate, the ERROR self-alert gate, and the DEBUG exemption. A structural
  rewrite on top would make any regression hard to attribute.

The adopted alternative keeps one row per producer tag. Two changes below reduce duplication
without touching row identity: the shared `generic_notice` table (3.10) and the token-driven
priority resolver (3.11).

### 3.10 Shared table for the five `generic_notice` agents

`GTT_ASYMMETRIC`, `OCO_DOUBLE_FIRE`, `TICKER_RECOVERED`, `TICKER_DEGRADED`, and `VISITOR_REPORT`
share one renderer (`generic_notice`). Their specs now come from `_GENERIC_NOTICE_SPECS`
([event_agents.py:1107](../../backend/api/algo/event_agents.py#L1107)) through
`_build_generic_notice_agent()` ([:1138](../../backend/api/algo/event_agents.py#L1138)).

- Output is byte-identical to the five former literals. A regression test pins each dict.
- Identity is unchanged: five slugs, five names, each separately toggleable.
- A sixth notice of this kind is one table row.
- Channel priorities stay static per row (`high` or `urgent`). These are single-event agents, so a
  static priority is correct. Contrast with `agent-alert-rich` (3.11).

### 3.11 Token-driven ntfy priority for every event agent

Shipped earlier in `cbfa32e2`. The 2026-10 round added regression coverage only, and no production
code. The resolution order, applied per record and per channel, is:

1. The channel's own `priority` in its `events` entry. This always wins.
2. The record's `extra.priority`, set by `log(priority=...)`.
3. The record's `extra.tier`, mapped through `_TIER_TO_NTFY_PRIORITY`
   ([alert_utils.py:1281](../../backend/shared/helpers/alert_utils.py#L1281)).
4. The dispatching agent's own `tier`, in `_send_channel`
   ([event_agents.py:501](../../backend/api/algo/event_agents.py#L501)).
5. Nothing. The kwarg is omitted, and `send_ntfy_alert` applies its clock default.

Steps 2 and 3 are applied by `_rec_tracked_ntfy_priority` and
`_channel_with_tracked_priority`
([event_agents.py:638](../../backend/api/algo/event_agents.py#L638)). The action-required gate
(`_apply_action_required_ntfy_gate`) runs after them. It can still force `urgent` or drop the ntfy
channel entirely.

Invariant: a generic multiplexer (one that renders other agents' records) never uses its own
tier for a per-record notification. Coverage: `test_event_agents.py`, and
`test_notification_header_and_priority.py::TestTierToNtfyPriorityMapping`.

### 3.12 `SEED_VERSION` no longer overwrites an operator edit

Before, a `SEED_VERSION` bump rewrote `conditions`, `events`, and `actions` on every row whose
`seed_version` was behind, including rows an operator had edited on the Automation page
(`update_agent()` never bumps `seed_version`). The fix has three parts:

- **Column.** `agents.seed_spec_hash` (`VARCHAR(64)`, nullable, no default) in
  [models.py](../../backend/api/models.py). Migration `_migrate_agents_seed_spec_hash()` in
  [database.py](../../backend/api/database.py), idempotent, runs after the `has_explicit_alert`
  migration.
- **Hash.** `compute_seed_spec_hash()` returns the sha256 of the canonical JSON of
  `(conditions, events, actions)`, with sorted keys. Seeding stores the hash of what it actually
  wrote.
- **Decision.** `seed_action(row)` returns one of:
  - `insert`: no row.
  - `keep`: `seed_version` is at or above `SEED_VERSION`.
  - `modified_skip`: the version is behind, and the row's current content no longer hashes to
    `seed_spec_hash`. The content write is skipped. `seed_version` is still bumped, so the skip is
    logged once per code version, and `seed_spec_hash` keeps the original write's hash so a later
    bump still detects the same edit.
  - `update`: content matches the stored hash, or the row has no hash (legacy).

Caveats:

- **One-time legacy overwrite.** Rows seeded before this column have a `NULL` hash. The first
  `SEED_VERSION` bump after this lands overwrites them once, as before.
- **Operator-edited rows stay on the old spec.** To pick up a changed code spec for such a row, the
  operator must revert the edit or delete the row (a missing row yields `insert`). No UI path for
  this is built.

### 3.13 Dispatch trace and the unrouted-record catch-all

**Dispatch trace.** `_dispatch_trace()`
([event_agents.py:520](../../backend/api/algo/event_agents.py#L520)) writes one DEBUG line per
channel per matched record:
`dispatch-trace agent=<slug> tag=<tag> channel=<name> priority=<p> decision=<d>`.

| Decision | Meaning |
|---|---|
| `sent` | Channel delivered |
| `send_failed` | Sender raised; stderr line also written |
| `*_gate_override_applied` | Suffix on `sent` or `send_failed`: the action-required gate forced urgent |
| `suppressed_log_only` | Branch or sim gate; only the `log` channel passes |
| `suppressed_action_required_gate` | The gate dropped the ntfy channel (`action_required=false`) |

The trace is DEBUG-only, so it costs nothing when DEBUG is off.

**Unrouted-record catch-all.** After dispatch, `_run_unrouted_catch_all()`
([event_agents.py:861](../../backend/api/algo/event_agents.py#L861)) checks each row. A row that is
WARNING or higher and carries a tag from `LOG_TAG_TOKENS`, but matches no active event agent's
conditions, gets one WARNING:
`unrouted alertable record: tag=… level=… logger=… alert_event=… message=…`.

- **Fixed (commit `01fa2c10`) — this now reaches a real channel.** The first version logged this
  WARNING through `event_agents.py`'s own module logger, which `log_store._SKIP_PREFIXES`
  ([log_store.py:27](../../backend/shared/helpers/log_store.py#L27)) excludes from
  `LogStoreHandler` capture entirely. That gave recursion-safety but meant the self-alert could
  never reach `log_events`/`dispatch_rows` — stderr/file/console only, never a real Telegram/ntfy
  push, never visible in the LogPanel. That defeated the entire point of a fail-loud guard. Fixed:
  `_emit_unrouted_self_alert()` now logs through `_EVENTS_LOGGER`
  (`"backend.api.algo.events"`, NOT skip-listed — the same logger the pre-existing `log` channel
  action already uses), tagged `["agent"]` with a reserved `alert_event="unrouted_alert"`. A new
  seeded agent, `UNROUTED_ALERT_AGENT` (the 14th gated agent, bypasses the dev-capability toggle
  like the other safety-critical agents), matches that exact shape and delivers to telegram + ntfy
  at `priority="high"`, reusing the existing `generic_notice` renderer.
- **Recursion-safety is now a direct, tested condition exclusion, not a logger-name side
  effect.** `_looks_alert_shaped()` explicitly excludes any record carrying
  `alert_event == "unrouted_alert"` from being classified as alert-shaped at all — independent of
  whether `UNROUTED_ALERT_AGENT` is active, deleted, or ever fails to match for some other reason.
  `test_unrouted_catch_all_self_alert_record_cannot_trigger_a_second_self_alert` feeds the
  self-alert's own record shape back through `dispatch_rows()` and confirms
  `_emit_unrouted_self_alert` is never called a second time — the property is tested directly, not
  inferred from where the log call happens to live.
- **Companion fix: `SEED_VERSION` bumped 1 → 2.** Needed so an already-seeded `deploy-sync-alert`
  row (see §3.14's ntfy removal) actually picks up that change via `seed_action()` — without the
  bump, a row already at `seed_version=1` would read `>= current_version` forever and never update.
- **Condition matching only.** A record that matched an agent and was then suppressed by the
  log-only or action-required gate is routed, not unrouted.
- **Empty agent list.** If `load_agents()` returns nothing or fails, every alert-shaped row in the
  batch is flagged. That is the intended signal for a pipeline-wide break.

### 3.14 `DEPLOY_SYNC_AGENT` no longer sends to ntfy

Operator instruction, 2026-10: a deploy-out-of-sync notice is a developer concern and should not
page a phone. `DEPLOY_SYNC_AGENT` now has `events: []`
([event_agents.py:1029](../../backend/api/algo/event_agents.py#L1029)). No replacement channel was
requested.

- Its condition is unchanged. It still matches, and its `deploy_sync` renderer still runs.
- With no channel, the dispatch loop never runs, so no dispatch-trace line is written for it. A
  match is visible only in the log feed.
- Adding a channel later is a one-line change to `events`. No new mechanism is needed.
- Golden test: `test_alert_golden.py` pins `events == []`.

## 4. What changed, by file

| File | Change |
|---|---|
| `log_store.py` | New. Tag and row rules, bounded queue, writer, retention, shutdown drain, dropped report, origin |
| `ramboq_logger.py` | Attaches the log-store handler and the origin filter |
| `event_agents.py` | New. Agents, renderers, channels, gate wiring, dispatch |
| `error_alerts.py` | `ErrorAlertHandler` removed. `RepeatGate` and `SharedRepeatGate` |
| `alert_utils.py` | `dispatch_payload`, `_email_message`, `_send_email_to_recipients` split out of `_dispatch`. `order_failure_messages`. Summary and order failure record instead of send |
| `fill_notify.py` | Records instead of sends |
| `chase.py`, `template_attach.py`, `orders_place.py` | Record instead of send |
| `agent_engine.py` | Rich alert records instead of `_dispatch` |
| `background.py` | Deploy-sync and summary record. Cross-loop market save |
| `lab.py` | MCP pings record |
| `brokers/service/app.py`, `brokers/client/remote_broker.py` | Origin header and hook |
| `grammar.py` | Tag catalog extended (`agent`, `mcp`, `deploy`, `summary`, plus earlier tags) |
| `models.py`, `database.py` | `log_events`, `agents.schema_version`, `agents.kind`, `grammar_tokens.source` |
| `webhook/deploy.sh` | Long children no longer hold the deploy lock |
| `event_agents.py` (2026-10, `65de9319`, `5db3f7a8`) | `_GENERIC_NOTICE_SPECS` shared table. `compute_seed_spec_hash` and `seed_action` `modified_skip`. Dispatch trace and unrouted catch-all. `DEPLOY_SYNC_AGENT` events cleared |
| `models.py`, `database.py` (2026-10) | `agents.seed_spec_hash` column and idempotent migration |

## 5. Gaps

Each gap lists the risk, then a proposed fix. Priority: H high, M medium, L low.

**G1 (H). Seeding overwrites operator edits. [FIXED]** `seed_event_agents` rewrites
`conditions`, `events`, and `actions` on every startup. Any change made in the
database is lost on restart. Fix: insert on first run only, and keep a
`seed_version` column so code changes are applied explicitly.

**G2 (H). Two sources of channel truth. [FIXED: the `alert_routing` table, `_dispatch`, and `_alert_route` are removed; agent rows are the only source for these sends]** The `alert_routing` table in
`backend_config.yaml` and the `events` lists in the seeded agents can disagree.
Fix: make the agent row the only source, and remove the routing table once
every site is migrated.

**G3 (H). Event agents cannot trigger orders.** Event agents have no
`actions` execution path. Cycle agents execute actions (`place_order`,
`close_position`, `expiry_auto_close`, `chase_close`). Automation on events
(for example a fill starting a template) would need a guarded action path.
Fix: see section 6.

**G4 (M). Cycle breaches still use the old dispatch. [FIXED: telegram, email, and ntfy are recorded as `breach` records and sent by the event path; websocket, in-app, and log channels stay direct]** `events.dispatch`
sends cycle-agent channels directly. These do not go through log records or
the event renderers, so two channel implementations exist. Fix: move cycle
channels onto records after latch and cooldown are decided.

**G5 (M). Dropped records lose their alerts. [FIXED: alert records have their own queue, drained first, and their drops are counted separately; a burst of routine logs cannot push an alert out]** When the log queue is full,
the record is dropped and its alert is not sent. The drop is now reported on
stderr, but nothing retries or alerts on it. Fix: give alert records their
own unbounded or priority queue, separate from the storage queue.

**G6 (M). Origin header defaults to `main`. [FIXED: default is `unknown`, which is dropped]** A conn caller that omits
`X-Ramboq-Branch` is treated as prod. Fix: default to `unknown` and drop
unknown-origin records from dispatch, after every caller sends the header.

**G7 (M). Cycle log-leaf cursor is in memory. [FIXED: the high-water mark is kept in Redis and resumed after a restart, with a ten-minute lookback]** `log_feed` starts at the newest
row after a restart, so cycle agents with a log leaf miss records written
during the restart. Fix: persist the high-water mark, with a bounded replay
window, so restarts do not resend old alerts.

**G8 (M). Event agents ignore sim mode. [FIXED: sim records follow `simulator.notify_during_run`]** `simulator.notify_during_run` is
honoured by the rich path, but event agents do not check it. Simulated fills
are excluded by `mode=live`. Simulated errors and summaries can still send.
Fix: tag sim records (`sim_mode`) and add a sim gate to dispatch.

**G9 (M). Email recipients are one list. [PARTLY: an email channel entry may set `recipients` to `"alert"` (default) or to a list of addresses. Partner, general-partner, and per-user scopes need the admin resolver moved to a shared helper, which is not done]** All email goes to
`get_alert_recipients()`. Partner, general-partner, and per-user targets
(from the proposal) are not built. Fix: add a recipient scope to the email
channel entry, resolved by a shared resolver.

**G10 (M). Renderers and channel shapes are Python. [PARTLY: seed-time check against registered renderers and channels]** A new alert needs a
renderer in code. Only the agent rows are data. Fix: keep renderers as code,
but allow a row to choose from registered renderers only, and validate that at
seed time.

**G11 (M). Agents are not validated at seed time. [FIXED: seed refuses specs with unknown tags, renderers, or channels]** A seeded condition that
names an uncatalogued tag is accepted and never matches. Fix: validate the
condition tree and tags when seeding, and log an error for any failure.

**G12 (L). `event` key is overloaded. [FIXED: alert records use `alert_event`; WebSocket payloads keep `event`]** WebSocket payloads in `routes/orders.py`
and `routes/quote.py` use `"event"` too. They are not log records, so there is
no runtime collision, but the name is confusing. Fix: rename the record key to
`alert_event`, or document the split.

**G13 (L). Large bodies in `extra`. [FIXED: rendered bodies travel with the row for dispatch and are dropped before the insert, so `log_events` keeps only the inputs]** Summary and rich-alert records store
the full HTML table. Storage grows with each alert and is capped only by the
7-day retention. Fix: store the inputs and rebuild the table in the renderer,
as the order-failure path does.

**G14 (L). No UI for event agents. [FULLY FIXED (e37fab01)]** The automation
editor now supports full CRUD for event agents: "+ New Agent" entry point
with Kind selector (Threshold / Notification); dedicated builder with
renderer picker (from `GET /api/agents/renderers`), condition fields (log
tag / min-level / where), channel checklist using `EVENT_CHANNELS` vocabulary,
priority, and gate. Existing event agents are editable: channel-enabled /
priority / gate stay mutable; renderer / condition / slug stay fixed
post-creation (same lifecycle as seeded agents). Threshold agents retain their
existing inline JSON editor.

**G15 (L). Browser and live verification pending. [FIXED for the code path: `scripts/alert_smoke.py` validates every seeded agent and renders a sample through its renderer, sending nothing. Browser and live checks remain]** Golden tests pin the text.
Delivery has been confirmed only on prod for real events that have happened.
Fix: a scripted check that writes a test record on dev with channels stubbed
and asserts the rendered output.

## 6. Extension to order automation (design, not built)

Goal: an event agent can start an order action (for example, a fill starts a
template exit, or a tagged error triggers a close) with the same guards that
cycle agents already use.

Proposed shape:

1. An event agent may have an `actions` list of order actions drawn from the
   existing action registry (`grammar_tokens`, `action_type`). Action types
   are the ones cycle agents already run. No new order code.
2. Each action runs only when:
   - the agent's `trade_mode` is `live` and the record's `mode` is `live`
     (the same check as fills);
   - the global kill switch and paper switch allow it;
   - the per-rule guards pass (orders per day, quantity, notional);
   - an idempotency key `(agent_id, record_id, action_index)` has not already
     run (stored in `agent_events`).
3. Actions run in the writer's dispatch, off the logging thread, through the
   same `asyncio.to_thread` path as sends.
4. Each action writes an `agent_events` row and a tagged `agent` record, so the
   audit trail is the same as for alerts.
5. Failed actions emit an `order_failure` record, which reuses the existing
   alert path.

Open questions (need a decision before building):
- Should event-triggered orders require a confirm step, as the proposal says
  for live orders created from rules?
- Should an event agent be allowed to fire more than once for the same
  record? (Proposed: no, idempotency key.)
- Which record types may trigger orders? (Proposed: only `fill` and
  `breach`-class records, not renderer-only records.)

## 7. Operational notes

- Deploys: dev deploys on push to `dev` or `workshop`. Prod deploys on push
  to `main`. Both share one deploy lock.
- Tag catalog changes require a restart so `grammar_tokens` is re-seeded.
- The writer flushes every 2 seconds or on wake. Shutdown drains the queue.
- No secrets appear in this document. Redis, SMTP, and Telegram settings live
  in `secrets.yaml`.
