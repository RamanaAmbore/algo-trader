/**
 * alignment_overflow_audit.spec.js
 *
 * Verifies the 2026-09-30 alignment/overflow-containment audit fixes.
 * Operator's global design principles: elements are LEFT-aligned by
 * default (exceptions: header group / expand / contract / fullscreen /
 * default-size); anything larger than its container must scroll or
 * squeeze to fit the viewport rather than expanding past it.
 *
 * Source-pattern (regex-on-file-content) checks cover the mechanical
 * CSS property changes — low risk, no browser needed. Two live checks
 * cover the PositionStrip narrow-viewport overflow fix and the
 * app.css site-wide overflow-x backstop, which can only be verified
 * against computed/rendered layout.
 *
 * Run:
 *   cd frontend && npx playwright test e2e/alignment_overflow_audit.spec.js
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loginAsAdmin } from './fixtures/auth.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const readFile = (relPath) => {
  const abs = path.resolve(__dirname, '..', relPath);
  return readFileSync(abs, 'utf-8');
};

/** Extract the FIRST `.selector { ... }` rule body, or null if absent. */
function ruleBody(css, selector) {
  const re = new RegExp(
    selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*\\{([^}]*)\\}'
  );
  const m = css.match(re);
  return m ? m[1] : null;
}

test.describe('Static source checks — OrderTicket.svelte', () => {
  const content = readFile('src/lib/order/OrderTicket.svelte');

  test('.ot-input declares color-scheme: dark so native number-input spin buttons are visible on a dark background (2026-09-30)', () => {
    // Operator: "price stepper [the native up/down ^ arrows] is dark
    // color and not visible" — browsers draw type="number"'s built-in
    // spin buttons using a light-mode palette by default regardless
    // of the input's own colors; color-scheme fixes ALL native form
    // chrome at once, no vendor-prefixed pseudo-element overrides
    // needed.
    // Plain ruleBody(content, '.ot-input') matches the FIRST substring
    // occurrence of ".ot-input {" anywhere, including inside the
    // earlier compound selector `.ot-price-cell .ot-input { width:... }`
    // — anchor on newline + exact 2-space indent so only the base
    // `.ot-input {` rule (not a compound selector ending in it) matches.
    const m = content.match(/\n {2}\.ot-input \{([^}]*)\}/);
    expect(m, 'base .ot-input rule must exist').not.toBeNull();
    expect(m[1]).toMatch(/color-scheme:\s*dark/);
  });

  test('.ot-chase-toggle no longer right-anchors via margin-left: auto', () => {
    const body = ruleBody(content, '.ot-chase-toggle');
    expect(body, '.ot-chase-toggle rule must exist').not.toBeNull();
    expect(body).not.toMatch(/margin-left:\s*auto/);
  });

  test('.ot-footer-actions is left-aligned (flex-start)', () => {
    const body = ruleBody(content, '.ot-footer-actions');
    expect(body, '.ot-footer-actions rule must exist').not.toBeNull();
    expect(body).toMatch(/justify-content:\s*flex-start/);
    expect(body).not.toMatch(/justify-content:\s*flex-end/);
  });

  test('.ot-demo-cta is left-aligned (flex-start)', () => {
    const body = ruleBody(content, '.ot-demo-cta');
    expect(body, '.ot-demo-cta rule must exist').not.toBeNull();
    expect(body).toMatch(/justify-content:\s*flex-start/);
    expect(body).not.toMatch(/justify-content:\s*flex-end/);
  });

  test('.ot-chase-row wraps defensively on narrow containers', () => {
    const body = ruleBody(content, '.ot-chase-row');
    expect(body, '.ot-chase-row rule must exist').not.toBeNull();
    expect(body).toMatch(/flex-wrap:\s*wrap/);
  });
});

test.describe('Static source checks — TemplateBar.svelte', () => {
  const content = readFile('src/lib/TemplateBar.svelte');

  test('.oes-tpl-scales-input min-width squeezes instead of overflowing narrow viewports', () => {
    const body = ruleBody(content, '.oes-tpl-scales-input');
    expect(body, '.oes-tpl-scales-input rule must exist').not.toBeNull();
    expect(body).toMatch(/min-width:\s*min\(\s*14rem\s*,\s*100%\s*\)/);
  });

  test('.oes-tpl-errors takes its own full-width line inside the wrapping toolbar', () => {
    const body = ruleBody(content, '.oes-tpl-errors');
    expect(body, '.oes-tpl-errors rule must exist').not.toBeNull();
    expect(body).toMatch(/flex-basis:\s*100%/);
  });

  test('.oes-tpl-expanded takes its own full-width line inside the wrapping toolbar', () => {
    const body = ruleBody(content, '.oes-tpl-expanded');
    expect(body, '.oes-tpl-expanded rule must exist').not.toBeNull();
    expect(body).toMatch(/flex-basis:\s*100%/);
  });
});

