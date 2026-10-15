/**
 * console_ui_polish_bundle.spec.js — UI-polish bundle (2026-10-11, 4 items).
 *
 * 1. Console CLI order placement stays inline in the current panel (no
 *    SymbolPanel ticket modal) and shows success/failure in the
 *    AgentCliEditor's own `.agent-cli-submit-status` result area — same
 *    mechanism OrderCliTab already uses (see `console_relocation_cli_scope
 *    .spec.js`, which already proves submission itself is inline; this
 *    spec is the status-line + no-modal regression guard on top of that).
 * 2. Both Guide links (`ConsoleTab.svelte`, `OrderCliTab.svelte`) render
 *    as a button-shaped `.btn-secondary` pill, not a bare underlined
 *    text link.
 * 3. `CardControls` (via `CardHeader`) renders on the shared Console/
 *    Agents tab-strip card (`.aw-view`, Header button-group
 *    standardization Part 8, 2026-10-13 — superseded ConsoleTab's own
 *    now-removed CardHeader), with Fullscreen wired (Search omitted —
 *    no grid at this level).
 * 4. The three Agents-tab kind-count cards (Agents/Alerts/Brackets —
 *    Templates folded into Agents 2026-10-12) each render a `.kind-
 *    card-n` AND `.kind-card-l` color keyed off `data-kind`. Revised
 *    2026-10-13: Brackets moved OFF the amber "Open status card" scheme
 *    it briefly shared with Agents, onto the GREEN "Filled status"
 *    scheme instead (operator-requested) — all three kind-cards
 *    (Agents/Alerts/Brackets) are distinct colors again.
 *
 * Mocking — order submission is mocked at the network boundary
 * (`POST /api/orders/ticket`), same approach as the sibling
 * `console_relocation_cli_scope.spec.js` — never places a real order.
 *
 * Viewport: chromium-desktop only — CLI authoring + card chrome are not
 * mobile-first surfaces (same convention as every sibling CLI spec).
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.use({ projects: ['chromium-desktop'] });

const TIMEOUT = 20_000;

/** Mocks a write endpoint, capturing the posted JSON body into `out.body`. */
async function mockPost(/** @type {import('@playwright/test').Page} */ page, /** @type {string} */ pattern, /** @type {{body: any}} */ out, /** @type {any} */ responseBody, /** @type {number} */ status = 200) {
  await page.route(pattern, async (route) => {
    if (route.request().method() !== 'POST') { await route.continue(); return; }
    out.body = route.request().postDataJSON();
    await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(responseBody) });
  });
}

test.describe('Item 1 — Console CLI order placement stays inline, no modal', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('successful order(...) submit shows the result in .agent-cli-submit-status, no modal opens', async ({ page }) => {
    const captured = { body: null };
    await mockPost(page, '**/api/orders/ticket', captured, { status: 'ok', order_id: 'ZZZ-E2E-POLISH-1' });

    await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });

    const cli = page.locator('.agent-cli-textarea');
    await expect(cli).toBeVisible({ timeout: TIMEOUT });
    await cli.fill('order(account="ZG0790", symbol="RELIANCE", exchange="NSE", side="SELL", qty=1, price=100)');

    const placeBtn = page.locator('.agent-cli-place-order');
    await expect(placeBtn).toBeVisible({ timeout: TIMEOUT });
    await placeBtn.click();

    await expect.poll(() => captured.body, { timeout: TIMEOUT }).not.toBeNull();

    // Result shown inline, in the shared submitStatus mechanism.
    const status = page.locator('.agent-cli-submit-status');
    await expect(status).toBeVisible({ timeout: TIMEOUT });
    await expect(status).toContainText('Order placed');
    await expect(status).not.toHaveClass(/is-error/);

    // No ticket modal / SymbolPanel overlay ever appears on this path.
    await expect(page.locator('.oes-modal')).toHaveCount(0);
  });

  test('a failed order(...) submit shows an error in .agent-cli-submit-status, no modal opens', async ({ page }) => {
    const captured = { body: null };
    await mockPost(page, '**/api/orders/ticket', captured, { detail: 'margin shortfall' }, 400);

    await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });

    const cli = page.locator('.agent-cli-textarea');
    await expect(cli).toBeVisible({ timeout: TIMEOUT });
    await cli.fill('order(account="ZG0790", symbol="RELIANCE", exchange="NSE", side="SELL", qty=1, price=100)');

    const placeBtn = page.locator('.agent-cli-place-order');
    await expect(placeBtn).toBeVisible({ timeout: TIMEOUT });
    await placeBtn.click();

    await expect.poll(() => captured.body, { timeout: TIMEOUT }).not.toBeNull();

    const status = page.locator('.agent-cli-submit-status');
    await expect(status).toBeVisible({ timeout: TIMEOUT });
    await expect(status).toHaveClass(/is-error/);
    await expect(status).toContainText('Order failed');

    // Still no ticket modal — failure is shown inline too, not a popup.
    await expect(page.locator('.oes-modal')).toHaveCount(0);
    // CLI text is preserved on failure so the operator can fix + retry.
    await expect(cli).not.toHaveValue('');
  });
});

