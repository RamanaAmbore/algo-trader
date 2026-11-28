/**
 * orderTicketSubmit.test.js — Vitest coverage for the D4 + R7 pure-logic
 * helpers in orderTicketSubmit.js. The full helper suite (numericOverride,
 * classifyIntent, buildModifyPayload, buildOnSubmitPayload, buildPlacePayload,
 * formatPlacementOk, and these two) is the SSOT test file at
 * `frontend/scripts/orderTicketSubmit.test.js` (Node test runner, `node
 * --test`) — this file exists so `npx vitest run` (the project's canonical
 * frontend gate) also exercises the two functions added for this fix,
 * without duplicating the entire pre-existing suite.
 *
 * Five quality dimensions per feedback_test_dimensions.md:
 *  1. SSOT  — imports the same module as scripts/orderTicketSubmit.test.js;
 *             no logic duplicated, only re-asserted under a second runner
 *  2. Perf  — pure unit, no I/O, no component mount
 *  3. Stale — nextTriggerState reproduces the EXACT D4 regression sequence
 *             (submitting flips false on an already-seen trigger)
 *  4. Reuse — same module OrderTicket.svelte + SymbolPanel.svelte import
 *  5. UX    — formatSubmitLabel covers ADD/CLOSE verb derivation for both
 *             long and short positions, and the "no bare-Submit while
 *             chase is on" regression
 */

import { describe, it, expect } from 'vitest';
import {
  nextTriggerState, formatSubmitLabel, buildPlacePayload, marginChipMode,
} from '$lib/order/orderTicketSubmit.js';

describe('nextTriggerState (D4 — atomic submit-trigger guard)', () => {
  it('never fires on the initial render (prevSeen=-1)', () => {
    expect(nextTriggerState(-1, 0, false)).toEqual({ fire: false, seen: 0 });
  });

  it('fires on a genuine new trigger value when not submitting', () => {
    expect(nextTriggerState(5, 6, false)).toEqual({ fire: true, seen: 6 });
  });

  it('does not fire while a submit is already in flight', () => {
    expect(nextTriggerState(5, 6, true)).toEqual({ fire: false, seen: 6 });
  });

  it('regression: a rerun on an already-seen trigger never re-fires, even after submitting flips back to false', () => {
    // Click 1 fires.
    let state = nextTriggerState(5, 6, false);
    expect(state.fire).toBe(true);
    let lastSeen = state.seen;

    // Submit is in flight; some other dependency reruns the effect against
    // the SAME trigger value.
    state = nextTriggerState(lastSeen, 6, true);
    expect(state.fire).toBe(false);
    lastSeen = state.seen;

    // submitting flips back to false — this is exactly where the old buggy
    // effect (which only updated its "last seen" counter on the branch that
    // did NOT early-return) would re-fire a duplicate order.
    state = nextTriggerState(lastSeen, 6, false);
    expect(state.fire).toBe(false);
  });

  it('a deliberate second click (new trigger value) after the first completes does fire', () => {
    const first = nextTriggerState(5, 6, false);
    expect(nextTriggerState(first.seen, 7, false).fire).toBe(true);
  });
});

describe('formatSubmitLabel (R7 — Submit button reflects the real pending action)', () => {
  // Chain tab (basket legs staged) — operator (2026-09-29): "in chain
  // the button label should submit". Plain label, no leg count.
  it('basket mode shows plain "Submit"', () => {
    expect(formatSubmitLabel({ side: 'BUY', currentQty: 0, qty: 5, basketCount: 2 }))
      .toBe('Submit');
  });

  it('no side picked → bare "Submit"', () => {
    expect(formatSubmitLabel({ side: null, currentQty: 0, qty: 0, basketCount: 0 }))
      .toBe('Submit');
  });

  // Lot size dropped from the label — operator (2026-09-29): "remove lot
  // size from submit buttons in order ticket". "Submit" prefix dropped
  // entirely — operator (2026-09-30): "for order ticket the submit
  // button should not have Submit in the label".
  it('cold ticket with side — no "Submit" prefix, no qty', () => {
    expect(formatSubmitLabel({ side: 'BUY', currentQty: 0, qty: 75, basketCount: 0 }))
      .toBe('BUY');
  });

  it('long position + SELL → CLOSE', () => {
    expect(formatSubmitLabel({ side: 'SELL', currentQty: 75, qty: 75, basketCount: 0 }))
      .toBe('CLOSE/SELL');
  });

  it('long position + BUY → ADD', () => {
    expect(formatSubmitLabel({ side: 'BUY', currentQty: 75, qty: 75, basketCount: 0 }))
      .toBe('ADD/BUY');
  });

  it('short position + BUY → CLOSE', () => {
    expect(formatSubmitLabel({ side: 'BUY', currentQty: -75, qty: 75, basketCount: 0 }))
      .toBe('CLOSE/BUY');
  });

  it('short position + SELL → ADD', () => {
    expect(formatSubmitLabel({ side: 'SELL', currentQty: -75, qty: 75, basketCount: 0 }))
      .toBe('ADD/SELL');
  });

  // Regression: this function takes no chase parameter at all — the caller
  // no longer special-cases chase-on tickets to a bare "Submit" (the exact
  // scenario behind "close buy close sell buttons don't work", since chase
  // is the default for LIMIT/SL tickets). Still true post-2026-09-30: a
  // resolved side always yields real verb/side text, never a bare "Submit".
  it('label is always descriptive — no bare-Submit carve-out for any resolved-side state', () => {
    const label = formatSubmitLabel({ side: 'SELL', currentQty: 75, qty: 75, basketCount: 0 });
    expect(label).not.toBe('Submit');
  });
});

