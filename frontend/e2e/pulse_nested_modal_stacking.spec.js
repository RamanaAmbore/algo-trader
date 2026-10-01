/**
 * pulse_nested_modal_stacking.spec.js
 *
 * Wave B2 of the modal/menu/toast stacking-defect audit (2026-09-30) —
 * three modals openable from a button INSIDE a full-screen MarketPulse
 * card previously rendered BEHIND that card:
 *
 *   1. AddToPulseModal       — "+" button, Pinned/Watchlist full-screen card
 *   2. OrderPairModal        — "⟷ Pair" button, Positions full-screen card
 *   3. MarketPulse strike picker (ModalShell) — opened after an AddToPulse
 *      pick; covered via static source check only (reaching it live
 *      requires a real F&O-underlying typeahead backend round-trip, which
 *      this spec does not depend on for determinism).
 *
 * OrderPairModal also had NO Escape handling at all — a second, distinct
 * bug surfaced once the stacking fix makes the modal visible: Escape
 * exited the parent full-screen card but left the modal open on top of
 * the normal page. Fixed via the layerStack coordinator (same
 * `pushLayer`/`popLayer` pattern as stacking_defect_fixes.spec.js's
 * Wave A migrations), with a teardown-effect form (not the if/else form)
 * because this modal unmounts via `{#if _pairModalOpen}` in the same
 * reactive flush that flips `open` to false.
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

test.describe('Static source checks — OrderPairModal z-index, portal, Escape', () => {
  const opm = readFile('src/lib/order/OrderPairModal.svelte');

  test('overlay no longer uses the bare 9000 literal — uses --z-modal-nested', () => {
    expect(opm).not.toMatch(/z-index:\s*9000/);
    expect(opm).toMatch(/z-index:\s*var\(--z-modal-nested\)/);
  });

  test('overlay is portalled to document.body', () => {
    expect(opm).toMatch(/import \{ portal \} from '\$lib\/portal';/);
    expect(opm).toMatch(/<div class="opm-overlay"[^>]*use:portal/);
  });

  test('modal is wired through the layerStack coordinator (teardown-effect form)', () => {
    expect(opm).toMatch(/import \{ pushLayer, popLayer \} from '\$lib\/utils\/layerStack\.js';/);
    expect(opm).toMatch(/const id = pushLayer\(\(\) => \{ open = false; \}\);/);
    expect(opm).toMatch(/return \(\) => popLayer\(id\);/);
  });
});

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

  test('OrderPairModal: visible above full-screen Positions card, Escape closes without leaking (full-screen survives)', async ({ page }) => {
    const expandBtn = page.locator('button[aria-label="Expand Positions to fullscreen"]');
    await expect(expandBtn).toBeVisible({ timeout: 15_000 });
    await expandBtn.click();

    // Full-screen card now at z=9999.
    const card = page.locator('section.mp-bucket-positions.fs-card-on');
    await expect(card).toBeVisible({ timeout: 5_000 });

    const pairBtn = page.locator('button.mp-pair-btn');
    await expect(pairBtn).toBeVisible();
    await pairBtn.click();

    const panel = page.locator('.opm-card');
    await expect(panel).toBeVisible({ timeout: 5_000 });

    // Occlusion check — the audit's own verification method: hit-test the
    // panel's visual centre and confirm the modal itself answers, not the
    // ag-Grid viewport underneath.
    const box = await panel.boundingBox();
    expect(box, 'panel must have a bounding box').not.toBeNull();
    const hit = await page.evaluate(({ x, y }) => {
      const el = document.elementFromPoint(x, y);
      return {
        insidePanel: !!el?.closest('.opm-card'),
        tag: el?.tagName,
      };
    }, { x: box.x + box.width / 2, y: box.y + box.height / 2 });
    expect(hit.insidePanel, `expected the opm-card to own this point, got <${hit.tag}>`).toBe(true);

    // Computed z-index must resolve the --z-modal-nested token, not 9000.
    const resolvedZ = await page.evaluate(() => {
      const el = document.querySelector('.opm-overlay');
      return Number(getComputedStyle(el).zIndex);
    });
    const nestedToken = await page.evaluate(() =>
      Number(getComputedStyle(document.documentElement).getPropertyValue('--z-modal-nested'))
    );
    expect(resolvedZ).toBe(nestedToken);
    expect(resolvedZ).toBeGreaterThan(9999); // above the full-screen card tier

    // Escape closes ONLY the pair modal — the full-screen card must survive
    // (this is the leak/over-close guard: layerStack's capture-phase
    // stopPropagation must consume the key before DefaultSizeButton's own
    // bubble-phase listener sees it).
    await page.keyboard.press('Escape');
    await expect(panel).toHaveCount(0, { timeout: 3_000 });
    await expect(card).toBeVisible();

    // A second Escape now reaches the full-screen card's own handler.
    await page.keyboard.press('Escape');
    await expect(card).toHaveCount(0, { timeout: 3_000 });
  });

  test('OrderPairModal: Cancel button also pops the layer cleanly (no leak into a later Escape)', async ({ page }) => {
    const expandBtn = page.locator('button[aria-label="Expand Positions to fullscreen"]');
    await expandBtn.click();
    const card = page.locator('section.mp-bucket-positions.fs-card-on');
    await expect(card).toBeVisible({ timeout: 5_000 });

    await page.locator('button.mp-pair-btn').click();
    const panel = page.locator('.opm-card');
    await expect(panel).toBeVisible({ timeout: 5_000 });

    // Close via the Cancel button (not Escape) — this is the non-Escape
    // close path; its effect-cleanup must still pop the layer.
    await page.locator('.opm-cancel').click();
    await expect(panel).toHaveCount(0, { timeout: 3_000 });

    // If the Cancel path leaked a layer, this Escape would be swallowed by
    // the dead layer instead of reaching the full-screen card.
    await page.keyboard.press('Escape');
    await expect(card).toHaveCount(0, { timeout: 3_000 });
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