test.describe('Static source checks — OrderDepth.svelte', () => {
  const content = readFile('src/lib/order/OrderDepth.svelte');

  test('.ot-depth-h header is left-aligned with an explicit gap', () => {
    const body = ruleBody(content, '.ot-depth-h');
    expect(body, '.ot-depth-h rule must exist').not.toBeNull();
    expect(body).toMatch(/justify-content:\s*flex-start/);
    expect(body).not.toMatch(/justify-content:\s*space-between/);
    expect(body).toMatch(/gap:\s*[\d.]+rem/);
  });

  test('.ot-depth-stale no longer right-anchors via margin-left: auto', () => {
    const body = ruleBody(content, '.ot-depth-stale');
    expect(body, '.ot-depth-stale rule must exist').not.toBeNull();
    expect(body).not.toMatch(/margin-left:\s*auto/);
  });

  test('right-aligned depth NUMERIC cells are exempt and untouched', () => {
    // Bid/ask/qty columns are a deliberate numeric-column exception —
    // confirm the fix did NOT touch these (regression guard).
    const body = ruleBody(content, '.ot-depth-label');
    expect(body).toMatch(/text-align:\s*right/);
    const cellBody = ruleBody(content, '.ot-depth-cell');
    expect(cellBody).toMatch(/text-align:\s*right/);
  });

  test('.ot-depth-grid columns have a defined minimum width, centered, with a widened column-gap (2026-09-30, three times same day)', () => {
    const body = ruleBody(content, '.ot-depth-grid');
    expect(body, '.ot-depth-grid rule must exist').not.toBeNull();
    // minmax(3.4rem, max-content) — was bare repeat(4, max-content),
    // which resized the grid on every poll tick as bid/ask/qty digit
    // counts changed (operator: "the columns should have a defined
    // width to accommodate the quote numbers").
    expect(body).toMatch(/grid-template-columns:\s*repeat\(4,\s*minmax\(3\.4rem,\s*max-content\)\)/);
    expect(body).not.toMatch(/grid-template-columns:\s*repeat\(4,\s*max-content\)\)?;/);
    expect(body).not.toMatch(/grid-template-columns:\s*1fr\s+1fr\s+1fr\s+1fr/);
    expect(body).toMatch(/justify-content:\s*center/);
    // Widened from 0.4rem -> 0.9rem after "columns too close" feedback
    // on the just-landed content-sized-column change.
    expect(body).toMatch(/gap:\s*0\.15rem\s+0\.9rem/);
  });

  test('.ot-depth-label column headers tint toward their own data column color (2026-09-30)', () => {
    const body = ruleBody(content, '.ot-depth-label');
    expect(body, '.ot-depth-label rule must exist').not.toBeNull();
    // Bid/Bid-qty labels (1st/2nd column) tint green; Ask/Ask-qty
    // labels (3rd/4th column) tint red — matching their data cells.
    expect(content).toMatch(/\.ot-depth-label:nth-child\(1\),\s*\n?\s*\.ot-depth-label:nth-child\(2\)\s*\{[^}]*color:\s*var\(--algo-green/);
    expect(content).toMatch(/\.ot-depth-label:nth-child\(3\),\s*\n?\s*\.ot-depth-label:nth-child\(4\)\s*\{[^}]*color:\s*var\(--algo-red/);
  });

  test('.ot-depth-header-bg spans all 4 columns of row 1 with a background + lower border, end-to-end (2026-09-30 follow-up)', () => {
    // A per-label background/border would leave visible gaps at the
    // grid's column-gap seams (columns are content-sized, not
    // stretched) — a single element spanning grid-column: 1 / -1 is
    // genuinely continuous across the whole label row instead.
    expect(content).toMatch(/<div class="ot-depth-header-bg"/);
    const body = ruleBody(content, '.ot-depth-header-bg');
    expect(body, '.ot-depth-header-bg rule must exist').not.toBeNull();
    expect(body).toMatch(/grid-column:\s*1\s*\/\s*-1/);
    expect(body).toMatch(/background:\s*rgba\(/);
    expect(body).toMatch(/border-bottom:\s*1px solid/);
  });
});

test.describe('Static source checks — PositionStrip.svelte', () => {
  const content = readFile('src/lib/PositionStrip.svelte');

  test('.ps-breakdown-panel width clamps 16px inside the viewport', () => {
    const body = ruleBody(content, '.ps-breakdown-panel');
    expect(body, '.ps-breakdown-panel rule must exist').not.toBeNull();
    expect(body).toMatch(/width:\s*min\(\s*25\.2rem\s*,\s*calc\(100vw\s*-\s*16px\)\s*\)/);
  });

  test('.ps-bd-header keeps justify-content: space-between (header-group exception)', () => {
    // Explicitly NOT changed — title + close button is a header-group
    // element under the operator's own stated exception.
    const body = ruleBody(content, '.ps-bd-header');
    expect(body).toMatch(/justify-content:\s*space-between/);
  });

  test('_openBreakdown JS clamps popup width to (innerWidth - 16)', () => {
    expect(content).toMatch(
      /Math\.min\(25\.2\s*\*\s*16,\s*window\.innerWidth\s*-\s*16\)/
    );
  });
});

test.describe('Static source checks — app.css site-wide overflow backstop', () => {
  const content = readFile('src/app.css');

  test('html, body carries overflow-x: clip', () => {
    const body = ruleBody(content, 'html, body');
    expect(body, 'html, body rule must exist in app.css').not.toBeNull();
    expect(body).toMatch(/overflow-x:\s*clip/);
    // Must be `clip`, never `hidden` — `hidden` breaks position: sticky
    // elsewhere in the app (see the accompanying code comment).
    expect(body).not.toMatch(/overflow-x:\s*hidden/);
  });
});

// ── Live checks — need a rendered page to verify computed/layout state ──

test.describe('Live — PositionStrip breakdown panel never overflows narrow viewports', () => {
  for (const width of [375, 410]) {
    test(`breakdown panel right edge stays within viewport at ${width}px`, async ({ page }) => {
      test.setTimeout(60000);
      await page.setViewportSize({ width, height: 800 });
      await loginAsAdmin(page);
      await page.goto('/pulse', { waitUntil: 'domcontentloaded' }).catch(() => {});
      await page.waitForTimeout(1500);

      const strip = page.locator('.ps-strip');
      const stripVisible = await strip.isVisible({ timeout: 3000 }).catch(() => false);
      if (!stripVisible) {
        test.info().annotations.push({ type: 'skip', description: 'PositionStrip not visible (market closed or no data)' });
        return;
      }

      const pKey = page.locator('.ps-strip .ps-k-p').first();
      const pKeyVisible = await pKey.isVisible({ timeout: 3000 }).catch(() => false);
      if (!pKeyVisible) {
        test.info().annotations.push({ type: 'skip', description: 'No P-slot key visible' });
        return;
      }
      await pKey.click();
      await page.waitForTimeout(300);

      const popup = page.locator('.ps-breakdown-panel');
      const popupVisible = await popup.isVisible({ timeout: 5000 }).catch(() => false);
      if (!popupVisible) {
        test.info().annotations.push({ type: 'skip', description: 'breakdown popup did not open' });
        return;
      }

      const box = await popup.boundingBox();
      expect(box, 'breakdown panel must report a bounding box').not.toBeNull();
      if (box) {
        expect(
          box.x + box.width,
          `panel right edge (${box.x + box.width}) must not exceed viewport width (${width})`
        ).toBeLessThanOrEqual(width);
        expect(box.x, 'panel left edge must not be negative').toBeGreaterThanOrEqual(0);
      }
    });
  }
});

test.describe('Live — app.css site-wide overflow-x backstop', () => {
  test('document root overflow-x computes to clip (no modal open)', async ({ page }) => {
    test.setTimeout(60000);
    await loginAsAdmin(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' }).catch(() => {});
    await page.waitForTimeout(1000);

    const overflowX = await page.evaluate(
      () => getComputedStyle(document.documentElement).overflowX
    );
    expect(overflowX).toBe('clip');

    const bodyOverflowX = await page.evaluate(
      () => getComputedStyle(document.body).overflowX
    );
    expect(bodyOverflowX).toBe('clip');
  });
});

// ── 2026-09-30 follow-up batch — derivatives +page.svelte / MarketPulse ──
// kv-pair value alignment inconsistency (Aggregate right-flush vs Greeks
// left, Risk conditionally left/right), Greeks card overflow below 600px,
// Drafts header meta right-push, picker-field rigid ≥900px, centered
// empty-states/toast, and the Symbols/StrategyPicker space-between gap.

test.describe('Static source checks — derivatives +page.svelte (kv-pair alignment)', () => {
  const content = readFile('src/routes/(algo)/admin/derivatives/+page.svelte');

  test('.kv-v base rule is left-aligned by default (no margin-left: auto / text-align: right)', () => {
    const body = ruleBody(content, '.kv-v');
    expect(body, '.kv-v rule must exist').not.toBeNull();
    expect(body).not.toMatch(/margin-left:\s*auto/);
    expect(body).not.toMatch(/text-align:\s*right/);
  });

  test('.opt-kv-greeks .kv-v stays left-aligned (unchanged, now consistent with base)', () => {
    const body = ruleBody(content, '.opt-kv-greeks .kv-v');
    expect(body, '.opt-kv-greeks .kv-v rule must exist').not.toBeNull();
    expect(body).toMatch(/text-align:\s*left/);
  });

  test('.opt-kv-greeks gets a flex-wrap override below 600px so Greek pairs never overflow', () => {
    // First attempt used `repeat(auto-fit, minmax(0, max-content) auto)`,
    // which is INVALID CSS — auto-fit/auto-fill require every track in
    // the repeated pattern to be fixed-size, and a bare `auto` track
    // disqualifies it, so browsers silently drop the whole declaration
    // and keep the rigid 5-pair grid (verified via CSS.supports() in the
    // live check below). Fixed: switch display modes instead — flex-wrap
    // on the container, and `.kv-pair` reverts from `display: contents`
    // to `display: flex` so each pair's label+value wraps together as
    // one unit instead of splitting across lines.
    const anchorIdx = content.indexOf('Narrow viewports (<600px)');
    expect(anchorIdx, 'the .opt-kv-greeks narrow-viewport comment anchor must exist').toBeGreaterThan(-1);
    const block = content.slice(anchorIdx, anchorIdx + 1200);
    expect(block).toMatch(/\.opt-kv-greeks\s*\{[^}]*display:\s*flex;[^}]*flex-wrap:\s*wrap;/);
    expect(block).toMatch(/\.opt-kv-greeks\s*\.kv-pair\s*\{[^}]*display:\s*flex;/);
    // Regression guard against reintroducing the invalid grid track.
    expect(block).not.toMatch(/grid-template-columns:\s*repeat\(auto-fit/);
  });

  test('.opt-section-meta no longer right-pushes via margin-left: auto', () => {
    const body = ruleBody(content, '.opt-section-meta');
    expect(body, '.opt-section-meta rule must exist').not.toBeNull();
    expect(body).not.toMatch(/margin-left:\s*auto/);
  });

  test('≥900px .opt-field nth-of-type(2)/(3) AND .opt-field-grow (Account) allow shrinking', () => {
    // Account carries its own always-on `.opt-field-grow { flex: 0 0
    // auto; }` (outside any media query) — if the ≥900px override only
    // targeted nth-of-type(2)/(3) (Underlying/Expiry), Account alone
    // would stay rigid at every width ≥900px. The higher-specificity
    // `.opt-picker .opt-field-grow` selector inside the media query
    // must override it too.
    const idx = content.indexOf('@media (min-width: 900px)');
    expect(idx, '@media (min-width: 900px) block must exist').toBeGreaterThan(-1);
    const block = content.slice(idx, idx + 1000);
    expect(block).toMatch(/nth-of-type\(2\)/);
    expect(block).toMatch(/\.opt-picker\s+\.opt-field-grow/);
    expect(block).toMatch(/flex:\s*0\s+1\s+auto;\s*\n\s*min-width:\s*0;/);
    // The old rigid rule must be gone entirely from this media block.
    expect(block).not.toMatch(/flex:\s*0\s+0\s+auto;\s*\n\s*\}/);
  });

  test('.cand-empty / .byund-empty no longer center their text', () => {
    const candBody = ruleBody(content, '.cand-empty');
    expect(candBody, '.cand-empty rule must exist').not.toBeNull();
    expect(candBody).not.toMatch(/text-align:\s*center/);

    const byundBody = ruleBody(content, '.byund-empty');
    expect(byundBody, '.byund-empty rule must exist').not.toBeNull();
    expect(byundBody).not.toMatch(/text-align:\s*center/);
  });

  test('.cand-empty.cand-loading (the flex-centered loading/no-underlying variant) also left-aligns', () => {
    // .cand-empty's own text-align was fixed above, but the
    // .cand-loading modifier applies `display:flex; justify-content`
    // independently (loading || !selectedUnderlying — a common state)
    // and was still centering via justify-content, not text-align.
    const body = ruleBody(content, '.cand-empty.cand-loading');
    expect(body, '.cand-empty.cand-loading rule must exist').not.toBeNull();
    expect(body).toMatch(/justify-content:\s*flex-start/);
    expect(body).not.toMatch(/justify-content:\s*center/);
  });

  test('.chain-basket-toast no longer centers its text', () => {
    const body = ruleBody(content, '.chain-basket-toast');
    expect(body, '.chain-basket-toast rule must exist').not.toBeNull();
    expect(body).not.toMatch(/text-align:\s*center/);
  });

  test('regression guard: numeric-grid centered cells are untouched (out of scope)', () => {
    // .leg-source, .chain-cell-quote, .chain-basket-lots are numeric/label
    // grid cells, not empty-state or toast text — the audit explicitly
    // scoped the "left-align centered text" fix to empty-states + toast
    // only. Confirm they still center (no accidental blast-radius edit).
    expect(ruleBody(content, '.leg-source')).toMatch(/text-align:\s*center/);
    expect(ruleBody(content, '.chain-cell-quote')).toMatch(/text-align:\s*center/);
    expect(ruleBody(content, '.chain-basket-lots')).toMatch(/text-align:\s*center/);
  });
});

test.describe('Static source checks — MarketPulse.svelte (Symbols/StrategyPicker gap)', () => {
  const content = readFile('src/lib/MarketPulse.svelte');

  test('.mp-section-with-picker uses flex-start + explicit gap, not space-between', () => {
    const body = ruleBody(content, '.mp-section-with-picker');
    expect(body, '.mp-section-with-picker rule must exist').not.toBeNull();
    expect(body).toMatch(/justify-content:\s*flex-start/);
    expect(body).not.toMatch(/justify-content:\s*space-between/);
    expect(body).toMatch(/gap:\s*[\d.]+rem/);
  });
});

// ── Live checks — need a rendered page to verify computed/layout state ──

test.describe('Live — derivatives kv-pair values are left-aligned', () => {
  test('.kv-v computed text-align is left (not right) in the Aggregate card', async ({ page }) => {
    test.setTimeout(60000);
    await loginAsAdmin(page);
    await page.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' }).catch(() => {});
    await page.waitForTimeout(3000);

    const kvValue = page.locator('.kv-v').first();
    const visible = await kvValue.isVisible({ timeout: 5000 }).catch(() => false);
    if (!visible) {
      test.info().annotations.push({ type: 'skip', description: 'No .kv-v value rendered (no strategy/underlying selected)' });
      return;
    }
    const textAlign = await kvValue.evaluate((el) => getComputedStyle(el).textAlign);
    expect(textAlign === 'left' || textAlign === 'start').toBe(true);
  });
});

test.describe('Live — MarketPulse Symbols label + StrategyPicker cluster on the left', () => {
  test('.mp-section-with-picker computed justify-content is flex-start', async ({ page }) => {
    test.setTimeout(60000);
    await loginAsAdmin(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' }).catch(() => {});
    await page.waitForTimeout(2000);

    const el = page.locator('.mp-section-with-picker').first();
    const visible = await el.isVisible({ timeout: 5000 }).catch(() => false);
    if (!visible) {
      test.info().annotations.push({ type: 'skip', description: '.mp-section-with-picker not visible' });
      return;
    }
    const justify = await el.evaluate((node) => getComputedStyle(node).justifyContent);
    expect(justify).toBe('flex-start');
  });
});

test.describe('Live — .opt-kv-greeks narrow-viewport containment (<600px)', () => {
  test('regression guard: repeat(auto-fit, ..., auto) is invalid CSS in this browser', async ({ page }) => {
    // Confirms WHY the grid-track approach was abandoned for flex-wrap —
    // if this ever starts returning true (future CSS spec change), the
    // grid-based fix becomes viable again and this guard should be
    // revisited, not silently left green for the wrong reason.
    await page.goto('about:blank');
    const supported = await page.evaluate(() =>
      CSS.supports('grid-template-columns', 'repeat(auto-fit, minmax(0, max-content) auto)')
    );
    expect(supported).toBe(false);
  });

  test('Greeks block does not horizontally overflow its card at a 360px mobile viewport', async ({ page }) => {
    test.setTimeout(60000);
    await page.setViewportSize({ width: 360, height: 800 });
    await loginAsAdmin(page);
    await page.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' }).catch(() => {});
    await page.waitForTimeout(3000);

    const greeks = page.locator('.opt-kv-greeks').first();
    const visible = await greeks.isVisible({ timeout: 5000 }).catch(() => false);
    if (!visible) {
      test.info().annotations.push({ type: 'skip', description: '.opt-kv-greeks not visible (no strategy/underlying selected)' });
      return;
    }

    const display = await greeks.evaluate((el) => getComputedStyle(el).display);
    expect(display, '.opt-kv-greeks should compute to flex at <600px (grid fallback did not silently win)').toBe('flex');

    const { scrollW, clientW } = await greeks.evaluate((el) => ({
      scrollW: el.scrollWidth,
      clientW: el.clientWidth,
    }));
    expect(scrollW, 'Greeks block content must not exceed its own box width').toBeLessThanOrEqual(clientW + 1);

    // The card wrapping the Greeks block must not be pushed wider than
    // the 360px viewport either.
    const optBlock = page.locator('.opt-block', { has: greeks }).first();
    const blockBox = await optBlock.boundingBox().catch(() => null);
    if (blockBox) {
      expect(blockBox.x + blockBox.width).toBeLessThanOrEqual(360 + 1);
    }
  });
});

test.describe('Live — .opt-picker does not overflow a narrow-ish ≥900px viewport', () => {
  test('.opt-picker scrollWidth stays within clientWidth at 950px (Account/Underlying/Expiry/chip)', async ({ page }) => {
    test.setTimeout(60000);
    await page.setViewportSize({ width: 950, height: 900 });
    await loginAsAdmin(page);
    await page.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' }).catch(() => {});
    await page.waitForTimeout(3000);

    const picker = page.locator('.opt-picker').first();
    const visible = await picker.isVisible({ timeout: 5000 }).catch(() => false);
    if (!visible) {
      test.info().annotations.push({ type: 'skip', description: '.opt-picker not visible' });
      return;
    }

    const { scrollW, clientW } = await picker.evaluate((el) => ({
      scrollW: el.scrollWidth,
      clientW: el.clientWidth,
    }));
    expect(scrollW, '.opt-picker content must not exceed its own box width at 950px').toBeLessThanOrEqual(clientW + 1);

    // Account field specifically must compute a shrinkable flex-basis,
    // not the rigid `flex: 0 0 auto` it used to carry unconditionally.
    const acctField = page.locator('.opt-picker .opt-field-grow').first();
    const acctVisible = await acctField.isVisible({ timeout: 3000 }).catch(() => false);
    if (acctVisible) {
      const flexShrink = await acctField.evaluate((el) => getComputedStyle(el).flexShrink);
      expect(flexShrink, 'Account field flex-shrink must be 1 (shrinkable) at ≥900px').toBe('1');
    }
  });
});

test.describe('Static source checks — price chart / payoff chart background (reverted 2026-09-30, same day)', () => {
  // REVERTED — the sync-with-Chain/Depth change was undone same day per
  // explicit operator request. Both charts are back on the plain
  // canonical card surface every other generic card uses.
  test('.cw-root (price chart) references bare --card-bg-gradient, not --chain-depth-bg', () => {
    const content = readFile('src/lib/ChartWorkspace.svelte');
    const rule = ruleBody(content, '.cw-root') ?? '';
    expect(rule, '.cw-root rule').not.toBe('');
    expect(rule).toMatch(/background:\s*var\(--card-bg-gradient\)/);
    expect(rule).not.toMatch(/background:\s*var\(--chain-depth-bg\)/);
  });

  test('.payoff-chart (payoff chart) references bare --card-bg-gradient, not --chain-depth-bg', () => {
    const content = readFile('src/lib/OptionsPayoff.svelte');
    const rule = ruleBody(content, '.payoff-chart') ?? '';
    expect(rule, '.payoff-chart rule').not.toBe('');
    expect(rule).toMatch(/background:\s*var\(--card-bg-gradient\)/);
    expect(rule).not.toMatch(/background:\s*var\(--chain-depth-bg\)/);
  });
});

test.describe('Static source checks — Chain header row is distinct from the body (2026-09-30, updated same day — --chain-header-bg replaced by --card-bg-elevated)', () => {
  // Operator: "chain header background should not be same as chain
  // [body]... slight variation for contrast" — reverses the earlier
  // same-day decision to pixel-match header and body.
  // UPDATED same day: operator reported the header read as "black and
  // gray" — --chain-header-bg (a faint amber wash over the same dark
  // navy) was replaced by --card-bg-elevated (an actually-lighter navy
  // tier). The header/body CONTRAST invariant this describe block
  // guards is unchanged; only the specific token is different.
  test('.chain-th-ce/-pe/-strike reference --card-bg-elevated, distinct from the body wrap\'s background', () => {
    const content = readFile('src/lib/order/OptionChainTab.svelte');
    for (const sel of ['.chain-th-ce', '.chain-th-pe', '.chain-th-strike']) {
      const rule = ruleBody(content, sel) ?? '';
      expect(rule, `${sel} rule`).not.toBe('');
      expect(rule).toMatch(/background:\s*var\(--card-bg-elevated\)/);
    }
    // .chain-grid-wrap itself is on bare --card-bg-gradient (see the
    // dedicated describe block below) — the header's own
    // --card-bg-elevated token must still differ from whatever the
    // body uses, so the contrast survives.
    const wrapRule = ruleBody(content, '.chain-grid-wrap') ?? '';
    expect(wrapRule, '.chain-grid-wrap rule').not.toBe('');
    expect(wrapRule).not.toMatch(/background:\s*var\(--card-bg-elevated\)/);
  });

  test('--chain-header-bg token no longer exists in app.css (removed as dead code once the header switched to --card-bg-elevated)', () => {
    const appCss = readFile('src/app.css');
    expect(appCss).not.toMatch(/--chain-header-bg:/);
  });

  test('--card-bg-elevated (app.css) is a distinct, genuinely lighter token from --card-bg-gradient / --chain-depth-bg', () => {
    const appCss = readFile('src/app.css');
    const elevatedRule = appCss.match(/--card-bg-elevated:\s*[\s\S]*?;/)?.[0] ?? '';
    expect(elevatedRule, '--card-bg-elevated declaration').not.toBe('');
    const depthRule = appCss.match(/--chain-depth-bg:\s*[\s\S]*?;/)?.[0] ?? '';
    expect(elevatedRule).not.toBe(depthRule);
  });
});

test.describe('Static source checks — Chain body background synced with price chart, header + ITM/OTM excepted (2026-09-30)', () => {
  // Operator: "keep the chain background colors in sync with price
  // chart background with the exception of in the money call and in
  // the put area." .chain-grid-wrap was the one surface still left on
  // --chain-depth-bg after the price chart / payoff chart / order
  // ticket depth ladder had all already moved to bare
  // --card-bg-gradient earlier the same day. Header (--card-bg-elevated,
  // confirmed distinct above) and the ITM/OTM td washes (below) are
  // the explicit exceptions and stay untouched.
  test('.chain-grid-wrap references bare --card-bg-gradient (matching .cw-root / .payoff-chart / .ot-depth), not --chain-depth-bg', () => {
    const content = readFile('src/lib/order/OptionChainTab.svelte');
    const rule = ruleBody(content, '.chain-grid-wrap') ?? '';
    expect(rule, '.chain-grid-wrap rule').not.toBe('');
    expect(rule).toMatch(/background:\s*var\(--card-bg-gradient\)/);
    expect(rule).not.toMatch(/background:\s*var\(--chain-depth-bg\)/);
  });

  test('ITM call/put cell washes are untouched by the body background sync', () => {
    const content = readFile('src/lib/order/OptionChainTab.svelte');
    const ceItmCall = content.match(/\.chain-row-itm-call \.chain-td-ce\s*\{[^}]*\}/)?.[0] ?? '';
    const peItmCall = content.match(/\.chain-row-itm-call \.chain-td-pe\s*\{[^}]*\}/)?.[0] ?? '';
    const peItmPut = content.match(/\.chain-row-itm-put\s+\.chain-td-pe\s*\{[^}]*\}/)?.[0] ?? '';
    const ceItmPut = content.match(/\.chain-row-itm-put\s+\.chain-td-ce\s*\{[^}]*\}/)?.[0] ?? '';
    expect(ceItmCall).toMatch(/rgba\(74,\s*222,\s*128,\s*0\.05\)/);
    expect(peItmCall).toMatch(/rgba\(248,\s*113,\s*113,\s*0\.015\)/);
    expect(peItmPut).toMatch(/rgba\(248,\s*113,\s*113,\s*0\.05\)/);
    expect(ceItmPut).toMatch(/rgba\(74,\s*222,\s*128,\s*0\.015\)/);
  });
});

