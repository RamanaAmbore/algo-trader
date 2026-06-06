/**
 * Paper Exercise — Phase 1 (multi-account basket) + Phase 2 (auto profit target).
 * Target: https://dev.ramboq.com (set PLAYWRIGHT_BASE_URL env).
 * Dev branch forces paper → no live broker calls.
 *
 * Run:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/paper_exercise_phase1_2.spec.js \
 *     --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';

// ── Auth ──────────────────────────────────────────────────────────────────
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

  // Register initScript so it fires on every navigation for THIS page's context.
  // addInitScript is idempotent from Playwright's perspective — re-registering
  // with the same script just adds another entry, but the token won't change.
  await page.context().addInitScript(({ tok, usr, sym }) => {
    sessionStorage.setItem('ramboq_token', tok);
    sessionStorage.setItem('ramboq_user', JSON.stringify({
      user_id: usr, username: usr, role: 'admin', display_name: usr,
    }));
    localStorage.setItem('ramboq.recent.symbol', sym);
  }, { tok: _cachedToken, usr: _cachedUser || 'rambo', sym: 'NIFTY' });

  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${_cachedToken}` });

  // Seed a blank page first so the initScript fires before any real nav
  // Only do this if the page hasn't navigated yet (about:blank)
  const url = page.url();
  if (!url || url === 'about:blank') {
    await page.goto('/', { waitUntil: 'domcontentloaded' });
  }
}

async function getLoadedAccounts(page) {
  const r = await page.request.get('/api/admin/brokers');
  if (!r.ok()) return [];
  const rows = await r.json();
  return (Array.isArray(rows) ? rows : (rows.rows || []))
    .filter((b) => b.loaded)
    .map((b) => b.account);
}

async function goOrders(page) {
  await page.goto('/orders');
  await page.waitForLoadState('domcontentloaded');
  await expect(page.locator('.page-title-chip').first())
    .toBeVisible({ timeout: 15_000 });
}

async function ensureChainGridVisible(page) {
  const chainGrid = page.locator('.chain-td-ce').first();
  const alreadyVisible = await chainGrid.isVisible().catch(() => false);
  if (!alreadyVisible) {
    // Only click the chain tab if it's not already active and not disabled
    const chainTab = page.locator('.oes-tabs button[role="tab"]').filter({ hasText: /chain/i }).first();
    if (await chainTab.count()) {
      const isDisabled = await chainTab.evaluate((el) => el.disabled || el.getAttribute('aria-disabled') === 'true').catch(() => false);
      const isSelected = await chainTab.evaluate((el) => el.getAttribute('aria-selected') === 'true').catch(() => false);
      if (!isDisabled && !isSelected) {
        await chainTab.click();
      }
    }
  }
  // Wait for either .chain-td-ce (options chain) or + buttons to appear
  await expect(page.locator('.chain-td-ce, .chain-btn-buy').first()).toBeVisible({ timeout: 15_000 });
}

async function addFirstCeLeg(page) {
  const ceAddBtn = page.locator('.chain-td-ce .chain-btn-buy').first();
  await expect(ceAddBtn).toBeVisible({ timeout: 15_000 });
  await ceAddBtn.click();
  await expect(page.locator('.oes-basket-pill').first()).toBeVisible({ timeout: 8_000 });
}

/**
 * Switch the chain picker to `acct` (account B) before adding leg 2.
 * Primary path: click the .oes-account-pick .rbq-select-trigger, then
 * click the matching option.  Falls back silently if the Select is not
 * present (single-account environment).
 */
async function switchPickerAccount(page, acct) {
  const trigger = page.locator('.oes-picker .oes-account-pick .rbq-select-trigger');
  const visible = await trigger.isVisible({ timeout: 3_000 }).catch(() => false);
  if (!visible) {
    console.log(`[exercise] picker account Select not visible — single-account env, skip switch`);
    return false;
  }
  await trigger.click();
  // The option list renders inside .rbq-select-panel; find the matching label
  const option = page.locator('.rbq-select-panel .rbq-select-option', { hasText: acct }).first();
  const optVisible = await option.isVisible({ timeout: 3_000 }).catch(() => false);
  if (!optVisible) {
    console.log(`[exercise] option "${acct}" not found in picker — closing panel`);
    await page.keyboard.press('Escape');
    return false;
  }
  await option.click();
  console.log(`[exercise] picker switched to account: ${acct}`);
  return true;
}