test.describe('Item 2 — Guide link renders as a button, not a bare link', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('Console tab Guide link has button-pill chrome', async ({ page }) => {
    await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });

    const guideLink = page.locator('a[href="/automation/guide"]').first();
    await expect(guideLink).toBeVisible({ timeout: TIMEOUT });
    await expect(guideLink).toHaveClass(/btn-secondary/);
    // Renamed "Guide: CLI grammar reference" -> "Trader Guide" (2026-10
    // operator request) — Orders' OWN CLI-tab Guide link (sibling test
    // below, OrderCliTab.svelte) got the same rename in a later pass
    // (2026-10, "remove account, exchange, symbol, console details.
    // just keep trader guide button for link").
    await expect(guideLink).toContainText('Trader Guide');

    // A real button-shaped affordance has padding + a border, not an
    // underline-only text link.
    const style = await guideLink.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { textDecorationLine: cs.textDecorationLine, borderWidth: cs.borderTopWidth };
    });
    expect(style.textDecorationLine).toBe('none');
    expect(parseFloat(style.borderWidth)).toBeGreaterThan(0);
  });

  test('Orders CLI tab Guide link has button-pill chrome', async ({ page }) => {
    // 2026-10 — the Guide link moved from OrderCliTab's own body
    // (.order-cli-guide-link, removed) up into SymbolPanel's .oes-tabs
    // row (.oes-cli-guide-link), replacing LTP/CHASE for the CLI tab
    // specifically (operator: "when cli is selected ... ltp and chase
    // should not be displayed. in its place, trader guide button for
    // link should be displayed").
    await page.goto('/orders', { waitUntil: 'domcontentloaded' });

    const cliTab = page.getByRole('tab', { name: 'CLI' });
    await expect(cliTab).toBeVisible({ timeout: TIMEOUT });
    await cliTab.click();

    const guideLink = page.locator('a.oes-cli-guide-link');
    await expect(guideLink).toBeVisible({ timeout: TIMEOUT });
    await expect(guideLink).toHaveClass(/btn-secondary/);
    await expect(guideLink).toContainText('Trader Guide');

    const style = await guideLink.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { textDecorationLine: cs.textDecorationLine, borderWidth: cs.borderTopWidth };
    });
    expect(style.textDecorationLine).toBe('none');
    expect(parseFloat(style.borderWidth)).toBeGreaterThan(0);
  });
});

