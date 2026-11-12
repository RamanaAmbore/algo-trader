# Plan: Remove action-layer channel-naming (notify= + two hardcoded producer spots + two audit-found direct sends); keep alert(...)/Agent.events intact

## Task
Action agents (anything taking a real side-effecting action, or any producer code outside the
alert-agent layer) must never pick a delivery channel or send a notification directly — they may
only log a tagged record (tags/level/alert_event/source_type/source_name), and a dedicated seeded
"alert agent" in `backend/api/algo/event_agents.py` is the only thing that matches that log
record, resolves channels via `alert_routing.py`, and actually sends. The log record is the sole
interface between the two.

**Corrected scope (superseding an earlier, broader draft of this plan):** the `alert(...)` DSL
channel-call clause and everything it populates (`Agent.events`, `has_explicit_alert`,
`resolve_channels()`'s per-agent-override read of `agent.events`, the decompiler round-trip) are
an alert agent's own legitimate mechanism for picking its own channels based on its own
condition — they are KEPT fully intact, unchanged. Nothing about `alert(...)` is removed. Only
action-layer/producer code that embeds or names a channel outside of logging is in scope:

1. The universal `notify=[...]` action-call kwarg — removed entirely, no replacement.
2. Two hardcoded `[telegram, ntfy]` literals in `actions_live.py` and `orders.py` — converted to
   tags/level-only logging, picked up by a seeded agent + per-slug-override row.
3. Two more direct-send violations a follow-up audit found, neither covered by 1/2:
   - `backend/api/background.py:_ds_tick_draft_sweep` (~line 4584) — calls
     `alert_utils._send_telegram_info` directly for the 08:00 draft-sweep notice.
   - `backend/shared/helpers/app_message.py:_route_sinks` (~lines 126-145) — calls
     `send_ntfy_alert` directly for any error/critical-tagged `AppMessage`; currently unreached
     (today's only caller always logs at `level="info"`) but reachable, and bypasses the whole
     alert-agent architecture if ever triggered at error/critical.

## Agents
- backend:
  1. `notify=` removal — delete `_al_notify_channels_for_log`/`_al_maybe_notify_action` and the
     `execute()` call site in `backend/api/algo/actions.py`; delete `_render_action_notify` and
     retire the seeded `ACTION_NOTIFY_AGENT` row (deactivate, never delete); add a validation rule
     in `backend/api/routes/agents.py:_age_validate_action_entries` rejecting a `notify` key on any
     `actions` entry; add a one-time idempotent migration in `backend/api/database.py` that strips
     any stored `notify` key from every `actions` entry (no other field touched — `events`,
     preamble, and `has_explicit_alert` are explicitly NOT cleared by this migration).
  2. Fix the two hardcoded producer spots — remove the hardcoded channel list from `extra` in
     `actions_live.py:_al_send_agent_close_hold_alert` and `orders.py:_opp_send_cancel_alert`; add
     matching `_PER_SLUG_OVERRIDES` rows in `alert_routing.py` with the verbatim-identical channel
     lists removed; add the one new seeded agent + renderer needed for the close-hold alert in
     `event_agents.py`, and stop `_render_order_cancelled` from returning a dynamic channel-override
     tuple element.
  3. Fix the two audit-found violations — convert both to tags/level-only logging picked up by a
     seeded agent (draft-sweep) or by making the existing log path tag/alert_event-bearing instead
     of sending directly (`app_message.py`'s error/critical sink).
  4. Leave untouched: `alert_routing.py:resolve_channels()`'s `agent.events` read (step b), every
     seeded `BUILTIN_AGENTS` entry's `events` list in `agent_engine.py`, `events.py`'s
     `_emit_breach_record`/`_dispatch_channel`, and `event_agents.py`'s `_render_breach`'s `out[4]`
     channel override — all of these are the `alert(...)` mechanism working as intended.
  5. Loose ends from the follow-up audit: fix/rewrite `backend/tests/test_actions_notify.py` (it
     currently imports now-deleted functions and will ImportError); add `WITH ORDINALITY ... ORDER
     BY` to the notify-strip migration's `jsonb_agg` rebuild so action order is guaranteed, not
     incidental; clean up the retired `ACTION_NOTIFY_AGENT` spec's dangling reference to the
     unregistered `"action_notify"` renderer name.
  6. Write/update a test for every file touched.

- frontend:
  1. In `frontend/src/lib/command/grammars/agents.js`, remove ONLY `_splitNotifyArg` /
     `_compileNotifyClause` and the `notify=` stripping/merge in the action-call compiler
     (compile error, no replacement), and the identical removal in `compileTokenStmt`'s
     notify-kind fragment body syntax.
  2. Leave fully intact: `compileAlertClause`, `_compileAlertCallAsOrdinaryCall`, the
     `rawName === 'alert'` branch in `_compileSharedCall`, the `has_explicit_alert` splice in
     `compileAgentStmt`, and every decompiler branch that renders a stored `alert(...)`
     preamble/DO entry back into text. None of this is touched by this plan.
  3. Update/trim only the `notify=`-specific grammar/decompiler/console-dispatch test cases;
     leave every `alert(...)`-compiling/decompiling test case as-is.
  4. Add/update Playwright coverage for the `notify=` compile-error path only.

- broker: skip
- doc: After implementation lands, sync CLAUDE.md to describe: `notify=` and the two hardcoded
  producer spots are removed/fixed; the two audit-found direct sends (draft-sweep,
  `app_message.py`) are fixed; `alert(...)`/`Agent.events`/`has_explicit_alert` are explicitly
  confirmed unchanged and still the correct, intended mechanism for an alert agent's own channel
  choice. Do not describe `alert(...)` or `Agent.events` as removed anywhere in CLAUDE.md.
- backend-test: covered inline in the backend brief above.
- playwright: covered inline in the frontend brief above.

## Tests
- pytest: yes
- svelte-check: yes
- playwright: yes

## Commit message
refactor(alerts): remove action-layer channel-naming (notify=, two hardcoded producer spots, two audit-found direct sends)

Action/producer code can no longer name a delivery channel directly — notify= is removed, the
two hardcoded [telegram, ntfy] literals in actions_live.py/orders.py and the two direct-send
paths a follow-up audit found (background.py's draft sweep, app_message.py's error/critical
sink) all now log tags/level only, picked up by a seeded alert agent via alert_routing.py. The
alert(...) DSL clause and Agent.events — an alert agent's own legitimate per-agent channel
choice — are explicitly unchanged.

## Done when
- `notify=[...]` produces a clear compile error on any action call; no code path can log with an
  inline channel list from action/producer code anywhere in the system.
- The two hardcoded Python producer spots and the two audit-found direct-send paths all route
  through a seeded-agent + per-slug-row pattern instead of calling a sender directly.
- `alert(...)` compiles, decompiles, and resolves channels via `agent.events` exactly as it did
  before this work — zero behavior change for any agent using it.
- `test_actions_notify.py` passes (rewritten, not left importing deleted functions); the
  notify-strip migration uses explicit ordinality; the retired `ACTION_NOTIFY_AGENT` spec has no
  dangling renderer reference.
- pytest, svelte-check, and Playwright are all green; CLAUDE.md accurately reflects that
  `alert(...)`/`Agent.events` were kept, not removed.