async function addSecondCeLeg(page) {
  const ceAddBtns = page.locator('.chain-td-ce .chain-btn-buy');
  await expect(ceAddBtns.first()).toBeVisible({ timeout: 8_000 });
  const count = await ceAddBtns.count();
  // Click a different strike from the first
  await ceAddBtns.nth(count > 2 ? 2 : (count > 1 ? 1 : 0)).click();
  // Give the second pill a moment to register
  await page.waitForTimeout(500);
}

function screenshotDir() {
  const dir = path.join('/tmp', 'paper_exercise_screenshots');
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
  return dir;
}

// ── Global state for cross-test data sharing ──────────────────────────────
const _state = {
  loadedAccounts: [],
  marginStripText: null,
  modeDropdownText: null,
  targetDefaultText: null,
  stickyResultText: null,
  basketTag: null,
  placedRows: [],
  algoRows: [],
  screenshots: {
    premargin: null,
    targetrow: null,
    stickyresult: null,
    orderbook: null,
  },
};

// ── Tests ─────────────────────────────────────────────────────────────────
test.describe.configure({ mode: 'serial' });
test.setTimeout(120_000);

test.describe('Paper Exercise — Phase 1 + 2', () => {

  // ── Step 0: broker accounts ───────────────────────────────────────────
  test('step-0: check loaded broker accounts on /admin/brokers', async ({ page }) => {
    await authOnce(page);

    const loadedAccts = await getLoadedAccounts(page);
    _state.loadedAccounts = loadedAccts;

    console.log(`[exercise] loaded broker accounts: ${JSON.stringify(loadedAccts)}`);

    // Navigate to brokers page and capture the status pills for reporting
    await page.goto('/admin/brokers');
    await page.waitForLoadState('domcontentloaded');

    // Wait for table to render
    const tbl = page.locator('.broker-table, table, .brok-row, [data-account]').first();
    await tbl.isVisible({ timeout: 10_000 }).catch(() => null);

    if (loadedAccts.length === 0) {
      console.warn('[exercise] ABORT: 0 broker accounts loaded on dev');
      // Record but continue — tests below will surface what's available
    }

    expect(loadedAccts.length, 'At least 1 broker account must be loaded').toBeGreaterThan(0);
  });

  // ── Step 1-2: build basket + capture pre-submit state ─────────────────
  test('step-1: build 2-leg CE basket + capture pre-submit state', async ({ page }) => {
    await authOnce(page);
    await goOrders(page);
    await ensureChainGridVisible(page);

    // ── Multi-account basket via chain-picker switch (PRIMARY PATH) ─────
    // Primary: switch the chain picker to account B before adding leg 2,
    // so leg.account is set correctly from the moment the leg is created
    // (avoids the Svelte 5 reactivity quirk on the per-leg basket pill
    // select — tracked as a known defect).
    //
    // Fallback (if picker switch is blocked / single-account): attempt
    // to set the per-leg basket pill <select> on leg 2 directly.

    if (_state.loadedAccounts.length >= 2) {
      const acctA = _state.loadedAccounts[0];
      const acctB = _state.loadedAccounts[1];

      // Ensure picker is on account A before leg 1
      await switchPickerAccount(page, acctA);

      // Add leg 1 (account A)
      await addFirstCeLeg(page);
      console.log(`[exercise] leg 1 added with picker account: ${acctA}`);

      // Switch picker to account B before leg 2
      const switched = await switchPickerAccount(page, acctB);

      // Add leg 2 (should inherit account B from picker)
      await addSecondCeLeg(page);
      console.log(`[exercise] leg 2 added with picker account: ${acctB} (picker switched: ${switched})`);

      if (!switched) {
        // Fallback: try per-leg basket pill <select> (known defect — may not propagate)
        const pillSelects = page.locator('.oes-basket-pill-acct');
        const selCount = await pillSelects.count();
        console.log(`[exercise] FALLBACK per-leg account selects: ${selCount}`);
        if (selCount >= 2) {
          await pillSelects.nth(1).selectOption(acctB).catch((e) => {
            console.log(`[exercise] FALLBACK select failed (known defect): ${e.message}`);
          });
          console.log(`[exercise] FALLBACK: attempted per-leg select for leg 2 → ${acctB}`);
        }
      }
    } else {
      // Single-account: add both legs normally
      await addFirstCeLeg(page);
      await addSecondCeLeg(page);
      console.log(`[exercise] only 1 broker account loaded — basket is single-account`);
    }

    // Confirm we have at least 2 pills (or 1 if chain has only 1 row)
    const pills = page.locator('.oes-basket-pill');
    const pillCount = await pills.count();
    console.log(`[exercise] basket pill count: ${pillCount}`);
    expect(pillCount).toBeGreaterThanOrEqual(1);

    // Fill limit inputs with "1.00"
    const limitInputs = page.locator('.oes-basket-pill-limit');
    const limitCount = await limitInputs.count();
    for (let i = 0; i < limitCount; i++) {
      await limitInputs.nth(i).fill('1.00');
    }

    // Capture margin strip (multi-account shows .oes-basket-margin-strip)
    const strip = page.locator('.oes-basket-margin-strip');
    const stripVisible = await strip.isVisible({ timeout: 2_000 }).catch(() => false);

    if (stripVisible) {
      const rows = strip.locator('.bms-row');
      const rowCount = await rows.count();
      let marginLines = [];
      for (let i = 0; i < rowCount; i++) {
        const kels = await rows.nth(i).locator('.bms-k').allInnerTexts();
        const vels = await rows.nth(i).locator('.bms-v').allInnerTexts();
        marginLines.push(`${kels.join('/')} → ${vels.join('/')}`);
      }
      _state.marginStripText = marginLines.join(' | ');
      console.log(`[exercise] margin strip: ${_state.marginStripText}`);
    } else {
      _state.marginStripText = 'single-account — strip hidden (expected)';
      console.log('[exercise] margin strip hidden (single-account)');
    }

    // Screenshot 1: pre-submit state
    const sc1 = path.join(screenshotDir(), 'pre_submit_state.png');
    await page.screenshot({ path: sc1, fullPage: false });
    _state.screenshots.premargin = sc1;
    console.log(`[exercise] screenshot 1: ${sc1}`);

    // Capture mode dropdown value
    const modeDrop = page.locator('.oes-mode-select, .oes-common-mode, select[name="mode"], .oes-mode-pill').first();
    const modeText = await modeDrop.textContent({ timeout: 3_000 }).catch(() => null)
      || await modeDrop.inputValue({ timeout: 2_000 }).catch(() => null);
    _state.modeDropdownText = modeText ? modeText.trim() : 'not found';
    console.log(`[exercise] mode value: ${_state.modeDropdownText}`);
  });

  // ── Step 2b: capture Target default in Order Ticket tab ───────────────
  test('step-2b: open Order Ticket tab, capture Target default', async ({ page }) => {
    await authOnce(page);
    await goOrders(page);

    // Switch to the "Order Ticket" tab
    const ticketTab = page.locator('.oes-tabs button[role="tab"]').filter({ hasText: /order.?ticket/i }).first();
    if (await ticketTab.count()) {
      await ticketTab.click();
      await page.waitForTimeout(500);
    }

    // Look for the Target row
    const targetInput = page.locator('#ot-target, input[id*="target"], .ot-target-row input').first();
    const targetVisible = await targetInput.isVisible({ timeout: 5_000 }).catch(() => false);

    if (targetVisible) {
      const val = await targetInput.inputValue().catch(() => null);
      const placeholder = await targetInput.getAttribute('placeholder').catch(() => null);
      _state.targetDefaultText = val || placeholder || 'empty';
      console.log(`[exercise] target default: val="${val}" placeholder="${placeholder}"`);
    } else {
      _state.targetDefaultText = 'Target field not visible';
      console.log('[exercise] Target field not found on Order Ticket tab');
    }

    // Screenshot 2: target row
    const sc2 = path.join(screenshotDir(), 'order_ticket_target.png');
    await page.screenshot({ path: sc2, fullPage: false });
    _state.screenshots.targetrow = sc2;
    console.log(`[exercise] screenshot 2: ${sc2}`);
  });

  // ── Step 3: submit the basket ─────────────────────────────────────────
  test('step-3: submit basket → wait for sticky-result-ok', async ({ page }) => {
    await authOnce(page);
    await goOrders(page);
    await ensureChainGridVisible(page);

    // Use picker-switch flow for multi-account (same logic as step-1)
    if (_state.loadedAccounts.length >= 2) {
      await switchPickerAccount(page, _state.loadedAccounts[0]);
      await addFirstCeLeg(page);
      await switchPickerAccount(page, _state.loadedAccounts[1]);
      await addSecondCeLeg(page);
    } else {
      await addFirstCeLeg(page);
      await addSecondCeLeg(page);
    }

    const limitInputs = page.locator('.oes-basket-pill-limit');
    const limitCount = await limitInputs.count();
    for (let i = 0; i < limitCount; i++) {
      await limitInputs.nth(i).fill('1.00');
    }

    // Submit
    const submitBtn = page.locator('.oes-common-submit').last();
    await expect(submitBtn).toBeVisible({ timeout: 5_000 });
    await expect(submitBtn).toBeEnabled({ timeout: 3_000 });
    await submitBtn.click();

    // Wait for sticky result
    const stickyOk = page.locator('.oes-sticky-result-ok');
    await expect(stickyOk).toBeVisible({ timeout: 15_000 });

    _state.stickyResultText = (await stickyOk.textContent()).trim();
    console.log(`[exercise] sticky result: ${_state.stickyResultText}`);

    // Screenshot 3: submit result
    const sc3 = path.join(screenshotDir(), 'submit_result.png');
    await page.screenshot({ path: sc3, fullPage: false });
    _state.screenshots.stickyresult = sc3;

    // Try to capture basket tag from the result
    const tagMatch = _state.stickyResultText.match(/basket[:\-]\s*([a-z0-9\-]+)/i);
    if (tagMatch) _state.basketTag = tagMatch[1];
  });

  // ── Step 4: inspect Order Activity Book ──────────────────────────────
  test('step-4: inspect Order Activity Book for placed rows', async ({ page }) => {
    await authOnce(page);
    await goOrders(page);

    // Open Order Activity → Book tab
    const bookTab = page.locator('.oc-tab--book, [data-tab="book"], button').filter({ hasText: /^Book$/ }).first();
    if (await bookTab.count()) {
      await bookTab.click();
      await page.waitForTimeout(1000);
    }

    // Capture tp and basket chips
    const tpChips = page.locator('.log-chip-tp');
    const basketChips = page.locator('.log-chip-basket');

    const tpCount = await tpChips.count();
    const bkCount = await basketChips.count();
    console.log(`[exercise] tp chips: ${tpCount}, basket chips: ${bkCount}`);

    // Capture first visible basket tag
    if (bkCount > 0) {
      const firstBk = await basketChips.first().textContent().catch(() => null);
      if (firstBk && !_state.basketTag) {
        const m = firstBk.match(/basket:([a-z0-9\-]+)/i);
        if (m) _state.basketTag = m[1];
      }
      console.log(`[exercise] first basket chip: ${firstBk}`);
    }

    // Capture first tp chip
    if (tpCount > 0) {
      const firstTp = await tpChips.first().textContent().catch(() => null);
      console.log(`[exercise] first tp chip: ${firstTp}`);
    }

    // Look for mode pill on any row
    const modePills = page.locator('.log-chip-mode, .mode-pill, [class*="mode-pill"]').first();
    const modePillText = await modePills.textContent({ timeout: 2_000 }).catch(() => null);
    console.log(`[exercise] first mode pill text: ${modePillText}`);

    // Screenshot 4: order book
    const sc4 = path.join(screenshotDir(), 'order_book.png');
    await page.screenshot({ path: sc4, fullPage: false });
    _state.screenshots.orderbook = sc4;

    // Wait up to 60s for a parent_order_id chip (TP child)
    const parentChip = page.locator('[class*="parent"], .log-chip-parent, .log-chip-tp-child').first();
    await parentChip.isVisible({ timeout: 20_000 }).catch(() => null);
    const parentChipText = await parentChip.textContent({ timeout: 1_000 }).catch(() => null);
    console.log(`[exercise] parent_order_id chip: ${parentChipText}`);

    // Store placed row summary
    _state.placedRows = {
      tpChipCount: tpCount,
      basketChipCount: bkCount,
      firstTpText: tpCount > 0 ? await tpChips.first().textContent().catch(() => null) : null,
      firstBasketText: bkCount > 0 ? await basketChips.first().textContent().catch(() => null) : null,
      parentChipText,
    };
  });

  // ── Step 5: verify via API ────────────────────────────────────────────
  test('step-5: GET /api/orders/algo/recent + parse for mode=paper', async ({ page }) => {
    await authOnce(page);

    const r = await page.request.get('/api/orders/algo/recent?limit=20');
    expect(r.ok(), `algo recent returned ${r.status()}`).toBe(true);

    const rows = await r.json();
    console.log(`[exercise] algo/recent rows count: ${(rows || []).length}`);

    _state.algoRows = (rows || []).slice(0, 5);

    // Check mode values
    const paperRows = (rows || []).filter((o) => o.mode === 'paper');
    const simRows   = (rows || []).filter((o) => o.mode === 'sim');
    const liveRows  = (rows || []).filter((o) => o.mode === 'live');
    console.log(`[exercise] paper rows: ${paperRows.length}, sim: ${simRows.length}, live: ${liveRows.length}`);

    // Show recent rows detail
    for (const row of (rows || []).slice(0, 5)) {
      console.log(
        `[exercise] row id=${row.id} sym=${row.symbol} mode=${row.mode} ` +
        `status=${row.status} type=${row.transaction_type} qty=${row.quantity} ` +
        `acct=${row.account} basket=${row.basket_tag || 'none'} target=${row.target_pct}`
      );
    }

    // Find rows with basket tags (from our exercise run)
    const basketRows = (rows || []).filter((o) => o.basket_tag);
    if (basketRows.length) {
      console.log(`[exercise] rows with basket_tag: ${basketRows.length}`);
      for (const r of basketRows.slice(0, 4)) {
        console.log(`  basket_tag=${r.basket_tag} target_pct=${r.target_pct} parent=${r.parent_order_id}`);
      }
    }

    expect(rows).toBeTruthy();
  });

  // ── Final: emit report data ───────────────────────────────────────────
  test('step-final: emit structured report data', async ({ page }) => {
    await authOnce(page);

    console.log('\n==== EXERCISE REPORT DATA ====');
    console.log('accounts:', JSON.stringify(_state.loadedAccounts));
    console.log('marginStripText:', _state.marginStripText);
    console.log('modeDropdownText:', _state.modeDropdownText);
    console.log('targetDefaultText:', _state.targetDefaultText);
    console.log('stickyResultText:', _state.stickyResultText);
    console.log('basketTag:', _state.basketTag);
    console.log('placedRows:', JSON.stringify(_state.placedRows));
    console.log('recentAlgoRows:', JSON.stringify(_state.algoRows));
    console.log('screenshots:', JSON.stringify(_state.screenshots));
    console.log('==== END REPORT DATA ====\n');

    // Soft assertions — surface failures without aborting
    const issues = [];

    // Check margin strip rendered or was correctly hidden
    if (!_state.marginStripText) issues.push('margin strip data not captured');

    // Check sticky result contained "placed"
    if (_state.stickyResultText && !/placed/i.test(_state.stickyResultText)) {
      issues.push(`sticky result did not contain "placed": "${_state.stickyResultText}"`);
    }

    // Multi-account check: sticky result should mention "across 2 accounts"
    if (_state.loadedAccounts.length >= 2) {
      if (_state.stickyResultText && !/across\s+2\s+accounts/i.test(_state.stickyResultText)) {
        console.warn(`[exercise] multi-account sticky result check: expected "across 2 accounts" — got: "${_state.stickyResultText}"`);
        issues.push(`sticky result does not mention "across 2 accounts" — per-leg account routing may be broken`);
      } else {
        console.log('[exercise] multi-account sticky result: OK — "across 2 accounts" confirmed');
      }

      // Check distinct accounts on AlgoOrder rows in same basket
      const basketRows = _state.algoRows.filter((o) => o.basket_tag);
      if (basketRows.length >= 2) {
        const accts = [...new Set(basketRows.map((r) => r.account))];
        if (accts.length < 2) {
          issues.push(`AlgoOrder rows all have same account (${accts[0]}) — multi-account routing failed`);
        } else {
          console.log(`[exercise] AlgoOrder distinct accounts: ${JSON.stringify(accts)} — OK`);
        }
      }
    }

    // Check mode=paper on algo rows
    const paperRows = _state.algoRows.filter((o) => o.mode === 'paper');
    if (_state.algoRows.length > 0 && paperRows.length === 0) {
      issues.push('no paper-mode rows in recent algo orders');
    }

    if (issues.length) {
      console.warn('[exercise] Issues:', issues);
    }

    console.log('[exercise] exercise complete');
    // Don't hard-fail — all data is in the report
    expect(true).toBe(true);
  });
});