test.describe('Item 3 — Workspace header removed; Console/Agents tab row carries the kind-dropdown + Guide link instead (2026-10)', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  // Operator request (2026-10): remove the "Workspace" CardHeader (title +
  // Fullscreen) entirely; the Console/Agents tab strip becomes a decorated
  // CardHeader row with no title and no CardControls cluster at all
  // (`showControls={false}`) — the kind-dropdown + Guide link share that
  // same row instead, console-only. Uses direct navigation (not a tab
  // click) to select the active tab — a separate, pre-existing bug in the
  // Console/Agents click-to-switch mechanism (unrelated to this change,
  // reproduced against the unmodified file too) makes click-based
  // switching unreliable; direct `?tab=` navigation is unaffected by it.
  test('no "Workspace" title and no Fullscreen control anywhere on the tab-strip header', async ({ page }) => {
    await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });

    await expect(page.locator('.console-card')).toHaveCount(0);
    await expect(page.locator('.ch-title', { hasText: 'Workspace' })).toHaveCount(0);

    const header = page.locator('.aw-view .card-header');
    await expect(header).toBeVisible({ timeout: TIMEOUT });
    await expect(header.locator('button[aria-label*="fullscreen" i], button[title*="fullscreen" i]')).toHaveCount(0);
    await expect(page.locator('.aw-view.fs-card-on')).toHaveCount(0);
  });

  test('tab-strip header omits the whole CardControls cluster (no Search/Download either)', async ({ page }) => {
    await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });

    const header = page.locator('.aw-view .card-header');
    await expect(header).toBeVisible({ timeout: TIMEOUT });

    await expect(header.locator('button[title*="search" i], button[aria-label*="search" i]')).toHaveCount(0);
    await expect(header.locator('button[title*="download" i], button[aria-label*="download" i]')).toHaveCount(0);
  });

  test('Console/Agents tabs and the Guide link share the SAME CardHeader row', async ({ page }) => {
    await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });

    const tabsHeader = page.locator('.aw-tabs-wrap').locator('xpath=ancestor::div[contains(@class,"card-header")]');
    const guideHeader = page.locator('a.console-guide-btn').locator('xpath=ancestor::div[contains(@class,"card-header")]');
    await expect(tabsHeader).toHaveCount(1);
    await expect(guideHeader).toHaveCount(1);
    expect(await tabsHeader.evaluate((el) => el)).toBeTruthy();

    const sameRow = await page.evaluate(() => {
      const tabs = document.querySelector('.aw-tabs-wrap');
      const guide = document.querySelector('a.console-guide-btn');
      return !!(tabs && guide && tabs.closest('.card-header') === guide.closest('.card-header'));
    });
    expect(sameRow).toBe(true);
  });

  // Bug fix (2026-10, operator: "keep order dropdown just after agents
  // tab. make the the link button to read as Trader Guide. make it to
  // display after the dropdown. elements like dropdown, trader guide
  // shoule be of same height and should be verticall centered") —
  // tabs/dropdown/Guide link all moved into CardHeader's ONE `left`
  // zone (were split across left/right, pushed apart by the middle
  // spacer), in that exact order, with height parity between the
  // dropdown and the Guide link.
  test('tabs, kind-dropdown, and "Trader Guide" link render in that exact order, same height, vertically centered', async ({ page }) => {
    await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });

    const left = page.locator('.aw-view .card-header .ch-left');
    await expect(left).toBeVisible({ timeout: TIMEOUT });

    const info = await left.evaluate((el) => {
      const children = Array.from(el.children);
      const order = children.map((c) => {
        // 2026-10-15 — AutomationTabs is now wrapped in `.aw-tabs-in-
        // header` (a scoping span that zeroes the shared `.aw-tabs-wrap`
        // component's own bottom margin, which otherwise ate the
        // vertical-centering space in this CardHeader context) — the
        // direct `.ch-left` child is the wrapper, not `.aw-tabs-wrap`
        // itself anymore.
        if (c.classList.contains('aw-tabs-in-header') || c.classList.contains('aw-tabs-wrap')) return 'tabs';
        // 2026-10-15 — Select is now wrapped in `.aw-kind-select` (a
        // scoping span for the --ctl-h height/width override, since
        // Select.svelte has no class/style passthrough prop) — the
        // direct .ch-left child is the wrapper, not `.rbq-select`
        // itself anymore.
        if (c.classList.contains('aw-kind-select') || c.classList.contains('rbq-select')) return 'dropdown';
        if (c.classList.contains('console-guide-btn')) return 'guide';
        return 'other';
      });
      const select = el.querySelector('.rbq-select-trigger');
      const guide = el.querySelector('.console-guide-btn');
      const selectRect = select?.getBoundingClientRect();
      const guideRect = guide?.getBoundingClientRect();
      return {
        order,
        guideText: guide?.textContent.trim(),
        selectHeight: selectRect?.height,
        guideHeight: guideRect?.height,
        selectCenterY: selectRect ? selectRect.top + selectRect.height / 2 : null,
        guideCenterY: guideRect ? guideRect.top + guideRect.height / 2 : null,
      };
    });

    expect(info.order).toEqual(['tabs', 'dropdown', 'guide']);
    expect(info.guideText).toBe('Trader Guide');
    // Height parity within 1px (sub-pixel rendering tolerance), and
    // vertical centers aligned within 1px.
    expect(Math.abs(info.selectHeight - info.guideHeight)).toBeLessThanOrEqual(1);
    expect(Math.abs(info.selectCenterY - info.guideCenterY)).toBeLessThanOrEqual(1);
  });

  // 2026-10-15 — operator: "console dropdown and trader guide elements on
  // header should look like order cli header in terms of size, position,
  // and vertical alignment. the dropdown width should be expanded
  // further." Both elements now converge on the SAME --ctl-h (1.7rem ≈
  // 27.2px) floor the Orders CLI tab's own `.oes-cli-guide-link` uses
  // (fixed earlier this session) — not just parity with EACH OTHER
  // (already covered above), but matching that external reference, and
  // the kind-select is no longer clipped/cramped at its old ~61.5px
  // natural width.
  test('kind-dropdown and Guide link height matches the Orders CLI tab reference (--ctl-h), and dropdown width is expanded', async ({ page }) => {
    await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });
    const select = page.locator('.aw-kind-select .rbq-select-trigger');
    const guide = page.locator('.console-guide-btn');
    await expect(select).toBeVisible({ timeout: TIMEOUT });

    const selectBox = await select.boundingBox();
    const guideBox = await guide.boundingBox();
    // Orders CLI tab's own reference height, confirmed live earlier this
    // session: 27.1875px (--ctl-h: 1.7rem at 16px root).
    expect(selectBox.height).toBeGreaterThan(26);
    expect(selectBox.height).toBeLessThan(29);
    expect(guideBox.height).toBeGreaterThan(26);
    expect(guideBox.height).toBeLessThan(29);
    // Width expanded well past the old ~61.5px natural width — wide
    // enough that "Bracket" (the longest kind label) isn't cramped.
    expect(selectBox.width).toBeGreaterThan(100);
  });

  // 2026-10-15 — operator: "I want trader guide and dropdown control in
  // console/agents tab header to be aligned with tab text." The CONSOLE/
  // AGENTS tab text used to sit flush to the TOP of `.ch-left` instead of
  // vertically centered like Select/Guide — root cause was
  // AutomationTabs.svelte's `.aw-tabs-wrap` bottom margin (meant for
  // page-body spacing in its original standalone usage) being counted in
  // the flex cross-axis sizing here, consuming the space `align-items:
  // center` needed to center the wrapper. Zeroed in this CardHeader-only
  // context via a scoped `.aw-tabs-in-header` wrapper.
  test('CONSOLE/AGENTS tab text is vertically centered with the kind-dropdown/Guide link, not flush to the top', async ({ page }) => {
    await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });
    const select = page.locator('.aw-kind-select .rbq-select-trigger');
    await expect(select).toBeVisible({ timeout: TIMEOUT });

    const info = await page.evaluate(() => {
      const tabs = Array.from(document.querySelectorAll('.algo-tabs-strip .algo-tab'));
      const consoleBtn = tabs.find((b) => b.textContent.trim().toLowerCase() === 'console');
      const select = document.querySelector('.rbq-select-trigger');
      const g = (el) => {
        const r = el.getBoundingClientRect();
        return r.top + r.height / 2;
      };
      return { tabCenter: consoleBtn ? g(consoleBtn) : null, selectCenter: select ? g(select) : null };
    });
    expect(info.tabCenter).not.toBeNull();
    // Within 1px — same sub-pixel tolerance the pre-existing Select-vs-
    // Guide parity test above already uses.
    expect(Math.abs(info.tabCenter - info.selectCenter)).toBeLessThanOrEqual(1);
  });

  test('the kind-dropdown + Guide link are console-only — absent on the Agents tab', async ({ page }) => {
    await page.goto('/automation?tab=agents', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('a.console-guide-btn')).toHaveCount(0);

    // AutomationTabs itself still renders on the Agents tab — only the
    // console-specific controls are gated.
    await expect(page.locator('.aw-tabs-wrap')).toBeVisible({ timeout: TIMEOUT });
  });

  // Bug fix (2026-10, operator: "need to click two times on agents tab to
  // switch — that is a bug") — a stale-`page.url` race in the URL<->tab
  // sync `$effect` meant ONE click on "Agents" silently reverted itself
  // (the Console textarea stayed visible), and only a SECOND click made
  // the switch stick. Fixed via `untrack()` around the effect's own
  // comparison/write reads. Both directions, single click each.
  test('a single click switches Console -> Agents -> Console (no double-click needed)', async ({ page }) => {
    await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });
    const awView = page.locator('.aw-view');

    await awView.getByRole('tab', { name: 'Agents' }).click();
    await expect(page.locator('.kind-card-grid')).toBeVisible({ timeout: TIMEOUT });
    await expect(page.locator('textarea.agent-cli-textarea')).toHaveCount(0);

    await awView.getByRole('tab', { name: 'Console' }).click();
    await expect(page.locator('textarea.agent-cli-textarea')).toBeVisible({ timeout: TIMEOUT });
    await expect(page.locator('.kind-card-grid')).toHaveCount(0);
  });
});

