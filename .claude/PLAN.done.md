# Plan: Chain/Order-Ticket/Derivatives/NavStrip fixes (5 defects)

## Context

Five independent, disjoint-file UI defects were reported during a walkthrough of the order
Chain tab, the standalone Order Ticket, the Derivatives page, and NavStrip. Each was
root-caused via a dedicated read-only investigation (Explore agents) and then the exact
edit was confirmed/corrected via a Plan-agent pass that re-traced the actual code (one
original hypothesis for issue 1 was wrong and is corrected below). None of the five touch
the same file, so they can be implemented and tested independently, each as its own commit
(per this project's "fix one issue at a time" convention) — but dispatched in parallel since
there's no file overlap.

Two related but separately-scoped follow-ups came up during this same investigation and are
queued, NOT part of this plan (operator confirmed): (a) backend alert-agent consolidation
(fold 3 remaining `generic_notice` agents into `ALERT_EVENT_DISPATCH`, 16→13; merge
`expiry-nfo-risk-alert` + `expiry-mcx-risk-alert`), and (b) a frontend reload/SWR fix for the
Orders page, LogPanel (all tabs), and the Derivatives page's non-store state (`strategy`,
option-chain/payoff), extending the existing `createDataStore` pattern that already does this
correctly for 8 broker-data domains. Both are separate tasks to run after this plan ships.

---

## Issue 1 — Basket leg tagged to stale account after switching accounts mid-build

**Root cause (confirmed, corrected from initial hypothesis):** `OrderTicket.svelte`'s local
`_account` mirror is seeded once and never re-synced while the Ticket tab is hidden
(`display:none`, still mounted) behind the Chain tab. There is **no echo-snapback loop** as
first suspected — `_lastNotifiedAcct` latches on mount and the echo effect correctly no-ops
while stale. The real bug is plainer: `_basketPayload()` builds `{ account: _account, ... }`
straight from the stale local value, so any leg added via the Ticket tab's own `onAddToBasket`
path carries the old account. Separately, `OptionChainTab.svelte`'s `_mergeIntoBasket` **and**
`_netAgainstBasket` (both, not just one) match an existing leg by `sym`+`side` only — no
`account` in the key — so re-pressing +/- on an already-staged strike after switching accounts
silently folds into (or nets against) the OLD leg, keeping the old account.

**Fix:**
1. `frontend/src/lib/order/OrderTicket.svelte` (~line 1554, before the existing echo effect):
   add a continuous re-sync effect mirroring `OptionChainTab.svelte:218-221`'s pattern —
   `if (account && _isRealAcct(account) && account !== untrack(() => _account)) _account = account;`.
   Ungated (writes only local state).
2. Same file, line ~1562: gate the existing echo effect with `if (suspended) return;` at the
   top — defends against the OTHER seed branch (stale-account-no-longer-in-list fallback)
   firing while hidden and clobbering the shared account. `suspended` is already an in-scope
   prop here.
3. `frontend/src/lib/order/OptionChainTab.svelte`: add `account` to the match key in both
   `_mergeIntoBasket` (lines 594-615) and `_netAgainstBasket` (lines 617-641) —
   `b.sym === incoming.sym && b.side === incoming.side && (b.account||'') === (incoming.account||'')`.
   Pass `account: _account` into both call sites (`addOptionToBasket` ~line 696,
   `addFuturesToBasket` ~line 777) and into the `_netAgainstBasket(...)` calls alongside them.

**Explicitly out of scope, flagged only:** `SymbolPanel.svelte:2453` passes
`account={_sharedAccount || _ticketProps.account || account}` — `_sharedAccount` takes
precedence over a modify/repeat order's own carried account. Pre-existing, independent
question; not touched by this fix.

---

## Issue 2 — Qty +/- stepper on a staged pill never removes the leg at zero

Chain-tab's own strike +/- (`_netAgainstBasket`) already removes a leg at qty 0 — correct.
The generic pill stepper shown under the chain for ANY staged leg (from either tab) instead
floors at 1 and disables, requiring a separate "×" to remove.

**Fix:** `frontend/src/lib/SymbolPanel.svelte:2637-2640` — on the "−" button, when
`(leg.lots||1) - 1 <= 0`, call `removeBasketLeg(i)` (the same function the "×" button already
calls, `:629-631`/`:2701`) instead of clamping. Drop the `|| (leg.lots||1) <= 1` disabled
condition.

---

## Issue 3 — Derivatives page: Legs/Paired/Exp Close as a dropdown, new unpaired default

