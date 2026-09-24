# Plan: Fix Derivatives Admin Underlying LTP Desync/Staleness

## Context

Operator-reported bug on the derivatives admin page: the "Snapshot" section's underlying LTP and the "Payoff" overlay's LTP sometimes disagree with each other, sometimes go stale after switching the selected root, and sometimes both show the previous day's close instead of a live price. A read-only audit (confirmed against actual code, not assumed) found this isn't one bug — it's one dominant root cause plus several genuinely independent contributing defects that happen to surface through the same two UI surfaces. The primary defect can leave the page showing yesterday's price indefinitely with no recovery path short of a page reload, which is a real trust problem for a page used to price live option strategies.

## Primary defect — symbolStore staleness guard blocks poll updates forever

`frontend/src/lib/data/symbolStore.svelte.js` restores each symbol's last tick timestamp from localStorage on load. Every REST poll writes with `ltp_ts: 0`. The staleness guard (`if (incomingTs < storedTs) continue`) means any restored old-session timestamp permanently blocks all REST poll updates for that symbol — only a genuinely new SSE tick can override it. A symbol that hasn't ticked yet today (pre-market, evening MCX-only window, illiquid contract, failed subscribe) shows yesterday's last price indefinitely. Both Snapshot LTP and the Payoff overlay's primary LTP source read from this same store, so both are hit identically — this explains most of the "shows prev_close" and "stale on root switch" symptoms.

**Fix**: gate the staleness comparison on trading-session boundary, not raw timestamp ordering. A stored `ltp_ts` from before today's session start no longer blocks a poll write — it's treated as unstamped for comparison purposes only (not physically overwritten, so still valid for display/pruning until something newer arrives).

- Add `startOfTodayIST()` to `frontend/src/lib/dateFormat.js` (reuses the existing `todayIST()` SSOT).
- New pure, rune-free file `frontend/src/lib/data/symbolStoreArbitration.js` exporting `sessionBoundaryMs()` (day-memoized) and `effectiveStoredLtpTs(storedTs, boundaryMs)` — returns `0` for a pre-session timestamp, passes through unchanged otherwise. Rune-free so it's directly Vitest-importable (the `.svelte.js` files can't be imported by the current Vitest config, which is why this store has never had a real unit test — this closes that gap).
- In `symbolStore.svelte.js`'s `_mergeSymbolWrite` (`:243-247` and the stamp-bump block `:284-286`), route both the comparison AND the max-computation through the same `effectiveStoredLtpTs()` call — using it at only one site would let a gated-to-0 comparison get compared against its real pre-session value when computing the new max, re-introducing the bug. Do not touch `snapshot_ts` handling (polls always stamp it with real `Date.now()`, not affected). Do not add a separate hydration-time reset — the merge-time gate uniformly covers both the hydrated-entry case and a tab left open overnight.

**Explicit residual gap, out of scope for this fix**: a symbol that ticks once early and then goes silent for the rest of a live session (subscribe drop, illiquid contract) will still show that stale tick — REST polls are still rejected because the timestamp is *within* today's session. Fixing this needs a "max tick age within a live session" heuristic that risks masking legitimate circuit-breaker halts; not pursued here.

## Secondary defects — desync between Snapshot and Payoff even once data is fresh

