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
import { loginAsAdmin } from './fixtures/auth.js';

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
    if (/\/api\/orders\/?(\?.*)?$/.test(url)) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ rows: [], refreshed_at: new Date().toISOString() }) });
      return;
    }
    await route.continue();
  });
}

test.describe('/orders — ChaseCard dual-mount remount-loop regression', () => {
  test.setTimeout(60_000);

  test('a non-empty /chases/active mock does not trigger a runaway poll loop', async ({ page }) => {
    await loginAsAdmin(page);

    let hitCount = 0;
    await _mockOrdersApis(page, () => { hitCount++; return [_ACTIVE_CHASE]; });

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');

    await page.waitForTimeout(10_000);

    // /orders runs TWO independent ~3s pollers by design (the layout's
    // persistent headless ChaseCard, plus this page's own) — a healthy
    // 10s window lands around 6-10 hits. The pre-fix bug produced
    // hundreds to thousands of hits in the same window.
    console.log(`[regression] hits to /chases/active in 10s: ${hitCount}`);
    expect(hitCount).toBeGreaterThan(0);
    expect(hitCount).toBeLessThan(20);
  });

  test('transitioning from idle (no chases) to an active chase mid-session stays bounded — the hidden-poller use case', async ({ page }) => {
    await loginAsAdmin(page);

    let chasesActive = false;
    let hitCount = 0;
    await _mockOrdersApis(page, () => {
      hitCount++;
      return chasesActive ? [_ACTIVE_CHASE] : [];
    });

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');

    // Idle for a few seconds first — confirms the hidden instance's own
    // mount (and the layout's persistent instance) don't themselves spin.
    await page.waitForTimeout(3000);
    expect(hitCount).toBeLessThan(10);

    // Flip the mock — simulates a chase firing mid-session. The hidden
    // instance's own idle-cadence poller (max(15000, pollMs*10) = 30s)
    // must still pick this up and show the section — this is the exact
    // use case the hidden `display:none` instance exists to serve.
    chasesActive = true;
    await expect(page.locator('.cc-root').first()).toBeVisible({ timeout: 40_000 });

    // And once visible, request volume for the next 10s must stay sane
    // — not degenerate into the remount loop the fix closes.
    const hitsBeforeSettle = hitCount;
    await page.waitForTimeout(10_000);
    const hitsDuringSettle = hitCount - hitsBeforeSettle;
    console.log(`[regression] hits in the 10s window after .cc-root appeared: ${hitsDuringSettle}`);
    expect(hitsDuringSettle).toBeLessThan(20);
  });
});
