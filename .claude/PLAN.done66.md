# Plan: Fix alert source-identity formatting inconsistencies found by audit; add explicit alert-agent label

## Task
A follow-up audit of the alert pipeline (after the notify=/hardcoded-channel cleanup in the prior
commit) found that source-identity formatting is NOT consistent system-wide: a fake "manual" tag
gets invented for alerts with no real identity, several renderers build their own ad-hoc
header/timestamp instead of the shared `format_notification_header()`/`resolve_source()`
mechanism (causing duplicate or missing timestamps), the Alerts tab computes its own identity
label independently, and a CRITICAL `app_message` is silently never delivered to any channel at
all because of a self-alerted-skip/catch-all interaction gap. Separately, the operator wants the
dispatching alert agent to get its own clear, explicit label in the trailing tag/pill — distinct
from `Agent:` (who triggered it) and `Source:` (what process logged it), not just a bare hashtag.

## Agents
- backend:
  **Agent A — identity/header/timestamp core.** Files: `backend/shared/helpers/alert_utils.py`,
  `backend/api/algo/event_agents.py`, `backend/api/algo/events.py`, `backend/api/algo/actions_live.py`,
  `backend/api/routes/lab.py`, `backend/api/algo/fill_notify.py`, `backend/shared/helpers/app_message.py`.
  1. Stop fabricating `#manual` in the trailing tag (`_resolve_tag_labels` in `event_agents.py`,
     branch 2 of `resolve_source` in `alert_utils.py`) for a record that carries NEITHER a
     `source_type` NOR an `agent_id`/`agent_name` key at all. The key-presence check (not
     truthiness) must distinguish "explicitly manual" (key present, value `None`) from "genuinely
     unknown" (key absent) — only the former may render `manual`/`#manual`. Verify with the
     unrouted self-alert and MCP ping records specifically.
  2. Give `_emit_unrouted_self_alert()` (`event_agents.py`) a real `source_type="process"` +
     `source_name` (e.g. "Unrouted alert catch-all") instead of no identity at all.
  3. Classify `_mcp_ping` (`routes/lab.py`) as `source_type="process"`, `source_name="MCP"` — an
     MCP tool-call is an external-client-triggered process event, not a manual operator action and
     not a named agent. Move the header/timestamp construction out of the producer and into
     `_render_mcp_ping` (`event_agents.py`), matching every other renderer's convention.
  4. Fix the close-hold alert's missing timestamp: `_al_agent_close_hold_message`
     (`actions_live.py`) currently does `format_notification_header(...).splitlines()[0]`, which
     throws away the timestamp line. Build the header in `_render_agent_close_hold`
     (`event_agents.py`) instead of the producer, so the full header (identity + timestamp) survives.
  5. Stop `send_summary()` (`alert_utils.py`) from duplicating the IST timestamp in the email
     subject when `subject_detail` already contains it.
  6. Fix the breach path (`events.py` ~line 109-126): its own home-made `{sim}Alert{branch} —
     {name}` lead line currently pushes the real identity line down to line 2 and duplicates the
     agent name. Put the identity on line 1 via `format_notification_header()`/`dispatch_payload()`
     properly, and align the email subject to carry a timestamp and match the `Agent: X | ts` shape
     used elsewhere.
  7. Move `_render_order_failure` (`event_agents.py`) onto the `Source: ... triggered ...` form:
     have its producer (`alert_utils.py` ~line 1544, `order_failure_messages()`) set
     `source_type="process"`/`source_name` (e.g. "Order failure"), and pass `agent_name` alongside
     the existing `agent_id` so a real agent never shows as the bare `Agent #id` fallback.
  8. In `fill_notify.py` (~line 126-140): when `_resolve_agent_names` fails (its DB-error path
     returns `{}`), do not let a real agent silently render as "manual" — pass `agent_id` through
     to `resolve_origin()` alongside `agent_name` so a lookup failure renders `Agent #{id}`
     (honest: a real agent, name unresolved) rather than the false "manual".
  9. Fix the CRITICAL `app_message` silent-delivery bug: investigate `_error_agent_self_alerted()`
     (`event_agents.py` ~line 1952) to confirm EXACTLY why a CRITICAL record carrying
     `alert_event="app_message"` is skipped by `ERROR_AGENT`'s catch-all with nothing else picking
     it up. The established, safer fix (confirm before implementing, don't guess): give
     `alert_event="app_message"` its own dedicated seeded agent (matching at WARNING+), the same
     pattern every other alert_event in this file already has — rather than loosening
     `_error_agent_self_alerted()`'s check, which could affect other records relying on it.
  10. **New requirement — explicit alert-agent label.** In `_resolve_tag_labels()`/
      `_agent_tag_line()`/`_agent_pill_html()` (`event_agents.py`), give the dispatching alert
      agent's own slot a distinct, explicit label — not just a bare hashtag indistinguishable from
      the action/process hashtags. For Telegram/ntfy, prefix it distinctly (e.g. `#via_<slug>`
      instead of a bare `#<slug>`) so it reads unambiguously as "which agent sent this," separate
      from the action-agent and process hashtags. For email, give it its own labeled pill — e.g.
      "Alert agent: {name}" — distinct from the existing "Agent:"/"Source:" pills. Keep the
      existing dedup logic (`_dedup_source_labels()`) working across all four slots.
  11. Write/update a test for every change (see Tests below).

  **Agent B — background.py / Alerts tab / market-status trigger passthrough.** Files:
  `backend/api/background.py`, `backend/api/routes/alerts.py`,
  `backend/api/algo/grammar_tokens/action_send_market_status.py`.
  1. `routes/alerts.py:_log_event_to_alert` currently derives its displayed label from the last
     segment of the logger name instead of calling `resolve_source(event.extra)`. Fix it to call
     the shared resolver, and expose `tags`/`source_name`/`agent_name` on the `AlertEvent` shape
     instead of dropping them, so Alerts-tab rows routed through `_DISPATCH_LOGGER` (order_failure,
     summary, order_cancelled, agent_close_hold, market_status, draft_sweep, app_message) show
     their real identity instead of the generic logger-name fallback "events".
  2. Remove the self-added duplicate timestamp from the deploy-sync body (`background.py` ~line
     8521-8527 adds `{timestamp_display()}` on top of the header's own timestamp) and from the
     ticker-recovered/ticker-degraded bodies (~line 6345-6422, which also add a home-made
     `TickerWatchdog — …` title line on top of the header's `Source: Ticker watchdog` identity).
     Let the shared header supply the timestamp and identity; the body should carry only the
     actual message content.
  3. `grammar_tokens/action_send_market_status.py` (~line 210) hard-codes a process identity even
     when `actions.py` has already put a real `agent_id`/`agent_name` into the action context
     (~line 835) — when an agent invokes this action, thread the real trigger through instead of
     discarding it, matching how `send_summary` already does this.
  4. Remove the dead `send_ntfy_alert` import at `background.py` ~line 8487 (no longer called).
  5. Write/update a test for every change.

- broker: skip
- doc: After implementation lands, fix CLAUDE.md drift found by the audit — the "Alert header
  consolidation" entry still describes a one-line pipe-separated `Alert:` header that was reverted
  back to a two-line `Agent:`-labeled format on 2026-11-03/04 (now doc drift, not current
  behavior); the "Alert identity SSOT" entry still lists producer lines for the GTT
  reconciler/OCO watcher that no longer exist; the `_emit_unrouted_self_alert` gap description
  understates the actual defect (fixed in this round); the "Alert message format" entry still
  claims summaries are agent-less, which is no longer universally true. Add a short new entry
  describing the explicit alert-agent label (distinct from `Agent:`/`Source:`) added in this
  round. Note the handful of seeded agents with no live producer (template_guard/attach_fail,
  gtt_asymmetric/oco_double_fire, etc.) as deliberately-kept-but-currently-unreachable, not a defect.
- backend-test: covered inline in both backend briefs above.
- playwright: skip (this round is backend-only; no frontend surface changed).

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(alerts): consistent source-identity formatting — no fake "manual", no duplicate/missing timestamps, explicit alert-agent label, fix silent CRITICAL app_message drop

A follow-up audit found source-identity formatting was inconsistent: a fake "manual" tag was
invented for alerts with no real identity, several renderers built their own header/timestamp
instead of the shared resolver (causing duplicates or a missing timestamp), the Alerts tab
computed identity independently of resolve_source(), and a CRITICAL app_message was silently
never delivered to any channel. All now route through the shared
format_notification_header()/resolve_source() mechanism. The dispatching alert agent also gets
its own explicit label in the trailing tag/pill, distinct from the action-agent and process labels.

## Done when
- No alert ever shows a fabricated "manual" identity when the real answer is "unknown."
- Every renderer gets its header/timestamp from the shared resolver — no duplicate or missing
  timestamps anywhere (deploy-sync, ticker recovered/degraded, close-hold alert, breach, summary
  email subject).
- The Alerts tab shows each row's real source/agent identity and tags, not a generic logger-name
  fallback.
- A CRITICAL `app_message` is confirmed to actually reach a channel.
- The dispatching alert agent has its own clearly labeled slot in the trailing tag/pill, visibly
  distinct from the action-agent and process labels.
- `fill_notify` never renders a real agent as "manual" on a lookup failure.
- pytest is green; CLAUDE.md's alert-identity documentation matches the current code.