test.describe('Item 4 — Agents-tab kind-cards each get a distinct color', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  // Revised 2026-10-13 — operator: "brackets color should match Filled
  // status, not Open" — Brackets now uses the GREEN scheme (Orders'
  // `.ob-sc[data-status="active"]`/"Filled" chip tokens), distinct from
  // both Agents' amber and Alerts' sky. All three kind-cards assert
  // pairwise distinct colors.
  test('Agents/Alerts/Brackets kind-cards each have a distinct color (.kind-card-n)', async ({ page }) => {
    await page.goto('/automation', { waitUntil: 'domcontentloaded' });

    const colorOf = async (kind) => {
      const card = page.locator(`.kind-card[data-kind="${kind}"] .kind-card-n`);
      await expect(card).toBeVisible({ timeout: TIMEOUT });
      return card.evaluate((el) => getComputedStyle(el).color);
    };
    const [agents, alerts, brackets] = await Promise.all([colorOf('agents'), colorOf('alerts'), colorOf('brackets')]);
    expect(brackets).not.toBe(agents);
    expect(alerts).not.toBe(agents);
    expect(brackets).not.toBe(alerts);
  });

  test('Agents/Alerts/Brackets kind-cards each have a distinct .kind-card-l (label) color', async ({ page }) => {
    await page.goto('/automation', { waitUntil: 'domcontentloaded' });

    const colorOf = async (kind) => {
      const label = page.locator(`.kind-card[data-kind="${kind}"] .kind-card-l`);
      await expect(label).toBeVisible({ timeout: TIMEOUT });
      return label.evaluate((el) => getComputedStyle(el).color);
    };
    const [agents, alerts, brackets] = await Promise.all([colorOf('agents'), colorOf('alerts'), colorOf('brackets')]);
    expect(brackets).not.toBe(agents);
    expect(alerts).not.toBe(agents);
    expect(brackets).not.toBe(alerts);
  });
});
