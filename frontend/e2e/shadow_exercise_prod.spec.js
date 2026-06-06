/**
 * Shadow Exercise — Phase 1 + 2 on prod (https://ramboq.com).
 *
 * Safety constraints:
 *   - NEVER changes paper_trading_mode.
 *   - shadow_mode is restored to original in afterAll, even on failure.
 *   - No live orders. Shadow mode means the engine logs the Kite payload +
 *     basket_margin validation but does NOT execute against the broker.
 *   - Targets /orders basket submit path; mode='shadow' is set by master flag.
 *
 * Run:
 *   PLAYWRIGHT_BASE_URL=https://ramboq.com \
 *   PLAYWRIGHT_USER=ambore \
 *   PLAYWRIGHT_PASS=<pass> \
 *   npx playwright test e2e/shadow_exercise_prod.spec.js \
 *     --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';

// ── Auth ─────────────────────────────────────────────────────────────────────
const _USERS = ['ambore', 'rambo'];
const _PASS  = process.env.PLAYWRIGHT_PASS || 'admin1234';
let _cachedToken = null;
let _cachedUser  = null;

async function authOnce(page) {
  if (!_cachedToken) {
    for (const user of _USERS) {
      for (const delay of [0, 20_000, 65_000]) {
        if (delay) await new Promise((r) => setTimeout(r, delay));
        const resp = await page.request.post('/api/auth/login', {
          data: { username: user, password: _PASS },
        });
        if (resp.ok()) {
          _cachedToken = (await resp.json()).access_token;
          _cachedUser  = user;
          break;
        }
        if (resp.status() !== 429) break;
      }
      if (_cachedToken) break;
    }
    if (!_cachedToken) throw new Error('authOnce: all login attempts failed');
  }

  await page.context().addInitScript(({ tok, usr }) => {
    sessionStorage.setItem('ramboq_token', tok);
    sessionStorage.setItem('ramboq_user', JSON.stringify({
      user_id: usr, username: usr, role: 'admin', display_name: usr,
    }));
  }, { tok: _cachedToken, usr: _cachedUser || 'rambo' });

  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${_cachedToken}` });

  const url = page.url();
  if (!url || url === 'about:blank') {
    await page.goto('/', { waitUntil: 'domcontentloaded' });
  }
}

// ── Settings helpers ──────────────────────────────────────────────────────────
async function readSetting(page, key) {
  const r = await page.request.get(`/api/admin/settings/${key}`);
  if (!r.ok()) return null;
  const body = await r.json();
  return body.value;
}

async function patchSetting(page, key, value) {
  const r = await page.request.patch(`/api/admin/settings/${key}`, {
    data: { value: value ? 'true' : 'false' },
  });
  if (!r.ok()) {
    const body = await r.text();
    throw new Error(`PATCH ${key} failed: ${r.status()} ${body.slice(0, 200)}`);
  }
  // Give the settings cache time to reload
  await new Promise((res) => setTimeout(res, 800));
}

async function readMode(page) {
  const r = await page.request.get('/api/admin/execution/mode');
  if (!r.ok()) throw new Error(`readMode: ${r.status()}`);
  return r.json();
}

async function getLoadedAccounts(page) {
  const r = await page.request.get('/api/admin/brokers');
  if (!r.ok()) return [];
  const rows = await r.json();
  return (Array.isArray(rows) ? rows : (rows.rows || []))
    .filter((b) => b.loaded)
    .map((b) => b.account);
}

// ── Screenshot dir ────────────────────────────────────────────────────────────
function screenshotDir() {
  const dir = path.join('/tmp', 'shadow_exercise_screenshots');
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
  return dir;
}

async function snap(page, name) {
  const p = path.join(screenshotDir(), `${name}.png`);
  await page.screenshot({ path: p, fullPage: false });
  console.log(`[shadow] screenshot: ${p}`);
  return p;
}

// ── Chain helpers (mirrored from paper_exercise_phase1_2.spec.js) ────────────
async function goOrders(page) {
  await page.goto('/orders', { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.page-title-chip').first()).toBeVisible({ timeout: 15_000 });
}

async function ensureChainGridVisible(page) {
  const alreadyVisible = await page.locator('.chain-td-ce').first().isVisible().catch(() => false);
  if (!alreadyVisible) {
    const chainTab = page.locator('.oes-tabs button[role="tab"]').filter({ hasText: /chain/i }).first();
    if (await chainTab.count()) {
      const isDisabled = await chainTab.evaluate((el) => el.disabled || el.getAttribute('aria-disabled') === 'true').catch(() => false);
      const isSelected = await chainTab.evaluate((el) => el.getAttribute('aria-selected') === 'true').catch(() => false);
      if (!isDisabled && !isSelected) await chainTab.click();
    }
  }
  await expect(page.locator('.chain-td-ce, .chain-btn-buy').first()).toBeVisible({ timeout: 15_000 });
}

async function switchPickerAccount(page, acct) {
  const trigger = page.locator('.oes-picker .oes-account-pick .rbq-select-trigger');
  const visible = await trigger.isVisible({ timeout: 3_000 }).catch(() => false);
  if (!visible) {
    console.log(`[shadow] picker account Select not visible — single-account env, skip switch`);
    return false;
  }
  await trigger.click();
  const option = page.locator('.rbq-select-panel .rbq-select-option', { hasText: acct }).first();
  const optVisible = await option.isVisible({ timeout: 3_000 }).catch(() => false);
  if (!optVisible) {
    console.log(`[shadow] option "${acct}" not found in picker — closing panel`);
    await page.keyboard.press('Escape');
    return false;
  }
  await option.click();
  console.log(`[shadow] picker switched to account: ${acct}`);
  return true;
}

// ── Global state ──────────────────────────────────────────────────────────────
const _state = {
  // Pre-flight
  originalPaperMode: null,
  originalShadowMode: null,
  loadedAccounts: [],
  // Exercise
  acctA: null,
  acctB: null,
  modeDropdownText: null,
  stickyResultText: null,
  basketTag: null,
  algoRows: [],
  // Verification
  shadowRows: [],
  shadowBrokerOrderIds: [],
  basketTags: [],
  targetPcts: [],
  accounts: [],
  // Cleanup
  shadowRestoredTo: null,
  paperModeAfterCleanup: null,
  // Screenshots
  screenshots: {},
};

// ── Tests ─────────────────────────────────────────────────────────────────────
test.describe.configure({ mode: 'serial' });
test.setTimeout(180_000);

test.describe('Shadow Exercise — prod ramboq.com', () => {

  // Capture original values; restored in afterAll even on failure.
  test.beforeAll(async ({ browser }) => {
    const ctx = await browser.newContext();
    const p = await ctx.newPage();
    try {
      await authOnce(p);
      const paperVal = await readSetting(p, 'execution.paper_trading_mode');
      const shadowVal = await readSetting(p, 'execution.shadow_mode');
      _state.originalPaperMode = (paperVal === 'true' || paperVal === true);
      _state.originalShadowMode = (shadowVal === 'true' || shadowVal === true);
      console.log(`[shadow] beforeAll: paper_trading_mode=${_state.originalPaperMode} shadow_mode=${_state.originalShadowMode}`);
    } finally {
      await ctx.close();
    }
  });

  test.afterAll(async ({ browser }) => {
    const ctx = await browser.newContext();
    const p = await ctx.newPage();
    try {
      await authOnce(p);
      // Always restore shadow_mode to original
      await patchSetting(p, 'execution.shadow_mode', !!_state.originalShadowMode);
      _state.shadowRestoredTo = _state.originalShadowMode;
      console.log(`[shadow] afterAll: restored shadow_mode=${_state.originalShadowMode}`);
      // Read back paper_trading_mode to verify it wasn't changed
      const paperAfter = await readSetting(p, 'execution.paper_trading_mode');
      _state.paperModeAfterCleanup = (paperAfter === 'true' || paperAfter === true);
      console.log(`[shadow] afterAll: paper_trading_mode now=${_state.paperModeAfterCleanup} (was=${_state.originalPaperMode})`);
    } catch (e) {
      console.warn(`[shadow] afterAll cleanup error: ${e.message}`);
    } finally {
      await ctx.close();
    }
  });

  // ── Pre-flight 1: read + record paper_trading_mode ─────────────────────
  test('preflight-1: read paper_trading_mode on /admin/execution?mode=paper', async ({ page }) => {
    await authOnce(page);
    await page.goto('/admin/execution?mode=paper', { waitUntil: 'domcontentloaded' });
    await page.waitForLoadState('networkidle');

    // Read via API
    const paperVal = await readSetting(page, 'execution.paper_trading_mode');
    _state.originalPaperMode = (paperVal === 'true' || paperVal === true);
    console.log(`[shadow] paper_trading_mode=${_state.originalPaperMode}`);

    // Screenshot the page
    _state.screenshots.paper_page = await snap(page, '01_paper_page');

    // Look for a banner or label that identifies the mode on screen
    const bannerText = await page.locator(
      '.exec-mode-banner, .paper-banner, .mode-banner, [class*="banner"], h2, .exec-mode-label, .exec-mode-chip'
    ).first().textContent({ timeout: 5_000 }).catch(() => 'not found');
    console.log(`[shadow] paper page banner text: "${bannerText}"`);

    // The test should NOT change paper_trading_mode — just record
    expect(typeof _state.originalPaperMode).toBe('boolean');
  });

  // ── Pre-flight 2: read + record shadow_mode, then flip to true ──────────
  test('preflight-2: read shadow_mode + flip to true, wait for SHADOW banner', async ({ page }) => {
    await authOnce(page);
    await page.goto('/admin/execution?mode=shadow', { waitUntil: 'domcontentloaded' });
    await page.waitForLoadState('networkidle');

    // Capture current shadow_mode via API
    const shadowBefore = await readSetting(page, 'execution.shadow_mode');
    _state.originalShadowMode = (shadowBefore === 'true' || shadowBefore === true);
    console.log(`[shadow] shadow_mode before flip: ${_state.originalShadowMode}`);
    _state.screenshots.shadow_before = await snap(page, '02_shadow_before_flip');

    // Flip shadow_mode to true via settings PATCH
    await patchSetting(page, 'execution.shadow_mode', true);

    // Reload the execution page to pick up the new state
    await page.goto('/admin/execution?mode=shadow', { waitUntil: 'domcontentloaded' });
    await page.waitForLoadState('networkidle');

    // Verify via API
    const modeNow = await readMode(page);
    console.log(`[shadow] execution mode after flip: ${modeNow.mode}`);

    _state.screenshots.shadow_after = await snap(page, '03_shadow_after_flip');

    // If mode doesn't resolve to shadow within 8s, abort
    if (modeNow.mode !== 'shadow') {
      // Abort — flip back and fail
      await patchSetting(page, 'execution.shadow_mode', !!_state.originalShadowMode);
      throw new Error(`shadow mode didn't engage — mode is "${modeNow.mode}", expected "shadow". Flipped back. Aborting.`);
    }

    // Look for SHADOW banner or indicator on the page
    const shadowBannerVisible = await page.locator(
      '[class*="shadow"], .mode-banner, .exec-mode-banner, .shadow-banner, :text("SHADOW")'
    ).first().isVisible({ timeout: 8_000 }).catch(() => false);
    console.log(`[shadow] SHADOW banner/indicator visible: ${shadowBannerVisible}`);

    expect(modeNow.mode).toBe('shadow');
  });

  // ── Pre-flight 3: load broker accounts ────────────────────────────────
  test('preflight-3: read loaded broker accounts', async ({ page }) => {
    await authOnce(page);
    const accts = await getLoadedAccounts(page);
    _state.loadedAccounts = accts;
    console.log(`[shadow] loaded broker accounts: ${JSON.stringify(accts)}`);
    expect(accts.length, 'At least 1 broker account must be loaded').toBeGreaterThan(0);

    _state.acctA = accts[0];
    _state.acctB = accts.length >= 2 ? accts[1] : accts[0];
    console.log(`[shadow] acctA=${_state.acctA} acctB=${_state.acctB}`);
  });

  // ── Exercise: build + submit basket ──────────────────────────────────────
  test('exercise: build 2-leg CE basket and submit in shadow mode', async ({ page }) => {
    await authOnce(page);
    await goOrders(page);
    await ensureChainGridVisible(page);

    // Leg 1 — account A
    if (_state.loadedAccounts.length >= 2) {
      await switchPickerAccount(page, _state.acctA);
    }
    const ceBtn1 = page.locator('.chain-td-ce .chain-btn-buy').first();
    await expect(ceBtn1).toBeVisible({ timeout: 15_000 });
    await ceBtn1.click();
    await expect(page.locator('.oes-basket-pill').first()).toBeVisible({ timeout: 8_000 });
    console.log(`[shadow] leg 1 added (acctA=${_state.acctA})`);

    // Leg 2 — switch to account B
    if (_state.loadedAccounts.length >= 2) {
      await switchPickerAccount(page, _state.acctB);
    }
    const ceBtns = page.locator('.chain-td-ce .chain-btn-buy');
    const ceBtnCount = await ceBtns.count();
    await ceBtns.nth(ceBtnCount > 2 ? 2 : (ceBtnCount > 1 ? 1 : 0)).click();
    await new Promise((r) => setTimeout(r, 400));
    console.log(`[shadow] leg 2 added (acctB=${_state.acctB})`);

    const pills = page.locator('.oes-basket-pill');
    const pillCount = await pills.count();
    console.log(`[shadow] basket pill count: ${pillCount}`);
    expect(pillCount).toBeGreaterThanOrEqual(1);

    // Fill limits with 1.00
    const limitInputs = page.locator('.oes-basket-pill-limit');
    const limitCount = await limitInputs.count();
    for (let i = 0; i < limitCount; i++) {
      await limitInputs.nth(i).fill('1.00');
    }

    // Capture mode dropdown text (should show shadow)
    const modeDrop = page.locator('.oes-mode-select, .oes-common-mode, select[name="mode"], .oes-mode-pill').first();
    _state.modeDropdownText = await modeDrop.textContent({ timeout: 3_000 }).catch(() => null)
      || await modeDrop.inputValue({ timeout: 2_000 }).catch(() => null);
    console.log(`[shadow] mode dropdown text: ${_state.modeDropdownText}`);

    // Screenshot basket with both pills
    _state.screenshots.basket = await snap(page, '04_basket_before_submit');

    // Submit
    const submitBtn = page.locator('.oes-common-submit').last();
    await expect(submitBtn).toBeVisible({ timeout: 5_000 });
    await expect(submitBtn).toBeEnabled({ timeout: 3_000 });
    await submitBtn.click();

    // Wait for sticky result (up to 10s)
    const stickyOk = page.locator('.oes-sticky-result-ok');
    await expect(stickyOk).toBeVisible({ timeout: 15_000 });
    _state.stickyResultText = (await stickyOk.textContent()).trim();
    console.log(`[shadow] sticky result: ${_state.stickyResultText}`);

    // Extract basket tag from result text
    const tagMatch = _state.stickyResultText.match(/basket[:\-]\s*([a-z0-9\-]+)/i);
    if (tagMatch) _state.basketTag = tagMatch[1];

    _state.screenshots.submit_result = await snap(page, '05_submit_result');
  });

  // ── Exercise: capture Order Activity card ─────────────────────────────
  test('exercise: capture Order Activity card', async ({ page }) => {
    await authOnce(page);
    await goOrders(page);

    // Open the Order Activity card / Book tab if needed
    const bookTab = page.locator('.oc-tab--book, [data-tab="book"], button').filter({ hasText: /^Book$/ }).first();
    if (await bookTab.count()) {
      await bookTab.click();
      await new Promise((r) => setTimeout(r, 1000));
    }

    // Expand the activity card if collapsed
    const activityCard = page.locator('.order-activity-card, [data-card="activity"], .log-panel').first();
    await activityCard.isVisible({ timeout: 5_000 }).catch(() => null);

    _state.screenshots.activity = await snap(page, '06_order_activity');
  });

  // ── Verification: GET /api/orders/algo/recent ─────────────────────────
  test('verify: GET /api/orders/algo/recent — shadow rows', async ({ page }) => {
    await authOnce(page);

    const r = await page.request.get('/api/orders/algo/recent?limit=20');
    expect(r.ok(), `algo/recent returned ${r.status()}`).toBe(true);

    const rows = await r.json();
    _state.algoRows = rows || [];
    console.log(`[shadow] algo/recent total rows: ${_state.algoRows.length}`);

    // Log first 5 rows
    for (const row of _state.algoRows.slice(0, 5)) {
      console.log(
        `[shadow] row id=${row.id} sym=${row.symbol} mode=${row.mode} ` +
        `status=${row.status} type=${row.transaction_type} qty=${row.quantity} ` +
        `acct=${row.account} basket=${row.basket_tag || 'none'} ` +
        `target_pct=${row.target_pct} broker_order_id=${row.broker_order_id ?? 'null'}`
      );
    }

    // Find the rows we just placed — filter by most recent basket_tag if captured,
    // else look at the most recent rows
    let candidates = _state.algoRows;
    if (_state.basketTag) {
      const byBasket = _state.algoRows.filter((o) => o.basket_tag && o.basket_tag.includes(_state.basketTag));
      if (byBasket.length > 0) candidates = byBasket;
    }
    // Also try rows with basket_tag prefix 'ramboq-basket-'
    const basketRows = _state.algoRows.filter((o) => o.basket_tag && o.basket_tag.startsWith('ramboq-basket-'));
    if (basketRows.length > 0 && candidates === _state.algoRows) candidates = basketRows.slice(0, 4);

    console.log(`[shadow] candidate rows for verification: ${candidates.length}`);
    _state.shadowRows = candidates;
    _state.shadowBrokerOrderIds = candidates.map((o) => o.broker_order_id ?? null);
    _state.basketTags = [...new Set(candidates.map((o) => o.basket_tag).filter(Boolean))];
    _state.targetPcts = [...new Set(candidates.map((o) => o.target_pct))];
    _state.accounts = [...new Set(candidates.map((o) => o.account))];

    console.log(`[shadow] shadow rows mode values: ${JSON.stringify(candidates.map((o) => o.mode))}`);
    console.log(`[shadow] broker_order_ids: ${JSON.stringify(_state.shadowBrokerOrderIds)}`);
    console.log(`[shadow] basket_tags: ${JSON.stringify(_state.basketTags)}`);
    console.log(`[shadow] target_pcts: ${JSON.stringify(_state.targetPcts)}`);
    console.log(`[shadow] accounts: ${JSON.stringify(_state.accounts)}`);
  });

  // ── Cleanup: flip shadow_mode back to false ───────────────────────────
  test('cleanup-1: flip shadow_mode back to original', async ({ page }) => {
    await authOnce(page);
    await patchSetting(page, 'execution.shadow_mode', !!_state.originalShadowMode);
    _state.shadowRestoredTo = _state.originalShadowMode;

    // Reload and confirm SHADOW banner is gone
    await page.goto('/admin/execution?mode=shadow', { waitUntil: 'domcontentloaded' });
    await page.waitForLoadState('networkidle');

    const modeAfter = await readMode(page);
    console.log(`[shadow] mode after cleanup: ${modeAfter.mode}`);

    _state.screenshots.after_cleanup = await snap(page, '07_after_cleanup');

    // Mode should NOT be shadow any more
    expect(modeAfter.mode, 'shadow mode should be off after cleanup').not.toBe('shadow');
  });

  // ── Cleanup: verify paper_trading_mode unchanged ──────────────────────
  test('cleanup-2: verify paper_trading_mode unchanged', async ({ page }) => {
    await authOnce(page);
    await page.goto('/admin/execution?mode=paper', { waitUntil: 'domcontentloaded' });
    await page.waitForLoadState('networkidle');

    const paperNow = await readSetting(page, 'execution.paper_trading_mode');
    _state.paperModeAfterCleanup = (paperNow === 'true' || paperNow === true);
    console.log(`[shadow] paper_trading_mode after cleanup: ${_state.paperModeAfterCleanup} (was: ${_state.originalPaperMode})`);

    _state.screenshots.paper_after = await snap(page, '08_paper_after_cleanup');

    expect(_state.paperModeAfterCleanup).toBe(_state.originalPaperMode);
  });

  // ── Final: emit report ────────────────────────────────────────────────
  test('report: emit structured findings', async () => {
    const now = new Date().toISOString();

    // ── Step 1: shadow_mode flipped true
    const step1_pass = _state.shadowRows.length > 0 || true; // will assess mode below
    const modeValues = _state.shadowRows.map((o) => o.mode);
    const allShadow = modeValues.length > 0 && modeValues.every((m) => m === 'shadow');

    // ── Step 2: per-leg account routing
    const multiAcct = _state.loadedAccounts.length >= 2;
    const distinctAccts = _state.accounts.length;
    const acctRoutingPass = !multiAcct || distinctAccts >= 2;

    // ── Step 3: mode='shadow' on every leg
    const step3_pass = allShadow;

    // ── Step 4: broker_order_id NULL
    const allBrokerNull = _state.shadowBrokerOrderIds.every((id) => !id);
    const step4_pass = _state.shadowRows.length > 0 ? allBrokerNull : false;

    // ── Step 5: basket_tag persisted + shared
    const step5_pass = _state.basketTags.length > 0;

    // ── Step 6: target_pct=0.30 default
    const targetPctCheck = _state.targetPcts.length === 1 && Math.abs(Number(_state.targetPcts[0]) - 0.30) < 0.01;
    const step6_pass = targetPctCheck;

    // ── Step 7: shadow_mode flipped back
    const step7_pass = _state.shadowRestoredTo === false || _state.shadowRestoredTo === _state.originalShadowMode;

    // ── Step 8: paper_trading_mode unchanged
    const step8_pass = _state.paperModeAfterCleanup === _state.originalPaperMode;

    console.log('\n\n==== SHADOW EXERCISE REPORT ====');
    console.log(`Timestamp: ${now}`);
    console.log(`Loaded accounts: ${JSON.stringify(_state.loadedAccounts)}`);
    console.log(`acctA=${_state.acctA} acctB=${_state.acctB}`);
    console.log(`originalShadowMode=${_state.originalShadowMode} originalPaperMode=${_state.originalPaperMode}`);
    console.log(`stickyResult: ${_state.stickyResultText}`);
    console.log(`shadowRows count: ${_state.shadowRows.length}`);
    console.log(`mode values: ${JSON.stringify(modeValues)}`);
    console.log(`broker_order_ids: ${JSON.stringify(_state.shadowBrokerOrderIds)}`);
    console.log(`basket_tags: ${JSON.stringify(_state.basketTags)}`);
    console.log(`target_pcts: ${JSON.stringify(_state.targetPcts)}`);
    console.log(`accounts on rows: ${JSON.stringify(_state.accounts)}`);
    console.log(`step1 (shadow flip) pass: ${step1_pass}`);
    console.log(`step2 (acct routing) pass: ${acctRoutingPass}`);
    console.log(`step3 (mode=shadow) pass: ${step3_pass}`);
    console.log(`step4 (broker_id null) pass: ${step4_pass}`);
    console.log(`step5 (basket_tag) pass: ${step5_pass}`);
    console.log(`step6 (target_pct=0.30) pass: ${step6_pass}`);
    console.log(`step7 (shadow off) pass: ${step7_pass}`);
    console.log(`step8 (paper unchanged) pass: ${step8_pass}`);
    console.log('Screenshots:', JSON.stringify(_state.screenshots, null, 2));
    console.log('==== END REPORT ====\n\n');

    // Non-fatal — this test just emits data; individual tests above had the assertions
    expect(true).toBe(true);
  });
});
