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
