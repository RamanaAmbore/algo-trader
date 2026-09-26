/**
 * derivatives_include_holdings_toggle.spec.js
 *
 * Regression guard for the Include-Holdings toggle divergence bug (2026-09).
 *
 * Background:
 *   `_includeHoldings` (+page.svelte) used to have two independent
 *   persistence paths: the canonical `includeHoldings` writable store
 *   (stores.js, localStorage-backed, SSOT for NavStrip's P slots too) and
 *   `_loadCache()`'s own direct restore from a sessionStorage snapshot
 *   (`ramboq:options-state`) that bypassed the store entirely. Once the
 *   two diverged, `_flipHoldings` computed the next value off the STALE
 *   local mirror (`includeHoldings.set(!_includeHoldings)`), and Svelte's
 *   `writable.set()` no-ops when the computed value already equals the
 *   store's actual current value — so the toggle could get permanently
 *   stuck (can't disable) or silently revert on remount within the
 *   cache's 5-minute TTL.
 *
 *   Fix: `_includeHoldings` dropped entirely from the sessionStorage cache
 *   (both the `_saveCache` write and the `_loadCache` read), and
 *   `_flipHoldings` switched to `includeHoldings.update(v => !v)` so it
 *   always computes off the store's own current value, never a local
 *   mirror.
 *
 * Test strategy (deterministic, no reliance on race timing):
 *   Seed a DELIBERATELY DIVERGED sessionStorage snapshot (`_includeHoldings`
 *   opposite of the localStorage-backed store value) before navigation,
 *   then assert the on-page HOLD toggle reflects the STORE's value, not
 *   the stale cache — both on cold load and after a click.
 *
 * Five quality dimensions:
 *   1. SSOT   — `opt.includeHoldings` (localStorage, via the `includeHoldings`
 *               store) is the only source the toggle's initial state may
 *               come from; the sessionStorage snapshot must never win.
 *   2. Perf   — no extra fetch triggered by the toggle fix itself.
 *   3. Stale  — grep confirms `_includeHoldings` no longer appears as a
 *               field in the `_saveCache`/`_loadCache` payload.
 *   4. Reuse  — same store (`includeHoldings`) also drives NavStrip's P
 *               slots; a store-level fix (not a page-local one) benefits
 *               both surfaces.
 *   5. UX     — toggle is never visually stuck: a click always flips the
 *               rendered `aria-pressed` state.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import * as fs from 'node:fs';
import * as path from 'node:path';

test.setTimeout(90_000);

const PAGE_SRC = path.resolve(
  process.cwd(),
  'src/routes/(algo)/admin/derivatives/+page.svelte'
);

const CACHE_KEY = 'ramboq:options-state';
const HOLDINGS_KEY = 'opt.includeHoldings';

const HOLD_BUTTON = 'button.legend-toggle:has-text("HOLD")';

/**
 * Seed localStorage's `opt.includeHoldings` flag + a deliberately diverged
 * `ramboq:options-state` sessionStorage snapshot, both BEFORE the page's
 * own scripts run (addInitScript fires before any page script on every
 * navigation in this browsing context).
 *
 * @param {import('@playwright/test').Page} page
 * @param {{ storeOn: boolean, cacheOn: boolean }} opts
 */
async function seedDivergedState(page, { storeOn, cacheOn }) {
  await page.addInitScript(
    ({ cacheKey, holdingsKey, storeOn, cacheOn }) => {
      try {
        localStorage.setItem(holdingsKey, storeOn ? '1' : '0');
      } catch (_) { /* ignore */ }
      try {
        sessionStorage.setItem(
          cacheKey,
          JSON.stringify({ ts: Date.now(), _includeHoldings: cacheOn })
        );
      } catch (_) { /* ignore */ }
    },
    { cacheKey: CACHE_KEY, holdingsKey: HOLDINGS_KEY, storeOn, cacheOn }
  );
}

test.describe('Derivatives — Include-Holdings toggle divergence fix', () => {
  test('SSOT: sessionStorage cache no longer carries _includeHoldings', () => {
    const src = fs.readFileSync(PAGE_SRC, 'utf-8');
    const saveStart = src.indexOf('function _saveCache(');
    const saveEnd = src.indexOf('\n  }', saveStart);
    const saveBody = src.slice(saveStart, saveEnd);
    expect(
      /_includeHoldings\s*,?\s*\n?\s*};/.test(saveBody) === false &&
      !/const payload = \{[^}]*_includeHoldings[^}]*\}/s.test(saveBody),
      '_saveCache payload must not include _includeHoldings'
    ).toBe(true);

    const loadStart = src.indexOf('function _loadCache(');
    const loadEnd = src.indexOf('\n  }', loadStart);
    const loadBody = src.slice(loadStart, loadEnd);
    expect(
      /_includeHoldings\s*=\s*d\._includeHoldings/.test(loadBody),
      '_loadCache must not restore _includeHoldings from the cached snapshot'
    ).toBe(false);

    expect(
      src.includes('includeHoldings.update(v => !v)'),
      '_flipHoldings must compute off the store\'s own current value via .update(), not a local mirror'
    ).toBe(true);
  });

  test('toggle reflects store value (OFF) even when cache says ON, and a click flips it', async ({ page }) => {
    await loginAsAdmin(page);
    // Store OFF, cache diverged to ON — pre-fix this made the toggle
    // appear stuck ON with clicks unable to disable it.
    await seedDivergedState(page, { storeOn: false, cacheOn: true });

    await page.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });
    const holdBtn = page.locator(HOLD_BUTTON).first();
    await holdBtn.waitFor({ state: 'visible', timeout: 30_000 });

    // Cold load must reflect the STORE (OFF), not the diverged cache (ON).
    await expect(holdBtn).toHaveAttribute('aria-pressed', 'false');

    // Click must flip it — the toggle must never be stuck.
    await holdBtn.click();
    await expect(holdBtn).toHaveAttribute('aria-pressed', 'true');

    const lsValue = await page.evaluate((k) => localStorage.getItem(k), HOLDINGS_KEY);
    expect(lsValue === '1' || lsValue === 'true').toBe(true);
  });

  test('toggle reflects store value (ON) even when cache says OFF — no silent revert on remount', async ({ page }) => {
    await loginAsAdmin(page);
    // Store ON, cache diverged to OFF — pre-fix this silently reverted the
    // toggle to OFF on any remount within the 5-min TTL.
    await seedDivergedState(page, { storeOn: true, cacheOn: false });

    await page.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });
    const holdBtn = page.locator(HOLD_BUTTON).first();
    await holdBtn.waitFor({ state: 'visible', timeout: 30_000 });

    await expect(holdBtn).toHaveAttribute('aria-pressed', 'true');

    // Reload within the TTL — same diverged cache is still present
    // (sessionStorage survives a same-tab reload). Toggle must still
    // reflect the store, not revert to the stale cached OFF.
    await page.reload({ waitUntil: 'domcontentloaded' });
    const holdBtnAfterReload = page.locator(HOLD_BUTTON).first();
    await holdBtnAfterReload.waitFor({ state: 'visible', timeout: 30_000 });
    await expect(holdBtnAfterReload).toHaveAttribute('aria-pressed', 'true');
  });
});
