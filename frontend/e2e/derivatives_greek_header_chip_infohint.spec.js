/**
 * derivatives_greek_header_chip_infohint.spec.js
 *
 * The compact Payoff-header Greek chips (Δ Γ Θ 𝒱 ρ, ~lines 5546-5596 of
 * /admin/derivatives) first moved from a plain `title="..."` native
 * tooltip to <InfoHint popup text="..."> (commit 4a67445e), which added a
 * visible `(i)` button chip next to each value.
 *
 * Follow-up (this spec's current revision): the operator asked for the
 * GREEK VALUE TEXT ITSELF to be the click trigger, with no separate
 * visible `(i)` chip. Each chip now renders:
 *   - a plain-text-styled `<button class="greek-val-trigger">` carrying
 *     the Greek's numeric value (the only click target)
 *   - an `<InfoHint popup hideButton anchor={...} bind:open={...}>` with
 *     the SAME wording as before, rendering no button of its own
 *
 * This spec guards:
 *
 *   1. SSOT    — header-chip InfoHint text is byte-identical to the
 *                Greeks-card InfoHint text for all 5 Greeks (source parity).
 *   2. Hidden  — all 5 header-chip InfoHint instances pass `hideButton`;
 *                the Greeks-card instances (still the (i)-button mode) do
 *                NOT. No `.info-btn` renders anywhere inside a header chip.
 *   3. Perf    — click-to-popover opens within budget (no hang/regression).
 *   4. UX      — clicking the Greek VALUE opens a role="tooltip" popover
 *                with the matching wording; clicking the same value again
 *                CLOSES it (verifies the anchor click-outside-exemption
 *                fix in InfoHint.svelte, not just that opening works).
 *   5. Isolation — opening one Greek's popover and then another closes
 *                  the first and shows only the second's text (per-chip
 *                  independent state, no cross-talk).
 *   6. Regression — the pre-existing Greeks (position) CARD (a sibling,
 *                    unrelated InfoHint consumer on the same page) is
 *                    UNCHANGED: still renders its own visible `.info-btn`
 *                    and still opens/shows the same wording via it.
 *
 * Run locally (auto-starts local vite dev server, proxies /api to
 * dev.ramboq.com per vite.config.js):
 *   npx playwright test e2e/derivatives_greek_header_chip_infohint.spec.js \
 *     --project=chromium-desktop
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import { resolve } from 'path';
import { loginAsAdmin } from './fixtures/auth.js';

const PAGE_PATH = resolve(process.cwd(), 'src/routes/(algo)/admin/derivatives/+page.svelte');

/** Known-good Greek descriptions, copied verbatim from the Greeks (position)
 *  card — the SSOT wording. */
const GREEK_TEXT = {
  delta: 'Delta — net directional exposure. +50 ≈ ₹50 gained per ₹1 spot rise. Includes +qty for enabled equity-holding legs.',
  gamma: 'Gamma — rate-of-change of delta as spot moves. High Γ = position is becoming more/less directional quickly.',
  theta: 'Theta — daily decay in rupees. Positive when net short premium. A Θ of −5 = position loses ₹5/day from time decay alone.',
  vega: 'Vega — P&L change per 1% IV move. Positive = long volatility (benefits from IV expansion).',
  rho: 'Rho — sensitivity to a 1% rate change. Mostly cosmetic for short-dated index options.',
};

// ── Suite 1: Source audit — byte-exact wording parity + hideButton wiring (always green, no network) ──

