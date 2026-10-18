/**
 * LogPanel Alerts tab — 2026-10 nav restructure regression spec,
 * extended 2026-10-13 for the "CardHeader + Activity-panel consistency
 * sweep" (Part 1).
 *
 * The standalone /admin/alerts page was retired and its functionality
 * folded into LogPanel as a new 'alerts' tab, gated by the view_audit
 * capability. Until 2026-10-13 it was mounted ONLY on /automation
 * (explicit `tabs=` override) — every OTHER embedding site
 * (ActivityLogModal, /activity page, /dashboard card) fell through to
 * LogPanel's own default `tabs` array, which never included 'alerts'
 * to begin with. That was a baked-in omission in the component's own
 * default, not a deliberate per-page choice anywhere — fixing the one
 * default array (`LogPanel.svelte`, the `tabs` prop default) fixes
 * every site that doesn't already explicitly override it.
 *
 * This spec now also covers:
 *   - the tab's display label flip back to "Alerts" (from the interim
 *     "Alert Log" used during Sprint C — see LogPanel.svelte's own
 *     comment above `_ALL_TABS` for the full history + the accepted,
 *     purely-cosmetic wording overlap with /automation's "Alerts"
 *     kind-card)
 *   - tab-set consistency across every real embedding site
 *   - contextual default-active-tab on ActivityLogModal, threaded via
 *     PageHeaderActions' new `logTabHint` prop (reuses the existing
 *     `openActivityModal(tab)` deep-link mechanism BrokerHealthBadge
 *     already used for 'conn' — see stores.js)
 *   - width-consistency between the /automation bottom-attached strip
 *     and the bell-icon modal (both now wrap a LogPanel instance with
 *     the same effective horizontal gutter)
 *
 * Five quality dimensions (per feedback_test_dimensions.md):
 *   SSOT     — the view_audit cap gate lives once, inside LogPanel's own
 *              VISIBLE_TABS filter; no page-level duplicate gate. The
 *              tab SET lives once, inside LogPanel's own default `tabs`
 *              prop; no page-level duplicate list (except /automation's
 *              own harmless, already-matching explicit override).
 *   Perf     — the strip is a single LogPanel instance shared by every
 *              AutomationTabs sub-view (Console/Agents), not remounted
 *              per tab switch.
 *   Stale    — source-scan confirms the deleted /admin/alerts route is
 *              gone from disk and from the nav link array; no dangling
 *              href elsewhere in the app.
 *   Reusable — Alerts rows render through the same _logRow()-based
 *              convention every other LogPanel tab uses (time chip /
 *              level chip / message / tag), not a bespoke layout.
 *   UX       — bottom strip stays mounted across AutomationTabs
 *              sub-views (Console/Agents); an operator without
 *              view_audit never sees the tab at all; a modal opened
 *              from a different launch site lands on a different,
 *              contextually-relevant tab.
 */

import { test, expect } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';
import { fileURLToPath } from 'node:url';
import { loginAsAdmin } from './fixtures/auth.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname  = path.dirname(__filename);