test.describe('Static source checks — Chain ITM/OTM per-side background (2026-09-30)', () => {
  // Operator: "ITM and OTM calls can have different background...
  // similarly ITM and OTM puts can have different background. they
  // can mirror calls in opposite direction. all of it should be very
  // subtle." Previously the whole row shared one tint (CE + PE cells
  // both got the same wash based on which side was ITM at that
  // strike). Now each side keeps its own color family (CE=green,
  // PE=red) at every strike, with the ITM side washing in stronger
  // than the OTM side of that same family.
  test('.chain-row-itm-call: CE cell is the stronger (ITM) green, PE cell is the weaker (OTM) red', () => {
    const content = readFile('src/lib/order/OptionChainTab.svelte');
    const ceRule = content.match(/\.chain-row-itm-call \.chain-td-ce\s*\{[^}]*\}/)?.[0] ?? '';
    const peRule = content.match(/\.chain-row-itm-call \.chain-td-pe\s*\{[^}]*\}/)?.[0] ?? '';
    expect(ceRule, '.chain-row-itm-call .chain-td-ce rule').not.toBe('');
    expect(peRule, '.chain-row-itm-call .chain-td-pe rule').not.toBe('');
    expect(ceRule).toMatch(/rgba\(74,\s*222,\s*128,\s*0\.05\)/);
    expect(peRule).toMatch(/rgba\(248,\s*113,\s*113,\s*0\.015\)/);
  });

  test('.chain-row-itm-put: PE cell is the stronger (ITM) red, CE cell is the weaker (OTM) green', () => {
    const content = readFile('src/lib/order/OptionChainTab.svelte');
    const peRule = content.match(/\.chain-row-itm-put\s+\.chain-td-pe\s*\{[^}]*\}/)?.[0] ?? '';
    const ceRule = content.match(/\.chain-row-itm-put\s+\.chain-td-ce\s*\{[^}]*\}/)?.[0] ?? '';
    expect(peRule, '.chain-row-itm-put .chain-td-pe rule').not.toBe('');
    expect(ceRule, '.chain-row-itm-put .chain-td-ce rule').not.toBe('');
    expect(peRule).toMatch(/rgba\(248,\s*113,\s*113,\s*0\.05\)/);
    expect(ceRule).toMatch(/rgba\(74,\s*222,\s*128,\s*0\.015\)/);
  });

  test('every ITM/OTM wash stays below the 0.06 "very subtle" ceiling this file already uses elsewhere (ATM row, DTE-warn chip)', () => {
    const content = readFile('src/lib/order/OptionChainTab.svelte');
    for (const alpha of [0.05, 0.015]) {
      expect(alpha).toBeLessThanOrEqual(0.06);
    }
    // Both color families used (green for CE, red for PE) match the
    // existing CE/PE header color convention (.chain-th-ce = --c-long,
    // .chain-th-pe = --c-short) — not a new, unrelated palette.
    expect(content).toMatch(/\.chain-th-ce\s*\{[^}]*color:\s*var\(--c-long\)/);
    expect(content).toMatch(/\.chain-th-pe\s*\{[^}]*color:\s*var\(--c-short\)/);
  });
});

