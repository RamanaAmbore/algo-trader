/**
 * console_unify_cli_dispatch.spec.js — Sprint I Phase 1 (2026-10-11,
 * "unify console's two CLI boxes into one window via a dispatch layer").
 *
 * Covers:
 *   1. The Console tab now mounts exactly ONE visible CLI textbox
 *      (`.agent-cli-textarea`) — the legacy `CommandLineTab`'s own input
 *      chrome (`.clt-info-strip`, its CommandBar, its +Basket/Submit/
 *      Clear buttons) is gone from the DOM entirely.
 *   2. Typing legacy-grammar-only syntax (a shell passthrough) into that
 *      ONE box shows a "Run shell command" fallback action; clicking it
 *      routes through the EXISTING `/api/admin/exec` path (mocked) and
 *      the result renders in the still-visible command-history list.
 *   3. Typing `WHEN`/`GIVEN`-led text NEVER reaches `/api/admin/exec` —
 *      regression guard for the dispatch rule's own first-token peek.
 *   4. The Sprint 12 bare-ALERT disambiguation (ordinary agent vs.
 *      event_stmt) survives the new dispatch layer unchanged — a bare
 *      `ALERT log_tag=...` statement shows the agent-family "Create
 *      agent" button, never the legacy fallback button.
 *   5. Operator-requested UI refinements (2026-10-11 follow-up):
 *      - a "CLI" label sits directly above the textbox, immediately
 *        followed by the Guide button on the SAME row — not the tab's
 *        own nav-strip label, which stays "Console".
 *      - the Guide button text no longer carries a trailing arrow.
 *      - a kind-selector (Order/Bracket/Agent/Alert) sits above the
 *        textbox, defaults to "Order", and all four options are
 *        clickable.
 *
 * Mocking: `/api/admin/exec` and the Console write endpoints are
 * intercepted via `page.route()` — same convention as the sibling
 * console_relocation_cli_scope.spec.js — so this spec never runs a real
 * shell command or creates a real row against dev.
 *
 * Viewport: chromium-desktop only (CLI authoring is not a mobile-first
 * surface — same convention as every sibling CLI spec in this file set).
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.use({ projects: ['chromium-desktop'] });

const TIMEOUT = 20_000;

test.describe('Console — single CLI box (Sprint I Phase 1)', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/automation?tab=console');
    await page.waitForLoadState('networkidle');
  });

  test('exactly one visible CLI textbox — no second (legacy) input bar in the DOM', async ({ page }) => {
    await expect(page.locator('.agent-cli-textarea')).toHaveCount(1);
    await expect(page.locator('.agent-cli-textarea')).toBeVisible({ timeout: TIMEOUT });
    // The legacy CommandLineTab's own input chrome is gone entirely —
    // `headless` removes it from the DOM (not just CSS-hidden).
    await expect(page.locator('.clt-info-strip')).toHaveCount(0);
    await expect(page.locator('.cmd-container')).toHaveCount(0);
  });

  test('legacy shell passthrough: typing a non-agent statement shows "Run shell command" and routes to /api/admin/exec', async ({ page }) => {
    let execBody = null;
    await page.route('**/api/admin/exec', async (route) => {
      execBody = route.request().postDataJSON();
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ stdout: 'hi\n', stderr: '', returncode: 0 }),
      });
    });

    const cli = page.locator('.agent-cli-textarea');
    await cli.fill('echo hi');

    const fallbackBtn = page.locator('.console-fallback-btn');
    await expect(fallbackBtn).toBeVisible({ timeout: TIMEOUT });
    await expect(fallbackBtn).toContainText('Run shell command');
    // The agent-family error block must NOT render for legacy text —
    // this grammar correctly fails to parse "echo hi", but showing a red
    // "Errors:" block for text never meant to compile here would be
    // confusing, not informative (showCompileErrors gating).
    await expect(page.locator('.agent-cli-errors')).toHaveCount(0);

    await fallbackBtn.click();

    await expect.poll(() => execBody, { timeout: TIMEOUT }).not.toBeNull();
    expect(execBody.command).toBe('echo hi');

    // Result shows in the still-visible command-history list.
    await expect(page.locator('.clt-history')).toBeVisible({ timeout: TIMEOUT });
    await expect(page.locator('.clt-history')).toContainText('echo hi');
    await expect(page.locator('.clt-history')).toContainText('hi');

    // Textbox cleared on a successful dispatch (consistent with the
    // agent-family submit functions' own convention).
    await expect(cli).toHaveValue('', { timeout: TIMEOUT });
  });

  test('WHEN...-led text never reaches /api/admin/exec (regression guard)', async ({ page }) => {
    let execHit = false;
    await page.route('**/api/admin/exec', async (route) => {
      execHit = true;
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
    });

    const cli = page.locator('.agent-cli-textarea');
    await cli.fill('WHEN pnl_pct@positions.total <= -5 ALERT telegram DO nop');
    await page.waitForTimeout(500); // let the debounced compile settle

    // Agent-family fallback button must not appear for agent-family text.
    await expect(page.locator('.console-fallback-btn')).toHaveCount(0);
    expect(execHit).toBe(false);
  });

  test('GIVEN-led text never reaches /api/admin/exec (classifyConsoleStatement fix)', async ({ page }) => {
    let execHit = false;
    await page.route('**/api/admin/exec', async (route) => {
      execHit = true;
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
    });

    const cli = page.locator('.agent-cli-textarea');
    await cli.fill('GIVEN x = pnl_pct@positions.total WHEN x <= -5 ALERT telegram DO nop');
    await page.waitForTimeout(500);

    await expect(page.locator('.console-fallback-btn')).toHaveCount(0);
    expect(execHit).toBe(false);
  });

  test('bare ALERT with event-meta kwargs still shows "Create agent", never the legacy fallback', async ({ page }) => {
    const cli = page.locator('.agent-cli-textarea');
    await cli.fill('ALERT log_tag=error, min_level=ERROR, render=error, telegram #name="ZZZ Dispatch Check"');
    await page.waitForTimeout(500);

    await expect(page.locator('.console-agent-cli-btn', { hasText: 'Create agent' })).toBeVisible({ timeout: TIMEOUT });
    await expect(page.locator('.console-fallback-btn')).toHaveCount(0);
  });
});

