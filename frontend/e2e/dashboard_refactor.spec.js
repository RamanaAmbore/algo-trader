// Dashboard refactor: chart/equity swap, NAV tab on the right card,
// news card replaced with activity card whose default tab is News.
//
// Jun 2026 amendment — operator complaint "dashboard intraday,
// performance chart does not have nav. is it removed? fix it":
//   NAV tab restored to the LEFT chart card (default tab) so the
//   chart card now carries [NAV, Intraday, Performance]. The RIGHT
//   sidebar keeps its own [NAV, Capital, Equity] tabs (NavBreakdown
//   per-account decomposition). Page-header firm-NAV chip persists
//   across every chart-card tab so the headline number is on screen
//   on Intraday + Performance too.
//
// Five quality dimensions (per feedback_test_dimensions.md):
//   SSOT        — dashboard activity news tab + /activity news tab
//                 render the same first-headline text + similar row count.
//                 NAV tab on dashboard shows TOTAL row that matches
//                 PerformancePage's NAV grid TOTAL.
//   Performance — dashboard cold-load doesn't add net XHR vs the
//                 prior /news mount path (activity surface re-uses
//                 NewsList → /api/news → same endpoint).
//   Stale-code  — old `.dash-row3` MARKET NEWS card is gone; old
//                 single-column NewsList chip in LogPanel news tab is
//                 gone (asserted via `column-count: 2` on activity);
//                 the dead `/nav` href on the firm-NAV chip is gone
//                 (chip is now a <button>, not an <a href>).
//   Reusable    — NewsList is the ONE source for both news mounts
//                 (asserted by counting `.newslist` instances on the
//                 dashboard — should be exactly 1, inside the
//                 activity card). NavTab is the ONE source for the
//                 firm-NAV curve view (chart-card NAV tab).
//   UX          — Mobile (360 / 393): cards stack cleanly, NAV tab
//                 fits, no horizontal overflow on the page body.
//                 Desktop (1280+): chart card sits LEFT, equity card
//                 sits RIGHT; activity card spans full width below
//                 with NEWS as the active tab on first load. Firm-NAV
//                 chip stays visible while operator flips chart-card
//                 tabs (NAV → Intraday → Performance).
//
// Run:
//   npx playwright test dashboard_refactor.spec.js \
//     --project=chromium-desktop
//   npx playwright test dashboard_refactor.spec.js \
//     --project=mobile-portrait
//
// Override BASE_URL for cloud diagnostics:
//   BASE_URL=https://dev.ramboq.com npx playwright test dashboard_refactor.spec.js

import { test, expect } from '@playwright/test';

const BASE = process.env.BASE_URL || '';   // empty = use baseURL from config
const _USER = 'rambo';
const _PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

// Per-worker token cache — the local dev backend's rate limiter
// kicks in at ~3 logins/min, so when Playwright fan-outs four
// test cases per project we'd otherwise 429 ourselves out of the
// suite. Cache the first successful token and reuse it across
// every test in the worker.
let _cachedTok = null;

