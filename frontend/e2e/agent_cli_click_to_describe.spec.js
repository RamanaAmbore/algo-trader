/**
 * agent_cli_click_to_describe.spec.js
 *
 * End-to-end coverage for "click an already-typed token to see its
 * description" (.claude/PLAN.md, "Click-to-describe already-entered CLI
 * tokens") — distinct from the existing live-autocomplete dropdown-hover
 * description (`agent_cli_editor_suggestion_hover_description.spec.js`,
 * which only describes a candidate while it's still being typed).
 *
 * Covers (per the plan's own Tests section):
 *   (a) click-to-describe on a committed catalog token in each of
 *       WHEN/ALERT/DO/GIVEN clauses
 *   (b) click-to-describe on a GIVEN-bound label reference
 *   (c) click on a call param key (open AND closed call)
 *   (d) dropdown-wins-over-describe precedence
 *   (e) Escape dismisses the describe panel without closing the host
 *       order-ticket modal (SymbolPanel)
 *   (f) panel never overlaps the Place-order/Cancel-all buttons
 *   (g) regression smoke for the existing hover-dropdown-description
 *       mechanism (current selectors — see note below)
 *   (h) the JSON-preview-card click mechanism on the Automation page
 *
 * Reaches AgentCliEditor via `/automation?tab=console` (full statement
 * scope), mirroring `agent_cli_dropdown.spec.js`'s own `goConsole()`
 * helper — same mount, same catalog-ready wait strategy.
 *
 * Note on (g): the dedicated hover-description spec
 * (`agent_cli_editor_suggestion_hover_description.spec.js`) asserts
 * against `.agent-cli-suggest-pill`, a class that no longer exists in
 * the current component (renamed to `.agent-cli-dropdown-row` by a later
 * "Sprint [CLI dropdown]" sprint) — that drift predates this task and is
 * unrelated to it (confirmed via `grep` against the live component
 * source before writing this file). This spec's own (g) test below
 * exercises the SAME underlying mechanism (`_hoverLabel`/`_hoverDesc`,
 * `.agent-cli-suggest-desc`) against the CURRENT selectors, so the
 * mechanism itself is verified live even though the older dedicated spec
 * file needs its own, separate selector refresh.
 *
 * Viewport: chromium-desktop only (CLI authoring is not a mobile-first
 * surface — matches every other AgentCliEditor-adjacent spec).
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.use({ projects: ['chromium-desktop'] });

const TIMEOUT = 20_000;

/** Navigate to /automation?tab=console and wait for the AgentCliEditor's
 *  catalog to be ready. Waits on the THREE real `fetchAgentCatalog()`
 *  network responses directly (condition/notify/action) rather than
 *  racing the preview/error DOM nodes — confirmed via live debug probe
 *  (2026-10-17) that the preview/error race is unreliable here: on a
 *  genuinely EMPTY textarea `compiled` stays `null` (the component skips
 *  compiling empty text entirely), so neither node ever appears and the
 *  race always falls through to its 5s timeout — which happened to be
 *  "long enough" in practice, but a direct response-wait is the actual
 *  correct signal, not a timing coincidence. */
async function goConsole(page) {
  const waits = ['condition', 'notify', 'action'].map((g) =>
    page.waitForResponse((r) => r.url().includes(`/api/admin/grammar/tokens?grammar=${g}`), { timeout: TIMEOUT }));
  await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });
  const textarea = page.locator('textarea.agent-cli-textarea');
  await textarea.waitFor({ timeout: TIMEOUT });
  await Promise.all(waits);
  await textarea.focus();
  return textarea;
}

/** Sets the textarea's real `value` + fires a real `input` event (so the
 *  component's own `oninput`/compile-debounce effect runs), WITHOUT the
 *  per-character `pressSequentially` cost — click-to-describe itself
 *  doesn't depend on incremental typing the way live-suggest does, only
 *  on the final text + an explicit click afterward. */
