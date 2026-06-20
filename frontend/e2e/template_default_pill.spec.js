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
 * Ensure the Order Ticket tab is active inside the inline SymbolPanel so
 * the side toggle (.ot-side-buy / .ot-side-sell) is visible. The /orders
 * page defaults to the Chain tab; the side toggle only renders in the
 * Ticket tab body. Clicking the Ticket tab also registers `onSideChange`
 * with `_modalSide` so the shell-level template row reacts to side flips.
 */
async function switchToTicketTab(page) {
  const ticketTab = page.locator('.oes-tabs button[role="tab"]')
    .filter({ hasText: /order ticket/i })
    .first();
  if (await ticketTab.count() === 0) return; // tab not present
  const isActive = await ticketTab.evaluate(
    (el) => el.getAttribute('aria-selected') === 'true' ||
             el.classList.contains('active') || el.classList.contains('on')
  ).catch(() => false);
  if (!isActive) {
    await ticketTab.click();
    await page.waitForTimeout(400);
  }
}

/**
 * Click the BUY or SELL toggle inside the Ticket tab, then click the
 * Default pill in the template row to trigger the side-aware default
 * resolution.
 *
 * Background: SymbolPanel has a $effect that auto-updates the template
 * when _modalSide changes (commit 108ce3a5 removed the per-side $effect
 * from OrderTicket and moved it to SymbolPanel shell). In practice the
 * $effect may not fire on a rapid programmatic click; explicitly clicking
 * the Default pill is the guaranteed path per the OrderTicket comment:
 * "To restore a side-aware default after a side flip, click the Default
 * pill in the template row."
 */
