// 2026-10 P0 audit fix — OrderCard.svelte previously carried its own
// `:global(.algo-status-card[data-status="…"])` block (and a duplicate
// `:global(.algo-status-pill)` base rule), which — because `:global()`
// escapes Svelte's component CSS scoping — repainted EVERY
// `.algo-status-card` in the app, not just order cards. Confirmed victim:
// the /automation agent cards (unrelated surface), which picked up
// OrderCard's different literal status colors whenever OrderCard's
// styles happened to load after app.css. Fix: deleted the duplicate
// block entirely; OrderCard now relies on app.css's shared
// `.algo-status-pill` + `.algo-status-card[data-status="…"]` SSOT. The
// one genuinely OrderCard-local status ("held") was moved into app.css's
// shared block rather than kept as a local override.
//
// Source-level guard, same pattern as held_orders_card_styling.spec.js /
// automation_agent_row_card_styling.spec.js.

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const orderCardSrc = readFileSync(
  new URL('../src/lib/order/OrderCard.svelte', import.meta.url).pathname, 'utf8'
);
const appCss = readFileSync(
  new URL('../src/app.css', import.meta.url).pathname, 'utf8'
);

test.describe('OrderCard.svelte — status color SSOT lives in app.css only', () => {
  test('OrderCard.svelte no longer declares a :global(.algo-status-card[data-status=…]) rule', () => {
    expect(orderCardSrc).not.toMatch(/:global\(\.algo-status-card\[data-status=/);
  });

  test('OrderCard.svelte no longer declares a :global(.algo-status-pill) base rule', () => {
    expect(orderCardSrc).not.toMatch(/:global\(\.algo-status-pill\)/);
  });

  test('OrderCard.svelte still renders .algo-status-card / .algo-status-pill in markup (class names preserved)', () => {
    expect(orderCardSrc).toMatch(/class="algo-status-card/);
    expect(orderCardSrc).toMatch(/class="algo-status-pill/);
  });

  test('app.css carries a "held" status variant in the shared .algo-status-card block', () => {
    const rule = appCss.match(/\.algo-status-card\[data-status="held"\]\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).not.toBe('');
    expect(rule).toMatch(/--st-fg:\s*#c084fc/);
    expect(rule).toMatch(/--st-bg:/);
    expect(rule).toMatch(/--st-border:/);
  });

  test('app.css shared block still covers every status OrderCard._statusDataAttr can emit', () => {
    // complete / rejected / cancelled / held / running / error / inactive
    for (const status of ['complete', 'rejected', 'cancelled', 'held', 'running', 'error', 'inactive']) {
      const rule = appCss.match(new RegExp(`\\.algo-status-card\\[data-status="${status}"\\]\\s*\\{[^}]*\\}`));
      expect(rule, `app.css missing .algo-status-card[data-status="${status}"]`).not.toBeNull();
    }
  });

  test('app.css error variant stays red (not OrderCard\'s old orange), matching layout.svelte\'s red error border', () => {
    const rule = appCss.match(/\.algo-status-card\[data-status="error"\]\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).toMatch(/--st-fg:\s*#f87171/);
  });
});
