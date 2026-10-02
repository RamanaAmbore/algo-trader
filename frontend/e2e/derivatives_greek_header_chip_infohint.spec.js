/**
 * derivatives_greek_header_chip_infohint.spec.js
 *
 * The compact Payoff-header Greek chips (Δ Γ Θ 𝒱 ρ, ~lines 5538-5557 of
 * /admin/derivatives) used to carry a plain unstyled `title="..."` native
 * tooltip. They now reuse <InfoHint popup text="..."> with wording copied
 * VERBATIM from the "Greeks (position)" card (~lines 6123-6140), which was
 * already correct. This spec guards:
 *
 *   1. SSOT    — header-chip InfoHint text is byte-identical to the
 *                Greeks-card InfoHint text for all 5 Greeks (source parity).
 *   2. Perf    — click-to-popover opens within budget (no hang/regression);
 *                desktop is comfortably sub-500ms, budget set generously
 *                to also cover touch-emulated mobile projects' overhead on
 *                InfoHint's RAF-based viewport fit/position step.
 *   3. Stale   — no `title="..."` attribute remains on the 5 header chip
 *                spans (the old mechanism); InfoHint's own generic
 *                "Show details" button title is unaffected/expected.
 *   4. Reuse   — all 5 chips render `button.info-btn` (InfoHint component),
 *                not a hand-rolled tooltip.
 *   5. UX      — clicking/hovering a header chip's InfoHint shows text
 *                matching the known Greek description; popover carries
 *                role="tooltip".
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
 *  card (lines 6123, 6127, 6131, 6135, 6139) — the SSOT wording. */
const GREEK_TEXT = {
  delta: 'Delta — net directional exposure. +50 ≈ ₹50 gained per ₹1 spot rise. Includes +qty for enabled equity-holding legs.',
  gamma: 'Gamma — rate-of-change of delta as spot moves. High Γ = position is becoming more/less directional quickly.',
  theta: 'Theta — daily decay in rupees. Positive when net short premium. A Θ of −5 = position loses ₹5/day from time decay alone.',
  vega: 'Vega — P&L change per 1% IV move. Positive = long volatility (benefits from IV expansion).',
  rho: 'Rho — sensitivity to a 1% rate change. Mostly cosmetic for short-dated index options.',
};

// ── Suite 1: Source audit — byte-exact wording parity (always green, no network) ──

test.describe('Source audit — header-chip Greek InfoHint text matches Greeks card', () => {
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
      // Header chip must contain the SAME text via InfoHint, not `title=`.
      expect(chipsBlock, `Header chip missing InfoHint text for ${greek}`).toContain(text);
    });
  }

  test('no `title=` attribute remains on any of the 5 Greek header chip spans', () => {
    // Match each `<span class="opt-section-tag ... tag-greek ...">` opening
    // tag up to its `>` and assert none carry a `title=` attribute.
    const chipOpenTags = chipsBlock.match(/<span class="opt-section-tag[^>]*tag-greek[^>]*>/g) || [];
    expect(chipOpenTags.length).toBe(5);
    for (const tag of chipOpenTags) {
      expect(tag, `Stale title= attribute found on Greek chip: ${tag}`).not.toMatch(/\btitle=/);
    }
  });

  test('all 5 Greek header chips render <InfoHint', () => {
    const infoHintCount = (chipsBlock.match(/<InfoHint\s/g) || []).length;
    // EV chip (not a Greek, out of scope) keeps its native title — only
    // the 5 Greek chips should carry InfoHint inside this block.
    expect(infoHintCount).toBe(5);
  });
});

// ── Suite 2: Live DOM — chips render InfoHint, not a native title ────────────

test.describe('/admin/derivatives — Greek header chips render InfoHint', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });
  });

  test('Reuse + Stale: 5 tag-greek chips exist, each with button.info-btn, none with a bespoke title=', async ({ page }) => {
    const chips = page.locator('.opt-section-tag.tag-greek');
    await expect(chips).toHaveCount(5, { timeout: 20_000 });

    const count = await chips.count();
    for (let i = 0; i < count; i++) {
      const chip = chips.nth(i);
      // Reuse: InfoHint's own button is present.
      await expect(chip.locator('button.info-btn')).toHaveCount(1);
      // Stale: the chip span itself must not carry the old bespoke title.
      const title = await chip.getAttribute('title');
      expect(title, `Chip ${i} still has a native title attribute: "${title}"`).toBeNull();
    }
  });

  test('UX + Perf: clicking the Delta chip InfoHint opens a role=tooltip popover quickly with matching wording', async ({ page }) => {
    const chips = page.locator('.opt-section-tag.tag-greek');
    await expect(chips).toHaveCount(5, { timeout: 20_000 });

    // Delta is always the first of the 5 (Δ Γ Θ 𝒱 ρ order).
    const deltaChip = chips.nth(0);
    await expect(deltaChip).toContainText('Δ');

    const btn = deltaChip.locator('button.info-btn');
    await expect(btn).toBeVisible();

    const t0 = Date.now();
    await btn.click();
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

    // Close so later tests in this file aren't affected by a stray open popover.
    await page.keyboard.press('Escape');
  });

  test('UX: all 5 header chips open distinct, correctly-worded popovers', async ({ page }) => {
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
      const btn = chip.locator('button.info-btn');
      await btn.click();
      const popover = page.locator('[role="tooltip"]').first();
      await expect(popover).toBeVisible({ timeout: 2000 });
      const text = (await popover.textContent()) || '';
      expect(text, `Chip ${i} popover missing expected wording`).toContain(anchors[i]);
      await page.keyboard.press('Escape');
      await page.waitForTimeout(80);
    }
  });
});
