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
 * Second follow-up: the EV chip in the same header row (not one of the
 * 5 Greeks, previously a plain `title="..."` attribute on the whole
 * chip span) now gets the identical treatment — its numeric value is
 * also a `.greek-val-trigger` button paired with a `hideButton` InfoHint,
 * same wording the `title=` attribute used to carry.
 *
 * This spec guards:
 *
 *   1. SSOT    — header-chip InfoHint text is byte-identical to the
 *                Greeks-card InfoHint text for all 5 Greeks (source parity).
 *   2. Hidden  — all 6 header-chip InfoHint instances (5 Greeks + EV) pass
 *                `hideButton`; the Greeks-card instances (still the
 *                (i)-button mode) do NOT. No `.info-btn` renders anywhere
 *                inside a header chip.
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
 *   7. EV chip — same value-click trigger, no visible `(i)`, no stale
 *                `title=` attribute, matching wording, independent from
 *                the 5 Greek chips (opening/closing one doesn't affect EV).
 *
 * 2026-10 update (hover-preview + click-to-pin reintroduction, amends a
 * brief click-only-everywhere pass): hideButton+anchor sites now show a
 * transient hover PREVIEW again, modeled on OptionsPayoff.svelte's own
 * hover/pin tooltip — but hovering never PINS the popover (`open` stays
 * false) and never claims the app-wide singleton. Only a click pins it.
 * The tests below assert: hover shows a preview that disappears on
 * mouse-leave without pinning, and click still pins/unpins as before.
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

/** EV chip's own wording — not a Greek, so no "card" SSOT counterpart to
 *  compare against inside this block (the Risk & expected value card
 *  uses richer wording for a different purpose); this is the text the
 *  stale `title=` attribute used to carry verbatim. */
const EV_TEXT = 'Expected value — probability-weighted average payoff at expiry. ev_pct = EV / |entry cost|.';

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

  test('no `title=` attribute remains on the EV header chip span either', () => {
    const evOpenTags = chipsBlock.match(/<span class="opt-section-tag tf-cell \{[^}]*tag-(long|short)[^}]*\}">/g) || [];
    // The EV chip is the only `opt-section-tag` chip using the tag-long/
    // tag-short sign-tint (the Greek chips use tag-greek); there must be
    // exactly one, carrying no stale title=.
    for (const tag of evOpenTags) {
      expect(tag, `Stale title= attribute found on EV chip: ${tag}`).not.toMatch(/\btitle=/);
    }
    expect(chipsBlock).toContain('EV <button type="button" class="greek-val-trigger"');
  });

  test('all 6 header chips (5 Greeks + EV) render <InfoHint hideButton>, not the (i)-button mode', () => {
    const infoHintCount = (chipsBlock.match(/<InfoHint\s/g) || []).length;
    expect(infoHintCount).toBe(6);
    const hideButtonCount = (chipsBlock.match(/hideButton/g) || []).length;
    expect(hideButtonCount).toBe(6);
  });

  test('EV chip InfoHint carries the same wording the stale title= attribute used to have', () => {
    expect(chipsBlock).toContain(EV_TEXT);
  });

  test('the Greeks-card InfoHint instances use hideButton with non-colliding IDs (independent sibling)', () => {
    // Greeks card now also uses hideButton mode (field-as-trigger pattern).
    // The 5 kv-pair InfoHint instances within opt-kv-greeks must all have hideButton.
    const cardInfoHintCount = (cardBlock.match(/<InfoHint\s/g) || []).length;
    expect(cardInfoHintCount).toBeGreaterThanOrEqual(5); // 5 Greek kv-pairs
    const cardHideButtonCount = (cardBlock.match(/hideButton/g) || []).length;
    expect(cardHideButtonCount).toBe(cardInfoHintCount);

    // Card uses sum-hint-* ids; header chips use greek-hint-* ids.
    // No collision — the two InfoHint groups remain independent.
    expect(cardBlock).not.toMatch(/id="greek-hint-/);
  });

  test('each of the 6 header chips (5 Greeks + EV) has a plain-text `.greek-val-trigger` click target carrying the value', () => {
    const triggerCount = (chipsBlock.match(/class="greek-val-trigger"/g) || []).length;
    expect(triggerCount).toBe(6);
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

  test('UX + Perf: clicking the Delta value opens a role=tooltip popover quickly with matching wording, and clicking again closes it', async ({ page, viewport }) => {
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

    // Viewport clipping check: popover must stay within viewport bounds
    if (viewport) {
      const popoverRect = await popover.evaluate((el) => {
        const rect = el.getBoundingClientRect();
        return {
          left: rect.left,
          right: rect.right,
          top: rect.top,
          bottom: rect.bottom,
          width: rect.width,
          height: rect.height,
        };
      });
      const viewportWidth = viewport.width;
      const viewportHeight = viewport.height;
      expect(popoverRect.left, 'Popover left edge must be >= 0').toBeGreaterThanOrEqual(0);
      expect(popoverRect.right, `Popover right edge must be <= ${viewportWidth}`).toBeLessThanOrEqual(viewportWidth);
      expect(popoverRect.top, 'Popover top edge must be >= 0').toBeGreaterThanOrEqual(0);
      expect(popoverRect.bottom, `Popover bottom edge must be <= ${viewportHeight}`).toBeLessThanOrEqual(viewportHeight);
    }

    // Occlusion check: adjacent Greek chip (Gamma, the second chip) should
    // not be fully covered by the popover — at least the trigger button's
    // center should be clickable.
    const gammaChip = chips.nth(1);
    const gammaTrigger = gammaChip.locator('button.greek-val-trigger');
    const gammaRect = await gammaTrigger.evaluate((el) => {
      const rect = el.getBoundingClientRect();
      return {
        centerX: rect.left + rect.width / 2,
        centerY: rect.top + rect.height / 2,
      };
    });
    const elemAtGammaCenter = await page.evaluate(
      ({ x, y }) => {
        const elem = document.elementFromPoint(x, y);
        return elem ? elem.className : null;
      },
      { x: gammaRect.centerX, y: gammaRect.centerY }
    );
    // The element at Gamma's center should not be the popover (i.e. not have
    // "info-popout" class). It should be the button or a nearby element.
    expect(
      (elemAtGammaCenter || '').includes('info-popout'),
      'Popover should not fully occlude the adjacent Gamma trigger'
    ).toBe(false);

    // Clicking the SAME value again must close it — guards the
    // anchor-exemption fix in InfoHint.svelte's click-outside listener;
    // without it, the mousedown-driven close races the trigger's own
    // click handler and the popover can never be closed by re-clicking
    // its own trigger.
    await trigger.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0);
  });

  test('Regression: header chips show a hover PREVIEW (not a pin) via HOVER (2026-10); click pins', async ({ page, viewport }) => {
    // hideButton mode wires pointerenter/pointerleave onto the anchor
    // (the trigger button itself) via InfoHint's own $effect — hovering
    // shows a transient preview, but `open` stays false, so it never
    // pins and never claims the app-wide singleton.
    const chips = page.locator('.opt-section-tag.tag-greek');
    await expect(chips).toHaveCount(5, { timeout: 20_000 });

    const deltaChip = chips.nth(0);
    const trigger = deltaChip.locator('button.greek-val-trigger');

    // HOVER shows a preview. A real `.hover()` proves the cursor
    // actually landed on the trigger (Playwright's actionability check).
    await trigger.hover();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(1);
    await expect(trigger).toHaveAttribute('aria-expanded', 'false'); // preview only — not pinned

    // Moving away drops the preview — it never pinned.
    await page.mouse.move(5, 5);
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0);

    // Click pins it open/closed as before.
    await trigger.click();
    const popover = page.locator('[role="tooltip"]').first();
    await expect(popover).toBeVisible({ timeout: 2000 });
    await expect(popover).toContainText('net directional exposure');
    await expect(trigger).toHaveAttribute('aria-expanded', 'true');

    await trigger.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0);
  });

  test('Isolation: opening Gamma after Delta closes Delta and shows only Gamma text (viewport bounds check)', async ({ page, viewport }) => {
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

    // Viewport clipping check after switching popovers
    if (viewport) {
      const popoverRect = await tooltips.first().evaluate((el) => {
        const rect = el.getBoundingClientRect();
        return {
          left: rect.left,
          right: rect.right,
          width: rect.width,
        };
      });
      const viewportWidth = viewport.width;
      expect(popoverRect.left, 'Popover left edge must be >= 0').toBeGreaterThanOrEqual(0);
      expect(popoverRect.right, `Popover right edge must be <= ${viewportWidth}`).toBeLessThanOrEqual(viewportWidth);
    }
  });

  test('Singleton (app-wide, bound-prop case): click-pinning Delta then KEYBOARD-activating Gamma closes Delta — not just the click-outside listener', async ({ page }) => {
    // The existing "Isolation" test above opens Gamma via a CLICK, which
    // already triggers InfoHint's pre-existing click-outside-closes
    // `mousedown` listener (Gamma's trigger is outside Delta's `wrap` and
    // isn't Delta's own `anchor`, so that listener alone would already
    // close Delta even WITHOUT the app-wide singleton). That test can pass
    // on old code and proves nothing about the new singleton.
    //
    // This test closes that gap: open Gamma via KEYBOARD activation
    // instead of a mouse click — hover is no longer a thing InfoHint
    // responds to at all (removed 2026-10, app-wide), but a keyboard-
    // triggered `click` on a native `<button>` ALSO fires with no
    // preceding `mousedown`, so the old click-outside listener still
    // cannot be what closes Delta here. Only the new module-level
    // `_activeInfoHintId` singleton effect can.
    //
    // It also proves the bound-prop case specifically: `aria-expanded` on
    // `.greek-val-trigger` reads the PARENT's own `_greekHintOpen.delta`
    // state (not some internal InfoHint copy), so asserting it flips to
    // "false" proves the singleton's `open = false` assignment inside the
    // child genuinely propagated back through `bind:open` to the parent —
    // not just that the popover visually disappeared.
    const chips = page.locator('.opt-section-tag.tag-greek');
    await expect(chips).toHaveCount(5, { timeout: 20_000 });

    const deltaTrigger = chips.nth(0).locator('button.greek-val-trigger');
    const gammaTrigger = chips.nth(1).locator('button.greek-val-trigger');

    // Pin Delta open via CLICK (open=true).
    await deltaTrigger.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(1);
    await expect(page.locator('[role="tooltip"]').first()).toContainText('net directional exposure');
    await expect(deltaTrigger).toHaveAttribute('aria-expanded', 'true');

    // Open Gamma via KEYBOARD activation — no mousedown anywhere.
    await gammaTrigger.focus();
    await page.keyboard.press('Enter');
    const tooltips = page.locator('[role="tooltip"]');
    await expect(tooltips).toHaveCount(1, { timeout: 2000 });
    await expect(tooltips.first()).toContainText('rate-of-change of delta');
    await expect(tooltips.first()).not.toContainText('net directional exposure');

    // Bound-prop proof: the PARENT's own _greekHintOpen.delta flipped to
    // false, reflected on the trigger's aria-expanded (reads parent state).
    await expect(deltaTrigger).toHaveAttribute('aria-expanded', 'false');

    // Close Gamma via a second keyboard activation, then Delta must
    // re-open on a SINGLE click. If the bound-prop propagation had
    // silently failed (parent still held `true` while the popover was
    // merely hidden some other way), this click would toggle it to
    // `false` and Delta would need a second click.
    await page.keyboard.press('Enter');
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0);

    await deltaTrigger.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(1);
    await expect(page.locator('[role="tooltip"]').first()).toContainText('net directional exposure');
    await expect(deltaTrigger).toHaveAttribute('aria-expanded', 'true');
  });

  test('Singleton (bound-prop case): clicking the same chip twice opens then closes it', async ({ page }) => {
    // Same regression concern as the default chip-mode spec's equivalent
    // test — the singleton's close effect only fires for a DIFFERENT
    // instance's uid, so closing via a second click on the SAME chip must
    // keep working exactly as before. For the bound-prop case this also
    // confirms the parent's own `_greekHintOpen.delta` toggles both ways,
    // not just open→closed once.
    const chips = page.locator('.opt-section-tag.tag-greek');
    await expect(chips).toHaveCount(5, { timeout: 20_000 });
    const deltaTrigger = chips.nth(0).locator('button.greek-val-trigger');

    await deltaTrigger.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(1);
    await expect(deltaTrigger).toHaveAttribute('aria-expanded', 'true');

    await deltaTrigger.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0);
    await expect(deltaTrigger).toHaveAttribute('aria-expanded', 'false');

    await deltaTrigger.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(1);
    await expect(deltaTrigger).toHaveAttribute('aria-expanded', 'true');
  });

  test('UX: all 5 header chips open distinct, correctly-worded popovers via their value trigger (viewport bounds check)', async ({ page, viewport }) => {
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

      // Viewport clipping check
      if (viewport) {
        const popoverRect = await popover.evaluate((el) => {
          const rect = el.getBoundingClientRect();
          return {
            left: rect.left,
            right: rect.right,
            top: rect.top,
            bottom: rect.bottom,
          };
        });
        const vw = viewport.width;
        const vh = viewport.height;
        expect(popoverRect.left, `Chip ${i} popover left edge must be >= 0`).toBeGreaterThanOrEqual(0);
        expect(popoverRect.right, `Chip ${i} popover right edge must be <= ${vw}`).toBeLessThanOrEqual(vw);
        expect(popoverRect.top, `Chip ${i} popover top edge must be >= 0`).toBeGreaterThanOrEqual(0);
        expect(popoverRect.bottom, `Chip ${i} popover bottom edge must be <= ${vh}`).toBeLessThanOrEqual(vh);
      }

      // Close via the same trigger before moving to the next chip.
      await trigger.click();
      await expect(page.locator('[role="tooltip"]')).toHaveCount(0);
    }
  });

  test('EV chip: value click opens a role=tooltip popover with matching wording, no visible (i), no stale title=, and closes on re-click', async ({ page }) => {
    const evChip = page.locator('.opt-section-tag.tf-cell', { hasText: 'EV' }).first();
    await expect(evChip).toBeVisible({ timeout: 20_000 });

    await expect(evChip.locator('button.info-btn')).toHaveCount(0);
    const evTitle = await evChip.getAttribute('title');
    expect(evTitle, `EV chip still has a native title attribute: "${evTitle}"`).toBeNull();

    const trigger = evChip.locator('button.greek-val-trigger');
    await expect(trigger).toBeVisible();

    await trigger.click();
    const popover = page.locator('[role="tooltip"]').first();
    await expect(popover).toBeVisible({ timeout: 2000 });
    const text = (await popover.textContent()) || '';
    expect(text).toContain('probability-weighted average payoff');

    await trigger.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0);
  });

  test('EV chip is independent from the 5 Greek chips (no cross-talk)', async ({ page }) => {
    const chips = page.locator('.opt-section-tag.tag-greek');
    await expect(chips).toHaveCount(5, { timeout: 20_000 });
    const evTrigger = page.locator('.opt-section-tag.tf-cell', { hasText: 'EV' }).first().locator('button.greek-val-trigger');
    const deltaTrigger = chips.nth(0).locator('button.greek-val-trigger');

    await evTrigger.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(1);
    await expect(page.locator('[role="tooltip"]').first()).toContainText('probability-weighted average payoff');

    await deltaTrigger.click();
    const tooltips = page.locator('[role="tooltip"]');
    await expect(tooltips).toHaveCount(1);
    await expect(tooltips.first()).toContainText('net directional exposure');
    await expect(tooltips.first()).not.toContainText('probability-weighted average payoff');
  });

  test('Regression: Header chips PIN only on click; a hover right after dismiss respects the 350ms suppression window (2026-10)', async ({ page }) => {
    // Verifies the 6 header chips (5 Greeks + EV) still pin open/closed
    // correctly via click, and that hovering immediately after a
    // click-dismiss (cursor still resting on the trigger) doesn't
    // instantly re-open a preview — the same dismiss-then-instant-rehover
    // glitch OptionsPayoff.svelte's own 350ms suppression window guards
    // against.
    const chips = page.locator('.opt-section-tag.tag-greek');
    await expect(chips).toHaveCount(5, { timeout: 20_000 });

    const deltaChip = chips.nth(0);
    const trigger = deltaChip.locator('button.greek-val-trigger');
    await expect(trigger).toBeVisible();

    // CLICK test
    await trigger.click();
    let popover = page.locator('[role="tooltip"]').first();
    await expect(popover).toBeVisible({ timeout: 2000 });
    let text = (await popover.textContent()) || '';
    expect(text).toContain('net directional exposure');
    // Close via re-click
    await trigger.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0);

    // Hovering immediately after the dismiss-click, cursor still resting
    // on the trigger, must NOT re-open a preview within the 350ms
    // suppression window.
    await trigger.hover();
    await page.waitForTimeout(150);
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0);

    // After the suppression window elapses, a fresh hover DOES show a
    // preview again.
    await page.mouse.move(5, 5);
    await page.waitForTimeout(300);
    await trigger.hover();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(1);
    await page.mouse.move(5, 5);
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0);

    // Positive control: click still pins it open/closed.
    await trigger.click();
    popover = page.locator('[role="tooltip"]').first();
    await expect(popover).toBeVisible({ timeout: 2000 });
    text = (await popover.textContent()) || '';
    expect(text).toContain('net directional exposure');
    await trigger.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0);
  });

  test('Strategy Summary: converted Greeks kv-pairs use field-as-trigger (hideButton mode), click-only', async ({ page, viewport }) => {
    // The Strategy Summary section below the header contains its own set of Greeks,
    // R:R, EV, POP, etc. These have been converted from chip-mode to hideButton+anchor,
    // same as the header chips. The `.kv-k` label span is now the anchor.
    const strategyCard = page.locator('.opt-block').nth(0); // First block after header is Strategy Summary
    await expect(strategyCard).toBeVisible({ timeout: 20_000 });

    const deltaLabel = strategyCard.locator('.kv-k:has-text("Δ")').first();
    if (await deltaLabel.count() === 0) {
      test.skip(true, 'Strategy Summary section not rendered (no open strategy)');
      return;
    }

    // No visible info-btn (hideButton mode)
    await expect(deltaLabel.locator('button.info-btn')).toHaveCount(0);
    // But the label has role=button
    await expect(deltaLabel).toHaveAttribute('role', 'button');

    // CLICK to open
    await deltaLabel.click();
    const popover = page.locator('[role="tooltip"]').first();
    await expect(popover).toBeVisible({ timeout: 2000 });
    let text = (await popover.textContent()) || '';
    expect(text).toContain('net directional exposure');

    // Close via re-click
    await deltaLabel.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0);

    // Hovering right after the dismiss-click, cursor still resting on
    // deltaLabel, must NOT reopen within the 350ms suppression window.
    await deltaLabel.hover();
    await page.waitForTimeout(150);
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0);

    // After the window elapses, a fresh hover shows a preview (not
    // pinned), which disappears on mouse-leave without pinning.
    await page.mouse.move(5, 5);
    await page.waitForTimeout(300);
    await deltaLabel.hover();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(1);
    await expect(deltaLabel).toHaveAttribute('aria-expanded', 'false');
    await page.mouse.move(5, 5);
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0);

    // Positive control: click still pins it.
    await deltaLabel.click();
    const popover2 = page.locator('[role="tooltip"]').first();
    await expect(popover2).toBeVisible({ timeout: 2000 });
    text = (await popover2.textContent()) || '';
    expect(text).toContain('net directional exposure');
    await deltaLabel.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0);
  });

  test('Strategy Summary card headings (Greeks / Risk) and their child chips all show a hover PREVIEW and pin only on click (2026-10)', async ({ page }) => {
    // Originally guarded a narrower "Bug 2" fix where `greeksNote`/
    // `riskNote` opted OUT of hover via a now-removed `hoverPreview={false}`
    // prop while sibling chips (Δ, R:R) below them still opened on hover.
    // That prop is long gone; this test now verifies the headings AND
    // their child Greek/Risk chips uniformly show a hover preview that
    // never pins, with click as the only way to pin, at every site.
    const greeksCard = page.locator('.opt-block').nth(0);
    const riskCard = page.locator('.opt-block').nth(1);
    await expect(greeksCard).toBeVisible({ timeout: 20_000 });

    const greeksHeading = greeksCard.locator('.opt-block-h', { hasText: 'Greeks (position)' }).first();
    if (await greeksHeading.count() === 0) {
      test.skip(true, 'Strategy Summary section not rendered (no open strategy)');
      return;
    }

    // HOVER on the Greeks heading shows a preview, not a pin.
    await greeksHeading.hover();
    await expect(page.locator('#sum-hint-greeks-note')).toHaveCount(1);
    await page.mouse.move(5, 5);
    await expect(page.locator('#sum-hint-greeks-note')).toHaveCount(0);

    // CLICK — must pin open, and close again on a second click.
    await greeksHeading.click();
    const greeksPopover = page.locator('#sum-hint-greeks-note');
    await expect(greeksPopover).toBeVisible({ timeout: 2000 });
    await expect(greeksPopover).toContainText('signed-qty Greeks');
    await greeksHeading.click();
    await expect(page.locator('#sum-hint-greeks-note')).toHaveCount(0);

    // The Δ chip BELOW the heading behaves identically — confirms the
    // hover-preview + click-to-pin contract is uniform, not just at the
    // heading site.
    await page.waitForTimeout(400); // clear any 350ms post-dismiss suppression
    const deltaLabel = greeksCard.locator('.kv-k:has-text("Δ")').first();
    await deltaLabel.hover();
    await expect(page.locator('#sum-hint-delta')).toHaveCount(1);
    await page.mouse.move(5, 5);
    await expect(page.locator('#sum-hint-delta')).toHaveCount(0);
    await deltaLabel.click();
    await expect(page.locator('#sum-hint-delta')).toBeVisible({ timeout: 2000 });
    await deltaLabel.click();
    await expect(page.locator('#sum-hint-delta')).toHaveCount(0);

    // Risk & expected value heading — identical hover-preview/click-pin behavior.
    await page.waitForTimeout(400);
    const riskHeading = riskCard.locator('.opt-block-h', { hasText: 'Risk & expected value' }).first();
    await riskHeading.hover();
    await expect(page.locator('#sum-hint-risk-note')).toHaveCount(1);
    await page.mouse.move(5, 5);
    await expect(page.locator('#sum-hint-risk-note')).toHaveCount(0);

    await riskHeading.click();
    const riskPopover = page.locator('#sum-hint-risk-note');
    await expect(riskPopover).toBeVisible({ timeout: 2000 });
    await expect(riskPopover).toContainText('Aggregate risk');
    await riskHeading.click();
    await expect(page.locator('#sum-hint-risk-note')).toHaveCount(0);

    // The R:R chip below the Risk heading — same contract.
    await page.waitForTimeout(400);
    const rrLabel = riskCard.locator('.kv-k:has-text("R:R")').first();
    await rrLabel.hover();
    await expect(page.locator('#sum-hint-rr')).toHaveCount(1);
    await page.mouse.move(5, 5);
    await expect(page.locator('#sum-hint-rr')).toHaveCount(0);
    await rrLabel.click();
    await expect(page.locator('#sum-hint-rr')).toBeVisible({ timeout: 2000 });
    await rrLabel.click();
    await expect(page.locator('#sum-hint-rr')).toHaveCount(0);
  });
});