test.describe('LogPanel Alerts tab on /automation', () => {
  test.setTimeout(60_000);

  test('authorized (view_audit) operator sees the Alerts tab and it renders rows or an empty state', async ({ page }) => {
    await loginAsAdmin(page); // rambo — admin role, holds view_audit per rbac.py CAPS
    await page.goto('/automation', { waitUntil: 'domcontentloaded' });

    // Bottom-attached strip renders regardless of which AutomationTabs
    // sub-view is active — confirm it's present on first paint (default
    // sub-view is Agents).
    const strip = page.locator('.lp-header-wrap').first();
    await expect(strip).toBeVisible({ timeout: 15_000 });

    const alertsTab = page.getByRole('tab', { name: 'Alerts', exact: true });
    await expect(alertsTab, 'Alerts tab must be visible for a view_audit-capable operator').toBeVisible({ timeout: 10_000 });

    // Strip stays mounted (not remounted/torn down) when switching the
    // page's own top-level AutomationTabs sub-view — the same
    // always-visible structural role Orders' own activity card plays.
    const topTabs = page.locator('.aw-tabs-wrap');
    const consoleTab = topTabs.getByRole('tab', { name: 'Console' });
    if (await consoleTab.count()) {
      await consoleTab.click();
      await expect(strip, 'bottom strip must stay mounted across AutomationTabs sub-views').toBeVisible();
      await expect(page.getByRole('tab', { name: 'Alerts', exact: true })).toBeVisible();
      // Return to Agents so later assertions aren't order-dependent.
      await topTabs.getByRole('tab', { name: 'Agents' }).click();
    }

    await alertsTab.click();
    // Either real rows or the explicit empty state — never a blank panel.
    const rowsOrEmpty = page.locator('.log-row').first();
    await expect(rowsOrEmpty, 'Alerts tab must render rows or the "No alert events yet." empty state').toBeVisible({ timeout: 10_000 });
  });

  test('unauthorized (no view_audit) operator never sees the Alerts tab', async ({ page }) => {
    // Strip view_audit from the /whoami response so the SAME admin
    // account renders as if it lacked the capability — avoids needing
    // a separate partner/risk-role fixture account.
    await page.route('**/api/auth/whoami', async (route) => {
      const resp = await route.fetch();
      let json;
      try { json = await resp.json(); } catch { json = null; }
      if (json && Array.isArray(json.caps)) {
        json.caps = json.caps.filter((c) => c !== 'view_audit');
      }
      await route.fulfill({ response: resp, json: json ?? undefined });
    });

    await loginAsAdmin(page);
    await page.goto('/automation', { waitUntil: 'domcontentloaded' });

    const strip = page.locator('.lp-header-wrap').first();
    await expect(strip).toBeVisible({ timeout: 15_000 });

    // Give the tab strip a moment to settle (poll-driven caps bridge),
    // then assert Alerts never appears.
    await page.waitForTimeout(1500);
    await expect(page.getByRole('tab', { name: 'Alerts', exact: true }), 'Alerts tab must stay hidden without view_audit').toHaveCount(0);

    // The rest of the strip must still work normally — Agents tab is
    // always visible regardless of the cap gate. Scoped to the bottom
    // log strip specifically — /automation's own top-level AutomationTabs
    // sub-view switcher also has an "Agents" entry (a different
    // AlgoTabs instance, non-compact), so an unscoped role lookup
    // ambiguously matches both.
    await expect(strip.getByRole('tab', { name: 'Agents' })).toBeVisible();
  });

  test('visiting the deleted /admin/alerts route no longer renders the old Alerts page', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/admin/alerts', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(1000);

    // The old page's own title chip must be gone — whatever SvelteKit's
    // default not-found behaviour renders instead, it is NOT the
    // retired Alerts page.
    const oldTitle = page.locator('.page-title-chip', { hasText: 'Alerts' });
    await expect(oldTitle, 'the retired /admin/alerts page must not render').toHaveCount(0);
  });
});

