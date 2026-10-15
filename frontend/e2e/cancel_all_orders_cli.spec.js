/**
 * cancel_all_orders_cli.spec.js — Sprint [cancel-all-cli] (2026-10).
 *
 * Covers the new `cancel_all_orders(...)` bare CLI statement (agents.js
 * grammar) added alongside the backend's `POST /api/orders/cancel-all`
 * route (`backend/api/routes/orders.py`, shared helper
 * `actions_live.cancel_all_orders_scoped`):
 *
 *   1. Automation Console tab (full statement scope) — PAPER mode (the
 *      real dev boot default) refuses the statement outright with a
 *      visible compile error (never a silently-disabled button faking
 *      the restriction, per compileStatement's own documented
 *      convention) and renders no submit button at all. Ctrl/Cmd+Enter
 *      in PAPER mode is proven to route through the agent-family
 *      dispatch path, never falling through to the legacy
 *      `/api/admin/exec` shell passthrough (the `_AGENT_CALL_RE` fix in
 *      consoleDispatch.js). LIVE mode compiles + submits via
 *      `POST /api/orders/cancel-all` with the exact request body both
 *      by clicking the button AND via Ctrl/Cmd+Enter — this LIVE-mode
 *      coverage depends on a real reactivity fix landed alongside this
 *      spec (ConsoleTab.svelte's `_liveModeTracked`, see that describe
 *      block's own leading comment: AgentCliEditor's `mode` prop used to
 *      be fed from an untracked store read, freezing it at whatever
 *      value `executionMode` held at mount and silently breaking every
 *      live-gated bare statement on this one host, including the
 *      pre-existing `order(...)`).
 *   2. Orders page's CLI tab (order-scoped mount, `allowedKinds`
 *      widened to include `cancel_all_orders`) — compiles + submits in
 *      LIVE mode via `POST /api/orders/cancel-all` with the exact
 *      request body the backend's `CancelAllOrdersRequest` struct
 *      expects, using the SAME submit button element AgentCliEditor's
 *      own order-placement flow uses, re-styled destructive (never a
 *      parallel button); `scope="account"` without `account` is refused
 *      with a clear, visible compile error; PAPER mode refuses the
 *      statement outright; a `WHEN...DO cancel_all_orders(...)` agent-
 *      wrapped statement is REJECTED by the scope restriction — proving
 *      the new bare-statement kind is reachable on this order-scoped
 *      mount only through its own dedicated grammar production, never
 *      through the pre-existing DO-action dispatch path.
 *   3. SymbolPanel order-ticket modal's own CLI tab (same OrderCliTab
 *      mount, different host context) — LIVE-mode submit smoke test.
 *
 * Mocking — `POST /api/orders/cancel-all` is ALWAYS mocked at the network
 * boundary (`page.route`), never hit for real: this route is genuinely
 * live-only (no PaperTradeEngine involvement) and fleet/account-wide
 * destructive against real broker accounts on dev. `GET /api/admin/
 * execution/mode` is mocked to force LIVE mode for the gate-open tests —
 * never by mutating a real backend setting, same convention
 * console_relocation_cli_scope.spec.js already established.
 *
 * Viewport: chromium-desktop only — CLI authoring is not a mobile-first
 * surface (same convention as every sibling CLI spec in this file set).
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.use({ projects: ['chromium-desktop'] });

const TIMEOUT = 20_000;

/** Mocks a write endpoint, capturing the posted JSON body into `out.body`. */
async function mockPost(/** @type {import('@playwright/test').Page} */ page, /** @type {string} */ pattern, /** @type {{body: any}} */ out, /** @type {any} */ responseBody) {
  await page.route(pattern, async (route) => {
    if (route.request().method() !== 'POST') { await route.continue(); return; }
    out.body = route.request().postDataJSON();
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(responseBody) });
  });
}

/** Forces LIVE execution mode via the same mocked endpoint the existing
 *  console_relocation_cli_scope.spec.js live-confirm test already uses —
 *  never by mutating a real backend setting. */
