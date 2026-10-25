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

*Amended 2026-10-24.* The Channels column above is historical. The live channel set for each
seeded agent is resolved by `alert_routing.py` (§3.15). The table also omits
`user-registered-alert` (§3.16). `SEEDED_AGENTS` now has 23 entries. `alert_routing.py` has 22
per-slug rows, and `unrouted-alert-catch-all` is the one entry without one.

*Amended 2026-10-25 (commit `17a1c85a`).* The `deploy-sync-alert` row in the table above
(`none`) is superseded. It routes to the dedicated `telegram_deploy` channel (§3.4, §3.14).
`partial-gtt-alert` and `template-attach-high` now also list `telegram`. `gtt-not-accepted-alert`
remains ntfy-only in `alert_routing.py`. See §3.21.

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
| `telegram_deploy` | `event_agents._send_telegram_deploy_html` (dedicated deploy bot/chat) | none (gate off) |
| `ntfy` | `alert_utils.send_ntfy_alert` with priority | `ntfy` unless `gate: false` |
| `email` | `mail_utils.send_email` to `get_alert_recipients()` | none (gate off) |

All three Telegram senders send plain text, with no `parse_mode` (§3.21). `telegram_deploy` takes
secrets `telegram_bot_token_deploy` / `telegram_chat_id_deploy`. When either is absent, it falls
back to `telegram_bot_token` / `telegram_chat_id`.

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
calls, with an optional `WHEN … DO …` pair wrapping the shared vocabulary. The compiler tags a
compiled WHEN/DO statement's output `kind:'agent'` or `kind:'event'` — purely a compiled-shape
detail, decided by whether the condition is a `log(...)` leaf (`compileAgentStmt`'s `isEventKind`),
never a separate parsed statement form. The old standalone `ALERT log_tag=…` event statement
(`event_stmt`) is retired (Wave 2, 2026-10-24): a log-stream subscription is now written
`WHEN log(tag=…, min_level=…, where={…}) DO render(render=…)`, with `|` combining multiple filters
in place of the old `match=[…]` list. See `docs/specs/AGENTS_GRAMMAR_EBNF.md`.

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

### 3.9 One Agent row per producer tag: the collapse was rejected (2026-10) — PARTIALLY REVERSED, see §3.22

**2026-10-25 update — this decision was revisited and the collapse shipped, in a narrower form than
the original rejected proposal.** §3.22 is the current design. The findings below are kept as
history: two of the four objections (renderer-per-row, condition duplication) are answered by
§3.22's own mechanism (a lookup table keyed by `alert_event`, read from the retired agents'
own `conditions`/`actions`/channel rows, never retyped). The third (per-event toggle loss on the
Automation page) is NOT answered — it is an accepted, documented tradeoff (§3.22). The fourth (no
soak time) no longer applies; the file has had a stable period since those four fixes landed.

