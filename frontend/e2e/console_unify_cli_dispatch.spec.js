/**
 * console_unify_cli_dispatch.spec.js — Console's single CLI textbox.
 *
 * Covers:
 *   1. The Console tab mounts exactly ONE visible CLI textbox
 *      (`.agent-cli-textarea`) — no second input bar exists in the DOM.
 *   2. Typing text that doesn't classify as agent-family syntax shows an
 *      inline "Unsupported syntax" hint and never calls any API — the
 *      legacy positional orders.js grammar, CommandBar.svelte and
 *      CommandLineTab.svelte (and their `/api/admin/exec` shell
 *      passthrough / `agent ...` interpret fallback) were deleted
 *      entirely (2026-10-17, operator-confirmed removal — see the
 *      2026-10-17 entry in docs/specs/AUTOMATION_SPEC.md's change log).
 *   3. Typing `WHEN`/`GIVEN`-led text NEVER reaches `/api/admin/exec` or
 *      `/api/orders/ticket` — regression guard for the dispatch rule's
 *      own first-token peek.
 *   4. The Sprint 12 bare-ALERT disambiguation (ordinary agent vs.
 *      event_stmt) survives unchanged — a bare `ALERT log_tag=...`
 *      statement shows the agent-family "Create agent" button, never
 *      the (now-removed) legacy fallback button.
 *   5. Operator-requested UI refinements (2026-10-11 follow-up):
 *      - a "CLI" label sits directly above the textbox, immediately
 *        followed by the Guide button on the SAME row — not the tab's
 *        own nav-strip label, which stays "Console".
 *      - the Guide button text no longer carries a trailing arrow.
 *      - a kind-selector (Order/Bracket/Agent/Alert) sits above the
 *        textbox, defaults to "Order", and all four options are
 *        clickable.
 *
 * Mocking: `/api/admin/exec` and `/api/orders/ticket` are intercepted
 * via `page.route()` — same convention as the sibling
 * console_relocation_cli_scope.spec.js — so this spec never runs a real
 * shell command or places a real order against dev.
 *
 * Viewport: chromium-desktop only (CLI authoring is not a mobile-first
 * surface — same convention as every sibling CLI spec in this file set).
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.use({ projects: ['chromium-desktop'] });

const TIMEOUT = 20_000;

/** Bug fix (2026-10) — both `beforeEach` blocks in this file used
 *  `page.waitForLoadState('networkidle')`, which never reliably resolves
 *  on this page (the Automation page carries persistent background
 *  polling/WebSocket activity, so "idle" network is rarely reached) —
 *  confirmed by ALL tests in this file (including ones unrelated to any
 *  2026-10 change) failing on the exact same `beforeEach` timeout.
 *  Replaced with the proven wait pattern already used successfully by
 *  the sibling `agent_cli_dropdown.spec.js`/`cli_dropdown_layout_shift
 *  .spec.js` specs: wait for the textarea, then wait for the catalog's
 *  own compile effect to have run at least once (proof via EITHER the
 *  preview or an error message appearing), falling back to a bounded
 *  timeout rather than blocking indefinitely either way. */
async function goConsole(page) {
  await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });
  const textarea = page.locator('textarea.agent-cli-textarea');
  await textarea.waitFor({ timeout: TIMEOUT });
  const preview = page.locator('pre.agent-cli-preview-json');
  const errors = page.locator('.agent-cli-errors');
  await Promise.race([
    preview.waitFor({ timeout: 5_000 }).catch(() => {}),
    errors.waitFor({ timeout: 5_000 }).catch(() => {}),
    page.waitForTimeout(5_000),
  ]);
}