async function forceLiveMode(/** @type {import('@playwright/test').Page} */ page) {
  await page.route('**/api/admin/execution/mode', async (route) => {
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ mode: 'live', allowed_modes: ['paper', 'live', 'shadow', 'sim', 'replay'], branch: 'main' }),
    });
  });
}

/** Waits until the navbar's mode badge (`button.mode-trigger`) has
 *  actually picked up the mocked LIVE mode from `loadMode()`'s on-mount
 *  fetch — a fixed `waitForTimeout` raced this on a slow dev server and
 *  intermittently left the CLI compiling against a still-stale 'paper'
 *  mode. */
async function waitForLiveModeBadge(/** @type {import('@playwright/test').Page} */ page) {
  await expect(page.locator('button.mode-trigger[data-mode="live"]').first()).toBeVisible({ timeout: TIMEOUT });
}

/** Waits until AgentCliEditor's `catalog` prop has actually arrived
 *  (`fetchAgentCatalog()`'s on-mount fetch in ConsoleTab/OrderCliTab) —
 *  without this, `.fill()`-ing a statement immediately after the mode
 *  badge appears races the catalog's own async load: the compile effect
 *  bails out early on `!catalog` (no preview, no error, no submit
 *  button — the exact empty-result-area symptom this races into) since
 *  the mode badge mounts well before the catalog promise resolves. Same
 *  "race preview-or-errors-or-timeout on a harmless probe statement, then
 *  settle" pattern `goConsole()` already established in
 *  agent_cli_dropdown.spec.js, generalized to also cover OrderCliTab's/
 *  SymbolPanel's own AgentCliEditor mounts. */
async function waitForCatalogReady(/** @type {import('@playwright/test').Page} */ page) {
  const preview = page.locator('pre.agent-cli-preview-json');
  const errors = page.locator('.agent-cli-errors');
  await Promise.race([
    preview.waitFor({ timeout: 5000 }).catch(() => {}),
    errors.waitFor({ timeout: 5000 }).catch(() => {}),
    page.waitForTimeout(5000),
  ]);
}

/** Fails the test immediately if a shell/agent-authoring passthrough
 *  endpoint is ever hit — proves the dispatch-classification fix keeps
 *  this statement entirely on the cancel-all path. */
async function assertNoLegacyOrAgentCalls(/** @type {import('@playwright/test').Page} */ page) {
  let hit = null;
  await page.route('**/api/admin/exec', async (route) => { hit = 'exec'; await route.fulfill({ status: 200, body: '{}' }); });
  // POST only — a plain GET /api/agents/ list fetch is normal page
  // furniture (Agents tab row list) unrelated to this statement; only a
  // POST (agent-authoring create) would mean cancel_all_orders(...) was
  // wrongly routed through the agent-creation path.
  await page.route('**/api/agents/', async (route) => {
    if (route.request().method() === 'POST') { hit = 'agents'; await route.fulfill({ status: 200, body: '{}' }); return; }
    await route.continue();
  });
  return () => expect(hit, 'legacy/agent-authoring endpoint should never be hit for cancel_all_orders(...)').toBeNull();
}

