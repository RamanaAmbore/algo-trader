# Plan: Site improvements — GOLDM subscription, rollover sig, LTP tooltip, npm audit

## Task
Four improvements identified in site audit and confirmed by the planning council (4 APPROVE · 2 CONCERN · 0 BLOCK):

1. **GOLDM anchor subscription gap** — when `selectedUnderlying` has no portfolio positions (e.g. GOLDM from the dropdown), `_underlyingQuoteKeys` never includes it → `loadUnderlyingQuotes` never fires for GOLDM26JULFUT → KiteTicker never subscribes → `liveSnap` returns undefined → spot LTP blank in payoff + Greeks. Fix: extend `_underlyingQuoteKeys` ($derived at line 874 of `+page.svelte`) to also include `selectedUnderlying` when it is NOT already present in the portfolio set. Must remain gated on `void instrumentsReady` (already present at line 875) so cold-start doesn't resolve a synthetic MCX stub.

2. **Front-month rollover detection** — `_lastQuoteSig` at line 4003 keys only on `.root`; when GOLDM rolls from GOLDM26SEPFUT → GOLDM26OCTFUT the root stays "GOLDM", sig is unchanged, `loadUnderlyingQuotes` never re-fires. Fix: include `quoteKey` in the sig: `.map(p => p.root + ':' + p.quoteKey)`.

3. **LTP null tooltip** — pulse-grid LTP cells already show `—` via `numFmt` when `value == null`. The missing piece is context: add `tooltipValueGetter: (p) => p.value == null ? 'No live price' : null` to `mkLtpCol` in `pulseColumns.js` so the dash is explained on hover.

4. **npm audit fix + Phase 2 comment removal** — run `npm audit fix` (no `--force`) in `frontend/` to patch HIGH/MODERATE js-yaml, browserslist, devalue, @vitest/mocker. Remove lines 16-17 (dead planning comment) in `MarketPulse.svelte`.

## Agents
- backend: skip
- frontend: (1) In `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`, extend `_underlyingQuoteKeys` at line 874 to also push `{ root: selectedUnderlying, quoteKey: r.quoteKey }` when `selectedUnderlying` is non-empty and not already covered by the `_byUnderlyingTotals` loop (use a `seen` Set). Keep `void instrumentsReady` guard. (2) At line 4003, change the sig to `.map(p => p.root + ':' + p.quoteKey).sort().join('|')` and update the comment at lines 3997-4000 to drop the incorrect "quoteKey change catches the same poll cycle" sentence. (3) In `frontend/src/lib/data/pulseColumns.js`, add `tooltipValueGetter: (p) => p.value == null ? 'No live price' : null` to the return value of `mkLtpCol` (after `valueFormatter`). (4) In `frontend/src/lib/MarketPulse.svelte`, delete lines 16-17 (the "Phase 2 additions (not wired yet)" comment). (5) Run `cd /Users/ramanambore/projects/ramboq/frontend && npm audit fix` (no --force). For every changed file write or update a test: pulseColumns change → update `frontend/src/lib/__tests__/data/pulseColumns.test.js` to assert `mkLtpCol` returns a `tooltipValueGetter` that returns "No live price" when `p.value == null` and `null` otherwise. The `_underlyingQuoteKeys` and `_lastQuoteSig` changes are component-internal state; confirm via `svelte-check` that no type errors are introduced.
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(derivatives): subscribe selectedUnderlying to KiteTicker when not in portfolio; fix rollover sig; add LTP null tooltip

## Done when
- Selecting GOLDM (or any MCX virtual root with no portfolio positions) in the derivatives page causes the resolved front-month futures contract to appear in `_underlyingQuoteKeys` → triggers `loadUnderlyingQuotes` → KiteTicker subscribes it → liveSpot populates within one SSE tick cycle
- Changing `selectedUnderlying` on rollover day (when quoteKey changes but root stays the same) correctly fires `loadUnderlyingQuotes` again
- Hovering over a `—` LTP cell in any pulse grid shows "No live price" tooltip
- `svelte-check` exits 0 errors
- `npx vitest run` passes with the new `tooltipValueGetter` assertion in `pulseColumns.test.js`
