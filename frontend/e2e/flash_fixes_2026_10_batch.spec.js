/**
 * flash_fixes_2026_10_batch.spec.js
 *
 * Covers the user-visible behaviour of items 3, 4, 5, 6, 9, 10 from the
 * "GOLD prev_close race + positions fallback + flash correctness" batch
 * (.claude/PLAN.md). Items 1 and 2 (data-layer close/prev_close fixes) and
 * item 10 (tickFlash.svelte.js's Math.abs(last) fix) are pure-JS and get
 * Vitest coverage instead (src/lib/__tests__/data/) per this project's
 * test-location convention — see marketDataStoresMeta.test.js,
 * pulseUnified.test.js, and tickFlash.test.js.
 *
 * Part A (live DOM, runs against the local/dev server): MarketPulse's
 * tickBus subscriber is fed directly from the emitted {sym, dir} event —
 * UPDATED 2026-10-19 (operator decision: drop percentage-threshold gating
 * entirely): the earlier "accumulate until the cumulative move crosses a
 * pct threshold" logic (and the ltpFlashPct-driven gate it replaced) are
 * both gone. symbolStore's own tickBus.emit() already only fires when the
 * LTP genuinely changed, so the subscriber flashes on every real change,
 * no magnitude check at all. These tests still drive real LTP writes via
 * `window.__stores.mergeSymbolUpdate` (the same production write path real
 * SSE ticks use) so the assertions exercise the real write→emit→flash
 * path end to end (see tick_flash_accumulate_until_threshold.spec.js for
 * the GOLD-sized-tick-specific coverage of this same redesign).
 *   - Item 9 (superseded 2026-10-19): a sub-threshold tick now ALSO
 *     flashes — there is no threshold left to gate on. This spec asserts
 *     the new behaviour (every real change flashes, regardless of size).
 *   - Item 3: the Chg% (day_pnl_pct) cell never gets tf-up/tf-down, even
 *     while the LTP cell on the SAME row is actively flashing.
 *
 * Part B (source-scan regression guards, no server needed): Derivatives'
 * tick handler (+page.svelte) additionally depends on symbolStore's own
 * `getSnapshot(root)` read to source the flash VALUE — reproducing a real
 * LTP change end-to-end would require mocking the SSE quote stream
 * (EventSource) or exposing a new symbolStore write hook on window, both
 * beyond this change's scope (not in the critical-files list, and risks
 * colliding with the parallel agent's OptionsPayoff.svelte/PerformancePage.svelte
 * work in the same layout file). Mirrors the exact "stale-code guard"
 * convention already established in derivatives_legs_ltp_flash.spec.js
 * (whose own live-DOM tests are `test.skip` for the identical reason —
 * dependency on real broker position data this sandbox doesn't have).
 *   - Item 4: CandidateLegRow.svelte's Chg% cell reuses the `:ltp` flash
 *     key (not a separate `:chg` key); +page.svelte no longer writes a
 *     `leg:${k}:chg` key anywhere (poll or live tick).
 *   - Item 5: +page.svelte's live-tick leg loop skips flash.update for
 *     qty=0 (closed) legs.
 *   - Item 6: +page.svelte's poll effects no longer call flash.update for
 *     the `:ltp` keys (Snapshot underlying + Legs per-row) — only the
 *     live-tick handler does.
 *   - Should-fix: Legs underlying-anchor row now wires
 *     flash.classOf(`${legsUnderlyingAnchor.underlying}:ltp`) — confirmed
 *     to share the Snapshot grid's own key for the same underlying.
 */

import { test, expect } from '@playwright/test';
import fs from 'fs';
import path from 'path';
import { loginAsAdmin } from './fixtures/auth.js';

const ACCT = 'ZG0790';
const SYM = 'FLASHTEST01'; // synthetic — doesn't collide with any real holding
const POSITIONS_RE = /\/api\/positions\/(\?.*)?$/;

const ROW = {
  tradingsymbol: SYM, symbol: SYM, exchange: 'NSE',
  account: ACCT, product: 'CNC', quantity: 10, average_price: 100, last_price: 105,
  pnl: 50, realised: 0, unrealised: 50, day_pnl: 50, prev_settlement_pnl: 0,
  prev_close: 100, close_price: 100,
  pair_group_key: null, is_orphan: false, has_gtt: false,
};

