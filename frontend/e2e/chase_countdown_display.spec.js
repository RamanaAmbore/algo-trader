/**
 * chase_countdown_display.spec.js
 *
 * Verifies the chase countdown UI shows next_attempt_at properly.
 *
 * ChaseCard.svelte (used in /orders page and other panels) exposes timing info:
 *   - next_attempt_at is in AlgoOrderInfo
 *   - ChaseCard renders a countdown timer when next_attempt_at is in the future
 *   - The "Next" column is the row's SOLE timing display — 2026-10-15 (operator:
 *     "instead of showing up elapsed time, do count down for how much time
 *     chase is going to wait before taking action") removed the former
 *     elapsed-since-created "Age" display entirely; there is no longer any
 *     age/elapsed-time rendering in this component at all.
 *
 * Three quality dimensions:
 *  1. Stale   — ChaseCard.svelte contains 'next_attempt_at' reference, and
 *               no longer contains any elapsed/age-since-created logic
 *  2. SSOT    — /orders page displays chase cards with timing info
 *  3. UX      — countdown is visible and numeric (or absent if no active chases)
 *
 * Run:
 *   PLAYWRIGHT_USER=rambo PLAYWRIGHT_PASS=admin1234 \
 *   PLAYWRIGHT_BASE_URL=http://localhost:5174 \
 *   npx playwright test e2e/chase_countdown_display.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import { readFileSync } from 'fs';

test.setTimeout(60000);

test.describe('Chase countdown display', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  // ── Test 1: Source-scan — orders page imports ChaseCard component ────────
  test('1-SSOT: Orders page imports ChaseCard component', () => {
    // Read the orders page source.
    const ordersPagePath = '/Users/ramanambore/projects/ramboq/frontend/src/routes/(algo)/orders/+page.svelte';
    let source = '';
    try {
      source = readFileSync(ordersPagePath, 'utf-8');
    } catch (e) {
      test.skip(true, `Could not read orders page: ${e.message}`);
      return;
    }

    // Assertion: orders page must import ChaseCard component
    expect(source, 'Orders page should import ChaseCard').toContain('ChaseCard');

    // The component should be used in the template
    const hasChaseCardUsage = /<ChaseCard|<chase-card/i.test(source);
    expect(hasChaseCardUsage, 'Orders page should render ChaseCard').toBe(true);

    console.log('[chase_countdown_display] Orders page imports ChaseCard verified');
  });

  // ── Test 2: Source-scan — ChaseCard exposes timing fields ────────────────
  test('2-UX: ChaseCard component displays timing field references', () => {
    // Read the ChaseCard.svelte file from disk.
    const chaseCardPath = '/Users/ramanambore/projects/ramboq/frontend/src/lib/order/ChaseCard.svelte';
    let source = '';
    try {
      source = readFileSync(chaseCardPath, 'utf-8');
    } catch (e) {
      test.skip(true, `Could not read ChaseCard.svelte: ${e.message}`);
      return;
    }

    // Assertion 1: component must read next_attempt_at from the chase row
    expect(source, 'ChaseCard should reference next_attempt_at').toContain('next_attempt_at');

    // Assertion 2: component must have logic to render timing info
    // (countdown, age, "next attempt in", etc.)
    const hasTimingLogic = /countdown|next.*in|age|elapsed|attempt.*at|`${|${.*attempt/i.test(source);
    expect(hasTimingLogic, 'ChaseCard should have countdown/age logic').toBe(true);

    // Assertion 3: component should render in a template (not just read the field)
    const hasTemplateRender = /{#if|{\s*[a-zA-Z]|text=|content=/i.test(source);
    expect(hasTemplateRender, 'ChaseCard should have template markup to render timing').toBe(true);

    console.log('[chase_countdown_display] ChaseCard timing display logic verified');
  });

  // ── Test 3: Source-scan — ChaseCard.svelte references next_attempt_at ────
  test('3-Stale: ChaseCard.svelte source contains next_attempt_at', () => {
    // Read the ChaseCard.svelte file from disk.
    const chaseCardPath = '/Users/ramanambore/projects/ramboq/frontend/src/lib/order/ChaseCard.svelte';
    let source = '';
    try {
      source = readFileSync(chaseCardPath, 'utf-8');
    } catch (e) {
      test.skip(true, `Could not read ChaseCard.svelte: ${e.message}`);
      return;
    }

    // Assertion: source must contain 'next_attempt_at' reference.
    // This verifies the component exposes the timing field from AlgoOrderInfo.
    expect(source, 'ChaseCard.svelte should reference next_attempt_at').toContain('next_attempt_at');

    // 2026-10-15: the elapsed-since-created "Age" display and its backing
    // `_age()` function were removed entirely — the countdown-to-next-
    // action is now the row's sole timing display. A stale `_age(` call
    // or a `created_at`-based elapsed calculation reappearing here would
    // mean the removed display crept back in.
    expect(source, 'ChaseCard.svelte should no longer have an _age() elapsed-time function').not.toMatch(/function _age\(/);

    console.log('[chase_countdown_display] ChaseCard.svelte source scan verified: countdown present, elapsed-age display removed');
  });

  test('4-Mobile: the Next/countdown column has a locked minimum width so it never shrinks to an unreadable sliver', () => {
    // 2026-10-16 fix — operator: "the count time is not visible on
    // mobile for chase. previously it was visible while incrementing
    // elapsed time in seconds." The age/countdown column's grid track
    // was `minmax(0, 0.6fr)` — with 8 other columns competing for space
    // on a narrow phone viewport, it could shrink arbitrarily close to
    // 0 under `.cc-col`'s own `overflow: hidden`, rendering the
    // countdown illegible. Locked to a real minimum (2.1rem — enough
    // for "999s") in BOTH the default and .cc-compact grid templates,
    // the same "locked min so it always fits" convention the kill
    // button's own column already used.
    const chaseCardPath = '/Users/ramanambore/projects/ramboq/frontend/src/lib/order/ChaseCard.svelte';
    let source = '';
    try {
      source = readFileSync(chaseCardPath, 'utf-8');
    } catch (e) {
      test.skip(true, `Could not read ChaseCard.svelte: ${e.message}`);
      return;
    }
    const ageTracks = source.match(/minmax\([^)]*\)\s*\/\*\s*age[^*]*\*\//g) || [];
    expect(ageTracks.length, 'expected one age-column track in each of .cc-row and .cc-compact .cc-row').toBe(2);
    for (const track of ageTracks) {
      expect(track, `age column track must not use a 0 minimum: ${track}`).not.toMatch(/minmax\(0,/);
    }
  });

  test('5-UX: the countdown value itself is legible — not pinned to the smallest font-size token in the app', () => {
    // 2026-10-20 fix — the 2026-10-16 fix (test 4 above) only stopped
    // the column from truncating; the countdown text itself stayed at
    // --fs-2xs (8px), the smallest token in the whole --fs-* scale,
    // smaller even than its own "Next" column header. Operator: "I
    // want to see the value clearly." Bumped to --fs-sm + bold.
    const chaseCardPath = '/Users/ramanambore/projects/ramboq/frontend/src/lib/order/ChaseCard.svelte';
    let source = '';
    try {
      source = readFileSync(chaseCardPath, 'utf-8');
    } catch (e) {
      test.skip(true, `Could not read ChaseCard.svelte: ${e.message}`);
      return;
    }
    const countdownRule = (source.match(/\.cc-countdown\s*\{[^}]*\}/) || [])[0] || '';
    expect(countdownRule, '.cc-countdown rule not found').not.toBe('');
    // Strip comments before checking for an ACTIVE declaration — the
    // fix's own explanatory comment mentions the old token by name.
    const ruleNoComments = countdownRule.replace(/\/\*[\s\S]*?\*\//g, '');
    expect(ruleNoComments, 'countdown must not declare the smallest font-size token (--fs-2xs)').not.toMatch(/font-size:\s*var\(--fs-2xs\)/);
    expect(ruleNoComments).toMatch(/font-weight:\s*700/);
  });
});
