# Plan: Sprint 14 — wire up event-agent/token/group features that compile but never reach the backend

## Task

A post-Sprint-12 audit found that three previously-"shipped" sprints
each have a working compiler but a missing or broken last wire to the
real UI/API:

1. **Event agents (Sprint 12) have no submit button at all.**
   `console/+page.svelte:224-233` only renders a submit button for
   `kind === 'agent'` or `kind === 'bracket'`. `compileStatement`
   correctly returns `kind:'event'` for an `ALERT log_tag=...`
   statement (confirmed, Sprint 12's own tests assert this), but the
   page shows no button for it — `submitConsoleAgent`'s `kind==='event'`
   branch is unreachable from the real UI. `AgentCliEditor.svelte:
   132-137`'s `previewPayload` also returns null for `event`/`bracket`/
   `token`, so there's no JSON preview either.

2. **`token(kind=notify)` (Sprint 10) compiles but nothing submits it.**
   No `submitConsoleToken` function exists. `createAgentFragment`
   (`frontend/src/lib/api.js:394`) is never imported anywhere.
   `console/+page.svelte` has no token button.
   `consoleDispatch.js:48`'s `_AGENT_CALL_RE` regex
   (`/^(order|place_order|bracket)\s*\(/i`) doesn't include `token`, so
   a `token(...)` statement typed into the legacy `CommandLineTab` gets
   misclassified as a shell command and POSTed to `/api/admin/exec`.

3. **`#group=` (Sprint 7) is write-only for agents — read path missing,
   so group grids silently empty on reload.** `AgentInfo`/
   `_agent_to_info` (`backend/api/routes/agents.py:62-106`, `489-511`)
   have no `group` field (also missing `cli_source`). Sprint 9's group
   grids read `a.group` (`automation/+page.svelte:747,751`) — always
   undefined. `setAgentGroup` writes `group_name` correctly, shows a
   success toast, then `loadAgents()` reloads with no `group` field, so
   the agent visibly snaps back to "ungrouped". Brackets are NOT
   affected — `templates.py:_to_out` already emits `group` correctly;
   only agents are missing this.

Also fold in two small, cheap should-fix items from the same audit:

4. **`applies_to` enum mismatch.** `agents.js:2702`'s
   `_BRACKET_APPLIES_TO_SPEC` allows `['buy_any','sell_option','both']`;
   the real backend `_APPLIES_TO_CHOICES` (`templates.py:39`) also
   allows `sell_any`. Add `sell_any` to the CLI's enum list so the CLI
   isn't missing a real, valid backend choice. (Separately: a seeded
   template `default-long-option` uses `buy_option`, which the
   backend's OWN validator rejects — this is a PRE-EXISTING backend/seed
   data bug unrelated to the CLI grammar; do NOT fix it as part of this
   task, just don't let the CLI enum list silently diverge further —
   note it in your handback as a separate, pre-existing defect.)

5. **Stale "Fragment"/"AgentTemplate" wording still reaches operators.**
   `backend/api/routes/agent_templates.py`'s `HTTPException` details
   ("Fragment N not found", "Cannot delete system fragments…") surface
   verbatim in TokensTab's toasts. `agents.js:2792-2793`'s compile
   errors say "AgentTemplate has no tags/group field". Reword these
   specific strings to say "Token" instead — purely cosmetic string
   changes, no behavior change.

## Agents