test.describe('[STALE] source-scan — /admin/alerts route fully removed', () => {
  test('route directory deleted from disk', () => {
    const routeDir = path.resolve(__dirname, '..', 'src', 'routes', '(algo)', 'admin', 'alerts');
    expect(fs.existsSync(routeDir), `${routeDir} must not exist`).toBe(false);
  });

  test('no nav link entry still points at /admin/alerts', () => {
    const layoutPath = path.resolve(__dirname, '..', 'src', 'routes', '(algo)', '+layout.svelte');
    const src = fs.readFileSync(layoutPath, 'utf8');
    expect(src, '+layout.svelte must not carry a nav entry for /admin/alerts')
      .not.toMatch(/href:\s*['"]\/admin\/alerts['"]/);
  });

  test('LogPanel carries the alerts tab definition, labelled "Alerts", gated by view_audit', () => {
    const logPanelPath = path.resolve(__dirname, '..', 'src', 'lib', 'LogPanel.svelte');
    const src = fs.readFileSync(logPanelPath, 'utf8');
    expect(src, 'LogPanel must define an alerts tab labelled "Alerts"').toMatch(/\['alerts',\s*'Alerts'\]/);
    expect(src, 'LogPanel must gate the alerts tab by view_audit').toContain("hasCap('view_audit'");
  });

  test('LogPanel\'s own default `tabs` array includes alerts — the actual Finding #3 fix', () => {
    // Prior to the 2026-10-13 fix, 'alerts' was present in `_ALL_TABS`
    // (the catalog of every known tab) but MISSING from the default
    // `tabs` prop value — every mount that didn't explicitly pass its
    // own `tabs=` override silently never got the Alerts tab. This is
    // a direct regression guard on that specific default-value line.
    const logPanelPath = path.resolve(__dirname, '..', 'src', 'lib', 'LogPanel.svelte');
    const src = fs.readFileSync(logPanelPath, 'utf8');
    const defaultTabsMatch = src.match(/tabs\s*=\s*\[([^\]]*)\]\s*,\s*\n\s*defaultTab/);
    expect(defaultTabsMatch, 'default `tabs` prop value must be found just above `defaultTab`').not.toBeNull();
    const defaultTabsLiteral = defaultTabsMatch[1];
    expect(defaultTabsLiteral, "LogPanel's default tabs array must include 'alerts'").toContain("'alerts'");
  });

  test('_canViewAlerts reads $userCaps/$userRole directly — no $state+$effect bridge (Finding #3, round 2)', () => {
    // Prior to the 2026-10-13 round-2 fix, `_canViewAlerts` bridged the
    // shared $userCaps/$userRole stores through an intermediate
    // $state+$effect pair that started at the wrong initial value
    // ([] / 'partner') and only corrected one render tick later — the
    // exact anti-pattern CLAUDE.md's "Reactive safety" section warns
    // against for any $derived touching a legacy store. This is a
    // source-level guard (not a DOM/behavioral one) because the one-tick
    // gap itself is not reliably observable via black-box Playwright once
    // $userCaps/$userRole have already resolved for the session — see the
    // "Fresh-mount remount guard" describe block above for the measured
    // reasoning. Confirmed to fail against the pre-fix source (temporarily
    // restored the old bridge locally, re-ran this test, saw it fail,
    // reverted) before landing this assertion.
    const logPanelPath = path.resolve(__dirname, '..', 'src', 'lib', 'LogPanel.svelte');
    const src = fs.readFileSync(logPanelPath, 'utf8');
    expect(src, '_canViewAlerts must read $userCaps/$userRole directly inside its own $derived')
      .toMatch(/const _canViewAlerts = \$derived\(hasCap\('view_audit',\s*\$userCaps,\s*\$userRole\)\)/);
    expect(src, 'the intermediate _alertsCaps $state bridge must not be reintroduced')
      .not.toContain('_alertsCaps');
    expect(src, 'the intermediate _alertsRole $state bridge must not be reintroduced')
      .not.toContain('_alertsRole');
  });
});

// ── Tab-set consistency across every real embedding site ──────────────────
// Finding #3's whole point: fixing the ONE default array fixes every site
// that doesn't already explicitly override `tabs=`. This block proves that
// end-to-end across four distinct mount points (page, card, two modal
// launch sites), not just via the source-level grep above.
test.describe('Tab-set consistency — every embedding site shows the full tab set', () => {
  test.describe.configure({ mode: 'serial' });
  test.use({ viewport: { width: 1440, height: 900 } });
  // /dashboard mounts several concurrent chart/NAV/news fetches that
  // compete with the view_audit cap check (/whoami) for bandwidth —
  // the Alerts tab only appears once that cap resolves, which has been
  // observed to take noticeably longer on this specific page than on
  // lighter pages. Generous timeout avoids a false failure; the gate
  // itself is correct (same cap check as every other mount).
  test.setTimeout(120_000);

  // Tabs guaranteed to render regardless of the live executionMode —
  // 'simulator' (labelled "Ticks") is deliberately excluded: it's gated
  // by the CURRENT executionMode store (pre-existing, unrelated to this
  // fix — see LogPanel.svelte's VISIBLE_TABS filter), so it's tolerated
  // as optional here exactly like the rest of the e2e suite already does
  // (see activity-panel.spec.ts's "Ticks/Simulator tab not present"
  // skip pattern).
  const EXPECTED_ALWAYS = ['Orders', 'Agents', 'Alerts', 'System', 'Conn', 'Console', 'News'];

  /**
   * @param {import('@playwright/test').Locator} container
   * @param {string} label
   */
  async function assertFullTabSet(container, label) {
    const wrap = container.locator('.lp-header-wrap').first();
    await expect(wrap, `[${label}] .lp-header-wrap renders`).toBeVisible({ timeout: 15_000 });
    for (const name of EXPECTED_ALWAYS) {
      await expect(
        wrap.getByRole('tab', { name, exact: true }),
        `[${label}] tab "${name}" must be present`
      ).toBeVisible({ timeout: 20_000 });
    }
  }

  test('/activity page shows all tabs including "Alerts"', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/activity', { waitUntil: 'domcontentloaded' });
    await assertFullTabSet(page.locator('.activity-page-body'), '/activity');
  });

  test('/dashboard card shows all tabs including "Alerts"', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    await assertFullTabSet(page.locator('section.dash-activity'), '/dashboard');
  });

  test('/automation bottom strip shows all tabs including "Alerts" (not "Alert Log")', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/automation', { waitUntil: 'domcontentloaded' });
    await assertFullTabSet(page.locator('body'), '/automation');
    // Explicit negative check — the interim Sprint C label must be gone.
    await expect(page.getByRole('tab', { name: 'Alert Log' }), 'the old "Alert Log" label must not render anywhere').toHaveCount(0);
  });

  test('ActivityLogModal opened from /orders bell icon shows all tabs including "Alerts"', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/orders', { waitUntil: 'domcontentloaded' });
    await page.getByRole('button', { name: 'Open Activity' }).click();
    const modal = page.locator('[role="dialog"][aria-label="Activity log"]');
    await expect(modal, 'ActivityLogModal opens from /orders bell icon').toBeVisible({ timeout: 10_000 });
    await assertFullTabSet(modal, '/orders modal');
  });

  test('ActivityLogModal opened from /admin/derivatives bell icon shows all tabs including "Alerts"', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });
    await page.getByRole('button', { name: 'Open Activity' }).click();
    const modal = page.locator('[role="dialog"][aria-label="Activity log"]');
    await expect(modal, 'ActivityLogModal opens from /admin/derivatives bell icon').toBeVisible({ timeout: 10_000 });
    await assertFullTabSet(modal, '/admin/derivatives modal');
  });
});

