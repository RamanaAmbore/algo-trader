/**
 * logpanel_console_rename_and_cmdhistory.spec.js
 *
 * 2026-10-18 item 6 — LogPanel's "Terminal" tab renamed to "Console" and
 * re-sequenced (order/agent/alerts/system/conn/console/simulator/news),
 * and `/automation`'s own Console tab (the CLI grammar text editor) now
 * wires real compile/save outcomes into LogPanel's Console tab via
 * ConsoleTab's `onResult` callback → `/automation/+page.svelte`'s
 * `_cmdHistory` state → `<ActivityLogSurface cmdHistory={_cmdHistory}>`.
 *
 * Covers:
 *   (a) LogPanel's bottom tab bar on /automation shows "Console", not
 *       "Terminal", in the new tab order.
 *   (b) Submitting a real CLI statement on automation's own Console tab
 *       (the CLI editor) — success and failure — produces a new entry
 *       visible in LogPanel's (bottom-panel) Console tab.
 *
 * Mocking: `POST /api/agents/` is intercepted via `page.route()` — same
 * convention as the sibling console_relocation_cli_scope.spec.js — so
 * this spec never creates a real row against dev.
 *
 * Viewport: chromium-desktop only (CLI authoring is not a mobile-first
 * surface — same convention as every sibling CLI spec in this file set).
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.use({ projects: ['chromium-desktop'] });

const TIMEOUT = 20_000;

/** Mocks POST /api/agents/, capturing the posted JSON body and returning
 *  either a success body or a 400 error, depending on `opts.fail`. */
async function mockAgentsPost(/** @type {import('@playwright/test').Page} */ page, /** @type {{fail?: boolean}} */ opts = {}) {
  await page.route('**/api/agents/', async (route) => {
    if (route.request().method() !== 'POST') { await route.continue(); return; }
    if (opts.fail) {
      await route.fulfill({ status: 400, contentType: 'application/json', body: JSON.stringify({ detail: 'duplicate slug' }) });
    } else {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ slug: 'zzz-e2e-cmdhistory-agent', ok: true }) });
    }
  });
}

/** Switch to LogPanel's own (bottom-panel) Console tab — scoped to
 *  `.lp-header-wrap` since the page ALSO renders a page-level "Console"
 *  tab (AutomationTabs, Console/Agents) sharing the same AlgoTabs
 *  component and tab label — without this scope a bare role-based
 *  locator would resolve ambiguously to either tab strip. */
async function openLogPanelConsoleTab(/** @type {import('@playwright/test').Page} */ page) {
  const wrap = page.locator('.lp-header-wrap').first();
  await expect(wrap, '.lp-header-wrap (LogPanel header) renders').toBeVisible({ timeout: TIMEOUT });
  const consoleTab = wrap.getByRole('tab', { name: 'Console', exact: true });
  await expect(consoleTab, 'LogPanel Console tab button visible').toBeVisible({ timeout: TIMEOUT });
  await consoleTab.click();
  return wrap;
}

test.describe('LogPanel — Console tab rename + order (on /automation)', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });
  });

  test('bottom LogPanel tab bar shows "Console", not "Terminal"', async ({ page }) => {
    const wrap = page.locator('.lp-header-wrap').first();
    await expect(wrap, '.lp-header-wrap renders').toBeVisible({ timeout: TIMEOUT });
    await expect(wrap.getByRole('tab', { name: 'Console', exact: true })).toBeVisible({ timeout: TIMEOUT });
    await expect(wrap.getByRole('tab', { name: 'Terminal', exact: true })).toHaveCount(0);
  });

  test('bottom LogPanel tab order matches order/agent/alerts/system/conn/console/simulator/news', async ({ page }) => {
    const wrap = page.locator('.lp-header-wrap').first();
    await expect(wrap, '.lp-header-wrap renders').toBeVisible({ timeout: TIMEOUT });
    // `.algo-tab` renders `text-transform: uppercase` — `innerText`
    // reflects the VISUALLY rendered (uppercased) text, not the literal
    // label string, so normalize case before comparing.
    const labels = await wrap.locator('[role="tab"]').allInnerTexts();
    const cleaned = labels.map((l) => l.trim().toUpperCase()).filter(Boolean);
    // 'Alerts' is cap-gated (view_audit) and 'Ticks'/'Simulator' is
    // executionMode-gated — both may legitimately be absent (same
    // tolerance the sibling logpanel_alerts_tab.spec.js / activity-panel
    // specs already apply). Assert the relative order of every tab that
    // IS always expected to render, rather than an exact array match.
    const expectedOrder = ['ORDERS', 'AGENTS', 'SYSTEM', 'CONN', 'CONSOLE', 'NEWS'];
    const actualFiltered = cleaned.filter((l) => expectedOrder.includes(l));
    expect(actualFiltered).toEqual(expectedOrder);
  });
});

