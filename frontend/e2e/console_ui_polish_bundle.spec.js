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
 * 4. (Historical — superseded.) The Agents-tab kind counts (originally
 *    Agents/Alerts/Brackets — Templates folded into Agents 2026-10-12)
 *    each carried a colour keyed to their kind. The kind-count grid
 *    itself was replaced by a dropdown (2026-10-31), and the Brackets
 *    kind was removed entirely (2026-11, Track 3 T3.4, Bracket/Template/
 *    GTT removal) — no test below exercises this item anymore.
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
import { agentKindTrigger, pickAgentKind } from './fixtures/agentKind.js';

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
  test('tabs + kind-dropdown render in order in .ch-left; Trader Guide pins to .ch-right at the far edge, same height, vertically centered', async ({ page }) => {
    // 2026-11-01 (operator: "trade guide in automation page" ... "make
    // [it] to shift to right edge") — Trader Guide moved from `.ch-left`
    // (sequential after tabs/dropdown) into `.ch-right`, which sits after
    // `.ch-middle` (CardHeader's own flex:1 spacer) and so pins to the
    // header's far right edge. Supersedes the prior "same-container,
    // three-item order" assertion below.
    await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });

    const header = page.locator('.aw-view .card-header');
    const left = header.locator('.ch-left');
    await expect(left).toBeVisible({ timeout: TIMEOUT });

    const info = await header.evaluate((headerEl) => {
      const leftEl = headerEl.querySelector('.ch-left');
      const order = Array.from(leftEl.children).map((c) => {
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
        return 'other';
      });
      const select = leftEl.querySelector('.rbq-select-trigger');
      const guide = headerEl.querySelector('.ch-right .console-guide-btn');
      const selectRect = select?.getBoundingClientRect();
      const guideRect = guide?.getBoundingClientRect();
      const headerRect = headerEl.getBoundingClientRect();
      return {
        order,
        guideInRight: !!headerEl.querySelector('.ch-right .console-guide-btn'),
        guideText: guide?.textContent.trim(),
        selectHeight: selectRect?.height,
        guideHeight: guideRect?.height,
        selectCenterY: selectRect ? selectRect.top + selectRect.height / 2 : null,
        guideCenterY: guideRect ? guideRect.top + guideRect.height / 2 : null,
        // Guide's right edge must be at (or very near) the header's own
        // right edge — the "pinned to the far right" assertion.
        guideRightGap: guideRect ? headerRect.right - guideRect.right : null,
      };
    });

    expect(info.order).toEqual(['tabs', 'dropdown']);
    expect(info.guideInRight).toBe(true);
    expect(info.guideText).toBe('Trader Guide');
    // Height parity within 1px (sub-pixel rendering tolerance), and
    // vertical centers aligned within 1px — still true across the two
    // separate flex children, since both share the same `.card-header`
    // row with `align-items: center`.
    expect(Math.abs(info.selectHeight - info.guideHeight)).toBeLessThanOrEqual(1);
    expect(Math.abs(info.selectCenterY - info.guideCenterY)).toBeLessThanOrEqual(1);
    // Within a few px of the header's own right padding — "pinned to the
    // right edge", not just "somewhere after middle".
    expect(info.guideRightGap).toBeLessThanOrEqual(8);
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
  //
  // 2026-10-31 — SUPERSEDED by a further operator refinement (three
  // follow-ups in one ask): "make orders dropdown in automation look
  // similar size and format like all accounts dropdown in pulse page" /
  // "make orders dropdwon and trade guide element of same height and
  // vertical alaignement" / "They should be aligned tab text too." The
  // reference control is now Pulse's AccountMultiSelect
  // (`MultiSelect.svelte`'s `.rbq-multi-trigger`, min-height 1.55rem),
  // not the Orders CLI tab's 1.7rem floor — the height range below moves
  // from 26-29px to the new ~24.8px (desktop) target. See the dated
  // comment above `.console-guide-btn` in `+page.svelte` for the full
  // box-model derivation. Width/gap assertions are UNCHANGED — this
  // round's ask was height/alignment only.
  test('kind-dropdown and Guide link height matches the Pulse AccountMultiSelect reference (2026-10-31), width is 5.4rem (reduced a further 10%, 2026-10-17), with a gap before Trader Guide', async ({ page }) => {
    await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });
    const select = page.locator('.aw-kind-select .rbq-select-trigger');
    const guide = page.locator('.console-guide-btn');
    await expect(select).toBeVisible({ timeout: TIMEOUT });

    // 2026-11-01 — Console's default kind changed 'Order' -> 'Action agent'
    // (to match the Agents tab's own default), so the trigger's CONTENT-
    // DRIVEN width (min-width is a floor, not a cap — see `.aw-kind-select`
    // in +page.svelte) now renders wider at default than this test's
    // 80-92px window, which was tuned against the short 'Order' label.
    // Explicitly select 'Order' first so this test still measures the
    // floor-width case it was written for, independent of whatever kind
    // happens to be the default.
    await select.click();
    await page.getByRole('option', { name: 'Order' }).click();
    await expect(select).toContainText('Order');

    const selectBox = await select.boundingBox();
    const guideBox = await guide.boundingBox();
    // Pulse AccountMultiSelect's own reference height, confirmed live
    // on this page post-fix: 24.796875px (min-height: 1.55rem at 16px
    // root, matching MultiSelect.svelte's `.rbq-multi-trigger` box model
    // byte-for-byte — see the 2026-10-31 comment in +page.svelte).
    // 2026-10-31 — `test.use({ projects: [...] })` above this describe
    // is NOT a real Playwright restriction (confirmed by running this
    // spec explicitly with `--project=mobile-portrait`: it still
    // executes) — a plain `npx playwright test` with no `--project`
    // filter runs every spec on all three configured projects. The CSS
    // this test checks is now viewport-aware (`max(1.55rem, var(
    // --toolbar-h))`, which steps to 28px at the ≤600px breakpoint), so
    // the expected height must branch on the real viewport width rather
    // than assume chromium-desktop's 1400px.
    const vpWidth = page.viewportSize()?.width ?? 1400;
    const [lo, hi] = vpWidth <= 600 ? [27, 29] : [23, 26];
    expect(selectBox.height).toBeGreaterThan(lo);
    expect(selectBox.height).toBeLessThan(hi);
    expect(guideBox.height).toBeGreaterThan(lo);
    expect(guideBox.height).toBeLessThan(hi);
    // 2026-10-15 operator follow-up: "rdudce the width of dropdown by
    // 20%" — down from the first pass's 7.5rem (120px) to 6rem (96px).
    // 2026-10-17 operator follow-up: reduced a further 10%, to 5.4rem
    // (86.4px) min-width — still wide enough that "Action agent" (the
    // longest kind label; "Bracket" was removed 2026-11, Track 3 T3.4,
    // Bracket/Template/GTT removal, but was never the longest label to
    // begin with) isn't cramped.
    expect(selectBox.width).toBeGreaterThan(80);
    expect(selectBox.width).toBeLessThan(92);
    // "add additional space after the dropdown and before trader guide"
    // — a real gap now exists beyond `.ch-left`'s own smaller default
    // --ch-gap spacing.
    expect(guideBox.x - (selectBox.x + selectBox.width)).toBeGreaterThan(10);
  });

  // 2026-10-17 operator follow-up #1: "the dropdown and trade guide should
  // be of same height... the dropdown box needs to have a shaded
  // background to differentiate it from trade guide which is just a
  // button for a link." First pass shaded via a box-shadow HALO on the
  // `.aw-kind-select` wrapper (outside the layout box, so it didn't add
  // to the wrapper's rendered height).
  //
  // 2026-10-17 operator follow-up #2 (supersedes #1's halo): "you added
  // shade around it. i want shade inside the dropdown which looks like a
  // button... remove shade around it." The halo read as a glow AROUND
  // the control, not a fill INSIDE it. Replaced with a background-color
  // directly on the trigger itself (`.rbq-select-trigger`) — a
  // background override never changes box-model height, so the
  // same-height-as-Guide invariant holds without the halo trick.
  test('kind-dropdown trigger has an inside background shade (no outer halo), still EXACTLY the same height as Trader Guide', async ({ page }) => {
    await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });
    const wrap = page.locator('.aw-kind-select');
    const trigger = page.locator('.aw-kind-select .rbq-select-trigger');
    const guide = page.locator('.console-guide-btn');
    await expect(wrap).toBeVisible({ timeout: TIMEOUT });

    const [wrapBox, guideBox, wrapBoxShadow, triggerBg] = await Promise.all([
      wrap.boundingBox(),
      guide.boundingBox(),
      wrap.evaluate((el) => getComputedStyle(el).boxShadow),
      trigger.evaluate((el) => getComputedStyle(el).backgroundColor),
    ]);
    expect(Math.abs(wrapBox.height - guideBox.height), `wrap=${wrapBox.height} guide=${guideBox.height}`).toBeLessThan(0.5);
    expect(wrapBoxShadow, 'wrapper must NOT have an outer halo any more').toBe('none');
    // rgba(255,255,255,0.10) — asserts an actual visible alpha fill, not
    // transparent/none and not the shared Select component's own navy
    // gradient (which this page-scoped override replaces).
    expect(triggerBg, 'trigger must have a visible inside background shade').toMatch(/rgba\(255,\s*255,\s*255,\s*0\.1\)/);
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

  test('the Guide link renders on the Agents tab too, with the Agents tab\'s own kind-dropdown — 2026-11-01', async ({ page }) => {
    // Supersedes the prior "console-only" expectation (operator: "the
    // dropdown in automation agents tab for various agents count can be
    // on the header like in console" / "even trade guide can be
    // a[gents] automation" / "on the header") — the Guide link and a
    // kind-dropdown both render on either tab now, in the same header
    // zone. Console's own `.aw-kind-select` is a DIFFERENT dropdown
    // (aria-label "Statement kind") from the Agents tab's (aria-label
    // "Agent kind") — this test only needs to confirm SOMETHING with
    // that class renders, not which one.
    await page.goto('/automation?tab=agents', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('a.console-guide-btn')).toHaveCount(1);
    await expect(page.locator('.aw-kind-select')).toHaveCount(1);

    // AutomationTabs itself still renders on the Agents tab.
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
    await expect(agentKindTrigger(page)).toBeVisible({ timeout: TIMEOUT });
    await expect(page.locator('textarea.agent-cli-textarea')).toHaveCount(0);

    await awView.getByRole('tab', { name: 'Console' }).click();
    await expect(page.locator('textarea.agent-cli-textarea')).toBeVisible({ timeout: TIMEOUT });
    await expect(agentKindTrigger(page)).toHaveCount(0);
  });
});
