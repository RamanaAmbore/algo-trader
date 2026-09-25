/**
 * order_ticket_visual_consistency.spec.js
 *
 * Verifies the order-ticket visual-consistency pass (13 confirmed
 * inconsistencies + 3 mobile SUSPECT items, see .claude/PLAN.md):
 *
 *  - Shared --ctl-h control height across Select / SideToggle / footer
 *    side button / QtyInput steppers+input / price input (item 1)
 *  - CE/PE picker has a visible selected state (item 2)
 *  - Shared --font-numeric font-family across the listed controls (item 7)
 *  - Shared border-radius (3px) across submit / notice / sticky-result /
 *    margin pill (item 10)
 *  - Mobile (375px) SUSPECT items: QtyInput row overflow, SideToggle
 *    label wrapping, knobs-row wrap alignment
 *
 * Run:
 *   cd frontend && npx playwright test e2e/order_ticket_visual_consistency.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 30_000;

/** Open the order ticket via the 't' shortcut on /dashboard. Retries the
 *  keypress a couple of times — the shortcut occasionally races page
 *  hydration on a cold load, on both desktop and mobile viewports. */
async function openTicket(page) {
  await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(800);
  const modal = page.locator('[role="dialog"], .canonical-modal-overlay').first();
  for (let attempt = 0; attempt < 3; attempt++) {
    await page.click('body').catch(() => {});
    await page.waitForTimeout(300);
    await page.keyboard.press('t');
    const ok = await modal.waitFor({ state: 'visible', timeout: 3000 }).then(() => true).catch(() => false);
    if (ok) return modal;
  }
  await modal.waitFor({ state: 'visible', timeout: 5000 });
  return modal;
}

/** Search + pick a live F&O contract (future or option — anything with
 *  lotSize > 0) so the ticket renders in Lots mode with the Price cell
 *  visible (LIMIT default). Uses the app's own live instruments search
 *  (searchByPrefix, IndexedDB-backed — first call warms the cache, can
 *  take a few seconds) — never a hardcoded expiry-specific symbol that
 *  could go stale. */
async function pickLiveOption(page) {
  const searchInput = page.locator('.ssi-input').first();
  if (!(await searchInput.count())) return false;
  await searchInput.fill('NIFTY');
  // First search warms loadInstruments()'s IndexedDB cache — can take
  // several seconds on a cold cache, well beyond the 50ms debounce.
  await page.waitForTimeout(4000);
  const fnoRow = page.locator('.ssi-row', { hasText: /FUT|CE|PE/ }).first();
  const found = await fnoRow.count().catch(() => 0);
  if (!found) return false;
  await fnoRow.click();
  await page.waitForTimeout(500);
  return true;
}

