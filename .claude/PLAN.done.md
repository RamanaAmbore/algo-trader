# Plan: Create/update agent UI + global switches with per-card override

## Task
Add a real create/update flow for both threshold and notification (event) agents
from the frontend, add a global-switches settings panel (paper_trading_mode,
default_agent_trade_mode, capability flags) with write access, and add a
per-agent-card override that falls back to the global default when unset. Also
fix a live bug: `tier`/`topic` silently fail to persist on agent save because
`AgentCreateRequest`/`AgentUpdateRequest` lack those fields even though the model
and frontend already carry them.

Hard scope boundary: renderers (`RENDERS` in `event_agents.py`) are hand-written
Python functions — the event-agent create form is a renderer PICKER (existing
renderer + condition + channel), never a code editor for a new renderer.

Full design rationale, research findings, and out-of-scope items:
/Users/ramanambore/.claude/plans/purrfect-marinating-pixel.md

## Agents
- backend: In `backend/api/routes/agents.py`: add `kind` (literal "threshold"|"event", default "threshold"), `tier`, `topic` to `AgentCreateRequest` and `AgentUpdateRequest`; fix `update_agent`'s per-field copy loop to persist `tier`/`topic` (currently silently dropped on save — confirmed live bug, write a regression test for this specifically). For `kind=="event"` creates/updates, call `validate_seed_spec()` from `event_agents.py` (slug uniqueness, known log tag against `grammar.py:LOG_TAG_TOKENS`, known renderer key, known channel) instead of the threshold-agent condition/action validator — on the event-agent create/update path do NOT run the threshold-only validation. Add `GET /api/agents/renderers` returning `{key, label, description}` for every entry in `event_agents.py`'s `RENDERS` dict. Add `set_bool`/`set_string` write helpers to `backend/shared/helpers/settings.py` (currently read-only `get_bool`/`get_string`) and a `PATCH /api/admin/global-switches` route (in `backend/api/routes/admin.py`) covering `execution.paper_trading_mode`, `execution.default_agent_trade_mode`, and the `cap_in_dev`/`alert_*` capability flags — every write audit-logged (who/when/old→new) via the existing write-event pattern, never silent. A threshold agent's effective `trade_mode` must resolve at READ time as `row.trade_mode or global_default` — never freeze the default at creation time; this is the one subtle correctness point in the whole design, give it an explicit test. For every file you change, write or update a pytest test covering the changed lines (tier/topic round-trip, event-agent create via validate_seed_spec rejecting an unknown renderer/channel/tag, renderers-list endpoint, global-switch write + audit log entry, and the trade_mode fallback-resolution-at-read-time behavior).
- frontend: In `frontend/src/routes/(algo)/automation/+page.svelte`: add a real "+ New Agent" entry point (not routed through Ask-AI) with a Kind selector (threshold vs notification/event). Threshold kind reuses the existing inline editor form as-is. Event kind shows a new builder: renderer picker populated from the new `GET /api/agents/renderers` endpoint (human label + description, never hardcode the renderer list), log-tag/min-level/where condition fields, a channel checklist using the EVENT-agent channel vocabulary only (`ntfy`/`telegram`/`telegram_info`/`email` — do NOT reuse or merge with the threshold-agent channel set `telegram`/`email`/`websocket`/`log`), priority, gate. After creation, relax the existing `disabled={agent.kind === 'event'}` whole-form `<fieldset>` (around line 864) to field-level: channel-enabled, priority, and gate stay editable for an event agent post-creation; renderer, condition, and slug stay fixed (same lifecycle as a seeded agent) — keep the Save/Validate buttons disabled only for the fixed fields' validation path, not unconditionally. Confirm `_buildEditPayload()` already includes `tier`/`topic` in its outgoing payload (likely yes — this fix is mostly backend-side) and add a UI regression check that editing tier/topic and reloading shows the new value. Add a global-switches panel to `frontend/src/routes/(algo)/admin/settings/+page.svelte`: each switch's current value with a toggle that calls the new PATCH route, plus an explicit blast-radius warning specifically on paper_trading_mode ("affects every account, prod-wide"). On each agent card in the automation page, show "Global: <value>" with an override toggle — threshold agents toggle `trade_mode` (null = inherit), event agents toggle per-channel `enabled` inside the existing `events: [...]` array on the spec. Add `frontend/src/lib/api.js` wrappers for the new renderers-list and global-switches-PATCH endpoints. For every file you change, write or update a Playwright spec per the standing test rule — source-level specs (reading component source, asserting the expected markup/guard logic, matching this session's established pattern) are fine for the override-toggle wiring and renderer-picker plumbing; write at least one spec that actually drives the create-event-agent flow if a dev server is reachable, otherwise a thorough source-level spec is an acceptable substitute — note which you did in your summary.
- broker: skip
- doc: skip (doc sync happens after commit, in Step 5.5 of /impl)
- backend-test: skip (backend agent above writes its own tests per the standing rule)
- playwright: skip (frontend agent above writes its own specs per the standing rule)

## Tests
- pytest: yes
- svelte-check: yes
- playwright: yes

## Commit message
feat(agents): create/update UI for threshold + event agents; global switches with per-card override; fix tier/topic save bug

## Done when
- Creating a new event (notification) agent from the UI works end-to-end: renderer picker populated from the backend, condition + channel + priority set, saved, fires correctly through the existing event-agent pipeline.
- Editing tier/topic on any agent persists (regression test added for the silent-drop bug).
- Global switches panel shows current values and can set them, each write audited.
- Each agent card shows the global default and can override it; threshold agents via `trade_mode`, event agents via per-channel `events[]` enabled flags.
- Full pytest + svelte-check + the new Playwright coverage green.