Confirmed as one design decision, not three independent bugs (operator confirmed: **always show front-month, uniformly, across Snapshot/Payoff/NavStrip** — not the strategy's pricing anchor contract when they differ):

- **Two tick-handler paths write different contracts into one store slot** (`+page.svelte:1677-1702`) — one path writes the anchor contract's tick, another writes the front-month tick, into the same `underlyingSpotStore` slot. On contango MCX roots this flips the displayed value between two contracts tick-to-tick. **Fix**: drop the anchor-contract write (Path 2's `patchUnderlyingSpot` call) — once the primary LTP tier reads live SSE data directly via `liveSnap` on the resolved front-month contract, this write becomes redundant, and removing it eliminates the race outright.
- **Snapshot and Payoff use different fallback chains** — Snapshot falls back to a bare-root pulse/mover snapshot; Payoff falls back to a separate `_activeQuoteLtp`. **Fix**: unify via a shared `resolveUnderlyingTradingsymbol(root, findNearestFuture)` helper in `frontend/src/lib/data/resolveUnderlying.js`, used by both `underlyingSpotStore.svelte.js:getUnderlyingSpot()` (which also fixes NavStrip, a third consumer that was silently on a fourth divergent path) and `+page.svelte`'s `_undLiveLtp`/`liveSpot` resolution.
- **Snapshot's own row is internally inconsistent** — LTP from one source, Chg%/P.Close denominator from another. **Fix**: widen `_undLiveLtp` into a combined `_undLive` derived (`{ltp, close}` per root, iterating `_underlyingQuoteKeys` so positionless-but-selected roots like GOLDM populate correctly — closing a real gap where a freshly-selected root with zero book positions never got a Tier-1 value), and have the Snapshot row read LTP and Chg%/P.Close from that single source, falling back to `_underlyingQuotes` only when `_undLive` has nothing yet.

## Cold-start spot-anchor corruption (found during plan design, not in the original audit)

`backend/api/routes/options.py:_resolve_spot` — when the frontend sends a nonzero `spot` override (which it does on every refetch once `liveSpot` is available, `+page.svelte:3682`), the backend returns `prev_close=None` and `anchor_contract=None` for that response. This is a one-way ratchet: the moment an override is used, the frontend permanently loses the signal it needs to identify the anchor contract on the *next* refetch, and the payoff curve/Greeks/EV get priced against whatever the override was (front-month) instead of the contract matching the legs' modal expiry — for the rest of that session. Confirmed no other caller (`ChartWorkspace.svelte`, `SimulatorPanel.svelte`) sends this override — blast radius is contained to this page.

**Fix (operator-confirmed)**: stop sending the `spot` override entirely (`+page.svelte:3682`, `fetchStrategyAnalytics(cleanLegs, {})` instead of passing `spot: liveSpot ?? null`). The backend then always returns its own correctly-resolved anchor/prev_close/source. The Payoff overlay's spot *marker* (drawn on top of the curve) is unaffected — it's driven independently by `liveSpot`'s existing tier chain via props, not by this override. What changes: the curve itself only recenters on an actual refetch event (legs/underlying change) rather than on every successful load — in practice this was already mostly the case due to existing legs-signature memoization, so the visible behavior change is small relative to the correctness gain. No backend change needed — `_resolve_spot`'s existing behavior is correct as-is; this is purely "stop calling it with the corrupting argument."

## Underlying-quote batch-fetch race guards

`underlyingSpotStore.svelte.js:loadUnderlyingSpots` has no `ltp > 0` guard (a missing backend row returns `ltp=0`, which currently clobbers a previously-good value) and no ordering guard (an in-flight poll response can land after and overwrite a newer tick-derived value). Both are real races given this function is shared by `PositionStrip.svelte`, `+page.svelte`, and `portfolioStore.svelte.js`.

**Fix**: extract the merge logic into a new pure, testable function `buildUnderlyingQuoteUpdate(pairs, items, prevQuotes, lastTickAt, reqStartedAt)` in `underlyingQuoteUtils.js` — guards zero-value overwrites, and per-root (not global) compares each request's start time against the last tick-applied time for that root, discarding a stale response's `ltp` while still allowing it to update `prev_close`/`day_pct`. `patchUnderlyingSpot` records `lastTickAt[root] = Date.now()` on every live-tick apply.

## Should-fix items