// ── Contextual default-active tab on ActivityLogModal open ────────────────
// Prior to this fix, ActivityLogModal always defaulted via the SHARED,
// global activityStore.activeTab (whichever tab was last viewed ANYWHERE),
// not contextual to which page's bell icon opened it. PageHeaderActions'
// new `logTabHint` prop threads a per-launch-site default through the
// pre-existing `openActivityModal(tab)` deep-link mechanism (the same one
// BrokerHealthBadge already used for 'conn').
test.describe('Contextual default-active tab — modal opened from different launch sites', () => {
  test.use({ viewport: { width: 1440, height: 900 } });
  test.setTimeout(60_000);

  test('Orders bell icon defaults to the Orders tab; Derivatives bell icon defaults to a different tab', async ({ browser }) => {
    // Two independent, freshly-authenticated contexts so neither
    // navigation's in-memory activityStore state can leak into the
    // other — each is a genuine "first open" for its own launch site.
    const ordersCtx = await browser.newContext();
    const ordersPage = await ordersCtx.newPage();
    await loginAsAdmin(ordersPage);
    await ordersPage.goto('/orders', { waitUntil: 'domcontentloaded' });
    await ordersPage.getByRole('button', { name: 'Open Activity' }).click();
    const ordersModal = ordersPage.locator('[role="dialog"][aria-label="Activity log"]');
    await expect(ordersModal, 'modal opens from /orders bell icon').toBeVisible({ timeout: 10_000 });
    const ordersActiveTab = ordersModal.getByRole('tab', { selected: true });
    await expect(ordersActiveTab, 'an active tab is marked on open').toBeVisible({ timeout: 10_000 });
    const ordersActiveLabel = (await ordersActiveTab.textContent())?.trim();
    expect(ordersActiveLabel, '/orders bell icon defaults to the Orders tab').toBe('Orders');
    await ordersCtx.close();

    const derivCtx = await browser.newContext();
    const derivPage = await derivCtx.newPage();
    await loginAsAdmin(derivPage);
    await derivPage.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });
    await derivPage.getByRole('button', { name: 'Open Activity' }).click();
    const derivModal = derivPage.locator('[role="dialog"][aria-label="Activity log"]');
    await expect(derivModal, 'modal opens from /admin/derivatives bell icon').toBeVisible({ timeout: 10_000 });
    const derivActiveTab = derivModal.getByRole('tab', { selected: true });
    await expect(derivActiveTab, 'an active tab is marked on open').toBeVisible({ timeout: 10_000 });
    const derivActiveLabel = (await derivActiveTab.textContent())?.trim();
    await derivCtx.close();

    // The actual proof: two different launch sites land on two
    // different default-active tabs, not "whatever was last viewed".
    // Derivatives deliberately hints 'agent' (not the thematically
    // closer 'alerts') — see that page's own PageHeaderActions mount
    // comment: seeding 'alerts' loses a real, pre-existing race against
    // LogPanel's async view_audit cap check on every cold page load.
    expect(derivActiveLabel, 'Derivatives bell icon must default to a DIFFERENT tab than Orders').not.toBe(ordersActiveLabel);
    expect(derivActiveLabel, 'Derivatives bell icon contextual default is Agents').toBe('Agents');
  });

  test('generic open (keyboard `h`, no launch-site hint) preserves the remembered tab instead of resetting', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    // Open via the generic keyboard shortcut, switch to a non-default
    // tab, close, then reopen via the SAME generic path — the tab must
    // still be the one the operator switched to (no launch-site hint
    // means activityStore.activeTab's remembered-tab convenience must
    // be fully preserved, unchanged from before this fix).
    await page.locator('body').click({ position: { x: 400, y: 100 } });
    await page.keyboard.press('h');
    let modal = page.locator('[role="dialog"][aria-label="Activity log"]');
    await expect(modal).toBeVisible({ timeout: 10_000 });
    await modal.getByRole('tab', { name: 'Conn', exact: true }).click();
    await expect(modal.getByRole('tab', { name: 'Conn', exact: true, selected: true })).toBeVisible({ timeout: 5_000 });
    await page.keyboard.press('Escape');
    await expect(modal).toBeHidden({ timeout: 5_000 });

    await page.keyboard.press('h');
    modal = page.locator('[role="dialog"][aria-label="Activity log"]');
    await expect(modal).toBeVisible({ timeout: 10_000 });
    const reopenedActiveTab = modal.getByRole('tab', { selected: true });
    await expect(reopenedActiveTab, 'remembered tab persists across a generic (no-hint) re-open').toHaveText('Conn');
  });
});

