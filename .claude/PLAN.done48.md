# Plan: Standardize alert message format, origin, and channel routing; surface event-agent alerts in the Alerts tab

## Context

RamboQuant has two parallel, never-unified alerting subsystems: the cycle/threshold-agent
engine (`agent_engine.py`, writes `AgentEvent` rows, feeds the Alerts tab) and the `kind='event'`
log-tag agent pipeline (`event_agents.py`'s `dispatch()`/`_dispatch_agent`, feeds
Telegram/ntfy/email directly, writes nothing to any DB table at all). A three-agent investigation
this session confirmed both the message-format inconsistency across the 15 real renderers and the
structural reason event-agent alerts are invisible in the operator-facing Alerts tab.

The operator's explicit requirements, confirmed via AskUserQuestion where ambiguous:
1. Every alert message must clearly state its ORIGIN (manual order, which agent, a scheduled
   event, a deploy, etc.) and carry BOTH IST and EDT timestamps.
2. Telegram and ntfy must be plain text — no HTML tags anywhere, reversing the current deliberate
   design (confirmed: today Telegram is sent `parse_mode: HTML` with real `<b>`/`<code>` tags).
3. Every alert goes to Telegram, with no exceptions — **approved**: the three currently
   ntfy-only agents (`partial-gtt-alert`, `template-attach-high`, `gtt-not-accepted-alert`) gain
   Telegram; `deploy-sync-alert` (currently gets ZERO channels — a pre-existing bug) gains
   Telegram only.
4. Deploy/code-deployment alerts never go to ntfy or email — **approved**: this also covers the
   REAL deploy notification, `webhook/notify_deploy.py` (a standalone script outside this whole
   pipeline), whose ntfy send is removed. `deploy-sync-alert` already excludes ntfy/email via its
   existing per-slug override (`[]` in `alert_routing.py`) — only its missing Telegram entry is
   added, per #3.
5. Action-required alerts go to both ntfy (already gated) and email — **approved**: add a
   matching forced-email gate, mirroring the existing `_apply_action_required_ntfy_gate`.
6. Alert-worthy log lines should carry a standard token so the Alerts tab can surface them too
   (currently impossible — the log-tag pipeline has zero DB persistence).

## Approach — two work-streams, each independently testable and deployable

### Stream A — Alert-worthy log lines get an `alert` tag; Alerts tab surfaces them

**Tagging mechanism**: append `"alert"` to the existing `extra={"tags": [...]}` list at each
alert-worthy LOG-CALL SITE (not inside `event_agents.py`'s dispatch path — confirmed via two
independent investigations that `dispatch_rows()` shallow-copies every row dict at
`event_agents.py:910` before `_dispatch_agent` ever sees it, so any tag mutation there is invisible
to `log_store.py:flush_once()`'s subsequent `insert_rows()` call, which persists the ORIGINAL
pre-copy dicts). `tags_for()` (`log_store.py:136-171`) only ever extends and dedupes a record's tag
list, so this is purely additive — zero risk to any existing `_eval_log` condition matching on the
original tag.