async function setStatement(page, textarea, text) {
  await textarea.evaluate((el, value) => {
    el.value = value;
    el.dispatchEvent(new Event('input', { bubbles: true }));
  }, text);
  // Debounced compile (150ms) — give it time to settle before any
  // assertion that reads `compiled`/`canonicalLines`.
  await page.waitForTimeout(250);
}

/** Clicks inside the textarea at a position INSIDE the Nth (0-indexed)
 *  whole-word occurrence of `token` in its current value — word-boundary
 *  matched (so clicking "p" never lands inside "pnl"/"positions", and
 *  clicking "order" never lands inside "DO order" at a position still
 *  inside "DO") — sets `selectionStart`/`selectionEnd` directly then
 *  dispatches a real `click` event, so the component's own `onclick`
 *  handler (which reads `textareaEl.selectionStart` via `_updateCursor()`)
 *  sees EXACTLY this position, without needing pixel-perfect mouse
 *  coordinates inside a `<textarea>` (which has no per-character
 *  hit-testing API). Lands in the MIDDLE of the token (or at offset 0 for
 *  a 1-character token) — never right at its start/end boundary, which
 *  `describeTokenAt`'s own half-open `[start, end)` covering-token check
 *  could otherwise attribute to a neighboring token/punctuation instead. */
async function clickTokenAt(page, textarea, token, occurrence = 0) {
  await textarea.evaluate((el, { token, occurrence }) => {
    const esc = token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const re = new RegExp(`(?<![A-Za-z0-9_])${esc}(?![A-Za-z0-9_])`, 'g');
    const matches = [...el.value.matchAll(re)];
    const m = matches[occurrence];
    if (!m) throw new Error(`clickTokenAt: token "${token}" occurrence ${occurrence} not found in "${el.value}"`);
    const mid = m.index + Math.max(0, Math.floor(token.length / 2));
    el.focus();
    el.selectionStart = el.selectionEnd = mid;
    el.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  }, { token, occurrence });
}

const describePanel = (page) => page.locator('[data-testid="agent-cli-describe-panel"]');