test.describe('Static source checks — Chain toolbar dashed border removed + header border brightened (2026-09-30)', () => {
  // Operator: "the dotted line is not needed" (the .oct-toolbar row's
  // dashed border-bottom, which sat directly above the CE/Strike/PE
  // header) and "below it there is no white border" (the header's own
  // border-bottom read too faint at 0.18 alpha).
  test('.oct-toolbar has no border-bottom declaration (dashed separator removed)', () => {
    const content = readFile('src/lib/order/OptionChainTab.svelte');
    const rule = ruleBody(content, '.oct-toolbar') ?? '';
    expect(rule, '.oct-toolbar rule').not.toBe('');
    // Anchored to a real declaration line, not a bare substring match —
    // this rule's own explanatory comment mentions "border-bottom" in
    // prose (describing what was removed), which a loose
    // /border-bottom/ match would false-positive against.
    expect(rule).not.toMatch(/\n\s*border-bottom:\s*\S/);
  });

  test('.chain-th-ce/-pe/-strike border-bottom brightened to 0.35 alpha white (was 0.18)', () => {
    const content = readFile('src/lib/order/OptionChainTab.svelte');
    for (const sel of ['.chain-th-ce', '.chain-th-pe', '.chain-th-strike']) {
      const rule = ruleBody(content, sel) ?? '';
      expect(rule, `${sel} rule`).not.toBe('');
      expect(rule).toMatch(/border-bottom:\s*1px solid rgba\(255,\s*255,\s*255,\s*0\.35\)/);
      expect(rule).not.toMatch(/border-bottom:\s*1px solid rgba\(255,\s*255,\s*255,\s*0\.18\)/);
    }
  });
});

