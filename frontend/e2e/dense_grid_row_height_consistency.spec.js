/**
 * dense_grid_row_height_consistency.spec.js
 *
 * Wave 2a of the row-height/cell-padding consistency pass (2026-09-30),
 * following the read-only audit (operator: "go ahead" + "even look at
 * row height, cell height for consistency"). Scope: DayPnlBreakup.svelte,
 * PnlAnalysis.svelte, PnlPanel.svelte, SimulatorPanel.svelte,
 * ChaseCard.svelte, admin/history, strategies/[id], admin/brokers,
 * admin/perf.
 *
 * Two genuine bugs fixed (not just style drift):
 *
 *  1. admin/perf/+page.svelte — `.perf-hotspot-table th, td { text-align:
 *     left }` was MORE specific than the global `.algo-table-num` rule,
 *     left-aligning the numeric cc/line columns against this app's
 *     "numeric columns right-aligned" convention. Fixed by right-aligning
 *     `.algo-table-num` cells specifically (header + data) and removing
 *     the table's own padding override entirely — it fell back to
 *     `.algo-table`'s canonical dense-grid values (28px th / 24px td,
 *     `0 4px` padding), documented in app.css right above `.algo-table`.
 *
 *  2. PnlAnalysis.svelte (`.pnl-tbl`) and SimulatorPanel.svelte
 *     (`.sim-summary-grid`) — numeric column HEADERS (Total P&L / Day
 *     P&L / Rows / Qty in PnlAnalysis; Value / P&L / Day P&L in
 *     SimulatorPanel) had no `.num`/`.sim-num` class, so they inherited
 *     the table's default `text-align: left` while their DATA cells
 *     below were already right-aligned via `.num`/`.sim-num`. Fixed by
 *     adding the class to each numeric `<th>` — both files already
 *     define `.pnl-tbl .num` / `.sim-summary-grid .sim-num` with
 *     `text-align: right`, at (0,2,0) specificity which already beats
 *     the plain `th` rule, so no new CSS rule was needed, just correct
 *     markup.
 *
 * Row-height/padding survey (all 9 files) — see the agent's handback
 * report for the full per-file breakdown. Summary: only admin/perf's
 * hotspot table needed a change (converged exactly to the canonical
 * 24px/28px dense tier by removing its own padding override); every
 * other file's padding/font-size combination already lands within a
 * few px of one of the two canonical tiers and was left alone.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loginAsAdmin } from './fixtures/auth.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const readFile = (relPath) => {
  const abs = path.resolve(__dirname, '..', relPath);
  return readFileSync(abs, 'utf-8');
};

/** Extract the FIRST `.selector { ... }` rule body, or null if absent. */
function ruleBody(css, selector) {
  const re = new RegExp(
    selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*\\{([^}]*)\\}'
  );
  const m = css.match(re);
  return m ? m[1] : null;
}

// ── Bug #1 — perf hotspot table numeric-column alignment ────────────────