test.describe('AgentCliEditor — click-to-describe already-typed tokens', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  // ── (a) clauseKind coverage ──────────────────────────────────────────
  test('clicking a catalog token in WHEN/ALERT/DO/GIVEN each shows a description panel', async ({ page }) => {
    const textarea = await goConsole(page);
    const statement =
      'GIVEN p = pnl@positions.total\n' +
      'WHEN p <= -50000\n' +
      'ALERT telegram\n' +
      'DO order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1)';
    await setStatement(page, textarea, statement);

    // GIVEN clause — click the "pnl" metric on the GIVEN line.
    await clickTokenAt(page, textarea, 'pnl');
    await expect(describePanel(page)).toBeVisible({ timeout: TIMEOUT });
    await expect(describePanel(page)).toContainText('pnl');

    // WHEN clause — click the bound label "p" (a binding, still a
    // describable token — see the dedicated (b) test below for the
    // binding-specific assertion). Occurrence 1 — "p" also appears as the
    // GIVEN line's own defining occurrence (occurrence 0), which must NOT
    // be clicked here (clicking a label's OWN definition site never
    // resolves as a binding-reference — see describeTokenAt.test.js).
    await clickTokenAt(page, textarea, 'p', 1);
    await expect(describePanel(page)).toBeVisible({ timeout: TIMEOUT });

    // ALERT clause — click "telegram".
    await clickTokenAt(page, textarea, 'telegram');
    await expect(describePanel(page)).toBeVisible({ timeout: TIMEOUT });
    await expect(describePanel(page)).toContainText('telegram');

    // DO clause — click "order" (the action name itself, not inside its
    // parens).
    await clickTokenAt(page, textarea, 'order');
    await expect(describePanel(page)).toBeVisible({ timeout: TIMEOUT });
  });

  // ── (b) GIVEN-bound label ────────────────────────────────────────────
  test('clicking a GIVEN-bound label used in WHEN describes the binding, not a catalog miss', async ({ page }) => {
    const textarea = await goConsole(page);
    await setStatement(page, textarea,
      'GIVEN myloss = pnl@positions.total\nWHEN myloss <= -50000\nALERT telegram\nDO nop');

    // Occurrence 1 — the WHEN clause's USE of the label, not the GIVEN
    // clause's own defining occurrence (0).
    await clickTokenAt(page, textarea, 'myloss', 1);
    const panel = describePanel(page);
    await expect(panel).toBeVisible({ timeout: TIMEOUT });
    await expect(panel).toContainText('myloss');
    await expect(panel).toContainText('Bound via GIVEN');
  });

  // ── (c) call param key — open and closed ─────────────────────────────
  test('clicking a param key inside a CLOSED call shows that param\'s description', async ({ page }) => {
    const textarea = await goConsole(page);
    await setStatement(page, textarea,
      'WHEN pnl@positions.total <= -50000\nALERT telegram\nDO order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1)');

    await clickTokenAt(page, textarea, 'symbol');
    const panel = describePanel(page);
    await expect(panel).toBeVisible({ timeout: TIMEOUT });
    await expect(panel).toContainText('symbol');
  });

  test('clicking a param key inside a STILL-OPEN call (missing closing paren) also resolves', async ({ page }) => {
    const textarea = await goConsole(page);
    // Deliberately unclosed — the realistic "operator clicked an
    // already-typed key while still mid-edit on this call" case.
    await setStatement(page, textarea,
      'WHEN pnl@positions.total <= -50000\nALERT telegram\nDO order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL"');

    await clickTokenAt(page, textarea, 'symbol');
    const panel = describePanel(page);
    await expect(panel).toBeVisible({ timeout: TIMEOUT });
    await expect(panel).toContainText('symbol');
  });

  // ── (d) dropdown-wins precedence ─────────────────────────────────────
  // A bare CLICK on a param key resolves `describeTokenAt` AND would, on
  // its own, also make `suggestAgentCliAt` return non-empty `kind:'param'`
  // candidates for that same cursor position (the already-typed prefix
  // trivially fuzzy-matches itself) — `_onTokenClick`'s own `_dismissedAt`
  // write keeps the dropdown suppressed for THIS exact click so the two
  // panels don't fight over the same click. The MOMENT the operator
  // actually types further (a real edit, not just a click), the dropdown
  // is the correct thing to show instead, and the describe panel steps
  // aside — this test asserts BOTH halves, and that they're never visible
  // together.
  test('a param-key click shows the describe panel (not the dropdown); typing afterward shows the dropdown instead (never both)', async ({ page }) => {
    const textarea = await goConsole(page);
    // Trailing ", " — a genuinely fresh, not-yet-typed param-key slot —
    // is what the SECOND half of this test types into. Typing INTO an
    // already-complete word instead (e.g. extending "symbol" with an
    // extra character) breaks fuzzy-matching entirely (no remaining
    // candidate contains an arbitrary extra character), so the dropdown
    // would correctly stay empty for an unrelated reason — confirmed via
    // a live debug probe before writing this; this statement avoids that
    // trap by typing into real blank space instead.
    await setStatement(page, textarea,
      'WHEN pnl@positions.total <= -50000\nALERT telegram\nDO order(account="ZG0790", symbol="NIFTY25JULFUT", ');

    await clickTokenAt(page, textarea, 'symbol');
    await expect(describePanel(page)).toBeVisible({ timeout: TIMEOUT });
    await expect(page.locator('.agent-cli-dropdown')).toHaveCount(0);

    // A real keystroke into a genuinely fresh slot (not another click) is
    // the "actively editing" signal the live dropdown exists for.
    await textarea.evaluate((el) => { el.focus(); el.selectionStart = el.selectionEnd = el.value.length; });
    await textarea.pressSequentially('s', { delay: 15 });
    await expect(describePanel(page)).toHaveCount(0);
    await expect(page.locator('.agent-cli-dropdown')).toBeVisible({ timeout: TIMEOUT });
  });

  // ── (e) Escape dismisses without closing the host modal ─────────────
  test('Escape dismisses the describe panel without closing the host order-ticket modal', async ({ page }) => {
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(800);
    const modal = page.locator('[role="dialog"], .canonical-modal-overlay').first();
    for (let attempt = 0; attempt < 3; attempt++) {
      await page.click('body').catch(() => {});
      await page.waitForTimeout(300);
      await page.keyboard.press('t');
      const ok = await modal.waitFor({ state: 'visible', timeout: 3000 }).then(() => true).catch(() => false);
      if (ok) break;
    }
    await expect(modal).toBeVisible({ timeout: TIMEOUT });

    const cliTab = modal.getByRole('tab', { name: 'CLI' });
    // OrderCliTab mounts its OWN AgentCliEditor + its own
    // `fetchAgentCatalog()` call only once this tab becomes active — the
    // click handler is a no-op (`_clickDescResult = null`) until that
    // resolves, so the describe panel never appears without this wait
    // (confirmed via a live debug probe: `catalog?` was `false` without
    // it). Registered BEFORE the click that triggers the fetch.
    const catalogWaits = ['condition', 'notify', 'action'].map((g) =>
      page.waitForResponse((r) => r.url().includes(`/api/admin/grammar/tokens?grammar=${g}`), { timeout: TIMEOUT }).catch(() => null));
    await cliTab.click();
    await Promise.all(catalogWaits);
    const textarea = modal.locator('textarea.agent-cli-textarea');
    await expect(textarea).toBeVisible({ timeout: TIMEOUT });
    await setStatement(page, textarea, 'order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1)');

    await clickTokenAt(page, textarea, 'symbol');
    await expect(describePanel(page)).toBeVisible({ timeout: TIMEOUT });

    await page.keyboard.press('Escape');
    await expect(describePanel(page)).toHaveCount(0);
    // The host modal itself must still be open.
    await expect(modal).toBeVisible();
  });

  // ── (f) placement guard — never over Place-order/Cancel-all ─────────
  test('the describe panel never overlaps the Place-order button', async ({ page }) => {
    const textarea = await goConsole(page);
    // A genuinely-compilable equity order (not F&O) — "NIFTY25JULFUT" (the
    // worked example used elsewhere in this file for describe-only
    // assertions, which never need a successful compile) is an EXPIRED
    // contract in this environment and fails lot-size resolution, so no
    // "Place order" button would ever render for it — confirmed via a
    // live debug probe before writing this. This test specifically needs
    // the real button to exist to assert non-overlap against it.
    await setStatement(page, textarea,
      'order(account="ZG0790", symbol="RELIANCE", exchange="NSE", side="SELL", order_type="MARKET", qty=10)');

    await clickTokenAt(page, textarea, 'symbol');
    const panel = describePanel(page);
    await expect(panel).toBeVisible({ timeout: TIMEOUT });

    const placeOrderBtn = page.locator('.agent-cli-place-order');
    await expect(placeOrderBtn).toBeVisible({ timeout: TIMEOUT });

    const panelBox = await panel.boundingBox();
    const btnBox = await placeOrderBtn.boundingBox();
    expect(panelBox).toBeTruthy();
    expect(btnBox).toBeTruthy();
    // No vertical overlap between the two boxes.
    const overlaps = panelBox.y < btnBox.y + btnBox.height && panelBox.y + panelBox.height > btnBox.y
      && panelBox.x < btnBox.x + btnBox.width && panelBox.x + panelBox.width > btnBox.x;
    expect(overlaps).toBe(false);
  });

  // ── (g) existing hover-dropdown-description mechanism regression ────
  test('hovering a LIVE dropdown suggestion still shows its own description (unaffected by click-to-describe)', async ({ page }) => {
    const textarea = await goConsole(page);
    await textarea.click();
    await textarea.pressSequentially('WHEN pnl', { delay: 15 });

    const pnlRow = page.locator('.agent-cli-dropdown-row', { hasText: 'pnl' }).first();
    await expect(pnlRow).toBeVisible({ timeout: TIMEOUT });
    await expect(page.locator('.agent-cli-suggest-desc')).toHaveCount(0);

    await pnlRow.hover();
    const descEl = page.locator('.agent-cli-suggest-desc');
    await expect(descEl).toBeVisible({ timeout: TIMEOUT });
    await expect(descEl).not.toHaveText('');

    await textarea.hover();
    await expect(page.locator('.agent-cli-suggest-desc')).toHaveCount(0);
  });

  // ── Generated-statement preview click-to-describe ────────────────────
  test('clicking a token in the "Generated statement" preview describes it', async ({ page }) => {
    const textarea = await goConsole(page);
    await setStatement(page, textarea, 'WHEN pnl@positions.total <= -50000\nALERT telegram\nDO nop');

    const previewToken = page.locator('[data-testid="agent-cli-canonical-token"]', { hasText: 'telegram' }).first();
    await expect(previewToken).toBeVisible({ timeout: TIMEOUT });
    await previewToken.click();

    const previewPanel = page.locator('[data-testid="agent-cli-describe-panel-preview"]');
    await expect(previewPanel).toBeVisible({ timeout: TIMEOUT });
    await expect(previewPanel).toContainText('telegram');
  });
});

