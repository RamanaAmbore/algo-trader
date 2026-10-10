# Plan: Agent CLI grammar — Sprint 9, console/automation consolidation

## Task

Two independent file-scoped changes (dispatched as two parallel agents):

**A. `/console`** becomes the single, always-open CLI surface for
everything typed: orders (today's `orders.js` positional grammar, kept),
agents (`WHEN...ALERT...DO...` + the now-valid shorter forms), and
brackets (`bracket(...)`). A dispatch layer in front of both grammars
decides which one a given statement belongs to; `orders.js` and
`agents.js` themselves are NOT merged into one syntax (confirmed via
earlier investigation: structurally different parsers — positional-slot
vs. fully-keyword — merging them is a real rewrite for cosmetic gain,
explicitly out of scope).

**B. `/automation`** becomes pure browse/manage: no creation entry point
of any kind (no CLI, no "Ask AI", no "+ New Agent"), just list existing
agents + brackets + Tokens(fragments) and edit them (Structured-JSON tab
for agents, equivalent structured editing for brackets). Templates
(Brackets) and Agent-templates fold into tabs on this one page instead of
separate routes. New group-based grids. Stale code from all of the above
removed outright, not hidden.

**Non-goal, explicitly deferred**: a CLI tab inside the SymbolPanel/order
ticket modal — this was tried and deliberately reverted before this
session's work began (confirmed via git history: commits `a21d5411`/
`07e6012c`, "Command Line tab retired per operator request"). Do not
re-add it. `/console` is this CLI's only standalone home for now.

**Invariant, state explicitly in both briefs**: an order placed via any
CLI surface produces the IDENTICAL `AlgoOrder` row, chase loop,
template-attach pipeline, and `OrderCard` display as one placed via the
Ticket form. The CLI is a new input method only, never a second execution
path.

## Agents

- frontend (console): In `frontend/src/routes/(algo)/console/+page.svelte`
  and `frontend/src/lib/order/CommandLineTab.svelte` (read both fully
  first — confirm the real current structure before changing it):
  1. Add a dispatch layer ahead of the existing `orderGrammar`
     (`frontend/src/lib/command/grammars/orders.js`) and the agent-family
     grammar (`frontend/src/lib/command/grammars/agents.js`). Lex the
     typed text far enough to decide: does it start with a bare verb
     matching `orders.yaml`'s verb set (`buy`/`sell`/`modify`/`cancel`) →
     route to the existing positional parser/compiler, unchanged. Does it
     start with `WHEN`/`ALERT`/`DO`/`order(`/`bracket(` (reserved words or
     call names this grammar already recognizes) → route to
     `compileAgentCliStatement`. Write this as a small, focused dispatch
     function — do not change either underlying grammar's own parsing
     logic.
  2. Render BOTH outcomes through ONE shared UI (reuse
     `AgentCliEditor.svelte`'s existing textarea/suggestion-row/error-list/
     preview shell for the agent-family path; keep `CommandLineTab`'s
     existing UI for the orders-grammar path, OR unify the shell if you
     find it's a small lift once you've read both components — your
     call, but don't force a merge that creates new bugs just for
     tidiness).
  3. A `kind:'order'` compile result (bare order/basket statement) needs
     the `bracket="name"` → `template_id` resolution Sprint 8 left as this
     sprint's responsibility: before submitting a ticket/basket whose
     compiled leg carries `template_slug`, call `GET
     /api/admin/templates/` (confirm this exact path first), find the
     matching row by `slug`, substitute its real `id` as `template_id` on
     the request, and only then submit. If no match is found, surface a
     clear error to the operator BEFORE reaching the ticket endpoint —
     never send a request with a stale/guessed `template_id`, never
     silently drop the bracket reference.
  4. A `kind:'agent'` compile result saves/updates via the existing
     `createAgent`/`updateAgent` endpoints (same as `/automation`'s
     Sprint 4 CLI tab already did — read that code for the exact pattern
     before it gets removed from `/automation` in the parallel task).
  5. A `kind:'bracket'`-shaped compile result (Sprint 8's `compileBracketStmt`
     output) saves/updates via `OrderTemplateController`'s existing
     create/patch endpoints.
  6. The CLI input is ALWAYS present/open on this page — no toggle to
     reveal it (it may already be this way; confirm, don't assume).

  **Test coverage (mandatory)**: extend whatever Vitest file already
  covers this page's command-bar integration (or add one if none exists —
  check first) with: dispatch correctly routes a `buy ...` statement to
  the orders grammar and a `WHEN ...`/`order(...)`/`bracket(...)`
  statement to the agent-family grammar; a `bracket="name"` reference
  resolves to the correct `template_id` before submission (mock the
  `GET /api/admin/templates/` call); a no-match slug surfaces a clear
  error without ever calling the ticket endpoint.

  Run: `cd /Users/ramanambore/projects/ramboq/frontend && npx vitest run 2>&1 | tail -20`
  then `npx svelte-check --output machine 2>&1 | tail -20`

