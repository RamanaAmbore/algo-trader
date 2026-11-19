/**
 * orderCardLimitPrice.test.js
 *
 * 2026-10-29 live operator report: OrderCard showed "0" for the limit
 * price once chase had re-quoted an order. Root cause — `_limit` read
 * only `order?.price ?? order?.initial_price`, which are the order's
 * ORIGINAL submission price and go stale the moment chase cancels and
 * replaces at a new price. `current_limit` (chase's live re-quoted
 * price, written on every cancel-and-replace) was never read here, even
 * though OrderBook.svelte/LogPanel.svelte already merge it onto the row
 * before OrderCard ever sees it, and the sibling ChaseCard.svelte
 * already prioritizes it the same way.
 *
 * No @testing-library/svelte in this repo for OrderCard (see
 * orderCardOriginChip.test.js's own note) — same static-source-grep
 * pattern.
 *
 * Five quality dimensions per feedback_test_dimensions.md:
 *  1. SSOT   — asserts the SAME priority order ChaseCard.svelte already
 *              uses (current_limit ?? initial_price), not a new formula
 *  2. Perf   — pure text/regex, no DOM, no network
 *  3. Stale  — explicitly guards against the pre-fix shape (price ??
 *              initial_price with no current_limit at all) reappearing
 *  4. Reuse  — no new derived/helper invented; one priority chain
 *  5. UX     — `_limit` still falls through to null (renders '—'), never
 *              coerces to 0, when none of the three fields are present
 */

import { describe, it, expect } from 'vitest';
import SRC from '../order/OrderCard.svelte?raw';

const scriptStart = SRC.indexOf('<script');
const scriptEnd = SRC.indexOf('</script>');
const script = SRC.slice(scriptStart, scriptEnd);

describe('OrderCard.svelte — limit price reflects chase re-quotes', () => {
  it('reads order.current_limit ahead of order.price / order.initial_price', () => {
    const idx = script.indexOf('const _limit');
    expect(idx).toBeGreaterThan(-1);
    const line = script.slice(idx, script.indexOf('\n', idx));
    expect(line).toMatch(/order\?\.current_limit\s*\?\?\s*order\?\.price\s*\?\?\s*order\?\.initial_price\s*\?\?\s*null/);
  });

  it('does not fall back to price before checking current_limit (pre-fix regression guard)', () => {
    const idx = script.indexOf('const _limit');
    const line = script.slice(idx, script.indexOf('\n', idx));
    const currentLimitPos = line.indexOf('current_limit');
    const pricePos = line.indexOf('order?.price');
    expect(currentLimitPos).toBeGreaterThan(-1);
    expect(pricePos).toBeGreaterThan(-1);
    expect(currentLimitPos).toBeLessThan(pricePos);
  });

  it('never coerces an absent limit to 0 — falls through to null', () => {
    const idx = script.indexOf('const _limit');
    const line = script.slice(idx, script.indexOf('\n', idx));
    expect(line).toMatch(/\?\?\s*null\s*\)\s*;?\s*$/);
  });
});

/**
 * 2026-11-18 follow-up (PLAN.md Issue 5) — a resting/OPEN order genuinely
 * reports `average_price: 0.0` (non-optional float, backend-coerced via
 * `float(... or 0)`). The old `_filled` read
 * (`order?.average_price ?? order?.fill_price ?? null`) only falls through
 * on null/undefined, so `0` always won, and the price chip
 * (`_filled != null ? priceFmt(_filled) : _limit != null ? priceFmt(_limit)
 * : '—'`) always preferred the literal "0.00" over the correctly
 * chase-updating `_limit` for the entire time an order stayed OPEN.
 *
 * These tests extract the REAL `_filled` expression text from the source
 * (not a hand-written shadow copy) and evaluate it against sample order
 * objects, so a drift between this test and the shipped formula is
 * impossible — a regression to the plain-`??` shape would make the first
 * two assertions below fail directly.
 */
describe('OrderCard.svelte — _filled treats non-positive average_price as absent', () => {
  /** Pulls the real `(Number(...) ...)` expression out of the source and
   *  compiles it into a callable so the test exercises the shipped logic
   *  verbatim, not a reimplementation that could silently diverge. */
  function _realFilledFn() {
    const idx = script.indexOf('const _filled');
    expect(idx).toBeGreaterThan(-1);
    const line = script.slice(idx, script.indexOf('\n', idx));
    const exprMatch = line.match(/\$derived\((.*)\);?\s*$/);
    expect(exprMatch).not.toBeNull();
    // eslint-disable-next-line no-new-func
    return new Function('order', `return (${exprMatch[1]});`);
  }

  it('uses the Number(...) > 0 guard, not a plain ?? check, on average_price/fill_price', () => {
    const idx = script.indexOf('const _filled');
    const line = script.slice(idx, script.indexOf('\n', idx));
    expect(line).toMatch(/Number\(order\?\.average_price\)\s*>\s*0/);
    expect(line).toMatch(/Number\(order\?\.fill_price\)\s*>\s*0/);
    // Pre-fix regression guard — the old plain-??-coerces-0 shape must be gone.
    expect(line).not.toMatch(/order\?\.average_price\s*\?\?\s*order\?\.fill_price\s*\?\?\s*null/);
  });

  it('an OPEN order with average_price: 0 and a populated current_limit renders the limit, not "0.00"', () => {
    const filledFn = _realFilledFn();
    const order = {
      status: 'OPEN',
      average_price: 0,
      fill_price: null,
      current_limit: 123.45,
      price: 100,
      initial_price: 100,
    };
    const filled = filledFn(order);
    const limit = order.current_limit ?? order.price ?? order.initial_price ?? null;
    expect(filled).toBeNull();
    // Reproduces the exact chip ternary from the markup — with `_filled`
    // null, the chip falls through to `_limit`, never renders "0.00".
    const rendered = filled != null ? String(filled) : limit != null ? String(limit) : '—';
    expect(rendered).toBe('123.45');
    expect(rendered).not.toBe('0');
  });

  it('a genuine partial/terminal fill with a positive average_price still displays the fill price', () => {
    const filledFn = _realFilledFn();
    const order = {
      status: 'FILLED',
      average_price: 587.25,
      fill_price: null,
      current_limit: 590,
      price: 590,
      initial_price: 590,
    };
    const filled = filledFn(order);
    expect(filled).toBe(587.25);
    const rendered = filled != null ? String(filled) : '—';
    expect(rendered).toBe('587.25');
  });

  it('falls back to fill_price (also guarded) when average_price is absent/non-positive', () => {
    const filledFn = _realFilledFn();
    expect(filledFn({ average_price: 0, fill_price: 412.1 })).toBe(412.1);
    expect(filledFn({ average_price: null, fill_price: 0 })).toBeNull();
    expect(filledFn({ average_price: undefined, fill_price: undefined })).toBeNull();
  });
});