// ── (h) Automation page JSON-preview card click-to-describe ────────────

const PARAM_AGENT_ROW = {
  id: 9001, slug: 'click-describe-json-e2e', name: 'Click Describe JSON E2E', long_name: null,
  description: 'Fixture agent for JSON-preview click-to-describe coverage',
  conditions: { metric: 'pnl', scope: 'positions.total', op: '<=', value: -50000 },
  events: [{ channel: 'telegram', enabled: true }],
  actions: [{ type: 'close_position', params: { account: 'ZG0790', symbol: 'NIFTY25JULFUT' } }],
  scope: 'total', schedule: 'market_hours', cooldown_minutes: 30, fire_at_time: null,
  status: 'active', last_triggered_at: null, trigger_count: 0, last_error: null,
  is_system: false, lifespan_type: 'persistent', lifespan_max_fires: null,
  lifespan_expires_at: null, debounce_minutes: 0, tags: [],
  blackout_windows: [], trade_mode: 'paper', kind: 'cycle', tier: 'medium', topic: 'general',
  group: null, cli_source: null,
};

async function mockAgents(page) {
  await page.route('**/api/agents/', async (route) => {
    if (route.request().method() !== 'GET') { await route.continue(); return; }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([PARAM_AGENT_ROW]) });
  });
}

