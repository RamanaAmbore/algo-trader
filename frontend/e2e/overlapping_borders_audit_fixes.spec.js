/**
 * overlapping_borders_audit_fixes.spec.js — 2026-11-03 overlapping-border
 * audit, 2 verified findings fixed.
 *
 * 1. `.algo-table thead th` (app.css) used a literal border-bottom under
 *    `border-collapse: collapse`. A sticky `<th>` inside such a table can
 *    silently drop its own border on repaint in Chrome/WebKit — the same
 *    bug class OptionChainTab's ATM row marker already had to migrate
 *    away from (see CLAUDE.md). The admin/audit page pairs `.algo-table`
 *    with a sticky header, inheriting that risk. Migrated both the shared
 *    rule and the audit page's own `.audit-table th` override to
 *    box-shadow, which isn't part of the table border model and can't be
 *    dropped by that repaint bug.
 *
 *    (The audit's "specificity fight" framing for #1 was checked and is
 *    NOT accurate — the audit page's amber divider was already winning
 *    the cascade, verified via a live computed-style check. This is a
 *    robustness fix against the repaint bug, not a visibility fix.)
 *
 * 2. The derivatives Legs grid's TOTAL row stacked a 2px amber border-top
 *    directly against the last leg row's own 1px slate border-bottom —
 *    CSS grid doesn't collapse borders the way a <table> does, so both
 *    rendered as two separate lines 1px apart. Suppressed via
 *    `:has(+ .cand-row-total)`, the same pattern PnlAnalysis.svelte
 *    already uses for this exact case.
 */
import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 20_000;

test.use({ projects: ['chromium-desktop'] });

test('audit table sticky header uses box-shadow, not a real border, and keeps its amber divider color', async ({ page }) => {
  await page.route('**/api/admin/audit*', async (route) => {
    if (route.request().method() !== 'GET') { await route.continue(); return; }
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        rows: [{
          id: 1, created_at: new Date().toISOString(), actor_username: 'test', actor_role: 'admin',
          category: 'http', action: 'GET /x', target_type: null, target_id: null, status_code: 200,
          summary: 'ok', request_id: 'abc12345', client_ip: '127.0.0.1',
        }],
        total: 1,
      }),
    });
  });
  await loginAsAdmin(page);
  await page.goto('/admin/audit', { waitUntil: 'domcontentloaded' });

  const th = page.locator('.audit-table th').first();
  await expect(th).toBeVisible({ timeout: TIMEOUT });
  const style = await th.evaluate((el) => {
    const cs = getComputedStyle(el);
    return { borderBottomWidth: cs.borderBottomWidth, boxShadow: cs.boxShadow };
  });
  // No real border left on this edge — box-shadow carries the divider,
  // which a sticky-header repaint can't silently drop.
  expect(style.borderBottomWidth).toBe('0px');
  expect(style.boxShadow).toContain('inset');
  expect(style.boxShadow).toMatch(/251,\s*191,\s*36/); // amber, not the shared grey
});

test('shared .algo-table thead th rule (app.css) uses box-shadow, not a real border', async () => {
  // Source-level check — reliable regardless of which page/fixture happens
  // to render a sticky `.algo-table` header at test time. The live
  // rendered case (this exact rule, inherited by .audit-table) is already
  // proven by the test above.
  const fs = await import('node:fs/promises');
  const css = await fs.readFile('src/app.css', 'utf-8');
  const m = css.match(/\.algo-table thead th\s*{([^}]*)}/);
  expect(m).not.toBeNull();
  const body = m[1];
  expect(body).not.toMatch(/border-bottom\s*:/);
  expect(body).toMatch(/box-shadow\s*:\s*inset/);
});

test('derivatives Legs grid: the leg row immediately before TOTAL suppresses its own border-bottom', async () => {
  const fs = await import('node:fs/promises');
  const css = await fs.readFile('src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte', 'utf-8');
  expect(css).toMatch(/\.cand-row:has\(\+ \.cand-row-total\)\s*{\s*border-bottom:\s*none;?\s*}/);
});