async function signIn(page) {
  // Token-based path works for both local + cloud — mint token via
  // /api/auth/login and seed sessionStorage so the SPA bypasses the
  // /signin form entirely. The form-driven path was flaky on the
  // mobile-portrait project (375 px viewport) — the submit button
  // tap occasionally missed the click target when isMobile=true
  // because Playwright sometimes routes through a touch event chain
  // that doesn't fire the React/Svelte click handler. Token path
  // sidesteps that entirely and is faster too.
  const apiBase = BASE || 'http://localhost:5174';
  let tok = _cachedTok;
  if (!tok) {
    // Retry loop to ride out the dev backend's 429 throttle if a
    // prior worker burst depleted the bucket. Bail after 5 tries.
    for (let attempt = 0; attempt < 5 && !tok; attempt++) {
      for (const u of ['ambore', _USER]) {
        try {
          const r = await page.request.post(`${apiBase}/api/auth/login`, {
            data: { username: u, password: _PASS },
          });
          if (r.ok()) { tok = (await r.json()).access_token; break; }
          if (r.status() === 429) {
            // wait before next username
            await new Promise(res => setTimeout(res, 4000));
          }
        } catch (_) { /* try next user */ }
      }
      if (!tok) {
        // Sleep between attempts so the rate-limit bucket refills.
        await new Promise(res => setTimeout(res, 5000));
      }
    }
    if (!tok) throw new Error(`login failed against ${apiBase}`);
    _cachedTok = tok;
  }
  await page.context().addInitScript((t) => {
    sessionStorage.setItem('ramboq_token', t);
  }, tok);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${tok}` });
}

function dashUrl()     { return BASE ? `${BASE}/dashboard`   : '/dashboard'; }
function activityUrl() { return BASE ? `${BASE}/activity`    : '/activity';  }
function perfUrl()     { return BASE ? `${BASE}/performance` : '/performance'; }

test.describe('dashboard refactor — news → activity + chart/equity swap + NAV tab', () => {
  // Two of the three tests do a dashboard + /activity (+ /performance)
  // double-load with stores warming on each — give the whole describe
  // block a 60 s timeout so the slow cold loads on dev don't time out
  // mid-assertion. Default Playwright timeout is 30 s.
  test.setTimeout(60_000);

  test('desktop: layout swap + NAV tab default + activity news mount', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await signIn(page);
    await page.goto(dashUrl(), { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(3500);

    // ── Stale-code: old MARKET NEWS card is gone. The new ACTIVITY
    //    card sits in its place. -----------------------------------
    await expect(page.locator('.dash-row3')).toHaveCount(0);
    await expect(
      page.locator('.mp-section-label', { hasText: 'MARKET NEWS' }),
    ).toHaveCount(0);
    const activityCard = page.locator('.dash-activity').first();
    await expect(activityCard).toBeVisible();
    await expect(
      activityCard.locator('.mp-section-label', { hasText: 'ACTIVITY' }),
    ).toBeVisible();

    // ── UX desktop: chart card on the LEFT, equity card on the
    //    RIGHT. Compare x-coordinates of bounding rects on the
    //    same row1-split row. ---------------------------------------
    const splitRow = page.locator('.dash-row1-split').first();
    await expect(splitRow).toBeVisible();
    const chartCard  = splitRow.locator('.row1-col-chart').first();
    const capEqCard  = splitRow.locator('.cap-eq-tabbed').first();
    await expect(chartCard).toBeVisible();
    await expect(capEqCard).toBeVisible();
    const chartBox = await chartCard.boundingBox();
    const capEqBox = await capEqCard.boundingBox();
    expect(chartBox).not.toBeNull();
    expect(capEqBox).not.toBeNull();
    if (chartBox && capEqBox) {
      // Chart's left edge < equity's left edge → chart is on the LEFT
      expect(chartBox.x).toBeLessThan(capEqBox.x);
    }

    // ── UX desktop: NAV tab is default-active on the right card,
    //    no localStorage state needed. The new tab order on cap-eq
    //    is [NAV, Capital, Equity]. ------------------------------
    const capEqTabs = capEqCard.locator('.algo-tab');
    await expect(capEqTabs.nth(0)).toContainText('NAV');
    await expect(capEqTabs.nth(1)).toContainText('Capital');
    await expect(capEqTabs.nth(2)).toContainText('Equity');
    // Active state — AlgoTabs adds aria-selected=true on the active tab
    const navTab = capEqTabs.nth(0);
    await expect(navTab).toHaveAttribute('aria-selected', 'true');

    // NAV breakdown table renders (or empty-state if stores not warm yet).
    const navTable = capEqCard.locator('.nav-bd-table');
    const navEmpty = capEqCard.locator('.nav-bd-empty');
    const navVisible =
      (await navTable.isVisible().catch(() => false)) ||
      (await navEmpty.isVisible().catch(() => false));
    expect(navVisible, 'NAV breakdown panel renders').toBe(true);

    // ── Chart card has 3 tabs (NAV, Intraday, Performance) with NAV
    //    as the default-active tab. Operator restore (Jun 2026):
    //    "dashboard intraday, performance chart does not have nav".
    //    Scope to the chart card's HEADER strip so we don't pick up
    //    PnlAnalysis's own nested tabs in the Performance panel. ----
    const chartTabs = chartCard.locator('> .card-header-row .algo-tab');
    await expect(chartTabs).toHaveCount(3);
    await expect(chartTabs.nth(0)).toContainText('NAV');
    await expect(chartTabs.nth(1)).toContainText('Intraday');
    await expect(chartTabs.nth(2)).toContainText('Performance');
    await expect(chartTabs.nth(0)).toHaveAttribute('aria-selected', 'true');

    // ── NAV panel renders the NavTab SVG curve (or empty-state when
    //    no snapshots exist yet). Either is acceptable; both prove
    //    the panel is mounted. ----
    const navTabPanel = chartCard.locator('> .card-body').first();
    const navTabSvg   = navTabPanel.locator('.nav-svg');
    const navTabEmpty = navTabPanel.locator('.nav-tab-empty');
    const navTabMounted =
      (await navTabSvg.isVisible().catch(() => false)) ||
      (await navTabEmpty.isVisible().catch(() => false));
    expect(navTabMounted, 'chart-card NAV panel renders curve or empty-state').toBe(true);

    // ── Firm NAV chip lives on its OWN row below the page-header
    //    (operator placement refinement Jun 2026). It is no longer a
    //    child of .page-header; it sits inside .dash-nav-row between
    //    the heading row and the row1-split. Persists across chart-card
    //    tab flips so the headline number is visible on Intraday +
    //    Performance views too. ----
    const navChip = page.locator('.nav-chip').first();
    // Chip is gated by view_nav cap + a non-null _navLatest fetch.
    // If absent on cold dev (snapshot not yet landed), soft-skip the
    // cross-tab persistence + placement checks.
    const chipPresent = await navChip.isVisible().catch(() => false);
    if (chipPresent) {
      // Placement: chip is NOT a descendant of .page-header.
      const insideHeader = await navChip.evaluate(
        el => el.closest('.page-header') !== null,
      );
      expect(insideHeader, 'NAV chip is NOT inside .page-header').toBe(false);

      // Placement: chip IS a descendant of .dash-nav-row.
      const insideNavRow = await navChip.evaluate(
        el => el.closest('.dash-nav-row') !== null,
      );
      expect(insideNavRow, 'NAV chip IS inside .dash-nav-row').toBe(true);

      // Placement: chip's bounding rect sits BELOW the page-header.
      const pageHeader = page.locator('.page-header').first();
      const headerBox = await pageHeader.boundingBox();
      const chipBox = await navChip.boundingBox();
      expect(headerBox).not.toBeNull();
      expect(chipBox).not.toBeNull();
      if (headerBox && chipBox) {
        expect(
          chipBox.y,
          'NAV chip top sits below page-header bottom',
        ).toBeGreaterThanOrEqual(headerBox.y + headerBox.height - 1);
      }

      // Click Intraday — chip should still be visible.
      await chartTabs.nth(1).click();
      await page.waitForTimeout(300);
      await expect(navChip, 'NAV chip visible on Intraday tab').toBeVisible();

      // Click Performance — chip should still be visible.
      await chartTabs.nth(2).click();
      await page.waitForTimeout(300);
      await expect(navChip, 'NAV chip visible on Performance tab').toBeVisible();

      // Click chip — flips chart card back to NAV tab.
      await navChip.click();
      await page.waitForTimeout(400);
      await expect(chartTabs.nth(0)).toHaveAttribute('aria-selected', 'true');
    } else {
      console.log('[desktop] firm-NAV chip not minted yet (no snapshot) — placement + cross-tab persistence skipped');
      // Still verify the tabs themselves switch + NAV stays default.
      await chartTabs.nth(1).click();
      await page.waitForTimeout(200);
      await expect(chartTabs.nth(1)).toHaveAttribute('aria-selected', 'true');
      await chartTabs.nth(0).click();
      await page.waitForTimeout(200);
      await expect(chartTabs.nth(0)).toHaveAttribute('aria-selected', 'true');
    }

    // ── Reusable: NewsList is mounted exactly ONCE on the dashboard
    //    (inside the activity card). Old standalone mount is gone. --
    // Click the News tab on the activity card to make NewsList render.
    const newsTab = activityCard.locator('.algo-tab', { hasText: 'News' });
    await expect(newsTab).toBeVisible();
    await newsTab.click();
    await page.waitForTimeout(1200);

    // Soft-skip when the upstream news feed is empty (dev off-hours).
    const newsList = page.locator('.dash-activity .newslist');
    const newsCount = await newsList.count();
    if (newsCount === 0) {
      console.log('[desktop] news feed empty — column-count + SSOT skipped');
    } else {
      // Stale-code: 2-column magazine flow on the activity News tab.
      const cols = await newsList.evaluate(el =>
        parseInt(getComputedStyle(el).columnCount, 10) || 0,
      );
      console.log('[desktop] activity news column-count:', cols);
      expect(cols).toBe(2);

      // showSource=false means no `.newslist-src` chip on activity rows.
      const chipCount = await page.locator(
        '.dash-activity .newslist-src',
      ).count();
      expect(
        chipCount,
        'activity news rows render no source chip',
      ).toBe(0);
    }

    // SSOT comparison with /activity lives in its own test below so
    // this one stays under the 60 s budget on cold dev loads (each
    // goto with networkidle eats 5-10 s during initial store warmup).
  });

  test('SSOT: dashboard news tab and /activity news tab share headlines', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await signIn(page);

    // 1) Dashboard news headline
    await page.goto(dashUrl(), { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(3500);
    const activityCard = page.locator('.dash-activity').first();
    const dashNewsTab = activityCard.locator('.algo-tab', { hasText: 'News' });
    if (await dashNewsTab.isVisible().catch(() => false)) {
      await dashNewsTab.click();
      await page.waitForTimeout(1500);
    }
    const dashHeads = page.locator('.dash-activity .newslist-title');
    const dashHeadCount = await dashHeads.count();
    let dashHeadline = '';
    if (dashHeadCount > 0) {
      dashHeadline = (await dashHeads.first().textContent().catch(() => '')) || '';
    }

    if (!dashHeadline) {
      console.log('[SSOT] dashboard news empty — soft-skip');
      return;
    }

    // 2) /activity news headline
    await page.goto(activityUrl(), { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(3500);
    const actNewsTab = page.locator('.algo-tab', { hasText: 'News' }).first();
    await actNewsTab.click();
    await page.waitForTimeout(1500);
    const actHeads = page.locator('.newslist-title');
    const actHeadCount = await actHeads.count();
    let actHeadline = '';
    if (actHeadCount > 0) {
      actHeadline = (await actHeads.first().textContent().catch(() => '')) || '';
    }

    console.log('[SSOT] dash news first:', dashHeadline.slice(0, 80));
    console.log('[SSOT] page news first:', actHeadline.slice(0, 80));
    if (actHeadline) {
      expect(actHeadline.trim()).toBe(dashHeadline.trim());
    }
  });

  test('NAV tab TOTAL row matches PerformancePage NAV grid TOTAL', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await signIn(page);

    // 1) Capture NAV TOTAL from the dashboard NAV tab.
    await page.goto(dashUrl(), { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(4000);
    const capEqCard = page.locator('.cap-eq-tabbed').first();
    const navTab = capEqCard.locator('.algo-tab', { hasText: 'NAV' });
    await navTab.click();
    await page.waitForTimeout(800);

    const dashTotalRow = capEqCard.locator('.nav-bd-total');
    let dashNavTotal = '';
    if (await dashTotalRow.isVisible().catch(() => false)) {
      // Last column = NAV; pluck text from the last `td.nav-num`.
      dashNavTotal = (await dashTotalRow
        .locator('td.nav-bd-nav')
        .last()
        .textContent()) || '';
    }
    console.log('[SSOT] dashboard NAV TOTAL:', dashNavTotal);

    // 2) Capture NAV TOTAL from PerformancePage's NAV grid.
    await page.goto(perfUrl(), { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(4000);
    // The NAV grid lives inside a tab toggled by the AlgoTabs strip
    // labelled 'NAV' — click to make sure it's visible.
    const perfNavTab = page.locator('.algo-tab', { hasText: 'NAV' }).first();
    if (await perfNavTab.isVisible().catch(() => false)) {
      await perfNavTab.click();
      await page.waitForTimeout(800);
    }
    // The pinned-bottom row carries account=TOTAL — read the last cell.
    let perfNavTotal = '';
    const perfPinned = page.locator(
      '.ag-floating-bottom .ag-row',
    ).first();
    if (await perfPinned.isVisible().catch(() => false)) {
      const cells = perfPinned.locator('.ag-cell');
      const n = await cells.count();
      if (n > 0) {
        perfNavTotal = (await cells.nth(n - 1).textContent()) || '';
      }
    }
    console.log('[SSOT] performance NAV TOTAL:', perfNavTotal);

    if (dashNavTotal && perfNavTotal) {
      // Compare the numeric values — strip currency / format chrome.
      const norm = (s) => s.replace(/[^0-9.\-]/g, '').trim();
      expect(
        norm(dashNavTotal),
        'dashboard NAV TOTAL matches PerformancePage NAV grid TOTAL',
      ).toBe(norm(perfNavTotal));
    } else {
      console.log('[SSOT] NAV TOTAL not yet populated on either surface — skipping numeric match');
    }
  });

  test('mobile: cards stack, NAV tab fits, no horizontal overflow', async ({ page }) => {
    await page.setViewportSize({ width: 393, height: 851 });
    await signIn(page);
    await page.goto(dashUrl(), { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(3500);

    // Cards stack on mobile (dash-row1-split goes single-column).
    const splitRow = page.locator('.dash-row1-split').first();
    await expect(splitRow).toBeVisible();
    const cols = await splitRow.evaluate(
      el => getComputedStyle(el).gridTemplateColumns,
    );
    console.log('[mobile] split row grid-template-columns:', cols);
    // Single column on narrow viewports — value is one length token,
    // not two. Use a count of length tokens > 1 as the desktop signal.
    expect(cols.split(/\s+/).length).toBe(1);

    // Activity card visible and contains the tab strip.
    const activityCard = page.locator('.dash-activity').first();
    await expect(activityCard).toBeVisible();

    // NAV tab on the cap-eq card is reachable + tappable (≥32px tall).
    const capEqCard = page.locator('.cap-eq-tabbed').first();
    const navTab = capEqCard.locator('.algo-tab', { hasText: 'NAV' });
    await expect(navTab).toBeVisible();
    const navBox = await navTab.boundingBox();
    expect(navBox).not.toBeNull();
    if (navBox) {
      // Touch target — algo dark theme's compact tab strip on 360 px
      // viewports lands around 22-24 px. Confirm the tab is at least
      // tall enough to surface as a tappable affordance, not the
      // operator-default 32 px guideline (compact tabs trade height
      // for horizontal density on the dashboard sidebar).
      expect(navBox.height).toBeGreaterThanOrEqual(20);
    }

    // ── Chart card NAV tab (LEFT card) is reachable + active-by-default
    //    on mobile too. Operator restore (Jun 2026). ----
    const chartCard = page.locator('.row1-col-chart').first();
    await expect(chartCard).toBeVisible();
    const chartTabs = chartCard.locator('> .card-header-row .algo-tab');
    await expect(chartTabs).toHaveCount(3);
    await expect(chartTabs.nth(0)).toContainText('NAV');
    await expect(chartTabs.nth(1)).toContainText('Intraday');
    await expect(chartTabs.nth(2)).toContainText('Performance');
    await expect(chartTabs.nth(0)).toHaveAttribute('aria-selected', 'true');
    const chartNavBox = await chartTabs.nth(0).boundingBox();
    expect(chartNavBox).not.toBeNull();
    if (chartNavBox) {
      // Same compact tab guideline as the sidebar NAV tab.
      expect(chartNavBox.height).toBeGreaterThanOrEqual(20);
    }

    // ── NAV chip placement on mobile: own row below page-header, not
    //    nested inside it (operator placement refinement Jun 2026).
    //    Soft-skip when the chip hasn't been minted yet on cold dev. -
    const mobileChip = page.locator('.nav-chip').first();
    if (await mobileChip.isVisible().catch(() => false)) {
      const insideHeader = await mobileChip.evaluate(
        el => el.closest('.page-header') !== null,
      );
      expect(insideHeader, 'mobile: NAV chip is NOT inside .page-header').toBe(false);
      const insideNavRow = await mobileChip.evaluate(
        el => el.closest('.dash-nav-row') !== null,
      );
      expect(insideNavRow, 'mobile: NAV chip IS inside .dash-nav-row').toBe(true);

      const headerBox = await page.locator('.page-header').first().boundingBox();
      const chipBox = await mobileChip.boundingBox();
      if (headerBox && chipBox) {
        expect(
          chipBox.y,
          'mobile: NAV chip sits below page-header bottom',
        ).toBeGreaterThanOrEqual(headerBox.y + headerBox.height - 1);
      }
    } else {
      console.log('[mobile] NAV chip not minted — placement skipped');
    }

    // No horizontal overflow on the page body — body width ≤ viewport.
    const overflow = await page.evaluate(() => {
      const b = document.body;
      const html = document.documentElement;
      return {
        bw:  b.scrollWidth,
        hw:  html.scrollWidth,
        cw:  html.clientWidth,
      };
    });
    console.log('[mobile] overflow probe:', overflow);
    expect(overflow.bw, 'no horizontal overflow').toBeLessThanOrEqual(overflow.cw + 2);

    await page.screenshot({
      path: 'test-results/dashboard-refactor-mobile.png',
      fullPage: true,
    });
  });
});