test.describe('Automation page — JSON-preview card click-to-describe (Part 2)', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await mockAgents(page);
  });

  test('clicking a top-level JSON param key shows its description without touching the raw JSON text', async ({ page }) => {
    await page.goto('/automation', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.kind-card-grid')).toBeVisible({ timeout: TIMEOUT });
    await page.locator('.kind-card[data-kind="agents"]').click();
    const row = page.locator('[role="listitem"]').filter({ hasText: 'Click Describe JSON E2E' });
    await expect(row).toBeVisible({ timeout: TIMEOUT });
    await row.locator('[role="button"]').first().click();

    const paramsPre = row.locator('pre.preview-action-params');
    await expect(paramsPre).toBeVisible({ timeout: TIMEOUT });
    const rawTextBefore = await paramsPre.textContent();
    // The raw JSON text is unaffected by the describe-click mechanism —
    // it must still contain the real key=value pairs verbatim.
    expect(rawTextBefore).toContain('"account"');
    expect(rawTextBefore).toContain('ZG0790');

    const symbolKey = row.locator('[data-testid="agent-param-key"]', { hasText: 'symbol' }).first();
    await expect(symbolKey).toBeVisible({ timeout: TIMEOUT });
    await symbolKey.click();

    const descPanel = row.locator('[data-testid="agent-param-desc"]');
    await expect(descPanel).toBeVisible({ timeout: TIMEOUT });
    await expect(descPanel).toContainText('symbol');

    // The raw JSON preview text itself is still byte-identical afterward
    // — this is a pure, read-only lookup.
    const rawTextAfter = await paramsPre.textContent();
    expect(rawTextAfter).toBe(rawTextBefore);
  });
});