// Sprint 2b (docs/proposals/SPRINT2_LAYER_INTEGRATION.md §3/§4.4) — source
// vocabulary plumbing. OrderTicket.svelte's only placement call site is
// buildPlacePayload(); the request body must carry an explicit 'ticket'
// source so the backend's AlgoOrder.source column (and the OrderCard
// "Origin" chip it feeds, see OrderCard.svelte) is populated instead of
// silently falling through to the backend default.
describe('buildPlacePayload — source tagging (Sprint 2b)', () => {
  const ctx = /** @type {any} */ ({
    mode: 'paper',
    side: 'BUY',
    resolvedSymbol: 'RELIANCE',
    symbol: 'RELIANCE',
    exchange: 'NSE',
    resolvedExchange: 'NSE',
    qty: 1,
    lots: 1,
    lotSize: 1,
    currentQty: 0,
    product: 'CNC',
    type: 'LIMIT',
    variety: 'regular',
    validity: 'DAY',
    showLimit: true,
    showTrigger: false,
    roundToTick: (v) => Number(v),
    price: '2500',
    trigger: '',
    account: 'ZG0790',
    chase: false,
    chaseAgg: 'low',
    strategyId: null,
  });

  it('always tags source="ticket" on the outgoing request', () => {
    expect(buildPlacePayload(ctx).source).toBe('ticket');
  });
});

// Draft cleanup (2026-10) — draft_id lets the backend delete the
// originating payoffDrafts row ONLY on confirmed success (see
// TicketOrderRequest.draft_id, backend/api/schemas.py). Regression
// guard for the "delete-before-confirmation" bug: the frontend must
// forward whatever draftId OrderTicket.svelte threads in verbatim —
// null/undefined collapse to null (never omitted, never 0-coerced).
describe('buildPlacePayload — draft_id threading (2026-10 Bug A fix)', () => {
  const baseCtx = /** @type {any} */ ({
    mode: 'paper', side: 'BUY', resolvedSymbol: 'RELIANCE', symbol: 'RELIANCE',
    exchange: 'NSE', resolvedExchange: 'NSE', qty: 1, lots: 1, lotSize: 1,
    currentQty: 0, product: 'CNC', type: 'LIMIT', variety: 'regular', validity: 'DAY',
    showLimit: true, showTrigger: false, roundToTick: (v) => Number(v),
    price: '2500', trigger: '', account: 'ZG0790', chase: false, chaseAgg: 'low',
    strategyId: null,
  });

  it('carries a real draftId through as draft_id', () => {
    expect(buildPlacePayload({ ...baseCtx, draftId: 42 }).draft_id).toBe(42);
  });

  it('collapses an omitted draftId to null (no field dropped)', () => {
    expect(buildPlacePayload(baseCtx).draft_id).toBeNull();
  });

  it('collapses an explicit null draftId to null', () => {
    expect(buildPlacePayload({ ...baseCtx, draftId: null }).draft_id).toBeNull();
  });
});