async function mockPositions(page) {
  await page.route(POSITIONS_RE, async (route) => {
    if (route.request().method() !== 'GET') { await route.continue(); return; }
    let body = { rows: [] };
    try {
      const real = await route.fetch();
      body = { ...(await real.json()) };
    } catch (_) { /* backend unreachable — minimal envelope below */ }
    body.rows = [ROW];
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
}

/**
 * Write a real LTP value through the production write path
 * (symbolStore._mergeSymbolWrite → tickBus.emit), via the
 * window.__stores.mergeSymbolUpdate dev hook — exercises the real
 * write→emit→flash path end to end rather than a bare tickBus.emit().
 */
async function writeLtp(page, sym, ltp) {
  await page.evaluate(
    ([s, v]) => {
      /** @type {any} */
      const stores = window.__stores;
      if (!stores?.mergeSymbolUpdate) throw new Error('window.__stores.mergeSymbolUpdate not found');
      stores.mergeSymbolUpdate(s, { ltp: v }, { ltp_ts: Date.now() });
    },
    [sym, ltp],
  );
}

/** Read back the symbol's current live LTP from symbolStore. */
async function readLtp(page, sym) {
  return page.evaluate(
    (s) => {
      /** @type {any} */
      const stores = window.__stores;
      return stores?.getSnapshot?.(s)?.ltp ?? null;
    },
    sym,
  );
}

/**
 * Retry-write large jumps until the target locator's class matches
 * `classRe`, or `maxMs` elapses. Needed because MarketPulse's OWN onMount
 * async chain (which reaches its `tickBus.subscribe(...)` call only after
 * a real backend round-trip — `loadPulse({force:true})` →
 * positions/holdings/accounts/movers/funds/batchQuote) comes up with
 * latency that varies with this shared dev server's cold-cache state
 * (observed 0.4s–10s+ for a single endpoint in manual probing). A single
 * write timed too early is silently missed (no subscriber registered yet)
 * rather than failing the check it's gating — so this retries at a cadence
 * respecting the bus's own 250ms per-symbol throttle until the real
 * subscription is live. Each retry multiplies by 1.05 (+5%) so a genuinely
 * fresh write always clears the threshold once the subscription comes up,
 * regardless of how many earlier attempts were silently dropped.
 */
async function writeUntilClassMatches(page, locator, sym, startLtp, classRe, maxMs = 45_000) {
  const start = Date.now();
  let val = startLtp;
  while (Date.now() - start < maxMs) {
    await writeLtp(page, sym, val);
    // Check while the flash is still active (classes clear ~300ms after
    // a registered tick — see MarketPulse.svelte's _ltpFlashTimers), but
    // wait a little first so the debounced refreshCells (~50ms window)
    // has run.
    await page.waitForTimeout(150);
    const matched = await locator.evaluate(
      (el, re) => new RegExp(re).test(el.className),
      classRe.source,
    ).catch(() => false);
    if (matched) return true;
    // Space out the next attempt past the bus's 250ms per-sym throttle.
    await page.waitForTimeout(150);
    val = val * 1.05;
  }
  return false;
}

test.describe('Part A — MarketPulse live DOM (window.__stores.tickBus)', () => {
  // Generous timeout — MarketPulse's onMount awaits a real backend
  // round-trip (loadPulse: positions/holdings/accounts/movers/funds/
  // batchQuote) before it ever reaches the tickBus.subscribe() call this
  // spec exercises. `window.__stores.tickBus` itself is exposed earlier,
  // independently, by the (algo) layout's OWN onMount — its presence is
  // NOT proof MarketPulse's subscription is live yet. Manual probing of
  // this shared dev-server environment observed single real endpoint
  // round-trips up to ~10s on a cold cache, so the "warm-up" step below
  // retries (not a single blind wait) until the real subscription proves
  // itself live via an actual observed flash.
  test.setTimeout(120_000);

  test('item 9 (superseded): a small tick that used to be sub-threshold now flashes too, just like a large tick', async ({ page }) => {
    await loginAsAdmin(page);
    await mockPositions(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    // ag-Grid renders pinned-left columns (pos_state/tradingsymbol) and
    // center columns (ltp/day_pnl_pct/...) as TWO SEPARATE DOM row elements
    // sharing the same row-id — `.filter({ has: ... })` picks the one
    // that actually contains the center-column cell we need.
    const row = page.locator(`[row-id="${SYM}::${ACCT}__pos"]`)
      .filter({ has: page.locator('[col-id="ltp"]') }).first();
    const ltpCell = row.locator('[col-id="ltp"]');
    await expect(ltpCell).toBeVisible({ timeout: 60_000 });

    // Warm-up: retry a large write until MarketPulse's real tickBus
    // subscription proves itself live via an observed flash.
    const warmedUp = await writeUntilClassMatches(page, ltpCell, SYM, ROW.last_price, /tf-up/, 60_000);
    expect(warmedUp, 'MarketPulse tickBus subscription never came up — real backend round-trip too slow in this environment').toBe(true);
    // Let the flash fully clear (300ms clear timer) before the real check.
    await expect(ltpCell).not.toHaveClass(/tf-up/, { timeout: 2_000 });
    await page.waitForTimeout(300);

    // Read back the real baseline MarketPulse just locked in (the warm-up
    // flash's own value) so the percentage math below is exact regardless
    // of how many warm-up retries were needed.
    const baseline = await readLtp(page, SYM);
    expect(baseline).toBeGreaterThan(0);

    // A 0.05% move — under the OLD default 0.1% ltpFlashPct gate this would
    // NOT have flashed. With threshold gating removed entirely (operator
    // decision 2026-10-19), this real change DOES flash.
    await writeLtp(page, SYM, baseline * 1.0005);
    await expect(ltpCell).toHaveClass(/tf-up/, { timeout: 2_000 });

    // A much larger move also flashes, same as before — confirms removing
    // the gate didn't break the large-move case.
    await expect(ltpCell).not.toHaveClass(/tf-up/, { timeout: 2_000 });
    await page.waitForTimeout(300); // past the bus's 250ms per-sym throttle
    const baseline2 = await readLtp(page, SYM);
    await writeLtp(page, SYM, baseline2 * 1.05);
    await expect(ltpCell).toHaveClass(/tf-up/, { timeout: 2_000 });
  });

  test('item 3: Chg% cell never flashes even while the LTP cell on the same row is flashing', async ({ page }) => {
    await loginAsAdmin(page);
    await mockPositions(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    const row = page.locator(`[row-id="${SYM}::${ACCT}__pos"]`)
      .filter({ has: page.locator('[col-id="ltp"]') }).first();
    const ltpCell = row.locator('[col-id="ltp"]');
    const chgCell = row.locator('[col-id="day_pnl_pct"]');
    await expect(ltpCell).toBeVisible({ timeout: 60_000 });
    await expect(chgCell).toBeVisible({ timeout: 10_000 });

    // Retry (see Part A describe-block comment) until the LTP cell
    // actually flashes, confirming the write genuinely registered.
    const flashed = await writeUntilClassMatches(page, ltpCell, SYM, ROW.last_price, /tf-up/, 60_000);
    expect(flashed, 'MarketPulse tickBus subscription never came up — real backend round-trip too slow in this environment').toBe(true);
    // Chg% cell on the SAME row must never carry tf-up/tf-down —
    // PULSE_SPEC.md §16: only LTP flashes on Pulse.
    await expect(chgCell).not.toHaveClass(/tf-up/);
    await expect(chgCell).not.toHaveClass(/tf-down/);

    // Let the up-flash clear, then confirm the down-direction case too.
    await expect(ltpCell).not.toHaveClass(/tf-up/, { timeout: 2_000 });
    await page.waitForTimeout(300);
    const baseline = await readLtp(page, SYM);
    expect(baseline).toBeGreaterThan(0);
    await writeLtp(page, SYM, baseline * 0.94); // -6% — well past threshold, down direction
    await expect(ltpCell).toHaveClass(/tf-down/, { timeout: 2_000 });
    await expect(chgCell).not.toHaveClass(/tf-up/);
    await expect(chgCell).not.toHaveClass(/tf-down/);
  });
});

test.describe('Part B — Derivatives Legs flash architecture (source-scan regression guards)', () => {
  const pagePath = path.join(
    process.cwd(),
    'src/routes/(algo)/admin/derivatives/+page.svelte',
  );
  const legRowPath = path.join(
    process.cwd(),
    'src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte',
  );
  const pageSrc = fs.readFileSync(pagePath, 'utf-8');
  const legRowSrc = fs.readFileSync(legRowPath, 'utf-8');
  // Naive line-comment stripper for "must NOT contain" assertions below —
  // several of this change's own explanatory `//` comments intentionally
  // quote the OLD (now-removed) call syntax for context, which would
  // otherwise self-collide with a raw substring/regex check against the
  // full source. Code-only view avoids that false positive. Indices for
  // locating a region are still found via the ORIGINAL pageSrc (comments
  // are useful anchors); only the "must not match" check runs against a
  // stripped version of that specific slice.
  function stripLineComments(text) {
    return text
      .split('\n')
      .map((line) => line.replace(/\/\/.*$/, ''))
      .join('\n');
  }

  test('item 4: CandidateLegRow Chg% cell reuses the :ltp flash key, not a separate :chg key', async () => {
    // The Chg% span's flash.classOf call must key off `${_legFlashKey}:ltp`
    // (same key the LTP span uses), mirroring Snapshot's own Chg%-reuses-
    // :ltp pattern (+page.svelte line ~6222).
    const chgSpanMatch = legRowSrc.match(
      /cand-chg-sep[^>]*flash\.classOf\(`\$\{_legFlashKey\}:(ltp|chg)`\)/,
    );
    expect(chgSpanMatch, 'Chg% span (cand-chg-sep) not found in CandidateLegRow.svelte').not.toBeNull();
    expect(chgSpanMatch[1]).toBe('ltp');
  });

  test('item 4: no leg:${k}:chg flash key remains anywhere in +page.svelte (poll or live tick)', async () => {
    expect(stripLineComments(pageSrc)).not.toMatch(/leg:\$\{k\}:chg/);
  });

  test('item 5: live-tick leg loop skips flash.update for qty=0 (closed) legs', async () => {
    // The CandidateLegRow LTP re-arm loop (tickBus.subscribe handler) must
    // skip closed legs before calling flash.update — same qty=0 freeze
    // convention CandidateLegRow.svelte's own `isClosed` derived uses.
    const tickHandlerSection = pageSrc.slice(
      pageSrc.indexOf('CandidateLegRow LTP cells — re-arm for each leg'),
      pageSrc.indexOf('CandidateLegRow LTP cells — re-arm for each leg') + 1200,
    );
    expect(tickHandlerSection).toMatch(/if\s*\(Number\(c\.qty \|\| 0\) === 0\)\s*continue;/);
  });

  test('item 6: poll effects no longer call flash.update for the Snapshot underlying :ltp key', async () => {
    // The old poll-path write (inside the `_underlyingQuotes` $effect) must
    // be gone — flash-tracking for this key now comes only from the
    // live-tick handler. Scoped to the effect's own region (identified by
    // its surrounding comment) rather than the whole file, since the
    // live-tick handler further down legitimately still writes this key.
    const idx = pageSrc.indexOf('Item 6 fix (2026-10)');
    expect(idx, 'Item 6 fix comment (poll-effect removal) not found').toBeGreaterThan(-1);
    const nextEffectIdx = pageSrc.indexOf('Payoff header chips', idx);
    const removedEffectRegion = stripLineComments(pageSrc.slice(idx, nextEffectIdx));
    expect(removedEffectRegion).not.toMatch(/flash\.update\(/);
  });

  test('item 6: poll effect for Legs per-row no longer writes leg:${k}:ltp (day/pnl/exp still do)', async () => {
    // Locate the Legs per-row poll effect (day/pnl/exp/ltp/chg block) by its
    // distinctive comment and confirm the :ltp write was removed while the
    // other three sibling writes remain intact.
    const idx = pageSrc.indexOf('Legs per-row: Day P&L, P&L, Exp P&L');
    expect(idx, 'Legs per-row poll effect comment not found').toBeGreaterThan(-1);
    const block = pageSrc.slice(idx, idx + 1500);
    expect(block).toMatch(/flash\.update\(`leg:\$\{k\}:day`/);
    expect(block).toMatch(/flash\.update\(`leg:\$\{k\}:pnl`/);
    expect(block).toMatch(/flash\.update\(`leg:\$\{k\}:exp`/);
    expect(block).not.toMatch(/flash\.update\(`leg:\$\{k\}:ltp`/);
    expect(block).not.toMatch(/flash\.update\(`leg:\$\{k\}:chg`/);
  });

  test('item 6: live-tick handler is the sole writer of leg:${k}:ltp and ${root}:ltp', async () => {
    // tickBus.subscribe handler block still writes both keys.
    const idx = pageSrc.indexOf('tickBus.subscribe(({ sym }) => {');
    expect(idx, 'tickBus.subscribe handler not found').toBeGreaterThan(-1);
    const block = pageSrc.slice(idx, idx + 4200);
    expect(block).toMatch(/flash\.update\(`\$\{root\}:ltp`/);
    expect(block).toMatch(/flash\.update\(`leg:\$\{k\}:ltp`/);
  });

  test('should-fix: Legs underlying-anchor row wires flash.classOf keyed on its own underlying\'s :ltp', async () => {
    expect(pageSrc).toMatch(
      /flash\.classOf\(`\$\{legsUnderlyingAnchor\.underlying\}:ltp`\)/,
    );
    // Confirm the anchor row's own underlying field is the SAME value
    // Snapshot's by-underlying grid groups on (selectedUnderlying) — the
    // explicit confirmation this fix required before wiring the key.
    const anchorDeclIdx = pageSrc.indexOf('const legsUnderlyingAnchor = $derived.by');
    expect(anchorDeclIdx).toBeGreaterThan(-1);
    const anchorBlock = pageSrc.slice(anchorDeclIdx, anchorDeclIdx + 800);
    expect(anchorBlock).toMatch(/underlying:\s*selectedUnderlying,/);
  });
});
