# Plan: Agent CLI grammar — Sprint 11, rich token discoverability

## Task

The real backend `grammar_tokens` rows already carry a `description`
field, and each `params_schema` entry already carries its OWN
`description` too — none of this reaches the frontend today.
`_buildCatalogEntry` (agents.js) builds only `{token, valueType,
paramsSchema, paramKeys}`, silently dropping `row.description`.
`AgentCliEditor.svelte`'s suggestion row shows bare names only.

**Fix**: carry `description` through the catalog entry (top-level; each
param's own `description` is already present inside `paramsSchema[key]`,
just needs rendering, not a new field). Add a detail view to the
suggestion dropdown: on hover/focus of a suggested token, show its
description; when suggesting a param name inside an open call, show that
param's own description; when suggesting an enum value, show what it
means if the schema has per-value descriptions (check — may not exist,
don't invent one if the backend doesn't have it, just show the bare enum
list as today in that case). Applies uniformly across every token kind —
metrics, scopes, channels, actions, Tokens, bracket/order TP-SL-Wing
kwargs — same field, same display component, no per-kind special-casing.

**Independent of every other sprint** — purely additive to the
already-shipped autocomplete (Sprint 4/5's `suggestAgentCliAt`).

## Agents

- frontend: In `frontend/src/lib/command/grammars/agents.js`:
  1. `_buildCatalogEntry` — add `description: row.description || ''` to
     the returned entry object (don't touch `paramsSchema` — it already
     carries each param's own `description`, confirm this by reading a
     real row's shape before assuming).
  2. `suggestAgentCliAt`'s returned suggestion shape — check its current
     return type (`{suggestions, replaceRange, kind}`, per Sprint 4/5's
     own documentation in this file) and extend it to carry enough for
     the UI to look up each suggested item's description: either add a
     parallel `descriptions: string[]` array (same order/length as
     `suggestions`) or change `suggestions` from `string[]` to
     `{label:string, description:string}[]` — your call on which is
     cleaner given how `AgentCliEditor.svelte` currently consumes this
     return value (read it first), but pick ONE approach and apply it
     consistently for all three suggestion kinds (token/param/enum), not
     a different shape per kind.

  **Test coverage (mandatory)**: extend
  `frontend/src/lib/__tests__/agentsSuggest.test.js` — a token suggestion
  carries its catalog description; a param-name suggestion (inside an
  open call) carries that param's own description; an enum-value
  suggestion with no backend-provided per-value description degrades
  gracefully (empty string, not a crash); confirm existing tests asserting
  on the OLD `suggestions: string[]` shape still pass or are updated
  consistently if you changed the shape (don't leave half the test file
  asserting the old shape and half the new one).

  Run: `cd /Users/ramanambore/projects/ramboq/frontend && npx vitest run 2>&1 | tail -20`
  then `npx svelte-check --output machine 2>&1 | tail -20`

- frontend: In `frontend/src/lib/command/AgentCliEditor.svelte` (read it
  fully first): render the description text in the suggestion
  dropdown/row — on hover or keyboard-focus of a suggestion item, show
  its description in a small detail area (reuse this app's existing
  `InfoHint`-style presentation pattern if one fits here, or a simple
  inline secondary-text line below/beside each suggestion — your call,
  keep it visually consistent with this app's existing density/typography
  conventions, don't invent a new visual pattern). No description
  available (empty string) → just don't render the detail area for that
  item, not an empty box.

  No new test file needed if this component has no existing Vitest seam
  (check first, per this project's established convention — Playwright-
  only for UI-only changes with no new logic is acceptable here; this is
  a small, additive rendering change, not new business logic).

## Tests
- pytest: no (no backend changes this sprint — `description` already
  exists on real rows)
- svelte-check: yes
- playwright: no (small additive UI change to already-tested surface;
  defer to general e2e coverage, not a new dedicated spec)

## Commit message
feat(agents): Sprint 11 — surface token/param descriptions in CLI autocomplete

## Done when
- Hovering/focusing a suggested metric/channel/action/Token shows its
  real backend description.
- Hovering/focusing a suggested param name (inside an open call) shows
  that param's own description.
- No description available renders nothing extra, not an empty box.
- Full frontend test suite green, svelte-check clean.
