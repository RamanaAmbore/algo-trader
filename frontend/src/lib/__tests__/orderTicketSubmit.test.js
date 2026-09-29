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
import { nextTriggerState, formatSubmitLabel } from '$lib/order/orderTicketSubmit.js';

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