test.describe('Console — single CLI box', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await goConsole(page);
  });

  test('exactly one visible CLI textbox — no second (legacy) input bar in the DOM', async ({ page }) => {
    await expect(page.locator('.agent-cli-textarea')).toHaveCount(1);
    await expect(page.locator('.agent-cli-textarea')).toBeVisible({ timeout: TIMEOUT });
    // The legacy CommandLineTab host (and its CommandBar) were deleted
    // entirely (2026-10-17) — there is no second input chrome to assert
    // against; its own classes never render anywhere in the DOM.
    await expect(page.locator('.clt-info-strip')).toHaveCount(0);
    await expect(page.locator('.cmd-container')).toHaveCount(0);
  });

  test('unsupported syntax: typing a non-agent statement shows an inline hint, never calls /api/admin/exec or /api/orders/ticket', async ({ page }) => {
    let execHit = false;
    let ticketHit = false;
    await page.route('**/api/admin/exec', async (route) => {
      execHit = true;
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
    });
    await page.route('**/api/orders/ticket', async (route) => {
      ticketHit = true;
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
    });

    const cli = page.locator('.agent-cli-textarea');
    await cli.fill('echo hi');
    await page.waitForTimeout(500); // let the debounced compile settle

    const hint = page.locator('.console-unsupported-hint');
    await expect(hint).toBeVisible({ timeout: TIMEOUT });
    await expect(hint).toContainText('Unsupported syntax');
    // The legacy fallback button no longer exists anywhere.
    await expect(page.locator('.console-fallback-btn')).toHaveCount(0);
    // The agent-family error block must NOT render for unsupported text —
    // this grammar correctly fails to parse "echo hi", but showing a red
    // "Errors:" block for text never meant to compile here would be
    // confusing, not informative (showCompileErrors gating).
    await expect(page.locator('.agent-cli-errors')).toHaveCount(0);

    // Ctrl+Enter is the keyboard equivalent of the (now-absent) action
    // button — must still never reach any API.
    await cli.press('Control+Enter');
    await page.waitForTimeout(300);
    expect(execHit).toBe(false);
    expect(ticketHit).toBe(false);
  });

  test('the retired positional buy/sell grammar never reaches /api/admin/exec or /api/orders/ticket', async ({ page }) => {
    let execHit = false;
    let ticketHit = false;
    await page.route('**/api/admin/exec', async (route) => {
      execHit = true;
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
    });
    await page.route('**/api/orders/ticket', async (route) => {
      ticketHit = true;
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
    });

    const cli = page.locator('.agent-cli-textarea');
    await cli.fill('buy ZG0790 EQ INFY 1 MARKET');
    await page.waitForTimeout(500);

    await expect(page.locator('.console-unsupported-hint')).toBeVisible({ timeout: TIMEOUT });
    await cli.press('Control+Enter');
    await page.waitForTimeout(300);
    expect(execHit).toBe(false);
    expect(ticketHit).toBe(false);
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

    // No unsupported hint and no legacy fallback button for agent-family text.
    await expect(page.locator('.console-unsupported-hint')).toHaveCount(0);
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

    await expect(page.locator('.console-unsupported-hint')).toHaveCount(0);
    expect(execHit).toBe(false);
  });

  test('bare ALERT with event-meta kwargs still shows "Create agent", never the unsupported hint', async ({ page }) => {
    const cli = page.locator('.agent-cli-textarea');
    await cli.fill('ALERT log_tag=error, min_level=ERROR, render=error, telegram #name="ZZZ Dispatch Check"');
    await page.waitForTimeout(500);

    await expect(page.locator('.console-agent-cli-btn', { hasText: 'Create agent' })).toBeVisible({ timeout: TIMEOUT });
    await expect(page.locator('.console-fallback-btn')).toHaveCount(0);
  });
});

