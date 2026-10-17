# Plan: Click-to-describe already-entered CLI tokens

## Task

Add a "click an already-typed token to see what it means" feature to the CLI grammar
editor (`AgentCliEditor.svelte` + `agents.js`), distinct from the existing live-autocomplete
dropdown-hover description (which only works while a token is still being typed). Covers:
Automation Console, Agent/Alert/Bracket/Template Edit flow, and the Orders-CLI tab inside
the order ticket (`OrderCliTab.svelte`) — all share this one component/engine. Also adds a
separate, bespoke tooltip mechanism for the Automation page's read-only JSON-preview card
(a different surface, not CLI grammar text). Explicitly out of scope: `CommandLineTab.svelte`
/`CommandBar.svelte`/`orders.js` (confirmed legacy, no visible UI uses it as primary surface).

Council ran 6/6 CONCERN, 0 BLOCK — design is directionally sound (confirmed zero execution/
P&L blast radius: this is a pure read-only lookup, structurally unreachable from compile/
submit) but several concrete corrections are required before implementation. All incorporated
below. Operator confirmed via follow-up: "orders cli" = `OrderCliTab.svelte` (not
`CommandLineTab.svelte`); "detailed view" = both the Edit flow AND the read-only JSON preview
card.

## Design — Part 1: `AgentCliEditor.svelte` (Console / Edit / Orders-CLI tab)

**New resolver** `describeTokenAt(text, pos, catalog)` in `frontend/src/lib/command/grammars/agents.js`:

1. **Lexing (two-tier fallback, not full-text-only or prefix-only)** — try `lexAgentStatement(text)`
   (full text) first; if it throws (e.g. a later, unrelated unclosed call — the realistic
   "in-progress agent" case), fall back to `lexAgentStatement(text.slice(0, endOfClickedToken))`
   (prefix extended just past the clicked token, mirroring the existing proven pattern at
   `_suggestAgentCliAtImpl`); if that also throws (e.g. an unterminated string spanning the
   click position), return `null` — no tooltip, never a wrong one. Wrap the whole function in
   try/catch mirroring `suggestAgentCliAt`'s own pattern (never throws to the caller).
2. **Find covering token** — linear scan for the token whose `[start,end)` covers `pos`
   (NAME tokens only; literals/punctuation → no tooltip).
3. **Clause context** — reuse the existing `_clauseKindFromTokenPrefix` helper (already used by
   `suggestAgentCliAt`) rather than a new hand-rolled backward scan.
