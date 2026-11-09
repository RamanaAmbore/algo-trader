// Sprint 4 (H6) — hold UI: HELD badge and held-exit warning on the order card, and release-all with confirm.
//
// Source-level guard, no server or auth needed. Reads the real component sources.
//
// Five quality dimensions:
//   1. SSOT    — hold state comes from the order row's hold_json (backend field), not a copy.
//   2. Perf    — pure fs read, no browser.
//   3. Stale   — guards the wiring so a refactor cannot silently drop the badge or confirm step.
//   4. Reuse   — uses the shared ConfirmModal, as the other destructive actions do.
//   5. UX      — release-all asks first and reports how many were released or refused.

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const read = (rel) => readFileSync(new URL(rel, import.meta.url).pathname, 'utf8');
const card = read('../src/lib/order/OrderCard.svelte');
const held = read('../src/lib/HeldOrdersCard.svelte');
const orderInfo = read('../../backend/api/routes/orders_helpers.py');

test.describe('hold UI (H6)', () => {
  test('order list exposes hold_json to the card', () => {
    expect(orderInfo).toMatch(/hold_json: str \| None = None/);
    // getattr(..., None) form (2026-10 P1 defensive fix) — still sources
    // from the row's own hold_json field, not a copy.
    expect(read('../../backend/api/routes/orders.py')).toMatch(
      /hold_json=getattr\(r,\s*"hold_json",\s*None\),/
    );
  });

  test('order card shows a Pending badge from hold_json', () => {
    // 2026-11-02 (PLAN.md item 3) — "HELD" renamed to "Pending" on the
    // chip; "held" already means something else (covered-call leg label
    // elsewhere in the app). The CSS class `held-badge`/`held` stays
    // unchanged (internal selector, not operator-visible).
    expect(card).toMatch(/const holdInfo = \$derived\(parseHold\(order\.hold_json\)\)/);
    expect(card).toMatch(/\{#if holdInfo\}[\s\S]*Pending<\/span>/);
  });

  test('order card no longer warns about a held template exit (removed 2026-11 T3.4 — Bracket removed)', () => {
    expect(card).not.toMatch(/holdInfo\?\.category === 'template_exit'/);
    expect(card).not.toMatch(/Unprotected until released/);
  });

  test('release-all is guarded by the shared confirm modal', () => {
    expect(held).toMatch(/import ConfirmModal from '\$lib\/ConfirmModal\.svelte'/);
    expect(held).toMatch(/await confirmRef\?\.ask\(\{/);
    expect(held).toMatch(/if \(!ok\) return;/);
  });

  test('release-all calls the per-order release endpoint for each row and reports the result', () => {
    expect(held).toMatch(/for \(const row of \[\.\.\.rows\]\)/);
    expect(held).toMatch(/await releaseHeldOrder\(row\.id\);/);
    expect(held).toMatch(/Released \$\{released\}/);
  });
});
