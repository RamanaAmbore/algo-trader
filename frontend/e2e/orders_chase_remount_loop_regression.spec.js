/**
 * orders_chase_remount_loop_regression.spec.js
 *
 * 2026-10-23 — regression guard for a confirmed runaway-refetch bug.
 *
 * Root cause: `/orders` mounts TWO `<ChaseCard>` instances inside a
 * mutually-exclusive `{#if _showChases}...{:else}...{/if}`, both bound
 * via `bind:activeCount={_activeChases}` — the very variable
 * `_showChases` is derived from. `ChaseCard.svelte`'s mirror effect used
 * to write `activeCount` unconditionally on every render, including the
 * very first one after a fresh mount, when its own local `_chases` is
 * still `[]` (n = 0) while the inherited bound value can already be
 * non-zero. That write forced `activeCount` back to 0 before the new
 * instance's own `_load()` had resolved, flipping `_showChases` back to
 * false, destroying the instance that just mounted and remounting the
 * other branch — whose `onMount` fired a fresh `_load()` fetch,
 * resolved non-empty, flipped the condition again, and repeated
 * unboundedly. Confirmed empirically: ~350-590 requests/second against
 * `/api/orders/chases/active` (an isolated local repro with console
 * instrumentation showed ~345 full destroy/mount cycles in 1 second,
 * matching the fetch count exactly).
 *
 * Fix (`ChaseCard.svelte`): a `_loadedOnce` flag gates every bindable
 * write until this instance's OWN first fetch has resolved (success or
 * failure), so a freshly-mounted instance can never stomp an inherited
 * non-zero value with its transient empty-array state. The effect also
 * reads the CURRENT `activeCount`/`loadError` via `untrack()`, so it
 * only re-runs on this instance's own local state changing — not on a
 * write from any other holder of the same shared bindable — closing the
 * general hazard for any future consumer that shares this bindable
 * across more than one mounted instance, not just this specific page.
 *
 * This file does NOT re-assert ChaseCard's rendered content (covered by
 * chase_badge_live_status_drawer.spec.js) — only request VOLUME and
 * TIMING, which is what the bug actually broke.
 *
 * Run:
 *   cd frontend && npx playwright test \
 *     e2e/orders_chase_remount_loop_regression.spec.js --project=chromium-desktop
 */
import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loginAsAdmin } from './fixtures/auth.js';

// 2026-10-23 follow-up — /orders migrated off the dual-mount `{#if
// _showChases}<ChaseCard/>{:else}<div style="display:none"><ChaseCard
// compact/></div>{/if}` swap onto the single-instance `headless` pattern
// already used by (algo)/+layout.svelte's header drawer (see that file's
// own ChaseCard mount for the reference implementation). ONE <ChaseCard>
// now stays mounted continuously; `headless={!_showChases}` suppresses its
// own template render while idle, so there is never a destroy/recreate
// cycle on this page at all — not just a safer one guarded by ChaseCard's
// own `_loadedOnce` fix.
const _ORDERS_PAGE_PATH = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '../src/routes/(algo)/orders/+page.svelte',
);

const _NOW_SEC = Math.floor(Date.now() / 1000);
const _ACTIVE_CHASE = {
  id: 601, account: 'T1', transaction_type: 'BUY', quantity: 50,
  symbol: 'RBQ-REPRO', current_limit: 102, initial_price: 100,
  attempts: 2, status: 'OPEN', next_attempt_at: _NOW_SEC + 12,
  mode: 'live', child_order_ids: [],
};

/** @param {import('@playwright/test').Page} page
 *  @param {() => any[]} getChases — called per request so a test can
 *  flip the mocked response mid-run (idle → active transition case). */
async function _mockOrdersApis(page, getChases) {
  await page.route('**/api/orders/**', async (route) => {
    const req = route.request();
    const url = req.url();
    if (req.method() !== 'GET') { await route.continue(); return; }
    if (url.includes('/orders/chases/active')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(getChases()) });
      return;
    }
    if (url.includes('/orders/events/recent')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) });
      return;
    }
    if (url.includes('/orders/algo/recent')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) });
      return;
    }
    if (url.includes('/orders/gtts')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ gtts: [], count: 0 }) });
      return;
    }
    if (url.includes('/orders/drafts')) {
      // payoffDrafts.load() — must be an explicit empty array so the
      // idle-state test below isn't accidentally desynced by real
      // backend draft rows (this route previously fell through to
      // route.continue(), hitting the real backend unmocked).
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) });
      return;
    }
    if (/\/api\/orders\/?(\?.*)?$/.test(url)) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ rows: [], refreshed_at: new Date().toISOString() }) });
      return;
    }
    await route.continue();
  });
}