- `_prevClose` (`+page.svelte:1818-1822`) reads `selectedUnderlying` inside `untrack`, so after a root switch it can show the old root's close for a few seconds. Move the root read outside `untrack` so it's a real reactive dependency.
- `loadStrategy({clear: true})` passes a `clear` option the function never reads (`+page.svelte:1485` vs `3621`) — root-switch invalidation currently relies solely on `didUnderlyingChange`, which has a real gap (returns `false` when switching from an equity-only synthesized strategy to a real option/futures strategy, since it short-circuits on empty legs before comparing roots). Wire up `clear` to force-reset the legs-signature cache key, closing that gap.
- Path 2's stale-root tick write is resolved for free by the secondary-defect fix above (the write is removed entirely).

## Files to change

- `frontend/src/lib/data/symbolStore.svelte.js` — session-boundary-gated staleness comparison
- `frontend/src/lib/data/symbolStoreArbitration.js` (new) — pure arbitration helpers
- `frontend/src/lib/dateFormat.js` — add `startOfTodayIST()`
- `frontend/src/lib/data/resolveUnderlying.js` — add `resolveUnderlyingTradingsymbol()`
- `frontend/src/lib/data/underlyingSpotStore.svelte.js` — `getUnderlyingSpot()` unification, race-guarded `loadUnderlyingSpots()`
- `frontend/src/lib/data/underlyingQuoteUtils.js` — new `buildUnderlyingQuoteUpdate()`
- `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` — `_undLive`/`_undLiveLtp` widening, Snapshot row source unification, drop Path 2's `patchUnderlyingSpot` write, drop the `spot` override in `loadStrategy`, `_prevClose` reactive-dependency fix, wire up `clear`
- `backend/api/routes/options.py` — read-only reference only, no change planned (`_resolve_spot` is correct as-is)

## Verification

**Vitest** (new/updated, all against pure `.js` — no rune-import blockers):
- `symbolStoreArbitration.test.js` (new) — session boundary correctness across day rollover.
- `underlyingSpotStore.test.js` — rewrite to import the real `buildUnderlyingQuoteUpdate` instead of a hand-mirrored helper (closes an existing "tests a mirror, not the real code" gap); zero-value and stale-response cases.
- `resolveUnderlying.test.js` — front-month resolution for MCX roots including `_NEXT` virtual roots.
- `pageLoad.test.js` — `didUnderlyingChange`'s equity-synth-to-real-strategy gap, documenting what `clear:true` closes.

**Playwright** (`frontend/e2e/`):
- New: seed `localStorage` with a stale `ltp_ts` via `page.addInitScript`, route the batch-quote endpoint to a different live value, assert Snapshot and Payoff both show the routed (live) value, not the seeded stale one.
- New: root-switch desync — switch between two roots (MCX pair if available), assert Snapshot LTP and Payoff overlay LTP agree within one poll/tick cycle.
- Extend `derivatives_snapshot_spot_smoke.spec.js` — assert Chg% is internally consistent with displayed LTP/P.Close per row.
- Add `navstrip_p_slot_derivatives.spec.js` to the regression set — NavStrip and Snapshot now share a resolution path, assert they move together.
- Review (may need only name substitutions, not behavior changes): `derivatives_reactive_chain.spec.js`, `derivatives_payoff_regression.spec.js`, `derivatives_pulse_fallback.spec.js` — these assert on source text of functions/tiers being renamed/changed.

**Manual, live account** (cannot be simulated):
- Pre-market: confirm a live-looking price appears on first poll even with yesterday's data in localStorage.
- MCX evening session boundary: confirm NSE underlyings freeze correctly at close while MCX roots keep updating.
- Cold start on a freshly-selected root with zero book positions (e.g. GOLDM) — confirm it populates correctly instead of staying blank.

## Done when

Backend pytest green (no backend changes expected, so this should be a no-op check), `npx svelte-check` clean, `npx vitest run` green including new arbitration/store tests, new and updated Playwright specs green against a local dev server, and the three manual live-account checks above confirmed by the operator before this ships past `dev`.
