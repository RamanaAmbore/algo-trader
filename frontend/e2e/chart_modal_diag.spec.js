/**
 * chart_modal_diag.spec.js
 *
 * Diagnostic spec for three user-reported failures:
 *   (1) Chart modal X (close) button doesn't close the modal
 *   (2) Chart refresh button doesn't trigger a refresh
 *   (3) Pulse not opening chart for symbol in Pinned watchlist
 *
 * Target: https://dev.ramboq.com (rambo user has empty watchlist by default,
 * so the spec adds NIFTY to Default first via API).
 *
 * Run:
 *   cd frontend && PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/chart_modal_diag.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test } from '@playwright/test';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';
const API_HOST = BASE.includes('localhost') ? 'https://dev.ramboq.com' : BASE;

let _cachedToken = null;

async function login(page) {
  if (!_cachedToken) {
    for (const u of ['ambore', 'rambo', 'admin']) {
      const r = await page.request.post(`${API_HOST}/api/auth/login`, {
        data: { username: u, password: _AUTH_PASS },
        timeout: 15_000,
      }).catch(() => null);
      if (r && r.ok()) { _cachedToken = (await r.json()).access_token; break; }
    }
    if (!_cachedToken) throw new Error(`login failed`);
  }
  await page.context().addInitScript((t) => {
    sessionStorage.setItem('ramboq_token', t);
  }, _cachedToken);
}

/** Seed NIFTY into the Default pinned watchlist so the Pulse row exists. */
async function seedDefaultWatchlist(page) {
  // Discover the Default watchlist id
  const wl = await page.request.get(`${API_HOST}/api/watchlist/`, {
    headers: { Authorization: `Bearer ${_cachedToken}` },
    timeout: 15_000,
  });
  if (!wl.ok()) throw new Error(`GET /api/watchlist/ → ${wl.status()}`);
  const lists = await wl.json();
  // The endpoint returns { watchlists: [...] }
  const arr = Array.isArray(lists) ? lists : (lists.watchlists || []);
  const def = arr.find((w) => w.is_default || w.name === 'Default' || w.name === 'default');
  if (!def) throw new Error(`No Default watchlist; got ${JSON.stringify(arr).slice(0, 200)}`);
  // Add NIFTY 50 spot — use a real index quote key shape
  await page.request.post(`${API_HOST}/api/watchlist/${def.id}/items`, {
    headers: { Authorization: `Bearer ${_cachedToken}` },
    data: { tradingsymbol: 'NIFTY 50', exchange: 'NSE' },
    timeout: 15_000,
  }).catch(() => null); // 409 if already present — fine
}

function attachLogs(page, log) {
  page.on('console', (m) => {
    if (m.type() === 'error') log.consoleErrors.push(`[err] ${m.text().slice(0, 200)}`);
  });
  page.on('pageerror', (e) => log.pageErrors.push(String(e).slice(0, 200)));
  page.on('response', (r) => {
    const u = r.url();
    if (/\/api\/(charts|quote|options)/.test(u) && r.status() >= 400) {
      log.apiErrors.push(`${r.status()} ${r.request().method()} ${u.slice(-90)}`);
    }
  });
}