Landed on `workshop` in `65de9319` and `5db3f7a8`. The file held 21, then (after further additions)
23 seeded agents (`SEEDED_AGENTS`,
[event_agents.py:1223](../../backend/api/algo/event_agents.py#L1223)) before the §3.22 collapse. The
table in 3.3 is not exhaustive.

A 6-agent council reviewed a proposal to collapse the 21 agents into 4 channel-keyed agents, and
rejected it at the time. Findings, so a revisit starts here:

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
  *Amended 2026-10-24:* "static per row" now refers to the `priority` token on the spec. The
  per-channel ntfy priority lives in the agent's per-slug row in `alert_routing.py` (§3.15). It is
  still static per slug. `UNROUTED_ALERT_AGENT` sits outside this table and keeps its own literal
  `events` list.

### 3.11 Token-driven ntfy priority for every event agent

Shipped earlier in `cbfa32e2`. The 2026-10 round added regression coverage only, and no production
code. The resolution order, applied per record and per channel, is:

1. The channel's own `priority` in its `events` entry. This always wins.
   *Amended 2026-10-24:* "its `events` entry" now means the resolved channel entry from
   `alert_routing.resolve_channels()` (§3.15). For a seeded agent with no explicit `events`, that
   is its per-slug row. The rule itself is unchanged.
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
- *Amended 2026-10-24.* "`DEPLOY_SYNC_AGENT` now has `events: []`" and the "one-line change to
  `events`" bullet above now refer to the per-slug row in `alert_routing.py`, not the spec. The spec
  carries `priority: medium` instead. Adding a channel is a one-line change to that row (§3.15).
- *Amended 2026-10-25 (commit `17a1c85a`).* The "no channel" decision above is reversed. Operator
  correction: deploy-sync notices reach "the telegram code deployment alerts channel alone". The
  `deploy-sync-alert` row in `alert_routing.py` now lists exactly one entry,
  `{"channel": "telegram_deploy", "enabled": True, "gate": False}`
  ([alert_routing.py:117-119](../../backend/api/algo/alert_routing.py#L117)). It is never the generic
  `telegram`, ntfy or email. The `telegram_deploy` sender is
  `event_agents._send_telegram_deploy_html`. `webhook/notify_deploy.py` follows the same rule: it
  sends Telegram only, through the same dedicated credential pair with the same fallback, and no
  ntfy or email.

### 3.15 Routing tree: channels resolved, not hardcoded (2026-10, `ec19aa1b`)

A seeded event agent no longer carries a channel list in its spec. The spec carries a `priority`
token instead. [alert_routing.py](../../backend/api/algo/alert_routing.py) resolves the channel
set. The module follows the Prometheus Alertmanager routing model: specific rules first, a general
rule behind them, and a mandatory catch-all last.

**Precedence.** `resolve_channels()`
([alert_routing.py:236](../../backend/api/algo/alert_routing.py#L236)) returns the first match:

1. A `BREACH` record's dynamic `out[4]` channels. These are read per record in `_dispatch_agent`
   and never reach the table.
2. `agent.events`, when non-empty. An operator-authored `ALERT ...` statement, or a future operator
   edit to a seeded row, keeps its explicit channel choice.
3. A per-slug rule in `_PER_SLUG_OVERRIDES`
   ([:55](../../backend/api/algo/alert_routing.py#L55)), keyed on the agent's `slug`.
4. A generic tier rule in `_TIER_DEFAULTS` ([:183](../../backend/api/algo/alert_routing.py#L183)),
   keyed on `priority`.
5. `CATCH_ALL_CHANNELS` ([:222](../../backend/api/algo/alert_routing.py#L222)). Logged once per
   `(name, priority)` pair, so a misconfigured agent does not log on every flush.

`_dispatch_agent` resolves once per agent per dispatch call
([event_agents.py:609](../../backend/api/algo/event_agents.py#L609)), not per record. BREACH's
own dynamic channels bypass the table at the per-record line.

**Two catch-alls, two mechanisms.** Do not merge them:

- `CATCH_ALL_CHANNELS` (step 5) covers an agent that matched and rendered, but fits no per-slug rule
  and no recognised tier. Its shape copies `UNROUTED_ALERT_AGENT`'s channels, and
  `test_unrouted_alert_agent_events_matches_catch_all_channels` pins the copy.
- `UNROUTED_ALERT_AGENT` with `_run_unrouted_catch_all()` (§3.13) covers a record that matches no
  active agent's conditions at all. That mechanism is unchanged. `UNROUTED_ALERT_AGENT` keeps its
  own literal `events` list and has no `priority`.

**Why a tree, not a function of priority.** A pure priority-to-channels function is wrong for part
of the seeded set. The table below was audited against the spec values at the time of this
migration. "Tier default" is what `_TIER_DEFAULTS` would give for that row's `priority`.

| Slug | Spec `priority` | Tier default | Per-slug row | Deviation |
|---|---|---|---|---|
| `deploy-sync-alert` | medium | telegram | `[]` | Channel: none at any tier |
| `cycle-breach-alert` | none | none | `[]` | Channel: dynamic per record (BREACH) |
| `mcp-ping-alert` | info | telegram_info | telegram, gate off | Channel: ops telegram, not info |
| `visitor-report-alert` | info | telegram_info | telegram_info, gate off | Gate only |
| `market-summary` | info | telegram_info | telegram_info + email, gate off | Channel: adds email |
| `order-failure-alert` | urgent | telegram + ntfy urgent | telegram + ntfy urgent + email, gate off | Channel: adds email; gate on the rest |
| `agent-alert-rich` | medium | telegram | telegram + ntfy + email, gate off | Channel: adds ntfy and email |
| `error-alert` | medium | telegram | telegram + ntfy | Channel: adds ntfy |
| `fill-alert` | medium | telegram | telegram | None (redundant but explicit) |
| `user-registered-alert` | high | telegram + ntfy high | telegram + ntfy high, gate off | Gate only |

Two kinds of deviation appear. A *channel* deviation adds, removes or swaps a channel relative to
the tier rule. A *gate* deviation keeps the channel and sets `gate: false`. `_TIER_DEFAULTS` cannot
express `gate: false`, so every gate deviation needs its own per-slug row. That is the reason the
per-slug tier exists, not a simplification. The tier defaults serve a future agent with no row.
No current production agent falls through to them.

**Empty `events` means "use the tree", not "silence".** `if agent_events:` treats `[]` as falsy.
Clearing a seeded row's channels in the Automation UI therefore reverts it to the per-slug default.
To stop a seeded agent, deactivate it.

**Inert on existing databases until a `SEED_VERSION` bump.** `seed_event_agents()` rewrites
`events` only on the `update` branch
([event_agents.py:1451-1458](../../backend/api/algo/event_agents.py#L1451)). An existing row whose
stored `events` is non-empty keeps it, so step 2 wins and the tree does not apply. A fresh database
takes the `insert` branch, which writes `events=[]`
([:1446-1450](../../backend/api/algo/event_agents.py#L1446)). The tree is live there from first
boot. `SEED_VERSION` is still 2. This work did not bump it.

*Amended 2026-10-24 (`f8cae116`).* `SEED_VERSION` is now 3
([event_agents.py:1307](../../backend/api/algo/event_agents.py#L1307)). The tree is live on existing
databases as well as fresh ones, so the paragraph above no longer describes current behaviour. The
`update` branch rewrites each seeded row's `events` to the spec's empty list, and step 2 no longer
wins for any seeded agent. The pre-bump audit, [scripts/alert_seed_audit.py](../../scripts/alert_seed_audit.py),
found all 23 `kind='event'` rows hash-clean on prod and dev, so no row was at `modified_skip` risk.
The script is read-only and is the check to re-run before any later bump. See §3.19.

**Known gap, deliberately deferred.** `routes/agents.py` still serialises the raw `agent.events`
(`_agent_to_info`, [agents.py:610](../../backend/api/routes/agents.py#L610); the update path,
[:1702](../../backend/api/routes/agents.py#L1702)). A migrated seeded row on a fresh database
therefore shows an empty channel list on its Automation card. Both serialisers must resolve through
`resolve_channels()` before the `SEED_VERSION` bump ships. The two changes are coupled.

*Amended 2026-10-24 (`f8cae116`, `e840ca0d`).* The display half of this gap is closed, additively.
`AgentInfo.resolved_channels` carries the resolved set on every event-agent response, and the
Automation page reads it. Raw `events` is still serialised unchanged. See §3.19 for why it is not
overwritten.

### 3.16 `USER_REGISTERED_AGENT` and the `auth_internal` tag (2026-10, `42156582`, `9c93dfe9`)

A new seeded agent, `user-registered-alert`, sends Telegram and ntfy (high) when a user
self-registers and awaits approval ([event_agents.py:1203](../../backend/api/algo/event_agents.py#L1203)).
Its channels are the per-slug row in `alert_routing.py`, both with `gate: false`.

**The bug was missing instrumentation, not routing.** The registration log line in
[routes/auth.py:884](../../backend/api/routes/auth.py#L884) had always been emitted. It carried
no `extra={"tags": ..., "alert_event": ...}`, and every other alert-producing log call in the
codebase does. No event agent could match it, so nothing reached any channel. The fix builds the
`extra` dict in `_registration_alert_extra()`
([auth.py:258](../../backend/api/routes/auth.py#L258)). It sets `tags=["auth"]`,
`alert_event="user_registered"`, and the `title`/`body` for the shared `generic_notice` renderer.
`username` and `email` are `html.escape()`d, because that renderer treats `body` as HTML.

**Adding `auth` to the tag vocabulary has a side effect.** `log_store.py`'s `tags_for()` adds the
logger's last dotted name as a tag when a call has no explicit `tags`. Every bare call in
`routes/auth.py` therefore picks up `auth`. Once `auth` is a catalogued tag
([agent_grammar.yaml:1451](../../backend/config/grammars/agent_grammar.yaml#L1451)), those calls
become alert-shaped. None of them carry `alert_event="user_registered"`, so each would fall through
to `UNROUTED_ALERT_AGENT` and page Telegram and ntfy with no dedup.

The 12 pre-existing calls are tagged `extra=_AUTH_INTERNAL_TAGS`
([auth.py:215](../../backend/api/routes/auth.py#L215), `{"tags": ["auth_internal"]}`).
`auth_internal` is deliberately absent from the catalogue. Owner tags are then non-empty, so the
logger-name fallback is skipped, and the calls are as invisible as they were before `auth` existed.
Test: `test_auth_internal_tagged_records_never_reach_the_catch_all`
([test_alert_routing.py:267](../../backend/tests/test_alert_routing.py#L267)).

**Invariant.** An alert must carry explicit `tags` and `alert_event`. A bare call in a module whose
logger name matches a catalogued tag is alert-shaped by accident. Adding that tag to the catalogue
changes every such call.

### 3.17 `Agent.priority` (2026-10, `ec19aa1b`)

`agents.priority` is a nullable `String(16)` column with no default
([models.py:1223](../../backend/api/models.py#L1223)). Valid values are the keys of
`_TIER_DEFAULTS`, exposed as `KNOWN_PRIORITIES`: `critical`, `urgent`, `high`, `medium`, `info`,
`low`. `validate_seed_spec()` rejects any other value
([event_agents.py:1281](../../backend/api/algo/event_agents.py#L1281)). `None` is valid, and the
resolver treats it as "no tier rule".

- Migration: `_migrate_agents_priority()`
  ([database.py:1158](../../backend/api/database.py#L1158)), `ADD COLUMN IF NOT EXISTS`, idempotent.
- Not reused from `tier`. `tier` drives topic suppression and resolves an ntfy priority value for
  several renderers. Conflating the two would change pinned golden behaviour. `ERROR_AGENT`, for
  example, must resolve `None`.
- Excluded from `compute_seed_spec_hash()`
  ([event_agents.py:1349](../../backend/api/algo/event_agents.py#L1349)). Every row seeded before
  this column stores a hash computed without `priority`. Hashing it would change that hash for every
  such row. The next seed attempt would classify all of them as operator-modified (`modified_skip`),
  though nothing was edited.
- Written only on the `insert` and `update` branches
  ([:1450](../../backend/api/algo/event_agents.py#L1450),
  [:1455](../../backend/api/algo/event_agents.py#L1455)), alongside the content it describes.

**Consequence.** `seed_action()` compares the hash of `conditions`, `events` and `actions` only. A
`priority`-only edit made in the UI is not seen as an operator edit. If the row's content still
matches its stored hash, the next `SEED_VERSION` bump's `update` branch rewrites `priority` to the
spec value.

### 3.18 `log(...)` accepts `name=` and `tags=` (2026-10, `ec19aa1b`)

The `emit_log` resolver ([actions.py:1244](../../backend/api/algo/actions.py#L1244)) takes two
optional params. They are stored in the record's `extra` as `route_name` and `route_tags`. They are
not stored as `name` or `tags`, for two reasons. Both were caught before commit.

- `name` is a reserved `logging.LogRecord` attribute. `Logger.makeRecord` raises `KeyError` as soon
  as `extra` carries it. The call would vanish silently, because the action handler's exception
  guard swallows the error.
- `tags` is the structural field the log store and every event agent's `{"log": {"tag": ...}}`
  leaf match on. Reusing it would change which agents match the record, not only how it is routed.

No routing rule reads `route_name` or `route_tags` yet. `resolve_channels()` resolves per agent, not
per record. With neither param set, `extra` is byte-identical to before.

### 3.19 Routing tree activated: `SEED_VERSION` 3 and the display field (2026-10-24, `f8cae116`, `e840ca0d`)

Sections 3.15 and 3.16 describe the tree as inert on existing databases. This section records the
activation.

- **Bump.** `SEED_VERSION` goes 2 → 3
  ([event_agents.py:1307](../../backend/api/algo/event_agents.py#L1307)). A seed pass at 3 takes the
  `update` branch for each seeded row. The branch writes the spec's `events` (empty) and `priority`,
  and the tree resolves the channels from then on.
- **Pre-flight.** [scripts/alert_seed_audit.py](../../scripts/alert_seed_audit.py) is a read-only audit
  of every `kind='event'` row. It calls the real `seed_action(..., current_version=3)` and
  `compute_seed_spec_hash()`, and compares the channel set dispatched today with the set a reseed would
  produce. It never writes or commits. All 23 rows were hash-clean on both prod and dev, so none
  would be `modified_skip`.
- **`AgentInfo.resolved_channels`** ([routes/agents.py:160](../../backend/api/routes/agents.py#L160)).
  Computed in `_agent_to_info` by `_event_agent_resolved_channels()`
  ([agents.py:631](../../backend/api/routes/agents.py#L631)). It calls `alert_routing.resolve_channels()`
  with the same arguments `_dispatch_agent` uses, and returns a deep copy. It is gated to
  `kind == 'event'`. Cycle agents get `[]`, and their resolver is never called, so no spurious
  catch-all WARNING is logged on each page load.
- **Why additive.** `events` is never overwritten by the display path. The Automation page's Edit
  button decompiles from `agent.events`. If the resolved set were written into `events`, an innocent
  no-op Save would store it and move the agent off the tree permanently. That is the same failure
  class as the `has_explicit_alert` sticky-false incident in the CLAUDE.md routing entry. The page reads
  `resolved_channels` for event agents only
  ([automation/+page.svelte:658](../../frontend/src/routes/(algo)/automation/+page.svelte#L658)).
- **`AgentInfo.is_builtin_event`** ([agents.py:170](../../backend/api/routes/agents.py#L170)). Drives the
  "Built-in" badge and hides the Delete control
  ([automation/+page.svelte:828](../../frontend/src/routes/(algo)/automation/+page.svelte#L828),
  [:1063](../../frontend/src/routes/(algo)/automation/+page.svelte#L1063)). It is `True` only for the 23
  `SEEDED_AGENTS` slugs, computed by `_is_builtin_seeded_event_agent()`. It is not `Agent.is_system`,
  because the seed insert path never sets that flag on event rows.
- **Delete protection.** `delete_agent()` ([agents.py:1599](../../backend/api/routes/agents.py#L1599))
  refuses a seeded event agent with the same 403 `is_system` gets. A deleted seeded row would be
  re-inserted by `seed_event_agents()` on the next restart, with its alert dark in the meantime.
- **`UNROUTED_ALERT_AGENT["events"]`** ([event_agents.py:1302](../../backend/api/algo/event_agents.py#L1302))
  is now built as `[dict(ch) for ch in alert_routing.CATCH_ALL_CHANNELS]`. This is a per-entry copy, not
  an alias. It replaces the hand-typed literal, so the two cannot drift. The agent still does not route
  through `resolve_channels()`. `CATCH_ALL_CHANNELS` carries `gate: False` and tier defaults do not, and
  this agent must dispatch regardless of `priority`. It has no `priority` field.

**Invariant.** The display path never writes `events`. Delete is refused for every seeded event agent.
A seeded agent's channels come from the tree once `SEED_VERSION` is 3.

### 3.20 `agent ai refine` safety guards (2026-10-24, `d1fd197c`, `b17ada22`)

`b17ada22` is the destructive-action-stripping / `lifespan_type` write-back guard
(`AgentDraft.clamp_effects`, `_ai_refine_actions_safe_to_write`,
`_ai_refine_lifespan_safe_to_write`) — a separate commit from `d1fd197c`'s `trade_mode`
exclusion, landed the same day.

`_cmd_ai_refine` ([routes/agents.py:2053](../../backend/api/routes/agents.py#L2053)) reuses
`draft_agent_from_prompt()`. That call runs `_clamp_safety`
([agent_ai.py:251](../../backend/api/algo/agent_ai.py#L251)), which is correct for the CREATE path.
On a refine it could silently change fields the operator never asked about. Four guards now apply.

- **`trade_mode` is never writable.** It is absent from `_AI_REFINE_WRITABLE_FIELDS`
  ([agents.py:721](../../backend/api/routes/agents.py#L721)). The clamp forces `paper` on every draft,
  and without this a refine on an unrelated instruction flipped a live agent to paper. An instruction
  that names a mode switch is rejected before the LLM call. The keyword check is only a UX message. The
  real boundary is the field exclusion.
- **Tree-driven `events` is never writable.** `_ai_refine_writable_fields()` removes `events` when
  `_ai_refine_is_tree_driven()` is true. The prompt context shows `resolved_channels` read-only instead.
- **Destructive actions are not stripped as a side effect.** `_clamp_safety` records what it removed in
  `AgentDraft.clamp_effects` ([agent_ai.py:65](../../backend/api/algo/agent_ai.py#L65)), as
  `stripped_actions`. `_ai_refine_actions_safe_to_write()`
  ([agents.py:902](../../backend/api/routes/agents.py#L902)) drops `actions` from the write-back when the
  clamp removed a destructive type the row already has. The exception is an instruction that mentions
  actions. A refine that proposes a new destructive action is still stripped, as before.
- **`lifespan_type` is not downgraded as a side effect.** When the clamp defaulted `lifespan_type` to
  `one_shot` (`clamp_effects['lifespan_type_defaulted']`),
  `_ai_refine_lifespan_safe_to_write()` ([agents.py:940](../../backend/api/routes/agents.py#L940)) drops
  the field if the row is not already `one_shot` or unset.

A missing `clamp_effects` (a hand-built test double) degrades to "nothing known", so nothing extra is
excluded. Real calls always populate it.

**Invariant.** A refine's write-back never removes a destructive action, downgrades lifespan, or changes
trade mode unless the instruction names that field. The keyword checks only decide whether the operator
sees a specific message. The field exclusions and clamp-effect checks are the safety boundary.

### 3.21 Message format, forced email, and the Alerts-tab merge (2026-10-25, `17a1c85a`)

Four changes that sit on top of §3.2–§3.20. Each one is in code now. Tests pin the output:
`test_alert_golden.py`, `test_alert_routing.py`, `test_ntfy_action_required_gate.py` and
`test_telegram_alert_routing.py`.

**Plain text.** Telegram and ntfy carry no markup.

- `alert_utils._send_telegram()` and `_send_telegram_info()` send no `parse_mode`. Nothing in the
  codebase passes one.
- `dispatch_payload()` builds `telegram_msg` without `<b>`/`<code>` and without `html.escape()`.
- The event-path senders `_send_telegram_html` and `_send_telegram_info_html` are pass-throughs.
  Their `_html` suffix is kept for name stability.
- `alert_utils._html_to_plain()` has no production caller left.
- Email is unchanged and still HTML.
- `webhook/notify_deploy.py` sends plain text to Telegram only. Its ntfy send is removed, and the
  email path stays retired.

**Origin line first.** `format_notification_header()` emits `Agent: {origin}` as line 1, then the
dual-tz timestamp. Renderers that build their own text lead with it. That covers chase-cancel,
partial-GTT, template-attach, order-correction, gtt-not-accepted and error. Order-failure gets its
header from `order_failure_messages()`. MCP pings get theirs at `lab.py`'s single chokepoint.

Two cases do not follow the rule:

- *Agent-less summaries, by design.* `dispatch_payload()` adds the agent line only when both
  `agent_name` and `agent_id` are present. Open and close summaries therefore lead with the title
  line, not an origin line.
- *`generic_notice` producers, a known gap.* `_render_deploy_sync` passes `extra["body"]` through
  unchanged. The producers build bare messages with no `Agent:` line. These are the five
  `background.py` sites (gtt-asymmetric, oco-double-fire, ticker-recovered, ticker-degraded and
  visitor-report), plus deploy-sync. The visitor-report body is now the plain-text
  `visitor_report._summary_text()`, not the HTML builder.

**`telegram_deploy` and the per-slug rows.** `CHANNELS["telegram_deploy"]` is the dedicated
deploy-alerts bot and chat. Only `deploy-sync-alert` names it, through its per-slug row in
`alert_routing.py`. `partial-gtt-alert` and `template-attach-high` gained `telegram`.
`gtt-not-accepted-alert` did not, and remains ntfy-only.

**Forced email for action-required records.** `_apply_action_required_email_gate()` mirrors
the ntfy gate in §3.11. When `extra["action_required"] is True` and the channel list has no
`email` entry, it appends `{"channel": "email", "enabled": True, "gate": False}`. The gate runs
once per record, before the per-channel ntfy gate. It never duplicates an existing `email` entry.
`False` or absent leaves the list unchanged.

**Alerts tab merges `alert`-tagged log rows.** `GET /api/admin/alerts/history` also returns
`LogEvent` rows whose tags include `alert`. They are sorted by real timestamp alongside
`AgentEvent` rows.

- The merge runs only when no `agent_id`, `agent_slug` or `event_type` filter is set. A
  LogEvent-sourced row has no real `Agent` FK to filter on.
- Each merged row gets `id = -event.id` and `agent_id = 0`. The `0` is a sentinel, never a real
  autoincrement id. The negated id keeps merged rows out of the positive `AgentEvent` id space.
  The code states the negation, not the reason.
- `rich_alert` and `breach` are not `alert`-tagged. Their firings already exist as `AgentEvent`
  rows, and tagging them would double each one.

**Fixed same day, commit `492fd988` (G16) — this was zero channel delivery, not just an Alerts-tab
gap.** `alert_utils` is in `log_store._SKIP_PREFIXES`. `LogStoreHandler.emit()` drops any record
whose logger name starts with that prefix, before it is queued, unconditionally. The order-failure
record (`_send_order_failure_messages()`) and the market-summary record (`send_summary()`, tag
`summary`) both logged through `alert_utils.py`'s own module `logger` — so neither ever reached
`log_events`, `dispatch_rows()` only ever receiving what `handler.drain()` returns. Confirmed via
git history: zero Telegram/ntfy/email delivery for order failures and summaries for ~19 days, since
`_SKIP_PREFIXES` gained this entry on 2026-10-06 (`54860caa`), three months after `5f0a6d5c`
(2026-07-23) had already migrated both functions onto this log-and-dispatch model. Fix: both now
log through a new `_DISPATCH_LOGGER` (name `backend.api.algo.events`, the same non-skip-listed name
`event_agents.py`'s own `_EVENTS_LOGGER` already uses) instead of the skip-listed `logger`. The
skip-list itself is unchanged. A real-logger regression test confirms the record now survives
`emit()`. See G16 in §5.

### 3.22 Alert-channel collapse: 23 seeded event agents down to 10 (2026-10-25)

Revisits §3.9. 16 of the 23 `kind='event'` seeded agents are retired from `SEEDED_AGENTS` and
replaced by 3 generic channel agents (`TELEGRAM_CHANNEL_AGENT`/`NTFY_CHANNEL_AGENT`/
`EMAIL_CHANNEL_AGENT`) plus a new lookup table, `event_agents.ALERT_EVENT_DISPATCH`. The result is
10 real seeded agents, down from 23.

**Why this is narrower than the proposal §3.9 rejected.** The original 2026-10 proposal collapsed
all agents (including the rich-alert multiplexer, the cycle-breach dispatcher, and the condition-
match catch-all) into 4 channel-keyed rows with a flat, shared condition. This version does three
things differently: it answers §3.9's renderer-per-row and condition-duplication objections
structurally rather than papering over them; it excludes 7 agents whose own mechanism does not fit
a per-`alert_event` table; and it derives every field of the new table from existing data, so there
is nothing hand-retyped to drift.

**The 16 collapsed agents.** `CHASE_CANCEL_AGENT`, `PARTIAL_GTT_AGENT`,
`TEMPLATE_ATTACH_URGENT_AGENT` (2 events: `wing_unprotected`, `wing_hard_reject`),
`TEMPLATE_ATTACH_HIGH_AGENT` (2 events: `wing_skip`, `wing_offset_skip`), `ORDER_FAILURE_AGENT`,
`ORDER_CORRECTION_AGENT`, `TEMPLATE_GUARD_AGENT`, `TEMPLATE_ATTACH_FAIL_AGENT`, `MCP_PING_AGENT`,
`GTT_ASYMMETRIC_AGENT`, `OCO_DOUBLE_FIRE_AGENT`, `TICKER_RECOVERED_AGENT`, `TICKER_DEGRADED_AGENT`,
`GTT_NOT_ACCEPTED_AGENT`, `FILL_AGENT`, `USER_REGISTERED_AGENT` — 16 specs, 18 `alert_event` keys.
Their dicts are **kept, byte-identical**, as `event_agents._COLLAPSED_DETECTOR_SPECS`
([event_agents.py](../../backend/api/algo/event_agents.py)) — still read by
`test_event_agent_tag_unification.py`'s existing per-agent condition pins, and now also the source
data `ALERT_EVENT_DISPATCH` is built from. They are removed from `SEEDED_AGENTS` and their DB rows
are deactivated (not deleted) on the next `seed_event_agents()` run.

**The 7 agents that stay dedicated, and why each doesn't fit the table:**

| Slug | Reason |
|---|---|
| `error-alert` | Blanket catch-all keyed on `tag="error"` with **no** `alert_event` at all — matches any ERROR+/CRITICAL log codebase-wide. Carries the repeat-gate (`actions[0]['gate']=True`) and `_error_agent_self_alerted` keys on its literal slug. None of this fits a per-event table. |
| `visitor-report-alert` | Its real channel is `telegram_info` only — a dedicated channel none of the 3 generic agents cover, same reason the next two stay dedicated. Not in the original brief's own enumeration of the 18 — found and corrected during implementation review. |
| `deploy-sync-alert` | Dedicated `telegram_deploy` channel, never generic telegram/ntfy/email (§3.14, §3.21). |
| `market-summary` | Dedicated `telegram_info` + email, never generic. |
| `agent-alert-rich` | Fires from the cycle-agent engine, not a log-tag producer event; already writes a real `AgentEvent` row; resolves ntfy priority from a per-record tracked tier (§3.11), not a static table entry. |
| `cycle-breach-alert` | Same cycle-agent-engine class as above; 100% dynamic per-record channels (`out[4]`). |
| `unrouted-alert-catch-all` | The condition-match catch-all safety net itself (§3.13) — must keep dispatching unconditionally, independent of this table. |

**`ALERT_EVENT_DISPATCH` is derived, never retyped.** `_build_alert_event_dispatch_table()`
([event_agents.py](../../backend/api/algo/event_agents.py)) reads, for each of the 16 collapsed
specs: its render key from `actions[0]['render']`; its `(alert_event, min_level, where)` leaves from
its own `conditions`; and its channel list from `alert_routing._PER_SLUG_OVERRIDES[spec['slug']]` —
the exact verbatim list that spec's own (now-retired) standalone Agent row used to resolve to. This
is the structural answer to §3.9's "conditions/channels would be duplicated" objection: there is
nothing here to retype, because nothing here is typed at all.

**The 3 channel agents share one generated condition.** `_build_shared_channel_condition()` builds
`{"any": [...]}`, one `{"log": {"tag": "alert", "min_level": ..., "where": ...}}` leaf per
`ALERT_EVENT_DISPATCH` entry — reproducing exactly the per-event `min_level`/`where` match semantics
each collapsed spec enforced on its own. A bare `{"tag": "alert", "min_level": "INFO"}` condition
was considered and rejected during implementation review: it would have matched every alert-tagged
record regardless of `alert_event` (defeating the unrouted-catch-all's unknown-event test), and
dropped `FILL_AGENT`'s `mode: "live"` filter (leaking paper/sim fills to Telegram).

**Dispatch mechanics.** `_dispatch_agent()` branches to `_dispatch_channel_agent()`
([event_agents.py](../../backend/api/algo/event_agents.py)) for any agent whose render action names
the internal sentinel `event_table` (never a real registered renderer) and carries a `channel_key`
(`"telegram"`/`"ntfy"`/`"email"`). For each matched record, it looks up `extra["alert_event"]` in
`ALERT_EVENT_DISPATCH`, calls the real renderer, and sends only the ONE channel entry naming
`channel_key` from that event's verbatim channel list — via `_dispatch_record_channels()`'s new
`only_channel` parameter, applied AFTER the forced-email gate (§3.21) runs against the full list, so
an action-required record (e.g. `cancel_unconfirmed`) still gets exactly one forced email, from
whichever of the 3 channel agents evaluates it, never zero and never duplicated. Firing all 3
channel agents against the same record therefore reproduces exactly the set of real sends that
record's former single dedicated agent used to make, split across 3 agent objects instead of 1.
`_dispatch_agent` never calls `alert_routing.resolve_channels()` for one of the 3 channel-agent
slugs — real dispatch for them is 100% table-driven, never routing-tree-driven.

**Display-only per-slug rows.** `alert_routing._PER_SLUG_OVERRIDES` gained 3 new rows
(`alert-channel-telegram`/`-ntfy`/`-email`, each a single matching channel entry, no `gate` key —
there is no real gate semantic to declare, since real dispatch never reaches this table for these
slugs). These exist purely so two read-only callers — `routes/agents.py`'s
`_event_agent_resolved_channels()` (the Automation page's channel chips, §3.19) and
`scripts/alert_seed_audit.py` — get a sensible single-channel answer instead of falling through to
`CATCH_ALL_CHANNELS` and logging a spurious WARNING. The 16 retired slugs' own per-slug rows are
**kept unchanged** in this same table — they are what `ALERT_EVENT_DISPATCH` reads from, and they
also let a retired (now-inactive) agent's historical channel set still display correctly on the
Automation page.

**Retirement, not deletion.** `_RETIRED_SLUGS` is the frozenset of the 16 collapsed specs' own
slugs. `_retire_collapsed_event_agents()`, called from `seed_event_agents()` right before its final
commit, deactivates (`status = 'inactive'`) any currently-active row whose slug is in that set — the
pre-existing insert/update loop only ever touches a slug present in `SEEDED_AGENTS`, so a slug simply
removed from that tuple would otherwise be left `active` forever and double-deliver alongside the
new channel agents. A fresh database (rows never inserted) makes this a no-op. The row (and its
history) stays reversible; nothing is deleted.

**`SEED_VERSION` 4 -> 5.** Pre-flight: [scripts/alert_seed_audit.py](../../scripts/alert_seed_audit.py)
was run against both prod and dev on 2026-10-25, immediately before this change shipped. All 23
pre-collapse `kind='event'` rows were hash-clean on both (zero `modified_skip` risk) — no row's
`conditions`/`events`/`actions` had ever diverged from what seeding last wrote, so the deactivation
pass cannot silently discard an operator edit.

**Observability tradeoff, accepted and documented.** Per-event activate/deactivate on the Automation
page is lost for the 16 collapsed events — they share one on/off switch per channel now, not one
each. This is NOT offset by any pre-existing per-type fire count: `trigger_count`/`last_triggered_at`
are written only by `agent_engine.py`'s cycle-agent path ([agent_engine.py:4061](../../backend/api/algo/agent_engine.py#L4061),
[:4631](../../backend/api/algo/agent_engine.py#L4631)); no `kind='event'` agent, collapsed or not,
has ever had per-type trigger-count observability. `extra['alert_event']` on the dispatched/logged
record is unchanged and still carries the specific type, so the Alerts tab / log history (§3.21)
still supports per-type filtering even though the Automation page's per-agent row count collapses to
per-channel.

**What did NOT change.** Every renderer function (`_render_chase_cancel`, `_render_order_failure`,
`_render_fill`, etc.) is untouched. The repeat-gate, the action-required ntfy/email gates, the
per-channel priority resolution order (§3.11), the DEBUG exemption, and the unrouted-record catch-
all (§3.13) are all untouched — `_dispatch_channel_agent()` reuses `_dispatch_record_channels()`
unchanged except for the new, backward-compatible `only_channel` parameter.

**Tests.** `backend/tests/test_alert_channel_collapse.py` — for every one of the 18 `alert_event`
keys, proves the exact same channel set fires at the exact same priority via the exact same
renderer (reusing `test_alert_golden.py`'s own pinned `rec` fixtures and expected literals verbatim
where they exist), proves the 3 excluded + dedicated agents are byte-identical to before, proves an
unknown `alert_event` and a known-event-below-its-min_level both still fall through to the unrouted
catch-all, proves a paper-mode fill still never sends, and proves the retirement pass. Existing
tests that asserted the pre-collapse 23-agent shape (`test_alert_routing.py`,
`test_alert_routing_tree_activation.py`, `test_event_agent_tag_unification.py`,
`test_background_alert_route_migration.py`, `test_agent_builtin_event_label.py`,
`test_seeded_event_agents_frontend_fixture.py`, `test_grammar_event_action_gate_field.py`) were
updated to read from the wider `SEEDED_AGENTS | _COLLAPSED_DETECTOR_SPECS` pool where their own
intent (pinning `resolve_channels()`'s per-slug table, or a spec's own `conditions`) is unaffected by
which pool a slug's row lives in, and to the new counts/slug sets where it is.

**Invariant.** `ALERT_EVENT_DISPATCH` is read, never retyped, from the 16 retired specs' own
`conditions`/`actions` and from `alert_routing._PER_SLUG_OVERRIDES`. A channel agent never sends a
channel absent from its matched event's own verbatim channel list, and never sends the same forced
email twice for one record.

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
| `alert_routing.py` (2026-10, `ec19aa1b`) | New. Per-slug overrides, tier defaults, catch-all, `resolve_channels()` (§3.15) |
| `event_agents.py`, `models.py`, `database.py` (2026-10, `ec19aa1b`) | Seeded agents carry `priority` instead of `events`. `Agent.priority` column and migration (§3.17). `USER_REGISTERED_AGENT` (§3.16) |
| `routes/auth.py` (2026-10, `42156582`) | Registration log line tagged `auth`. The 12 pre-existing bare calls tagged `auth_internal` (§3.16) |
| `actions.py` (2026-10, `ec19aa1b`) | `emit_log` accepts `name` and `tags`, stored as `route_name` and `route_tags` (§3.18) |
| `event_agents.py` (2026-10-24, `f8cae116`) | `SEED_VERSION` 2 → 3, so the routing tree is live on existing databases (§3.19). `UNROUTED_ALERT_AGENT["events"]` derived from `CATCH_ALL_CHANNELS` |
| `routes/agents.py` (2026-10-24, `f8cae116`, `d1fd197c`, `b17ada22`) | `AgentInfo.resolved_channels` and `is_builtin_event` (§3.19). Seeded event agents refused on delete. `_cmd_ai_refine` write-back guards (§3.20) |
| `agent_ai.py` (2026-10-24, `f8cae116`, `b17ada22`) | `AgentDraft.clamp_effects` side-channel, written by `_clamp_safety` (§3.20) |
| `automation/+page.svelte` (2026-10-24, `e840ca0d`) | Reads `resolved_channels` for event agents. "Built-in" badge from `is_builtin_event` (§3.19) |
| `scripts/alert_seed_audit.py` (2026-10-24) | New. Read-only pre-bump audit of `kind='event'` rows (§3.19) |
| `alert_utils.py`, `event_agents.py`, `alert_routing.py`, `webhook/notify_deploy.py`, `routes/alerts.py` (2026-10-25, `17a1c85a`) | Plain-text Telegram and ntfy. `telegram_deploy` channel. Forced email gate. `alert`-tagged `LogEvent` merge into the Alerts tab (§3.21). Only the commit message names the file list. The working tree was checked, not the full stat |
| `alert_utils.py` (2026-10-25, `492fd988`) | `_DISPATCH_LOGGER` replaces the skip-listed `logger` in `_send_order_failure_messages`/`send_summary` — fixes G16, zero delivery since 2026-10-06 |
| `event_agents.py`, `alert_routing.py` (2026-10-25, alert-channel collapse) | 16 seeded agents retired from `SEEDED_AGENTS`, kept as `_COLLAPSED_DETECTOR_SPECS`; 3 new generic channel agents; `ALERT_EVENT_DISPATCH` table; `SEED_VERSION` 4 -> 5; `_retire_collapsed_event_agents()`; `alert_routing._PER_SLUG_OVERRIDES` gains 3 display-only rows (§3.22) |
| `backend/tests/test_alert_channel_collapse.py` (2026-10-25) | New. Before/after delivery-identical golden gate for all 18 collapsed `alert_event` keys (§3.22) |

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

**G16 (H). `alert_utils` records were dropped before the log pipeline. [FIXED, commit `492fd988`,
same day]** `alert_utils` is in `log_store._SKIP_PREFIXES`, so `LogStoreHandler.emit()` dropped its
records before they were queued. Order-failure (`_send_order_failure_messages`) and market-summary
(`send_summary`) records never reached `log_events`, so `ORDER_FAILURE_AGENT` and the summary agent
could never match them — zero delivery to any channel, confirmed via git history to have been
broken since 2026-10-06 (`54860caa`), ~19 days. Fix: both now log through a new `_DISPATCH_LOGGER`
(name `backend.api.algo.events`, the same logger name `event_agents.py`'s own `_EVENTS_LOGGER`
already uses for this purpose) instead of the skip-listed `logger`. A regression test feeds each
record through a real `LogStoreHandler.emit()` (not a mock) and asserts it is queued. Found and
fixed 2026-10-25, in §3.21.

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