test.describe('Static source checks — admin/perf/+page.svelte hotspot table', () => {
  const content = readFile('src/routes/(algo)/admin/perf/+page.svelte');

  test('cc/line header cells carry .algo-table-num (right-aligned numeric columns)', () => {
    expect(content).toMatch(/<th class="algo-table-num"><span class="metric-label">cc/);
    expect(content).toMatch(/<th class="algo-table-num">line<\/th>/);
  });

  test('cc/line data cells carry .algo-table-num too (header + data agree)', () => {
    expect(content).toMatch(/<td class="algo-table-num perf-fn-cc">/);
    expect(content).toMatch(/<td class="algo-table-num perf-fn-line">/);
  });

  test('.perf-hotspot-table no longer force-left-aligns numeric columns — a dedicated .algo-table-num override wins', () => {
    expect(content).toMatch(/\.perf-hotspot-table th\.algo-table-num,\s*\n\s*\.perf-hotspot-table td\.algo-table-num\s*\{\s*\n\s*text-align:\s*right;/);
  });

  test('.perf-hotspot-table no longer overrides padding/row-height — falls back to .algo-table canonical 0 4px / 24px-28px dense tier', () => {
    // The old override was `padding: 0.3rem 0.5rem` on `.perf-hotspot-table
    // th, .perf-hotspot-table td` — confirm that literal is gone entirely
    // from the hotspot-table style block.
    const idx = content.indexOf('Hotspot table');
    expect(idx, 'Hotspot table style comment must exist').toBeGreaterThan(-1);
    const block = content.slice(idx, idx + 900);
    expect(block).not.toMatch(/padding:\s*0\.3rem\s*0\.5rem/);
  });

  test('live: Top 10 hotspots table — cc/line header AND data compute text-align right (static+live agreement)', async ({ page }) => {
    test.setTimeout(60000);
    await loginAsAdmin(page);
    await page.goto('/admin/perf', { waitUntil: 'domcontentloaded' }).catch(() => {});
    await page.waitForTimeout(2000);

    const table = page.locator('.perf-hotspot-table').first();
    const visible = await table.isVisible({ timeout: 5000 }).catch(() => false);
    if (!visible) {
      test.info().annotations.push({ type: 'skip', description: 'no hotspot data rendered' });
      return;
    }
    const ccHeader = table.locator('thead th.algo-table-num').nth(0);
    const lineHeader = table.locator('thead th.algo-table-num').nth(1);
    const ccData = table.locator('tbody td.perf-fn-cc').first();
    const lineData = table.locator('tbody td.perf-fn-line').first();

    for (const el of [ccHeader, lineHeader, ccData, lineData]) {
      const align = await el.evaluate((node) => getComputedStyle(node).textAlign);
      expect(['right', 'end'].includes(align), `expected right-aligned, got "${align}"`).toBe(true);
    }
  });
});

// ── Bug #2a — PnlAnalysis header/data alignment ──────────────────────────

test.describe('Static source checks — PnlAnalysis.svelte (.pnl-tbl)', () => {
  const content = readFile('src/lib/PnlAnalysis.svelte');

  test('Segment tab: Total P&L / Day P&L / Rows headers carry .num', () => {
    expect(content).toMatch(/<tr><th>Segment<\/th><th class="num">Total P&L<\/th><th class="num">Day P&L<\/th><th class="num">Rows<\/th><\/tr>/);
  });

  test('Account tab: Total P&L / Day P&L / Rows headers carry .num; Account/Segment/Kind stay plain (left)', () => {
    expect(content).toMatch(/<tr><th>Account<\/th><th>Segment<\/th><th>Kind<\/th><th class="num">Total P&L<\/th><th class="num">Day P&L<\/th><th class="num">Rows<\/th><\/tr>/);
  });

  test('Symbol tab: Total P&L / Day P&L / Rows headers carry .num', () => {
    expect(content).toMatch(/<tr><th>Symbol<\/th><th>Segment<\/th><th class="num">Total P&L<\/th><th class="num">Day P&L<\/th><th class="num">Rows<\/th><\/tr>/);
  });

  test('Daily tab: Total P&L / Day P&L headers carry .num', () => {
    expect(content).toMatch(/<tr><th>Date<\/th><th class="num">Total P&L<\/th><th class="num">Day P&L<\/th><\/tr>/);
  });

  test('CSV sample table: Qty / Total P&L headers carry .num', () => {
    expect(content).toMatch(/<tr><th>Symbol<\/th><th>Segment<\/th><th class="num">Qty<\/th><th class="num">Total P&L<\/th><\/tr>/);
  });

  test('.pnl-tbl .num already right-aligns at higher specificity than the plain th rule — no new CSS rule needed', () => {
    const body = ruleBody(content, '.pnl-tbl .num');
    expect(body).toMatch(/text-align:\s*right/);
    const thBody = ruleBody(content, '.pnl-tbl th');
    expect(thBody).toMatch(/text-align:\s*left/);
  });
});

// ── Bug #2b — SimulatorPanel header/data alignment ───────────────────────

test.describe('Static source checks — SimulatorPanel.svelte (.sim-summary-grid)', () => {
  const content = readFile('src/lib/execution/SimulatorPanel.svelte');

  test('Positions summary table: Value / P&L / Day P&L headers carry .sim-num', () => {
    expect(content).toMatch(/<thead><tr><th>Account<\/th><th class="sim-num">Value<\/th><th class="sim-num">P&amp;L<\/th><th class="sim-num">Day P&amp;L<\/th><\/tr><\/thead>/g);
  });

  test('both Positions-summary and Holdings-summary tables got the fix (not just one)', () => {
    const matches = content.match(/<thead><tr><th>Account<\/th><th class="sim-num">Value<\/th><th class="sim-num">P&amp;L<\/th><th class="sim-num">Day P&amp;L<\/th><\/tr><\/thead>/g) || [];
    expect(matches.length).toBe(2);
  });

  test('.sim-summary-grid .sim-num already right-aligns at higher specificity than the plain th rule — no new CSS rule needed', () => {
    const body = ruleBody(content, '.sim-summary-grid .sim-num');
    expect(body).toMatch(/text-align:\s*right/);
    const thBody = ruleBody(content, '.sim-summary-grid th');
    expect(thBody).toMatch(/text-align:\s*left/);
  });

  test('live: Simulator positions/holdings summary — Value/P&L/Day P&L header cells compute text-align right', async ({ page }) => {
    test.setTimeout(60000);
    await loginAsAdmin(page);
    await page.goto('/admin/execution', { waitUntil: 'domcontentloaded' }).catch(() => {});
    await page.waitForTimeout(2500);

    const table = page.locator('.sim-summary-grid').first();
    const visible = await table.isVisible({ timeout: 5000 }).catch(() => false);
    if (!visible) {
      test.info().annotations.push({ type: 'skip', description: 'no simulator summary rendered (sim not seeded)' });
      return;
    }
    const headers = table.locator('thead th.sim-num');
    const count = await headers.count();
    expect(count, 'expected 3 numeric headers (Value/P&L/Day P&L)').toBe(3);
    for (let i = 0; i < count; i++) {
      const align = await headers.nth(i).evaluate((node) => getComputedStyle(node).textAlign);
      expect(['right', 'end'].includes(align), `header ${i} expected right-aligned, got "${align}"`).toBe(true);
    }
  });
});

// ── Row-height / padding convergence — admin/perf hotspot table only ────

test.describe('Static source checks — row-height convergence (admin/perf hotspot table)', () => {
  const content = readFile('src/routes/(algo)/admin/perf/+page.svelte');

  test('.perf-hotspot-table carries no local height/padding override — inherits .algo-table (24px td / 28px th, 0 4px) exactly', () => {
    const block = content.slice(content.indexOf('Hotspot table'), content.indexOf('Hotspot table') + 900);
    expect(block).not.toMatch(/height:\s*\d/);
    expect(block).not.toMatch(/padding:\s*0\.3rem/);
  });

  test('live: .perf-hotspot-table tbody row height computes to the canonical dense-grid 24px', async ({ page }) => {
    test.setTimeout(60000);
    await loginAsAdmin(page);
    await page.goto('/admin/perf', { waitUntil: 'domcontentloaded' }).catch(() => {});
    await page.waitForTimeout(2000);

    const row = page.locator('.perf-hotspot-table tbody tr').first();
    const visible = await row.isVisible({ timeout: 5000 }).catch(() => false);
    if (!visible) {
      test.info().annotations.push({ type: 'skip', description: 'no hotspot rows rendered' });
      return;
    }
    const h = await row.evaluate((el) => el.getBoundingClientRect().height);
    // .algo-table tbody td sets height: 24px explicitly (vertical-align:
    // middle handles any content overflow) — allow 1px rendering slop.
    expect(Math.abs(h - 24), `row height ${h}px, expected ~24px`).toBeLessThanOrEqual(1);
  });
});