test.describe('Automation Console tab — cancel_all_orders(...) (Sprint [cancel-all-cli])', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  // NOTE on LIVE-mode coverage for THIS mount — fixed as PART of this
  // change (see ConsoleTab.svelte's `_liveModeTracked`): AgentCliEditor's
  // `mode` prop used to be fed from `_execModeForCompile()` directly,
  // which reads `executionMode` via `safeRead()`/`untrack()` — correct
  // for a one-off synchronous read inside an event handler, but a
  // PROP-VALUE expression built ONLY from an untracked read has zero
  // tracked dependencies, so Svelte evaluated it once at mount and never
  // again. Confirmed independently of this feature via a live probe: a
  // plain pre-existing `order(...)` statement's "Place order" button
  // also stayed stuck on "(PAPER)" after the exact same mocked mode-flip
  // used below, BEFORE this fix — and correctly flips to "(LIVE)" after
  // it. Fixed by feeding the prop from `_liveModeTracked`, a real
  // `$derived($executionMode === 'live')` (Svelte's tracked auto-
  // subscribe syntax), same pattern SymbolPanel's own
  // `_sharedMode = $derived($executionMode ...)` already uses for
  // OrderCliTab. `_execModeForCompile()` itself is unchanged and still
  // correct where it's actually used — synchronous submit-time compiles
  // in `_recompileAgentCli`/`_onAgentCliOrderSubmit`.

  test('PAPER mode (the default boot state) refuses the statement outright — visible compile error, no submit button', async ({ page }) => {
    await page.goto('/automation?tab=console');

    const cli = page.locator('.agent-cli-textarea');
    await expect(cli).toBeVisible({ timeout: TIMEOUT });
    await cli.fill('cancel_all_orders(scope="total")');

    const errors = page.locator('.agent-cli-errors');
    await expect(errors).toBeVisible({ timeout: TIMEOUT });
    await expect(errors).toContainText(/live-only|not available in paper mode/i);
    await expect(page.locator('.agent-cli-cancel-all')).toHaveCount(0);
  });

  test('Ctrl+Enter in PAPER mode never falls through to the legacy /api/admin/exec shell passthrough (dispatch-classification fix)', async ({ page }) => {
    // Covers the `_AGENT_CALL_RE` fix in consoleDispatch.js without
    // depending on the separate, pre-existing LIVE-mode-reactivity gap
    // noted above: regardless of mode, `cancel_all_orders(...)` must
    // classify as 'agents' (own compile-error block visible, Ctrl+Enter
    // routed to the agent-family handler) — never 'legacy' (which would
    // show a spurious "Run shell command" button and POST the raw text
    // to /api/admin/exec on Ctrl+Enter).
    const assertClean = await assertNoLegacyOrAgentCalls(page);

    await page.goto('/automation?tab=console');

    const cli = page.locator('.agent-cli-textarea');
    await expect(cli).toBeVisible({ timeout: TIMEOUT });
    await cli.fill('cancel_all_orders(scope="total")');

    // The real compile error (live-only gate, PAPER mode) stays visible —
    // never silently swapped for emptiness or a legacy-fallback button.
    const errors = page.locator('.agent-cli-errors');
    await expect(errors).toBeVisible({ timeout: TIMEOUT });
    await expect(errors).toContainText(/live-only|not available in paper mode/i);
    await expect(page.locator('.console-fallback-btn', { hasText: 'Run shell command' })).toHaveCount(0);

    await cli.press('Control+Enter');
    // Nothing should fire at all (compiled.ok is false) — give any
    // wrongly-routed async call a moment to land before asserting clean.
    await page.waitForTimeout(500);
    assertClean();
  });

  test('LIVE mode: compiles and submits via POST /api/orders/cancel-all with the exact request body', async ({ page }) => {
    await forceLiveMode(page);
    const captured = { body: null };
    await mockPost(page, '**/api/orders/cancel-all', captured, { cancelled: 3, errors: 0, scope: 'account', account: 'ZG0790', symbol: null, side: null, detail: 'Cancel-all requested' });
    const assertClean = await assertNoLegacyOrAgentCalls(page);

    await page.goto('/automation?tab=console');
    await waitForLiveModeBadge(page);
    await waitForCatalogReady(page);

    const cli = page.locator('.agent-cli-textarea');
    await expect(cli).toBeVisible({ timeout: TIMEOUT });
    await cli.fill('cancel_all_orders(scope="account", account="ZG0790")');

    const cancelBtn = page.locator('.agent-cli-cancel-all');
    await expect(cancelBtn).toBeVisible({ timeout: TIMEOUT });
    await expect(cancelBtn).toContainText('Cancel orders (LIVE)');
    // Same shared button element, re-styled — never a second parallel
    // "Place order" button alongside it.
    await expect(page.locator('.agent-cli-place-order')).toHaveCount(1);
    await expect(page.locator('.btn-primary.agent-cli-cancel-all')).toHaveCount(0);

    await cancelBtn.click();

    await expect.poll(() => captured.body, { timeout: TIMEOUT }).not.toBeNull();
    expect(captured.body).toEqual({ scope: 'account', account: 'ZG0790' });

    const status = page.locator('.agent-cli-submit-status');
    await expect(status).toBeVisible({ timeout: TIMEOUT });
    await expect(status).toContainText('Cancel requested');
    await expect(status).not.toContainText('Cancelled');

    assertClean();
  });

  test('LIVE mode: Ctrl+Enter actually submits the same way as clicking the button', async ({ page }) => {
    await forceLiveMode(page);
    const captured = { body: null };
    await mockPost(page, '**/api/orders/cancel-all', captured, { cancelled: 0, errors: 0, scope: 'total', detail: 'Cancel-all requested' });
    const assertClean = await assertNoLegacyOrAgentCalls(page);

    await page.goto('/automation?tab=console');
    await waitForLiveModeBadge(page);
    await waitForCatalogReady(page);

    const cli = page.locator('.agent-cli-textarea');
    await expect(cli).toBeVisible({ timeout: TIMEOUT });
    await cli.fill('cancel_all_orders(scope="total")');
    await expect(page.locator('.agent-cli-cancel-all')).toBeVisible({ timeout: TIMEOUT });

    await cli.press('Control+Enter');

    await expect.poll(() => captured.body, { timeout: TIMEOUT }).not.toBeNull();
    expect(captured.body).toEqual({ scope: 'total' });
    assertClean();
  });
});

