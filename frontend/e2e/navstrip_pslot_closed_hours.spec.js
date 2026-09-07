/**
 * navstrip_pslot_closed_hours.spec.js
 *
 * Verifies PositionStrip.svelte guards dispPositionsToday / dispHoldingsToday
 * against zero-flash when the tab returns from hibernation or at market close.
 *
 * Zero-flash fix (two parts):
 *  1. Freeze $effect guard: when positionsDayPnlStore.total === 0 but positions
 *     still exist, keep the previous dispPositionsToday (don't overwrite to 0).
 *     Only clear to 0 when the positions array is genuinely empty.
 *  2. Shimmer guard: skip the bottom-border animation burst when
 *     postHibernationRefiring is true (SSE reconnect ticks on tab return).
 *
 * Three quality dimensions:
 *  1. UX      — P∆/HD∆ pills show last non-zero value on tab return (no flash to 0)
 *  2. SSOT    — guard pattern present in freeze $effect; postHibernationRefiring imported
 *  3. Stale   — shimmer suppressed during post-hibernation burst window
 *
 * Run:
 *   PLAYWRIGHT_USER=rambo PLAYWRIGHT_PASS=admin1234 \
 *   PLAYWRIGHT_BASE_URL=http://localhost:5174 \
 *   npx playwright test e2e/navstrip_pslot_closed_hours.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const STRIP_PATH = '/Users/ramanambore/projects/ramboq/frontend/src/lib/PositionStrip.svelte';

test.describe('NavStrip P-slot / HD∆ zero-flash guard', () => {
  // ── Test 1: freeze $effect guard pattern ──────────────────────────────────
  test('1-SSOT: freeze $effect uses "keep last value" pattern for positions', () => {
    const source = readFileSync(STRIP_PATH, 'utf-8');

    // The new guard: only write 0 when positions.length === 0 (list truly empty)
    expect(source, 'freeze $effect must guard with positions.length === 0')
      .toMatch(/else if \(positions\.length === 0\)\s*\{\s*dispPositionsToday = 0;/);

    // Must NOT have the old "length > 0 OR total !== 0" pattern that wrote 0 on tab return
    expect(source, 'old OR-guard pattern must be removed')
      .not.toMatch(/positions\.length > 0 \|\| positionsDayPnlStore\.total !== 0/);

    console.log('[navstrip_pslot_closed_hours] freeze $effect guard pattern verified');
  });

  // ── Test 2: holdings guard mirrors positions guard ─────────────────────────
  test('2-SSOT: freeze $effect uses "keep last value" pattern for holdings', () => {
    const source = readFileSync(STRIP_PATH, 'utf-8');

    expect(source, 'freeze $effect must guard holdings with holdings.length === 0')
      .toMatch(/else if \(holdings\.length === 0\)\s*\{\s*dispHoldingsToday = 0;/);

    expect(source, 'old OR-guard pattern for holdings must be removed')
      .not.toMatch(/holdings\.length > 0 \|\| holdingsDayPnlStore\.total !== 0/);

    console.log('[navstrip_pslot_closed_hours] holdings guard pattern verified');
  });

  // ── Test 3: postHibernationRefiring imported and guards shimmer ───────────
  test('3-Stale: postHibernationRefiring guards tickBus shimmer', () => {
    const source = readFileSync(STRIP_PATH, 'utf-8');

    // Import must be present
    expect(source, 'postHibernationRefiring must be imported from $lib/stores')
      .toContain('postHibernationRefiring');

    // get() from svelte/store must be imported to read store in callback
    expect(source, 'get must be imported from svelte/store')
      .toContain("from 'svelte/store'");

    // Shimmer guard in tickBus callback
    expect(source, 'tickBus subscribe must check postHibernationRefiring before shimmer')
      .toMatch(/get\(postHibernationRefiring\).*_shimmer\.notify|_shimmer\.notify.*postHibernationRefiring/);

    console.log('[navstrip_pslot_closed_hours] shimmer guard verified');
  });

  // ── Test 4: (algo) layout still mounts PositionStrip ──────────────────────
  test('4-UX: (algo) layout renders PositionStrip', () => {
    const layoutPath =
      '/Users/ramanambore/projects/ramboq/frontend/src/routes/(algo)/+layout.svelte';
    const source = readFileSync(layoutPath, 'utf-8');

    expect(source, '(algo) layout should import PositionStrip').toContain('PositionStrip');
    expect(/<PositionStrip|<position-strip/i.test(source),
      '(algo) layout should render PositionStrip').toBe(true);

    console.log('[navstrip_pslot_closed_hours] PositionStrip in layout verified');
  });
});