Call sites to tag (every log line that currently backs one of the 15 real renderers' conditions —
cross-reference each `conditions.log.tag`/`where.alert_event` pair already in
`event_agents.py`'s `SEEDED_AGENTS`): order-failure, fill, chase-cancel, partial-gtt,
template-attach (guard/fail/urgent/high), gtt-not-accepted, order-correction, mcp-ping. Error-level
records already universally match `ERROR_AGENT`'s catch-all — do NOT add `"alert"` there; it would
tag every `logger.error()` call in the codebase as alert-worthy, which is not the intent.

**Catalog**: add `- token: alert` to `backend/config/grammars/agent_grammar.yaml`'s `log_tags`
list (~line 1487-1504) so `_check_owner_tags()` doesn't warn and a future `log(tag="alert")`
condition validates via `REGISTRY.log_tag()`.

**Alerts-tab backend**: extend `GET /api/admin/alerts/history`
(`backend/api/routes/alerts.py`) with a second query against `LogEvent`
(`backend/api/models.py:2678`), filtered `LogEvent.tags.overlap(["alert"])` (same ORM comparator
already used for a different table at `backend/api/routes/messages.py:88`) plus the existing
`since_dt` bound (no `sim_mode` column on `LogEvent` — read it from `extra.get("sim_mode")`,
matching `event_agents.py:916,919`'s own convention). Map each row into the SAME `AlertEvent`
msgspec struct (synthetic `id` offset to avoid colliding with real `AgentEvent.id` values,
`agent_name`/`agent_id` best-effort from `logger`/`extra`, `conditions_summary` from `message`,
`channels_sent` left `[]` — this information isn't persisted for this pipeline and isn't worth a
schema change for, `detail` from `message`, `triggered_at` via the same `format_dual_tz()`,
`event_type` set to a fixed literal e.g. `"log_alert"`). Concatenate with the existing
`AgentEvent` query's results, sort by timestamp descending, truncate to `effective_limit` — this
exact two-source-concat-sort-truncate shape already has a direct precedent in this codebase:
`backend/api/routes/logs.py`'s `GET /api/logs/unified` (`_fetch_order_events`/`_fetch_agent_events`
→ concat → sort → truncate). Confirmed this requires ZERO frontend change — `LogPanel.svelte`'s
Alerts tab consumes whatever `AlertEvent`-shaped rows the route returns; server-side normalization
means the existing `_alertRows`/`_alertContentHtml` keep working unmodified.

### Stream B — Standardize every renderer's format + fix channel routing gaps

**Plain-text reversal (no HTML anywhere in Telegram/ntfy)**:
- `_send_telegram()` (`alert_utils.py:218-257`) and `_send_telegram_info()` (`:260-297`): stop
  sending `parse_mode: "HTML"` (or pass `None`).
- Remove HTML-tag construction from: `_render_error` (`event_agents.py:363-364`),
  `order_failure_messages()` (`alert_utils.py:1108,1111`), `template_guard_message()` /
  `template_attach_fail_message()` (`template_attach.py:244-258` and the fail-message twin),
  `dispatch_payload()`'s `_build_tg_warning_block` (`alert_utils.py:479-486`), the MCP-ping call
  sites (`routes/lab.py:975,1023,...`), and the visitor-report `generic_notice` caller
  (`background.py:7234`).
- Fix the SNEAKY second HTML-injection point: `_send_telegram_html`/`_send_telegram_info_html`
  (`event_agents.py:398-401,442-444`) wrap ANY bare 2-tuple-returning renderer's title in `<b>`
  regardless of what the renderer itself built — this affects 6 renderers that never build HTML
  explicitly (fill, chase_cancel, partial_gtt, template_attach, order_correction,
  gtt_not_accepted). Change these two dispatch-layer functions to send plain `f"{title}\n{body}"`.
- `webhook/notify_deploy.py` (`:90,74,79`): drop `parse_mode`/HTML tags too — same rule applies to
  the standalone deploy script, which is otherwise outside this renderer pipeline.
- `_render_breach` (`events.py:111-125`) is ALREADY plain-text today for both telegram/ntfy —
  use it as the reference implementation; no change needed there.
- `_html_to_plain()` becomes largely redundant once nothing builds HTML in the first place — leave
  the function in place (it's still a documented, scoped decoder) but it should have no new
  Telegram-path callers; don't invest in generalizing it.

**Origin + dual-tz gaps** (add `format_notification_header()` + `timestamp_display()`/
`format_dual_tz()`, LEADING line, matching every already-compliant renderer's convention):
- `_render_template_attach` (`event_agents.py:~129-141`): currently has NO origin line at all, and
  its timestamp is appended at the END instead of leading — fix both.
- `_render_mcp_ping` (`event_agents.py:~246-247` + call sites in `routes/lab.py`): currently NO
  origin, NO timestamp anywhere in the message text — add both.
- `_render_deploy_sync`/`generic_notice` family (shared fn, 6 callers across `background.py`):
  currently NO origin, NO timestamp for any of the 6 — add both; this is the function backing
  `deploy-sync-alert` and the 5 `generic_notice` agents (gtt-asymmetric, oco-double-fire,
  ticker-recovered, ticker-degraded, visitor-report).
- `_render_summary` (open/close summaries): deliberately has no single originating agent — keep
  as a documented exception, but still add the dual-tz timestamp if it's missing (confirmed
  partial today, relies on caller-supplied `ist_display`).

**Channel routing fixes** (`backend/api/algo/alert_routing.py`, `_PER_SLUG_OVERRIDES`):
- Add `telegram` to `partial-gtt-alert`, `template-attach-high`, `gtt-not-accepted-alert` (today
  ntfy-only).
- Add `telegram` to `deploy-sync-alert` (today `[]` — zero channels, a pre-existing bug); keep it
  excluded from ntfy/email.
- `webhook/notify_deploy.py`: remove the ntfy send entirely (Telegram-only for real deploy
  notifications).

**Forced-email-on-action-required gate** (`event_agents.py`): add
`_apply_action_required_email_gate(ch, action_required)`, mirroring
`_apply_action_required_ntfy_gate` (`:714-742`) — when `action_required is True` and the resolved
channel list has no `email` entry, synthesize one (same `enabled: True` shape every other channel
entry uses) so `chase-cancel-alert`/`order-correction-alert`/any future action-required agent
reaches email even if its static per-slug/tier row never listed it. Wire into
`_dispatch_record_channels` alongside the existing ntfy gate call.

## Files

- `backend/shared/helpers/alert_utils.py` — plain-text Telegram send functions, `_html_to_plain`
  callers, `order_failure_messages()`, `dispatch_payload()`.
- `backend/api/algo/event_agents.py` — renderer origin/timestamp gaps, dispatch-layer HTML
  fallback, new email action-required gate, catalog-aligned `"alert"` tag additions at the
  renderer-adjacent call sites that log the triggering record.
- `backend/api/algo/template_attach.py` — `template_guard_message()`/`template_attach_fail_message()`.
- `backend/api/algo/alert_routing.py` — per-slug channel-list fixes.
- `backend/api/routes/alerts.py` — second `LogEvent` query, merge/sort/truncate.
- `backend/config/grammars/agent_grammar.yaml` — new `alert` log-tag catalog entry +
  `backend/tests/golden/agent_grammar_baseline.json` update.
- `backend/api/routes/lab.py`, `backend/api/background.py` — mcp-ping / generic_notice call-site
  updates (origin, timestamp, drop HTML).
- `webhook/notify_deploy.py` — drop ntfy send + HTML.
- Tag-at-log-site additions scattered across the real producers (`orders_place.py`, `chase.py`,
  `template_attach.py`, `orders_postback.py`, etc.) — same pattern each site, append `"alert"` to
  an existing `extra={"tags": [...]}` list.

## Tests

- Golden-output tests (extend `test_alert_golden.py`'s existing pattern) for every renderer whose
  output string changes — plain-text Telegram body, leading origin line, dual-tz timestamp.
- New test: `LogEvent`-tagged `"alert"` row surfaces in `/admin/alerts/history`'s merged response,
  sorted correctly alongside `AgentEvent` rows, both sources respecting `since_minutes`/`limit`.
- New test: a `tags_for()` call with an explicit `"alert"` owner tag produces no catalog warning
  (the `agent_grammar.yaml` addition) and dedupes correctly against an existing tag.
- New test: `_apply_action_required_email_gate` forces an email entry exactly when
  `action_required is True` and none is already present; leaves an existing email entry alone;
  never touches non-ntfy/email channels.
- Routing test: the three newly-telegram'd agents and `deploy-sync-alert` resolve `telegram` in
  their channel list via `alert_routing.resolve_channels()`.
- `webhook/notify_deploy.py`: a scripted/manual check (it's a standalone script, not covered by
  the pytest suite today) confirming no ntfy call and no `parse_mode` remain.
- Full backend + frontend suites green; CC gate clean.

## Done when

- Every one of the 15 renderers leads with an origin line and a dual-tz timestamp (summary
  excepted per its documented design), with zero exceptions silently left out.
- No Telegram or ntfy send anywhere in the pipeline (including `notify_deploy.py`) contains an
  HTML tag; `_send_telegram`/`_send_telegram_info` no longer request HTML parse mode.
- Every event agent resolves at least `telegram` in its channel list (verified via
  `alert_routing.resolve_channels()` for all 20 per-slug rows + the 3 tier-default fallbacks); no
  agent resolves to `[]` any more.
- `deploy-sync-alert` and `notify_deploy.py` never reach ntfy or email.
- Action-required alerts always reach both ntfy (existing) and email (new gate).
- A log line tagged `"alert"` at its call site appears in `/admin/alerts/history`'s response,
  correctly interleaved by timestamp with real `AgentEvent` rows, with no frontend change required.
- Full suite + CC gate green.
