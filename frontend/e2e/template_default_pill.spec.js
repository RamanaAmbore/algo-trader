/**
 * template_default_pill.spec.js
 *
 * Verifies the side-aware Default pill in the SymbolPanel template row.
 * Tests the 4-scope matrix seeded in commit 567c24a1:
 *   buy_option  → Default Long Option  (TP 80 MARKET, no SL, no wing)
 *   sell_option → Default Short Vol    (TP 50, wing prem% 10, no SL)
 *   buy_any     → Default Bull         (TP 30, SL 20, no wing)
 *   sell_any    → Default Bear         (TP 30, SL 20, no wing)
 *
 * Strategy:
 *   - Pre-seed localStorage (ramboq.recent.symbol) via addInitScript so
 *     SymbolPanel auto-reads a known symbol on mount without needing to
 *     drive the typeahead search.
 *   - The template row (.oes-basket-tpl-row-shell) is always rendered in
 *     the SymbolPanel shell when action=open and templates are loaded.
 *   - Side flips via the BUY/SELL toggle buttons in the modal.
 *   - Scenarios 1+2 use a CE symbol (buy_option / sell_option scope).
 *   - Scenarios 3+4 use a plain EQ symbol (buy_any / sell_any scope).
 *   - All 5 steps run inside a single test() with a single modal mount
 *     to keep state minimal; a page reload between CE and EQ phases
 *     resets the symbol to the new pre-seeded value.
 *
 * Run:
 *   cd frontend && PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/template_default_pill.spec.js \
 *     --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';

const BASE     = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const API_HOST = BASE.includes('localhost') ? 'https://dev.ramboq.com' : BASE;
const _PASS    = process.env.PLAYWRIGHT_PASS || 'admin1234';

// ── Auth ──────────────────────────────────────────────────────────────────
let _cachedToken = null;

/**
 * Obtain a JWT and inject it into every page load via addInitScript.
 * Handles rate-limit retries (429) with 20 s + 65 s back-offs.
 */
async function loginOnce(page, token = null) {
  if (token) { _cachedToken = token; }
  if (!_cachedToken) {
    for (const user of ['ambore', 'rambo', 'admin']) {
      let tok = null;
      for (const delay of [0, 20_000, 65_000]) {
        if (delay) await new Promise((r) => setTimeout(r, delay));
        const r = await page.request.post(`${API_HOST}/api/auth/login`, {
          data: { username: user, password: _PASS },
          timeout: 15_000,
        }).catch(() => null);
        if (r && r.ok()) { tok = (await r.json()).access_token; break; }
        if (r && r.status() !== 429) break;
      }
      if (tok) { _cachedToken = tok; break; }
    }
  }
  if (!_cachedToken) throw new Error('template_default_pill: login failed for all users');
}

/**
 * Navigate to /orders with a pre-seeded recent symbol.
 * SymbolPanel reads `localStorage['ramboq.recent.symbol']` on mount
 * (via recentSymbolStore) and pre-fills `_localSymbol` if no `symbol`
 * prop is supplied — which is the case for the inline orders entry card.
 */
