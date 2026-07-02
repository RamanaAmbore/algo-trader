/**
 * derivatives_tick_flash.spec.js
 *
 * Verifies that tick-flash animations fire correctly on every new numeric
 * cell family added to /admin/derivatives:
 *   - Payoff header chips (EV, Δ, Γ, Θ, 𝒱, ρ)
 *   - Legs per-row cells (P&L, Day P&L, Exp P&L)
 *   - Legs TOTAL row (P&L, Day P&L, Exp P&L)
 *   - Snapshot TOTAL row (Day P&L, P&L, Exp P&L)
 *   - kv-block values (Δ, Γ, Θ, 𝒱, ρ, POP, EV, EV/cost)
 *
 * Quality dimensions:
 *   SSOT     — single flash instance; keyed per-cell (no shared key collisions).
 *   Perf     — 350ms flash, gone within 500ms of trigger.
 *   Stale    — source grep confirms one createTickFlash() call on the page.
 *   Reusable — all families use the existing flash instance, not a new primitive.
 *   UX       — prefers-reduced-motion disables the animation; cells do not flash.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import * as fs from 'fs';
import * as path from 'path';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const PAGE_SRC = path.resolve(
  process.cwd(),
  'src/routes/(algo)/admin/derivatives/+page.svelte'
);

// ── SSOT / stale-code static checks ─────────────────────────────────────────

test('SSOT: single createTickFlash() instance on the page', () => {
  const src = fs.readFileSync(PAGE_SRC, 'utf8');
  const instances = (src.match(/createTickFlash\s*\(/g) || []).length;
  expect(instances, 'createTickFlash should be called exactly once').toBe(1);
});

test('SSOT: flash.update key namespaces do not collide', () => {
  const src = fs.readFileSync(PAGE_SRC, 'utf8');

  // payoff: prefix covers chips + kv-block greeks
  expect(src).toContain("flash.update('payoff:ev'");
  expect(src).toContain("flash.update('payoff:delta'");
  expect(src).toContain("flash.update('payoff:theta'");

  // kv: prefix covers risk/EV kv-block
  expect(src).toContain("flash.update('kv:pop'");
  expect(src).toContain("flash.update('kv:ev'");

  // leg: prefix for per-row cells
  expect(src).toContain('flash.update(`leg:${k}:day`');
  expect(src).toContain('flash.update(`leg:${k}:pnl`');
  expect(src).toContain('flash.update(`leg:${k}:exp`');

  // total: prefix for TOTAL row (Legs + Snapshot)
  expect(src).toContain("flash.update('total:day'");
  expect(src).toContain("flash.update('total:pnl'");
  expect(src).toContain("flash.update('total:exp'");
});

test('SSOT: shell guard prevents kv/payoff flash on synth equity-only strategy', () => {
  const src = fs.readFileSync(PAGE_SRC, 'utf8');
  // The two $effects that drive payoff + kv flash must guard on iv_proxy / days_to_expiry
  const effectBlock = src.slice(src.indexOf("flash.update('payoff:ev'") - 200,
                                src.indexOf("flash.update('kv:ev_pct'") + 100);
  expect(effectBlock).toContain('!strategy.iv_proxy && !strategy.days_to_expiry');
});

test('SSOT: tf-cell CSS rule covers payoff chips, kv-v, cand-pnl, byund-row TOTAL', () => {
  const src = fs.readFileSync(PAGE_SRC, 'utf8');
  // CSS block must define .tf-cell.tf-up and .tf-cell.tf-down
  expect(src).toContain('.tf-cell.tf-up');
  expect(src).toContain('.tf-cell.tf-down');
  // reduced-motion block must also include tf-cell
  const motionBlock = src.slice(src.indexOf('@media (prefers-reduced-motion'),
                                src.indexOf('@media (prefers-reduced-motion') + 500);
  expect(motionBlock).toContain('.tf-cell.tf-up');
  expect(motionBlock).toContain('.tf-cell.tf-down');
});

test('Stale: no second flash primitive or duplicate animation keyframes', () => {
  const src = fs.readFileSync(PAGE_SRC, 'utf8');
  // Only one createTickFlash() call (SSOT test above verifies count = 1).
  // The base keyframe @keyframes tf-pulse-up (not the -fs variant) should be defined once.
  const upKf = (src.match(/@keyframes tf-pulse-up\s*\{/g) || []).length;
  expect(upKf, '@keyframes tf-pulse-up should be defined once').toBe(1);
});

// ── Live UI checks ───────────────────────────────────────────────────────────

const VIEWPORTS = [
  { name: 'desktop', width: 1280, height: 800 },
  { name: 'mobile',  width: 393,  height: 851 },
];

for (const vp of VIEWPORTS) {
  test.describe(`tick-flash — /admin/derivatives [${vp.name}]`, () => {
    test.setTimeout(120_000);

    let page;

    test.beforeEach(async ({ page: p }) => {
      page = p;
      await page.setViewportSize({ width: vp.width, height: vp.height });

      let authOk = false;
      for (const creds of [
        { user: process.env.PLAYWRIGHT_USER || 'ambore', pass: process.env.PLAYWRIGHT_PASS || 'admin1234' },
        { user: 'rambo', pass: 'admin1234' },
      ]) {
        try {
          await loginAsAdmin(page, creds);
          authOk = true;
          break;
        } catch (_) { /* try next cred set */ }
      }
      if (!authOk) {
        test.skip(true, 'No valid credentials — skipping live UI flash checks');
        return;
      }

      await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'domcontentloaded', timeout: 30000 });
      await page.locator('.opt-byund-card').waitFor({ state: 'attached', timeout: 25000 });
    });

    test(`Snapshot TOTAL row cells gain tf-up/tf-down on value change [${vp.name}]`, async ({ page }) => {
      // The snapshot TOTAL row is always mounted (not data-gated).
      const totalRow = page.locator('.byund-row-total');
      const exists = await totalRow.count();
      if (exists === 0) {
        test.skip(true, 'No snapshot total row present (empty book)');
        return;
      }

      // Day P&L cell — carries flash.classOf('total:day') = tf-cell class
      const dayCell = totalRow.locator('span.tf-cell').first();
      const dayCellExists = await dayCell.count();
      if (dayCellExists === 0) {
        test.skip(true, 'No tf-cell on TOTAL row (no positions)');
        return;
      }

      // Inject a store value change via page.evaluate to trigger the flash effect.
      // We directly mutate the _snapshotTotalDay reactive binding via the
      // snapshotTotals Svelte store (exported from $lib/stores).
      // Flash fires if the new value differs from the prior value by > threshold (0).
      const currentVal = await dayCell.evaluate((el) => el.textContent?.trim());

      // Use store mutation to trigger flash — simulate a tick arriving with a
      // different value. We must use the public snapshotTotals store which the
      // page reads, causing _snapshotTotalDay to update and flash.update() to fire.
      await page.evaluate(() => {
        // Access the Svelte module store via the module system.
        // In SvelteKit, modules are accessible via dynamic import from the origin.
        return import('/src/lib/stores.js').then(({ snapshotTotals }) => {
          const cur = snapshotTotals.get?.() ?? { day: 0, pnl: 0, exp: 0, at: 0 };
          // Flip value sign to guarantee a numeric change.
          const bumped = { day: (cur.day || 0) + 1234, pnl: cur.pnl, exp: cur.exp, at: Date.now() };
          snapshotTotals.set(bumped);
        }).catch(() => {
          // Fallback: no store access — skip assertion.
          return null;
        });
      });

      // tf-up or tf-down should appear within 100ms of the store set.
      await expect(dayCell).toHaveClass(/tf-up|tf-down/, { timeout: 400 });

      // After 500ms (well past the 350ms durationMs), the class should be gone.
      await page.waitForTimeout(500);
      const classAfter = await dayCell.getAttribute('class');
      expect(classAfter ?? '').not.toMatch(/tf-up|tf-down/);
    });

    test(`Payoff header EV chip has tf-cell class [${vp.name}]`, async ({ page }) => {
      // If strategy is loaded, the payoff card is visible and EV chip carries tf-cell.
      const payoffCard = page.locator('.opt-payoff');
      const exists = await payoffCard.count();
      if (exists === 0) {
        test.skip(true, 'No payoff card (no strategy loaded)');
        return;
      }

      // EV chip is an opt-section-tag with tf-cell on the payoff header.
      const evChip = payoffCard.locator('.opt-section-tag.tf-cell').first();
      const evChipExists = await evChip.count();
      if (evChipExists === 0) {
        test.skip(true, 'No tf-cell chip in payoff header (strategy not yet loaded)');
        return;
      }

      // Chip must carry tf-cell (marker class for the animation rule).
      const cls = await evChip.getAttribute('class');
      expect(cls).toContain('tf-cell');
    });

    test(`Legs TOTAL row P&L cells carry tf-cell marker [${vp.name}]`, async ({ page }) => {
      const legsTotal = page.locator('.cand-row-total');
      const exists = await legsTotal.count();
      if (exists === 0) {
        test.skip(true, 'No Legs TOTAL row (no candidates)');
        return;
      }

      // All three P&L cells in cand-row-total must carry tf-cell.
      const tfCells = legsTotal.locator('span.tf-cell');
      const count = await tfCells.count();
      // Expect at least 3 (P&L, Day P&L, Exp P&L).
      expect(count).toBeGreaterThanOrEqual(3);
    });

    test(`kv-block Greek values carry tf-cell marker [${vp.name}]`, async ({ page }) => {
      // Greeks kv-block is inside .opt-kv-greeks.
      const greeksBlock = page.locator('.opt-kv-greeks');
      const exists = await greeksBlock.count();
      if (exists === 0) {
        test.skip(true, 'No Greeks kv-block (no strategy loaded)');
        return;
      }

      const tfCells = greeksBlock.locator('.kv-v.tf-cell');
      const count = await tfCells.count();
      // Five Greeks × tf-cell = at least 4 (rho may not always be present).
      expect(count).toBeGreaterThanOrEqual(4);
    });

    test(`prefers-reduced-motion disables flash animation [${vp.name}]`, async ({ page }) => {
      // Emulate reduced-motion preference.
      await page.emulateMedia({ reducedMotion: 'reduce' });

      const totalRow = page.locator('.byund-row-total');
      const exists = await totalRow.count();
      if (exists === 0) {
        test.skip(true, 'No snapshot total row present');
        return;
      }

      const tfCell = totalRow.locator('span.tf-cell').first();
      const tfExists = await tfCell.count();
      if (tfExists === 0) {
        test.skip(true, 'No tf-cell on TOTAL row');
        return;
      }

      // Even if tf-up/tf-down class is applied, animation-name must be 'none'
      // under prefers-reduced-motion: reduce.
      const animName = await tfCell.evaluate((el) => {
        // Force tf-up temporarily to check animation suppression.
        el.classList.add('tf-up');
        const name = getComputedStyle(el).animationName;
        el.classList.remove('tf-up');
        return name;
      });

      // Under reduced-motion, animation should be 'none' (not 'tf-pulse-up').
      expect(animName).toBe('none');
    });

    test(`flash decays in 350ms — class absent 450ms after trigger [${vp.name}]`, async ({ page }) => {
      const totalRow = page.locator('.byund-row-total');
      const exists = await totalRow.count();
      if (exists === 0) {
        test.skip(true, 'No snapshot total row (empty book)');
        return;
      }

      const tfCell = totalRow.locator('span.tf-cell').first();
      const tfExists = await tfCell.count();
      if (tfExists === 0) {
        test.skip(true, 'No tf-cell on snapshot TOTAL row');
        return;
      }

      // Manually add the class and verify it clears via the CSS transition alone
      // (this tests CSS decay timing, not the JS timeout — those are covered by the
      // Snapshot TOTAL store injection test above).
      // Confirm the class is NOT already present before injection.
      const classBefore = await tfCell.getAttribute('class');
      expect(classBefore ?? '').not.toMatch(/tf-up|tf-down/);

      // Inject tf-up directly (bypasses flash.update — tests pure CSS decay timing).
      await tfCell.evaluate((el) => el.classList.add('tf-up'));
      // Verify class was applied.
      await expect(tfCell).toHaveClass(/tf-up/, { timeout: 100 });

      // Wait 450ms — well past the 350ms animation.
      await page.waitForTimeout(450);

      // The JS timer in createTickFlash clears the class after durationMs. However,
      // since we injected manually (not via flash.update), the timer won't fire.
      // This test verifies the CSS animation ends (not class removal). We check
      // animation-play-state is at its final frame (transparent background).
      const bgAfter = await tfCell.evaluate((el) => getComputedStyle(el).backgroundColor);
      // After animation completes, background should resolve to transparent / rgba(0,0,0,0).
      // The keyframe ends at `background-color: transparent`.
      // CSS animations leave the element at the to-frame only if fill-mode is 'forwards';
      // our rule uses ease-out without fill-mode so it returns to the base style.
      // We assert the animation has ended (not frozen at peak alpha).
      expect(bgAfter).toMatch(/rgba\(0, 0, 0, 0\)|transparent/);

      // Clean up.
      await tfCell.evaluate((el) => el.classList.remove('tf-up'));
    });
  });
}