test.describe('Chart modal + refresh + pulse diagnosis', () => {
  test.setTimeout(120_000);

  test('Issue #2: refresh button on /charts page triggers API calls', async ({ page }) => {
    const log = { consoleErrors: [], pageErrors: [], apiErrors: [], timeline: [] };
    attachLogs(page, log);

    await login(page);
    log.timeline.push('navigate to /charts?symbol=NIFTY&mode=live');
    await page.goto(`${BASE}/charts?symbol=NIFTY&mode=live`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(4500);

    // Probe page-header refresh button — title="Refresh" or aria-label
    const refresh = page.locator('header button[title*="efresh" i], .page-header button[title*="efresh" i], button[aria-label*="efresh" i]').first();
    const refreshCount = await refresh.count();
    log.timeline.push(`refresh button candidates: ${refreshCount}`);

    if (refreshCount === 0) {
      log.timeline.push('FAIL: no refresh button found on /charts page');
      const bodyTextStart = (await page.locator('body').innerText()).slice(0, 500);
      log.timeline.push(`body text start: ${bodyTextStart.replace(/\n/g, ' | ')}`);
      console.log(JSON.stringify(log, null, 2));
      return;
    }

    const refreshTitle = await refresh.getAttribute('title');
    log.timeline.push(`first refresh button title="${refreshTitle}"`);

    // Capture chart API calls AFTER click
    const chartCalls = [];
    const respHandler = (r) => {
      const u = r.url();
      if (/\/api\/(charts|quote|options)/.test(u)) {
        chartCalls.push(`${r.status()} ${r.request().method()} ${u.slice(u.indexOf('/api/'))}`);
      }
    };
    page.on('response', respHandler);

    log.timeline.push('clicking refresh button');
    await refresh.click();
    await page.waitForTimeout(4000);
    page.off('response', respHandler);

    log.timeline.push(`API calls during 4s post-click window: ${chartCalls.length}`);
    chartCalls.slice(0, 10).forEach((c) => log.timeline.push(`  ${c}`));

    if (chartCalls.length === 0) {
      log.timeline.push('❌ BUG CONFIRMED (Issue #2): refresh click made zero /api/charts /api/quote /api/options calls');
    } else {
      log.timeline.push(`✓ Refresh triggered ${chartCalls.length} API calls`);
    }

    console.log('\n=== Issue #2 log ===');
    console.log(JSON.stringify(log, null, 2));
  });

  test('Issue #1: ChartModal X button + #3: open chart from Pulse pinned row', async ({ page }) => {
    const log = { consoleErrors: [], pageErrors: [], apiErrors: [], timeline: [] };
    attachLogs(page, log);

    await login(page);
    log.timeline.push('seeding NIFTY 50 into Default watchlist via API');
    await seedDefaultWatchlist(page).catch((e) => log.timeline.push(`seed failed: ${e.message}`));

    log.timeline.push('navigate to /pulse');
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(6000);

    // Verify we're on PINNED tab
    const pinnedTab = page.locator('button:has-text("PINNED"), [class*="toptab"]:has-text("PINNED")').first();
    if (await pinnedTab.count() > 0) {
      const isOn = await pinnedTab.evaluate((el) => /on|active/i.test(el.className));
      log.timeline.push(`Pinned tab present, active=${isOn}`);
      if (!isOn) await pinnedTab.click().catch(() => {});
      await page.waitForTimeout(800);
    }

    // Look for at least one row with symbol = NIFTY 50
    const niftyRow = page.locator('.ag-row').filter({ hasText: 'NIFTY' }).first();
    const rowCount = await niftyRow.count();
    log.timeline.push(`NIFTY rows in DOM: ${rowCount}`);

    if (rowCount === 0) {
      const allRows = await page.locator('.ag-row').count();
      log.timeline.push(`Total ag-rows visible: ${allRows}`);
      if (allRows === 0) {
        log.timeline.push('FAIL: Pinned grid is empty even after seed — sparkline cache may not have warmed yet');
        console.log(JSON.stringify(log, null, 2));
        return;
      }
    }

    // Click sym-actions ⋯ on first NIFTY row to open context menu
    const symActions = niftyRow.locator('.sym-actions').first();
    const actionsCount = await symActions.count();
    log.timeline.push(`.sym-actions in NIFTY row: ${actionsCount}`);

    if (actionsCount === 0) {
      log.timeline.push('FAIL: .sym-actions (⋯) not present in row');
      console.log(JSON.stringify(log, null, 2));
      return;
    }

    log.timeline.push('dispatching click via evaluate (CSS :hover opacity bypass)');
    const dispatched = await symActions.evaluate((el) => {
      const ev = new MouseEvent('click', { bubbles: true, cancelable: true, view: window });
      el.dispatchEvent(ev);
      return true;
    });
    log.timeline.push(`dispatched=${dispatched}`);
    await page.waitForTimeout(800);

    // Context menu should appear with "Chart →" item
    const chartItem = page.locator('button.ctx-item:has-text("Chart"), [role="menuitem"]:has-text("Chart")').first();
    const ciCount = await chartItem.count();
    log.timeline.push(`"Chart →" context-menu items: ${ciCount}`);

    if (ciCount === 0) {
      log.timeline.push('❌ BUG CONFIRMED (Issue #3a): ⋯ button did not open a context menu with Chart option');
      await page.screenshot({ path: 'test-results/pulse-no-ctx.png', fullPage: true });
      console.log(JSON.stringify(log, null, 2));
      return;
    }

    log.timeline.push('clicking "Chart →" in context menu');
    await chartItem.click();
    await page.waitForTimeout(2500);

    const modal = page.locator('.cm-overlay');
    const modalCount = await modal.count();
    const modalVisible = modalCount > 0 ? await modal.isVisible() : false;
    log.timeline.push(`After Chart click — .cm-overlay count=${modalCount}, visible=${modalVisible}`);

    if (modalCount === 0 || !modalVisible) {
      log.timeline.push('❌ BUG CONFIRMED (Issue #3): ChartModal did NOT open from pinned Pulse row');
      await page.screenshot({ path: 'test-results/pulse-no-modal.png', fullPage: true });
      console.log(JSON.stringify(log, null, 2));
      return;
    }
    log.timeline.push('✓ ChartModal opened from pinned Pulse row');
    await page.screenshot({ path: 'test-results/pulse-modal-open.png' });

    // ─── ISSUE #1: X close button ─────────────────────────────────────
    log.timeline.push('--- ISSUE #1: clicking X (.cm-close) ---');
    const closeBtn = page.locator('.cm-close').first();
    const closeBtnCount = await closeBtn.count();
    log.timeline.push(`.cm-close count: ${closeBtnCount}`);
    if (closeBtnCount === 0) {
      log.timeline.push('FAIL: .cm-close not in DOM');
      console.log(JSON.stringify(log, null, 2));
      return;
    }

    const closeBox = await closeBtn.boundingBox();
    log.timeline.push(`.cm-close box: ${JSON.stringify(closeBox)}`);
    const closeStyles = await closeBtn.evaluate((el) => {
      const cs = window.getComputedStyle(el);
      return {
        display: cs.display,
        pointerEvents: cs.pointerEvents,
        zIndex: cs.zIndex,
        visibility: cs.visibility,
      };
    });
    log.timeline.push(`.cm-close styles: ${JSON.stringify(closeStyles)}`);

    if (closeBox) {
      const cx = closeBox.x + closeBox.width / 2;
      const cy = closeBox.y + closeBox.height / 2;
      const topmost = await page.evaluate(({ x, y }) => {
        const el = document.elementFromPoint(x, y);
        return el ? {
          tag: el.tagName,
          cls: typeof el.className === 'string' ? el.className.slice(0, 80) : '<svg>',
        } : null;
      }, { x: cx, y: cy });
      log.timeline.push(`elementFromPoint(X-center): ${JSON.stringify(topmost)}`);
    }

    // Standard Playwright click
    await closeBtn.click({ force: false }).catch((e) => {
      log.timeline.push(`X .click() threw: ${e.message}`);
    });
    await page.waitForTimeout(900);
    const afterStdClick = await modal.count();
    log.timeline.push(`After standard .click() — modal count: ${afterStdClick}`);

    if (afterStdClick > 0) {
      // Walk parent chain to find where portal put the modal + look for Svelte 5
      // delegated handler properties (__click etc.)
      const probe = await closeBtn.evaluate((el) => {
        const chain = [];
        let cur = el;
        while (cur && cur !== document) {
          const ownProps = [];
          for (const k of Object.getOwnPropertyNames(cur)) {
            if (k.startsWith('__') || k === '$$d') ownProps.push(k);
          }
          chain.push({
            tag: cur.tagName,
            cls: typeof cur.className === 'string' ? cur.className.slice(0, 60) : '<svg>',
            svelteOwnProps: ownProps,
          });
          cur = cur.parentNode;
        }
        return { chain };
      });
      log.timeline.push(`Parent chain (X button → root): ${JSON.stringify(probe.chain, null, 2)}`);
    }

    const stillInDom = await modal.count();
    const stillVisible = stillInDom > 0 ? await modal.isVisible() : false;
    log.timeline.push(`After X click — modal in DOM: ${stillInDom}, visible: ${stillVisible}`);
    if (stillInDom > 0 || stillVisible) {
      log.timeline.push('❌ BUG CONFIRMED (Issue #1): X did NOT close the chart modal');
      await page.screenshot({ path: 'test-results/x-failed.png' });
    } else {
      log.timeline.push('✓ X closed the modal');
    }

    console.log('\n=== Issue #1 + #3 log ===');
    console.log(JSON.stringify(log, null, 2));
  });
});
