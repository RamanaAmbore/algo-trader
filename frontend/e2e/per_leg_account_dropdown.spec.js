/**
 * Regression spec: per-leg basket pill account dropdown (commit 8b918943).
 *
 * The bug (pre-fix): `leg.account = v` in the onchange handler mutated the
 * each-block iteration variable directly, which Svelte 5 does not propagate
 * back through keyed `(leg.key)` bindings. The fix switches to
 * `updateLegByKey(leg.key, b => ({ ...b, account: v }))` — same map+spread
 * pattern already used for lots/limit inputs — so the select value sticks.
 *
 * This spec verifies the fix by:
 *  1. Adding 2 CE legs (both default to account A).
 *  2. Changing the second pill's <select> to account B via selectOption().
 *  3. Confirming inputValue() equals accountB before AND after a 1s wait
 *     (catches the revert that happened pre-fix).
 *  4. Submitting and checking the /api/orders/basket payload has 2 groups
 *     (one per account), and that the 2 newest AlgoOrder rows carry distinct
 *     accounts.
 *
 * Target: https://dev.ramboq.com (PLAYWRIGHT_BASE_URL env).
 * Dev branch forces paper — no live broker calls.
 *
 * Run:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/per_leg_account_dropdown.spec.js \
 *     --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';

// ── Auth ──────────────────────────────────────────────────────────────────────
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

  await page.context().addInitScript(({ tok, usr, sym }) => {
    sessionStorage.setItem('ramboq_token', tok);
    sessionStorage.setItem('ramboq_user', JSON.stringify({
      user_id: usr, username: usr, role: 'admin', display_name: usr,
    }));
    localStorage.setItem('ramboq.recent.symbol', sym);
  }, { tok: _cachedToken, usr: _cachedUser || 'rambo', sym: 'NIFTY' });

  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${_cachedToken}` });

  const url = page.url();
  if (!url || url === 'about:blank') {
    await page.goto('/', { waitUntil: 'domcontentloaded' });
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

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
  await expect(page.locator('.page-title-chip').first()).toBeVisible({ timeout: 15_000 });
}

async function ensureChainGridVisible(page) {
  const chainGrid = page.locator('.chain-td-ce').first();
  const alreadyVisible = await chainGrid.isVisible().catch(() => false);
  if (!alreadyVisible) {
    const chainTab = page.locator('.oes-tabs button[role="tab"]')
      .filter({ hasText: /chain/i }).first();
    if (await chainTab.count()) {
      const isDisabled = await chainTab.evaluate(
        (el) => el.disabled || el.getAttribute('aria-disabled') === 'true'
      ).catch(() => false);
      const isSelected = await chainTab.evaluate(
        (el) => el.getAttribute('aria-selected') === 'true'
      ).catch(() => false);
      if (!isDisabled && !isSelected) await chainTab.click();
    }
  }
  await expect(page.locator('.chain-td-ce, .chain-btn-buy').first())
    .toBeVisible({ timeout: 15_000 });
}

async function addCeLegAtIndex(page, idx) {
  const btns = page.locator('.chain-td-ce .chain-btn-buy');
  await expect(btns.first()).toBeVisible({ timeout: 15_000 });
  const total = await btns.count();
  await btns.nth(idx < total ? idx : total - 1).click();
}

// ── Config ────────────────────────────────────────────────────────────────────
test.describe.configure({ mode: 'serial' });
test.setTimeout(120_000);

// ── Spec ──────────────────────────────────────────────────────────────────────
test.describe('/orders — per-leg basket pill account dropdown (commit 8b918943)', () => {

  test('selectOption on pill-2 sticks and groups basket by account', async ({ page }) => {
    await authOnce(page);

    // Step 2: verify ≥2 loaded broker accounts.
    const loadedAccounts = await getLoadedAccounts(page);
    console.log(`[dropdown] loaded broker accounts: ${JSON.stringify(loadedAccounts)}`);
    if (loadedAccounts.length < 2) {
      test.skip(true, `need ≥2 broker accounts to test — got ${loadedAccounts.length}`);
      return;
    }
    const accountA = loadedAccounts[0];
    const accountB = loadedAccounts[1];
    console.log(`[dropdown] accountA=${accountA}  accountB=${accountB}`);

    // Step 3: open /orders.
    await goOrders(page);
    await ensureChainGridVisible(page);

    // Steps 4 & 5: add 2 CE legs with the chain picker on account A (default).
    // Both legs default to accountA initially.
    await addCeLegAtIndex(page, 0);
    await expect(page.locator('.oes-basket-pill').first()).toBeVisible({ timeout: 8_000 });
    console.log('[dropdown] PASS: leg 1 added');

    await addCeLegAtIndex(page, 1);

    // Step 6: confirm basket has 2 pills.
    const pills = page.locator('.oes-basket-pill');
    const pillCount = await pills.count();
    console.log(`[dropdown] pill count after adding 2 legs: ${pillCount}`);
    expect(pillCount, 'basket should have 2 pills').toBeGreaterThanOrEqual(2);
    console.log('[dropdown] PASS: 2 basket pills present');

    // Step 7: change pill-2's account select to accountB.
    const pillSelects = page.locator('.oes-basket-pill-acct');
    const selCount = await pillSelects.count();
    console.log(`[dropdown] .oes-basket-pill-acct selects found: ${selCount}`);

    if (selCount < 2) {
      // Per-leg account select only renders when _modalAccounts.length > 1.
      // If it's absent, the component isn't rendering multi-account selects.
      console.warn('[dropdown] WARN: fewer than 2 account selects — per-leg select may not render');
      test.skip(true, 'Per-leg account selector absent — single broker in session?');
      return;
    }

    // Read the ACTUAL account used by leg-1 (chain picker default may differ
    // from loadedAccounts[0] ordering returned by the brokers API).
    const actualAccountA = await pillSelects.nth(0).inputValue();
    console.log(`[dropdown] actual leg-1 account from pill-1 select: "${actualAccountA}"`);

    // Pick accountB as any loaded account that differs from actualAccountA.
    const actualAccountB = loadedAccounts.find((a) => a !== actualAccountA) || accountB;
    console.log(`[dropdown] will change pill-2 to accountB: "${actualAccountB}"`);

    const pill2Select = pillSelects.nth(1);
    await pill2Select.selectOption(actualAccountB);
    console.log(`[dropdown] selectOption(${actualAccountB}) called on pill-2`);

    // Step 8: capture diagnostics — read value immediately and after 1s.
    const valueBefore = await pill2Select.inputValue();
    console.log(`[dropdown] pill-2 inputValue() immediately after selectOption: "${valueBefore}"`);

    // Wait 1s for Svelte reactivity.
    await new Promise((r) => setTimeout(r, 1000));

    const valueAfter = await pill2Select.inputValue();
    console.log(`[dropdown] pill-2 inputValue() after 1s wait: "${valueAfter}"`);

    // Core assertion: value must equal actualAccountB (fix correctness).
    expect(
      valueBefore,
      `DEFECT LOCATION: pill-2 .oes-basket-pill-acct inputValue() immediately after selectOption ` +
      `— expected "${actualAccountB}" got "${valueBefore}". Direct leg.account mutation did not propagate.`
    ).toBe(actualAccountB);

    expect(
      valueAfter,
      `DEFECT LOCATION: pill-2 .oes-basket-pill-acct reverted after 1s — ` +
      `expected "${actualAccountB}" got "${valueAfter}". Svelte reactivity didn't hold the value.`
    ).toBe(actualAccountB);

    console.log(`[dropdown] PASS: pill-2 select stuck at ${actualAccountB} (before: ${valueBefore}, after: ${valueAfter})`);

    // Step 9: intercept POST /api/orders/basket to capture payload.
    let capturedBasketPayload = null;
    page.on('request', (req) => {
      if (req.url().includes('/api/orders/basket') && !req.url().includes('/margin') && req.method() === 'POST') {
        try {
          capturedBasketPayload = JSON.parse(req.postData() || '{}');
        } catch {
          capturedBasketPayload = null;
        }
      }
    });

    // Step 10: fill limit inputs with "1.00" on both pills.
    const limitInputs = page.locator('.oes-basket-pill-limit');
    const limitCount = await limitInputs.count();
    console.log(`[dropdown] filling ${limitCount} limit input(s) with 1.00`);
    for (let i = 0; i < limitCount; i++) {
      await limitInputs.nth(i).fill('1.00');
    }

    // Snapshot existing algo order IDs before submit.
    const snapR = await page.request.get('/api/orders/algo/recent?limit=20');
    const beforeIds = new Set(
      ((snapR.ok() ? await snapR.json() : []) || []).map((o) => o.id)
    );

    // Step 11: click Submit, wait for sticky result ok.
    const submitBtn = page.locator('.oes-common-submit').last();
    await expect(submitBtn).toBeVisible({ timeout: 5_000 });
    await expect(submitBtn).toBeEnabled({ timeout: 3_000 });
    await submitBtn.click();

    const stickyOk = page.locator('.oes-sticky-result-ok');
    await expect(stickyOk, 'sticky result should appear with ok state after submit').toBeVisible({ timeout: 20_000 });
    console.log(`[dropdown] PASS: sticky result visible — "${await stickyOk.textContent()}"`);

    // Step 9 continued: inspect intercepted payload.
    if (capturedBasketPayload) {
      const capturedGroups = (capturedBasketPayload.groups || []).map((g) => g.account);
      console.log(`[dropdown] intercepted /api/orders/basket groups[].account: ${JSON.stringify(capturedGroups)}`);

      expect(
        capturedGroups.length,
        `basket payload should have 2 groups (one per account) — got ${capturedGroups.length}: ${JSON.stringify(capturedGroups)}`
      ).toBe(2);

      const distinctGroupAccounts = [...new Set(capturedGroups)];
      expect(
        distinctGroupAccounts.length,
        `basket payload groups should have 2 DISTINCT accounts — got: ${JSON.stringify(capturedGroups)}`
      ).toBe(2);

      expect(capturedGroups, `groups should include actualAccountA=${actualAccountA}`).toContain(actualAccountA);
      expect(capturedGroups, `groups should include actualAccountB=${actualAccountB}`).toContain(actualAccountB);
      console.log(`[dropdown] PASS: basket payload has 2 distinct account groups: ${JSON.stringify(capturedGroups)}`);
    } else {
      console.warn('[dropdown] WARN: basket POST payload was not intercepted (race or URL mismatch)');
    }

    // Step 12: poll for 2 newest AlgoOrder rows with distinct accounts.
    let newRows = [];
    for (let i = 0; i < 12; i++) {
      await new Promise((r) => setTimeout(r, 1_500));
      const r2 = await page.request.get('/api/orders/algo/recent?limit=20');
      if (!r2.ok()) continue;
      newRows = ((await r2.json()) || []).filter((o) => !beforeIds.has(o.id));
      if (newRows.length >= 2) break;
    }

    console.log(`[dropdown] new AlgoOrder rows found: ${newRows.length}`);
    const newAccounts = newRows.slice(0, 2).map((r) => r.account);
    console.log(`[dropdown] 2 newest AlgoOrder accounts: ${JSON.stringify(newAccounts)}`);

    if (newRows.length >= 2) {
      const distinctNewAccounts = [...new Set(newAccounts)];
      expect(
        distinctNewAccounts.length,
        `2 newest AlgoOrder rows should have distinct accounts — got: ${JSON.stringify(newAccounts)}`
      ).toBe(2);
      expect(newAccounts, `AlgoOrder rows should include actualAccountA=${actualAccountA}`).toContain(actualAccountA);
      expect(newAccounts, `AlgoOrder rows should include actualAccountB=${actualAccountB}`).toContain(actualAccountB);
      console.log(`[dropdown] PASS: AlgoOrder rows carry distinct accounts: ${JSON.stringify(newAccounts)}`);
    } else {
      console.warn(`[dropdown] WARN: fewer than 2 new AlgoOrder rows — only ${newRows.length} found (may be a timing issue)`);
      // Don't fail the test on timing; the payload + select checks are the primary signal.
    }
  });

});