- frontend (automation): In `frontend/src/routes/(algo)/automation/+page.svelte`
  (read it fully — it's large — before changing anything) and the
  routes it will absorb:
  1. **Remove, don't hide**: `aiOpen`/`aiPrompt`/`aiDraft`/`aiBusy`/
     `aiErrors`/`aiWarnings`/`aiWhy`/`aiSlug` state, the `aiDraftAgent`
     import/call, the "Ask AI" pill markup, the `.ai-pill`/`.ai-pill:hover`
     CSS (check whether `.new-agent-pill` shares any of these rules before
     deleting — it's a variant class, confirm which rules are Ask-AI-only).
     Also retire the backend path behind it: `backend/api/algo/agent_ai.py`
     and the `/api/agents/ai-draft` route handler in
     `backend/api/routes/agents.py` (grep for `ai_draft`/`AIDraftRequest`/
     `AIDraftResponse` to find every reference and remove them — this is a
     frontend-dispatched task but touches one backend file; if removing a
     whole backend module feels outside a frontend agent's comfort zone,
     do the frontend half and flag the backend retirement clearly in your
     handback instead of leaving it half-done silently).
  2. **Remove**: `toggleNewAgentPanel` and the "+ New Agent" pill/panel-
     opening state — no creation entry point survives on this page at all.
  3. **Remove**: the Sprint 4 CLI-tab integration ON THIS PAGE specifically
     — `editorMode`, `agentCliCatalog`, `cliText` field,
     `onCliAgentCompile`/`onCliOrderSubmit` handlers, the
     `AgentCliEditor`/`AlgoTabs` Structured/CLI toggle markup. **Do NOT
     delete `AgentCliEditor.svelte` or `agents.js` themselves** — the
     parallel console task reuses them; only this page's wiring into them
     is dead.
  4. **Fold into tabs**: read `frontend/src/routes/(algo)/automation/templates/+page.svelte`
     and `frontend/src/routes/(algo)/automation/agent-templates/+page.svelte`
     in full, port their list/create/edit UI into new tabs on
     `/automation` itself (reuse `AlgoTabs`, same primitive `AutomationTabs`
     already uses elsewhere), then DELETE both route files outright — not
     a redirect shim, an actual deletion; update `AutomationTabs.svelte`'s
     nav (it currently links to these as separate pages) to match the new
     single-page-with-tabs structure. Relabel the agent-templates tab
     "Tokens" (same underlying rows — this is a display-layer rename
     only, matching Sprint 10's naming decision which you don't need to
     implement the mechanics of, just use the label).
  5. **Delete** the 2 confirmed-dead stub routes:
     `frontend/src/routes/(algo)/automation/fragments/+page.svelte` and
     `frontend/src/routes/(algo)/automation/activity/+page.svelte` (0-line
     files, not linked from any nav — confirm this is still true before
     deleting, don't assume the earlier finding is stale).
  6. **New group-based grids**: a default "Agents" grid (every `Agent`
     row — derived from which list this page already fetches, no new
     query needed) and a default "Bracket" grid (every `OrderTemplate`
     row — new fetch, from the templates tab's own existing list-fetch
     logic you're porting in step 4). Any custom `group` value present on
     at least one agent or bracket creates an ADDITIONAL grid mixing both
     types together, filtered on that value. Let an operator drag an
     agent/bracket card from one grid to another, updating its `group`
     field via the existing `updateAgent`/`OrderTemplateController` patch
     endpoints (both already accept `group` as of Sprints 7/8). Build the
     scrollable-grid CSS as ONE shared class (DRY the existing
     `.channel-grid`/`.frag-row` height-cap-plus-scroll convention you'll
     find already duplicated between this page and the agent-templates
     page you're folding in — extract one class, use it for all of these
     new grids too).
  7. **Stale-code sweep**: after all the above, grep this file (and any
     file you touched) for now-unreferenced imports/functions/CSS
     selectors. The list above is the KNOWN set, not a ceiling — delete
     anything else you find that's genuinely dead as a direct result of
     these changes.

  **Test coverage (mandatory)**: this page has historically been
  Playwright-only (no Vitest seam) per earlier sprints' own findings —
  confirm that's still true, then note in your handback that e2e coverage
  for this sprint's UI changes is a follow-up (a dedicated playwright
  agent will be dispatched after you, same pattern as Sprint 4). Don't
  invent a new Vitest seam just for this if the page genuinely has never
  had one — but DO add/update any existing Playwright spec that directly
  asserts on something you removed (grep `frontend/e2e/` for
  `toggleNewAgentPanel`/`aiOpen`/`ai-pill`/the automation CLI tab markup
  Sprint 4 added, and fix or remove those specific assertions so the
  suite doesn't have stale false failures waiting — this is cleanup of
  existing specs, not new coverage, still your job).

  Run: `cd /Users/ramanambore/projects/ramboq/frontend && npx vitest run 2>&1 | tail -20`
  then `npx svelte-check --output machine 2>&1 | tail -20`

## Tests
- pytest: only if the Ask AI backend retirement (agent_ai.py/ai-draft
  route) is actually done — a quick regression test confirming the route
  is gone (404) would be appropriate; otherwise skip.
- svelte-check: yes
- playwright: deferred to a follow-up dispatch after this implementation
  lands (same pattern as Sprint 4) — not part of this immediate plan.

## Commit message
feat(agents): Sprint 9 — console becomes the one place you type, automation becomes pure browse/manage

## Done when
- `/console` accepts orders, agents, and brackets from one always-open
  CLI, dispatching correctly between the two underlying grammars.
- `bracket="name"` resolves to a real `template_id` before submission, or
  errors clearly before reaching the ticket endpoint.
- `/automation` has zero creation entry points — no CLI, no Ask AI, no
  "+ New Agent" — only list + edit, for agents, brackets, and Tokens, all
  as tabs on one page.
- Default Agents/Bracket grids plus any custom-group grids render and
  support drag-to-regroup.
- The 2 dead stub routes and the 2 now-redundant template/agent-template
  route files are deleted outright.
- Full frontend test suite green, svelte-check clean.