4. **Bound-label check FIRST** — call `_collectBindingsUpToCursor(text, pos)` (already
   generic over `pos`, not cursor-specific) and check whether the covering token's name matches
   a bound GIVEN/ALERT/DO label. If so, describe it as a bound-label reference (not a catalog
   lookup) — this was a confirmed coverage gap in the original design (devil's advocate): a
   GIVEN-bound label used elsewhere is arguably the single most useful case, and
   `_lookupAnyCatalogEntry` never checks bindings at all.
5. **Param-key resolution** — a CLOSED call lexes as one token with pre-parsed `.args` (each
   carrying its own position) per the existing lexer design; if the covering token is such a
   call and `pos` falls inside its args, scan `.args` directly for the one covering `pos` and
   resolve its description from that call's `paramsSchema`. For a still-OPEN call, reuse the
   existing `_findOpenCallAt(text, pos)` (already safe for an arbitrary offset, confirmed).
6. **Otherwise** — resolve via the existing `lookupAgentCliEntry(name, catalog, clauseKind)`,
   same as the dropdown-hover path already does.
7. Returns `{name, description, token, kind}` or `null`.

**Must-not-touch list** (risk council's explicit ask — keep the diff strictly additive):
do not modify `_findOpenCallAt`, `_lookupAnyCatalogEntry`, `_clauseKindFromTokenPrefix`,
or `_collectBindingsUpToCursor` themselves — only call them.

**Wiring in `AgentCliEditor.svelte`**:
- New dedicated click handler (e.g. `_onTokenClick`), separate from `_updateCursor` (which
  also fires on every keystroke/keyup/focus — bolting onto it was flagged independently by
  perf, UX, and risk). `onclick={(e) => { _updateCursor(); _onTokenClick(e); }}`.
- New dedicated `$state` var (e.g. `_clickDescResult`), written imperatively inside the click
  handler — NOT a `$derived` tracking `value` (would re-run on every keystroke for no reason),
  and NOT a reuse of `_hoverLabel`/`_hoverDesc` (those are keyed to the live suggestion
  dropdown's candidate list and will show wrong/missing descriptions for an arbitrary
  already-typed token — confirmed independently by architect, risk, and devil's advocate).
- New sibling panel markup (not nested inside the existing `{#if _showDropdown}` block,
  which doesn't exist in the DOM when there's no active dropdown — confirmed structurally
  wrong to "reuse as-is" by architect/devil's advocate).
- **Precedence rule**: if a click also causes `_showDropdown` to become true (the live
  autocomplete can fire on the same click), the dropdown wins — the describe-panel only
  renders when `_showDropdown` is false. Prevents two competing panels.
- **Dismiss rule**: clear `_clickDescResult` on next keystroke (inside `handleInput`), on
  blur, on click elsewhere, and give the panel its own entry in the existing `layerStack` so
  Escape dismisses it without closing the host order-ticket modal (SymbolPanel).
- **Placement guard**: panel must never render over the Submit/"Place order"/"Cancel orders
  (LIVE)" buttons — anchor near the clicked token, clamp away from the action-button row.
- Trigger stays **click**, not hover — matches the operator's explicit ask, matches this
  codebase's own established precedent (click-to-pin over hover for the Chase/Bracket/Draft
  InfoHint tooltips, commit `bd2f0946`), and a click already moves the textarea caret
  regardless of this feature, so it adds no new edit-disruption risk (UX + devil's advocate).

**Read-only "Generated statement(s)" preview** (`canonicalLines`, rendered via `<pre>`):
wrap each token in a `<span>` with the same click handler (simpler than a hover+mirror-overlay
layer, and keeps one consistent interaction model instead of introducing hover for just one
surface) — resolves positions against that string's own text, not the live textarea's `value`.
Scoped to the `agent`/`event`/`bracket`/`token` decompile kinds (real parseable grammar text);
`order`/`cancel_all_orders` kinds build a hand-written summary string, not grammar text, so
click-to-describe does not apply there (UX's finding) — acceptable, those legs already show
their own summary values directly.

## Design — Part 2: Automation page's read-only JSON-preview card

`frontend/src/routes/(algo)/automation/+page.svelte`'s `agentRowView` snippet renders
`JSON.stringify(a.params, null, 2)` (and related structured fields) — not CLI grammar text,
so `describeTokenAt` does not apply. Separate, smaller mechanism: wrap each top-level JSON key
in a `<span>` with its own click handler; resolve the key's description by looking up the
agent's own action/metric token (already known from the compiled agent JSON) via
`lookupAgentCliEntry(...).paramsSchema` — same catalog data, no new description table, just a
different lookup path keyed by JSON field name instead of by lexer token. Same dedicated
`$state`/panel/dismiss-rule pattern as Part 1 (no new infrastructure).

## Agents

- frontend: implement `describeTokenAt` (agents.js) + wiring in `AgentCliEditor.svelte` (Part 1,
  textarea + canonicalLines preview) and the JSON-key click mechanism in `automation/+page.svelte`
  (Part 2). Follow the must-not-touch list above.
- backend: skip (zero backend involvement — catalog is already client-resident).
- doc: update `docs/specs/` (new or amended CLI-editor spec section) describing both mechanisms,
  the click-wins-over-dropdown precedence rule, and the Escape-layer/dismiss behavior.
- backend-test: skip.
- playwright: new spec covering (a) click-to-describe on a committed catalog token in each of
  WHEN/ALERT/DO/GIVEN clauses, (b) click-to-describe on a GIVEN-bound label reference, (c) click
  on a call param key (open and closed call), (d) dropdown-wins-over-describe precedence, (e)
  Escape dismisses the describe panel without closing the host order-ticket modal, (f) panel
  never overlaps the Place-order/Cancel-all buttons, (g) regression check that the existing
  hover-dropdown-description spec stays green, (h) the JSON-preview-card click mechanism on the
  Automation page.

## Tests

- pytest: no (no backend changes)
- svelte-check: yes
- vitest: yes — unit tests for `describeTokenAt` covering: clauseKind inference across all
  four clause kinds; KEY-token inside an open call; KEY-token inside a closed call (args-scan);
  GIVEN-bound-label resolution; null/empty catalog; both lex-fallback-tier scenarios (a later
  unrelated unclosed call; a string literal spanning the click position); a click on
  whitespace/punctuation → null. Also a specific test asserting `compileAgentCliStatement`'s
  output and the textarea's `value`/`selectionStart`/`selectionEnd` are byte-identical before
  and after invoking the click-describe handler — the concrete proof of the "read-only" claim.
- playwright: yes (see above)

## Commit message

feat(command): click-to-describe already-entered CLI tokens (Console/Edit/Orders-CLI + Automation JSON preview)

## Done when

- Clicking any already-typed catalog token, call param, or GIVEN-bound label in the CLI
  textarea (Console, Edit flow, Orders-CLI tab) shows its description in a dedicated panel
  that never collides with the live-suggestion dropdown, never occludes Place-order/Cancel-all,
  and dismisses cleanly (keystroke/blur/click-elsewhere/Escape) without closing the host modal.
- The same click-to-describe works on the "Generated statement(s)" read-only preview for
  agent/event/bracket/token kinds.
- The Automation page's JSON-preview card supports click-to-describe on its top-level param keys.
- `describeTokenAt` never throws and never mutates `value`/selection/compile output.
- All new tests green; existing hover-dropdown-description spec unaffected.
