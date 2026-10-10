# Plan: Agent CLI grammar — Sprint 5, polish (scope-ref autocomplete, keyword naming)

## Task

Close the two concrete gaps Sprint 4 left open, per the master design doc's
Sprint 5 scope ("optional, pull forward into 2/4 if small enough" — doing it
now as its own pass since Sprint 4 is already committed). Both are small,
additive, frontend-only.

**Gap 1 — scope-ref autocomplete** (confirmed gap, not guessed): Sprint 4's
`suggestAgentCliAt`/`_suggestAgentCliAtImpl` in
`frontend/src/lib/command/grammars/agents.js` explicitly documents (its own
code comments at lines 2021-2023 and 2176) that the cursor position
"immediately after `@` or `.`" — i.e. inside a `scope_ref` like
`positions.total`/`funds.total` — is out of scope for Sprint 4's bare-name
suggestions. The catalog lookup machinery for this already exists
(`_lookupAnyCatalogEntry`'s `scopeHint` parameter, `catalog.scopes` map,
`_candidateTokensForClause`) — only the SUGGESTION path (detecting this
cursor position and offering `catalog.scopes` entries) is missing.

**Gap 2 — `trigger_price` keyword alias**: `order_fields.yaml` (both the
backend and frontend symlinked copies) declares `trigger_price` as a real
`place_order` field ("Required for SL / SL-M"), but
`frontend/src/lib/command/grammars/agents.js`'s `KWARG_ALIASES` map (line
128) has short aliases for every other order field EXCEPT this one:
`account→acct, symbol→sym, exchange→exch, order_type→otype, price→px,
product→prod, variety→var, chase_level→chase`. Add `trigger_price: 'trigpx'`
(matching the existing `price→px` abbreviation convention) — confirmed no
collision with any other declared alias or catalog token name.

**Explicitly NOT doing** (per the master plan's own "secondary" framing):
mapping a backend 422 validation error's field name back to a highlighted
CLI-text position. The client-side semantic validator from Sprint 2
(`validateAgentCliStatement`) already catches the overwhelming majority of
errors before they'd ever reach the backend as a 422, so this has low
marginal value for the added complexity — skip it, note it as a still-open,
low-priority idea in the design doc rather than implement it this pass.

## Agents

- frontend: In `frontend/src/lib/command/grammars/agents.js`:
  1. Extend `_suggestAgentCliAtImpl` (and/or `_nearestNonSpaceBefore`'s
     existing detection) to recognize the scope-ref cursor position:
     immediately after `@` (start of a scope_ref), or immediately after `.`
     where the text before the `.` is itself a valid in-progress scope
     segment (for dotted scopes like `positions.expiring_today.mcx_unhedged`).
     When detected, return `{suggestions: [...catalog.scopes keys...],
     replaceRange: [...], kind: 'token'}` the same shape every other
     suggestion branch already returns — fuzzy-ranked via the same
     `fuzzyFilter` import already used elsewhere in this function. Reuse
     `_lookupAnyCatalogEntry`'s existing `scopeHint`-ordering logic as a
     reference for how scope resolution already prioritizes `catalog.scopes`
     — don't duplicate that logic, factor out a shared helper if the overlap
     is more than a few lines.
  2. Add `trigger_price: 'trigpx'` to `KWARG_ALIASES` (line 128) and its
     mirrored `_ALIAS_TO_REAL` reverse map (already derived automatically
     from `KWARG_ALIASES`, so no second edit needed there — verify this by
     reading the surrounding code, don't assume).
  3. Update the EBNF/header-comment documentation in agents.js if it
     previously said scope-ref suggestions were "out of scope" (lines
     2021-2023, 2176) — remove or correct that note now that it's
     implemented, so the comment doesn't contradict the code.

  Test coverage (mandatory):
  - Extend `frontend/src/lib/__tests__/agentsSuggest.test.js` (Sprint 4's
    suite) with new cases: cursor right after `@` suggests scope names only
    (not metrics/channels/actions); cursor right after `.` in a dotted scope
    suggests the next valid segment; fuzzy ranking still applies; a position
    that still doesn't resolve degrades to empty suggestions, no throw
    (don't regress the existing empty-result tests).
  - Add a focused test (in the same file, or `agentsGrammar.test.js` if more
    appropriate — check which file already covers `KWARG_ALIASES`) confirming
    `trigger_price=` and its short form `trigpx=` both compile to the same
    JSON for a SL/SL-M order call.

  Run after making changes:
  ```
  cd /Users/ramanambore/projects/ramboq/frontend && npx vitest run 2>&1 | tail -20
  cd /Users/ramanambore/projects/ramboq/frontend && npx svelte-check --output machine 2>&1 | tail -20
  ```
  Both must stay clean (vitest 0 failures; svelte-check 0 new errors against
  the current 5-warning baseline). Report back: exact diff summary, test
  names added, final vitest/svelte-check results.

## Tests
- pytest: no (no backend changes this sprint)
- svelte-check: yes
- playwright: no (pure autocomplete/alias addition, no new UI surface or
  user-facing flow beyond what Sprint 4's spec already covers — the existing
  `automation_cli_grammar.spec.js` continuing to pass is the relevant
  regression guard, not a new spec)

## Commit message
feat(agents): Sprint 5 — CLI grammar polish: scope-ref autocomplete, trigger_price alias

## Done when
- Typing `mean_pnl(minutes=30)@` in the CLI editor offers scope names
  (`positions.total`, `funds.total`, etc.) in the suggestion row.
- Typing `positions.` offers valid next dotted-scope segments.
- `trigger_price=23050` and `trigpx=23050` both compile to the identical
  `place_order` params JSON for an SL/SL-M order.
- Full Vitest suite green, svelte-check clean (same baseline warnings).
- Master design doc's Sprint 5 section marked done; the deferred 422-mapping
  idea stays noted as open/low-priority, not implemented.
