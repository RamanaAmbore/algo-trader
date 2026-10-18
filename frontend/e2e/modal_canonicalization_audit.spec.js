/**
 * modal_canonicalization_audit.spec.js
 *
 * 6-dimension audit (2026-10-02) — hand-rolled modal overlays migrated
 * onto the canonical ModalShell/Select components, and drifted z-index
 * / dim literals brought back onto the shared app.css scale. One
 * `describe` block per numbered audit item; sections are appended as
 * each item ships in its own commit.
 *
 *   1. /admin/metrics drill-down modal → ModalShell (was hand-rolled,
 *      z-index:100, element-level Escape listener).
 *   3. Drifted modal dim levels (all pure-black) brought onto the
 *      canonical rgba(8,12,20,α) scale — OrderTicket.svelte's
 *      `.ot-overlay` (a full dialog → ModalShell's 0.72+blur(2px));
 *      OrderTimelineDrawer's `.otd-backdrop` (a lighter side drawer →
 *      the 0.42 canonical-modal-overlay value, no blur).
 *   4. z-index literals outside the shared app.css scale — ChartModal
 *      (10600, collided with .mode-combo-overlay → --z-modal-nested),
 *      ActivityLogModal (bare 10500 → var(--z-command)),
 *      BrokerHealthBadge (9990 → var(--z-drawer), matching its own
 *      modal's tier), RefreshButton (1000/1001 → --z-dropdown pair).
 *
 * Five quality dimensions (matches this repo's e2e convention):
 *   1. SSOT   — z-index/dim read from app.css custom properties at
 *               runtime, not re-hardcoded in the test
 *   2. Perf   — static source checks run with zero network/page loads
 *   3. Stale  — grep guards confirm the OLD hand-rolled overlay markup
 *               is actually gone, not just untested
 *   4. Reuse  — asserts the ACTUAL ModalShell/Select DOM output
 *               (`.ms-overlay.ms-dim[role=dialog]`), not just a visual
 *               screenshot
 *   5. UX     — functional checks (open/Escape/close) run in a real
 *               page against mocked API responses
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const readFile = (relPath) => readFileSync(path.resolve(__dirname, '..', relPath), 'utf-8');

// ─────────────────────────────────────────────────────────────────────────
// Item 1 — /admin/metrics drill-down modal migrated to ModalShell
// ─────────────────────────────────────────────────────────────────────────

test.describe('Static source checks — /admin/metrics drill-down uses ModalShell', () => {
  const page_src = readFile('src/routes/(algo)/admin/metrics/+page.svelte');

  test('imports ModalShell and the layerStack coordinator', () => {
    expect(page_src).toMatch(/import ModalShell from '\$lib\/ModalShell\.svelte';/);
    expect(page_src).toMatch(/import \{ pushLayer, popLayer \} from '\$lib\/utils\/layerStack\.js';/);
  });

  test('renders <ModalShell> wired to `selected` + closeDrill + the --z-modal token', () => {
    expect(page_src).toMatch(/<ModalShell open=\{!!selected\} onClose=\{closeDrill\}/);
    expect(page_src).toMatch(/zIndex="var\(--z-modal\)"/);
  });

  test('registers its own layerStack layer (teardown-effect form) instead of relying on ModalShell alone', () => {
    expect(page_src).toMatch(/if \(!selected\) return;\s*\n\s*const id = pushLayer\(closeDrill\);\s*\n\s*return \(\) => popLayer\(id\);/);
  });

  test('old hand-rolled overlay div + element-level Escape keydown handler are gone', () => {
    expect(page_src).not.toMatch(/<div class="metrics-modal-overlay"/);
    expect(page_src).not.toMatch(/onkeydown=\{\(e\) => \{ if \(e\.key === 'Escape'\) closeDrill\(\); \}\}/);
  });

  test('old bare z-index:100 literal is gone from the stylesheet', () => {
    expect(page_src).not.toMatch(/z-index:\s*100;/);
  });
});

test.describe('Functional — /admin/metrics drill-down modal (real browser, mocked API)', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);

    // Mock every code-metrics endpoint from ONE handler (disambiguated by
    // pathname) so the Detail button is always present regardless of
    // what the live DB happens to hold, and Playwright route-precedence
    // ordering never comes into play.
    await page.route('**/api/admin/code-metrics/**', (route) => {
      const { pathname } = new URL(route.request().url());
      if (pathname.endsWith('/trends')) {
        return route.fulfill({ json: { points: [] } });
      }
      if (pathname.endsWith('/code-metrics/v1.2.3')) {
        return route.fulfill({ json: { raw_payload: { mock: true } } });
      }
      // Bare list endpoint: /api/admin/code-metrics/?limit=...&offset=...
      return route.fulfill({
        json: {
          total: 1,
          rows: [{
            release_tag: 'v1.2.3',
            captured_at: new Date().toISOString(),
            backend_loc: 1000, backend_complexity_avg: 2, backend_complexity_max: 5,
            backend_stale_count: 0, backend_coverage_pct: 80,
            frontend_loc: 2000, frontend_complexity_avg: 3, frontend_duplicated_lines: 0,
            frontend_stale_count: 0, bug_count_since_last_release: 0,
            notes: '', per_page_latency_ms: {}, test_response_times: null,
          }],
        },
      });
    });
  });

  test('clicking Detail renders the canonical ModalShell structure (.ms-overlay.ms-dim[role=dialog])', async ({ page }) => {
    await page.goto(`${BASE}/admin/metrics`, { waitUntil: 'domcontentloaded' });

    const drillBtn = page.locator('button.metrics-drill').first();
    await expect(drillBtn, 'Detail button must render for the mocked row').toBeVisible({ timeout: 15_000 });
    await drillBtn.click();

    const overlay = page.locator('.ms-overlay.ms-dim[role="dialog"][aria-modal="true"]');
    await expect(overlay, 'ModalShell overlay must render').toBeVisible({ timeout: 5_000 });

    const panel = overlay.locator('.metrics-modal');
    await expect(panel, 'metrics-modal panel must render inside the ModalShell overlay').toBeVisible();
    await expect(panel.locator('.metrics-modal-head code')).toHaveText('v1.2.3');
  });

  test('ModalShell dim + z-index resolve to the canonical --z-modal / .ms-dim values at runtime', async ({ page }) => {
    await page.goto(`${BASE}/admin/metrics`, { waitUntil: 'domcontentloaded' });
    await page.locator('button.metrics-drill').first().click();

    const overlay = page.locator('.ms-overlay.ms-dim[role="dialog"]');
    await expect(overlay).toBeVisible({ timeout: 5_000 });

    const [bg, blur, zIndex, zModalVar] = await Promise.all([
      overlay.evaluate((el) => getComputedStyle(el).backgroundColor),
      overlay.evaluate((el) => getComputedStyle(el).backdropFilter),
      overlay.evaluate((el) => getComputedStyle(el).zIndex),
      page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--z-modal').trim()),
    ]);

    // .ms-dim: rgba(8, 12, 20, 0.72) + blur(2px) — ModalShell.svelte's own rule.
    expect(bg).toBe('rgba(8, 12, 20, 0.72)');
    expect(blur).toContain('blur(2px)');
    expect(zIndex).toBe(zModalVar);
  });

  test('Escape closes the modal (layerStack-coordinated, not ModalShell\'s own bubble listener)', async ({ page }) => {
    await page.goto(`${BASE}/admin/metrics`, { waitUntil: 'domcontentloaded' });
    await page.locator('button.metrics-drill').first().click();

    const overlay = page.locator('.ms-overlay.ms-dim[role="dialog"]');
    await expect(overlay).toBeVisible({ timeout: 5_000 });

    await page.keyboard.press('Escape');
    await expect(overlay).toHaveCount(0, { timeout: 3_000 });
  });

  test('clicking the × close button closes the modal', async ({ page }) => {
    await page.goto(`${BASE}/admin/metrics`, { waitUntil: 'domcontentloaded' });
    await page.locator('button.metrics-drill').first().click();

    const overlay = page.locator('.ms-overlay.ms-dim[role="dialog"]');
    await expect(overlay).toBeVisible({ timeout: 5_000 });

    await page.locator('.metrics-modal-close').click();
    await expect(overlay).toHaveCount(0, { timeout: 3_000 });
  });
});

// ─────────────────────────────────────────────────────────────────────────
// Item 3 — drifted modal dim levels brought onto the canonical scale
// ─────────────────────────────────────────────────────────────────────────

test.describe('Static source checks — modal dim levels match their role', () => {
  test('OrderTicket .ot-overlay uses ModalShell\'s dim (0.72 + blur), not pure black', () => {
    const orderTicket = readFile('src/lib/order/OrderTicket.svelte');
    const start = orderTicket.indexOf('.ot-overlay {');
    expect(start, '.ot-overlay rule must exist').toBeGreaterThan(-1);
    const close = orderTicket.indexOf('\n  }', start);
    const ruleBody = orderTicket.slice(start, close);
    expect(ruleBody).toMatch(/background:\s*rgba\(8,12,20,0\.72\);/);
    expect(ruleBody).toMatch(/backdrop-filter:\s*blur\(2px\);/);
    expect(ruleBody).not.toMatch(/background:\s*rgba\(0,0,0,0\.55\);/);
  });

  test('OrderTimelineDrawer .otd-backdrop uses the lighter canonical-modal-overlay dim (0.42, no blur)', () => {
    const drawer = readFile('src/lib/order/OrderTimelineDrawer.svelte');
    const start = drawer.indexOf('.otd-backdrop {');
    expect(start, '.otd-backdrop rule must exist').toBeGreaterThan(-1);
    const close = drawer.indexOf('\n  }', start);
    const ruleBody = drawer.slice(start, close);
    expect(ruleBody).toMatch(/background:\s*rgba\(8, 12, 20, 0\.42\);/);
    expect(ruleBody).not.toMatch(/backdrop-filter/);
    expect(ruleBody).not.toMatch(/rgba\(0, 0, 0, 0\.45\)/);
  });
});

// ─────────────────────────────────────────────────────────────────────────
// Item 4 — z-index literals outside the shared app.css scale
// ─────────────────────────────────────────────────────────────────────────

test.describe('Static source checks — z-index literals replaced with app.css scale tokens', () => {
  test('ChartModal .cm-overlay uses --z-modal-nested, not the old bare 10600 (collided with mode-combo-overlay)', () => {
    const chartModal = readFile('src/lib/ChartModal.svelte');
    expect(chartModal).toMatch(/z-index:\s*var\(--z-modal-nested\);/);
    expect(chartModal).not.toMatch(/z-index:\s*10600;/);
  });

  test('ActivityLogModal uses var(--z-command), not the old bare 10500 literal', () => {
    const activityLogModal = readFile('src/lib/ActivityLogModal.svelte');
    expect(activityLogModal).toMatch(/style="z-index:var\(--z-command\)"/);
    expect(activityLogModal).not.toMatch(/style="z-index:10500"/);
  });

  test('BrokerHealthBadge .bh-overlay uses var(--z-drawer) (matches its own .bh-modal tier), not the old bare 9990', () => {
    const bh = readFile('src/lib/BrokerHealthBadge.svelte');
    const start = bh.indexOf('.bh-overlay {');
    expect(start, '.bh-overlay rule must exist').toBeGreaterThan(-1);
    const close = bh.indexOf('\n  }', start);
    const ruleBody = bh.slice(start, close);
    expect(ruleBody).toMatch(/z-index:\s*var\(--z-drawer\);/);
    expect(ruleBody).not.toMatch(/z-index:\s*9990;/);
  });

  test('RefreshButton market-closed popup uses the --z-dropdown pair, not the old bare 1000/1001', () => {
    const refreshButton = readFile('src/lib/RefreshButton.svelte');
    expect(refreshButton).toMatch(/\.rf-closed-overlay\s*\{[\s\S]*?z-index:\s*var\(--z-dropdown\);/);
    expect(refreshButton).toMatch(/\.rf-closed-popup\s*\{[\s\S]*?z-index:\s*calc\(var\(--z-dropdown\)\s*\+\s*1\);/);
    expect(refreshButton).not.toMatch(/z-index:\s*1000;/);
    expect(refreshButton).not.toMatch(/z-index:\s*1001;/);
  });
});

test.describe('Functional — z-index tiers resolve correctly at runtime (real browser)', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('ChartModal (`k` shortcut) resolves to --z-modal-nested, above --z-command and the mode-combo pair', async ({ page }) => {
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });
    await page.locator('body').click({ position: { x: 5, y: 5 } });
    await page.keyboard.press('k');

    const overlay = page.locator('.cm-overlay');
    await expect(overlay, 'ChartModal overlay should open on `k`').toBeVisible({ timeout: 10_000 });

    const [zIndex, zModalNested, zCommand] = await Promise.all([
      overlay.evaluate((el) => Number(getComputedStyle(el).zIndex)),
      page.evaluate(() => Number(getComputedStyle(document.documentElement).getPropertyValue('--z-modal-nested').trim())),
      page.evaluate(() => Number(getComputedStyle(document.documentElement).getPropertyValue('--z-command').trim())),
    ]);
    expect(zIndex).toBe(zModalNested);
    expect(zIndex).toBeGreaterThan(zCommand);
    expect(zIndex).not.toBe(10600); // old literal — must not collide with mode-combo-overlay
  });

  test('ActivityLogModal (`h` shortcut) resolves to --z-command', async ({ page }) => {
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });
    await page.locator('body').click({ position: { x: 5, y: 5 } });
    await page.keyboard.press('h');

    const overlay = page.locator('[aria-label="Activity log"]');
    await expect(overlay, 'ActivityLogModal overlay should open on `h`').toBeVisible({ timeout: 10_000 });

    const [zIndex, zCommand] = await Promise.all([
      overlay.evaluate((el) => Number(getComputedStyle(el).zIndex)),
      page.evaluate(() => Number(getComputedStyle(document.documentElement).getPropertyValue('--z-command').trim())),
    ]);
    expect(zIndex).toBe(zCommand);
  });

  test('BrokerHealthBadge popover resolves to --z-drawer, matching its own .bh-modal tier', async ({ page }) => {
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });

    const brokerChip = page.locator('.broker-chip:visible').first();
    const chipVisible = await brokerChip.isVisible({ timeout: 8_000 }).catch(() => false);
    test.skip(!chipVisible, 'no broker chip rendered in this environment (no connected broker accounts)');

    await brokerChip.click();
    const modal = page.locator('.bh-modal');
    await expect(modal, 'broker health modal should open').toBeVisible({ timeout: 5_000 });

    const overlay = page.locator('.bh-overlay');
    const [overlayZ, modalZ, zDrawer] = await Promise.all([
      overlay.evaluate((el) => Number(getComputedStyle(el).zIndex)),
      modal.evaluate((el) => Number(getComputedStyle(el).zIndex)),
      page.evaluate(() => Number(getComputedStyle(document.documentElement).getPropertyValue('--z-drawer').trim())),
    ]);
    expect(overlayZ).toBe(zDrawer);
    expect(modalZ).toBe(zDrawer);
  });
});