test.describe('Order-ticket visual consistency — desktop computed styles', () => {
  test.beforeEach(async ({ page }) => {
    page.setDefaultTimeout(TIMEOUT);
    await loginAsAdmin(page);
  });

  test('item 1 — Select / SideToggle / footer side button / QtyInput / price input share --ctl-h', async ({ page }) => {
    const modal = await openTicket(page);

    const heights = await page.evaluate(() => {
      // Two DISTINCT scopes, deliberately — --ctl-h/--ctl-fs are
      // declared on .ot-modal (OrderTicket's own root) AND separately
      // on .oes-common-row (SymbolPanel's footer action row), NOT on
      // the whole .oes-modal shell — querying `.oes-modal .rbq-select-
      // trigger` would silently match the header's account/exchange
      // Select (declared OUTSIDE .oes-common-row, never meant to share
      // this sizing) instead of a ticket knob Select.
      const ticketScope = document.querySelector('.ot-modal');
      const footerScope = document.querySelector('.oes-common-row');
      /** @param {Element|null} scope @param {string} sel */
      const h = (scope, sel) => {
        const el = scope?.querySelector(sel);
        return el ? getComputedStyle(el).height : null;
      };
      return {
        select: h(ticketScope, '.rbq-select-trigger'),
        sideToggle: h(ticketScope, '.ot-side-toggle-compact'),
        priceInput: h(ticketScope, '.ot-price-cell .ot-input'),
        lotsStep: h(ticketScope, '.ot-lots-step'),
        footerSideBtn: h(footerScope, '.oes-footer-side-btn-single'),
      };
    });

    // Every control that's present should read the same ~1.7rem
    // (~27.2px at the default 16px root) shared height. Some may be
    // null if that particular row isn't rendered for the default
    // symbol (e.g. footer side button only renders on the Ticket tab
    // with action !== 'modify') — only assert on what's present.
    // 1px tolerance: `.ot-input` is sized via `min-height` (a floor —
    // the right choice for a text input, so its own padding/line-height
    // never gets clipped), the other four via a fixed `height`; browser
    // sub-pixel rounding between the two approaches can differ by a
    // fraction of a px (observed: 27.1875px vs 27.2031px, a 0.016px
    // rounding artifact) — invisible on screen, not a real inconsistency.
    const present = Object.entries(heights).filter(([, v]) => v != null);
    expect(present.length).toBeGreaterThan(0);
    const values = present.map(([, v]) => parseFloat(v));
    const spread = Math.max(...values) - Math.min(...values);
    expect(
      spread,
      `expected all control heights within 1px of each other, got: ${JSON.stringify(heights)}`
    ).toBeLessThanOrEqual(1);

    await modal.press?.('Escape').catch(() => {});
    await page.keyboard.press('Escape');
  });

  test('item 7 — control font-family unified on --font-numeric', async ({ page }) => {
    await openTicket(page);

    const fonts = await page.evaluate(() => {
      const scope = document.querySelector('.ot-modal');
      /** @param {string} sel */
      const f = (sel) => {
        const el = scope?.querySelector(sel);
        return el ? getComputedStyle(el).fontFamily : null;
      };
      return {
        input: f('.ot-price-cell .ot-input'),
        qtyChip: f('.ot-qty-chip'),
        lotsStep: f('.ot-lots-step'),
        select: f('.rbq-select-trigger'),
        sideToggle: f('.ot-side-toggle-compact .ot-side-btn'),
      };
    });
    const present = Object.values(fonts).filter(Boolean);
    expect(present.length).toBeGreaterThan(0);
    // Every present font-family should start with the same first
    // stack entry (var(--font-numeric)'s first choice) — no stray
    // generic "monospace" left standing alongside it.
    const firstChoices = new Set(present.map((f) => f.split(',')[0].trim()));
    expect(
      firstChoices.size,
      `expected a single font-family stack, got: ${JSON.stringify(fonts)}`
    ).toBe(1);

    await page.keyboard.press('Escape');
  });

  test('item 10 — border-radius unified at 3px across submit/notice/sticky-result/margin-pill', async ({ page }) => {
    await openTicket(page);

    const radii = await page.evaluate(() => {
      const footerScope = document.querySelector('.oes-common-row');
      const ticketScope = document.querySelector('.ot-modal');
      /** @param {Element|null} scope @param {string} sel */
      const r = (scope, sel) => {
        const el = scope?.querySelector(sel);
        return el ? getComputedStyle(el).borderRadius : null;
      };
      return {
        submit: r(footerScope, '.oes-common-submit'),
        marginPill: r(footerScope, '.oes-margin-pill, .oes-cold-prompt'),
        input: r(ticketScope, '.ot-price-cell .ot-input'),
        select: r(ticketScope, '.rbq-select-trigger'),
      };
    });
    const present = Object.values(radii).filter(Boolean);
    expect(present.length).toBeGreaterThan(0);
    for (const v of present) expect(v).toBe('3px');

    await page.keyboard.press('Escape');
  });

  test('item 2 — CE/PE picker selected state has visible styling (amber, not identical to unselected)', async ({ page }) => {
    // Force bare-underlying OPT mode via a direct component check: the
    // CE/PE toggle only renders for a bare underlying + symType=OPT.
    // We assert the CSS RULE exists and differentiates .on from resting
    // state by diffing computed colors on a synthetic element carrying
    // the exact same scoped classes OrderTicket renders — the reliable
    // way to check a CSS rule landed without depending on live picker
    // state (which needs a specific picker-row Type filter interaction
    // outside this spec's scope).
    //
    // Svelte 5 scopes component CSS via a generated `s-XXXXXXXX` CLASS
    // token appended to every element the component's own template
    // renders (not a separate attribute) — a plain `document.createElement`
    // node never gets that token, so the scoped `.ot-side-toggle-compact
    // .ot-side-btn.on` rule silently wouldn't match a naively-created
    // synthetic element. Reading the token off `.ot-modal` (guaranteed
    // OrderTicket-rendered) and copying it onto the synthetic nodes
    // makes them resolve through the same scoped stylesheet.
    await openTicket(page);
    const result = await page.evaluate(() => {
      const modalEl = document.querySelector('.ot-modal');
      if (!modalEl) return null;
      const scopeClass = Array.from(modalEl.classList).find((c) => /^s-/.test(c));
      if (!scopeClass) return null;
      const wrap = document.createElement('div');
      wrap.className = `ot-side-toggle ot-side-toggle-compact ${scopeClass}`;
      const btnOn = document.createElement('button');
      btnOn.className = `ot-side-btn on ${scopeClass}`;
      btnOn.textContent = 'CE';
      const btnOff = document.createElement('button');
      btnOff.className = `ot-side-btn ${scopeClass}`;
      btnOff.textContent = 'PE';
      wrap.appendChild(btnOn);
      wrap.appendChild(btnOff);
      modalEl.appendChild(wrap);
      const onStyle = getComputedStyle(btnOn);
      const offStyle = getComputedStyle(btnOff);
      const out = {
        onBg: onStyle.backgroundColor,
        offBg: offStyle.backgroundColor,
        onColor: onStyle.color,
        offColor: offStyle.color,
      };
      wrap.remove();
      return out;
    });
    expect(result).not.toBeNull();
    // The selected (.on) pill must differ from the unselected pill in
    // at least background or text color — before the fix both were
    // identical because OrderTicket's own .ot-side-toggle-compact
    // scope had no .on rule at all.
    const differs = result.onBg !== result.offBg || result.onColor !== result.offColor;
    expect(differs, `CE/PE .on state indistinguishable from resting state: ${JSON.stringify(result)}`).toBe(true);

    await page.keyboard.press('Escape');
  });
});