// ── Fresh-mount remount guard (Finding #3, round 2, 2026-10-13) ───────────
// `_canViewAlerts` (LogPanel.svelte) used to bridge the shared
// $userCaps/$userRole stores through an intermediate $state+$effect pair
// that started at the wrong initial value ([] / 'partner') and only
// corrected one render tick later — the exact anti-pattern CLAUDE.md's
// "Reactive safety" section warns against for any $derived touching a
// legacy store. Fixed by reading $userCaps/$userRole directly inside the
// $derived (no intermediate $state) — see LogPanel.svelte's own comment
// above `_canViewAlerts` for the full writeup.
//
// IMPORTANT, measured empirically while writing this test: this specific
// one-tick gap is NOT independently observable via black-box Playwright
// once $userCaps/$userRole have already resolved for the session — this
// test (run against BOTH the old bridge and the new direct-read code,
// manually, to confirm) passes identically either way once /whoami has
// settled, because Svelte flushes the correcting $effect before anything
// can read the stale intermediate frame. This test is therefore a
// behavioral guard that Alerts stays present AND selected across repeated
// fresh remounts (never regresses), NOT a red/green discriminator between
// the old bridge and the fix — see the `[STALE]` source-scan block below
// for the actual red/green proof that the bridge pattern is gone.
test.describe('Fresh-mount remount guard — Alerts tab stays present and selected', () => {
  test.setTimeout(90_000);

  test('reopening a FRESH ActivityLogModal with Alerts as the remembered tab keeps Alerts selected, every time, 3 reopens in a row', async ({ page }) => {
    await loginAsAdmin(page); // rambo — admin role, holds view_audit
    // /pulse's PageHeaderActions carries no `logTabHint` override (unlike
    // /orders → "order", which forces the Orders tab on EVERY open by
    // design — see PageHeaderActions.svelte / stores.js:openActivityModal
    // — or /admin/derivatives → "agent"), so its "Open Activity" button
    // always calls openActivityModal() with no arg — a generic open that
    // preserves whatever tab is already remembered in activityStore,
    // instead of forcing a specific one on every click. Using /orders
    // here would make this test assert the wrong thing (every reopen
    // would correctly land on "Orders", not "Alerts", regardless of the
    // fix).
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    // Seed the remembered tab to 'alerts' once via a real open → click →
    // close cycle, matching how an operator would actually land here.
    // From this point on, every open passes defaultTab='alerts' into a
    // brand-new LogPanel instance (ActivityLogModal fully unmounts on
    // close, confirmed via ActivityLogModal.svelte).
    await page.getByRole('button', { name: 'Open Activity' }).click();
    let modal = page.locator('[role="dialog"][aria-label="Activity log"]');
    await expect(modal).toBeVisible({ timeout: 10_000 });
    await modal.getByRole('tab', { name: 'Alerts', exact: true }).click();
    await expect(modal.getByRole('tab', { name: 'Alerts', exact: true, selected: true })).toBeVisible({ timeout: 5_000 });
    await page.keyboard.press('Escape');
    await expect(modal).toBeHidden({ timeout: 5_000 });

    // Repeat open/close 3 times — every single fresh mount must land on
    // Alerts, both present AND still the SELECTED tab. LogPanel's own
    // force-flip effect (fallback to 'agent' when !_canViewAlerts) is
    // one-way and never self-heals once triggered, so if a regression
    // ever reintroduces a real resolution lag, a flipped tab stays
    // flipped for that mount's whole life rather than recovering a
    // moment later — Playwright's own retrying `expect` cannot mask
    // that kind of permanent-not-transient failure.
    for (let i = 0; i < 3; i++) {
      await page.getByRole('button', { name: 'Open Activity' }).click();
      modal = page.locator('[role="dialog"][aria-label="Activity log"]');
      await expect(modal, `reopen #${i + 1}: modal visible`).toBeVisible({ timeout: 10_000 });

      const alertsTab = modal.getByRole('tab', { name: 'Alerts', exact: true });
      await expect(alertsTab, `reopen #${i + 1}: Alerts tab present`).toBeVisible({ timeout: 10_000 });

      const activeTab = modal.getByRole('tab', { selected: true });
      await expect(activeTab, `reopen #${i + 1}: Alerts stays the SELECTED tab (not force-flipped to Agents)`).toHaveText('Alerts', { timeout: 5_000 });

      await page.keyboard.press('Escape');
      await expect(modal, `reopen #${i + 1}: modal closes`).toBeHidden({ timeout: 5_000 });
    }
  });
});

