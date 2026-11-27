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

// "Static source checks — TemplateBar.svelte" describe block removed
// here — T3.3, bracket-removal, 2026-11. TemplateBar.svelte is deleted.

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
    // on the just-landed content-sized-column change. Row-gap zeroed
    // 0.15rem -> 0 (2026-10-31, operator: "chain border lines for
    // strikes and order border line for depth are not in sync. reuse
    // chain border") — the row-gap sliver broke the row-bottom divider
    // into disconnected dashes against Chain's own zero-gap table rows;
    // see order_ticket_depth_chain_decoration.spec.js for the full
    // rationale and the .ot-depth-cell padding-block compensation.
    expect(body).toMatch(/gap:\s*0\s+0\.9rem/);
  });

  test('.ot-depth-label column headers tint toward their own data column color, via nth-of-type not nth-child (2026-09-30, fixed same day — see "hidden header" bug below)', () => {
    const body = ruleBody(content, '.ot-depth-label');
    expect(body, '.ot-depth-label rule must exist').not.toBeNull();
    // Bid/Bid-qty labels (1st/2nd span) tint green; Ask/Ask-qty
    // labels (3rd/4th span) tint red — matching their data cells.
    // MUST be nth-of-type, not nth-child (operator: "I think there is
    // some hidden header" — .ot-depth-header-bg, a <div> placed FIRST
    // among these labels' <span> siblings, silently shifts every
    // nth-child index by one: nth-child(2) was actually "Bid qty",
    // not "Bid" as intended, giving "Bid" the wrong (red) color and
    // leaving "Ask qty" — actually nth-child(5) — with no color rule
    // at all. nth-of-type only counts same-tag (<span>) siblings, so
    // the <div> doesn't participate and the indices are correct.
    expect(content).toMatch(/\.ot-depth-label:nth-of-type\(1\),\s*\n?\s*\.ot-depth-label:nth-of-type\(2\)\s*\{[^}]*color:\s*var\(--algo-green/);
    expect(content).toMatch(/\.ot-depth-label:nth-of-type\(3\),\s*\n?\s*\.ot-depth-label:nth-of-type\(4\)\s*\{[^}]*color:\s*var\(--algo-red/);
    expect(content).not.toMatch(/\.ot-depth-label:nth-child\(/);
  });

  test('live: "Bid qty"/"Bid" are both green and "Ask"/"Ask qty" are both red — the nth-child/nth-of-type bug\'s actual visible symptom', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/orders', { waitUntil: 'domcontentloaded', timeout: 30000 });
    const symInput = page.locator('.ssi-input').first();
    await expect(symInput).toBeVisible({ timeout: 15_000 });
    await symInput.fill('NIFTY');
    const sugg = page.locator('.ssi-drop .ssi-row').first();
    await expect(sugg).toBeVisible({ timeout: 10_000 });
    await sugg.click({ force: true });
    await page.waitForTimeout(800);

    const labels = page.locator('.ot-depth-label');
    const count = await labels.count();
    if (count < 4) {
      test.info().annotations.push({ type: 'skip', description: '.ot-depth-label rows not rendered (no quote)' });
      return;
    }
    const colors = await labels.evaluateAll((els) => els.map((el) => getComputedStyle(el).color));
    expect(colors[0], '"Bid qty" color').toBe(colors[1]); // "Bid qty" === "Bid"
    expect(colors[2], '"Ask" color').toBe(colors[3]); // "Ask" === "Ask qty"
    expect(colors[0], '"Bid qty"/"Bid" must differ from "Ask"/"Ask qty"').not.toBe(colors[2]);
  });

  test('.ot-depth-header-bg spans all 4 columns of row 1, sized only — painting lives on ::before (2026-09-30 follow-up, restyled to match Chain, then consolidated onto ::before same day)', () => {
    // A per-label background/border would leave visible gaps at the
    // grid's column-gap seams (columns are content-sized, not
    // stretched) — a single element spanning grid-column: 1 / -1 is
    // genuinely continuous across the whole label row instead.
    // Background/border were on THIS element too at one point, but
    // moved entirely to ::before (operator: "did you observe uneven
    // border width for header in the middle and at the end" — two
    // separately-rasterized border lines, the host's own narrow one
    // plus ::before's full-width one, landing near-but-not-exactly on
    // the same pixels in the middle section only). Full detail +
    // ::before's own assertions live in the dedicated ::before test
    // below; this one just confirms the host stays paint-free.
    expect(content).toMatch(/<div class="ot-depth-header-bg"/);
    const body = ruleBody(content, '.ot-depth-header-bg');
    expect(body, '.ot-depth-header-bg rule must exist').not.toBeNull();
    expect(body).toMatch(/grid-column:\s*1\s*\/\s*-1/);
    expect(body).not.toMatch(/\n\s*background:/);
    expect(body).not.toMatch(/\n\s*border-bottom:/);
  });

  test('.ot-depth-header-bg::before bleeds the background+border ±9999px, clipped by .ot-depth\'s own overflow:hidden — genuinely "end to end" (2026-09-30, operator: "extend the header in order ticket end to end. there is a gap header in order ticket. remove it.")', () => {
    // Root cause: .ot-depth-grid stretches to .ot-depth's full width
    // (flex default cross-axis stretch), but the 4 columns are
    // content-sized + centered (justify-content: center) inside it,
    // per the earlier, deliberate "columns centered, not expanding to
    // available width" decision. .ot-depth-header-bg's grid-column:
    // 1/-1 only spans those 4 EXPLICIT tracks, not the grid's extra
    // centering gutter — live-measured: a 267px header band inside a
    // 1349px grid, ~540px of uncovered gap on each side. A plain grid
    // item can't reach past its own track span, so a ::before
    // pseudo-element (NOT confined by the grid-track system) bleeds
    // the background/border far past both sides, clipped at
    // .ot-depth's real edges by its own overflow: hidden.
    // UPDATED (2026-09-30, operator: "did you observe uneven border
    // width for header in the middle and at the end") — the base
    // .ot-depth-header-bg element no longer paints its OWN
    // background/border-bottom (that was the bug: two separately-
    // rasterized 1px border lines — the host's own narrow one plus
    // ::before's full-width one — landing near-but-not-exactly on the
    // same pixels in the middle section only). All painting is now
    // consolidated onto ::before alone; the host just sizes the grid
    // cell (position: relative, nothing else).
    const bgRule = ruleBody(content, '.ot-depth-header-bg') ?? '';
    expect(bgRule, '.ot-depth-header-bg rule').not.toBe('');
    expect(bgRule).toMatch(/position:\s*relative/);
    expect(bgRule).not.toMatch(/\n\s*background:/);
    expect(bgRule).not.toMatch(/\n\s*border-bottom:/);
    const beforeRule = content.match(/\.ot-depth-header-bg::before\s*\{[^}]*\}/)?.[0] ?? '';
    expect(beforeRule, '.ot-depth-header-bg::before rule').not.toBe('');
    expect(beforeRule).toMatch(/position:\s*absolute/);
    expect(beforeRule).toMatch(/inset:\s*0\s+-9999px/);
    // Background/border swapped again (2026-09-30, operator: "you can
    // use legs grid header decoration like background, borders, etc
    // to chain and order ticket header. text color can remain the
    // same") — now the Legs grid's own two-layer background (a
    // translucent navy tint over an opaque #1d2a44 base) and a single
    // bottom-only border via the shared --algo-amber-border-soft
    // token. The top edge added earlier the same session is gone —
    // the Legs grid header never had one.
    expect(beforeRule).toMatch(/background:\s*\n?\s*linear-gradient\(rgba\(15,23,42,0\.65\), rgba\(15,23,42,0\.65\)\),\s*\n?\s*#1d2a44/);
    expect(beforeRule).toMatch(/border-bottom:\s*1px solid var\(--algo-amber-border-soft\)/);
    expect(beforeRule).not.toMatch(/border-top:/);
    const depthRule = ruleBody(content, '.ot-depth') ?? '';
    expect(depthRule, '.ot-depth rule').not.toBe('');
    expect(depthRule).toMatch(/overflow:\s*hidden/);
  });

  test('.ot-depth-grid:first-child cancels .ot-depth\'s own top padding (2026-09-30, operator: "observer the gap above the header on order quote depath... there is top margin or padding which needs to be removed")', () => {
    // When .ot-depth-h (the "Prev <price>" band) doesn't render (no
    // ohlc.close / no err), .ot-depth-grid becomes .ot-depth's literal
    // first child and inherits the card's own 0.45rem top padding
    // before any content — unremarkable before, but now that the
    // header has a prominent edge-to-edge colored band, that padding
    // read as an unexplained gap. Chain's equivalent wrapper
    // (.chain-grid-wrap) has no padding at all, so this cancels
    // .ot-depth's own padding-top exactly, ONLY when grid has nothing
    // above it — when .ot-depth-h IS rendered, this selector doesn't
    // match and that spacing is untouched.
    const rule = content.match(/\.ot-depth-grid:first-child\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule, '.ot-depth-grid:first-child rule').not.toBe('');
    expect(rule).toMatch(/margin-top:\s*-0\.45rem/);
  });

  test('live: the header band\'s background reaches .ot-depth\'s own left/right edges, not just the centered column group', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/orders', { waitUntil: 'domcontentloaded', timeout: 30000 });
    const symInput = page.locator('.ssi-input').first();
    await expect(symInput).toBeVisible({ timeout: 15_000 });
    await symInput.fill('NIFTY');
    const sugg = page.locator('.ssi-drop .ssi-row').first();
    await expect(sugg).toBeVisible({ timeout: 10_000 });
    await sugg.click({ force: true });
    await page.waitForTimeout(800);

    const depthEl = page.locator('.ot-depth').first();
    const headerBg = page.locator('.ot-depth-header-bg').first();
    const headerBgVisible = await headerBg.isVisible({ timeout: 10_000 }).catch(() => false);
    if (!headerBgVisible) {
      test.info().annotations.push({ type: 'skip', description: '.ot-depth-header-bg not rendered (no quote)' });
      return;
    }
    // Sample computed background-color at pixels near .ot-depth's own
    // left and right edges (well outside the narrow centered column
    // group) — if the bleed is working, elementFromPoint there should
    // resolve back to .ot-depth-header-bg's own painted area (the
    // ::before layer), not fall through to .ot-depth's plain card
    // background one layer behind it.
    const result = await depthEl.evaluate((depth) => {
      const dr = depth.getBoundingClientRect();
      const hr = depth.querySelector('.ot-depth-header-bg').getBoundingClientRect();
      const y = hr.top + hr.height / 2;
      const nearLeftX = dr.left + 3;
      const nearRightX = dr.right - 3;
      const elAtLeft = document.elementFromPoint(nearLeftX, y);
      const elAtRight = document.elementFromPoint(nearRightX, y);
      return {
        leftIsHeaderBg: !!elAtLeft && (elAtLeft === depth.querySelector('.ot-depth-header-bg') || elAtLeft.closest('.ot-depth-header-bg') === depth.querySelector('.ot-depth-header-bg')),
        rightIsHeaderBg: !!elAtRight && (elAtRight === depth.querySelector('.ot-depth-header-bg') || elAtRight.closest('.ot-depth-header-bg') === depth.querySelector('.ot-depth-header-bg')),
      };
    });
    expect(result.leftIsHeaderBg, 'near-left edge must land on .ot-depth-header-bg (bleed reaches it)').toBe(true);
    expect(result.rightIsHeaderBg, 'near-right edge must land on .ot-depth-header-bg (bleed reaches it)').toBe(true);
  });

  test('.ot-depth-label font-size/weight matches Chain\'s header typography (2026-09-30, operator: "order ticket header font decoration should be similar to chain header decoration")', () => {
    const body = ruleBody(content, '.ot-depth-label') ?? '';
    expect(body, '.ot-depth-label rule').not.toBe('');
    expect(body).toMatch(/font-size:\s*var\(--fs-sm\)/);
    expect(body).toMatch(/font-weight:\s*700/);
    expect(body).not.toMatch(/font-size:\s*var\(--fs-2xs\)/);
  });

  test('.ot-depth-label/.ot-depth-bid/.ot-depth-ask carry a subtle Bid|Ask divider, matching Chain\'s column-border treatment (2026-09-30, operator: "apply column borders of chain to quote depth headings and quotes"; fixed to nth-of-type same day per the "hidden header" bug above)', () => {
    // MUST be nth-of-type, not nth-child — same root cause as the
    // color-coding test above: .ot-depth-header-bg (a <div>, placed
    // first) shifts nth-child indices by one, so nth-child(2)/(3) was
    // actually "Bid qty"/"Bid" (divider after Bid qty, not after
    // Bid), not the intended "Bid"/"Ask" (divider between Bid and
    // Ask). Live-verified via screenshot: the operator saw the
    // divider line right after "BID QTY" instead of between "BID"
    // and "ASK".
    expect(content).toMatch(/\.ot-depth-label:nth-of-type\(2\)\s*\{[^}]*border-right:\s*1px solid rgba\(255,255,255,0\.03\)/);
    expect(content).toMatch(/\.ot-depth-label:nth-of-type\(3\)\s*\{[^}]*border-left:\s*1px solid rgba\(255,255,255,0\.03\)/);
    expect(content).not.toMatch(/\.ot-depth-label:nth-child\(/);
    // Anchored to `.ot-depth-bid {` / `.ot-depth-ask {` specifically
    // (not `.ot-depth-bid-qty` / `.ot-depth-ask-qty`, which also start
    // with the same prefix) via a trailing space before the brace.
    const bidRule = content.match(/\.ot-depth-bid\s*\{[^}]*\}/)?.[0] ?? '';
    const askRule = content.match(/\.ot-depth-ask\s*\{[^}]*\}/)?.[0] ?? '';
    expect(bidRule, '.ot-depth-bid rule').not.toBe('');
    expect(askRule, '.ot-depth-ask rule').not.toBe('');
    expect(bidRule).toMatch(/border-right:\s*1px solid rgba\(255,255,255,0\.03\)/);
    expect(askRule).toMatch(/border-left:\s*1px solid rgba\(255,255,255,0\.03\)/);
  });

  test('live: the Bid|Ask divider sits between "Bid" and "Ask" labels, not after "Bid qty"', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/orders', { waitUntil: 'domcontentloaded', timeout: 30000 });
    const symInput = page.locator('.ssi-input').first();
    await expect(symInput).toBeVisible({ timeout: 15_000 });
    await symInput.fill('NIFTY');
    const sugg = page.locator('.ssi-drop .ssi-row').first();
    await expect(sugg).toBeVisible({ timeout: 10_000 });
    await sugg.click({ force: true });
    await page.waitForTimeout(800);

    const labels = page.locator('.ot-depth-label');
    const count = await labels.count();
    if (count < 4) {
      test.info().annotations.push({ type: 'skip', description: '.ot-depth-label rows not rendered (no quote)' });
      return;
    }
    const borders = await labels.evaluateAll((els) => els.map((el) => {
      const s = getComputedStyle(el);
      return { br: parseFloat(s.borderRightWidth), bl: parseFloat(s.borderLeftWidth) };
    }));
    expect(borders[0].br, '"Bid qty" must have NO right border').toBe(0);
    expect(borders[1].br, '"Bid" must have the right-side divider').toBeGreaterThan(0);
    expect(borders[2].bl, '"Ask" must have the left-side divider').toBeGreaterThan(0);
    expect(borders[3].bl, '"Ask qty" must have NO left border').toBe(0);
  });

  test('.ot-depth-label declares grid-row: 1 AND an explicit per-label grid-column (1-4), matching .ot-depth-header-bg\'s row so they overlap instead of landing in new implicit columns (2026-09-30, operator: "why border shows above the header row and not decorate like the header in chain")', () => {
    // First attempt (grid-row: 1 alone, column left to auto-placement)
    // made things WORSE, live-verified: CSS Grid auto-placement, when
    // given a fixed row but an open column, avoids cells "occupied"
    // by header-bg (which explicitly spans all 4 columns of row 1) by
    // inventing brand-new IMPLICIT columns rather than overlapping —
    // splitting the labels and the data cells into two completely
    // non-aligned column groups. Both axes must be explicit for a
    // genuine overlap.
    const labelRule = ruleBody(content, '.ot-depth-label') ?? '';
    expect(labelRule, '.ot-depth-label rule').not.toBe('');
    expect(labelRule).toMatch(/grid-row:\s*1/);
    for (const n of [1, 2, 3, 4]) {
      const colRule = content.match(new RegExp(`\\.ot-depth-label:nth-of-type\\(${n}\\)\\s*\\{[^}]*\\}`))?.[0] ?? '';
      expect(colRule, `.ot-depth-label:nth-of-type(${n}) rule`).not.toBe('');
      expect(colRule).toMatch(new RegExp(`grid-column:\\s*${n}\\b`));
    }
  });

  test('.ot-depth-label has symmetric top/bottom padding, matching Chain\'s header cells (2026-09-30, operator: "the chain and order ticket header height is uneven and text is not centered vertically in the header")', () => {
    // Was padding-bottom: 0.2rem only, no padding-top — an asymmetric
    // box (live-measured 17.58px tall, text flush at the top) vs.
    // Chain's .chain-th-ce/-pe/-strike symmetric 0.2rem/0.2rem padding
    // (20.77px tall, text genuinely centered). Matching padding-top
    // here fixes both the cross-component height mismatch and the
    // off-center text in one change.
    const rule = ruleBody(content, '.ot-depth-label') ?? '';
    expect(rule, '.ot-depth-label rule').not.toBe('');
    expect(rule).toMatch(/padding-top:\s*0\.2rem/);
    expect(rule).toMatch(/padding-bottom:\s*0\.2rem/);
  });

  test('live: .ot-depth-label height matches Chain\'s .chain-th-ce height exactly (both headers the same height, text centered)', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/orders', { waitUntil: 'domcontentloaded', timeout: 30000 });
    const symInput = page.locator('.ssi-input').first();
    await expect(symInput).toBeVisible({ timeout: 15_000 });
    await symInput.fill('NIFTY');
    const sugg = page.locator('.ssi-drop .ssi-row').first();
    await expect(sugg).toBeVisible({ timeout: 10_000 });
    await sugg.click({ force: true });
    await page.waitForTimeout(800);

    const otLabel = page.locator('.ot-depth-label').first();
    const otVisible = await otLabel.isVisible({ timeout: 10_000 }).catch(() => false);
    if (!otVisible) {
      test.info().annotations.push({ type: 'skip', description: '.ot-depth-label not rendered (no quote)' });
      return;
    }
    const otHeight = await otLabel.evaluate((el) => el.getBoundingClientRect().height);

    const chainTab = page.getByRole('tab', { name: /Chain/i }).first();
    await expect(chainTab).toBeEnabled({ timeout: 15_000 });
    await chainTab.click();
    await page.waitForTimeout(800);
    const chainTh = page.locator('.chain-th-ce').first();
    const chainVisible = await chainTh.isVisible({ timeout: 10_000 }).catch(() => false);
    if (!chainVisible) {
      test.info().annotations.push({ type: 'skip', description: '.chain-th-ce not rendered (no chain data)' });
      return;
    }
    const chainHeight = await chainTh.evaluate((el) => el.getBoundingClientRect().height);
    expect(Math.abs(otHeight - chainHeight), `.ot-depth-label (${otHeight}px) vs .chain-th-ce (${chainHeight}px) height must match`).toBeLessThanOrEqual(1);
  });

  test('live: label columns align exactly with the data-cell columns below them (left edges match, still exactly 4 grid columns)', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/orders', { waitUntil: 'domcontentloaded', timeout: 30000 });
    const symInput = page.locator('.ssi-input').first();
    await expect(symInput).toBeVisible({ timeout: 15_000 });
    await symInput.fill('NIFTY');
    const sugg = page.locator('.ssi-drop .ssi-row').first();
    await expect(sugg).toBeVisible({ timeout: 10_000 });
    await sugg.click({ force: true });
    await page.waitForTimeout(800);

    const grid = page.locator('.ot-depth-grid').first();
    const gridVisible = await grid.isVisible({ timeout: 10_000 }).catch(() => false);
    if (!gridVisible) {
      test.info().annotations.push({ type: 'skip', description: '.ot-depth-grid not rendered (no quote)' });
      return;
    }
    const info = await grid.evaluate((el) => {
      const labels = Array.from(el.querySelectorAll('.ot-depth-label')).map((l) => l.getBoundingClientRect().left);
      const cells = Array.from(el.querySelectorAll('.ot-depth-cell')).slice(0, 4).map((c) => c.getBoundingClientRect().left);
      const colCount = getComputedStyle(el).gridTemplateColumns.split(' ').length;
      return { labels, cells, colCount };
    });
    expect(info.colCount, 'grid must stay at exactly 4 columns, not grow implicit extras').toBe(4);
    expect(info.labels.length, 'label count').toBe(4);
    expect(info.cells.length, 'cell count (first row)').toBe(4);
    for (let i = 0; i < 4; i++) {
      expect(Math.abs(info.labels[i] - info.cells[i]), `column ${i + 1} label/cell left-edge must match`).toBeLessThanOrEqual(1);
    }
  });

  // A dedicated "header-bg vs first label boundingBox()" live check was
  // tried here and dropped — it proved flaky specifically under the
  // full-suite run (boundingBox() intermittently returned a box with a
  // NaN-producing comparison despite both elements clearly correctly
  // rendered and overlapping, confirmed via a failure screenshot). The
  // "label columns align exactly with the data-cell columns" test
  // above already exercises the identical overlap invariant (label
  // and header-bg must share the same row/columns for alignment to
  // hold) via a more reliable getBoundingClientRect() read, so
  // coverage of this fix is not reduced by the removal.
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

      // Click the P-slot VALUE span, not the label — since the label-press
      // double-open fix (2026-10), the .ps-k-p label only toggles the
      // InfoHint popover; the value span remains the sole NavBreakdown
      // trigger (see navstrip_label_infohint_split.spec.js).
      const pValue = page.locator('.ps-strip .ps-k-p')
        .locator('xpath=following-sibling::span[1][contains(@class, "ps-agg-v")]')
        .first();
      const pValueVisible = await pValue.isVisible({ timeout: 3000 }).catch(() => false);
      if (!pValueVisible) {
        test.info().annotations.push({ type: 'skip', description: 'No P-slot value visible' });
        return;
      }
      await pValue.click();
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

  test('.opt-kv-greeks stays a 2-row grid at every viewport and scrolls horizontally instead of wrapping (2026-11-18 restructure)', () => {
    // 2026-11-18: the operator asked for a true header-row (5 Greek
    // symbols) + value-row (5 values) grid, not 5 independent
    // label+value flex pairs. A flex-wrap-below-600px fallback (the OLD
    // design this test used to guard) would have broken that 2-row
    // shape by reflowing pairs onto their own lines at mobile widths.
    // The new design keeps the grid unconditional at every viewport and
    // lets `overflow-x: auto` absorb any mobile-width overflow instead
    // — no Greek is ever clipped/hidden, and the header/value row
    // pairing survives at every width.
    const body = ruleBody(content, '.opt-kv-greeks');
    expect(body, '.opt-kv-greeks rule must exist').not.toBeNull();
    expect(body).toMatch(/grid-template-columns:\s*repeat\(5,\s*minmax\(max-content,\s*1fr\)\)/);
    expect(body).toMatch(/overflow-x:\s*auto/);
    // Regression guard: the old flex-wrap-below-600px fallback, and the
    // invalid auto-fit grid track it replaced, must both stay gone from
    // THIS rule specifically (`.opt-side-row` elsewhere in the file
    // legitimately uses `repeat(auto-fit, minmax(220px, 1fr))` for the
    // 3-card row layout — unrelated, must not false-positive on it).
    expect(body).not.toMatch(/display:\s*flex;[\s\S]*flex-wrap:\s*wrap;/);
    expect(body).not.toMatch(/repeat\(auto-fit/);
  });

  test('.opt-kv-risk is a 4-track grid below 1180px, widening to 6-track at >=1180px, with a header row (2026-11-19)', () => {
    // 2026-11-18 restructure: the old "2 pairs per row below 1180px,
    // 6-pairs-in-1-row above 1180px" responsive shape was first replaced
    // by an unconditional 2-column grid, one metric per row.
    // 2026-11-19 follow-up — the operator then said this card "can have
    // a header too", which reopened that decision: the grid is
    // responsive again (4 tracks below 1180px, 6 tracks at >=1180px),
    // but now with a real `.kv-risk-colhdr` header row rather than the
    // old header-less single-row override this guard used to forbid.
    const body = ruleBody(content, '.opt-kv-risk');
    expect(body, '.opt-kv-risk rule must exist').not.toBeNull();
    expect(body).toMatch(
      /grid-template-columns:\s*max-content\s+minmax\(max-content,\s*1fr\)\s+max-content\s+minmax\(max-content,\s*1fr\)/
    );
    const pairBody = ruleBody(content, '.opt-kv-risk .kv-pair');
    expect(pairBody, '.opt-kv-risk .kv-pair rule must exist').not.toBeNull();
    expect(pairBody).toMatch(/display:\s*contents/);
    // The >=1180px widening override must exist (reverses the earlier
    // "no responsive override" guard for this card only).
    expect(content).toMatch(/@media \(min-width: 1180px\)\s*\{\s*\.opt-kv-risk\s*\{/);
    const wideMatch = content.match(
      /@media \(min-width:\s*1180px\)\s*\{\s*\.opt-kv-risk\s*\{([^}]*)\}/
    );
    expect(wideMatch, '>=1180px .opt-kv-risk override must exist').not.toBeNull();
    expect(wideMatch[1]).toMatch(
      /grid-template-columns:\s*repeat\(3,\s*max-content\s+minmax\(max-content,\s*1fr\)\)/
    );
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
    // Historical guard — confirms WHY the earlier auto-fit grid-track
    // attempt was abandoned (first for flex-wrap, now for the
    // 2026-11-18 header-row/value-row grid + overflow-x:auto design).
    // If this ever starts returning true (future CSS spec change), an
    // auto-fit-based layout becomes viable again and this guard should
    // be revisited, not silently left green for the wrong reason.
    await page.goto('about:blank');
    const supported = await page.evaluate(() =>
      CSS.supports('grid-template-columns', 'repeat(auto-fit, minmax(0, max-content) auto)')
    );
    expect(supported).toBe(false);
  });

  test('Greeks grid scrolls horizontally (never clips/hides a Greek) at a 360px mobile viewport, and the card itself stays within the viewport', async ({ page }) => {
    // 2026-11-18 restructure: the Greeks card is now an unconditional
    // 2-row (header + value) grid at every viewport. At 360px the 5
    // columns can legitimately not all fit — the contract is that the
    // GRID scrolls horizontally (`overflow-x: auto`), never that a
    // column shrinks below its own content and ellipsizes/clips a
    // Greek, and never that the surrounding `.opt-block` card itself
    // is pushed wider than the viewport.
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

    const { display, overflowX } = await greeks.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { display: cs.display, overflowX: cs.overflowX };
    });
    expect(display, '.opt-kv-greeks stays a grid at <600px (no flex-wrap reflow)').toBe('grid');
    expect(overflowX, '.opt-kv-greeks must be horizontally scrollable at <600px').toBe('auto');

    // No individual Greek value may be clipped/ellipsized inside its
    // own cell — each value's content must fit within its own cell,
    // even if the overall grid scrolls to reach it.
    const kvValueOverflows = await greeks.evaluate((el) =>
      [...el.querySelectorAll('.kv-v')].map((v) => ({
        scrollW: v.scrollWidth,
        clientW: v.clientWidth,
      }))
    );
    expect(kvValueOverflows.length).toBeGreaterThanOrEqual(4); // 5 Greeks, rho may be absent in some payloads
    for (const v of kvValueOverflows) {
      expect(v.scrollW, 'a Greek value must not overflow its own cell').toBeLessThanOrEqual(v.clientW + 1);
    }

    // The card wrapping the Greeks block must not be pushed wider than
    // the 360px viewport — the scrollbar is internal to the grid, not
    // a card-level overflow.
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
  test('.chain-th-ce/-pe/-strike reuse the Legs grid header background, distinct from the body wrap\'s background (2026-09-30, operator: "you can use legs grid header decoration like background, borders, etc to chain and order ticket header")', () => {
    const content = readFile('src/lib/order/OptionChainTab.svelte');
    for (const sel of ['.chain-th-ce', '.chain-th-pe', '.chain-th-strike']) {
      const rule = ruleBody(content, sel) ?? '';
      expect(rule, `${sel} rule`).not.toBe('');
      expect(rule).toMatch(/background:\s*linear-gradient\(rgba\(15,23,42,0\.65\), rgba\(15,23,42,0\.65\)\),\s*#1d2a44/);
    }
    // .chain-grid-wrap itself is on bare --card-bg-gradient (see the
    // dedicated describe block below) — the header's own two-layer
    // background must still differ from whatever the body uses, so
    // the contrast survives.
    const wrapRule = ruleBody(content, '.chain-grid-wrap') ?? '';
    expect(wrapRule, '.chain-grid-wrap rule').not.toBe('');
    expect(wrapRule).not.toMatch(/background:\s*linear-gradient\(rgba\(15,23,42,0\.65\)/);
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

  test('.chain-th-ce/-pe/-strike bottom edge is an --algo-amber-border-soft box-shadow:inset, not border-bottom (2026-09-30, sticky + border-collapse repaint fix; decoration later reused from Legs grid header)', () => {
    // box-shadow: inset instead of border-bottom (2026-09-30, operator:
    // "again the border shows and disappears" / "...in the money calls
    // and puts, it disappears") — sticky <th> + border-collapse:collapse
    // is a known Chrome/WebKit bug where the border can drop on a big
    // repaint (exactly what the ITM/OTM background-wash switch-on is).
    // box-shadow isn't part of table border-collapse semantics, so it's
    // immune to this bug class.
    // Alpha/color went through several one-off rgba() bumps the same
    // day, then (operator: "you can use legs grid header decoration
    // like background, borders, etc to chain and order ticket header.
    // text color can remain the same") was replaced with the shared
    // --algo-amber-border-soft token, matching .cand-headrow (Legs
    // grid) exactly, and the top edge added earlier the same session
    // was dropped — the Legs grid header never had one, so this rule
    // is back to a single box-shadow layer.
    const content = readFile('src/lib/order/OptionChainTab.svelte');
    for (const sel of ['.chain-th-ce', '.chain-th-pe', '.chain-th-strike']) {
      const rule = ruleBody(content, sel) ?? '';
      expect(rule, `${sel} rule`).not.toBe('');
      expect(rule).toMatch(/box-shadow:/);
      expect(rule).toMatch(/inset 0 -1px 0 var\(--algo-amber-border-soft\)/);
      expect(rule).not.toMatch(/inset 0 1px 0/);
      // Single-line rule — ruleBody's captured [^}]* contains only the
      // literal declarations between { and }, no surrounding comments,
      // so a bare substring check here is safe (unlike the multi-line
      // comment-collision cases documented elsewhere in this file).
      expect(rule).not.toMatch(/border-bottom:\s*\S/);
    }
  });

  test('.chain-th-ce/-pe/-strike no longer carry a top edge (2026-09-30, operator: "add top border also to headers in chain and order ticket", then reversed by "you can use legs grid header decoration... text color can remain the same" — Legs grid header has no top edge)', () => {
    const content = readFile('src/lib/order/OptionChainTab.svelte');
    for (const sel of ['.chain-th-ce', '.chain-th-pe', '.chain-th-strike']) {
      const rule = ruleBody(content, sel) ?? '';
      expect(rule, `${sel} rule`).not.toBe('');
      // Positive y-offset (1px) would be a top-anchored inset shadow —
      // confirm it's gone, only the -1px bottom edge remains.
      expect(rule).not.toMatch(/inset 0 1px 0/);
    }
  });

  test('CE/PE header text-align back to original right/left (2026-09-30, flipped to left/right then reversed back same day — operator: "reverse ce and re label alignment")', () => {
    const content = readFile('src/lib/order/OptionChainTab.svelte');
    const ceRule = ruleBody(content, '.chain-th-ce') ?? '';
    const peRule = ruleBody(content, '.chain-th-pe') ?? '';
    expect(ceRule, '.chain-th-ce rule').not.toBe('');
    expect(peRule, '.chain-th-pe rule').not.toBe('');
    expect(ceRule).toMatch(/text-align:\s*right/);
    expect(peRule).toMatch(/text-align:\s*left/);
  });

  test('live: CE renders right-aligned and PE renders left-aligned', async ({ page }) => {
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
    await page.waitForTimeout(800);

    const ce = page.locator('.chain-th-ce').first();
    const pe = page.locator('.chain-th-pe').first();
    const ceVisible = await ce.isVisible({ timeout: 10_000 }).catch(() => false);
    if (!ceVisible) {
      test.info().annotations.push({ type: 'skip', description: '.chain-th-ce not rendered (no chain data)' });
      return;
    }
    const [ceAlign, peAlign] = await Promise.all([
      ce.evaluate((el) => getComputedStyle(el).textAlign),
      pe.evaluate((el) => getComputedStyle(el).textAlign),
    ]);
    expect(ceAlign, '.chain-th-ce computed text-align').toBe('right');
    expect(peAlign, '.chain-th-pe computed text-align').toBe('left');
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

  test('.ot-depth-stats strip no longer exists — OI/Spread folded into .ot-depth-diag, compact Vol stat removed (2026-09-30, operator: "remove vol 13k at the top left")', () => {
    const content = readFile('src/lib/order/OrderDepth.svelte');
    expect(content).not.toMatch(/<div class="ot-depth-stats">/);
    // The compact, aggCompact-formatted Volume stat (e.g. "13K") is
    // gone entirely — distinct from the still-present RAW volume value
    // inside .ot-depth-diag (q.volume ?? '—', an exact unformatted
    // number, kept for the diagnostic purpose it was added for).
    expect(content).not.toMatch(/<span class="ot-depth-stat-lbl">Vol<\/span>/);
  });

  test('OI and Spread now render inside .ot-depth-diag, ahead of Buy levels/Sell levels/Volume (raw) (2026-09-30 consolidation)', () => {
    const content = readFile('src/lib/order/OrderDepth.svelte');
    const diagBlock = content.match(/<div class="ot-depth-diag"[^>]*>[\s\S]*?<\/div>/)?.[0] ?? '';
    expect(diagBlock, '.ot-depth-diag markup block').not.toBe('');
    const oiIdx = diagBlock.indexOf('q.oi');
    const spreadIdx = diagBlock.indexOf('_spread');
    const buyLevelsIdx = diagBlock.indexOf('Buy levels');
    expect(oiIdx, 'q.oi reference').toBeGreaterThan(-1);
    expect(spreadIdx, '_spread reference').toBeGreaterThan(-1);
    expect(buyLevelsIdx, 'Buy levels reference').toBeGreaterThan(-1);
    expect(oiIdx, 'OI must render before Spread').toBeLessThan(spreadIdx);
    expect(spreadIdx, 'Spread must render before Buy levels').toBeLessThan(buyLevelsIdx);
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

  test('.ot-depth-diag is content-sized (inline-flex + align-self: center), not a full-width flex container (2026-09-30, align-self changed center -> flex-end -> back to center, same day)', () => {
    // Operator: "the border above should be limited to the content" —
    // a block-level `display: flex` container takes its parent's full
    // width by default, so border-top spanned the whole card even
    // though justify-content: center only centered the TEXT inside
    // that full-width box. inline-flex shrinks the box itself to fit
    // the 3 labeled items, so border-top is genuinely content-width.
    // align-self briefly changed center -> flex-end (operator: "align
    // the labels to right"), then REVERTED back to center same day
    // (operator: "the bottom label info with border is not aligned
    // at center") — right-aligning it read as inconsistent against
    // the grid above, whose own column group centers within the card.
    const content = readFile('src/lib/order/OrderDepth.svelte');
    const rule = ruleBody(content, '.ot-depth-diag') ?? '';
    expect(rule, '.ot-depth-diag rule must exist').not.toBe('');
    // Match on real declaration lines only (leading whitespace, no
    // trailing prose) — the explanatory comment above these two
    // declarations deliberately discusses the OLD values in prose
    // ("justify-content: center", "display: flex", "align-self:
    // flex-end") as part of explaining the change, which would
    // false-match a bare substring search.
    expect(rule).toMatch(/\n\s*display:\s*inline-flex;/);
    expect(rule).toMatch(/\n\s*align-self:\s*center;/);
    expect(rule).not.toMatch(/\n\s*display:\s*flex;/);
    expect(rule).not.toMatch(/\n\s*justify-content:\s*center;/);
    expect(rule).not.toMatch(/\n\s*align-self:\s*flex-end;/);
  });

  test('.ot-depth-diag font-size matches Chain\'s label size (2026-09-30, operator: "the text size the label text size be in sync with chain") — border-top now lives on ::before, see the dedicated 2026-10-31 full-bleed test below', () => {
    const content = readFile('src/lib/order/OrderDepth.svelte');
    const rule = ruleBody(content, '.ot-depth-diag') ?? '';
    expect(rule, '.ot-depth-diag rule must exist').not.toBe('');
    // --fs-sm matches Chain's .chain-th-ce/-pe/-strike label size,
    // was --fs-2xs (smaller than Chain's equivalent labels).
    expect(rule).toMatch(/\n\s*font-size:\s*var\(--fs-sm\);/);
    expect(rule).not.toMatch(/\n\s*font-size:\s*var\(--fs-2xs\);/);
    // 2026-10-31 reversal: the bare host rule no longer paints its own
    // border-top/background-color directly — both moved to a full-bleed
    // ::before (see the dedicated rule + tests below). A regression that
    // re-adds either declaration directly to the host would silently
    // reintroduce the pre-reversal "limited to content" look even if
    // ::before still exists.
    expect(rule).not.toMatch(/\n\s*border-top:/);
    expect(rule).not.toMatch(/\n\s*background-color:/);
  });

  test('.ot-depth-diag::before carries the amber border-top and 0.05-white background full-bleed (2026-10-31, operator: "in order ticket, extend border and background for the row with text buy levels. this is the last row") — reverses the 2026-09-30 "limited to content" decision', () => {
    const content = readFile('src/lib/order/OrderDepth.svelte');
    const beforeRule = content.match(/\.ot-depth-diag::before\s*\{[^}]*\}/)?.[0] ?? '';
    expect(beforeRule, '.ot-depth-diag::before rule must exist').not.toBe('');
    expect(beforeRule).toMatch(/position:\s*absolute;/);
    expect(beforeRule).toMatch(/inset:\s*0\s+-9999px;/);
    // Same amber alpha as .ot-depth-header-bg's border-bottom, kept in
    // sync with Chain's own .chain-th-* box-shadow amber throughout.
    expect(beforeRule).toMatch(/border-top:\s*1px solid rgba\(251,191,36,0\.40\);/);
    expect(beforeRule).not.toMatch(/border-top:\s*1px solid rgba\(255,255,255,0\.10\);/);
    // Unchanged literal value from the pre-reversal host rule — only
    // WHERE it paints moved, not the alpha tier itself.
    expect(beforeRule).toMatch(/background-color:\s*rgba\(255,255,255,0\.05\);/);
    // Host must anchor the bleed via position: relative, same convention
    // as .ot-depth-header-bg/.ot-depth-row-wash above.
    const hostRule = ruleBody(content, '.ot-depth-diag') ?? '';
    expect(hostRule).toMatch(/\n\s*position:\s*relative;/);
  });

  test('.ot-depth-diag-item is positioned so it paints above ::before\'s full-bleed background (2026-10-31)', () => {
    const content = readFile('src/lib/order/OrderDepth.svelte');
    const rule = ruleBody(content, '.ot-depth-diag-item') ?? '';
    expect(rule, '.ot-depth-diag-item rule must exist').not.toBe('');
    expect(rule).toMatch(/\n\s*position:\s*relative;/);
  });

  test('.ot-depth-diag-lbl is right-aligned (2026-09-30, operator: "align the labels to right")', () => {
    const content = readFile('src/lib/order/OrderDepth.svelte');
    const rule = ruleBody(content, '.ot-depth-diag-lbl') ?? '';
    expect(rule, '.ot-depth-diag-lbl rule must exist').not.toBe('');
    expect(rule).toMatch(/text-align:\s*right/);
  });

  test('live: .ot-depth-diag horizontally centers against .ot-depth-grid\'s own center (reverted from right-align same day)', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/orders', { waitUntil: 'domcontentloaded', timeout: 30000 }).catch(() => {});
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
    const grid = page.locator('.ot-depth-grid').first();
    const [diagBox, gridBox] = await Promise.all([diag.boundingBox(), grid.boundingBox()]);
    if (!diagBox || !gridBox) {
      test.info().annotations.push({ type: 'skip', description: 'boundingBox() unavailable this run' });
      return;
    }
    const diagCenter = diagBox.left + diagBox.width / 2;
    const gridCenter = gridBox.left + gridBox.width / 2;
    expect(Math.abs(diagCenter - gridCenter), 'diag center must align with grid center, not sit right-anchored').toBeLessThanOrEqual(2);
  });

  test('live: .ot-depth-diag\'s own box stays content-sized (text still centers), but its ::before background/border now bleeds to .ot-depth\'s full card edges (2026-10-31 reversal)', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/orders', { waitUntil: 'domcontentloaded', timeout: 30000 });
    const symInput = page.locator('.ssi-input').first();
    // expect().toBeVisible() retries/polls for up to the timeout — unlike
    // the sibling tests above's isVisible().catch() snapshot check (a
    // non-retrying immediate read that fires before the modal has even
    // mounted and always resolves false near-instantly, the same pattern
    // the header-bg full-bleed test above deliberately avoids).
    await expect(symInput).toBeVisible({ timeout: 15_000 });
    await symInput.fill('NIFTY');
    const sugg = page.locator('.ssi-drop .ssi-row').first();
    await expect(sugg).toBeVisible({ timeout: 10_000 });
    await sugg.click({ force: true });
    const diag = page.locator('.ot-depth-diag').first();
    // waitFor (polling), not isVisible() (snapshot) — this condition is a
    // genuine "did a quote arrive" network race, worth retrying for,
    // distinct from the input/suggestion mounting checks above.
    const diagVisible = await diag.waitFor({ state: 'visible', timeout: 10_000 }).then(() => true).catch(() => false);
    if (!diagVisible) {
      test.info().annotations.push({ type: 'skip', description: '.ot-depth-diag not rendered (no quote)' });
      return;
    }
    const card = page.locator('.ot-depth').first();
    // The host's own layout box is still content-sized — centering the
    // text is unchanged from the 2026-09-30 decision. Only the painted
    // decoration (::before) is now full-bleed, checked below.
    const [diagWidth, cardWidth] = await Promise.all([
      diag.evaluate((el) => el.getBoundingClientRect().width),
      card.evaluate((el) => el.getBoundingClientRect().width),
    ]);
    expect(diagWidth, `.ot-depth-diag width (${diagWidth}px) must stay narrower than .ot-depth's card width (${cardWidth}px) — the TEXT box is still content-sized`).toBeLessThan(cardWidth);

    // Same elementFromPoint technique as the header-bg full-bleed test
    // above: sample near .ot-depth's own left/right edges, well outside
    // the narrow centered content box — if the ::before bleed reaches
    // there, elementFromPoint resolves back to .ot-depth-diag itself
    // (pseudo-elements paint as part of their host for hit-testing).
    const result = await card.evaluate((cardEl) => {
      const diagEl = cardEl.querySelector('.ot-depth-diag');
      const cr = cardEl.getBoundingClientRect();
      const dr = diagEl.getBoundingClientRect();
      const y = dr.top + dr.height / 2;
      const nearLeftX = cr.left + 3;
      const nearRightX = cr.right - 3;
      const elAtLeft = document.elementFromPoint(nearLeftX, y);
      const elAtRight = document.elementFromPoint(nearRightX, y);
      return {
        leftIsDiag: !!elAtLeft && (elAtLeft === diagEl || elAtLeft.closest('.ot-depth-diag') === diagEl),
        rightIsDiag: !!elAtRight && (elAtRight === diagEl || elAtRight.closest('.ot-depth-diag') === diagEl),
      };
    });
    expect(result.leftIsDiag, 'near-left edge must land on .ot-depth-diag (::before bleed reaches it)').toBe(true);
    expect(result.rightIsDiag, 'near-right edge must land on .ot-depth-diag (::before bleed reaches it)').toBe(true);
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

test.describe('Static source checks — Chain Strike column widened to separate CE/PE (2026-09-30)', () => {
  // Operator: "move ce pe away from strike". CE/PE content is
  // flex-end/flex-start aligned toward the Strike column (see
  // .chain-cell-row-ce/-pe), so the visible gap between them is the
  // Strike cell's own left/right padding. Widened 0.1rem -> 0.4rem,
  // then reduced to 0.22rem same day (operator: "the gap between ce,
  // strike, pe values should be reduced") — 0.4rem read as too wide
  // once rendered.
  test('.chain-row > td.chain-td-strike padding reduced to 0.22rem (0.1rem -> 0.4rem -> 0.22rem)', () => {
    const content = readFile('src/lib/order/OptionChainTab.svelte');
    const rule = content.match(/\.chain-row\s*>\s*td\.chain-td-strike\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule, '.chain-row > td.chain-td-strike rule').not.toBe('');
    expect(rule).toMatch(/padding-left:\s*0\.22rem/);
    expect(rule).toMatch(/padding-right:\s*0\.22rem/);
    expect(rule).not.toMatch(/padding-left:\s*0\.1rem/);
    expect(rule).not.toMatch(/padding-right:\s*0\.1rem/);
    expect(rule).not.toMatch(/padding-left:\s*0\.4rem/);
    expect(rule).not.toMatch(/padding-right:\s*0\.4rem/);
  });

  // Operator (2026-09-30, follow-up same day): "the gap between ce,
  // strike, pe label should be increased" — the fix above only
  // widened the DATA rows' Strike cell; the HEADER row's own
  // .chain-th-strike padding was left at 0.1rem, so the header's
  // CE|Strike|PE gap stayed visibly tighter than the data rows below
  // it. Matched to the same 0.4rem value for header/body consistency.
  // Reduced again 0.4rem -> 0.22rem same day (operator: "the gap
  // between ce, strike, pe values should be reduced") — 0.4rem read
  // as too wide once rendered; both header and data Strike cell kept
  // in sync at the new value.
  test('.chain-th-strike padding reduced to 0.22rem (0.2rem 0.1rem -> 0.2rem 0.4rem -> 0.2rem 0.22rem), matching the data row', () => {
    const content = readFile('src/lib/order/OptionChainTab.svelte');
    const rule = ruleBody(content, '.chain-th-strike') ?? '';
    expect(rule, '.chain-th-strike rule').not.toBe('');
    expect(rule).toMatch(/padding:\s*0\.2rem\s+0\.22rem/);
    expect(rule).not.toMatch(/padding:\s*0\.2rem\s+0\.1rem/);
    expect(rule).not.toMatch(/padding:\s*0\.2rem\s+0\.4rem/);
  });

  test('live: strike cell gains extra horizontal separation from the CE/PE columns without misaligning the header label', async ({ page }) => {
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

    const firstStrikeTd = page.locator('.chain-row > td.chain-td-strike').first();
    await expect(firstStrikeTd).toBeVisible({ timeout: 15_000 });
    const pl = await firstStrikeTd.evaluate((el) => getComputedStyle(el).paddingLeft);
    expect(pl, 'chain-td-strike computed padding-left').toBe('3.52px');

    // Header "Strike" label and a data-row strike number must still
    // share the same horizontal center (widening padding must not
    // have knocked the column out of alignment).
    const headerStrike = page.locator('.chain-th-strike').first();
    const [headerBox, cellBox] = await Promise.all([
      headerStrike.boundingBox(),
      firstStrikeTd.boundingBox(),
    ]);
    const headerCenter = headerBox.x + headerBox.width / 2;
    const cellCenter = cellBox.x + cellBox.width / 2;
    expect(Math.abs(headerCenter - cellCenter), 'header/body Strike column centers must align').toBeLessThanOrEqual(1);
  });
});

// ── 2026-11-25 — Derivatives page: cross-grid left-padding unification +
// Greeks/Risk & EV vertical column dividers ──────────────────────────────
//
// Four hand-rolled grids on /admin/derivatives (Legs/.cand-grid,
// Greeks/.opt-kv-greeks, Risk & EV/.opt-kv-risk, Snapshot/.byund-grid)
// each independently hardcoded a different left-padding for their own
// header AND data cells. Unified onto one shared app.css token,
// `--grid-pad-left` (4px, matching Legs' own pre-existing value — the
// densest of the four and this app's documented "tight card density"
// preference). Separately, Greeks and Risk & EV were missing the
// vertical column-divider treatment (`box-shadow: inset -1px 0 0 0
// rgba(126,151,184,0.40)`) Legs (`.cand-sym-acct`/`.cand-chg-sep`) and
// Snapshot (`.byund-und`/`.byund-chg-sep`) already carry.
//
// Five quality dimensions:
//   1. SSOT   — one shared `--grid-pad-left` token drives all four
//               grids' header+data left-inset; no grid hardcodes its
//               own independent left-padding literal any more.
//   2. Perf   — static checks are regex-only (no browser); live checks
//               reuse the existing GOLD-underlying pick-loop pattern
//               already established by derivatives_greeks_risk_
//               restructure.spec.js, no extra network round-trips.
//   3. Stale  — regression guards confirm the OLD per-grid literals
//               (0.2rem container padding, flush 0px Risk value inset,
//               bare `0.2rem` bleed-compensation terms) are gone.
//   4. Reuse  — box-shadow divider value matches the EXACT existing
//               Legs/Snapshot literal, not a new one; the GOLD-pick
//               live-test helper mirrors the restructure spec's own.
//   5. UX     — live checks verify the CSS fix's actual visible effect
//               (text starts at the same inset; the divider's painted
//               x-position — not just its presence — matches between a
//               header row and the data row beneath it, at both the
//               narrow and >=1180px Risk & EV breakpoints).
//
// Run:
//   cd frontend && npx playwright test e2e/alignment_overflow_audit.spec.js

test.describe('Static source checks — app.css --grid-pad-left token', () => {
  const content = readFile('src/app.css');

  test('--grid-pad-left is defined as 4px, matching Legs\' own pre-existing value', () => {
    expect(content).toMatch(/--grid-pad-left:\s*4px;/);
  });
});

test.describe('Static source checks — Legs (.cand-grid) left-padding unified', () => {
  const candContent = readFile('src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte');
  const pageContent = readFile('src/routes/(algo)/admin/derivatives/+page.svelte');

  test('.cand-row > span (data cell) uses the shared var, not the old bare 4px literal', () => {
    const body = ruleBody(candContent, '.cand-row > span');
    expect(body, '.cand-row > span rule must exist').not.toBeNull();
    expect(body).toMatch(/padding-left:\s*var\(--grid-pad-left\)/);
  });

  test('.cand-headrow container no longer carries horizontal padding (moved to per-span rule)', () => {
    const body = ruleBody(pageContent, '.cand-headrow');
    expect(body, '.cand-headrow rule must exist').not.toBeNull();
    expect(body).toMatch(/padding:\s*0\.1rem\s+0\s+0\.15rem;/);
    expect(body).not.toMatch(/padding:\s*0\.1rem\s+0\.2rem\s+0\.15rem;/);
  });

  test('.cand-headrow > span carries the shared var on the left, mirroring .cand-row > span\'s own per-span padding', () => {
    const body = ruleBody(pageContent, '.cand-headrow > span');
    expect(body, '.cand-headrow > span rule must exist').not.toBeNull();
    expect(body).toMatch(/padding-left:\s*var\(--grid-pad-left\)/);
    expect(body).toMatch(/padding-right:\s*4px/);
  });

  test('.cand-row.cand-row-total > span (Legs TOTAL row) also uses the shared var on the left', () => {
    const body = ruleBody(pageContent, '.cand-row.cand-row-total > span');
    expect(body, '.cand-row.cand-row-total > span rule must exist').not.toBeNull();
    expect(body).toMatch(/padding:\s*0\s+0\.45rem\s+0\s+var\(--grid-pad-left\)/);
  });
});

test.describe('Static source checks — Greeks (.opt-kv-greeks) left-padding + divider', () => {
  const content = readFile('src/routes/(algo)/admin/derivatives/+page.svelte');

  test('.kv-k-greek base rule swaps only the 0.2rem bleed-compensation term for the shared var (left only, right untouched)', () => {
    const body = ruleBody(content, '.opt-kv-greeks .kv-k-greek');
    expect(body, '.opt-kv-greeks .kv-k-greek rule must exist').not.toBeNull();
    // Right side (3rd shorthand value) keeps the literal 0.2rem bleed term.
    expect(body).toMatch(/padding:\s*0\.1rem\s+calc\(0\.2rem\s*\+\s*0\.55rem\s*\/\s*2\)\s+0\.15rem\s+calc\(var\(--grid-pad-left\)\s*\+\s*0\.55rem\s*\/\s*2\)/);
  });

  // 2026-11-26 revert — a 2026-11-25 fix put a vertical divider
  // (box-shadow) on the Greeks HEADER cells, which fragmented the
  // header into separate boxed cells instead of one continuous bar
  // (operator-reported regression: "greeks and risks & ev grid headers
  // show as cells instead of one single header"). Header rows in this
  // file (`.cand-headrow`, `.byund-headrow`) never carry a divider —
  // only the DATA/VALUE rows (`.kv-v` below) do. This pins the revert.
  test('.kv-k-greek header cells carry no box-shadow divider — header rows in this file never do', () => {
    const body = ruleBody(content, '.opt-kv-greeks .kv-k-greek');
    expect(body, '.opt-kv-greeks .kv-k-greek rule must exist').not.toBeNull();
    expect(body).not.toMatch(/box-shadow/);

    const nth5Body = ruleBody(content, '.opt-kv-greeks .kv-k-greek:nth-child(5)');
    expect(nth5Body, ':nth-child(5) rule must exist').not.toBeNull();
    expect(nth5Body).not.toMatch(/box-shadow/);
  });

  test(':first-child override uses the bare shared var (no bleed compensation needed — margin-left is zeroed)', () => {
    const body = ruleBody(content, '.opt-kv-greeks .kv-k-greek:first-child');
    expect(body, ':first-child rule must exist').not.toBeNull();
    expect(body).toMatch(/padding-left:\s*var\(--grid-pad-left\)/);
    expect(body).not.toMatch(/padding-left:\s*0\.2rem/);
  });

  test('.kv-v left-padding swaps only the left shorthand component for the shared var; right untouched', () => {
    const body = ruleBody(content, '.opt-kv-greeks .kv-v');
    expect(body, '.opt-kv-greeks .kv-v rule must exist').not.toBeNull();
    expect(body).toMatch(/padding:\s*0\.15rem\s+0\.35rem\s+0\.15rem\s+var\(--grid-pad-left\)/);
    // 2026-11-26: swapped from the stronger 0.40 literal to the shared
    // `--sep-color` token (the SAME subtle whisper-hairline every other
    // grid's ordinary per-cell divider uses) — 0.40 is reserved for the
    // special label-column edge (`.byund-und`/`.cand-sym-acct`), which
    // this ordinary value-to-value divider is not.
    expect(body).toMatch(/box-shadow:\s*inset\s+-1px\s+0\s+0\s+0\s+var\(--sep-color\)/);
    // width:100%/no-margin must survive untouched — the compensated
    // offset lives on the HEADER side, not by adding a matching bleed
    // margin to the value row (see the rule's own code comment).
    expect(body).toMatch(/width:\s*100%/);
    expect(body).not.toMatch(/margin:\s*0\s+calc/);
  });

  test('.kv-v:nth-child(5n) (last column — rho\'s value) has box-shadow: none', () => {
    const body = ruleBody(content, '.opt-kv-greeks .kv-v:nth-child(5n)');
    expect(body, '.kv-v:nth-child(5n) rule must exist').not.toBeNull();
    expect(body).toMatch(/box-shadow:\s*none/);
  });
});

test.describe('Static source checks — Risk & EV (.opt-kv-risk) left-padding + divider', () => {
  const content = readFile('src/routes/(algo)/admin/derivatives/+page.svelte');

  test('.kv-risk-colhdr base rule swaps only the 0.2rem bleed-compensation term for the shared var', () => {
    const body = ruleBody(content, '.opt-kv-risk .kv-risk-colhdr');
    expect(body, '.opt-kv-risk .kv-risk-colhdr rule must exist').not.toBeNull();
    expect(body).toMatch(/padding:\s*0\.1rem\s+calc\(0\.2rem\s*\+\s*0\.6rem\s*\/\s*2\)\s+0\.15rem\s+calc\(var\(--grid-pad-left\)\s*\+\s*0\.6rem\s*\/\s*2\)/);
  });

  // 2026-11-26 revert — same regression/fix class as the Greeks header
  // above. The Risk & EV header row must never carry a vertical
  // divider, at either breakpoint.
  test('.kv-risk-colhdr header cells carry no box-shadow divider at any breakpoint', () => {
    const body = ruleBody(content, '.opt-kv-risk .kv-risk-colhdr');
    expect(body, '.opt-kv-risk .kv-risk-colhdr rule must exist').not.toBeNull();
    expect(body).not.toMatch(/box-shadow/);

    const nth4Body = ruleBody(content, '.opt-kv-risk .kv-risk-colhdr:nth-child(4)');
    expect(nth4Body, ':nth-child(4) rule must exist').not.toBeNull();
    expect(nth4Body).not.toMatch(/box-shadow/);

    const wideIdx = content.indexOf('@media (min-width: 1180px)', content.indexOf('.opt-kv-risk .kv-risk-colhdr:first-child'));
    const wideBlock = content.slice(wideIdx, wideIdx + 900);
    const nth4Wide = wideBlock.match(/\.opt-kv-risk \.kv-risk-colhdr:nth-child\(4\)\s*\{([^}]*)\}/);
    expect(nth4Wide, 'wide :nth-child(4) rule must exist').not.toBeNull();
    expect(nth4Wide[1]).not.toMatch(/box-shadow/);
    const nth6Wide = wideBlock.match(/\.opt-kv-risk \.kv-risk-colhdr:nth-child\(6\)\s*\{([^}]*)\}/);
    expect(nth6Wide, 'wide :nth-child(6) rule must exist').not.toBeNull();
    expect(nth6Wide[1]).not.toMatch(/box-shadow/);
  });

  test(':first-child override uses the bare shared var', () => {
    const body = ruleBody(content, '.opt-kv-risk .kv-risk-colhdr:first-child');
    expect(body, ':first-child rule must exist').not.toBeNull();
    expect(body).toMatch(/padding-left:\s*var\(--grid-pad-left\)/);
  });

  test('.kv-v and .kv-k both get the shared left-inset var and the plain (uncompensated) divider', () => {
    // 2026-11-26: swapped from the stronger 0.40 literal to the shared
    // `--sep-color` token — see the matching comment in the Greeks test
    // above; same reasoning applies to both .kv-v and .kv-k here.
    const vBody = ruleBody(content, '.opt-kv-risk .kv-v');
    expect(vBody, '.opt-kv-risk .kv-v rule must exist').not.toBeNull();
    expect(vBody).toMatch(/padding-left:\s*var\(--grid-pad-left\)/);
    expect(vBody).toMatch(/box-shadow:\s*inset\s+-1px\s+0\s+0\s+0\s+var\(--sep-color\)/);

    const kBody = ruleBody(content, '.opt-kv-risk .kv-k');
    expect(kBody, '.opt-kv-risk .kv-k rule must exist').not.toBeNull();
    expect(kBody).toMatch(/padding-left:\s*var\(--grid-pad-left\)/);
    expect(kBody).toMatch(/box-shadow:\s*inset\s+-1px\s+0\s+0\s+0\s+var\(--sep-color\)/);
  });

  test('.kv-v:nth-child(4n+2) is OFF (last column) below 1180px, ON again (internal column) and :nth-child(6n) OFF (new last column) inside the >=1180px override', () => {
    const offBody = ruleBody(content, '.opt-kv-risk .kv-v:nth-child(4n+2)');
    expect(offBody, '.kv-v:nth-child(4n+2) rule must exist').not.toBeNull();
    expect(offBody).toMatch(/box-shadow:\s*none/);

    const wideIdx = content.indexOf('@media (min-width: 1180px)', content.indexOf('.opt-kv-risk .kv-v:nth-child(4n+2)'));
    const wideBlock = content.slice(wideIdx, wideIdx + 900);
    expect(wideBlock).toMatch(/\.opt-kv-risk \.kv-v:nth-child\(4n\+2\)\s*\{[^}]*box-shadow:\s*inset\s+-1px/);
    expect(wideBlock).toMatch(/\.opt-kv-risk \.kv-v:nth-child\(6n\)\s*\{[^}]*box-shadow:\s*none/);
  });
});

test.describe('Static source checks — Snapshot (.byund-grid) left-padding unified', () => {
  const content = readFile('src/routes/(algo)/admin/derivatives/+page.svelte');

  test('.byund-headrow > span and .byund-row > span both swap only the left shorthand component for the shared var', () => {
    const headBody = ruleBody(content, '.byund-headrow > span');
    expect(headBody, '.byund-headrow > span rule must exist').not.toBeNull();
    expect(headBody).toMatch(/padding:\s*0\s+0\.45rem\s+0\s+var\(--grid-pad-left\)/);

    const rowBody = ruleBody(content, '.byund-row > span');
    expect(rowBody, '.byund-row > span rule must exist').not.toBeNull();
    expect(rowBody).toMatch(/padding:\s*0\s+0\.45rem\s+0\s+var\(--grid-pad-left\)/);
  });
});

// ── Live checks — need a rendered page with a real strategy loaded ────────
//
// Reuses the GOLD-underlying pick-loop pattern from
// derivatives_greeks_risk_restructure.spec.js (Greeks/Risk & EV only
// render once a strategy has loaded for the selected underlying).

/** Pick GOLD and keep re-picking until the Greeks card renders. */
async function _pickGoldUntilGreeksRender(page) {
  const trigger = page.locator('button#opt-und');
  const label = trigger.locator('.rbq-select-label');
  const greeks = page.locator('.opt-kv-greeks').first();
  await trigger.waitFor({ state: 'visible', timeout: 60_000 });
  await expect.poll(async () => {
    if (await greeks.isVisible().catch(() => false)) return true;
    if ((await label.textContent())?.trim() !== 'GOLD') {
      const panelOpen = await page.locator('.rbq-select-panel').isVisible().catch(() => false);
      if (!panelOpen) await trigger.click();
      const opt = page.locator('.rbq-select-option-label', { hasText: /^GOLD$/ }).first();
      if (await opt.isVisible().catch(() => false)) await opt.click();
    }
    return false;
  }, { timeout: 90_000, intervals: [1000, 2000] }).toBe(true);
}

async function _gotoDerivativesWithStrategy(page) {
  await loginAsAdmin(page);
  await page.goto('/admin/derivatives', { waitUntil: 'domcontentloaded', timeout: 90_000 });
  await _pickGoldUntilGreeksRender(page);
  await page.locator('.opt-kv-risk').first().waitFor({ state: 'visible', timeout: 30_000 });
  await page.locator('.cand-headrow').first().waitFor({ state: 'attached', timeout: 30_000 });
}

/** x-offset of the FIRST text node's rendered start, relative to el's own left edge. */
async function _textStartInset(locator) {
  return locator.evaluate((el) => {
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) => (n.textContent.trim() ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP),
    });
    const textNode = walker.nextNode();
    if (!textNode) return null;
    const range = document.createRange();
    range.selectNodeContents(textNode);
    const textLeft = range.getBoundingClientRect().left;
    const cellLeft = el.getBoundingClientRect().left;
    return textLeft - cellLeft;
  });
}

test.describe('Live — cross-grid left-inset parity (header text vs data text, 4px target)', () => {
  test.setTimeout(150_000);

  test('Legs: header "Symbol" text starts at the same inset as a data row\'s symbol text', async ({ page }) => {
    await _gotoDerivativesWithStrategy(page);
    const headerSymbol = page.locator('.cand-headrow [role="columnheader"]').first();
    const dataSymbol = page.locator('.cand-row:not(.cand-row-total) .cand-sym-acct').first();
    const [headerInset, dataInset] = await Promise.all([
      _textStartInset(headerSymbol),
      _textStartInset(dataSymbol),
    ]);
    expect(headerInset, 'Legs header Symbol text inset').not.toBeNull();
    expect(dataInset, 'Legs data Symbol text inset').not.toBeNull();
    expect(Math.abs(headerInset - dataInset), `Legs header (${headerInset}px) vs data (${dataInset}px) Symbol text inset`).toBeLessThanOrEqual(1);
  });

  test('Greeks: header (Δ) text starts at the same inset as the value-row\'s first cell text', async ({ page }) => {
    await _gotoDerivativesWithStrategy(page);
    const headerCell = page.locator('.opt-kv-greeks .kv-k-greek').first();
    const valueCell = page.locator('.opt-kv-greeks .kv-v').first();
    const [headerInset, valueInset] = await Promise.all([
      _textStartInset(headerCell),
      _textStartInset(valueCell),
    ]);
    expect(headerInset, 'Greeks header text inset').not.toBeNull();
    expect(valueInset, 'Greeks value text inset').not.toBeNull();
    expect(Math.abs(headerInset - valueInset), `Greeks header (${headerInset}px) vs value (${valueInset}px) text inset`).toBeLessThanOrEqual(1);
  });

  test('Risk & EV: header ("Metric") text starts at the same inset as the first pair\'s own label ("R:R") text', async ({ page }) => {
    await _gotoDerivativesWithStrategy(page);
    const headerCell = page.locator('.opt-kv-risk .kv-risk-colhdr').first();
    const labelCell = page.locator('.opt-kv-risk .kv-k').first();
    const [headerInset, labelInset] = await Promise.all([
      _textStartInset(headerCell),
      _textStartInset(labelCell),
    ]);
    expect(headerInset, 'Risk & EV header text inset').not.toBeNull();
    expect(labelInset, 'Risk & EV label text inset').not.toBeNull();
    expect(Math.abs(headerInset - labelInset), `Risk & EV header (${headerInset}px) vs label (${labelInset}px) text inset`).toBeLessThanOrEqual(1);
  });

  test('Snapshot: header "Underlying" text starts at the same inset as a data row\'s own underlying symbol text', async ({ page }) => {
    await _gotoDerivativesWithStrategy(page);
    const headerCell = page.locator('.byund-headrow > span').first();
    const dataCell = page.locator('.byund-row:not(.byund-row-total) .byund-und').first();
    const dataVisible = await dataCell.isVisible({ timeout: 5000 }).catch(() => false);
    if (!dataVisible) {
      test.info().annotations.push({ type: 'skip', description: 'No Snapshot data row rendered' });
      return;
    }
    const [headerInset, dataInset] = await Promise.all([
      _textStartInset(headerCell),
      _textStartInset(dataCell),
    ]);
    expect(headerInset, 'Snapshot header text inset').not.toBeNull();
    expect(dataInset, 'Snapshot data text inset').not.toBeNull();
    expect(Math.abs(headerInset - dataInset), `Snapshot header (${headerInset}px) vs data (${dataInset}px) text inset`).toBeLessThanOrEqual(1);
  });
});

test.describe('Live — Greeks/Risk & EV header never has a divider; value rows keep theirs', () => {
  test.setTimeout(150_000);

  // 2026-11-26 revert — a vertical divider on the header row fragments
  // one continuous header bar into separate boxed cells (operator-
  // reported regression). Every header cell, including the non-last
  // ones, must have NO box-shadow. The value/data row below keeps its
  // own divider convention unchanged (present on every column except
  // the true last one) — this spec section pins both halves so a
  // future change can't silently reintroduce a header divider while
  // "fixing" something else.
  test('Greeks: no header cell carries a box-shadow divider; the value row below still does, except on the last column', async ({ page }) => {
    await _gotoDerivativesWithStrategy(page);
    const info = await page.evaluate(() => {
      const headers = [...document.querySelectorAll('.opt-kv-greeks .kv-k-greek')];
      const values = [...document.querySelectorAll('.opt-kv-greeks .kv-v')];
      const shadowOf = (el) => getComputedStyle(el).boxShadow;
      return {
        count: headers.length,
        headerShadows: headers.map(shadowOf),
        firstValueShadow: shadowOf(values[0]),
        lastValueShadow: shadowOf(values[values.length - 1]),
      };
    });
    expect(info.count, 'Greeks must render all 5 header cells').toBe(5);
    for (const [i, shadow] of info.headerShadows.entries()) {
      expect(shadow, `header cell ${i} must have NO divider`).toBe('none');
    }
    expect(info.firstValueShadow, 'first Greek value must still carry the divider').toContain('inset');
    expect(info.lastValueShadow, 'last Greek value (rho) must have NO divider').toBe('none');
  });

  test('Risk & EV: no header cell carries a box-shadow divider at either the narrow or >=1180px breakpoint', async ({ page }) => {
    for (const vp of [{ width: 1000, height: 900 }, { width: 1280, height: 900 }]) {
      await page.setViewportSize(vp);
      await _gotoDerivativesWithStrategy(page);
      const info = await page.evaluate(() => {
        const headers = [...document.querySelectorAll('.opt-kv-risk .kv-risk-colhdr')]
          .filter((el) => getComputedStyle(el).display !== 'none');
        const shadowOf = (el) => getComputedStyle(el).boxShadow;
        return {
          visibleHeaderCount: headers.length,
          headerShadows: headers.map(shadowOf),
        };
      });
      const expectedCount = vp.width >= 1180 ? 6 : 4;
      expect(info.visibleHeaderCount, `visible Risk & EV header count at ${vp.width}px`).toBe(expectedCount);
      for (const [i, shadow] of info.headerShadows.entries()) {
        expect(shadow, `header cell ${i} at ${vp.width}px must have NO divider`).toBe('none');
      }
    }
  });
});
