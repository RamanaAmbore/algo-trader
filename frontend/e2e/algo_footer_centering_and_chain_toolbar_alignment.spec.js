/**
 * algo_footer_centering_and_chain_toolbar_alignment.spec.js
 *
 * Guards three small fixes from the same session (2026-11-03):
 *
 *  1. Footer centering — `.algo-footer` on every algo page was never
 *     centered; `flex-start` just left-packed the brand/page-name/
 *     credits row with the leftover space trailing on the right. Fixed
 *     with flex auto-margins (`margin-left: auto` on `.algo-footer-
 *     brand`, `margin-right: auto` on the footer's own last child) —
 *     these absorb free space equally on both sides when content fits,
 *     and resolve to 0 the instant content overflows, so the existing
 *     "first item never clipped on mobile" guarantee is unaffected.
 *     Measured directly at desktop width (no content overflow).
 *
 *  2. Chain-tab Expiry row left-alignment — `.oct-toolbar`'s own left
 *     padding (0.1rem) was an outlier against every other row in the
 *     same modal (`.oes-picker`, `.oes-tabs`, `.oes-chase-row`, all
 *     0.4rem), pulling the Expiry label visibly left of CHASE/BRACKET/
 *     DRAFT below it. Source-scan (geometry-only, no live quote data
 *     needed to confirm the CSS value itself).
 *
 *  3. Order Ticket depth-ladder bid/ask flash parity — `OrderDepth.svelte`
 *     had no tick-flash at all, unlike the grid surfaces (Pulse/Legs)
 *     which already flash via `createTickFlash`. Source-scan for the
 *     wiring (poll-driven `update()` calls + `classOf()` reads on the
 *     bid/ask cells) — a live flash assertion would need real streaming
 *     quote data to trigger a genuine price change within the test
 *     window, which the rest of this suite's precedent (see
 *     `chain_live_wing_confirmation_badge.spec.js`) treats as
 *     impractical in CI.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
const DIR = import.meta.dirname ?? new URL('.', import.meta.url).pathname;
const LAYOUT_PATH = path.resolve(DIR, '../src/routes/(algo)/+layout.svelte');
const CHAIN_TAB_PATH = path.resolve(DIR, '../src/lib/order/OptionChainTab.svelte');
const ORDER_DEPTH_PATH = path.resolve(DIR, '../src/lib/order/OrderDepth.svelte');

test.describe('Algo footer centering — live measurement, desktop', () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test('the footer content block sits centered — equal left/right gaps to the viewport edges', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded', timeout: 30_000 });
    const footer = page.locator('.algo-footer');
    await expect(footer).toBeVisible({ timeout: 15_000 });

    const gaps = await footer.evaluate((el) => {
      const footerRect = el.getBoundingClientRect();
      const first = el.firstElementChild;
      const last = el.lastElementChild;
      const firstRect = first.getBoundingClientRect();
      const lastRect = last.getBoundingClientRect();
      return {
        left: +(firstRect.left - footerRect.left).toFixed(1),
        right: +(footerRect.right - lastRect.right).toFixed(1),
        overflowed: el.scrollWidth > el.clientWidth + 1,
      };
    });

    // Content must fit at this width — this assertion is about the
    // centered case. If this ever starts overflowing at 1440px, the
    // auto-margin mechanism correctly degrades to flex-start (by
    // design — see the CSS comment on `.algo-footer`), which is a
    // DIFFERENT, already-covered case (mobile clipping guard, commit
    // b8e0ae0f), not a regression of this fix.
    expect(gaps.overflowed, 'footer content unexpectedly overflows at 1440px').toBe(false);
    // Tight tolerance — equal auto-margin split, not an exact-px assertion.
    expect(Math.abs(gaps.left - gaps.right), `left gap ${gaps.left}px vs right gap ${gaps.right}px`).toBeLessThanOrEqual(2);
    // Both gaps must be genuinely non-trivial — proves this isn't just
    // two near-zero numbers that happen to be "equal" because nothing
    // moved (the pre-fix flex-start state had left≈0, right≈large).
    expect(gaps.left, 'left gap too small — looks like pre-fix flex-start').toBeGreaterThan(10);
  });
});

test.describe('Chain Expiry-row alignment + OrderDepth flash parity (source-scan)', () => {
  let chainSrc = '';
  let depthSrc = '';

  test.beforeAll(() => {
    chainSrc = readFileSync(CHAIN_TAB_PATH, 'utf8');
    depthSrc = readFileSync(ORDER_DEPTH_PATH, 'utf8');
  });

  test('.oct-toolbar left padding matches the modal-wide 0.4rem convention, not the old 0.1rem outlier', () => {
    const idx = chainSrc.indexOf('.oct-toolbar {');
    expect(idx).toBeGreaterThan(-1);
    const rule = chainSrc.slice(idx, chainSrc.indexOf('}', idx));
    expect(rule).toMatch(/padding:\s*0\.25rem 0\.4rem 0\.35rem/);
    expect(rule).not.toMatch(/padding:\s*0\.25rem 0\.1rem/);
  });

  test('OrderDepth.svelte imports createTickFlash and drives it from its own poll, not a separate effect', () => {
    expect(depthSrc).toContain("import { createTickFlash } from '$lib/data/tickFlash.svelte.js';");
    expect(depthSrc).toContain('const _depthFlash = createTickFlash(');
    const pollIdx = depthSrc.indexOf('async function poll()');
    expect(pollIdx).toBeGreaterThan(-1);
    const pollBody = depthSrc.slice(pollIdx, depthSrc.indexOf('\n  }', pollIdx));
    expect(pollBody).toMatch(/_depthFlash\.update\(`\$\{symbol\}:bid:\$\{i\}`/);
    expect(pollBody).toMatch(/_depthFlash\.update\(`\$\{symbol\}:ask:\$\{i\}`/);
  });

  test('the bid/ask cells read _depthFlash.classOf(), keyed by symbol so a symbol swap never flashes against a stale baseline', () => {
    // Svelte markup interpolation is single `{...}`, not a `${...}`
    // template literal — the expression itself still builds its key with
    // a real JS template literal inside those braces.
    expect(depthSrc).toMatch(/ot-depth-bid \{_depthFlash\.classOf\(`\$\{symbol\}:bid:\$\{i\}`\)\}/);
    expect(depthSrc).toMatch(/ot-depth-ask \{_depthFlash\.classOf\(`\$\{symbol\}:ask:\$\{i\}`\)\}/);
  });

  test('OrderDepth disposes its flash instance on destroy (no leaked timers)', () => {
    const idx = depthSrc.indexOf('onDestroy(() => {');
    expect(idx).toBeGreaterThan(-1);
    const body = depthSrc.slice(idx, depthSrc.indexOf('});', idx));
    expect(body).toContain('_depthFlash.dispose();');
  });
});