Today "Legs" (default) shows CE/PE auto-paired rows via `sortRowsByPairGroup`
(`pairGroupSort.js`), switched via a 2-option `<AlgoTabs>`. Target: a 3-option dropdown —
**Legs** (new default: account-wise, sorted within account, no CE/PE pairing — same grouping
convention the Pulse Positions grid already uses), **Paired** (today's current pairing
behavior, relabeled), **Exp Close** (unchanged).

**Fix:**
1. `frontend/src/lib/data/pairGroupSort.js` — add `sortRowsByAccountOnly(rows)`: group by
   `_rowAccount` (existing helper) only, then sort within each account by
   `decomposeSymbol` root → strike → CE-before-PE → symbol (mirrors `pulseUnified.js:984-1000`'s
   `_compareSameGroup` convention exactly — do NOT use `accountSort.js`'s operator-configured
   `display_order`, since `sortRowsByPairGroup` itself uses plain lexicographic account
   ordering; using a different convention for the new view would make the two views disagree).
2. `frontend/src/lib/data/candidateRowComposition.js` — widen `composeDisplayedCandidates`'s
   `isExpiryTab: boolean` param to `mode: 'legs'|'paired'|'expiry'`. `mode==='expiry'` keeps
   the existing early-return bypass; `mode==='paired'` calls `sortRowsByPairGroup` (today's
   behavior); `mode==='legs'` calls the new `sortRowsByAccountOnly`. The draft-rows-last
   reshuffle stays unconditional after either branch. Rewrite the module's header comment,
   which currently describes a 2-branch "byte-identical" invariant — it must describe the new
   3-mode contract instead (this file has a documented history of silent sort-composition
   regressions; the comment must not be left stale).
