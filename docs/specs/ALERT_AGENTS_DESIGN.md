# Alert Agents: Design, Before and After

Status: implemented on `main` as of `9323f9d3`; gap status in section 5 is current as of the working tree after that commit. Source of truth for the alert
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
| `deploy-sync-alert` | deploy, WARNING, deploy_out_of_sync | deploy_sync | ntfy high |
| `agent-alert-rich` | agent, INFO, rich_alert | rich_alert | telegram, ntfy urgent, email |
| `market-summary` | summary, INFO, summary | summary | telegram_info, email |

Channel entries may set `priority` (ntfy) and `gate` (default true). `gate:
false` skips the capability flag check, to keep the same behaviour as the
direct code it replaced.

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

**G14 (L). No UI for event agents. [PARTLY: the automation editor now shows a read-only view for kind='event' agents, with Activate/Deactivate still available. There is no structured builder for a new event agent's condition or channels]** Event agents are visible on the agents
page, but their conditions and channels cannot be edited there (the editor
writes cycle-agent JSON). Fix: extend the editor with a log-tag picker, which
is already built, and mark system agents read-only.

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