test.describe('Orders CLI tab — order-scoped mount widened to cancel_all_orders (Sprint [cancel-all-cli])', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('cancel_all_orders(...) submits (LIVE) via the same /api/orders/cancel-all path', async ({ page }) => {
    await forceLiveMode(page);
    const captured = { body: null };
    await mockPost(page, '**/api/orders/cancel-all', captured, { cancelled: 2, errors: 1, scope: 'account', account: 'ZG0790', symbol: 'RELIANCE', side: 'SELL', detail: 'Cancel-all requested' });

    await page.goto('/orders');
    await waitForLiveModeBadge(page);
    await waitForCatalogReady(page);

    const cliTab = page.getByRole('tab', { name: 'CLI' });
    await expect(cliTab).toBeVisible({ timeout: TIMEOUT });
    await cliTab.click();

    const cli = page.locator('.agent-cli-textarea');
    await expect(cli).toBeVisible({ timeout: TIMEOUT });
    await cli.fill('cancel_all_orders(scope="account", account="ZG0790", symbol="RELIANCE", side="SELL")');

    const cancelBtn = page.locator('.agent-cli-cancel-all');
    await expect(cancelBtn).toBeVisible({ timeout: TIMEOUT });
    await expect(cancelBtn).toContainText('Cancel orders (LIVE)');
    await cancelBtn.click();

    await expect.poll(() => captured.body, { timeout: TIMEOUT }).not.toBeNull();
    expect(captured.body).toEqual({ scope: 'account', account: 'ZG0790', symbol: 'RELIANCE', side: 'SELL' });

    const status = page.locator('.agent-cli-submit-status');
    await expect(status).toBeVisible({ timeout: TIMEOUT });
    // 1 error in the mocked response renders the status as an error —
    // "Cancel requested" is never shown as an unqualified success when
    // the backend itself reports partial failures.
    await expect(status).toHaveClass(/is-error/);
    await expect(status).toContainText('Cancel requested');
  });

  test('scope="account" without account is refused with a clear, visible compile error (LIVE mode)', async ({ page }) => {
    await forceLiveMode(page);
    await page.goto('/orders');
    await waitForLiveModeBadge(page);
    await waitForCatalogReady(page);

    const cliTab = page.getByRole('tab', { name: 'CLI' });
    await expect(cliTab).toBeVisible({ timeout: TIMEOUT });
    await cliTab.click();

    const cli = page.locator('.agent-cli-textarea');
    await expect(cli).toBeVisible({ timeout: TIMEOUT });
    await cli.fill('cancel_all_orders(scope="account")');

    const errors = page.locator('.agent-cli-errors');
    await expect(errors).toBeVisible({ timeout: TIMEOUT });
    await expect(errors).toContainText(/account is required when scope=account/i);
    await expect(page.locator('.agent-cli-cancel-all')).toHaveCount(0);
  });

  test('WHEN...DO cancel_all_orders(...) is rejected with the scope error — not reachable via the DO-action path on this mount', async ({ page }) => {
    await page.goto('/orders');

    const cliTab = page.getByRole('tab', { name: 'CLI' });
    await expect(cliTab).toBeVisible({ timeout: TIMEOUT });
    await cliTab.click();

    const cli = page.locator('.agent-cli-textarea');
    await expect(cli).toBeVisible({ timeout: TIMEOUT });
    await cli.fill(
      'WHEN pnl_pct@positions.total <= -5 ALERT telegram ' +
      'DO cancel_all_orders(scope="total") #name="Should Not Save"'
    );

    const errors = page.locator('.agent-cli-errors');
    await expect(errors).toBeVisible({ timeout: TIMEOUT });
    await expect(errors).toContainText(/Automation.*Console/i);
    await expect(page.locator('.agent-cli-cancel-all')).toHaveCount(0);
    await expect(page.locator('.console-agent-cli-btn')).toHaveCount(0);
  });

  test('PAPER mode (default) refuses cancel_all_orders(...) on this mount too', async ({ page }) => {
    await page.goto('/orders');

    const cliTab = page.getByRole('tab', { name: 'CLI' });
    await expect(cliTab).toBeVisible({ timeout: TIMEOUT });
    await cliTab.click();

    const cli = page.locator('.agent-cli-textarea');
    await expect(cli).toBeVisible({ timeout: TIMEOUT });
    await cli.fill('cancel_all_orders(scope="total")');

    const errors = page.locator('.agent-cli-errors');
    await expect(errors).toBeVisible({ timeout: TIMEOUT });
    await expect(errors).toContainText(/live-only|not available in paper mode/i);
    await expect(page.locator('.agent-cli-cancel-all')).toHaveCount(0);
  });
});

