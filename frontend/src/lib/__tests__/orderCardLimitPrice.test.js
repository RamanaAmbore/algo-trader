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