test.describe('/orders — ChaseCard dual-mount remount-loop regression', () => {
  test.setTimeout(60_000);

  // Structural check — no page fixture needed. Confirms the migration
  // actually happened in source, not just that the behavioral tests
  // below happen to stay under a request-count threshold (a single
  // instance polling at a slightly slower cadence could also pass a
  // volume-only check without the dual-mount pattern actually being
  // gone). Confirmed to fail against the pre-migration file
  // (`git show HEAD:"frontend/src/routes/(algo)/orders/+page.svelte"`
  // has 2 `<ChaseCard` tags; this file has 1).
  test('orders +page.svelte source mounts exactly one <ChaseCard>, no hidden-instance swap', () => {
    const src = fs.readFileSync(_ORDERS_PAGE_PATH, 'utf8');
    const chaseCardTags = src.match(/<ChaseCard\b/g) || [];
    expect(chaseCardTags.length).toBe(1);
    // The old dual-mount pattern's hidden branch used `compact` on the
    // second instance and a `display:none` wrapper div — confirm both
    // are gone, not just that the tag count dropped (a future
    // regression could reintroduce a second instance via some other
    // wrapper shape).
    expect(src).not.toMatch(/style="display:\s*none"/);
    expect(src).not.toMatch(/<ChaseCard[^>]*\bcompact\b/);
    // The single surviving instance must drive its visibility via the
    // `headless` prop (the whole point of this migration), not via a
    // template-level {#if}/{:else} around the element itself.
    expect(src).toMatch(/<ChaseCard[^>]*\bheadless=\{!_showChases\}/);
  });

  test('a non-empty /chases/active mock does not trigger a runaway poll loop', async ({ page }) => {
    await loginAsAdmin(page);

    let hitCount = 0;
    await _mockOrdersApis(page, () => { hitCount++; return [_ACTIVE_CHASE]; });

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');

    await page.waitForTimeout(10_000);

    // /orders runs TWO independent ~3s pollers by design (the layout's
    // persistent headless ChaseCard, plus this page's own single
    // always-mounted instance) — a healthy 10s window lands around
    // 6-10 hits. The pre-fix bug produced hundreds to thousands of
    // hits in the same window.
    console.log(`[regression] hits to /chases/active in 10s: ${hitCount}`);
    expect(hitCount).toBeGreaterThan(0);
    expect(hitCount).toBeLessThan(20);
  });

  test('transitioning from idle (no chases) to an active chase mid-session stays bounded', async ({ page }) => {
    await loginAsAdmin(page);

    let chasesActive = false;
    let hitCount = 0;
    await _mockOrdersApis(page, () => {
      hitCount++;
      return chasesActive ? [_ACTIVE_CHASE] : [];
    });

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');

    // Idle for a few seconds first — confirms the single always-mounted
    // instance's own idle-cadence poll (and the layout's persistent
    // instance) don't spin on their own while headless/closed.
    await page.waitForTimeout(3000);
    expect(hitCount).toBeLessThan(10);

    // Idle state must actually be hidden on screen — the chrome section
    // is always mounted now (never `{#if}`-destroyed) and relies on
    // `style:display` to hide while `_showChases` is false, so this is
    // the one check that distinguishes "genuinely hidden" from "always
    // visible, just empty".
    await expect(page.locator('.bucket-card-chase')).toBeHidden();

    // Flip the mock — simulates a chase firing mid-session. The page's
    // own instance polls in the background throughout (it is never
    // unmounted while idle, per the single-instance `headless` pattern)
    // and must pick this up on its own idle-cadence poller
    // (max(15000, pollMs*10) = 30s) without a page reload or remount.
    chasesActive = true;
    await expect(page.locator('.cc-root').first()).toBeVisible({ timeout: 40_000 });
    await expect(page.locator('.bucket-card-chase')).toBeVisible();
    await expect(page.getByText('Chases in flight')).toBeVisible();

    // And once visible, request volume for the next 10s must stay sane
    // — not degenerate into the remount loop the fix closes.
    const hitsBeforeSettle = hitCount;
    await page.waitForTimeout(10_000);
    const hitsDuringSettle = hitCount - hitsBeforeSettle;
    console.log(`[regression] hits in the 10s window after .cc-root appeared: ${hitsDuringSettle}`);
    expect(hitsDuringSettle).toBeLessThan(20);
  });
});