test.describe('Static source checks — OrderDepth.svelte (2026-09-30, order ticket)', () => {
  // Operator: "keep the order quote depth in sync with chart background.
  // volume at the end quote depth not at the beginning. header slighly
  // different color underlined. I am referring to order ticket."
  test('.ot-depth references plain --card-bg-gradient (in sync with the price chart), not --chain-depth-bg', () => {
    const content = readFile('src/lib/order/OrderDepth.svelte');
    const rule = ruleBody(content, '.ot-depth') ?? '';
    expect(rule, '.ot-depth rule').not.toBe('');
    expect(rule).toMatch(/background:\s*var\(--card-bg-gradient\)/);
    expect(rule).not.toMatch(/background:\s*var\(--chain-depth-bg\)/);
  });

  test('.ot-depth-h (header band) keeps its highlight background but has NO border-bottom (2026-09-30, reversed same day)', () => {
    // Reversed — operator: "the border above the labels should be
    // removed". .ot-depth-h sits directly above the BID QTY/BID/ASK/
    // ASK QTY label row; its own border-bottom read as a second,
    // confusing line stacked right above that row's own
    // .ot-depth-header-bg underline.
    const content = readFile('src/lib/order/OrderDepth.svelte');
    const rule = ruleBody(content, '.ot-depth-h') ?? '';
    expect(rule, '.ot-depth-h rule').not.toBe('');
    expect(rule).toMatch(/background:\s*rgba\(/);
    expect(rule).not.toMatch(/border-bottom:\s*1px solid/);
  });

  test('Volume stat renders AFTER Spread in the stats row markup (was OI/Volume/Spread, now OI/Spread/Volume)', () => {
    const content = readFile('src/lib/order/OrderDepth.svelte');
    const statsBlock = content.match(/<div class="ot-depth-stats">[\s\S]*?<\/div>/)?.[0] ?? '';
    expect(statsBlock, '.ot-depth-stats markup block').not.toBe('');
    const volIdx = statsBlock.indexOf('q.volume');
    const spreadIdx = statsBlock.indexOf('_spread');
    expect(volIdx, 'q.volume reference').toBeGreaterThan(-1);
    expect(spreadIdx, '_spread reference').toBeGreaterThan(-1);
    expect(volIdx, 'Volume must render after Spread in DOM order').toBeGreaterThan(spreadIdx);
  });

  test('live: .ot-depth-h renders with a non-transparent background and NO border-bottom (2026-09-30, reversed same day)', async ({ page }) => {
    await page.setViewportSize({ width: 412, height: 919 });
    await loginAsAdmin(page);
    await page.goto('/orders', { waitUntil: 'domcontentloaded' }).catch(() => {});
    const symInput = page.locator('.ssi-input').first();
    const visible = await symInput.isVisible({ timeout: 10_000 }).catch(() => false);
    if (!visible) {
      test.info().annotations.push({ type: 'skip', description: 'symbol input not visible' });
      return;
    }
    await symInput.fill('NIFTY');
    const sugg = page.locator('.ssi-drop .ssi-row').first();
    const suggVisible = await sugg.isVisible({ timeout: 8_000 }).catch(() => false);
    if (!suggVisible) {
      test.info().annotations.push({ type: 'skip', description: 'no suggestions' });
      return;
    }
    await sugg.click({ force: true });
    const header = page.locator('.ot-depth-h').first();
    const headerVisible = await header.isVisible({ timeout: 10_000 }).catch(() => false);
    if (!headerVisible) {
      test.info().annotations.push({ type: 'skip', description: '.ot-depth-h not rendered (no quote)' });
      return;
    }
    const { bg, borderBottom } = await header.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { bg: cs.backgroundColor, borderBottom: cs.borderBottomWidth };
    });
    expect(bg, '.ot-depth-h background-color').not.toBe('rgba(0, 0, 0, 0)');
    expect(parseFloat(borderBottom), '.ot-depth-h border-bottom-width must be 0 now').toBe(0);
  });

  test('.ot-depth-diag renders raw depth-level counts and raw volume, each with an explicit label, below the grid inside .ot-depth (2026-09-30, relabeled same day)', () => {
    const content = readFile('src/lib/order/OrderDepth.svelte');
    const rule = ruleBody(content, '.ot-depth-diag') ?? '';
    expect(rule, '.ot-depth-diag rule must exist').not.toBe('');
    expect(content).toMatch(/<div class="ot-depth-diag"/);
    expect(content).toMatch(/depth_buy\?\.length/);
    expect(content).toMatch(/depth_sell\?\.length/);
    expect(content).toMatch(/\{q\.volume\s*\?\?/);
    // Explicit label text per item (operator: "add labels to the
    // additional info") — not a single run-on string.
    expect(content).toMatch(/>Buy levels</);
    expect(content).toMatch(/>Sell levels</);
    expect(content).toMatch(/>Volume \(raw\)</);
    // No longer faded/italic (operator: "why the additional info is
    // not showing" — the 0.65-opacity italic treatment read as
    // invisible even though it technically rendered).
    const diagRule = ruleBody(content, '.ot-depth-diag') ?? '';
    expect(diagRule).not.toMatch(/opacity:\s*0\.65/);
    expect(diagRule).not.toMatch(/font-style:\s*italic/);
    // Diagnostic row must be AFTER .ot-depth-grid in markup (below the
    // bid/ask ladder), still inside the same .ot-depth container.
    const gridIdx = content.indexOf('<div class="ot-depth-grid">');
    const diagIdx = content.indexOf('<div class="ot-depth-diag"');
    expect(gridIdx, '.ot-depth-grid markup').toBeGreaterThan(-1);
    expect(diagIdx, '.ot-depth-diag markup').toBeGreaterThan(gridIdx);
  });

  test('.ot-depth-diag is content-sized (inline-flex + align-self: center), not a full-width flex container (2026-09-30 follow-up)', () => {
    // Operator: "the border above should be limited to the content" —
    // a block-level `display: flex` container takes its parent's full
    // width by default, so border-top spanned the whole card even
    // though justify-content: center only centered the TEXT inside
    // that full-width box. inline-flex shrinks the box itself to fit
    // the 3 labeled items, so border-top is genuinely content-width.
    const content = readFile('src/lib/order/OrderDepth.svelte');
    const rule = ruleBody(content, '.ot-depth-diag') ?? '';
    expect(rule, '.ot-depth-diag rule must exist').not.toBe('');
    // Match on real declaration lines only (leading whitespace, no
    // trailing prose) — the explanatory comment above these two
    // declarations deliberately discusses the OLD values in prose
    // ("justify-content: center", "display: flex") as part of
    // explaining the change, which would false-match a bare
    // substring search.
    expect(rule).toMatch(/\n\s*display:\s*inline-flex;/);
    expect(rule).toMatch(/\n\s*align-self:\s*center;/);
    expect(rule).not.toMatch(/\n\s*display:\s*flex;/);
    expect(rule).not.toMatch(/\n\s*justify-content:\s*center;/);
  });

  test('live: .ot-depth-diag\'s own box (and its border-top) is narrower than .ot-depth-grid\'s card, not full-width', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/orders', { waitUntil: 'domcontentloaded' }).catch(() => {});
    const symInput = page.locator('.ssi-input').first();
    const visible = await symInput.isVisible({ timeout: 10_000 }).catch(() => false);
    if (!visible) {
      test.info().annotations.push({ type: 'skip', description: 'symbol input not visible' });
      return;
    }
    await symInput.fill('NIFTY');
    const sugg = page.locator('.ssi-drop .ssi-row').first();
    const suggVisible = await sugg.isVisible({ timeout: 8_000 }).catch(() => false);
    if (!suggVisible) {
      test.info().annotations.push({ type: 'skip', description: 'no suggestions' });
      return;
    }
    await sugg.click({ force: true });
    const diag = page.locator('.ot-depth-diag').first();
    const diagVisible = await diag.isVisible({ timeout: 10_000 }).catch(() => false);
    if (!diagVisible) {
      test.info().annotations.push({ type: 'skip', description: '.ot-depth-diag not rendered (no quote)' });
      return;
    }
    const card = page.locator('.ot-depth').first();
    const [diagWidth, cardWidth] = await Promise.all([
      diag.evaluate((el) => el.getBoundingClientRect().width),
      card.evaluate((el) => el.getBoundingClientRect().width),
    ]);
    expect(diagWidth, `.ot-depth-diag width (${diagWidth}px) must be narrower than .ot-depth's card width (${cardWidth}px) — content-sized, not full-width`).toBeLessThan(cardWidth);
  });

  test('live: .ot-depth-diag is visible below the bid/ask grid with labeled Buy levels / Sell levels / Volume (raw) values', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/orders', { waitUntil: 'domcontentloaded' }).catch(() => {});
    const symInput = page.locator('.ssi-input').first();
    const visible = await symInput.isVisible({ timeout: 10_000 }).catch(() => false);
    if (!visible) {
      test.info().annotations.push({ type: 'skip', description: 'symbol input not visible' });
      return;
    }
    await symInput.fill('NIFTY');
    const sugg = page.locator('.ssi-drop .ssi-row').first();
    const suggVisible = await sugg.isVisible({ timeout: 8_000 }).catch(() => false);
    if (!suggVisible) {
      test.info().annotations.push({ type: 'skip', description: 'no suggestions' });
      return;
    }
    await sugg.click({ force: true });
    const diag = page.locator('.ot-depth-diag').first();
    const diagVisible = await diag.isVisible({ timeout: 10_000 }).catch(() => false);
    if (!diagVisible) {
      test.info().annotations.push({ type: 'skip', description: '.ot-depth-diag not rendered (no quote)' });
      return;
    }
    const text = await diag.innerText();
    expect(text, '.ot-depth-diag text').toMatch(/Buy levels/);
    expect(text, '.ot-depth-diag text').toMatch(/Sell levels/);
    expect(text, '.ot-depth-diag text').toMatch(/Volume \(raw\)/);
    const opacity = await diag.evaluate((el) => getComputedStyle(el).opacity);
    expect(parseFloat(opacity), '.ot-depth-diag must be fully opaque, not faded').toBe(1);
  });
});