// ── Width consistency — bottom strip vs modal ──────────────────────────────
// Finding #4: the strip (docked, embedded panel) and the modal (dialog)
// are structurally different containers by design, but the shared
// LogPanel/tab-bar CONTENT inside each should render at the same width.
// Root cause (confirmed via direct bounding-box measurement before this
// fix): ActivityLogModal's `.alm-body` imposed its own 0.6rem horizontal
// padding, diverging from `.algo-content`'s site-wide 0.5rem gutter that
// /automation's strip inherits — a real, measurable ~3.2px (0.2rem)
// narrower content width in the modal. Fixed by matching `.alm-body`'s
// horizontal padding to the 0.5rem site convention.
test.describe('Width consistency — strip vs modal LogPanel content', () => {
  test.use({ viewport: { width: 1440, height: 900 } });
  test.setTimeout(60_000);

  test('/automation strip and ActivityLogModal render the same .lp-header-wrap content width', async ({ page }) => {
    await loginAsAdmin(page);

    await page.goto('/automation', { waitUntil: 'domcontentloaded' });
    const stripWrap = page.locator('.lp-header-wrap').first();
    await expect(stripWrap).toBeVisible({ timeout: 15_000 });
    const stripBox = await stripWrap.boundingBox();
    expect(stripBox, 'strip .lp-header-wrap has a bounding box').not.toBeNull();

    await page.goto('/orders', { waitUntil: 'domcontentloaded' });
    await page.getByRole('button', { name: 'Open Activity' }).click();
    const modal = page.locator('[role="dialog"][aria-label="Activity log"]');
    await expect(modal).toBeVisible({ timeout: 10_000 });
    const modalWrap = modal.locator('.lp-header-wrap').first();
    await expect(modalWrap).toBeVisible({ timeout: 10_000 });
    const modalBox = await modalWrap.boundingBox();
    expect(modalBox, 'modal .lp-header-wrap has a bounding box').not.toBeNull();

    // Both are full-viewport-width containers (same 1440px viewport in
    // this test) minus their own horizontal gutter — after the fix
    // those gutters match, so the rendered content widths should be
    // equal within a couple of px of sub-pixel/scrollbar rounding.
    const delta = Math.abs((stripBox?.width ?? 0) - (modalBox?.width ?? 0));
    expect(delta, `strip width ${stripBox?.width} vs modal width ${modalBox?.width} — delta ${delta}px`).toBeLessThanOrEqual(2);
  });
});
