/**
 * derivatives_navstrip_redesign_regression.spec.js
 *
 * Playwright coverage for the NavStrip reactivity/Day P&L + derivatives
 * Exp P&L/Snapshot/Payoff price-basis unification plan (see
 * .claude/PLAN.md at implementation time). Extends the existing
 * derivatives e2e specs (derivatives_underlying_ltp_desync.spec.js,
 * derivatives_snapshot_spot_smoke.spec.js, navstrip_p_slot_derivatives.spec.js)
 * rather than duplicating their setup — same loginAsAdmin/seedToken
 * pattern, same graceful test.skip(!_sharedJwt, ...) fallback when the
 * server is unreachable.
 *
 * Three scenarios (per plan Verification section; tests 2 and 3 rewritten
 * post-audit to actually APPLY a filter / type in the search box during
 * the test — with no filter/search term applied the old and new code
 * compute identical numbers, so an unfiltered comparison never
 * discriminated pre-fix from post-fix behavior):
 *   1. Zero-flash regression — mount/remount PositionStrip must not
 *      flash a zero value when a prior non-zero value exists (§2 fix:
 *      _prevExecMode initializes from the actual current mode; PLUS the
 *      item-2 fix: dispPositionsToday/dispHoldingsToday initialize from
 *      the already-available store value on first render instead of
 *      unconditionally starting at 0).
 *   2. Snapshot-filter-consistency — picks a real account from the #opt-acct
 *      MultiSelect and narrows to it, then asserts the filtered TOTAL
 *      (Day P&L AND Extrinsic) equals the sum of the now-VISIBLE
 *      (filtered) rows (§5/item-4 fix: Day/Exp/Extrinsic values now
 *      thread the same account/strategy filter as the row set).
 *   3. Legs search-box decoupling — types a specific leg's own symbol
 *      into the Legs card's search box (narrowing the grid to fewer
 *      rows) and asserts Day P&L / P&L / Exp P&L TOTAL do NOT move
 *      (item-7 fix: TOTALs read _legsTotalsBase, unaffected by the
 *      search-filtered displayedCandidates).
 *
 * Five quality dimensions:
 *   1. SSOT   — assertions cross-check live-rendered numbers against each
 *               other (not against a hardcoded expectation), so they fail
 *               if any surface silently drifts from another.
 *   2. Perf   — no new long-task; reuses existing page-load waits.
 *   3. Stale  — static guards confirm the source-level fixes (§2 exec-mode
 *               init, §5/item-4/item-7 filter threading + reconciliation)
 *               are present, so a regression in a future edit is caught
 *               even without a live broker session.
 *   4. Reuse  — loginAsAdmin / seedToken / getPSlot1-style locators mirror
 *               navstrip_p_slot_derivatives.spec.js exactly.
 *   5. UX     — a zero-flash, a filtered-TOTAL divergence, or a search-box
 *               side-effect on TOTALs is exactly the class of
 *               operator-visible bug this plan fixes.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import path from 'path';

const BASE = process.env.PLAYWRIGHT_BASE_URL || process.env.BASE_URL || 'http://localhost:5174';

/**
 * Parse aggCompact()-formatted money text (see format.js) back to a plain
 * number. aggCompact abbreviates with K (×1e3) / L (×1e5, lakhs) / C
 * (×1e7, crores) suffixes above ₹1,000 — a naive digit-strip regex loses
 * that scale factor and silently produces false mismatches when comparing
 * a large aggCompact-formatted TOTAL against a sum of smaller unformatted
 * row values (or vice versa).
 * @param {string | null | undefined} s
 * @returns {number}
 */
function parseAggMoney(s) {
  const str = String(s ?? '').trim();
  if (!str || str === '—') return 0;
  const numPart = parseFloat(str.replace(/[^\d.-]/g, ''));
  if (!Number.isFinite(numPart)) return 0;
  const suffix = str.match(/([KLC])\s*$/i)?.[1]?.toUpperCase();
  if (suffix === 'K') return numPart * 1_000;
  if (suffix === 'L') return numPart * 100_000;
  if (suffix === 'C') return numPart * 10_000_000;
  return numPart;
}

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND_SRC = path.join(__dirname, '..', 'src');
const DERIV_FILE = path.join(FRONTEND_SRC, 'routes', '(algo)', 'admin', 'derivatives', '+page.svelte');
const STRIP_FILE = path.join(FRONTEND_SRC, 'lib', 'PositionStrip.svelte');