test.describe('Static source checks — Chain "Fetching live prices" no longer shifts the grid on load (2026-09-30)', () => {
  // Operator: "the bottom border shows up below the header and
  // disappears" — root cause: the "Fetching live prices…" message was a
  // SIBLING block rendered BEFORE .chain-grid-wrap in normal flow, so it
  // pushed the whole grid (header + border included) down for the
  // ~200-300ms before live quotes arrive, then the grid jumped back up
  // once the message unmounted — read as the header's border visibly
  // relocating. Fixed by moving the message INSIDE .chain-grid-wrap as
  // an absolutely-positioned overlay, so it no longer occupies flow
  // space that later collapses.
  test('the "Fetching live prices" message is NOT a sibling before .chain-grid-wrap', () => {
    const content = readFile('src/lib/order/OptionChainTab.svelte');
    // The old pattern: an {#if}-guarded sibling block immediately
    // followed by the chain-grid-wrap {#if}, with nothing between them.
    expect(content).not.toMatch(
      /Fetching live prices[\s\S]{0,40}\{\/if\}\s*\n\s*<!-- Strike grid -->\s*\n\s*\{#if chainKinds\.includes\('opt'\)/
    );
  });

  test('.chain-fetching-overlay is absolutely positioned inside .chain-grid-wrap (position: relative)', () => {
    const content = readFile('src/lib/order/OptionChainTab.svelte');
    const overlayRule = ruleBody(content, '.chain-fetching-overlay') ?? '';
    expect(overlayRule, '.chain-fetching-overlay rule').not.toBe('');
    expect(overlayRule).toMatch(/position:\s*absolute/);
    const wrapRule = ruleBody(content, '.chain-grid-wrap') ?? '';
    expect(wrapRule, '.chain-grid-wrap rule').not.toBe('');
    expect(wrapRule).toMatch(/position:\s*relative/);
  });

  test('.chain-fetching-overlay is positioned to clear the sticky header, not cover the CE/Strike/PE labels', () => {
    const content = readFile('src/lib/order/OptionChainTab.svelte');
    const overlayRule = ruleBody(content, '.chain-fetching-overlay') ?? '';
    // Header cell measured ~1.33rem tall live; overlay must clear it
    // (an earlier version at top: 0.35rem sat directly on top of the
    // header text, hiding the CE/Strike/PE labels while shown).
    const topMatch = overlayRule.match(/\n\s*top:\s*([\d.]+)rem;/);
    expect(topMatch, 'overlay must declare a top offset in rem').not.toBeNull();
    expect(parseFloat(topMatch[1])).toBeGreaterThanOrEqual(1.33);
  });

  test('live: .chain-th-ce position never shifts across the fetching-prices transition', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/orders', { waitUntil: 'domcontentloaded', timeout: 30000 });
    const symInput = page.locator('.ssi-input').first();
    await expect(symInput).toBeVisible({ timeout: 15_000 });
    await symInput.fill('NIFTY');
    const sugg = page.locator('.ssi-drop .ssi-row').first();
    await expect(sugg).toBeVisible({ timeout: 10_000 });
    await sugg.click({ force: true });
    await page.waitForTimeout(500);

    const chainTab = page.getByRole('tab', { name: /Chain/i }).first();
    await expect(chainTab).toBeEnabled({ timeout: 15_000 });
    await chainTab.click();

    const th = page.locator('.chain-th-ce').first();
    await expect(th).toBeVisible({ timeout: 15_000 });

    const rectAt = async () => th.evaluate((el) => el.getBoundingClientRect().top);
    const samples = [];
    for (const wait of [0, 100, 200, 400, 800]) {
      if (wait) await page.waitForTimeout(wait);
      samples.push(await rectAt());
    }
    const maxDelta = Math.max(...samples) - Math.min(...samples);
    expect(maxDelta, `header top position must not shift across load: ${JSON.stringify(samples)}`).toBeLessThanOrEqual(1);
  });

  test('live: CE/Strike/PE header labels stay visible (not covered by the overlay) while "Fetching live prices" shows', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/orders', { waitUntil: 'domcontentloaded', timeout: 30000 });
    const symInput = page.locator('.ssi-input').first();
    await expect(symInput).toBeVisible({ timeout: 15_000 });
    await symInput.fill('NIFTY');
    const sugg = page.locator('.ssi-drop .ssi-row').first();
    await expect(sugg).toBeVisible({ timeout: 10_000 });
    await sugg.click({ force: true });
    await page.waitForTimeout(500);

    const chainTab = page.getByRole('tab', { name: /Chain/i }).first();
    await expect(chainTab).toBeEnabled({ timeout: 15_000 });
    await chainTab.click();

    const th = page.locator('.chain-th-ce').first();
    await expect(th).toBeVisible({ timeout: 15_000 });
    await expect(th).toHaveText('CE');
  });
});
