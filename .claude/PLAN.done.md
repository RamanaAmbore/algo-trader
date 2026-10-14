# Plan: CLI lookahead dropdown (screener.in-style), replacing the below-textarea suggestion row

## Task

Replace `AgentCliEditor.svelte`'s current below-textarea suggestion row (a flat,
wrapping pill list, capped at 12 items, zero keyboard navigation) with a fixed-position
dropdown panel, modeled on screener.in's real open-source query builder
(`Mittal-Analytics/Screener.in/app/components/query.builder.jsx`): a plain dropdown
anchored to the editor's own container (not caret-pixel-tracked), gated on a minimum
typed-character threshold only for large candidate lists, with keyboard navigation and
an auto-advance flow that walks through a statement's structure automatically (function
→ its param names → `=` → its enum values → `,` → next param name...). Full rationale
and design detail in `~/.claude/plans/purrfect-marinating-pixel.md` — read it for
complete context before starting.

## Agents

- backend: skip
- frontend: Implement the full plan in `frontend/src/lib/command/AgentCliEditor.svelte`
  (and `frontend/src/lib/command/grammars/agents.js` only if a real gap is found during
  implementation — not speculatively, `suggestAgentCliAt` is already confirmed
  sufficient as a pure/synchronous function to drive this). Specifically:
  (1) **Positioning**: new dropdown panel, `position: absolute`, anchored to the
  editor's own wrapper directly below the textarea (same slot the old row occupied) —
  full width, `max-height` + `overflow-y: auto`. Reuse an existing dropdown-panel CSS
  pattern already in this codebase (e.g. `Select.svelte`'s own dropdown chrome) rather
  than inventing new visual chrome. Remove `.agent-cli-suggest-row`/
  `.agent-cli-suggest-pill`/`.agent-cli-suggest-desc` markup and CSS entirely, and the
  `.slice(0, 12)` cap — fully replaced, not kept alongside the new dropdown.
  (2) **Gating**: if the computed candidate list length exceeds ~10, require at least
  3 typed characters of the current partial word before showing the dropdown; below
  that threshold, show immediately on 0 characters (covers the common case — top-level
  keywords, a function's own param names, a short enum list).
  (3) **Keyboard navigation**: new `onkeydown` handler on the textarea — `ArrowDown`/
  `ArrowUp` move an `_activeIndex` through the current list (clamped, not wrapping);
  `Enter`/`Tab` accept the active suggestion (same path as a click); `Escape` dismisses
  the dropdown for the current cursor position only (track a dismissed-at cursor offset
  so it doesn't instantly reappear at the same position, but does reappear once cursor
  or text changes — and an auto-advance-triggered reopen at a DIFFERENT position must
  never be suppressed by a prior dismissal elsewhere). Mouse click/hover keep working
  as today.
  (4) **Auto-advance** in `applySuggestion`, driven by re-calling `suggestAgentCliAt`
  with the post-insert text/cursor (it's pure/synchronous, confirmed safe to call
  inline) rather than by extending its return contract: a function/token name that
  takes parameters (check via the same catalog-entry lookup the suggestion engine
  already uses internally — reuse it, don't duplicate; `entry.paramKeys.length > 0`)
  auto-inserts `(` and positions the cursor inside; a parameter name auto-inserts `=`;
  an accepted value (enum or otherwise) auto-inserts `, ` and the next param-name
  dropdown reopens; a bare zero-arg token or top-level keyword just re-runs suggest at
  the new cursor position with no extra auto-insert beyond the token itself. Use your
  own judgment (and state the choice in your handback, don't guess silently) on: whether
  to also auto-insert a trailing space after a bare keyword/zero-arg token (screener.in
  does this); the exact existing function/signature to reuse for the "needs parens"
  check; closing `)` is typed manually, never auto-inserted.
- broker: skip
- doc: skip
- backend-test: skip
- playwright: Write e2e coverage for the new dropdown: type a full statement from
  scratch using ONLY the dropdown (both keyboard path — arrows+Enter — and mouse-click
  path) and confirm the resulting compiled JSON/preview matches typing the same
  statement by hand; confirm ArrowDown/ArrowUp/Enter/Escape all behave correctly;
  confirm the old suggestion row (`.agent-cli-suggest-row`) is completely gone from the
  DOM; if the real candidate lists are all small enough that the 3-character gate never
  triggers in practice, note that in the spec rather than skipping the assertion
  silently. Use the full worked example from the already-shipped grammar-extensions
  delivery as the end-to-end script: `GIVEN p = pnl@positions.total`,
  `msg_txt = "Loss: " + p`, `WHEN p <= -50000`, `ALERT msg = msg_txt, telegram`,
  `DO order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1)`.

## Tests

- pytest: no
- svelte-check: yes
- playwright: yes

## Commit message

feat(agents-cli): screener.in-style lookahead dropdown with auto-advance, replacing the flat suggestion row

## Done when

- The old below-textarea suggestion row is completely removed, not kept as a fallback.
- A fixed-position dropdown (not caret-pixel-tracked) shows candidates at the cursor,
  gated on 3+ characters only when the candidate list is large.
- Selecting a function auto-shows its param names; selecting a param name auto-inserts
  `=` and shows its values; selecting a value auto-inserts `,` and reopens the
  param-name dropdown.
- Keyboard navigation (arrows/Enter/Tab/Escape) works end to end.
- svelte-check 0 errors, full Vitest suite green, new Playwright coverage green.