async function goOrdersWithSymbol(page, sym) {
  // addInitScript fires before EVERY navigation in this context.
  // Re-register on every call because we're swapping symbols between phases.
  await page.addInitScript(({ t, s }) => {
    sessionStorage.setItem('ramboq_token', t);
    sessionStorage.setItem('ramboq_user', JSON.stringify({
      user_id: 'rambo', username: 'rambo', role: 'admin', display_name: 'rambo',
    }));
    // Seed the recent symbol — SymbolPanel reads this via recentSymbolStore.
    localStorage.setItem('ramboq.recent.symbol', s);
    localStorage.setItem('ramboq.recent_symbol', s);  // legacy key guard
  }, { t: _cachedToken, s: sym });

  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${_cachedToken}` });
  await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.page-title-chip').first()).toBeVisible({ timeout: 15_000 });
}

/**
 * Wait for the SymbolPanel template row to become visible and return
 * its key child locators.
 */
async function getTemplateRow(page) {
  const row = page.locator('.oes-basket-tpl-row-shell').first();
  await expect(row).toBeVisible({ timeout: 15_000 });
  return {
    row,
    defaultPill: row.locator('.oes-tpl-btn').first(),
    nonePill:    row.locator('.oes-tpl-btn').last(),
    nameChip:    row.locator('.oes-basket-tpl-name').first(),
    params:      row.locator('.oes-basket-tpl-params').first(),
    tpInput:     row.locator('.oes-basket-tpl-param').nth(0).locator('input'),
    slInput:     row.locator('.oes-basket-tpl-param').nth(1).locator('input'),
    wingParams:  row.locator('.oes-basket-tpl-param').filter({ hasText: /wing/i }),
  };
}

/**
 * Click the BUY or SELL toggle inside the SymbolPanel.
 * The side toggle renders as buttons with text "BUY"/"SELL" (or
 * contextual labels like "Buy"/"Sell"/"Add to position"/etc.).
 * We look for the pill-group inside .oes-side-row or .oes-modal.
 */
async function setSide(page, side) {
  // Look for the side toggle pill — buttons with BUY or SELL text.
  // The outer class can vary; match by button text inside the modal area.
  const btn = page.locator('.oes-modal, .oes-modal-inline')
    .first()
    .locator('button.oes-side-btn, button.oes-side-pill, button[class*="side"]')
    .filter({ hasText: new RegExp(`^${side}$`, 'i') })
    .first();

  if (await btn.count() > 0 && await btn.isVisible()) {
    const alreadyOn = await btn.evaluate(
      (el) => el.classList.contains('on') || el.classList.contains('active')
    ).catch(() => false);
    if (!alreadyOn) await btn.click();
    await page.waitForTimeout(300);
    return;
  }

  // Fallback: any visible button whose full text is the side word.
  const allBtns = page.locator('.oes-modal, .oes-modal-inline').first().locator('button');
  const count = await allBtns.count();
  for (let i = 0; i < count; i++) {
    const el = allBtns.nth(i);
    const txt = (await el.textContent().catch(() => '')).trim().toUpperCase();
    if (txt === side) {
      const alreadyOn = await el.evaluate(
        (e) => e.classList.contains('on') || e.classList.contains('active')
      ).catch(() => false);
      if (!alreadyOn) await el.click();
      await page.waitForTimeout(300);
      return;
    }
  }
}

// ── Spec ──────────────────────────────────────────────────────────────────
test.describe('Template Default pill — side-aware 4-scope matrix', () => {
  test.setTimeout(150_000);

  test('default pill auto-swaps across buy_option / sell_option / buy_any / sell_any + None clears', async ({ page }) => {
    const log = { steps: [], errors: [] };
    page.on('pageerror', (e) => log.errors.push(String(e).slice(0, 200)));
    page.on('response', (r) => {
      if (r.status() >= 400 && !r.url().includes('/api/auth') && r.url().includes('/api/')) {
        log.errors.push(`${r.status()} ${r.url().split('/api/')[1]?.slice(0, 60)}`);
      }
    });

    await loginOnce(page);

    // ── Pre-check: commit 567c24a1 must be deployed on dev ────────────────
    // The 4-default matrix was seeded by that commit. Verify via API.
    const tplResp = await page.request.get(`${API_HOST}/api/admin/templates`, {
      headers: { Authorization: `Bearer ${_cachedToken}` },
      timeout: 15_000,
    }).catch(() => null);

    if (!tplResp || !tplResp.ok()) {
      log.steps.push('WARN: /api/admin/templates unreachable — skipping deploy guard');
    } else {
      const tpls = await tplResp.json().catch(() => []);
      const rows = Array.isArray(tpls) ? tpls : (tpls.rows || tpls.items || []);
      const slugs = rows.filter(t => t.is_active !== false).map(t => t.slug || '');
      const required = ['default-bull', 'default-long-option', 'default-bear', 'default-short-vol'];
      const missing  = required.filter(s => !slugs.includes(s));
      if (missing.length > 0) {
        log.steps.push(`DEPLOY_GUARD: missing ${missing.join(', ')} — commit 567c24a1 not deployed yet`);
        console.log(JSON.stringify(log, null, 2));
        test.skip(true, `Deploy not landed — missing templates: ${missing.join(', ')}`);
        return;
      }
      log.steps.push(`deploy guard OK: found ${required.join(', ')}`);
    }

    // ── Fetch a real CE tradingsymbol from dev instruments ────────────────
    // Use the instruments API to get a real NFO CE contract. We look for
    // a NIFTY weekly CE (format NIFTY2662N<strike>CE) that's active.
    let ceSym = 'NIFTY2662324150CE';  // sensible default from the API check above
    const instResp = await page.request.get(`${API_HOST}/api/instruments`, {
      headers: { Authorization: `Bearer ${_cachedToken}` },
      timeout: 20_000,
    }).catch(() => null);
    if (instResp && instResp.ok()) {
      const data = await instResp.json().catch(() => null);
      const items = Array.isArray(data) ? data : (data?.items || []);
      const firstCe = items.find(
        i => (i.s || '').toUpperCase().endsWith('CE') && (i.e || '') === 'NFO'
      );
      if (firstCe?.s) { ceSym = firstCe.s; }
    }
    log.steps.push(`using CE symbol: ${ceSym}`);

    // ── Phase 1: CE symbol — test buy_option + sell_option ────────────────
    await goOrdersWithSymbol(page, ceSym);
    // Wait for the template row which requires templates to be fetched.
    const tpl1 = await getTemplateRow(page);
    log.steps.push(`template row visible (CE phase)`);

    // ── Scenario 1 — BUY CE → buy_option → Default Long Option ──────────
    await test.step('Scenario 1: BUY CE → buy_option → Default Long Option', async () => {
      await setSide(page, 'BUY');
      // The _sideAwareDefault re-derives reactively.
      // Default pill must carry .on class.
      await expect(tpl1.defaultPill).toHaveClass(/\bon\b/, { timeout: 8_000 });
      // Name chip must contain "Default Long Option".
      await expect(tpl1.nameChip).toContainText(/default long option/i, { timeout: 8_000 });

      // TP% placeholder = '80'.
      const tpPh = await tpl1.tpInput.getAttribute('placeholder');
      log.steps.push(`S1 TP%="${tpPh}"`);
      expect(tpPh).toBe('80');

      // SL% placeholder = '—' (no sl_pct).
      const slPh = await tpl1.slInput.getAttribute('placeholder');
      log.steps.push(`S1 SL%="${slPh}"`);
      expect(slPh).toBe('—');

      // Wing inputs NOT present for buy_option.
      const wingCnt = await tpl1.wingParams.count();
      log.steps.push(`S1 wing count=${wingCnt}`);
      expect(wingCnt).toBe(0);
      log.steps.push('Scenario 1 ✓');
    });

    // ── Scenario 2 — SELL CE → sell_option → Default Short Vol ───────────
    await test.step('Scenario 2: SELL CE → sell_option → Default Short Vol', async () => {
      await setSide(page, 'SELL');
      await page.waitForTimeout(400);

      const tpl = await getTemplateRow(page);
      await expect(tpl.defaultPill).toHaveClass(/\bon\b/, { timeout: 8_000 });
      await expect(tpl.nameChip).toContainText(/default short vol/i, { timeout: 8_000 });

      const tpPh = await tpl.tpInput.getAttribute('placeholder');
      log.steps.push(`S2 TP%="${tpPh}"`);
      expect(tpPh).toBe('50');

      const slPh = await tpl.slInput.getAttribute('placeholder');
      log.steps.push(`S2 SL%="${slPh}"`);
      expect(slPh).toBe('—');

      // Wing prem% input must be visible for sell_option.
      const wingCnt = await tpl.wingParams.count();
      log.steps.push(`S2 wing count=${wingCnt}`);
      expect(wingCnt).toBeGreaterThan(0);

      // Wing prem% placeholder = '10'.
      const wingPremInput = tpl.wingParams.filter({ hasText: /prem/i }).locator('input').first();
      const wingPh = await wingPremInput.getAttribute('placeholder');
      log.steps.push(`S2 wing prem%="${wingPh}"`);
      expect(wingPh).toBe('10');
      log.steps.push('Scenario 2 ✓');
    });

    // ── Phase 2: EQ symbol — test buy_any + sell_any ─────────────────────
    await goOrdersWithSymbol(page, 'RELIANCE');
    const tpl2 = await getTemplateRow(page);
    log.steps.push(`template row visible (EQ phase)`);

    // ── Scenario 3 — BUY EQ → buy_any → Default Bull ─────────────────────
    await test.step('Scenario 3: BUY EQ → buy_any → Default Bull', async () => {
      await setSide(page, 'BUY');
      await page.waitForTimeout(400);

      const tpl = await getTemplateRow(page);
      await expect(tpl.defaultPill).toHaveClass(/\bon\b/, { timeout: 8_000 });
      await expect(tpl.nameChip).toContainText(/default bull/i, { timeout: 8_000 });

      const tpPh = await tpl.tpInput.getAttribute('placeholder');
      log.steps.push(`S3 TP%="${tpPh}"`);
      expect(tpPh).toBe('30');

      const slPh = await tpl.slInput.getAttribute('placeholder');
      log.steps.push(`S3 SL%="${slPh}"`);
      expect(slPh).toBe('20');

      const wingCnt = await tpl.wingParams.count();
      log.steps.push(`S3 wing count=${wingCnt}`);
      expect(wingCnt).toBe(0);
      log.steps.push('Scenario 3 ✓');
    });

    // ── Scenario 4 — SELL EQ → sell_any → Default Bear ───────────────────
    await test.step('Scenario 4: SELL EQ → sell_any → Default Bear', async () => {
      await setSide(page, 'SELL');
      await page.waitForTimeout(400);

      const tpl = await getTemplateRow(page);
      await expect(tpl.defaultPill).toHaveClass(/\bon\b/, { timeout: 8_000 });
      await expect(tpl.nameChip).toContainText(/default bear/i, { timeout: 8_000 });

      const tpPh = await tpl.tpInput.getAttribute('placeholder');
      log.steps.push(`S4 TP%="${tpPh}"`);
      expect(tpPh).toBe('30');

      const slPh = await tpl.slInput.getAttribute('placeholder');
      log.steps.push(`S4 SL%="${slPh}"`);
      expect(slPh).toBe('20');

      const wingCnt = await tpl.wingParams.count();
      log.steps.push(`S4 wing count=${wingCnt}`);
      expect(wingCnt).toBe(0);
      log.steps.push('Scenario 4 ✓');
    });

    // ── Scenario 5 — None pill clears name chip + param row ───────────────
    await test.step('Scenario 5: None pill clears active-template chip and param row', async () => {
      const tpl = await getTemplateRow(page);

      await tpl.nonePill.click();
      await page.waitForTimeout(300);

      // None pill must have .on.
      await expect(tpl.nonePill).toHaveClass(/\bon\b/, { timeout: 5_000 });

      // Name chip must disappear.
      const chipVisible = await tpl.nameChip.isVisible().catch(() => false);
      log.steps.push(`S5 name chip visible=${chipVisible}`);
      expect(chipVisible).toBe(false);

      // Params block must not be rendered.
      const paramsVisible = await tpl.params.isVisible().catch(() => false);
      log.steps.push(`S5 params visible=${paramsVisible}`);
      expect(paramsVisible).toBe(false);
      log.steps.push('Scenario 5 ✓');
    });

    console.log('\n=== template_default_pill results ===');
    console.log(JSON.stringify(log, null, 2));
    if (log.errors.length) {
      console.warn('non-fatal API errors during test:', log.errors);
    }
  });
});