// marginChipMode (2026-11 real-money audit fix, operator-corrected) —
// cash/margin × open/close chip-label mode. SSOT coverage lives in
// scripts/orderTicketSubmit.test.js; mirrored here (same 5 cases) so the
// project's canonical `npx vitest run` gate also exercises it directly.
//
// Five quality dimensions per feedback_test_dimensions.md:
//  1. SSOT  — imports the same orderTicketSubmit.js module, no duplicated logic
//  2. Perf  — pure unit, no I/O, no component mount
//  3. Stale — pins the operator's own correction ("when you are short and
//             when close it you release margin") against an earlier,
//             wrong draft of this fix that labeled close-short as a cost
//  4. Reuse — same module OrderTicket.svelte + SymbolPanel.svelte import
//  5. UX    — covers all four cases the margin/cash chip renders, plus the
//             equity and futures carve-outs
describe('marginChipMode (2026-11 real-money audit fix)', () => {
  it('open long (BUY, no position) → cash_cost', () => {
    expect(marginChipMode({ isEquity: false, kind: 'CE', side: 'BUY', currentQty: 0 }))
      .toBe('cash_cost');
  });

  it('open short (SELL, no position) → margin_required', () => {
    expect(marginChipMode({ isEquity: false, kind: 'PE', side: 'SELL', currentQty: 0 }))
      .toBe('margin_required');
  });

  it('close long (SELL, currentQty>0) → cash_released', () => {
    expect(marginChipMode({ isEquity: false, kind: 'CE', side: 'SELL', currentQty: 20 }))
      .toBe('cash_released');
  });

  it('close short (BUY, currentQty<0) → margin_released — NOT cost/debit', () => {
    // Operator's own correction, verbatim: "when you are short and when
    // close it you release margin." An earlier draft of this fix wrongly
    // returned a cost/debit-style classification here.
    const mode = marginChipMode({ isEquity: false, kind: 'PE', side: 'BUY', currentQty: -20 });
    expect(mode).toBe('margin_released');
    expect(mode).not.toBe('cash_cost');
  });

  // Fix #3 (2026-11 audit follow-up) — equity now follows the same
  // open-vs-close branching as options: a SELL to close an existing long
  // releases cash. It is no longer a flat cash_cost for every direction.
  it('equity SELL to close an existing long → cash_released (fix #3)', () => {
    expect(marginChipMode({ isEquity: true, kind: 'EQ', side: 'SELL', currentQty: 100 }))
      .toBe('cash_released');
  });

  it('equity still never gets "margin_released" framing — no short-sell ' +
     'concept for equity in this app', () => {
    const mode = marginChipMode({ isEquity: true, kind: 'EQ', side: 'BUY', currentQty: 100 });
    expect(mode).toBe('cash_cost');
    expect(mode).not.toBe('margin_released');
  });

  it('a future stays margin_required — close framing is out of scope here', () => {
    expect(marginChipMode({ isEquity: false, kind: 'FUT', side: 'BUY', currentQty: -1 }))
      .toBe('margin_required');
  });
});

// marginChipMode magnitude awareness (2026-11 audit follow-up, fix #2) —
// classifyIntent only checks sign; an oversized "close" also opens a new
// position for the remainder, and the chip must reflect THAT resource,
// not a flat "released" label that overstates the credit and hides the
// margin requirement for the remainder.
//
// Five quality dimensions per feedback_test_dimensions.md:
//  1. SSOT  — same module as scripts/orderTicketSubmit.test.js
//  2. Perf  — pure unit, no I/O
//  3. Stale — pins the real-money risk this fix closes: a 3-lot SELL
//             against a 1-lot long must never read as a flat cash_released
//  4. Reuse — same orderQty containment rule (`<=`) spreadGate.js's
//             resolveLegHeldContext/isCloseIntentLeg already use
//  5. UX    — covers F&O long/short overshoot, equity overshoot, the
//             omitted-orderQty backward-compat path, and the exact-fit
//             boundary
describe('marginChipMode — magnitude-aware over-close (fix #2)', () => {
  it('long 1 lot, SELL exactly 1 lot (full close) → cash_released', () => {
    expect(marginChipMode({ isEquity: false, kind: 'CE', side: 'SELL', currentQty: 1, orderQty: 1 }))
      .toBe('cash_released');
  });

  it('long 1 lot, SELL 3 lots (oversized close, opens a 2-lot short) → margin_required', () => {
    expect(marginChipMode({ isEquity: false, kind: 'CE', side: 'SELL', currentQty: 1, orderQty: 3 }))
      .toBe('margin_required');
  });

  it('short 1 lot, BUY 3 lots (oversized close, opens a 2-lot long) → cash_cost', () => {
    expect(marginChipMode({ isEquity: false, kind: 'PE', side: 'BUY', currentQty: -1, orderQty: 3 }))
      .toBe('cash_cost');
  });

  it('equity long 10, SELL 25 (oversized close) → cash_cost, not cash_released', () => {
    expect(marginChipMode({ isEquity: true, kind: 'EQ', side: 'SELL', currentQty: 10, orderQty: 25 }))
      .toBe('cash_cost');
  });

  it('omitted orderQty preserves pre-fix sign-only classification (backward compat)', () => {
    expect(marginChipMode({ isEquity: false, kind: 'CE', side: 'SELL', currentQty: 1 }))
      .toBe('cash_released');
  });

  it('orderQty exactly equal to |currentQty| counts as a full close', () => {
    expect(marginChipMode({ isEquity: false, kind: 'PE', side: 'BUY', currentQty: -5, orderQty: 5 }))
      .toBe('margin_released');
  });
});