// ── Static guards (no browser needed) ──────────────────────────────────────

test.describe('§2/§5 static regression guards', () => {
  test('PositionStrip._prevExecMode initializes from the live store, not a hardcoded "idle"', () => {
    const content = readFileSync(STRIP_FILE, 'utf8');
    // Old (buggy) pattern: `let _prevExecMode = 'idle';` — a hardcoded
    // literal misreads every fresh mount as a market-open mode transition.
    expect(content).not.toMatch(/let _prevExecMode\s*=\s*'idle';/);
    // New pattern: reads the actual current mode via get(executionMode).
    expect(content).toMatch(/let _prevExecMode\s*=\s*get\(executionMode\)/);
  });

  test('PositionStrip dispPositionsToday/dispHoldingsToday initialize from the already-available store value, not a hardcoded 0', () => {
    const content = readFileSync(STRIP_FILE, 'utf8');
    // Old (buggy) pattern: unconditional `$state(0)` discards an
    // already-available warm-cache value on every fresh page load/reload.
    expect(content).not.toMatch(/let dispPositionsToday = \$state\(0\);/);
    expect(content).not.toMatch(/let dispHoldingsToday {2}= \$state\(0\);/);
    // New pattern: seeded from positionsDayPnlStore.total / holdingsDayPnlStore.total.
    expect(content).toMatch(/let dispPositionsToday = \$state\(positionsDayPnlStore\.total \|\| 0\);/);
    expect(content).toMatch(/let dispHoldingsToday {2}= \$state\(holdingsDayPnlStore\.total \|\| 0\);/);
  });

  test('PositionStrip modeChanged excludes ONLY the first (boot-placeholder) idle transition, not every idle-exit (item-6 fix, round 4)', () => {
    const content = readFileSync(STRIP_FILE, 'utf8');
    // Old (buggy, round-3) pattern: ANY transition OUT of 'idle' was
    // permanently exempt from resetting the buffers — but 'idle' is also
    // a genuine, operator-selectable mode (pickMode('idle') in
    // (algo)/+layout.svelte), so a real mid-session idle→live/paper
    // switch silently failed to reset. Must be gone.
    expect(content).not.toMatch(/const modeChanged = _prevExecMode !== 'idle' && _execMode !== _prevExecMode;/);
    // New pattern: a one-shot latch (_sawFirstModeChange) exempts only the
    // very FIRST observed mode change from an 'idle' start (the dev
    // _bootMode() placeholder resolving to the real mode) — every
    // subsequent transition, idle-sourced or not, resets normally.
    expect(content).toMatch(/let _sawFirstModeChange = false;/);
    expect(content).toMatch(/const _rawModeChanged = _execMode !== _prevExecMode;/);
    expect(content).toMatch(/const _isBootIdleResolution = _rawModeChanged && !_sawFirstModeChange && _prevExecMode === 'idle';/);
    expect(content).toMatch(/if \(_rawModeChanged\) _sawFirstModeChange = true;/);
    expect(content).toMatch(/const modeChanged = _rawModeChanged && !_isBootIdleResolution;/);
  });

  test('loadStrategy stamps _stratLastFetchAt BEFORE the await, not after (item-6 fix)', () => {
    const content = readFileSync(DERIV_FILE, 'utf8');
    const fnIdx = content.indexOf('async function loadStrategy(');
    expect(fnIdx, 'loadStrategy function must exist').toBeGreaterThan(-1);
    const stampIdx  = content.indexOf('_stratLastFetchAt = Date.now();', fnIdx);
    const awaitIdx  = content.indexOf('const resp    = await fetchStrategyAnalytics(cleanLegs, {});', fnIdx);
    expect(stampIdx, '_stratLastFetchAt stamp must exist inside loadStrategy').toBeGreaterThan(-1);
    expect(awaitIdx, 'fetchStrategyAnalytics await must exist inside loadStrategy').toBeGreaterThan(-1);
    // Old (buggy): stamped AFTER the await resolved — only
    // (5000ms − request latency) counted as "elapsed" by the next 5s
    // interval tick, degrading the effective cadence to ~10s, and left
    // the stamp stale WHILE a request was in flight (letting other
    // independent triggers fire overlapping requests).
    expect(stampIdx, 'stamp must come BEFORE the await, not after').toBeLessThan(awaitIdx);
  });

  test('Snapshot TOTAL sums are computed from filtered per-row values, not a selection-independent global sum', () => {
    const content = readFileSync(DERIV_FILE, 'utf8');
    // Old (buggy) pattern: summed positionsDerivedStore.byRootPositions
    // directly — unfiltered by account/strategy, and comment literally
    // said "selection-independent".
    expect(content).not.toMatch(
      /Object\.values\(positionsDerivedStore\.byRootPositions\)\.reduce\(\(s, v\) => s \+ \(v\?\.day_pnl/,
    );
    // New pattern: TOTAL sums the SAME _byUnderlyingTotals rows rendered
    // on screen (g.day_without / g.pnl_without).
    expect(content).toMatch(/_byUnderlyingTotals\.reduce\(\(s, g\) => s \+ \(g\.day_without/);
    expect(content).toMatch(/_byUnderlyingTotals\.reduce\(\(s, g\) => s \+ \(g\.pnl_without/);
    // Exp P&L TOTAL/row (item-7 reconciliation): _rowExpPnlFor is the
    // single accessor both the row and _snapshotTotalExp use — for the
    // selected underlying it returns _legsExpPnlTotal (the Legs-tab SSOT,
    // what-if aware); every other root falls back to the filtered
    // per-root reduction (_filteredExpPnlByRoot, built with the same
    // matchAccount/matchStrategy gate as the other columns).
    expect(content).toMatch(/function _rowExpPnlFor\(/);
    expect(content).toMatch(/_byUnderlyingTotals\.reduce\(\(s, g\) => s \+ _rowExpPnlFor\(g\.underlying\)/);
    expect(content).toMatch(/_filteredExpPnlByRoot\[underlying\]/);
    // Extrinsic TOTAL/row (item-4 fix): must also read a filtered,
    // per-root reduction (_filteredExtrinsicByRoot) rather than the
    // unfiltered whole-book positionsDerivedStore.total.extrinsic.
    expect(content).not.toMatch(
      /byund-row-total[\s\S]{0,400}positionsDerivedStore\.total\.extrinsic/,
    );
    expect(content).toMatch(/const _filteredExtrinsicByRoot = \$derived\.by/);
    expect(content).toMatch(/const _snapshotTotalExtrinsic = \$derived\.by/);
  });

  test('Legs TOTAL / Exp P&L / expiry offset read the search-unfiltered leg set', () => {
    const content = readFileSync(DERIV_FILE, 'utf8');
    expect(content).toMatch(/const _legsTotalsBase = \$derived\.by/);
    // The three totals must reference _legsTotalsBase, not the
    // search-filtered displayedCandidates.
    const legsDayIdx = content.indexOf('const _legsDayPnlTotal = $derived.by');
    const legsExpIdx = content.indexOf('const _legsExpPnlTotal = $derived.by');
    const offsetIdx  = content.indexOf('const _expiryPnlOffset = $derived.by');
    for (const idx of [legsDayIdx, legsExpIdx, offsetIdx]) {
      expect(idx).toBeGreaterThan(-1);
      const body = content.slice(idx, idx + 400);
      expect(body).toContain('_legsTotalsBase');
    }
  });

  test('_rowEvFor gates the live-EV branch on !_strategyStale (item-5 fix — no stale EV during a root switch)', () => {
    const content = readFileSync(DERIV_FILE, 'utf8');
    const fnIdx = content.indexOf('function _rowEvFor(');
    expect(fnIdx, '_rowEvFor function must exist').toBeGreaterThan(-1);
    const body = content.slice(fnIdx, fnIdx + 600);
    // Old (buggy) pattern: no _strategyStale check — during the one-frame
    // window between selectedUnderlying changing and loadStrategy landing,
    // the PREVIOUS root's _mergedEv briefly displayed on the NEW root's row.
    expect(body).not.toMatch(
      /if \(underlying === selectedUnderlying && _mergedEv != null\) return _mergedEv;/,
    );
    expect(body).toMatch(
      /if \(underlying === selectedUnderlying && !_strategyStale && _mergedEv != null\) return _mergedEv;/,
    );
  });

  test('P&L TOTAL (Legs card) reads the search-unfiltered _legsTotalsBase, not displayedCandidates (item-7 fix)', () => {
    const content = readFileSync(DERIV_FILE, 'utf8');
    // Old (buggy) pattern: _totalPnl summed the search-filtered
    // displayedCandidates — typing in the search box moved P&L TOTAL
    // while Day/Exp TOTAL stayed fixed (already on _legsTotalsBase).
    expect(content).not.toMatch(
      /\{@const _totalPnl = _selectedCands\.reduce\(\(s, c\) => s \+ Number\(c\.pnl \?\? 0\), 0\)\}/,
    );
    expect(content).toMatch(
      /\{@const _totalPnl = _legsTotalsBase\.filter\(c => _isLegEnabled\(c\)\)\.reduce\(\(s, c\) => s \+ Number\(c\.pnl \?\? 0\), 0\)\}/,
    );
  });

  test('Extrinsic Snapshot TOTAL/row use a filtered per-root reduction, not the unfiltered whole-book store (item-4 fix)', () => {
    const content = readFileSync(DERIV_FILE, 'utf8');
    expect(content).toMatch(/const _filteredExtrinsicByRoot = \$derived\.by/);
    expect(content).toMatch(/const _snapshotTotalExtrinsic = \$derived\.by/);
    // The Snapshot section's TOTAL row must NOT read the unfiltered
    // positionsDerivedStore.total.extrinsic anymore.
    const byundTotalIdx = content.indexOf("byund-row byund-row-total");
    expect(byundTotalIdx, 'Snapshot TOTAL row must exist').toBeGreaterThan(-1);
    const totalRowBody = content.slice(byundTotalIdx, byundTotalIdx + 1200);
    expect(totalRowBody).not.toMatch(/positionsDerivedStore\.total\.extrinsic/);
    expect(totalRowBody).toMatch(/_snapshotTotalExtrinsic/);
  });

  test('_filteredExtrinsicByRoot computes extrinsic PER ROW via legExtrinsicDisplay, not the cross-account-summed positionsDerivedStore (item-2 fix, round 4)', () => {
    const content = readFileSync(DERIV_FILE, 'utf8');
    // Old (buggy, round-3) pattern: perRootReduce walks PER-ACCOUNT rows but
    // the accessor indexed positionsDerivedStore.get(c.symbol)?.extrinsic —
    // a value ALREADY summed across every account for that symbol — so a
    // symbol held in 2 accounts contributed its full cross-account total
    // TWICE (once per account row), and filtering to one account still
    // showed the whole cross-account sum instead of that account's share.
    const idx = content.indexOf('const _filteredExtrinsicByRoot = $derived.by');
    expect(idx, '_filteredExtrinsicByRoot must exist').toBeGreaterThan(-1);
    const body = content.slice(idx, idx + 400);
    expect(body).not.toMatch(/positionsDerivedStore/);
    // New pattern: legExtrinsicDisplay computes the value from THIS row's
    // own qty/avg_cost/ltp + its own poll-time underlying_ltp, so
    // perRootReduce's normal per-row accumulation sums real per-account
    // contributions instead of re-adding the same pre-summed total.
    expect(body).toMatch(/legExtrinsicDisplay\(c, Number\(c\?\.underlying_ltp\) \|\| 0\)/);
  });

  test('_rowExpPnlFor is uniform for every root — no selectedUnderlying special-case reading a differently-filtered source (item-3 fix, round 4)', () => {
    const content = readFileSync(DERIV_FILE, 'utf8');
    // Old (buggy, round-3/round-4-reintroduced) pattern: the selected
    // underlying's row read _legsExpPnlTotal (expiry-picker filtered, NOT
    // strategy-filtered) while every other root read _filteredExpPnlByRoot
    // (strategy-filtered, NOT expiry-picker filtered) — two different
    // filter bases on the SAME column, so the Snapshot row for a given
    // underlying changed value purely from (de)selecting that root, with
    // no change to the underlying book.
    const idx = content.indexOf('function _rowExpPnlFor(');
    expect(idx, '_rowExpPnlFor must exist').toBeGreaterThan(-1);
    const body = content.slice(idx, idx + 300);
    expect(body).not.toMatch(/selectedUnderlying/);
    expect(body).not.toMatch(/_legsExpPnlTotal/);
    // New pattern: every root, selected or not, reads the SAME
    // account/strategy-filtered reduction Day P&L/P&L already use.
    expect(body).toMatch(/return _filteredExpPnlByRoot\[underlying\] \?\? 0;/);
  });
});

// ── Browser tests ───────────────────────────────────────────────────────────

/** @type {string | null} */
let _sharedJwt = null;

test.describe('Zero-flash / account-filter consistency / legs-search decoupling (§2, item-4, item-7)', () => {
  test.use({ viewport: { width: 1400, height: 900 } });

  test.beforeAll(async ({ browser }) => {
    const page = await browser.newPage();
    try {
      const result = await loginAsAdmin(page);
      _sharedJwt = result.token;
    } catch (e) {
      _sharedJwt = null;
    } finally {
      await page.close();
    }
  });

  async function seedToken(page) {
    if (!_sharedJwt) return;
    await page.context().addInitScript((t) => {
      sessionStorage.setItem('ramboq_token', t);
    }, _sharedJwt);
  }

  function getPSlot1(page) {
    return page
      .locator('.ps-agg')
      // .ps-agg-k's textContent includes a trailing whitespace text node
      // before an HTML comment placeholder (InfoHint's popup slot) — match
      // with surrounding \s* rather than an exact /^P$/.
      .filter({ has: page.locator('.ps-agg-k', { hasText: /^\s*P\s*$/ }) })
      .locator('.ps-agg-v')
      .first();
  }

  test('1. Zero-flash: remounting PositionStrip (route leaving/re-entering the (algo) layout) does not flash P∆ to 0 when a prior value exists', async ({ page }) => {
    test.skip(!_sharedJwt, 'Server unreachable — skipping browser test');
    await seedToken(page);

    // 'load' rather than 'networkidle' — /pulse holds an open SSE
    // connection that keeps the network non-idle indefinitely (confirmed
    // via the pre-existing navstrip_p_slot_derivatives.spec.js also
    // timing out at 90s on networkidle for this same route).
    await page.goto(`${BASE}/pulse`, { waitUntil: 'load' });
    // PositionStrip mounts after hydration completes — wait for the
    // generic pill container first (cheap, always present once mounted)
    // before locating the specific P value span.
    await expect(page.locator('.ps-strip')).toBeVisible({ timeout: 20_000 });
    const slot1 = getPSlot1(page);
    await expect(slot1).toBeVisible({ timeout: 20_000 });

    // aggCompact(0) renders "0.00", not "₹0" — poll (rather than a single
    // fixed wait) for up to 15s for a genuinely non-zero baseline; skip if
    // this account never has a non-zero P∆ (nothing to flash away from).
    let before = 0;
    const baselineDeadline = Date.now() + 15_000;
    while (Date.now() < baselineDeadline) {
      const txt = (await slot1.textContent().catch(() => null))?.trim();
      before = parseAggMoney(txt);
      if (before !== 0) break;
      await page.waitForTimeout(200);
    }
    test.skip(before === 0, 'P∆ baseline is 0 for this account — no prior value to flash away from');

    // Force a full navigation (not client-side routing) so PositionStrip
    // actually unmounts + remounts — this is the exact scenario the
    // _prevExecMode fix targets (a hardcoded 'idle' init misread a fresh
    // mount as a market-open transition and force-reset the P∆ display).
    // NOTE: the misread only fires when the live executionMode DIFFERS
    // from the hardcoded 'idle' default — an account observed sitting in
    // IDLE mode can't exercise this path at all (idle === idle already),
    // so this browser test is a general zero-flash smoke check; the
    // "§2/§5 static regression guards" test above (asserting
    // _prevExecMode reads get(executionMode) instead of a literal 'idle')
    // is the guard that actually verifies this specific fix landed.
    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'commit' });

    // Poll every 50ms for up to 2s immediately after navigation — if a
    // zero-flash regression exists, we'd observe a parsed 0 in this
    // window even though the strip settles to the correct value shortly after.
    let sawZeroFlash = false;
    const deadline = Date.now() + 2000;
    while (Date.now() < deadline) {
      const txt = (await slot1.textContent().catch(() => null))?.trim();
      if (txt != null && parseAggMoney(txt) === 0) { sawZeroFlash = true; break; }
      await page.waitForTimeout(50);
    }
    expect(sawZeroFlash).toBe(false);

    await page.waitForTimeout(1500);
    const afterTxt = (await slot1.textContent())?.trim() ?? '';
    expect(parseAggMoney(afterTxt)).not.toBe(0);
  });

  test('2. Snapshot filter consistency: TOTAL row equals the sum of VISIBLE (account-filtered) rows', async ({ page }) => {
    test.skip(!_sharedJwt, 'Server unreachable — skipping browser test');
    await seedToken(page);

    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'networkidle' });
    await page.waitForLoadState('networkidle', { timeout: 15_000 }).catch(() => {});
    await page.waitForTimeout(500);

    // Reading TOTAL == sum(rows) with NO filter applied doesn't discriminate
    // old vs new behavior — before the fix, every value (rows AND TOTAL)
    // was still computed from the SAME unfiltered whole-book source, so
    // they trivially agreed. Apply a REAL account filter (narrowing to one
    // specific account) so the filtered row values and the filtered TOTAL
    // must independently agree on a SUBSET of the book — this is exactly
    // what the old code got wrong (rows/TOTAL stayed unfiltered regardless
    // of the picker).
    const acctTrigger = page.locator('#opt-acct');
    await expect(acctTrigger).toBeVisible({ timeout: 10_000 });
    await acctTrigger.click();
    const acctOptions = page.locator('.rbq-multi-panel .rbq-multi-option');
    const acctCount = await acctOptions.count();
    if (acctCount < 2) {
      // Can't meaningfully narrow with 0 or 1 account — close the panel
      // and skip; the static guard test above still covers the source-level
      // fix in this case.
      await page.keyboard.press('Escape');
      test.skip(true, `Only ${acctCount} account(s) available — cannot exercise a narrowing filter`);
      return;
    }
    await acctOptions.first().click();
    await page.keyboard.press('Escape'); // close the dropdown panel
    await page.waitForTimeout(800); // let the filtered $derived chain settle

    const rows = page.locator('.byund-row:not(.byund-row-total)');
    const rowCount = await rows.count();
    test.skip(rowCount === 0, 'No Snapshot rows for the filtered account — no open F&O positions to verify against');

    // .num span order: LTP(0), Chg%(1), P.Close(2), Day P&L(3), P&L(4),
    // Exp P&L(5), Extrinsic(6), Legs(7), F&O qty(8), EV(9).
    let sumDay = 0, sumExt = 0;
    for (let i = 0; i < rowCount; i++) {
      const cells = rows.nth(i).locator('.num');
      sumDay += parseAggMoney(await cells.nth(3).textContent());
      sumExt += parseAggMoney(await cells.nth(6).textContent());
    }

    const totalDayText = await page.locator('.byund-row-total .num').nth(3).textContent();
    const totalExtText = await page.locator('.byund-row-total .num').nth(6).textContent();
    const totalDay = parseAggMoney(totalDayText);
    const totalExt = parseAggMoney(totalExtText);

    // Allow small rounding tolerance from aggCompact's K/L/Cr formatting.
    expect(Math.abs(totalDay - sumDay)).toBeLessThanOrEqual(Math.max(1, Math.abs(totalDay) * 0.01));
    expect(Math.abs(totalExt - sumExt)).toBeLessThanOrEqual(Math.max(1, Math.abs(totalExt) * 0.01));
  });

  test('3. Legs search-box decoupling: typing in the Legs search box does NOT move Day/P&L/Exp TOTAL', async ({ page }) => {
    test.skip(!_sharedJwt, 'Server unreachable — skipping browser test');
    await seedToken(page);

    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'networkidle' });
    await page.waitForLoadState('networkidle', { timeout: 15_000 }).catch(() => {});
    await page.waitForTimeout(800);

    // Reading the TOTAL row with NO search term typed doesn't discriminate
    // old vs new behavior — with the search box empty, displayedCandidates
    // === the unfiltered leg set either way, so the old (buggy)
    // displayedCandidates-based P&L TOTAL and the new _legsTotalsBase-based
    // one compute the same number. Actually TYPING into the search box is
    // what exercises the fix (item-7): _totalPnl now reads _legsTotalsBase
    // (unaffected by the search box), not the search-filtered
    // displayedCandidates — so narrowing the visible grid must NOT move
    // Day P&L / P&L / Exp P&L TOTAL.
    const legRows = page.locator('.cand-row:not(.cand-row-total)');

    // The default-selected underlying may have zero open legs (a popular
    // placeholder root, not necessarily one the account holds). Actively
    // search the #opt-und picker for a root that DOES have >= 2 legs,
    // rather than skip outright — the account is known to hold F&O
    // positions somewhere, this just finds where.
    if ((await legRows.count()) < 2) {
      const undTrigger = page.locator('#opt-und');
      await expect(undTrigger).toBeVisible({ timeout: 10_000 });
      await undTrigger.click();
      const undOptions = page.locator('.rbq-select-option-label');
      const undOptCount = await undOptions.count();
      const maxProbe = Math.min(undOptCount, 25); // bounded — avoid an unbounded scan on a huge popular-roots list
      for (let i = 0; i < maxProbe; i++) {
        // Re-open the picker each iteration (picking an option closes it).
        if (!(await page.locator('.rbq-select-panel').count())) {
          await undTrigger.click();
        }
        const opt = page.locator('.rbq-select-option-label').nth(i);
        await opt.click();
        await page.waitForTimeout(400);
        if ((await legRows.count()) >= 2) break;
      }
    }
    const legCount = await legRows.count();
    test.skip(legCount < 2, 'No underlying in this account has >= 2 open F&O legs — search can\'t meaningfully narrow the grid');

    // Baseline TOTAL values (title-anchored so column-order changes don't
    // silently break the locator).
    const dayTotalCell = page.locator('.cand-row-total .cand-pnl[title*="Day P&L across enabled F&O legs"]');
    const pnlTotalCell = page.locator('.cand-row-total .cand-pnl[title*="P&L across every enabled leg"]');
    const expTotalCell = page.locator('.cand-row-total .cand-pnl[title*="Exp P&L across every selected leg"]');
    for (const cell of [dayTotalCell, pnlTotalCell, expTotalCell]) {
      test.skip(!(await cell.count()), 'A TOTAL cell is not present — likely no open F&O legs for the selected underlying');
    }
    const before = {
      day: parseAggMoney(await dayTotalCell.textContent()),
      pnl: parseAggMoney(await pnlTotalCell.textContent()),
      exp: parseAggMoney(await expTotalCell.textContent()),
    };

    // Get the FIRST leg's exact tradingsymbol from its title attribute
    // (the visible label may be a shortened root, e.g. "NIFTY 24000 CE" —
    // the title carries the full raw symbol the _filterLegs substring
    // match actually runs against).
    const firstSymTitle = await legRows.first().locator('.cand-sym').first().getAttribute('title');
    test.skip(!firstSymTitle, 'Could not read the first leg\'s symbol title attribute');

    // Open the Legs search box and type the full first-leg symbol — a
    // substring match this specific, on a book with >= 2 distinct legs,
    // should exclude at least the OTHER legs from the visible grid.
    const searchBtn = page.locator('button[aria-label="Toggle Legs symbol filter"]');
    await expect(searchBtn).toBeVisible({ timeout: 10_000 });
    await searchBtn.click();
    const searchInput = page.locator('input[aria-label="Filter Legs by symbol"]');
    await expect(searchInput).toBeVisible({ timeout: 5_000 });
    await searchInput.fill(/** @type {string} */ (firstSymTitle));
    await page.waitForTimeout(500); // let the filtered $derived (displayedCandidates) settle

    const filteredCount = await legRows.count();
    expect(filteredCount).toBeLessThan(legCount); // the search actually narrowed the grid

    const after = {
      day: parseAggMoney(await dayTotalCell.textContent()),
      pnl: parseAggMoney(await pnlTotalCell.textContent()),
      exp: parseAggMoney(await expTotalCell.textContent()),
    };

    const tol = (v) => Math.max(1, Math.abs(v) * 0.01);
    expect(Math.abs(after.day - before.day)).toBeLessThanOrEqual(tol(before.day));
    expect(Math.abs(after.pnl - before.pnl)).toBeLessThanOrEqual(tol(before.pnl));
    expect(Math.abs(after.exp - before.exp)).toBeLessThanOrEqual(tol(before.exp));

    // Clean up — clear the filter so it doesn't leak into other tests
    // sharing the same page/session state.
    await searchInput.fill('');
    await page.keyboard.press('Escape');
  });
});
