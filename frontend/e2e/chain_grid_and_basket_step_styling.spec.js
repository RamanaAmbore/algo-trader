/**
 * chain_grid_and_basket_step_styling.spec.js
 *
 * Guards two 2026-09-29 cosmetic fixes, source-grep only (no live render
 * needed for pure CSS-property regression guards):
 *
 * 1. Basket pill lot +/- steppers (`SymbolPanel.svelte` `.oes-basket-pill-step`,
 *    shared by both the Ticket-tab basket mode and the Chain tab — Chain is
 *    always basket mode) were borderless/transparent at rest and didn't read
 *    as buttons. Now carry the same amber-chip background/border treatment
 *    as `QtyInput.svelte`'s `.ot-lots-step` (the ticket form's own lot
 *    stepper) — reused token choice, not a new palette.
 * 2. `OptionChainTab.svelte`'s strike-grid table had row dividers but no
 *    column dividers between CE | Strike | PE. Added subtle (6% white)
 *    borders on the Strike column, matching the row dividers' whisper-quiet
 *    weight, so the grid reads as a true grid without extra visual noise.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';

const SYMBOL_PANEL_PATH = path.resolve(
  import.meta.dirname ?? new URL('.', import.meta.url).pathname,
  '../src/lib/SymbolPanel.svelte',
);
const CHAIN_TAB_PATH = path.resolve(
  import.meta.dirname ?? new URL('.', import.meta.url).pathname,
  '../src/lib/order/OptionChainTab.svelte',
);

test.describe('Stale-code: basket pill stepper has real button chrome', () => {
  test('.oes-basket-pill-step has a non-transparent background and a border', () => {
    const src = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    const rule = src.match(/\.oes-basket-pill-step\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).not.toContain('background: transparent');
    expect(rule).toMatch(/background:\s*rgba\(251,\s*191,\s*36,/);
    expect(rule).toMatch(/border:\s*1px solid rgba\(251,\s*191,\s*36,/);
    expect(rule).toContain('border-radius');
  });
});

test.describe('Stale-code: chain grid has subtle column dividers', () => {
  test('Strike column (header + body) carries a subtle left/right border', () => {
    const src = readFileSync(CHAIN_TAB_PATH, 'utf8');

    // .chain-th-strike's left/right border was folded into box-shadow:
    // inset (2026-09-30, commit 905c0c08 — sticky <th> + border-collapse
    // repaint-bug fix) alongside its bottom edge; still a border,
    // functionally, just expressed as box-shadow so it survives the
    // same repaint that used to drop border-bottom.
    const thRule = src.match(/\.chain-th-strike\s*\{[^}]*\}/)?.[0] ?? '';
    expect(thRule).toMatch(/box-shadow:[^;]*inset 1px 0 0 rgba\(255,\s*255,\s*255,\s*0\.03\)/);
    expect(thRule).toMatch(/box-shadow:[^;]*inset -1px 0 0 rgba\(255,\s*255,\s*255,\s*0\.03\)/);

    const tdRule = src.match(/\.chain-row\s*>\s*td\.chain-td-strike\s*\{[^}]*\}/)?.[0] ?? '';
    expect(tdRule).toMatch(/border-left:\s*1px solid rgba\(255,\s*255,\s*255,\s*0\.03\)/);
    expect(tdRule).toMatch(/border-right:\s*1px solid rgba\(255,\s*255,\s*255,\s*0\.03\)/);
  });

  test('Row dividers remain subtle (halved again 2026-09-29, "very very subtle" pass)', () => {
    const src = readFileSync(CHAIN_TAB_PATH, 'utf8');
    const rowRule = src.match(/\.chain-row\s*>\s*td\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rowRule).toMatch(/border-bottom:\s*1px solid rgba\(255,\s*255,\s*255,\s*0\.025\)/);
  });
});

test.describe('ITM wash spreads into the strike column (operator: "spread the left background color to right which is basically in the money call strike... spread the color in the money put strikes to the left")', () => {
  // Hue values updated 2026-10-24 — this describe block pre-dates the
  // 2026-10-24 CE=orange/PE=green hue swap (commit 14e64d68) and still
  // asserted the earlier green/red hues; not caught at the time since
  // this file wasn't touched by that commit. Updated here because this
  // same task's item-2 fix (ATM-shade layering) edits these exact two
  // rules to `background-color:` (was `background:` shorthand — see
  // OptionChainTab.svelte's own comment on that change).
  test('ITM-call rows tint the strike cell orange (same side, same alpha as the CE cell)', () => {
    const src = readFileSync(CHAIN_TAB_PATH, 'utf8');
    const rule = src.match(/\.chain-row-itm-call\s+\.chain-td-strike\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).toMatch(/background-color:\s*rgba\(251,\s*146,\s*60,\s*0\.05\)/);
  });

  test('ITM-put rows tint the strike cell green (same side, same alpha as the PE cell)', () => {
    const src = readFileSync(CHAIN_TAB_PATH, 'utf8');
    const rule = src.match(/\.chain-row-itm-put\s+\.chain-td-strike\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).toMatch(/background-color:\s*rgba\(74,\s*222,\s*128,\s*0\.05\)/);
  });

  test('ATM strip keeps its own distinct styling — the spread rules never target .chain-td-strike-atm', () => {
    const src = readFileSync(CHAIN_TAB_PATH, 'utf8');
    // The ATM row's own strike-cell class carries its amber "current
    // strike" treatment, not the ITM green/red wash — confirm the two
    // new spread rules are scoped to .chain-row-itm-call/-put (which
    // the ATM row's `dir` ('atm') never matches), not to the bare
    // .chain-td-strike-atm class itself.
    expect(src).toMatch(/\.chain-td-strike-atm\s*\{\s*color:\s*var\(--c-action\)/);
    const atmScopedSpread = /\.chain-row-atm\s+\.chain-td-strike\s*\{[^}]*background/;
    expect(src).not.toMatch(atmScopedSpread);
  });
});

test.describe('Stale-code: generic basket-pill stepper removes the leg at zero, not clamp-and-disable', () => {
  // Issue 2 (.claude/PLAN.md) — Chain's own strike +/- (_netAgainstBasket in
  // OptionChainTab.svelte) already removes a staged leg at qty 0. The
  // generic .oes-basket-pill-step "−" shown under the chain for ANY staged
  // leg (from either Chain or Ticket tab) used to floor at 1 and disable,
  // requiring a separate "×" to remove. Fixed in SymbolPanel.svelte: at
  // lots 1, the "−" click now calls removeBasketLeg(i) (the same function
  // the "×" button calls) instead of clamping.
  test('"−" is never disabled at lots=1 — the clamp-to-1-and-disable condition is gone', () => {
    const src = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    const stepBlock = src.match(
      /<button type="button" class="oes-basket-pill-step"\s*\n\s*title=\{[^}]*\}\s*\n\s*disabled=\{[^}]*\}\s*\n\s*onclick=\{[^]*?\}\}>−<\/button>/
    )?.[0] ?? '';
    expect(stepBlock).not.toBe('');
    // Disabled no longer depends on lots reaching the floor — only on
    // basketSubmitting. "<= 1" must not appear in the disabled attribute.
    const disabledAttr = stepBlock.match(/disabled=\{([^}]*)\}/)?.[1] ?? '';
    expect(disabledAttr).not.toMatch(/<=\s*1/);
    // The click handler removes the leg (same call the × button uses)
    // once decrementing would reach zero, instead of Math.max(1, ...).
    expect(stepBlock).toMatch(/\(leg\.lots \|\| 1\) - 1 <= 0/);
    expect(stepBlock).toMatch(/removeBasketLeg\(i\)/);
    expect(stepBlock).not.toMatch(/Math\.max\(1,/);
  });
});

test.describe('Live: basket-pill "−" stepper removes a lots=1 leg instead of disabling', () => {
  // Behavioural confirmation of the fix above, mirroring the auth +
  // chain-add flow already established in basket_pills.spec.js.
  const _AUTH_USER = process.env.PLAYWRIGHT_USER || 'rambo';
  const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';
  let _cachedToken = process.env.PLAYWRIGHT_AUTH_TOKEN || null;

  async function authOnce(page) {
    if (!_cachedToken) {
      let tok = null;
      for (const delay of [0, 20_000, 65_000]) {
        if (delay) await new Promise((r) => setTimeout(r, delay));
        const resp = await page.request.post('/api/auth/login', {
          data: { username: _AUTH_USER, password: _AUTH_PASS },
        });
        if (resp.ok()) { tok = (await resp.json()).access_token; break; }
        if (resp.status() !== 429) throw new Error(`authOnce: /api/auth/login returned ${resp.status()}`);
      }
      if (!tok) { test.skip(true, 'rate-limited'); return; }
      _cachedToken = tok;
    }
    await page.context().addInitScript((token) => {
      sessionStorage.setItem('ramboq_token', token);
      sessionStorage.setItem('ramboq_user', JSON.stringify({
        user_id: 'rambo', username: 'rambo', role: 'admin', display_name: 'rambo',
      }));
    }, _cachedToken);
    await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${_cachedToken}` });
  }

  async function pickFirstAccount(page) {
    const acctTrigger = page.locator('button#opt-acct').first();
    await expect(acctTrigger).toBeVisible({ timeout: 10_000 });
    await acctTrigger.click();
    const firstAcct = page.locator('.rbq-multi-panel .rbq-multi-option').first();
    await expect(firstAcct).toBeVisible({ timeout: 5000 });
    await firstAcct.click();
    await acctTrigger.click();
  }

  test('stepping a freshly-staged (lots=1) pill down via "−" removes it from the basket', async ({ page }) => {
    await authOnce(page);
    await page.goto('/admin/derivatives');
    await page.waitForLoadState('domcontentloaded');
    await expect(page.locator('button#opt-acct')).toBeVisible({ timeout: 15_000 });
    await expect(page.locator('button#opt-acct')).not.toContainText(
      /No accounts loaded/i, { timeout: 15_000 });
    await pickFirstAccount(page);

    const chainBtn = page.getByRole('button', { name: /Toggle chain picker/i });
    await expect(chainBtn).toBeEnabled({ timeout: 10_000 });
    await chainBtn.click();
    await page.waitForTimeout(1500); // let chain quotes settle

    // Add one leg via the first strike "+" button — stages at lots=1.
    const addButton = page.locator('.oes-modal button')
      .filter({ hasText: /^\+$/ })
      .first();
    await expect(addButton).toBeVisible({ timeout: 10_000 });
    await addButton.click();

    const shellPill = page.locator('.oes-basket-pill').first();
    await expect(shellPill).toBeVisible({ timeout: 5000 });
    await expect(shellPill.locator('.oes-basket-pill-lots')).toHaveText('1');

    // The "−" stepper must be enabled (not clamp-disabled) at lots=1, and
    // a single click must remove the leg entirely — not just stay at 1.
    const stepDown = shellPill.locator('.oes-basket-pill-step').filter({ hasText: '−' }).first();
    await expect(stepDown).toBeEnabled();
    await stepDown.click();

    await expect(page.locator('.oes-basket-pill')).toHaveCount(0, { timeout: 5000 });
  });
});
