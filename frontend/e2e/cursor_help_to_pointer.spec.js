/**
 * cursor_help_to_pointer.spec.js
 *
 * `cursor: help` implies hovering reveals something — a leftover
 * affordance from before InfoHint was made strictly click-only
 * (2026-10, see e4094945). Hovering an InfoHint trigger no longer does
 * anything, so the "?" cursor is misleading; every genuine InfoHint
 * trigger site was switched to `cursor: pointer`.
 *
 * Scope: ONLY sites that actually wrap/anchor an `<InfoHint>` component
 * were changed. Sites using a native `title=` attribute (which DOES
 * show something on hover, by the browser itself, not InfoHint) were
 * deliberately left at `cursor: help` — this spec locks in that
 * distinction in both directions so a future pass doesn't flip-flop it.
 *
 * Run:
 *   cd frontend && npx playwright test cursor_help_to_pointer --workers=1
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loginAsAdmin } from './fixtures/auth.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const readFile = (relPath) => readFileSync(path.resolve(__dirname, '..', relPath), 'utf-8');

test.describe('Static source checks — InfoHint trigger sites use cursor:pointer, not cursor:help', () => {
  test('ChartWorkspace.svelte — 6 Greek-item InfoHint anchors use cursor:pointer', () => {
    const src = readFile('src/lib/ChartWorkspace.svelte');
    const matches = src.match(/style="cursor:pointer"\s*bind:this=\{_cwGreekHintAnchor\./g) || [];
    expect(matches.length).toBe(6);
    expect(src).not.toMatch(/style="cursor:help"/);
  });

  test('ChartWorkspace.svelte — unrelated .signal-marker SVG <title> tooltip keeps cursor:help', () => {
    const src = readFile('src/lib/ChartWorkspace.svelte');
    expect(src).toMatch(/:global\(\.signal-marker\)\s*\{[^}]*cursor:\s*help;/);
  });

  test('admin/derivatives +page.svelte — greeksNote/riskNote headers + .kv-k rule use cursor:pointer', () => {
    const src = readFile('src/routes/(algo)/admin/derivatives/+page.svelte');
    expect(src).toMatch(/class="opt-block-h" style="cursor:pointer" bind:this=\{_sumHintAnchor\.greeksNote\}/);
    expect(src).toMatch(/class="opt-block-h" style="cursor:pointer" bind:this=\{_sumHintAnchor\.riskNote\}/);
    expect(src).toMatch(/\.kv-k\s*\{[^}]*cursor:\s*pointer;/);
  });

  test('admin/derivatives +page.svelte — unrelated .cand-hidden-hint (native title) keeps cursor:help', () => {
    const src = readFile('src/routes/(algo)/admin/derivatives/+page.svelte');
    expect(src).toMatch(/\.cand-hidden-hint\s*\{[^}]*cursor:\s*help;/);
  });

  test('admin/metrics +page.svelte — all 10 metric-label InfoHint anchors use cursor:pointer', () => {
    const src = readFile('src/routes/(algo)/admin/metrics/+page.svelte');
    const matches = src.match(/class="metric-label" style="cursor:pointer"/g) || [];
    expect(matches.length).toBe(10);
    expect(src).not.toMatch(/style="cursor:help"/);
  });

  test('admin/perf +page.svelte — all metric-label InfoHint anchors use cursor:pointer', () => {
    const src = readFile('src/routes/(algo)/admin/perf/+page.svelte');
    const matches = src.match(/class="metric-label" style="cursor:pointer"/g) || [];
    expect(matches.length).toBeGreaterThanOrEqual(9);
    expect(src).not.toMatch(/style="cursor:help"/);
  });

  test('OptionsPayoff.svelte — .ps-row (stat overlay InfoHint rows) uses cursor:pointer', () => {
    const src = readFile('src/lib/OptionsPayoff.svelte');
    expect(src).toMatch(/\.ps-row\s*\{\s*display:\s*contents;\s*cursor:\s*pointer;\s*\}/);
  });

  // Sites with NO InfoHint import at all — these only ever had a native
  // `title=` affordance, so `cursor: help` remains correct.
  test('PnlAnalysis.svelte / LogPanel.svelte .log-pf / brokers .test-result / automation .lifespan-chip keep cursor:help (native title, no InfoHint)', () => {
    const pnl = readFile('src/lib/PnlAnalysis.svelte');
    expect(pnl).not.toMatch(/InfoHint/);
    expect(pnl).toMatch(/\.kv\s*\{[^}]*cursor:\s*help;/);

    const logPanel = readFile('src/lib/LogPanel.svelte');
    expect(logPanel).toMatch(/:global\(\.log-pf\)\s*\{[^}]*cursor:\s*help;/);

    const brokers = readFile('src/routes/(algo)/admin/brokers/+page.svelte');
    expect(brokers).not.toMatch(/InfoHint/);
    expect(brokers).toMatch(/\.test-result\s*\{[^}]*cursor:\s*help;/);

    // automation/+page.svelte DOES import InfoHint elsewhere (agent-field
    // tooltips, unrelated to this chip) — just confirm `.lifespan-chip`
    // itself has no InfoHint wired to it and keeps its native title.
    const automation = readFile('src/routes/(algo)/automation/+page.svelte');
    expect(automation).toMatch(/\.lifespan-chip\s*\{[^}]*cursor:\s*help;/);
    expect(automation).toMatch(/title=\{_lc\.tooltip\}/);
  });

  test('app.css .log-sim-pill (native title badge, unrelated to InfoHint) keeps cursor:help', () => {
    const css = readFile('src/app.css');
    expect(css).toMatch(/\.log-panel \.log-sim-pill\s*\{[^}]*cursor:\s*help;/);
  });
});

test.describe('Live — computed cursor on an InfoHint trigger resolves to pointer', () => {
  test('/admin/derivatives Greeks header (opt-block-h) computes cursor:pointer, not help', async ({ page }) => {
    test.setTimeout(60000);
    await loginAsAdmin(page);
    await page.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });

    const header = page.locator('.opt-block-h').first();
    const visible = await header.isVisible({ timeout: 20_000 }).catch(() => false);
    if (!visible) {
      test.info().annotations.push({ type: 'skip', description: 'no strategy loaded — .opt-block-h not rendered' });
      return;
    }
    const cursor = await header.evaluate((el) => getComputedStyle(el).cursor);
    expect(cursor).toBe('pointer');
  });
});