3. `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`:
   - `:754` — widen the `LegsTab` typedef to the 3 values; default stays `'legs'` (`:755`,
     unchanged, now meaning unpaired).
   - `:906` — pass `mode: legsTab` instead of `isExpiryTab: legsTab === 'expiry'`.
   - `:928` — **required fix, confirmed regression otherwise:** `legsUnderlyingAnchor`'s guard
     currently reads `if (legsTab !== 'legs' || !selectedUnderlying) return null;` — under the
     new scheme this would hide the underlying-anchor row on the `'paired'` view (today's
     actual main view). Change to `if (legsTab === 'expiry' || !selectedUnderlying) return null;`.
   - `:6037-6068` — replace `<AlgoTabs>` with the existing `$lib/Select.svelte` component
     (already imported in this file and used identically for the `selectedUnderlying` picker at
     `:5679` — same convention, no new widget introduced). Options:
     `{value:'legs', label:'Legs (N)'}`, `{value:'paired', label:'Paired (N)'}`,
     `{value:'expiry', label:'Exp Close (N)'}` — fold the live row-count into each option's
     label text (operator-confirmed), reusing whatever count expression `AlgoTabs` currently
     computes per tab (`displayedCandidates.length`, `expiryCloseTotal`).
   - `CandidateLegRow.svelte` needs **no changes** — confirmed every `legsTab` check in it is
     `=== 'expiry'`/`!== 'expiry'`, so both new values fall through identically. Its per-row
     GTT/paired/orphan state badge keeps rendering unchanged on all 3 views (operator-confirmed:
     keep showing it on the new unpaired Legs view too — useful signal even though this view
     doesn't visually group by it).
4. Update 2 e2e specs that assert `role="tab"` on this control (will break once it's a
   `Select`, since the card's own `<CardHeader title="Positions">` already carries an
   independent accessible label so this isn't an accessibility regression, just a selector
   change): `frontend/e2e/cardheader_sweep_round3.spec.js:177-184`,
   `frontend/e2e/cardheader_adoption_sweep.spec.js:292,320`.

---

## Issue 4 — NavStrip "PE" (Positions Expiry P&L) pill shows "0" instead of "—" pre-load

**Root cause (confirmed):** `frontend/src/lib/data/portfolioStore.svelte.js:569` —
`_EMPTY_POSITIONS.total.exp_pnl` defaults to `0` (sibling `day_pnl` on the same line was
already fixed to `null` in a prior "real-money fix"; `exp_pnl` was left at 0 as out-of-scope
at the time). This is the only one of the three (`exp_pnl`/`extrinsic`/`prev_mv`) actually
rendered through a visible `fmtMoney` pill (`PositionStrip.svelte:813`).

**Fix:** change `exp_pnl: 0` → `exp_pnl: null` on that line. Do **not** touch
`extrinsic`/`prev_mv` (confirmed via repo-wide grep: neither is read through any visible
money pill — out of reported scope). Both external read sites
(`PositionStrip.svelte:701` via `tickFlash`, `:813` via `fmtMoney`) are already null-safe —
no downstream guard needed.

**Required test update (not just addition):** `frontend/src/lib/__tests__/data/portfolioAggregatesTriggers.test.js:240-247`
currently pins the *old, buggy* value via a source regex
(`exp_pnl:\s*0`) — update the regex to `exp_pnl:\s*null` and amend the test title/comment to
reflect that `exp_pnl` is now null while `extrinsic`/`prev_mv` remain the genuine 0 defaults.
Also update the JSDoc type at `frontend/src/lib/data/positionsDerivedStore.svelte.js:10` to
`exp_pnl: number|null`.

---

## Issue 5 — Order pill never shows (or updates with chase re-quotes) its limit price

**Root cause (confirmed):** `OrderCard.svelte:116` — `_filled = order?.average_price ?? order?.fill_price ?? null`.
A resting/OPEN Kite order genuinely reports `average_price: 0.0` (non-optional float,
backend-coerced via `float(... or 0)`, confirmed `backend/api/schemas.py:809`,
`backend/api/routes/orders.py:185`) — `??` only falls through on null/undefined, so `_filled`
becomes `0`, and the display chip (`:235`) always prefers `_filled` over the correctly
chase-updating `_limit` whenever `_filled != null` — which is always true once it's `0`. This
permanently masks the limit price for the entire time an order stays OPEN.

**Fix:** `OrderCard.svelte:116` and the equivalent read inside `_slippage()` (~line 129) —
treat a non-positive average_price/fill_price as absent:
```js
const _filled = $derived(
  (Number(order?.average_price) > 0) ? Number(order.average_price)
  : (Number(order?.fill_price) > 0) ? Number(order.fill_price)
  : null
);
```
Same `> 0` guard pattern inside `_slippage()`'s `avg`/`fp` reads. Confirmed safe for the
terminal-fill case (a real COMPLETE/FILLED order's average price is always positive) and
fixes a second, lower-exposure bug for free (a FILLED row with a `0` average_price mid-sync
race was computing a wrong negative slippage).

**Explicitly out of scope, flagged only:** once a genuine partial fill lands on a still-OPEN
order, this fix will show the real partial average price rather than the chase-updated limit
(same precedence as before this fix for any nonzero value) — if the operator wants the limit
visible even mid-partial-fill, that needs an additional status-based display gate. Not
requested; not included here.

---

## Tests (addition/update per issue — per this project's test-coverage convention)

- **Issue 1:** new Playwright e2e case — select Account A, add leg, switch to Account B, add
  another leg, assert both legs/pills carry their own correct account. Extend an existing
  basket/chain e2e spec rather than a new file if one already covers basket-building flow.
- **Issue 2:** new/extended Playwright assertion — stepping a staged pill's qty down to 0 via
  "−" removes it from the basket (pill disappears). Check `frontend/e2e/chain_grid_and_basket_step_styling.spec.js` for a natural extension point (confirmed unaffected by this JS change, but is the closest existing spec touching this selector).
- **Issue 3:** (a) Vitest unit test for `sortRowsByAccountOnly` in
  `frontend/src/lib/__tests__/data/` (account grouping + within-account ordering). (b) update
  the 2 e2e specs flagged above (`cardheader_sweep_round3.spec.js`,
  `cardheader_adoption_sweep.spec.js`) to assert against the new Select control instead of
  `role="tab"`. (c) new/extended e2e: switching the dropdown between all 3 options shows the
  right row set (paired-grouped vs unpaired-account-sorted vs exp-close).
- **Issue 4:** **update** (not just add) `portfolioAggregatesTriggers.test.js`'s pinned-value
  test per above — this is a required correction of an existing test that currently asserts
  the buggy value, not a fresh addition.
- **Issue 5:** **update** `__tests__/orderCardLimitPrice.test.js` (existing, from the
  2026-10-29 `current_limit` fix) to add a case for an OPEN order with `average_price: 0` —
  assert the chip shows `_limit`, not "0.00" — plus a case confirming a genuine partial/terminal
  fill with positive `average_price` still displays correctly.

## Done when

- All 5 fixes implemented in their own commits (disjoint files, can be parallel-dispatched).
- Each commit's test file(s) — new or updated per above — pass.
- `npx svelte-check --output machine` clean.
- Manual spot-check on dev: switch accounts mid-basket-build (issue 1), zero out a staged pill
  via the generic stepper (issue 2), confirm the derivatives dropdown shows 3 options with
  counts and the default view is unpaired/account-sorted (issue 3), confirm the NavStrip PE
  pill shows "—" before first load (issue 4), confirm an open order's pill shows and updates
  its limit price through a chase re-quote (issue 5).