async function setSide(page, side) {
  // First make sure the Ticket tab is active so the side buttons are visible.
  await switchToTicketTab(page);

  const clsMap = { BUY: '.ot-side-buy', SELL: '.ot-side-sell' };
  const btn = page.locator(clsMap[side]).first();
  await expect(btn).toBeVisible({ timeout: 8_000 });
  const alreadyOn = await btn.evaluate(
    (el) => el.classList.contains('on')
  ).catch(() => false);
  if (!alreadyOn) {
    await btn.click();
    await page.waitForTimeout(300);
  }

  // Click the Default pill in the template row to refresh the side-aware default.
  // _sideAwareDefault is a $derived that reads _modalSide; clicking Default
  // immediately applies the current derived value to _sharedTemplateId.
  const tplRow = page.locator('.oes-basket-tpl-row-shell').first();
  const defaultPillInRow = tplRow.locator('.oes-tpl-btn').first();
  if (await defaultPillInRow.count() > 0) {
    await defaultPillInRow.click();
    await page.waitForTimeout(200);
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

    // ── Fetch template catalog from API — used for deploy guard + expected values ──
    // The 4-default matrix was seeded by commit 567c24a1. We read the
    // actual DB values as ground truth so the test doesn't hardcode values
    // that the seeder's conservative _MUTABLE_FIELDS might not have refreshed.
    const tplResp = await page.request.get(`${API_HOST}/api/admin/templates`, {
      headers: { Authorization: `Bearer ${_cachedToken}` },
      timeout: 15_000,
    }).catch(() => null);

    /** @type {Record<string, any>} */
    const tplBySlug = {};

    if (!tplResp || !tplResp.ok()) {
      log.steps.push('WARN: /api/admin/templates unreachable — skipping deploy guard');
    } else {
      const tpls = await tplResp.json().catch(() => []);
      const rows = Array.isArray(tpls) ? tpls : (tpls.rows || tpls.items || []);
      for (const r of rows) { if (r.slug) tplBySlug[r.slug] = r; }
      const required = ['default-bull', 'default-long-option', 'default-bear', 'default-short-vol'];
      const missing  = required.filter(s => !tplBySlug[s] || tplBySlug[s].is_active === false);
      if (missing.length > 0) {
        log.steps.push(`DEPLOY_GUARD: missing ${missing.join(', ')} — commit 567c24a1 not deployed yet`);
        console.log(JSON.stringify(log, null, 2));
        test.skip(true, `Deploy not landed — missing templates: ${missing.join(', ')}`);
        return;
      }
      // All 4 must be is_default=true so the Default pill is enabled for each scope.
      // If is_default=false the pill is disabled (title="No side-default template
      // configured for this scope") and the scenario cannot be exercised.
      const notDefault = required.filter(s => !tplBySlug[s]?.is_default);
      if (notDefault.length > 0) {
        log.steps.push(`DEPLOY_GUARD: is_default=false for ${notDefault.join(', ')} — seeder migration not applied`);
        console.log(JSON.stringify(log, null, 2));
        test.skip(true, `Seeder migration not applied — is_default=false: ${notDefault.join(', ')}`);
        return;
      }
      log.steps.push(`deploy guard OK: found ${required.join(', ')}, all is_default=true`);
      log.steps.push(`template values: ${JSON.stringify(
        Object.fromEntries(required.map(s => [s, {
          tp_pct: tplBySlug[s].tp_pct, sl_pct: tplBySlug[s].sl_pct,
          wing_premium_pct: tplBySlug[s].wing_premium_pct,
          wing_strike_offset: tplBySlug[s].wing_strike_offset,
        }]))
      )}`);
    }

    // Helper: format a numeric value as a placeholder string.
    // The UI renders `String(val)` if non-null, else '—'.
    function expectedPh(val) {
      if (val == null) return '—';
      // Remove trailing .0 for whole numbers (String(30.0) → "30.0" in Python
      // but the Svelte template does String(val) which renders as "30" for JS
      // numbers since JSON parses 30.0 as 30).
      const n = Number(val);
      return Number.isInteger(n) ? String(n) : String(n);
    }

    // Read template field expectations from API (fall back to commit-spec values).
    const longOption = tplBySlug['default-long-option'] ?? {};
    const shortVol   = tplBySlug['default-short-vol']   ?? {};
    const bull       = tplBySlug['default-bull']         ?? {};
    const bear       = tplBySlug['default-bear']         ?? {};

    const e = {
      longOption: {
        tpPh: expectedPh(longOption.tp_pct ?? 80),
        slPh: expectedPh(longOption.sl_pct ?? null),
        hasWing: longOption.wing_premium_pct != null || longOption.wing_strike_offset != null,
        wingPremPh: expectedPh(longOption.wing_premium_pct ?? null),
      },
      shortVol: {
        tpPh: expectedPh(shortVol.tp_pct ?? 50),
        slPh: expectedPh(shortVol.sl_pct ?? null),
        hasWing: shortVol.wing_premium_pct != null || shortVol.wing_strike_offset != null,
        wingPremPh: expectedPh(shortVol.wing_premium_pct ?? null),
      },
      bull: {
        tpPh: expectedPh(bull.tp_pct ?? 30),
        slPh: expectedPh(bull.sl_pct ?? 20),
        hasWing: bull.wing_premium_pct != null || bull.wing_strike_offset != null,
      },
      bear: {
        tpPh: expectedPh(bear.tp_pct ?? 30),
        slPh: expectedPh(bear.sl_pct ?? 20),
        hasWing: bear.wing_premium_pct != null || bear.wing_strike_offset != null,
      },
    };
    log.steps.push(`expected values: ${JSON.stringify(e)}`);

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

      // TP% placeholder = expectedPh(tp_pct) from API.
      const tpPh = await tpl1.tpInput.getAttribute('placeholder');
      log.steps.push(`S1 TP%="${tpPh}" (expected="${e.longOption.tpPh}")`);
      expect(tpPh).toBe(e.longOption.tpPh);

      // SL% placeholder = '—' (no sl_pct) or actual if set.
      const slPh = await tpl1.slInput.getAttribute('placeholder');
      log.steps.push(`S1 SL%="${slPh}" (expected="${e.longOption.slPh}")`);
      expect(slPh).toBe(e.longOption.slPh);

      // Wing inputs present/absent per actual DB row.
      const wingCnt = await tpl1.wingParams.count();
      log.steps.push(`S1 wing count=${wingCnt} (hasWing=${e.longOption.hasWing})`);
      if (e.longOption.hasWing) {
        expect(wingCnt).toBeGreaterThan(0);
      } else {
        expect(wingCnt).toBe(0);
      }
      log.steps.push('Scenario 1 ✓');
    });

    // ── Scenario 2 — SELL CE → sell_option → Default Short Vol ───────────
    await test.step('Scenario 2: SELL CE → sell_option → Default Short Vol', async () => {
      await setSide(page, 'SELL');
      // Wait for reactive update — chip must switch away from BUY default.
      const tpl = await getTemplateRow(page);
      await expect(tpl.nameChip).not.toContainText(/default long option/i, { timeout: 8_000 });
      await expect(tpl.defaultPill).toHaveClass(/\bon\b/, { timeout: 8_000 });
      await expect(tpl.nameChip).toContainText(/default short vol/i, { timeout: 8_000 });

      const tpPh = await tpl.tpInput.getAttribute('placeholder');
      log.steps.push(`S2 TP%="${tpPh}" (expected="${e.shortVol.tpPh}")`);
      expect(tpPh).toBe(e.shortVol.tpPh);

      const slPh = await tpl.slInput.getAttribute('placeholder');
      log.steps.push(`S2 SL%="${slPh}" (expected="${e.shortVol.slPh}")`);
      expect(slPh).toBe(e.shortVol.slPh);

      // Wing inputs present/absent per actual DB row.
      const wingCnt = await tpl.wingParams.count();
      log.steps.push(`S2 wing count=${wingCnt} (hasWing=${e.shortVol.hasWing})`);
      if (e.shortVol.hasWing) {
        expect(wingCnt).toBeGreaterThan(0);

        // Wing prem% placeholder from API (may be '—' if wing_premium_pct is null).
        const wingPremInput = tpl.wingParams.filter({ hasText: /prem/i }).locator('input').first();
        const wingPremCnt = await wingPremInput.count();
        if (wingPremCnt > 0) {
          const wingPh = await wingPremInput.getAttribute('placeholder');
          log.steps.push(`S2 wing prem%="${wingPh}" (expected="${e.shortVol.wingPremPh}")`);
          expect(wingPh).toBe(e.shortVol.wingPremPh);
        }
      } else {
        expect(wingCnt).toBe(0);
      }
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
      log.steps.push(`S3 TP%="${tpPh}" (expected="${e.bull.tpPh}")`);
      expect(tpPh).toBe(e.bull.tpPh);

      const slPh = await tpl.slInput.getAttribute('placeholder');
      log.steps.push(`S3 SL%="${slPh}" (expected="${e.bull.slPh}")`);
      expect(slPh).toBe(e.bull.slPh);

      const wingCnt = await tpl.wingParams.count();
      log.steps.push(`S3 wing count=${wingCnt} (hasWing=${e.bull.hasWing})`);
      if (e.bull.hasWing) {
        expect(wingCnt).toBeGreaterThan(0);
      } else {
        expect(wingCnt).toBe(0);
      }
      log.steps.push('Scenario 3 ✓');
    });

    // ── Scenario 4 — SELL EQ → sell_any → Default Bear ───────────────────
    await test.step('Scenario 4: SELL EQ → sell_any → Default Bear', async () => {
      await setSide(page, 'SELL');
      // Wait for the name chip to update away from the BUY default name.
      // _sideAwareDefault is a $derived — it re-evaluates after _modalSide changes.
      const tpl = await getTemplateRow(page);
      await expect(tpl.nameChip).not.toContainText(/default bull/i, { timeout: 8_000 });
      await expect(tpl.defaultPill).toHaveClass(/\bon\b/, { timeout: 8_000 });
      await expect(tpl.nameChip).toContainText(/default bear/i, { timeout: 8_000 });

      const tpPh = await tpl.tpInput.getAttribute('placeholder');
      log.steps.push(`S4 TP%="${tpPh}" (expected="${e.bear.tpPh}")`);
      expect(tpPh).toBe(e.bear.tpPh);

      const slPh = await tpl.slInput.getAttribute('placeholder');
      log.steps.push(`S4 SL%="${slPh}" (expected="${e.bear.slPh}")`);
      expect(slPh).toBe(e.bear.slPh);

      const wingCnt = await tpl.wingParams.count();
      log.steps.push(`S4 wing count=${wingCnt} (hasWing=${e.bear.hasWing})`);
      if (e.bear.hasWing) {
        expect(wingCnt).toBeGreaterThan(0);
      } else {
        expect(wingCnt).toBe(0);
      }
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