- frontend: Fix items 1, 2, 4, 5 (frontend-only pieces):
  1. In `console/+page.svelte`, widen whatever condition gates the
     submit button (read the real code around line 224-233 first, don't
     assume the exact shape) to also render for `kind === 'event'` and
     `kind === 'token'`. In `AgentCliEditor.svelte`'s `previewPayload`
     (~line 132-137), return the real compiled payload for `event`/
     `bracket`/`token` kinds instead of null, so the JSON preview works
     for all compile kinds uniformly.
  2. Add `submitConsoleToken` to `consoleDispatch.js`, mirroring
     `submitConsoleAgent`'s existing pattern (same slug-handling
     conventions where applicable), POSTing to the existing
     `createAgentFragment` (`frontend/src/lib/api.js:394` —
     `/api/admin/fragments`). Confirm the compiled `{kind,name,body,
     description}` shape from `compileStatement` for a `token(...)`
     statement already matches `FragmentCreate`'s real shape
     (`backend/api/routes/agent_templates.py:56`) before wiring — if it
     doesn't match exactly, adapt the submission function, not the
     compiler (the compiler's output is covered by existing Sprint 10
     tests; don't break those). Wire a token button into
     `console/+page.svelte` (same submit-button widening as item 1,
     plus actually calling `submitConsoleToken`). Add `token` to
     `consoleDispatch.js:48`'s `_AGENT_CALL_RE` regex so the legacy
     `CommandLineTab` dispatch path also classifies `token(...)`
     correctly instead of routing it to `/api/admin/exec`.
  4. Add `sell_any` to `agents.js:2702`'s `_BRACKET_APPLIES_TO_SPEC`
     enum list.
  5. Reword `agents.js:2792-2793`'s compile-error strings from
     "AgentTemplate" to "Token".

  **Test coverage (mandatory)**: a NEW Playwright spec
  (`frontend/e2e/console_event_token_submit.spec.js` or similar) that
  types a full `ALERT log_tag=..., render=..., telegram #name="..."`
  statement into the real `/console` page, confirms a submit button
  renders, clicks it, and confirms a real agent row was created (via
  the agents list API or UI) — this is the exact gap the audit found
  ("no e2e spec covers it", "tests didn't catch it because
  consoleDispatch.test.js skips the page gate entirely"). Also cover
  the `token(kind=notify) ...` submit flow in the same spec or a sibling
  one. Update `consoleDispatch.test.js`/`agentsGrammar.test.js` for the
  `sell_any` enum addition and the reworded error strings.

- backend: Fix item 3 (backend-only piece):
  Add `group` and `cli_source` fields to `AgentInfo`
  (`backend/api/routes/agents.py`, wherever the struct is defined —
  check near line 62-106) and populate them in `_agent_to_info`
  (~line 489-511), mirroring exactly how `templates.py:_to_out` already
  emits `group` for brackets (read that function first, copy its
  pattern, don't reinvent). Also reword item 5's backend-side strings:
  `agent_templates.py`'s `HTTPException` details ("Fragment N not
  found", "Cannot delete system fragments…") → reword to say "Token".

  **Test coverage (mandatory)**: a pytest test that creates an agent
  with `group="hedge-book"` via the existing create endpoint, then GETs
  it back via `list_agents`/`get_agent` and asserts `group` AND
  `cli_source` are both present and correct in the response — this is
  exactly the round-trip Sprint 7's existing tests never covered (they
  only tested the write path). Add to `backend/tests/
  test_agent_kind_tier_topic.py` or a new file, whichever fits the
  existing test file's scope better.

  Run: `cd /Users/ramanambore/projects/ramboq && venv/bin/pytest backend/tests/ -k "agent" -q --tb=line`

## Tests
- pytest: yes (new group/cli_source round-trip test)
- svelte-check: yes
- playwright: yes (new console event/token submit spec)

## Commit message
fix(agents): Sprint 14 — wire event-agent/token submission and agent group read-path (audit findings)

## Done when
- Typing an `ALERT log_tag=...` or `token(kind=notify) ...` statement
  into the real `/console` page shows a submit button, and clicking it
  creates a real row via the existing backend endpoints.
- An agent's `#group=` value survives create → list/get round-trip, so
  Sprint 9's group grids stop snapping agents back to "ungrouped" after
  reload.
- `sell_any` is a valid CLI bracket `applies_to` value.
- No operator-visible string says "Fragment"/"AgentTemplate" in the
  paths touched by this sprint.
- Full backend + frontend test suites green (pytest, vitest,
  svelte-check, the new Playwright spec).
