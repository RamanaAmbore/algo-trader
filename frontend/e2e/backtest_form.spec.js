/**
 * Smoke spec: Backtest (Replay) panel grid-form structure on prod.
 *
 * Verifies the post-15af2e3 rewrite of ReplayPanel:
 *  - .bt-form grid container is visible
 *  - #bt-run-name auto-generated value (backtest-HHMMSS pattern)
 *  - #bt-symbols MultiSelect present
 *  - #bt-from and #bt-to date inputs present
 *  - #bt-rate stepper value is a number
 *  - #bt-agents MultiSelect present
 *  - "Start Backtest" button visible
 *
 * Target: prod (ramboq.com)
 *   TOK=$(curl -s -X POST https://ramboq.com/api/auth/login \
 *     -H 'Content-Type: application/json' \
 *     -d '{"username":"rambo","password":"admin1234"}' \
 *     | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')
 *   cd /Users/ramanambore/projects/ramboq/frontend
 *   BASE_URL=https://ramboq.com PLAYWRIGHT_AUTH_TOKEN="$TOK" \
 *   npx playwright test backtest_form.spec.js --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';

// ── auth (one API call per process; bypass PLAYWRIGHT_AUTH_TOKEN when set) ──────

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
      if (resp.ok()) {
        tok = (await resp.json()).access_token;
        break;
      }
      if (resp.status() !== 429) {
        throw new Error(`authOnce: /api/auth/login returned ${resp.status()}`);
      }
    }
    if (!tok) {
      test.skip(true, 'rate-limited — run in isolation or pass PLAYWRIGHT_AUTH_TOKEN');
      return;
    }
    _cachedToken = tok;
  }

  // Plant token directly into sessionStorage — avoids an extra /signin
  // round-trip that would count against prod's 5/min rate limit.
  await page.goto('/');
  await page.evaluate((tok) => {
    sessionStorage.setItem('ramboq_token', tok);
    sessionStorage.setItem('ramboq_user', JSON.stringify({
      user_id: 'rambo', username: 'rambo', role: 'admin', display_name: 'rambo',
    }));
  }, _cachedToken);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${_cachedToken}` });
}

// ── spec ─────────────────────────────────────────────────────────────────────

test.describe('/admin/execution — Backtest panel', () => {

  test('Backtest form grid structure renders correctly', async ({ page }) => {
    await authOnce(page);

    // Navigate directly to the Replay tab via query-string shortcut.
    await page.goto('/admin/execution?tab=replay');
    await page.waitForLoadState('domcontentloaded');

    // 1. Wait for the Backtest tab subtitle "historical data" to confirm
    //    the Replay tab is active and ReplayPanel has mounted.
    const activeTab = page.locator('.exec-tab-active');
    await expect(activeTab).toBeVisible({ timeout: 10_000 });
    const subtitle = activeTab.locator('.exec-tab-subtitle');
    await expect(subtitle).toContainText('historical data', { timeout: 8_000 });

    // 2. .bt-form grid container visible.
    const btForm = page.locator('.bt-form');
    await expect(btForm).toBeVisible({ timeout: 8_000 });

    // 3. #bt-run-name is visible and has an auto-generated non-empty value.
    const runName = page.locator('#bt-run-name');
    await expect(runName).toBeVisible();
    const runNameValue = await runName.inputValue();
    expect(runNameValue.trim().length, '#bt-run-name value is empty').toBeGreaterThan(0);
    // Pattern: backtest-HHMMSS (6 digits after the dash).
    expect(runNameValue, `unexpected run-name format: "${runNameValue}"`).toMatch(/^backtest-\d{6}$/);
    console.log(`[backtest_form] auto-generated run name: "${runNameValue}"`);

    // 4. #bt-symbols MultiSelect is present (the MultiSelect component
    //    renders a wrapper div with the given id, not a native <select>).
    const btSymbols = page.locator('#bt-symbols');
    await expect(btSymbols).toBeVisible();

    // 5. #bt-from and #bt-to date inputs are present.
    await expect(page.locator('#bt-from')).toBeVisible();
    await expect(page.locator('#bt-to')).toBeVisible();

    // 6. #bt-rate stepper value is a number (the stepper renders a
    //    <span id="bt-rate"> — not an <input> — showing the integer ms value).
    const rateSpan = page.locator('#bt-rate');
    await expect(rateSpan).toBeVisible();
    const rateText = (await rateSpan.textContent() || '').trim();
    const rateNum = Number(rateText);
    expect(
      Number.isFinite(rateNum) && rateNum > 0,
      `#bt-rate text "${rateText}" is not a positive number`,
    ).toBeTruthy();
    console.log(`[backtest_form] #bt-rate stepper value: ${rateNum} ms`);

    // 7. #bt-agents MultiSelect is present.
    const btAgents = page.locator('#bt-agents');
    await expect(btAgents).toBeVisible();

    // 8. "Start Backtest" button is visible (may be disabled while
    //    symbols/dates are unpopulated — only visibility is checked here).
    const startBtn = page.getByRole('button', { name: /start backtest/i });
    await expect(startBtn).toBeVisible();

    console.log('[backtest_form] all 8 assertions passed');
  });
});