test.describe('Console — Guide link + kind-selector dropdown, relocated to the tab row (2026-10)', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await goConsole(page);
  });

  // The "CLI" label row + statement-kind PILLS row (both originally inside
  // ConsoleTab.svelte) are retired entirely — relocated to a Select.svelte
  // DROPDOWN sharing the page-level Console/Agents tab-strip CardHeader
  // row (see `/automation/+page.svelte`). The "CLI" label itself was
  // dropped (the dropdown's own aria-label + the tab strip already
  // identify the area) — only the Guide link and the kind-selector
  // survived the relocation.

  test('the Guide button lives on the Console/Agents tab row, above the textbox, no trailing arrow', async ({ page }) => {
    const guideBtn = page.locator('a.console-guide-btn');
    await expect(guideBtn).toBeVisible({ timeout: TIMEOUT });
    const text = (await guideBtn.textContent()) || '';
    expect(text).not.toMatch(/→|&rarr;/);
    // Renamed "Guide: CLI grammar reference" -> "Trader Guide" (2026-10
    // operator request).
    expect(text.trim()).toBe('Trader Guide');

    const guideBox = await guideBtn.boundingBox();
    const cliBox = await page.locator('.agent-cli-textarea').boundingBox();
    expect(guideBox).not.toBeNull();
    expect(cliBox).not.toBeNull();
    expect(guideBox.y).toBeLessThan(cliBox.y);
  });

  test('the Console NAV TAB itself is unaffected — still labeled "Console"', async ({ page }) => {
    // AutomationTabs.svelte's own nav-strip tab — a separate, unrelated
    // control from the relocated Guide link/dropdown sharing its row.
    await expect(page.getByRole('tab', { name: 'Console' })).toBeVisible({ timeout: TIMEOUT });
  });

  test('kind-selector dropdown defaults to "Order" and all four options are pickable', async ({ page }) => {
    // Select.svelte's trigger text is "<label> ▾" (a trailing caret glyph,
    // .rbq-select-caret) — `toContainText` rather than an exact `toHaveText`
    // match, so the assertion isn't coupled to that decorative character.
    const trigger = page.getByRole('button', { name: 'Statement kind' });
    await expect(trigger).toBeVisible({ timeout: TIMEOUT });
    await expect(trigger).toContainText('Order');

    // Picking Bracket (box empty) seeds a bracket(...) starter snippet.
    await trigger.click();
    await page.getByRole('option', { name: 'Bracket' }).click();
    await expect(trigger).toContainText('Bracket');
    await expect(page.locator('.agent-cli-textarea')).toHaveValue(/^bracket\(/);

    // Picking Agent likewise seeds a WHEN... starter.
    await page.locator('.agent-cli-textarea').fill('');
    await trigger.click();
    await page.getByRole('option', { name: 'Agent' }).click();
    await expect(trigger).toContainText('Agent');
    await expect(page.locator('.agent-cli-textarea')).toHaveValue(/^WHEN/);

    // Picking Alert likewise seeds a bare ALERT starter.
    await page.locator('.agent-cli-textarea').fill('');
    await trigger.click();
    await page.getByRole('option', { name: 'Alert' }).click();
    await expect(trigger).toContainText('Alert');
    await expect(page.locator('.agent-cli-textarea')).toHaveValue(/^ALERT/);
  });

  test('kind-selector reflects auto-detection once typed text resolves to a kind, without overwriting it', async ({ page }) => {
    const cli = page.locator('.agent-cli-textarea');
    await cli.fill('bracket(applies_to=buy_any, tp_pct=30, sl_pct=-20) #name="X"');
    await page.waitForTimeout(500);

    const trigger = page.getByRole('button', { name: 'Statement kind' });
    await expect(trigger).toContainText('Bracket', { timeout: TIMEOUT });
    // Text is untouched — the selector only reflects, never rewrites.
    await expect(cli).toHaveValue('bracket(applies_to=buy_any, tp_pct=30, sl_pct=-20) #name="X"');
  });

  test('the kind-selector dropdown and Guide link are absent on the Agents tab', async ({ page }) => {
    await page.goto('/automation?tab=agents', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('a.console-guide-btn')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Statement kind' })).toHaveCount(0);
  });
});
