/**
 * pulse_nested_modal_stacking.spec.js
 *
 * Wave B2 of the modal/menu/toast stacking-defect audit (2026-09-30) —
 * modals openable from a button INSIDE a full-screen MarketPulse card
 * previously rendered BEHIND that card:
 *
 *   1. AddToPulseModal       — "+" button, Pinned/Watchlist full-screen card
 *   2. MarketPulse strike picker (ModalShell) — opened after an AddToPulse
 *      pick; covered via static source check only (reaching it live
 *      requires a real F&O-underlying typeahead backend round-trip, which
 *      this spec does not depend on for determinism).
 *
 * Five quality dimensions:
 *   1. SSOT   — z-index read from the real `--z-modal-nested` CSS custom
 *               property via computed style / app.css, not re-hardcoded
 *   2. Perf   — static source checks run with zero network/page loads
 *   3. Stale  — grep guards confirm the old bare 9000 literal and the
 *               old no-portal / no-Escape state are actually gone
 *   4. Reuse  — shared loginAsAdmin fixture; reuses the established
 *               `elementFromPoint` occlusion-check idiom from the audit
 *   5. UX     — functional checks (occlusion fixed, Escape closes
 *               without leaking a layer, non-Escape close path also
 *               pops cleanly) run in a real page, not just source greps
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
// Static source checks
// ─────────────────────────────────────────────────────────────────────────

test.describe('Static source checks — MarketPulse nested-modal zIndex wiring', () => {
  const pulse = readFile('src/lib/MarketPulse.svelte');

  test('AddToPulseModal call site passes --z-modal-nested', () => {
    expect(pulse).toMatch(/<AddToPulseModal[\s\S]{0,80}zIndex="var\(--z-modal-nested\)"/);
  });

  test('strike-picker ModalShell call site passes --z-modal-nested', () => {
    expect(pulse).toMatch(/<ModalShell open=\{!!optionPickerUnderlying\}[^>]*zIndex="var\(--z-modal-nested\)"/);
  });
});

test.describe('Static source checks — AddToPulseModal forwards zIndex (minimal additive change)', () => {
  const addModal = readFile('src/lib/AddToPulseModal.svelte');

  test('accepts a zIndex prop defaulting to 200 (unchanged default for every other caller)', () => {
    expect(addModal).toMatch(/zIndex = \/\*\* @type \{number \| string\} \*\/ \(200\)/);
  });

  test('forwards zIndex to the internal ModalShell', () => {
    expect(addModal).toMatch(/<ModalShell open=\{!!open\} \{onClose\} ariaLabel="Add to Pulse" \{zIndex\}>/);
  });
});

// ─────────────────────────────────────────────────────────────────────────
// Functional — real browser, full-screen card occlusion + Escape
// ─────────────────────────────────────────────────────────────────────────

test.describe('Functional — nested modals render above a full-screen Pulse card', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
  });

  test('AddToPulseModal: visible above full-screen Pinned/Watchlist card', async ({ page }) => {
    const expandBtn = page.locator('button[aria-label="Expand Pinned/Watchlist to fullscreen"]');
    await expect(expandBtn).toBeVisible({ timeout: 15_000 });
    await expandBtn.click();

    const card = page.locator('section.mp-bucket-pinwatch.fs-card-on');
    await expect(card).toBeVisible({ timeout: 5_000 });

    const addBtn = page.locator('button.mp-add-btn');
    await expect(addBtn).toBeVisible();
    await addBtn.click();

    const panel = page.locator('.search-modal').first();
    await expect(panel).toBeVisible({ timeout: 5_000 });

    const box = await panel.boundingBox();
    expect(box).not.toBeNull();
    const hit = await page.evaluate(({ x, y }) => {
      const el = document.elementFromPoint(x, y);
      return { insidePanel: !!el?.closest('.search-modal'), tag: el?.tagName };
    }, { x: box.x + box.width / 2, y: box.y + box.height / 2 });
    expect(hit.insidePanel, `expected .search-modal to own this point, got <${hit.tag}>`).toBe(true);

    const resolvedZ = await page.evaluate(() => {
      const overlays = Array.from(document.querySelectorAll('.ms-overlay'));
      const owning = overlays.find((el) => el.querySelector('.search-modal'));
      return owning ? Number(getComputedStyle(owning).zIndex) : null;
    });
    expect(resolvedZ).toBeGreaterThan(9999);
  });
});

// ─────────────────────────────────────────────────────────────────────────
// Wave C (2026-10-01) — AddToPulseModal's Escape handling was never
// migrated onto the layerStack coordinator (Wave B2 above only fixed its
// z-index). It relied entirely on ModalShell's own unconditional
// `<svelte:window onkeydown>`, plus a handful of per-input `onkeydown`
// branches for two sub-cases (cancel an in-progress rename; "close the
// typeahead suggestions first"). Pressing Escape while this modal was
// open over a fullscreen card (or any other already-migrated layer)
// closed BOTH at once.
//
// Fixed: a `$effect` (teardown form, matching BrokerHealthBadge.svelte's
// shape — safer than Select.svelte's if/else form against the host
// unmounting while this modal happens to be open) pushes one layer per
// open. Because the layerStack listener is capture-phase + stops
// propagation, the modal's own per-input Escape branches are now
// structurally unreachable — the genuinely visible one (cancelling an
// in-progress watchlist rename) is folded into the single layer callback
// instead of being ported as a second nested layer; the typeahead-closing
// branch was NOT ported because `typeaheadOpen` never actually gated the
// suggestion list's own markup (`{#if typeahead.length}`, independent of
// `typeaheadOpen`) — it was already a visual no-op before this fix.
// ─────────────────────────────────────────────────────────────────────────

test.describe('Static source checks — AddToPulseModal migrated onto layerStack (Wave C)', () => {
  const addModal = readFile('src/lib/AddToPulseModal.svelte');

  test('imports pushLayer/popLayer and pushes/pops via the teardown-effect form', () => {
    expect(addModal).toMatch(/import \{ pushLayer, popLayer \} from '\$lib\/utils\/layerStack\.js';/);
    expect(addModal).toMatch(/if \(!open\) return;\s*\n\s*const id = pushLayer\(\(\) => \{/);
    expect(addModal).toMatch(/return \(\) => popLayer\(id\);/);
  });

  test('layer callback cancels an in-progress rename instead of closing the whole modal', () => {
    expect(addModal).toMatch(/if \(renameId !== null && renameId === targetListId\) \{ onCancelRename\?\.\(\); return; \}/);
  });

  test('layer callback calls the real onClose prop (caller cleanup still runs), not a bare `open = false`', () => {
    expect(addModal).toMatch(/onClose\?\.\(\);\s*\n\s*\}\);\s*\n\s*return \(\) => popLayer\(id\);/);
  });

  test('now-unreachable per-input Escape branches are removed, not left as dead code', () => {
    // Old bug: three separate per-input onkeydown branches each raced
    // ModalShell's own listener to decide what Escape should do.
    expect(addModal).not.toMatch(/else if \(e\.key === 'Escape'\) \{ e\.preventDefault\(\); onCancelRename\(\); \}/);
    expect(addModal).not.toMatch(/else if \(e\.key === 'Escape'\) \{ e\.preventDefault\(\); onClose\(\); \}/);
    expect(addModal).not.toMatch(/if \(typeaheadOpen && typeahead\.length\) \{ typeaheadOpen = false; \}\s*\n\s*else \{ onClose\(\); \}/);
  });
});

test.describe('Functional — AddToPulseModal Escape coordination (real browser, Wave C)', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
  });

  test('AddToPulseModal opened over a fullscreen card: one Escape closes only AddToPulseModal', async ({ page }) => {
    const expandBtn = page.locator('button[aria-label="Expand Pinned/Watchlist to fullscreen"]');
    await expect(expandBtn).toBeVisible({ timeout: 15_000 });
    await expandBtn.click();
    const card = page.locator('section.mp-bucket-pinwatch.fs-card-on');
    await expect(card).toBeVisible({ timeout: 5_000 });

    const addBtn = page.locator('button.mp-add-btn');
    await expect(addBtn).toBeVisible();
    await addBtn.click();
    const panel = page.locator('.search-modal').first();
    await expect(panel, 'AddToPulseModal should open above the fullscreen card').toBeVisible({ timeout: 5_000 });

    // First Escape: closes ONLY AddToPulseModal (topmost, opened last).
    // Pre-fix, this Escape closed the fullscreen card underneath instead
    // (ModalShell's own listener never got a chance to coordinate with
    // the fullscreen card's own layer) and left AddToPulseModal open.
    await page.keyboard.press('Escape');
    await expect(page.locator('.search-modal'), 'AddToPulseModal should close on the first Escape').toHaveCount(0, { timeout: 3_000 });
    await expect(card, 'fullscreen card must still be open after the first Escape').toBeVisible();

    // Second Escape: now closes the fullscreen card.
    await page.keyboard.press('Escape');
    await expect(page.locator('section.mp-bucket-pinwatch.fs-card-on'), 'fullscreen card should close on the second Escape')
      .toHaveCount(0, { timeout: 3_000 });
  });

  test('AddToPulseModal + order modal (reachable via the "t" shortcut): one Escape closes only the topmost', async ({ page }) => {
    // Reachability: MarketPulse's own keydown handler pauses global
    // shortcuts only while focus sits on an INPUT/TEXTAREA/SELECT inside
    // it — AddToPulseModal's auto-focused symbol input is one of those,
    // but its own Select trigger / close button are plain <button>
    // elements, so focusing one of those and pressing the global `t`
    // shortcut (order ticket) opens the order modal on top. Confirmed
    // live before writing this test.
    const addBtn = page.locator('button.mp-add-btn');
    await expect(addBtn).toBeVisible({ timeout: 15_000 });
    await addBtn.click();
    const panel = page.locator('.search-modal').first();
    await expect(panel).toBeVisible({ timeout: 5_000 });

    await panel.locator('.search-close').focus();
    await page.keyboard.press('t');
    const overlay = page.locator('.canonical-modal-overlay').first();
    await expect(overlay, 'order modal should open on top of AddToPulseModal').toBeVisible({ timeout: 5_000 });

    // First Escape: closes ONLY the order modal (topmost — SymbolPanel
    // was already migrated onto layerStack in Wave A).
    await page.keyboard.press('Escape');
    await expect(page.locator('.canonical-modal-overlay'), 'order modal should close on the first Escape').toHaveCount(0, { timeout: 3_000 });
    await expect(page.locator('.search-modal'), 'AddToPulseModal must still be open after the first Escape').toBeVisible();

    // Second Escape: now closes AddToPulseModal.
    await page.keyboard.press('Escape');
    await expect(page.locator('.search-modal'), 'AddToPulseModal should close on the second Escape').toHaveCount(0, { timeout: 3_000 });
  });
});