test.describe('Automation Console (CLI editor) → LogPanel Console tab cmdHistory wiring', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('a successful CLI agent create produces a ✓ entry in LogPanel\'s Console tab', async ({ page }) => {
    await mockAgentsPost(page, { fail: false });
    await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });

    const cli = page.locator('.agent-cli-textarea');
    await expect(cli).toBeVisible({ timeout: TIMEOUT });
    await cli.fill(
      'WHEN pnl_pct@positions.total <= -5 ALERT telegram ' +
      'DO order(account="ZG0790", symbol="RELIANCE", exchange="NSE", side="SELL", qty=1) #name="ZZZ E2E CmdHistory Agent"'
    );

    const createBtn = page.locator('.console-agent-cli-btn', { hasText: 'Create agent' });
    await expect(createBtn).toBeVisible({ timeout: TIMEOUT });
    await createBtn.click();

    // Submit clears the textarea on success — proof the real submit path ran.
    await expect(cli).toHaveValue('', { timeout: TIMEOUT });

    const wrap = await openLogPanelConsoleTab(page);
    // ConsoleTab._submitAgentCliAgent sets `_cliSubmitStatus = { ok: true,
    // text: "Agent '<slug>' created" }` on success — the slug is derived
    // client-side from the #name= tag (slugifyAgentName), lowercased with
    // hyphens, not the original-case display name.
    const row = wrap.page().locator('.log-panel.log-rows .log-row', { hasText: 'zzz-e2e-cmdhistory-agent' }).first();
    await expect(row, 'a Console-tab row mentioning the created agent appears').toBeVisible({ timeout: TIMEOUT });
    // '✓' maps to the success CSS class (log-agent-success) + 'I' level chip.
    await expect(row).toHaveClass(/log-agent-success/);
  });

  test('a failed CLI agent create produces a ✗ entry in LogPanel\'s Console tab', async ({ page }) => {
    await mockAgentsPost(page, { fail: true });
    await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });

    const cli = page.locator('.agent-cli-textarea');
    await expect(cli).toBeVisible({ timeout: TIMEOUT });
    await cli.fill(
      'WHEN pnl_pct@positions.total <= -5 ALERT telegram ' +
      'DO order(account="ZG0790", symbol="RELIANCE", exchange="NSE", side="SELL", qty=1) #name="ZZZ E2E CmdHistory Fail"'
    );

    const createBtn = page.locator('.console-agent-cli-btn', { hasText: 'Create agent' });
    await expect(createBtn).toBeVisible({ timeout: TIMEOUT });
    await createBtn.click();

    // Failure keeps the text in the box (no clear-on-success behavior).
    await page.waitForTimeout(500);

    const wrap = await openLogPanelConsoleTab(page);
    // ConsoleTab._submitAgentCliAgent sets `_cliSubmitStatus = { ok: false,
    // text: 'Create failed: <error>' }` on a failed create — that `text`
    // (not the sibling toast.error's differently-worded message) is what
    // flows into cmdHistory via `onResult`.
    const row = wrap.page().locator('.log-panel.log-rows .log-row', { hasText: 'Create failed' }).first();
    await expect(row, 'a Console-tab row mentioning the failed create appears').toBeVisible({ timeout: TIMEOUT });
    await expect(row).toHaveClass(/log-agent-failed/);
  });
});