test.describe('Source audit — header-chip Greek InfoHint text + hideButton wiring', () => {
  const src = readFileSync(PAGE_PATH, 'utf8');

  // Isolate the header-chips block (opt-section-chips) and the Greeks-card
  // block (opt-kv-greeks) independently so we're comparing the right pair
  // of occurrences even though each Greek's text appears twice in the file.
  const chipsBlockStart = src.indexOf('<div class="opt-section-chips">');
  const chipsBlockEnd   = src.indexOf('{/snippet}', chipsBlockStart);
  const chipsBlock      = src.slice(chipsBlockStart, chipsBlockEnd);

  const cardBlockStart = src.indexOf('<div class="opt-kv opt-kv-greeks">');
  // Bounded by the next card's heading rather than a brittle whitespace-
  // sensitive closing-tag match — "Risk &amp; expected value" is the
  // next sibling card's title, guaranteed to appear immediately after
  // the Greeks card closes.
  const cardBlockEnd   = src.indexOf('Risk &amp; expected value', cardBlockStart);
  const cardBlock      = src.slice(cardBlockStart, cardBlockEnd);

  test('header-chips block and Greeks-card block were both located', () => {
    expect(chipsBlockStart).toBeGreaterThan(-1);
    expect(cardBlockStart).toBeGreaterThan(-1);
    expect(chipsBlock.length).toBeGreaterThan(50);
    expect(cardBlock.length).toBeGreaterThan(50);
  });

  for (const [greek, text] of Object.entries(GREEK_TEXT)) {
    test(`${greek}: header chip InfoHint text matches Greeks card verbatim`, () => {
      // Card must contain the SSOT text (sanity on our own fixture).
      expect(cardBlock, `Greeks card missing expected ${greek} text`).toContain(text);
      // Header chip must contain the SAME text via InfoHint.
      expect(chipsBlock, `Header chip missing InfoHint text for ${greek}`).toContain(text);
    });
  }

  test('no `title=` attribute remains on any of the 5 Greek header chip spans', () => {
    const chipOpenTags = chipsBlock.match(/<span class="opt-section-tag[^>]*tag-greek[^>]*>/g) || [];
    expect(chipOpenTags.length).toBe(5);
    for (const tag of chipOpenTags) {
      expect(tag, `Stale title= attribute found on Greek chip: ${tag}`).not.toMatch(/\btitle=/);
    }
  });

  test('all 5 Greek header chips render <InfoHint hideButton>, not the (i)-button mode', () => {
    const infoHintCount = (chipsBlock.match(/<InfoHint\s/g) || []).length;
    // EV chip (not a Greek, out of scope) keeps its native title — only
    // the 5 Greek chips should carry InfoHint inside this block.
    expect(infoHintCount).toBe(5);
    const hideButtonCount = (chipsBlock.match(/hideButton/g) || []).length;
    expect(hideButtonCount).toBe(5);
  });

  test('the Greeks-card InfoHint instances do NOT pass hideButton (unaffected sibling consumer)', () => {
    const cardInfoHintCount = (cardBlock.match(/<InfoHint\s/g) || []).length;
    expect(cardInfoHintCount).toBeGreaterThanOrEqual(5);
    expect(cardBlock).not.toMatch(/hideButton/);
  });

  test('each Greek header chip has a plain-text `.greek-val-trigger` click target carrying the value', () => {
    const triggerCount = (chipsBlock.match(/class="greek-val-trigger"/g) || []).length;
    expect(triggerCount).toBe(5);
    // No visible .info-btn chip markup inside the header-chips block at all.
    expect(chipsBlock).not.toMatch(/class="info-btn"/);
  });
});

// ── Suite 2: Live DOM — header chips trigger via value text, Greeks card unaffected ────────────

test.describe('/admin/derivatives — Greek header chips open via value click, no visible (i) chip', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });
  });

  test('Reuse + Stale: 5 tag-greek chips exist, each with a value trigger, none with a visible .info-btn or bespoke title=', async ({ page }) => {
    const chips = page.locator('.opt-section-tag.tag-greek');
    await expect(chips).toHaveCount(5, { timeout: 20_000 });

    const count = await chips.count();
    for (let i = 0; i < count; i++) {
      const chip = chips.nth(i);
      // No visible (i) button chip — operator's explicit ask.
      await expect(chip.locator('button.info-btn')).toHaveCount(0);
      // The value itself is the click target.
      await expect(chip.locator('button.greek-val-trigger')).toHaveCount(1);
      // Stale: the chip span itself must not carry the old bespoke title.
      const title = await chip.getAttribute('title');
      expect(title, `Chip ${i} still has a native title attribute: "${title}"`).toBeNull();
    }
  });

  test('UX + Perf: clicking the Delta value opens a role=tooltip popover quickly with matching wording, and clicking again closes it', async ({ page }) => {
    const chips = page.locator('.opt-section-tag.tag-greek');
    await expect(chips).toHaveCount(5, { timeout: 20_000 });

    // Delta is always the first of the 5 (Δ Γ Θ 𝒱 ρ order).
    const deltaChip = chips.nth(0);
    await expect(deltaChip).toContainText('Δ');

    const trigger = deltaChip.locator('button.greek-val-trigger');
    await expect(trigger).toBeVisible();

    const t0 = Date.now();
    await trigger.click();
    const popover = page.locator('[role="tooltip"]').first();
    await expect(popover).toBeVisible({ timeout: 2000 });
    const elapsed = Date.now() - t0;
    // Generous cross-viewport budget — touch-emulated mobile projects
    // (isMobile/hasTouch) add real overhead to the RAF-based viewport
    // fit step that a plain click doesn't hit on desktop. Still tight
    // enough to catch a genuine hang/regression.
    expect(elapsed).toBeLessThan(1_200);

    const text = (await popover.textContent()) || '';
    expect(text).toContain('net directional exposure');
    expect(text).toContain('equity-holding legs');

    // Clicking the SAME value again must close it — guards the
    // anchor-exemption fix in InfoHint.svelte's click-outside listener;
    // without it, the mousedown-driven close races the trigger's own
    // click handler and the popover can never be closed by re-clicking
    // its own trigger.
    await trigger.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0);
  });

  test('Isolation: opening Gamma after Delta closes Delta and shows only Gamma text', async ({ page }) => {
    const chips = page.locator('.opt-section-tag.tag-greek');
    await expect(chips).toHaveCount(5, { timeout: 20_000 });

    const deltaTrigger = chips.nth(0).locator('button.greek-val-trigger');
    const gammaTrigger = chips.nth(1).locator('button.greek-val-trigger');

    await deltaTrigger.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(1);
    await expect(page.locator('[role="tooltip"]').first()).toContainText('net directional exposure');

    await gammaTrigger.click();
    const tooltips = page.locator('[role="tooltip"]');
    await expect(tooltips).toHaveCount(1);
    await expect(tooltips.first()).toContainText('rate-of-change of delta');
    await expect(tooltips.first()).not.toContainText('net directional exposure');
  });

  test('UX: all 5 header chips open distinct, correctly-worded popovers via their value trigger', async ({ page }) => {
    const chips = page.locator('.opt-section-tag.tag-greek');
    await expect(chips).toHaveCount(5, { timeout: 20_000 });

    const anchors = [
      'net directional exposure',          // delta
      'rate-of-change of delta',           // gamma
      'daily decay in rupees',             // theta
      'P&L change per 1% IV move',         // vega
      'sensitivity to a 1% rate change',   // rho
    ];

    for (let i = 0; i < 5; i++) {
      const chip = chips.nth(i);
      const trigger = chip.locator('button.greek-val-trigger');
      await trigger.click();
      const popover = page.locator('[role="tooltip"]').first();
      await expect(popover).toBeVisible({ timeout: 2000 });
      const text = (await popover.textContent()) || '';
      expect(text, `Chip ${i} popover missing expected wording`).toContain(anchors[i]);
      // Close via the same trigger before moving to the next chip.
      await trigger.click();
      await expect(page.locator('[role="tooltip"]')).toHaveCount(0);
    }
  });

  test('Regression: Greeks (position) card — unrelated sibling InfoHint consumer — is unchanged', async ({ page }) => {
    // The card still uses the (i)-button mode (no hideButton); scope the
    // locator to the card's own container so we don't pick up a header
    // chip's popover from an earlier test in this file.
    const card = page.locator('.opt-kv.opt-kv-greeks');
    await expect(card).toBeVisible({ timeout: 20_000 });

    const cardDeltaBtn = card.locator('button.info-btn').first();
    await expect(cardDeltaBtn).toBeVisible();
    await cardDeltaBtn.click();
    const popover = page.locator('[role="tooltip"]').first();
    await expect(popover).toBeVisible({ timeout: 2000 });
    const text = (await popover.textContent()) || '';
    expect(text).toContain('net directional exposure');
  });
});
