/**
 * automation_console_unified_grammar.spec.js — grammar-unification CLI
 * wiring on Automation → Console (`/automation?tab=console`) and the
 * order-scoped CLI mounts (`/orders` tab, SymbolPanel order modal).
 *
 * Covers the six behaviours from the grammar-unification e2e pass:
 *   1. Bare unconditional statements (order / bracket / ALERT event /
 *      debug) compile and submit exactly once through their own button.
 *   2. A multi-kind preamble (debug + order + condition label) ahead of a
 *      `WHEN ... DO ...` tail compiles, and the preview JSON carries every
 *      preamble entry plus the conditional tail.
 *   3. A preamble-only statement (no WHEN/DO anywhere) is accepted as a
 *      complete statement — no "incomplete, add a WHEN/DO" error.
 *   4. `help(<name>)` shows its lookup result inline in the Console status
 *      line (`.agent-cli-submit-status`).
 *   5. A literal `GIVEN` clause is rejected with a real compile error.
 *   6. The order-scoped CLI (`/orders` and the SymbolPanel modal) still
 *      rejects `help(...)` with the Automation-scope error, and still
 *      accepts `order(...)` (positive control).
 *
 * Safety: every write route (`/api/orders/ticket`, `/api/agents/` POST,
 * `/api/admin/templates/`, `/api/agents/help`) is intercepted with
 * `page.route()` so no real order, agent, or bracket is ever persisted,
 * and the execution-mode poll is mocked to paper so no live path can be
 * reached. Assertions read the intercepted request bodies.
 *
 * Mode / catalog notes (verified against the dev catalog 2026-10-24):
 *   - `help` is seeded in `agent_grammar.yaml` on workshop but NOT in the
 *     dev DB's grammar_tokens yet (dev is behind workshop). Test 4 patches
 *     the catalog GET to append the `help` action row, and mocks the
 *     `/api/agents/help` lookup. Remove both mocks after /ddev deploys
 *     workshop to dev so test 4 runs fully end-to-end.
 *   - Desktop only (CLI authoring is not a mobile-first surface).
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 20_000;

const ORDER_RELIANCE = 'order(account="ZG0790", symbol="RELIANCE", exchange="NSE", side="SELL", qty=1, price=100)';
const CLOSE_INFY = 'close_position(account="ZG0790", symbol="INFY")';
const BRACKET = 'bracket(applies_to=buy_any, tp_pct=30, sl_pct=-20) #name="Console Unified Bracket"';
const ALERT_EVENT = 'ALERT log_tag=error, min_level=ERROR, render=error, telegram #name="ZZZ Unified Grammar"';

// Mirror of the `help` action row seeded in backend/config/grammars/agent_grammar.yaml.
// Appended to the catalog GET only while dev's grammar_tokens lacks it.
const HELP_ACTION_ROW = {
  id: 999001,
  grammar_kind: 'action',
  token_kind: 'action_type',
  token: 'help',
  value_type: 'void',
  units: null,
  description: 'Look up a token/function/operator\'s catalog description, parameter signature, and docstring — logged at DEBUG, never alert-eligible.',
  resolver: 'backend.api.algo.actions.help_lookup',
  params_schema: { name: { type: 'string', required: true } },
};

const HELP_PNL_RESPONSE = {
  action: 'help',
  status: 'logged',
  name: 'pnl',
  matches: [{
    grammar_kind: 'condition',
    token_kind: 'metric',
    token: 'pnl',
    description: 'Positions mark-to-market P&L in ₹ for the selected scope.',
    signature: 'pnl@scope',
    docstring: 'Mark-to-market P&L across the selected scope.',
  }],
};

/** Routes a path's POST to a recorder; fulfills with `reply`. Returns the
 *  live array of intercepted requests (`{ body }` per request). */
async function recordPosts(page, pathname, reply) {
  const hits = [];
  await page.route((url) => new URL(url).pathname === pathname, async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    hits.push({ body: route.request().postDataJSON() });
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(reply) });
  });
  return hits;
}

/** Paper mode for every test — the execution-mode poll is the only thing
 *  that decides LIVE vs PAPER labels and the live-confirm modal. */
async function forcePaperMode(page) {
  await page.route('**/api/admin/execution/mode', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ mode: 'paper', allowed_modes: ['paper', 'live', 'shadow', 'sim', 'replay'], branch: 'dev' }),
    });
  });
}

async function gotoConsole(page) {
  await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });
  await page.locator('textarea.agent-cli-textarea').waitFor({ timeout: TIMEOUT });
}