test.describe('SymbolPanel order-ticket modal CLI tab — cancel_all_orders(...) smoke (Sprint [cancel-all-cli])', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('cancel_all_orders(...) submits (LIVE) inside the modal context too', async ({ page }) => {
    await forceLiveMode(page);
    const captured = { body: null };
    await mockPost(page, '**/api/orders/cancel-all', captured, { cancelled: 1, errors: 0, scope: 'total', detail: 'Cancel-all requested' });

    await page.goto('/dashboard');
    await waitForLiveModeBadge(page);
    await waitForCatalogReady(page);

    const orderBtn = page.locator('button.pha-order').first();
    await expect(orderBtn).toBeVisible({ timeout: TIMEOUT });
    await orderBtn.click();

    const modal = page.locator('.oes-modal').first();
    await expect(modal).toBeVisible({ timeout: TIMEOUT });

    const cliTab = modal.getByRole('tab', { name: 'CLI' });
    await expect(cliTab).toBeVisible({ timeout: TIMEOUT });
    await cliTab.click();

    const cli = modal.locator('.agent-cli-textarea');
    await expect(cli).toBeVisible({ timeout: TIMEOUT });
    await cli.fill('cancel_all_orders(scope="total")');

    const cancelBtn = modal.locator('.agent-cli-cancel-all');
    await expect(cancelBtn).toBeVisible({ timeout: TIMEOUT });
    await cancelBtn.click();

    await expect.poll(() => captured.body, { timeout: TIMEOUT }).not.toBeNull();
    expect(captured.body).toEqual({ scope: 'total' });
  });
});