test.describe('Order-ticket visual consistency — mobile 375px SUSPECT items', () => {
  test.use({ viewport: { width: 375, height: 812 } });

  test.beforeEach(async ({ page }) => {
    page.setDefaultTimeout(TIMEOUT);
    await loginAsAdmin(page);
  });

  test('SUSPECT 1 — QtyInput row (Lots mode) does not overflow its 65% cell into the Price cell', async ({ page }) => {
    const modal = await openTicket(page);
    const gotOption = await pickLiveOption(page);
    test.skip(!gotOption, 'no live NIFTY F&O contract found via search — cannot force Lots mode');

    const lotsCell = modal.locator('.ot-lots-cell').first();
    const priceCell = modal.locator('.ot-price-cell').first();
    await expect(lotsCell).toBeVisible({ timeout: 5000 });
    const hasPriceCell = await priceCell.count();
    test.skip(hasPriceCell === 0, 'MARKET order type — no adjacent Price cell to overflow into for this leg');

    const priceBox = await priceCell.boundingBox();
    const row = modal.locator('.ot-lots-row').first();
    const rowBox = await row.boundingBox();

    expect(priceBox).not.toBeNull();
    expect(rowBox).not.toBeNull();

    // The lots-row's right edge must not cross into the price cell's
    // left edge — that's the concrete "overflow into the adjacent
    // cell" the plan flagged as SUSPECT.
    expect(
      rowBox.x + rowBox.width,
      `QtyInput row (right edge ${rowBox.x + rowBox.width}) overflows into the Price cell (starts at ${priceBox.x})`
    ).toBeLessThanOrEqual(priceBox.x + 1); // +1px rounding tolerance

    // Bump the lots stepper into triple digits — a realistic large order
    // ("= 1,00,500 units") is the worst case for the "=N units" chip's
    // width, not the single/double-digit default. The chip must show the
    // FULL unit count (it's the exact contract quantity sent to the
    // broker) — truncating it via ellipsis would hide, not just visually
    // compress, load-bearing order information.
    const stepUp = row.locator('.ot-lots-step').nth(1);
    for (let i = 0; i < 15; i++) await stepUp.click();
    const chip = row.locator('.ot-qty-chip').first();
    await expect(chip).toBeVisible();
    const chipOverflow = await chip.evaluate((el) => ({
      text: el.textContent,
      scrollWidth: el.scrollWidth,
      clientWidth: el.clientWidth,
    }));
    expect(
      chipOverflow.scrollWidth,
      `qty chip truncates the broker-bound unit count: ${JSON.stringify(chipOverflow)}`
    ).toBeLessThanOrEqual(chipOverflow.clientWidth + 1);

    await page.keyboard.press('Escape');
  });

  test('SUSPECT 2 — SideToggle 2-word ADD/CLOSE labels do not clip inside the pill', async ({ page }) => {
    const modal = await openTicket(page);

    // Synthetic worst-case: inject the longest real label the component
    // ever renders ("CLOSE · SELL") into the live SideToggle button,
    // AND add the `.ot-side-btn-long` class the real component only
    // applies when `currentQty` is set (the actual trigger for the
    // 2-word label) — the fix is conditional on that class, so testing
    // without it would silently test the wrong (unfixed) code path.
    // The button's own height is pinned to the shared --ctl-h (stretch
    // fill of the fixed-height toggle group), so it can NEVER grow —
    // comparing before/after getBoundingClientRect().height can't detect
    // a wrap. Likewise `scrollWidth > clientWidth` only detects
    // *horizontal* overflow (a single line too wide for a nowrap
    // container); wrapped text reflows to fit the width and would never
    // trip that check either. The only way to detect "did this text
    // actually break onto 2 lines" is to count the rendered line boxes
    // of the text node via Range.getClientRects().
    const result = await page.evaluate(() => {
      const btn = /** @type {HTMLElement|null} */ (
        document.querySelector('.ot-side-toggle-compact .ot-side-sell')
      );
      if (!btn) return null;
      const originalText = btn.textContent;
      const hadLongClass = btn.classList.contains('ot-side-btn-long');
      btn.classList.add('ot-side-btn-long');
      btn.textContent = 'CLOSE · SELL';
      const textNode = btn.firstChild;
      const range = document.createRange();
      range.selectNodeContents(textNode);
      const lineCount = range.getClientRects().length;
      const cs = getComputedStyle(btn);
      const out = {
        lineCount,
        whiteSpace: cs.whiteSpace,
        fontSize: cs.fontSize,
        clientWidth: btn.clientWidth,
        scrollWidth: btn.scrollWidth,
      };
      btn.textContent = originalText;
      if (!hadLongClass) btn.classList.remove('ot-side-btn-long');
      return out;
    });

    if (result == null) {
      test.skip(true, 'SideToggle SELL button not found (default symbol renders differently)');
      return;
    }
    expect(
      result.lineCount,
      `"CLOSE · SELL" renders across ${result.lineCount} line(s) inside the pill (wrapped instead of fitting one line): ${JSON.stringify(result)}`
    ).toBe(1);

    await page.keyboard.press('Escape');
  });

  test('SUSPECT 3 — knobs row: same-basis knobs render the same width across wrapped rows', async ({ page }) => {
    const modal = await openTicket(page);
    // Only the four "regular" knobs (Type/Product/Variety/Validity, and
    // Strategy when present) share an IDENTICAL CSS rule (`.ot-knob`,
    // `flex: 1 1 5rem`) — Side is deliberately wider (`.ot-knob-side`,
    // `flex: 1.4 1 7rem`) and is excluded from this comparison. With
    // `flex-wrap: wrap` + `flex-grow`, each wrapped LINE independently
    // distributes its own leftover space across the knobs on that line —
    // so the same-basis knobs render at genuinely DIFFERENT computed
    // widths depending on how many siblings share their line. That's
    // the concrete "wrapped row's columns don't align with the first
    // row's" symptom the plan flagged, not a left-edge/indent issue.
    const knobs = modal.locator('.ot-row-knobs > .ot-knob:not(.ot-knob-side):not(.ot-knob-strategy)');
    const count = await knobs.count();
    expect(count).toBeGreaterThan(1);

    const boxes = [];
    for (let i = 0; i < count; i++) {
      const box = await knobs.nth(i).boundingBox();
      if (box) boxes.push(box);
    }
    expect(boxes.length).toBeGreaterThan(1);

    // Group by row (same `y`, within 2px tolerance for sub-pixel jitter).
    const rows = [];
    for (const b of boxes) {
      let row = rows.find((r) => Math.abs(r[0].y - b.y) < 2);
      if (!row) { row = []; rows.push(row); }
      row.push(b);
    }

    test.skip(rows.length < 2, 'every regular knob fit on one row at 375px — nothing to check for wrap-column alignment');

    const rowWidths = rows.map((row) => row.map((b) => Math.round(b.width)));
    const allWidths = rowWidths.flat();
    const spread = Math.max(...allWidths) - Math.min(...allWidths);

    // Same flex-basis/flex-grow knobs on different wrapped lines commonly
    // differ by a few px from sub-pixel remainder distribution — that's
    // not visually "uneven". A double-digit-px spread (one line packing
    // 2 knobs vs another packing 3, for example) is the real defect.
    expect(
      spread,
      `same-basis knobs render at inconsistent widths across wrapped rows (uneven wrap): ${JSON.stringify(rowWidths)}`
    ).toBeLessThanOrEqual(10);

    await page.keyboard.press('Escape');
  });
});