test.describe('Console — unified grammar CLI (bare statements, preamble, help, GIVEN)', () => {
  test.skip(({ viewport }) => viewport.width !== 1400, 'desktop-only CLI surface (chromium-desktop 1400px)');

  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await forcePaperMode(page);
  });

  // ── 1. Bare unconditional statements — compile and submit exactly once ──

  test('bare order(...) compiles to a PAPER order and submits exactly once to /api/orders/ticket', async ({ page }) => {
    const tickets = await recordPosts(page, '/api/orders/ticket', { status: 'ok', order_id: 101 });
    await gotoConsole(page);

    await page.locator('textarea.agent-cli-textarea').fill(ORDER_RELIANCE);

    const placeBtn = page.locator('.agent-cli-place-order');
    await expect(placeBtn).toBeVisible({ timeout: TIMEOUT });
    await expect(placeBtn).toContainText('PAPER');
    await placeBtn.click();

    await expect(page.locator('.agent-cli-submit-status')).toContainText('Order placed', { timeout: TIMEOUT });
    expect(tickets).toHaveLength(1);
    expect(tickets[0].body.tradingsymbol).toBe('RELIANCE');
    expect(tickets[0].body.mode).toBe('paper');
    // Success clears the textarea — the statement cannot be re-submitted by accident.
    await expect(page.locator('textarea.agent-cli-textarea')).toHaveValue('');
  });

  test('bare bracket(...) compiles to a bracket template and saves exactly once', async ({ page }) => {
    const templates = await recordPosts(page, '/api/admin/templates/', { id: 501, name: 'Console Unified Bracket' });
    await gotoConsole(page);

    await page.locator('textarea.agent-cli-textarea').fill(BRACKET);

    const saveBtn = page.locator('.console-agent-cli-btn', { hasText: 'Save bracket' });
    await expect(saveBtn).toBeVisible({ timeout: TIMEOUT });
    await saveBtn.click();

    await expect(page.locator('.agent-cli-submit-status')).toContainText("Bracket 'Console Unified Bracket' created", { timeout: TIMEOUT });
    expect(templates).toHaveLength(1);
    expect(templates[0].body.name).toBe('Console Unified Bracket');
  });

  test('bare ALERT event statement compiles to an event agent and creates exactly once', async ({ page }) => {
    const agents = await recordPosts(page, '/api/agents/', { slug: 'zzz-unified-grammar', id: 601 });
    await gotoConsole(page);

    await page.locator('textarea.agent-cli-textarea').fill(ALERT_EVENT);

    const createBtn = page.locator('.console-agent-cli-btn', { hasText: 'Create agent' });
    await expect(createBtn).toBeVisible({ timeout: TIMEOUT });
    await createBtn.click();

    await expect(page.locator('.agent-cli-submit-status')).toContainText("Agent 'zzz-unified-grammar' created", { timeout: TIMEOUT });
    expect(agents).toHaveLength(1);
    expect(agents[0].body.kind).toBe('event');
  });

  test('bare debug(<token>) with a #name tag creates exactly once', async ({ page }) => {
    const agents = await recordPosts(page, '/api/agents/', { slug: 'zzz-debug-probe', id: 602 });
    await gotoConsole(page);

    await page.locator('textarea.agent-cli-textarea').fill('debug(pnl) #name="ZZZ Debug Probe"');

    const createBtn = page.locator('.console-agent-cli-btn', { hasText: 'Create agent' });
    await expect(createBtn).toBeVisible({ timeout: TIMEOUT });
    await expect(page.locator('.agent-cli-errors')).toHaveCount(0);
    await createBtn.click();

    await expect(page.locator('.agent-cli-submit-status')).toContainText("Agent 'zzz-debug-probe' created", { timeout: TIMEOUT });
    expect(agents).toHaveLength(1);
  });

  test('bare debug(<token>) without a #name is refused on save with a visible error and no request', async ({ page }) => {
    const agents = await recordPosts(page, '/api/agents/', { slug: 'should-not-save', id: 0 });
    await gotoConsole(page);

    await page.locator('textarea.agent-cli-textarea').fill('debug(pnl)');

    const createBtn = page.locator('.console-agent-cli-btn', { hasText: 'Create agent' });
    await expect(createBtn).toBeVisible({ timeout: TIMEOUT });
    await createBtn.click();

    await expect(page.locator('.agent-cli-submit-status')).toContainText('agent requires a #name= tag', { timeout: TIMEOUT });
    await expect(page.locator('.agent-cli-submit-status')).toHaveClass(/is-error/);
    expect(agents).toHaveLength(0);
  });

  // ── 2. Multi-kind preamble ahead of a WHEN ... DO tail ──

  test('multi-kind preamble (debug + order + condition label) with a WHEN...DO tail compiles; preview carries every preamble entry and the tail', async ({ page }) => {
    await gotoConsole(page);

    const text = [
      'x = 1',
      'debug(x)',
      ORDER_RELIANCE,
      'a = pnl@positions.total',
      `WHEN a <= -1 DO ${CLOSE_INFY}`,
    ].join('\n');
    await page.locator('textarea.agent-cli-textarea').fill(text);

    // Positive signal first: the compiled agent's own create button renders.
    await expect(page.locator('.console-agent-cli-btn', { hasText: 'Create agent' })).toBeVisible({ timeout: TIMEOUT });
    await expect(page.locator('.agent-cli-errors')).toHaveCount(0);
    await expect(page.locator('.console-unsupported-hint')).toHaveCount(0);

    const preview = page.locator('pre.agent-cli-preview-json');
    await expect(preview).toContainText('"preamble"');
    // Preamble entries: the debug(x) log line AND the order(...) line.
    await expect(preview).toContainText('"type": "log"');
    await expect(preview).toContainText('"type": "order"');
    await expect(preview).toContainText('"symbol": "RELIANCE"');
    // The conditional tail: the pnl condition and the close_position action.
    await expect(preview).toContainText('"metric": "pnl"');
    await expect(preview).toContainText('"type": "close_position"');
  });

  // ── 3. Preamble-only statement — no WHEN/DO anywhere ──

  test('preamble-only statement (no WHEN/DO) is accepted as a complete statement, not "incomplete"', async ({ page }) => {
    await gotoConsole(page);

    await page.locator('textarea.agent-cli-textarea').fill('x = 1\ndebug(x)');

    await expect(page.locator('.console-agent-cli-btn', { hasText: 'Create agent' })).toBeVisible({ timeout: TIMEOUT });
    // No compile error block, no WHEN/DO demand, no unsupported hint.
    await expect(page.locator('.agent-cli-errors')).toHaveCount(0);
    await expect(page.locator('.console-unsupported-hint')).toHaveCount(0);
  });

  // ── 4. help(<name>) — inline result in the Console status line ──

  test('help(pnl) renders the catalog lookup inline in the Console result line', async ({ page }) => {
    // Dev catalog lacks the `help` action row (see the header note). Append
    // it to the catalog GET, and mock the lookup itself.
    await page.route((url) => new URL(url).pathname === '/api/admin/grammar/tokens', async (route) => {
      const response = await route.fetch();
      const rows = await response.json();
      const grammar = new URL(route.request().url()).searchParams.get('grammar');
      if (grammar === 'action' && !rows.some((r) => r.token === 'help')) rows.push(HELP_ACTION_ROW);
      await route.fulfill({ response, json: rows });
    });
    const lookups = await recordPosts(page, '/api/agents/help', HELP_PNL_RESPONSE);
    await gotoConsole(page);

    await page.locator('textarea.agent-cli-textarea').fill('help(pnl)');

    const lookBtn = page.locator('.console-agent-cli-btn', { hasText: 'Look up' });
    await expect(lookBtn).toBeVisible({ timeout: TIMEOUT });
    await lookBtn.click();

    const status = page.locator('.agent-cli-submit-status');
    await expect(status).toContainText('pnl:', { timeout: TIMEOUT });
    await expect(status).toContainText('Positions mark-to-market P&L');
    expect(lookups).toHaveLength(1);
    expect(lookups[0].body).toEqual({ name: 'pnl' });
  });

  // ── 5. GIVEN is rejected ──

  test('a literal GIVEN clause is rejected with a real compile error, never silently accepted', async ({ page }) => {
    const agents = await recordPosts(page, '/api/agents/', { slug: 'should-not-save', id: 0 });
    await gotoConsole(page);

    await page.locator('textarea.agent-cli-textarea').fill('GIVEN x = pnl_pct@positions.total WHEN x <= -5 ALERT telegram DO nop');

    const errors = page.locator('.agent-cli-errors');
    await expect(errors).toBeVisible({ timeout: TIMEOUT });
    await expect(errors).toContainText('GIVEN is no longer part of the grammar');
    // No create affordance is offered for the rejected text.
    await expect(page.locator('.console-agent-cli-btn')).toHaveCount(0);
    expect(agents).toHaveLength(0);
  });

  // ── 6. Order-scoped CLI still rejects help(...) ──

  test('/orders CLI tab accepts order(...) but rejects help(...) with the Automation-scope error', async ({ page }) => {
    await page.goto('/orders', { waitUntil: 'domcontentloaded' });
    const cliTab = page.getByRole('tab', { name: 'CLI' });
    await expect(cliTab).toBeVisible({ timeout: TIMEOUT });
    await cliTab.click();

    const cli = page.locator('.agent-cli-textarea');
    await expect(cli).toBeVisible({ timeout: TIMEOUT });

    // Positive control — the editor is live and order-scoped statements compile.
    await cli.fill(ORDER_RELIANCE);
    await expect(page.locator('.agent-cli-place-order')).toBeVisible({ timeout: TIMEOUT });

    await cli.fill('help(pnl)');
    const errors = page.locator('.agent-cli-errors');
    await expect(errors).toBeVisible({ timeout: TIMEOUT });
    await expect(errors).toContainText(/Automation.*Console/i);
    await expect(errors).not.toContainText('action not found');
    await expect(page.locator('.agent-cli-place-order')).toHaveCount(0);
    await expect(page.locator('.console-agent-cli-btn')).toHaveCount(0);
  });

  test('SymbolPanel order-modal CLI tab rejects help(...) with the same Automation-scope error', async ({ page }) => {
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });
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

    await cli.fill('help(pnl)');
    const errors = modal.locator('.agent-cli-errors');
    await expect(errors).toBeVisible({ timeout: TIMEOUT });
    await expect(errors).toContainText(/Automation.*Console/i);
    await expect(modal.locator('.agent-cli-place-order')).toHaveCount(0);
  });
});
