/**
 * order_ticket_depth_chain_decoration.spec.js
 *
 * Operator (2026-09-30): "apply background and border decoration of chain to
 * order ticket depth" — then simplified mid-task to "just subtle border and
 * background. the last row with buy levels text can have a different
 * background", and "it should be subtle in sync with chain" (reuse Chain's
 * own literal alpha values, not new numbers picked by eye). That round
 * explicitly REJECTED a per-side bid=green/ask=orange wash design.
 *
 * Operator (2026-10-31): "chain border lines for strikes and order border
 * line for depth are not in sync. reuse chain border and look to order
 * ticket. follow different background shade ask qty and price, and bid qty
 * and price" — two fixes, both in OrderDepth.svelte:
 *
 *   1. Live screenshot comparison (desktop + 375px mobile) found the real
 *      discrepancy: `.ot-depth-grid`'s 0.15rem row-gap left the card's
 *      plain dark background showing between each row-wash band, so the
 *      border-bottom read as a series of short disconnected dashes instead
 *      of Chain's one continuous hairline (Chain's `<table>` rows abut with
 *      zero gap — border-bottom is the ONLY vertical separation). Fixed by
 *      zeroing the row-gap and restoring the removed vertical space via
 *      `.ot-depth-cell { padding-block: 0.075rem }` instead, so the overall
 *      ladder height is unchanged.
 *   2. The operator now explicitly asks for the per-side bid/ask tint that
 *      round 1 rejected — a direct, deliberate reversal (same class as the
 *      Chain ITM hue swap, 2026-10-25, CLAUDE.md), not an oversight. Two new
 *      per-row wash elements (`.ot-depth-row-wash-bid` / `-ask`) each span 2
 *      of the 4 explicit column tracks and bleed half the column-gap so the
 *      two fills meet exactly at the Bid|Ask boundary regardless of column
 *      width. `.ot-depth-row-wash` itself keeps ONLY the border-bottom
 *      divider now (its old flat background-color is gone).
 *
 * Source-grep only, matching this codebase's established convention for
 * pinning exact CSS decoration values (see
 * chain_atm_softening_and_leg_badge.spec.js) — these are static CSS rules,
 * not data-dependent rendering, so a live-server computed-style probe would
 * add flakiness (market hours / broker depth availability) for no
 * additional coverage over a direct source assertion. A live rendering
 * check (border continuity + bid/ask tint divergence) is added separately
 * below, gated to skip gracefully when login/the modal is unavailable, the
 * way order_modal_depth_populated.spec.js does.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';

const DEPTH_PATH = path.resolve(
  import.meta.dirname ?? new URL('.', import.meta.url).pathname,
  '../src/lib/order/OrderDepth.svelte',
);
const CHAIN_TAB_PATH = path.resolve(
  import.meta.dirname ?? new URL('.', import.meta.url).pathname,
  '../src/lib/order/OptionChainTab.svelte',
);

test.describe('OrderDepth — row divider reuses Chain\'s own literal alpha + zero row-gap (continuous line)', () => {
  test('Chain\'s row-divider alpha (0.025) is a real, current value in OptionChainTab.svelte', () => {
    // Guards the premise: if Chain's own divider alpha ever changes,
    // this test (and the depth ladder's copy below) should be revisited
    // in lockstep.
    const chainSrc = readFileSync(CHAIN_TAB_PATH, 'utf8');
    expect(chainSrc).toMatch(/\.chain-row > td \{[^}]*border-bottom: 1px solid rgba\(255,255,255,0\.025\)/);
  });

  test('Chain\'s data rows have zero vertical gap between them — border-bottom is the only separator', () => {
    // Chain is a <table> with border-collapse; rows abut by construction.
    // This guards the premise the 2026-10-31 row-gap fix is built on: there
    // is no competing row-gap/margin mechanism on Chain's own rows to stay
    // in sync with.
    const chainSrc = readFileSync(CHAIN_TAB_PATH, 'utf8');
    expect(chainSrc).not.toMatch(/\.chain-row\s*>\s*td\s*\{[^}]*(margin-bottom|row-gap)/);
  });

  test('.ot-depth-grid has zero row-gap (2026-10-31 fix) — only the column-gap remains', () => {
    const src = readFileSync(DEPTH_PATH, 'utf8');
    const rule = src.match(/\.ot-depth-grid\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).toMatch(/gap:\s*0\s+0\.9rem/);
  });

  test('.ot-depth-cell compensates the removed row-gap via padding-block, not a new row-gap', () => {
    const src = readFileSync(DEPTH_PATH, 'utf8');
    const rule = src.match(/\.ot-depth-cell\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).toMatch(/padding-block:\s*0\.075rem/);
  });

  test('.ot-depth-row-wash carries a border-bottom divider at Chain\'s exact 0.025 alpha', () => {
    // 2026-10-29 (operator: "extend the background shading for depth to
    // container left to right. no gap in background shading between bid
    // and ask and other numbers") — the wash + divider moved OFF the
    // per-cell .ot-depth-cell rule onto a full-bleed .ot-depth-row-wash
    // layer (one continuous band per row instead of 4 separate chips
    // with visible gaps at the column-gap and the grid's centering
    // gutter) — same technique, same alpha values, as .ot-depth-header-bg
    // above. Named "-wash", not "-bg", to avoid colliding with the
    // "Stale-code" guard below, which bans a DIFFERENT, unrelated prior
    // design (.ot-depth-row-bg, the dropped per-side bid=green/ask=orange
    // + best-row-marker scaffolding).
    const src = readFileSync(DEPTH_PATH, 'utf8');
    const rule = src.match(/\.ot-depth-row-wash::before\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).toMatch(/border-bottom:\s*1px solid rgba\(255,255,255,0\.025\)/);
  });

  test('.ot-depth-row-wash::before no longer carries its own flat background-color (2026-10-31 — fill moved to the per-side wash divs)', () => {
    const src = readFileSync(DEPTH_PATH, 'utf8');
    const rule = src.match(/\.ot-depth-row-wash::before\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).not.toMatch(/background-color:/);
  });

  test('the final depth row drops the divider, mirroring Chain\'s .chain-row:last-child > td { border-bottom: 0 }', () => {
    const src = readFileSync(DEPTH_PATH, 'utf8');
    expect(src).toMatch(/class:ot-depth-row-wash-last=\{isLastRow\}/);
    const rule = src.match(/\.ot-depth-row-wash-last::before\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).toMatch(/border-bottom:\s*none/);
  });

  test('.ot-depth-cell itself paints no background/border beyond its own padding — fill/divider painting lives on the wash divs', () => {
    const src = readFileSync(DEPTH_PATH, 'utf8');
    const rule = src.match(/\.ot-depth-cell\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).not.toMatch(/background-color:/);
    expect(rule).not.toMatch(/border-bottom:/);
  });

  test('isLastRow is computed from the real row index, not hardcoded', () => {
    const src = readFileSync(DEPTH_PATH, 'utf8');
    expect(src).toMatch(/\{@const isLastRow = i === buyRows\.length - 1\}/);
  });
});

test.describe('OrderDepth — per-side bid/ask background tint (2026-10-31 reversal)', () => {
  test('a dedicated bid-side wash div renders per row, spanning the bid-qty + bid columns (1/3)', () => {
    const src = readFileSync(DEPTH_PATH, 'utf8');
    expect(src).toContain('<div class="ot-depth-row-wash-bid" style="grid-row:{gridRow}" aria-hidden="true"></div>');
    // Targets the standalone one-liner rule specifically, not the earlier
    // shared `.ot-depth-row-wash-bid,\n.ot-depth-row-wash-ask { position:
    // relative; }` selector block both classes also appear in.
    expect(src).toMatch(/\.ot-depth-row-wash-bid\s*\{\s*grid-column:\s*1\s*\/\s*3;\s*\}/);
  });

  test('a dedicated ask-side wash div renders per row, spanning the ask + ask-qty columns (3/5)', () => {
    const src = readFileSync(DEPTH_PATH, 'utf8');
    expect(src).toContain('<div class="ot-depth-row-wash-ask" style="grid-row:{gridRow}" aria-hidden="true"></div>');
    expect(src).toMatch(/\.ot-depth-row-wash-ask\s*\{\s*grid-column:\s*3\s*\/\s*5;\s*\}/);
  });

  test('bid wash reuses the app\'s green token (74,222,128) and ask wash reuses the red token (248,113,113), both at the subtle 0.03 tier', () => {
    const src = readFileSync(DEPTH_PATH, 'utf8');
    const bidRule = src.match(/\.ot-depth-row-wash-bid::before\s*\{[^}]*\}/)?.[0] ?? '';
    const askRule = src.match(/\.ot-depth-row-wash-ask::before\s*\{[^}]*\}/)?.[0] ?? '';
    expect(bidRule).toMatch(/background-color:\s*rgba\(74,222,128,0\.03\)/);
    expect(askRule).toMatch(/background-color:\s*rgba\(248,113,113,0\.03\)/);
  });

  test('the two washes bleed exactly half the 0.9rem column-gap (0.45rem) so they meet at the gap midpoint, not a fixed 50% split', () => {
    const src = readFileSync(DEPTH_PATH, 'utf8');
    const bidRule = src.match(/\.ot-depth-row-wash-bid::before\s*\{[^}]*\}/)?.[0] ?? '';
    const askRule = src.match(/\.ot-depth-row-wash-ask::before\s*\{[^}]*\}/)?.[0] ?? '';
    expect(bidRule).toMatch(/inset:\s*0\s+-0\.45rem\s+0\s+-9999px/);
    expect(askRule).toMatch(/inset:\s*0\s+-9999px\s+0\s+-0\.45rem/);
  });
});

test.describe('OrderDepth — "the last row with buy levels text" gets a different background', () => {
  test('the rendered "Buy levels" label sits inside .ot-depth-diag — confirms this is the targeted row', () => {
    const src = readFileSync(DEPTH_PATH, 'utf8');
    // The literal rendered span, not a comment mentioning the same words.
    const labelIdx = src.indexOf('<span class="ot-depth-diag-lbl">Buy levels</span>');
    expect(labelIdx).toBeGreaterThan(-1);
    const diagOpenIdx = src.indexOf('<div class="ot-depth-diag"');
    const diagCloseIdx = src.indexOf('</div>', diagOpenIdx);
    expect(diagOpenIdx).toBeGreaterThan(-1);
    expect(labelIdx).toBeGreaterThan(diagOpenIdx);
    expect(labelIdx).toBeLessThan(diagCloseIdx);
    // And it must NOT sit inside the bid/ask grid above (.ot-depth-grid).
    const gridOpenIdx = src.indexOf('<div class="ot-depth-grid">');
    const gridCloseIdx = src.indexOf('</div>', src.indexOf('{/each}', gridOpenIdx));
    expect(labelIdx).toBeGreaterThan(gridCloseIdx);
  });

  test('.ot-depth-diag background reuses Chain\'s ATM-row literal alpha (0.05), distinct from the per-side 0.03 tint', () => {
    const src = readFileSync(DEPTH_PATH, 'utf8');
    const rule = src.match(/\.ot-depth-diag\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).toMatch(/background-color:\s*rgba\(255,255,255,0\.05\)/);
  });

  test('Chain\'s own ATM-row shade alpha (0.05) is a real, current value — the depth ladder\'s copy stays in sync', () => {
    const chainSrc = readFileSync(CHAIN_TAB_PATH, 'utf8');
    const rule = chainSrc.match(/\.chain-row-atm\s*>\s*td\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).toMatch(/background-image:\s*linear-gradient\(rgba\(255,255,255,0\.05\),\s*rgba\(255,255,255,0\.05\)\)/);
  });
});

test.describe('Stale-code: no residual elaborate best-row-marker design', () => {
  // The operator's first 2026-09-30 brief asked for Chain's full ITM/OTM-
  // style wash mapping (bid=green/ask=orange) plus an ATM-style box-shadow
  // "best row" marker on row 0. Both were explicitly simplified away
  // mid-task. The bid/ask color mapping itself was explicitly REVERSED
  // back in on 2026-10-31 (see .ot-depth-row-wash-bid/-ask above) — this
  // guard now only confirms the OLD, abandoned `.ot-depth-row-bg` scaffold
  // (a different implementation shape: per-side background directly on a
  // single div named "-bg", plus a best-row marker) never came back, not
  // that no per-side color exists at all.
  test('no .ot-depth-row-bg scaffolding (the dropped single-div bid/ask-half background design) remains', () => {
    const src = readFileSync(DEPTH_PATH, 'utf8');
    expect(src).not.toContain('ot-depth-row-bg');
  });

  test('no best-row box-shadow marker remains on the data rows', () => {
    const src = readFileSync(DEPTH_PATH, 'utf8');
    expect(src).not.toContain('ot-depth-row-bg-best');
    expect(src).not.toMatch(/box-shadow:\s*inset 0 1px 0 rgba\(251,191,36/);
  });
});

// ─────────────────────────────────────────────────────────────────────────
// Live rendering check — border continuity + bid/ask tint divergence.
// Skips gracefully (not a failure) when login or the order modal isn't
// reachable, the same convention as order_modal_depth_populated.spec.js.
// ─────────────────────────────────────────────────────────────────────────

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
const _AUTH_USER = process.env.PLAYWRIGHT_USER || 'rambo';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

async function _loginToken(page) {
  const r = await page.request.post(`${BASE}/api/auth/login`, {
    data: { username: _AUTH_USER, password: _AUTH_PASS },
    timeout: 15_000,
  }).catch(() => null);
  if (!r || !r.ok()) return null;
  return (await r.json()).access_token;
}

test.describe('OrderDepth — live render: continuous border + bid/ask tint divergence', () => {
  test.setTimeout(60_000);

  test('row-wash border-bottom color matches Chain\'s own, and bid-side/ask-side computed backgrounds differ', async ({ page }) => {
    const token = await _loginToken(page);
    test.skip(!token, 'auth unavailable — skipping live render check');

    await page.addInitScript((tok) => { sessionStorage.setItem('ramboq_token', tok); }, token);
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2000);

    const orderBtn = page.locator('button.pha-order').first();
    test.skip((await orderBtn.count()) === 0, 'no order-entry button found on /dashboard');
    await orderBtn.click({ force: true });
    await page.waitForTimeout(1500);

    // Pick a symbol with F&O depth so the row scaffold is populated
    // (5 rows render regardless of live quote availability).
    const symInput = page.locator('input.ssi-input').first();
    if (await symInput.count()) {
      await symInput.click();
      await symInput.fill('CRUDEOIL');
      await page.waitForTimeout(1200);
      const firstRow = page.locator('.ssi-row').first();
      if (await firstRow.count()) {
        await firstRow.click();
        await page.waitForTimeout(1200);
      }
    }

    const depth = page.locator('.ot-depth').first();
    test.skip((await depth.count()) === 0, 'OrderDepth not mounted');

    const washRow = page.locator('.ot-depth-row-wash').first();
    test.skip((await washRow.count()) === 0, 'no depth rows rendered');

    const borderColor = await washRow.evaluate((el) => {
      const cs = getComputedStyle(el, '::before');
      return cs.borderBottomColor;
    });
    // Chain's own literal divider color: rgba(255,255,255,0.025). Browsers
    // quantize the serialized alpha channel to the nearest 1/255 step, so
    // compare channels with tolerance rather than an exact string match
    // (observed: Chromium serializes 0.025 as 0.024).
    const m = borderColor.match(/rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+))?\s*\)/);
    expect(m, `unparseable color: ${borderColor}`).toBeTruthy();
    expect(Number(m[1])).toBe(255);
    expect(Number(m[2])).toBe(255);
    expect(Number(m[3])).toBe(255);
    expect(Number(m[4])).toBeCloseTo(0.025, 2);

    const bidWash = page.locator('.ot-depth-row-wash-bid').first();
    const askWash = page.locator('.ot-depth-row-wash-ask').first();
    const [bidBg, askBg] = await Promise.all([
      bidWash.evaluate((el) => getComputedStyle(el, '::before').backgroundColor),
      askWash.evaluate((el) => getComputedStyle(el, '::before').backgroundColor),
    ]);
    expect(bidBg).not.toBe(askBg);
    expect(bidBg.replace(/\s/g, '')).toContain('74,222,128');
    expect(askBg.replace(/\s/g, '')).toContain('248,113,113');

    // Bid-wash's right edge and ask-wash's left edge each bleed by the
    // same 0.45rem (half the 0.9rem column-gap) in opposite directions,
    // so the two painted fills meet exactly at the gap's midpoint — not
    // a boundingBox comparison (boundingBox reads the host div's own
    // grid-track box, not its ::before's bled paint area).
    const [bidRight, askLeft] = await Promise.all([
      bidWash.evaluate((el) => getComputedStyle(el, '::before').right),
      askWash.evaluate((el) => getComputedStyle(el, '::before').left),
    ]);
    // 0.45rem at the default 16px root == -7.2px bleed past the host's
    // own right/left edge respectively.
    expect(parseFloat(bidRight)).toBeCloseTo(-7.2, 0);
    expect(parseFloat(askLeft)).toBeCloseTo(-7.2, 0);
  });
});
