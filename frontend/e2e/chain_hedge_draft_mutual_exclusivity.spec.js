/**
 * chain_hedge_draft_mutual_exclusivity.spec.js
 *
 * Operator (2026-11-26): Hedge and Draft must be mutually exclusive in
 * the Chain tab toolbar, but DELIBERATELY ASYMMETRICALLY:
 *   - HEDGE selected/ON  → DRAFT shows unselected, but stays interactive
 *     (clickable) — Draft is never disabled by Hedge.
 *   - DRAFT selected/ON  → HEDGE shows unselected AND is disabled
 *     (greyed out, non-interactive) the whole time Draft is on.
 *
 * Implementation: a single `$effect(() => { if (draftMode) hedgeOn =
 * false; })` plus `disabled={draftMode}` on the HEDGE checkbox. Because
 * Hedge is disabled whenever Draft is on, the only way the two could
 * ever simultaneously be "on" is via an EXTERNAL draftMode change (the
 * Ticket tab's own Draft toggle shares the same `_sharedDraftMode` prop)
 * — the effect's job is specifically to catch that case too, not just
 * same-tab clicks.
 *
 * Also covers the related, same-day fix: the per-cell wide-spread
 * warning icon (`.chain-cell-spread-warn`, commit 2950638e) and the
 * pre-submit spread-gate banner are both suppressed entirely while
 * Draft is on — Draft never places a real order, so a liquidity
 * warning is meaningless there.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
const DIR = import.meta.dirname ?? new URL('.', import.meta.url).pathname;
const CHAIN_TAB_PATH = path.resolve(DIR, '../src/lib/order/OptionChainTab.svelte');

// ──────────────────────────────────────────────────────────────────
// Source-scan — structural guarantees (no browser needed)
// ──────────────────────────────────────────────────────────────────

test.describe('Hedge/Draft mutual exclusivity, source-scan', () => {
  let src = '';

  test.beforeAll(() => {
    src = readFileSync(CHAIN_TAB_PATH, 'utf8');
  });

  test('a $effect forces hedgeOn off whenever draftMode is true', () => {
    expect(src).toMatch(/\$effect\(\(\) => \{\s*if \(draftMode\) hedgeOn = false;\s*\}\);/);
  });

  test('the HEDGE checkbox carries disabled={draftMode} — Draft always wins', () => {
    const idx = src.indexOf('class="oct-hedge-toggle"');
    expect(idx).toBeGreaterThan(-1);
    const block = src.slice(idx, src.indexOf('</label>', idx));
    expect(block).toMatch(/disabled=\{draftMode\}/);
  });

  test('the DRAFT checkbox carries NO disabled binding — never disabled by Hedge', () => {
    const idx = src.indexOf('class="ot-draft-toggle"');
    expect(idx).toBeGreaterThan(-1);
    const block = src.slice(idx, src.indexOf('</label>', idx));
    expect(block).not.toMatch(/disabled=/);
  });

  test('a dedicated disabled visual class exists for the HEDGE toggle', () => {
    expect(src).toMatch(/class:oct-hedge-toggle-disabled=\{draftMode\}/);
    const ruleIdx = src.indexOf('.oct-hedge-toggle-disabled {');
    expect(ruleIdx).toBeGreaterThan(-1);
  });

  test('the per-cell spread-warning consts are gated on !draftMode', () => {
    expect(src).toMatch(/ceSpreadWide = !draftMode &&/);
    expect(src).toMatch(/peSpreadWide = !draftMode &&/);
  });
});

// ──────────────────────────────────────────────────────────────────
// Live — real browser, drives the actual Chain tab.
// ──────────────────────────────────────────────────────────────────

async function _openNiftyChain(page, sym = 'NIFTY') {
  await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded', timeout: 30000 });
  const symInput = page.locator('.ssi-input').first();
  await expect(symInput).toBeVisible({ timeout: 15_000 });
  await symInput.fill(sym);
  const sugg = page.locator('.ssi-drop .ssi-row').first();
  await expect(sugg).toBeVisible({ timeout: 10_000 });
  await sugg.click({ force: true });
  await page.waitForTimeout(500);

  const chainTab = page.getByRole('tab', { name: /Chain/i }).first();
  await expect(chainTab).toBeEnabled({ timeout: 15_000 });
  await chainTab.click();
}

test.describe('Live — Hedge/Draft mutual exclusivity', () => {
  test('turning DRAFT on forces HEDGE off and disables it; turning DRAFT off re-enables HEDGE', async ({ page }) => {
    await loginAsAdmin(page);
    await _openNiftyChain(page);

    const hedgeCheckbox = page.locator('.oct-hedge-toggle input[type="checkbox"]').first();
    const draftCheckbox = page.locator('.ot-draft-toggle input[type="checkbox"]').first();
    await expect(hedgeCheckbox).toBeVisible({ timeout: 15_000 });
    await expect(draftCheckbox).toBeVisible({ timeout: 15_000 });

    // Start clean: Draft off, Hedge off.
    if (await draftCheckbox.isChecked()) await draftCheckbox.click({ force: true });
    await expect(hedgeCheckbox).toBeEnabled();
    if (await hedgeCheckbox.isChecked()) await hedgeCheckbox.click();

    // Turn Hedge ON first — Draft stays off, Hedge checked.
    await hedgeCheckbox.click();
    await expect(hedgeCheckbox).toBeChecked();
    await expect(draftCheckbox).not.toBeChecked();
    // Draft stays interactive while Hedge is on (never disabled by Hedge).
    await expect(draftCheckbox).toBeEnabled();

    // Now turn Draft ON — Hedge must be forced off AND disabled.
    await draftCheckbox.click({ force: true });
    await expect(draftCheckbox).toBeChecked();
    await expect(hedgeCheckbox).not.toBeChecked();
    await expect(hedgeCheckbox).toBeDisabled();

    // Turn Draft back off — Hedge becomes interactive again (stays off
    // until the operator explicitly re-enables it; no auto re-arm).
    await draftCheckbox.click({ force: true });
    await expect(draftCheckbox).not.toBeChecked();
    await expect(hedgeCheckbox).toBeEnabled();
    await expect(hedgeCheckbox).not.toBeChecked();
  });

  test('Draft suppresses the spread-warning banner on a staged wide-spread leg', async ({ page }) => {
    await loginAsAdmin(page);
    await _openNiftyChain(page);

    const draftCheckbox = page.locator('.ot-draft-toggle input[type="checkbox"]').first();
    await expect(draftCheckbox).toBeVisible({ timeout: 15_000 });
    if (!(await draftCheckbox.isChecked())) await draftCheckbox.click({ force: true });
    await expect(draftCheckbox).toBeChecked();

    // With Draft on, the pre-submit spread-gate banner must never be
    // visible, however wide a staged leg's spread is — `_buildGatePlan`
    // returns no targets at all while draftMode is true.
    await page.waitForTimeout(500);
    await expect(page.locator('[data-testid="spread-gate-banner"]')).toHaveCount(0);
  });
});