test.describe('Console — CLI label, Guide button, kind selector (operator follow-up, 2026-10-11)', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/automation?tab=console');
    await page.waitForLoadState('networkidle');
  });

  test('a "CLI" label sits directly above the textbox, with the Guide button on the same row', async ({ page }) => {
    const label = page.locator('.console-cli-label');
    await expect(label).toBeVisible({ timeout: TIMEOUT });
    await expect(label).toHaveText('CLI');

    const row = page.locator('.console-cli-label-row');
    await expect(row).toBeVisible();
    // Guide button lives in the SAME row as the label.
    const guideInRow = row.locator('a.console-guide-btn');
    await expect(guideInRow).toBeVisible();

    // The row sits above the textbox (no element between them carries
    // the label/guide — verified via bounding box ordering).
    const rowBox = await row.boundingBox();
    const cliBox = await page.locator('.agent-cli-textarea').boundingBox();
    expect(rowBox).not.toBeNull();
    expect(cliBox).not.toBeNull();
    expect(rowBox.y).toBeLessThan(cliBox.y);
  });

  test('the Guide button no longer carries a trailing arrow', async ({ page }) => {
    const guideBtn = page.locator('a.console-guide-btn');
    await expect(guideBtn).toBeVisible({ timeout: TIMEOUT });
    const text = (await guideBtn.textContent()) || '';
    expect(text).not.toMatch(/→|&rarr;/);
    expect(text.trim()).toBe('Guide: CLI grammar reference');
  });

  test('the Console NAV TAB itself is unaffected — still labeled "Console"', async ({ page }) => {
    // AutomationTabs.svelte's own nav-strip tab — a separate, unrelated
    // label from the body-level "CLI" heading above.
    await expect(page.getByRole('tab', { name: 'Console' })).toBeVisible({ timeout: TIMEOUT });
  });

  test('kind selector defaults to "Order" and all four options are selectable', async ({ page }) => {
    const row = page.locator('.console-kind-row');
    await expect(row).toBeVisible({ timeout: TIMEOUT });

    const orderPill = row.locator('.console-kind-pill', { hasText: 'Order' });
    const bracketPill = row.locator('.console-kind-pill', { hasText: 'Bracket' });
    const agentPill = row.locator('.console-kind-pill', { hasText: 'Agent' });
    const alertPill = row.locator('.console-kind-pill', { hasText: 'Alert' });
    for (const pill of [orderPill, bracketPill, agentPill, alertPill]) {
      await expect(pill).toBeVisible();
    }

    // Default = Order.
    await expect(orderPill).toHaveClass(/is-active/);

    // Clicking Bracket (box empty) seeds a bracket(...) starter snippet
    // and flips the active pill.
    await bracketPill.click();
    await expect(bracketPill).toHaveClass(/is-active/);
    await expect(page.locator('.agent-cli-textarea')).toHaveValue(/^bracket\(/);

    // Clicking Agent likewise seeds a WHEN... starter.
    await page.locator('.agent-cli-textarea').fill('');
    await agentPill.click();
    await expect(agentPill).toHaveClass(/is-active/);
    await expect(page.locator('.agent-cli-textarea')).toHaveValue(/^WHEN/);

    // Clicking Alert likewise seeds a bare ALERT starter.
    await page.locator('.agent-cli-textarea').fill('');
    await alertPill.click();
    await expect(alertPill).toHaveClass(/is-active/);
    await expect(page.locator('.agent-cli-textarea')).toHaveValue(/^ALERT/);
  });

  test('kind selector reflects auto-detection once typed text resolves to a kind, without overwriting it', async ({ page }) => {
    const cli = page.locator('.agent-cli-textarea');
    await cli.fill('bracket(applies_to=buy_any, tp_pct=30, sl_pct=-20) #name="X"');
    await page.waitForTimeout(500);

    const bracketPill = page.locator('.console-kind-pill', { hasText: 'Bracket' });
    await expect(bracketPill).toHaveClass(/is-active/, { timeout: TIMEOUT });
    // Text is untouched — the selector only reflects, never rewrites.
    await expect(cli).toHaveValue('bracket(applies_to=buy_any, tp_pct=30, sl_pct=-20) #name="X"');
  });
});
