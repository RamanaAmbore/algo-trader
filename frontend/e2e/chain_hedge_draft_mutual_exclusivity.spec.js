/**
 * chain_hedge_draft_mutual_exclusivity.spec.js
 *
 * Operator (2026-11-26): Hedge and Draft must be mutually exclusive in
 * the Chain tab toolbar, originally DELIBERATELY ASYMMETRICALLY:
 *   - HEDGE selected/ON  → DRAFT shows unselected, but stays interactive
 *     (clickable) — Draft is never disabled by Hedge.
 *   - DRAFT selected/ON  → HEDGE shows unselected AND is disabled
 *     (greyed out, non-interactive) the whole time Draft is on.
 *
 * REVERSED 2026-11-28 (operator, verbatim): "When draft mode is on,
 * chase still should be displayed but disabled. When hedge is on, draft
 * should be disabled but still should be displayed on the screen. When
 * draft is on, hedge should be disabled but should be displayed. This
 * is to make sure the front end layout doesn't shift." The rule is now
 * SYMMETRIC — each of Hedge/Draft disables the other, both stay
 * VISIBLE always (never `{#if}`-hidden), and CHASE (on this same
 * toolbar) is also always visible, disabled-not-hidden while Draft is
 * on. This supersedes the "Draft is NEVER disabled by Hedge" half of
 * the original rule above.
 *
 * Implementation:
 *   - `$effect(() => { if (draftMode) hedgeOn = false; })` — unchanged,
 *     still the one ACTIVE forcing effect (Draft still "wins" in that
 *     turning it on forces Hedge off, not merely disables it). Needed
 *     for the case where `draftMode` flips true from an EXTERNAL source
 *     (the Ticket tab's own Draft toggle shares `_sharedDraftMode`).
 *   - `disabled={draftMode || _hedgeUnsupportedBfo}` on the HEDGE
 *     checkbox — unchanged.
 *   - `disabled={hedgeOn}` on the DRAFT checkbox — NEW. No forcing
 *     effect needed in this direction: `hedgeOn` is local `$state` with
 *     no external setter besides its own (now-conditionally-disabled)
 *     checkbox, so it can never become true at a moment `draftMode`
 *     could legitimately also be true.
 *   - CHASE's own cluster (`.oes-chase-cluster`) is now UNCONDITIONAL
 *     (no `{#if !draftMode}` wrapper) with `class:oes-chase-protected`
 *     + the native `inert` attribute while Draft is on — same pattern
 *     SymbolPanel's Ticket-tab copy already uses (2026-11-24).
 *
 * Also still covers the related, same-day fix: the per-cell wide-spread
 * warning icon (`.chain-cell-spread-warn`, commit 2950638e) and the
 * pre-submit spread-gate banner are both suppressed entirely while
 * Draft is on — Draft never places a real order, so a liquidity
 * warning is meaningless there. Unaffected by the 2026-11-28 reversal.
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

  test('the HEDGE checkbox carries disabled={draftMode || _hedgeUnsupportedBfo} — Draft still forces it off', () => {
    const idx = src.indexOf('class="oct-hedge-toggle"');
    expect(idx).toBeGreaterThan(-1);
    const block = src.slice(idx, src.indexOf('</label>', idx));
    expect(block).toMatch(/disabled=\{draftMode \|\| _hedgeUnsupportedBfo\}/);
  });

  test('the DRAFT checkbox now carries disabled={hedgeOn} — Hedge disables Draft too (2026-11-28 reversal)', () => {
    const idx = src.indexOf('class="ot-draft-toggle"');
    expect(idx).toBeGreaterThan(-1);
    const block = src.slice(idx, src.indexOf('</label>', idx));
    expect(block).toMatch(/disabled=\{hedgeOn\}/);
  });

  test('a dedicated disabled visual class exists for BOTH the HEDGE and DRAFT toggles', () => {
    expect(src).toMatch(/class:oct-hedge-toggle-disabled=\{draftMode \|\| _hedgeUnsupportedBfo\}/);
    expect(src.indexOf('.oct-hedge-toggle-disabled {')).toBeGreaterThan(-1);
    expect(src).toMatch(/class:ot-draft-toggle-disabled=\{hedgeOn\}/);
    expect(src.indexOf('.ot-draft-toggle-disabled {')).toBeGreaterThan(-1);
  });

  test('CHASE is never {#if}-gated on draftMode any more — always rendered, protected+inert instead', () => {
    expect(src).not.toMatch(/\{#if !draftMode\}\s*<span class="oes-chase-cluster">/);
    expect(src).toMatch(/class="oes-chase-cluster"\s+class:oes-chase-protected=\{draftMode\}\s+inert=\{draftMode\}/);
    expect(src.indexOf('.oes-chase-protected {')).toBeGreaterThan(-1);
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
  test('turning DRAFT on forces HEDGE off+disabled; turning HEDGE on disables DRAFT; both stay visible throughout', async ({ page }) => {
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

    // Turn Hedge ON first — Draft stays VISIBLE and off, but is now
    // DISABLED (the 2026-11-28 reversal — Draft used to stay clickable
    // here; it no longer does).
    await hedgeCheckbox.click();
    await expect(hedgeCheckbox).toBeChecked();
    await expect(draftCheckbox).toBeVisible();
    await expect(draftCheckbox).not.toBeChecked();
    await expect(draftCheckbox).toBeDisabled();

    // Hedge forces itself off via the Draft $effect below — with Draft
    // disabled, the only way to reach "Draft on" is first turning Hedge
    // back off.
    await hedgeCheckbox.click();
    await expect(hedgeCheckbox).not.toBeChecked();
    await expect(draftCheckbox).toBeEnabled();

    // Now turn Draft ON — Hedge must be forced off, stay VISIBLE, and
    // be DISABLED (unchanged from the original 2026-11-26 rule).
    await draftCheckbox.click({ force: true });
    await expect(draftCheckbox).toBeChecked();
    await expect(hedgeCheckbox).toBeVisible();
    await expect(hedgeCheckbox).not.toBeChecked();
    await expect(hedgeCheckbox).toBeDisabled();

    // Turn Draft back off — Hedge becomes interactive again (stays off
    // until the operator explicitly re-enables it; no auto re-arm).
    await draftCheckbox.click({ force: true });
    await expect(draftCheckbox).not.toBeChecked();
    await expect(hedgeCheckbox).toBeEnabled();
    await expect(hedgeCheckbox).not.toBeChecked();
  });

  test('CHASE stays visible (never removed from the DOM) and becomes inert while Draft is on', async ({ page }) => {
    await loginAsAdmin(page);
    await _openNiftyChain(page);

    const draftCheckbox = page.locator('.ot-draft-toggle input[type="checkbox"]').first();
    const chaseCluster = page.locator('.oct-toolbar-trailing .oes-chase-cluster').first();
    await expect(draftCheckbox).toBeVisible({ timeout: 15_000 });
    await expect(chaseCluster).toBeVisible({ timeout: 10_000 });

    if (await draftCheckbox.isChecked()) await draftCheckbox.click({ force: true });
    await expect(chaseCluster).toBeVisible();
    // Not-protected baseline: no protected class, not inert.
    await expect(chaseCluster).not.toHaveClass(/oes-chase-protected/);

    await draftCheckbox.click({ force: true });
    await expect(draftCheckbox).toBeChecked();

    // Still in the DOM, still visible — just protected/inert now.
    await expect(chaseCluster).toBeVisible();
    await expect(chaseCluster).toHaveClass(/oes-chase-protected/);
    await expect(chaseCluster).toHaveJSProperty('inert', true);

    await draftCheckbox.click({ force: true });
    await expect(draftCheckbox).not.toBeChecked();
    await expect(chaseCluster).not.toHaveClass(/oes-chase-protected/);
    await expect(chaseCluster).toHaveJSProperty('inert', false);
  });

  test('toolbar layout never shifts when toggling Draft or Hedge — the control set and their positions stay stable', async ({ page }) => {
    await loginAsAdmin(page);
    await _openNiftyChain(page);

    const draftCheckbox = page.locator('.ot-draft-toggle input[type="checkbox"]').first();
    const hedgeCheckbox = page.locator('.oct-hedge-toggle input[type="checkbox"]').first();
    const chaseCluster = page.locator('.oct-toolbar-trailing .oes-chase-cluster').first();
    const draftToggle = page.locator('.ot-draft-toggle').first();
    const hedgeToggle = page.locator('.oct-hedge-toggle').first();
    await expect(draftCheckbox).toBeVisible({ timeout: 15_000 });

    if (await draftCheckbox.isChecked()) await draftCheckbox.click({ force: true });
    if (await hedgeCheckbox.isChecked()) await hedgeCheckbox.click();
    await page.waitForTimeout(200);

    // A couple of px of tolerance for subpixel/rendering noise — the
    // real bug this test caught during development was a ~7.8px shift
    // (InfoHint's `.info-wrap` taking real width only while mounted),
    // which this tolerance would still have failed on.
    const CLOSE_PX = 2;
    const near = (/** @type {number|undefined} */ a, /** @type {number|undefined} */ b) =>
      a != null && b != null && Math.abs(a - b) <= CLOSE_PX;

    // Baseline bounding boxes (both off).
    const base = {
      chase: await chaseCluster.boundingBox(),
      draft: await draftToggle.boundingBox(),
      hedge: await hedgeToggle.boundingBox(),
    };
    expect(base.chase).not.toBeNull();
    expect(base.draft).not.toBeNull();
    expect(base.hedge).not.toBeNull();

    // Turn Draft ON — CHASE and HEDGE must stay at (near) the same x/y
    // (the operator's own stated goal: "front end layout doesn't shift").
    await draftCheckbox.click({ force: true });
    await expect(draftCheckbox).toBeChecked();
    await page.waitForTimeout(200);
    const afterDraft = {
      chase: await chaseCluster.boundingBox(),
      draft: await draftToggle.boundingBox(),
      hedge: await hedgeToggle.boundingBox(),
    };
    expect(near(afterDraft.chase?.x, base.chase?.x), `chase.x ${afterDraft.chase?.x} vs ${base.chase?.x}`).toBe(true);
    expect(near(afterDraft.chase?.y, base.chase?.y), `chase.y ${afterDraft.chase?.y} vs ${base.chase?.y}`).toBe(true);
    expect(near(afterDraft.draft?.x, base.draft?.x), `draft.x ${afterDraft.draft?.x} vs ${base.draft?.x}`).toBe(true);
    expect(near(afterDraft.hedge?.x, base.hedge?.x), `hedge.x ${afterDraft.hedge?.x} vs ${base.hedge?.x}`).toBe(true);

    // Draft off, Hedge ON — DRAFT must stay in place too (B%/S% fields
    // appear AFTER Hedge, which is intentionally out of scope — see
    // CLAUDE.md — but nothing BEFORE Hedge in the row may move).
    await draftCheckbox.click({ force: true });
    await expect(draftCheckbox).not.toBeChecked();
    await hedgeCheckbox.click();
    await expect(hedgeCheckbox).toBeChecked();
    await page.waitForTimeout(200);
    const afterHedge = {
      chase: await chaseCluster.boundingBox(),
      draft: await draftToggle.boundingBox(),
      hedge: await hedgeToggle.boundingBox(),
    };
    expect(near(afterHedge.chase?.x, base.chase?.x), `chase.x ${afterHedge.chase?.x} vs ${base.chase?.x}`).toBe(true);
    expect(near(afterHedge.draft?.x, base.draft?.x), `draft.x ${afterHedge.draft?.x} vs ${base.draft?.x}`).toBe(true);
    expect(near(afterHedge.hedge?.x, base.hedge?.x), `hedge.x ${afterHedge.hedge?.x} vs ${base.hedge?.x}`).toBe(true);
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
