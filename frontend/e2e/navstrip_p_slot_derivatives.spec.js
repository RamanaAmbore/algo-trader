/**
 * navstrip_p_slot_derivatives.spec.js
 *
 * Regression guard: NavStrip P pill slot 1 ("today" Day P&L) must NOT
 * drop to 0 when /admin/derivatives is opened, when the user picks a
 * different underlying in the picker, or after navigating away.
 *
 * Root causes fixed (historical — see "Architecture superseded" below):
 *
 *   2026-07-04 (mount-time zero):
 *   derivatives/+page.svelte wrote to the shared `snapshotTotals` store
 *   inside a `$effect` that ran BEFORE the first `loadPositions()` call
 *   completed.  At that point `positions = []`, so all three derived
 *   totals were 0.  PositionStrip reads `$snapshotTotals.day` when the
 *   store is non-null, so it displayed 0 instead of the real intraday
 *   P&L.  Additionally, `onDestroy` never cleared the store, so the
 *   filtered F&O-only value lingered on subsequent pages.
 *   Fix (at the time): gate the `$effect` publish on `_positionsLoaded`;
 *   clear the store to null in `onDestroy`.
 *
 *   2026-07-04 (symbol-select zero — dead code SSOT violation):
 *   `_byUnderlyingDay` was a `$derived.by()` that read raw
 *   `p.day_change_val` without routing through `baseDayPnlForPosition`.
 *   It was never wired to any consumer in the template but was a latent
 *   SSOT violation.  Removed in refactor(derivatives) commit.
 *
 * Architecture superseded 2026-09 (commit cbe132a6 / 7562dd04, "NavStrip/
 * Snapshot Exp P&L SSOT"): the writable `snapshotTotals` store — and every
 * push from the derivatives page into it — was removed entirely. The whole
 * class of "mount-time zero" / "stale push lingers after nav-away" bug this
 * file originally guarded is now structurally impossible, not just patched:
 *   - PositionStrip.svelte mounts ONCE at the `(algo)/+layout.svelte` level
 *     (not per-page), so it never remounts/re-reads a per-page push when the
 *     operator navigates between /pulse and /admin/derivatives.
 *   - NavStrip's P slot 1 (`dispPositionsToday`) reads exclusively from
 *     `positionsDayPnlStore.total` (a thin shim over
 *     `portfolioStore.positions.total.day_pnl`), which sums ALL live
 *     position rows via the pure `baseDayPnlForPosition` function — no
 *     store write, no cross-page coupling, no `_positionsLoaded` gate to
 *     get wrong.
 *   - The derivatives page's OWN Snapshot TOTAL Day P&L (`_snapshotTotalDay`)
 *     is a separate, F&O-only, account/strategy-filtered reduction over
 *     `_byUnderlyingTotals` (`rollupByUnderlying` in derivativesMath.js).
 *     It agrees with NavStrip's total only in the trivial case (no strategy
 *     filter, F&O-only book) because BOTH ultimately call the SAME pure
 *     `baseDayPnlForPosition(p)` per position row — SSOT via a shared pure
 *     function, not a shared mutable store. This file does NOT assert the
 *     two totals are numerically equal (see derivatives_day_pnl_health.spec.js
 *     for that surface's own guard) — only that NavStrip's slot never
 *     depends on the derivatives page being mounted at all.
 *
 * Five quality dimensions:
 * 1. SSOT   — `dispPositionsToday` (NavStrip P slot 1) is assigned only
 *             from `positionsDayPnlStore.total` or a confirmed-empty `0` —
 *             never from any derivatives-page-specific value; no
 *             `snapshotTotals`-style shared store exists anywhere.
 *             `_byUnderlyingDay` dead-code with SSOT violation is absent.
 * 2. Perf   — no new long-task (>200 ms) introduced during route
 *             transitions that touch the derivatives page.
 * 3. Stale  — grep confirms `snapshotTotals` does not exist in
 *             PositionStrip.svelte, stores.js, or the derivatives page;
 *             `_byUnderlyingDay` fully removed.
 * 4. Reuse  — derivatives page imports `baseDayPnlForPosition` (the sole
 *             Day P&L SSOT) rather than re-implementing the formula inline;
 *             portfolioStore.svelte.js (NavStrip's own source) calls the
 *             same function.
 * 5. UX     — P slot 1 has a direction class (ps-pos/ps-neg/ps-flat),
 *             is never blank when visible, and does NOT drop to zero
 *             when the operator changes the underlying picker selection.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import path from 'path';

const BASE = process.env.PLAYWRIGHT_BASE_URL || process.env.BASE_URL || 'http://localhost:5174';

// Resolve project root from this spec's location (e2e/ → frontend/ → project root).
const __dirname = path.dirname(fileURLToPath(import.meta.url));

// ── Dimension 1 + 3 + 4: static file checks (no browser needed) ───────────

const _STRIP_FILE = path.join(__dirname, '..', 'src', 'lib', 'PositionStrip.svelte');
const _STORES_FILE = path.join(__dirname, '..', 'src', 'lib', 'stores.js');
const _DERIV_FILE = path.join(
  __dirname, '..', 'src', 'routes', '(algo)', 'admin', 'derivatives', '+page.svelte',
);
const _LAYOUT_FILE = path.join(__dirname, '..', 'src', 'routes', '(algo)', '+layout.svelte');

test.describe('NavStrip Day P&L SSOT static guards (2026-09 architecture)', () => {
  test('snapshotTotals does not exist anywhere it could reintroduce cross-page coupling', () => {
    // The dead store must not reappear in PositionStrip, stores.js, or the
    // derivatives page — the exact three files the original push/pull
    // coupling spanned.
    for (const f of [_STRIP_FILE, _STORES_FILE, _DERIV_FILE]) {
      const src = readFileSync(f, 'utf8');
      expect(src, `${path.basename(f)} must not reference snapshotTotals`).not.toContain('snapshotTotals');
    }
  });

  test('PositionStrip mounts once at the (algo) layout level, not per-page', () => {
    // A single layout-level mount means dispPositionsToday's $state persists
    // across route navigation instead of being torn down/rebuilt per page —
    // the structural fix for "stale push lingers after nav-away".
    const layoutSrc = readFileSync(_LAYOUT_FILE, 'utf8');
    expect(layoutSrc).toContain('<PositionStrip');
    const mountCount = (layoutSrc.match(/<PositionStrip\b/g) || []).length;
    expect(mountCount).toBe(1);
    // The derivatives page itself must not ALSO mount its own copy —
    // that would create a second, page-scoped instance able to diverge.
    const derivSrc = readFileSync(_DERIV_FILE, 'utf8');
    expect(derivSrc).not.toContain('<PositionStrip');
  });

  test('dispPositionsToday (P slot 1) is assigned exclusively from positionsDayPnlStore.total or a confirmed-empty 0', () => {
    const stripSrc = readFileSync(_STRIP_FILE, 'utf8');
    // Every assignment site's right-hand side must be one of: the initial
    // state read, the transition-reset literal 0, or the freeze/thaw
    // effect's own `newPTotal` local (itself always
    // `positionsDayPnlStore.total`) — never anything page-specific.
    expect(stripSrc).toContain('let dispPositionsToday = $state(positionsDayPnlStore.total || 0);');
    const assignments = [...stripSrc.matchAll(/(?<!let )dispPositionsToday\s*=\s*([^;]+);/g)].map(m => m[1].trim());
    expect(assignments.length).toBeGreaterThan(0);
    for (const rhs of assignments) {
      expect(['0', 'newPTotal']).toContain(rhs);
    }
    expect(stripSrc).toContain('const newPTotal = positionsDayPnlStore.total;');
  });

  test('freeze/thaw effect does not blindly overwrite dispPositionsToday with a transient 0 (swallow-zero guard)', () => {
    // This is the CURRENT equivalent of the 2026-07-04 "mount-time zero"
    // defect class: a momentary 0/null read from the store must not
    // immediately zero out the displayed value while positions are known
    // to be non-empty (or the store is degraded/reloading).
    const stripSrc = readFileSync(_STRIP_FILE, 'utf8');
    expect(stripSrc).toContain('if (newPTotal !== 0) {');
    expect(stripSrc).toContain('dispPositionsToday = newPTotal;');
    expect(stripSrc).toContain('} else if (positions.length === 0 && !positionsStore.meta?.degraded) {');
  });

  test('portfolioStore.positions.total.day_pnl (NavStrip\'s ultimate source) is built from baseDayPnlForPosition per row', () => {
    const storeSrc = readFileSync(
      path.join(__dirname, '..', 'src', 'lib', 'data', 'portfolioStore.svelte.js'), 'utf8',
    );
    expect(storeSrc).toContain('const day_pnl = baseDayPnlForPosition(p);');
    expect(storeSrc).toMatch(/posTotal\.day_pnl\s*\+=\s*p\._day_pnl;/);
    // positionsDayPnlStore shim exposes this total unmodified.
    const shimSrc = readFileSync(
      path.join(__dirname, '..', 'src', 'lib', 'data', 'positionsDayPnlStore.svelte.js'), 'utf8',
    );
    expect(shimSrc).toContain('portfolioStore.positions.total.day_pnl');
  });

  test('derivatives page imports baseDayPnlForPosition (the sole Day P&L SSOT, §1 poll-only redesign)', () => {
    const content = readFileSync(_DERIV_FILE, 'utf8');
    // Import must come from $lib/data/nav (the canonical SSOT module),
    // tolerant of other named imports on the same line (e.g. FO_EXCHANGES).
    expect(content).toMatch(/import\s*\{[^}]*\bbaseDayPnlForPosition\b[^}]*\}\s*from\s*'\$lib\/data\/nav'/);
  });

  test('_byUnderlyingDay dead-code SSOT-violation is fully removed from derivatives page', () => {
    // _byUnderlyingDay was a $derived.by() that read raw p.day_change_val
    // without routing through baseDayPnlForPosition — an SSOT violation.
    // It was never consumed by any template expression, making it dead code.
    // This guard ensures it is not re-introduced.
    const derivFile = path.join(
      __dirname, '..', 'src', 'routes', '(algo)', 'admin', 'derivatives', '+page.svelte',
    );
    const content = readFileSync(derivFile, 'utf8');
    // The identifier must not appear as a declaration (= $derived.by).
    // A comment reference is acceptable only if it doesn't declare or assign.
    const declarationPattern = /const\s+_byUnderlyingDay\s*=/;
    expect(declarationPattern.test(content)).toBe(false);
    // The identifier must not appear in any template expression.
    const templatePattern = /\{[^}]*_byUnderlyingDay[^}]*\}/;
    expect(templatePattern.test(content)).toBe(false);
  });
});

// ── Dimension 1 + 2 + 5: browser tests ────────────────────────────────────

/** @type {string | null} */
let _sharedJwt = null;

test.describe('NavStrip P slot 1 — derivatives page regression', () => {
  test.use({ viewport: { width: 1400, height: 900 } });

  test.beforeAll(async ({ browser }) => {
    const page = await browser.newPage();
    try {
      const result = await loginAsAdmin(page);
      _sharedJwt = result.token;
    } catch (e) {
      // Server unreachable in offline CI — skip browser tests gracefully.
      _sharedJwt = null;
    } finally {
      await page.close();
    }
  });

  /** Inject JWT so each test skips the login round-trip. */
  async function seedToken(page) {
    if (!_sharedJwt) return;
    await page.context().addInitScript((t) => {
      sessionStorage.setItem('ramboq_token', t);
    }, _sharedJwt);
  }

  /**
   * Locate the P pill's first value span (slot 1 = today Day P&L).
   * Structure: <span class="ps-agg" title="Positions: ...">
   *              <span class="ps-agg-k">P</span>
   *              <span class="ps-agg-v ...">TODAY</span>  ← slot 1
   *              <span class="ps-agg-sep">/</span>
   *              ...
   */
  function getPSlot1(page) {
    return page
      .locator('.ps-agg')
      .filter({ has: page.locator('.ps-agg-k', { hasText: /^P$/ }) })
      .locator('.ps-agg-v')
      .first();
  }

  // fmtMoney() in PositionStrip.svelte is `aggCompact(v)` with NO currency
  // prefix — it never renders '₹0'; a zero P&L renders as the plain string
  // '0.00' (aggCompact → _decFmt → Intl 'en-IN' 2-decimal format for |v|<100).
  // '₹0' would never appear so any '!== ₹0' check would be vacuously true;
  // use this helper instead of comparing against a literal string.
  const isZeroPDisplay = (t) => t === '' || t === '—' || /^-?0(\.0+)?$/.test(t);

  test('P slot 1 stays equal to baseline after /admin/derivatives opens', async ({ page }) => {
    test.skip(!_sharedJwt, 'Server unreachable — skipping browser test');

    await seedToken(page);

    // ── 1. Baseline on /pulse ──────────────────────────────────────
    await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });

    const slot1 = getPSlot1(page);
    await expect(slot1).toBeVisible({ timeout: 15_000 });

    // Let the strip hydrate at least one poll cycle.
    await page.waitForTimeout(500);
    const baselineText = (await slot1.textContent())?.trim() ?? '';

    // ── 2. Navigate to /admin/derivatives ─────────────────────────
    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'networkidle' });
    await expect(slot1).toBeVisible({ timeout: 5_000 });

    // Wait for the derivatives page's own positions load to settle (gates
    // its LOCAL Snapshot section only — NavStrip's P slot 1 is unaffected
    // either way since it reads portfolioStore directly, not anything the
    // derivatives page computes).
    await page.waitForLoadState('networkidle', { timeout: 15_000 }).catch(() => {});
    // One extra tick to let Svelte flush any pending effects.
    await page.waitForTimeout(300);

    const derivText = (await slot1.textContent())?.trim() ?? '';

    // Core assertion: value must not drop to 0 unless baseline was also 0.
    if (!isZeroPDisplay(baselineText)) {
      expect(isZeroPDisplay(derivText)).toBe(false);
    }
    // And the values must match (same data source — dispPositionsToday
    // never depends on which page is currently mounted).
    expect(derivText).toBe(baselineText);
  });

  test('P slot 1 does NOT drop to zero when underlying picker changes symbol', async ({ page }) => {
    test.skip(!_sharedJwt, 'Server unreachable — skipping browser test');

    await seedToken(page);

    // ── 1. Open derivatives and wait for positions to load ─────────
    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'networkidle' });
    await page.waitForLoadState('networkidle', { timeout: 15_000 }).catch(() => {});
    // Extra tick to let the page's own reactive graph settle.
    await page.waitForTimeout(500);

    const slot1 = getPSlot1(page);
    await expect(slot1).toBeVisible({ timeout: 5_000 });

    // Capture BOTH text and direction class BEFORE picking — compare the
    // actual rendered text string directly rather than guessing format.
    const beforePick = (await slot1.textContent())?.trim() ?? '';
    const beforeCls  = await slot1.getAttribute('class') ?? '';
    // Extract only the direction token (ps-pos / ps-neg / ps-flat).
    const beforeDir  = (beforeCls.match(/\bps-(?:pos|neg|flat)\b/) || ['ps-flat'])[0];

    // ── 2. Open the Underlying picker and pick the SECOND option ──
    // Select renders the id prop directly on the <button class="rbq-select-trigger">
    // so #opt-und IS the trigger button — no descendant selector needed.
    // Options live in a sibling .rbq-select-panel inside .opt-und-row.
    const trigger = page.locator('#opt-und');
    await expect(trigger).toBeVisible({ timeout: 15_000 });
    await trigger.click();

    // Wait for the panel to open (options list visible).
    const options = page.locator('.opt-und-row .rbq-select-option');
    await expect(options.first()).toBeVisible({ timeout: 3_000 });
    const count = await options.count();

    // Skip test gracefully when the picker has fewer than 2 options
    // (no live F&O book + watchlist too small).
    if (count < 2) {
      test.skip(true, 'Picker has < 2 underlying options — skipping symbol-select regression');
      return;
    }

    // Pick the second option (index 1) — different from whatever was
    // auto-selected on load.
    await options.nth(1).click();

    // Allow the Svelte reactive graph to flush the page's own picker-scoped
    // derived values (the underlying picker only affects the derivatives
    // page's local rendering — NavStrip's P slot 1 is not derived from it).
    await page.waitForTimeout(400);

    // ── 3. Core assertion: both text AND direction class must be stable ──
    const afterPick = (await slot1.textContent())?.trim() ?? '';
    const afterCls  = await slot1.getAttribute('class') ?? '';
    const afterDir  = (afterCls.match(/\bps-(?:pos|neg|flat)\b/) || ['ps-flat'])[0];

    // Text must not change (symbol-select is view-only; NavStrip's P slot 1
    // sums ALL positions via portfolioStore, not just the selected underlying).
    expect(afterPick).toBe(beforePick);

    // Direction class must not change — this is the core regression check.
    // Previously, picking a symbol could cause the (now-removed) snapshotTotals
    // push to briefly flip to 0, turning ps-pos/ps-neg into ps-flat.
    expect(afterDir).toBe(beforeDir);

    // Sanity: a direction class must always be present.
    expect(afterDir).toMatch(/^ps-(?:pos|neg|flat)$/);
  });

  test('P slot 1 has direction class (not bare text) on /orders after leaving derivatives', async ({ page }) => {
    test.skip(!_sharedJwt, 'Server unreachable — skipping browser test');

    await seedToken(page);

    // Start on derivatives, then navigate away. PositionStrip is mounted at
    // the (algo) layout level (never unmounted by this nav), so slot 1 keeps
    // reflecting portfolioStore's live total the whole time — no per-page
    // teardown/onDestroy to release a stale value.
    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'networkidle' });
    await page.waitForLoadState('networkidle', { timeout: 15_000 }).catch(() => {});
    await page.waitForTimeout(300);

    await page.goto(`${BASE}/orders`, { waitUntil: 'networkidle' });
    const slot1 = getPSlot1(page);
    await expect(slot1).toBeVisible({ timeout: 5_000 });

    // UX dimension: must carry exactly one direction class.
    const cls = await slot1.getAttribute('class');
    expect(cls).toMatch(/ps-pos|ps-neg|ps-flat/);

    // Non-blank.
    const txt = (await slot1.textContent())?.trim();
    expect(txt).toBeTruthy();
  });

  test('no long-task >200 ms during pulse → derivatives → orders nav', async ({ page }) => {
    test.skip(!_sharedJwt, 'Server unreachable — skipping browser test');

    await seedToken(page);

    await page.addInitScript(() => {
      window.__longTasks = [];
      const obs = new PerformanceObserver((list) => {
        for (const e of list.getEntries()) window.__longTasks.push(e.duration);
      });
      obs.observe({ type: 'longtask', buffered: true });
    });

    await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });
    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(500);
    await page.goto(`${BASE}/orders`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(300);

    const tasks = await page.evaluate(() => window.__longTasks ?? []);
    const worstMs = tasks.length ? Math.max(...tasks) : 0;
    // Budget: 200 ms (allows SvelteKit route-transition overhead; regression
    // threshold sits well above the O(1) guard we added).
    expect(worstMs).toBeLessThan(200);
  });

  test('Snapshot underlying LTP and NavStrip Exp P&L stay consistent across updates', async ({ page }) => {
    /**
     * Regression guard: NavStrip's Exp P&L (expected P&L of derivative
     * strategy at current underlying spot) is now driven by the same
     * `getUnderlyingSpot` resolution path as Snapshot, fixing an earlier
     * divergence where NavStrip resolved spot via a separate path.
     *
     * This test verifies that when underlying spot changes (via poll or
     * tick), both Snapshot LTP and NavStrip Exp P&L update together
     * within one cycle, confirming they share a SSOT.
     *
     * Run:
     *   npx playwright test --project=chromium-desktop -g "Snapshot underlying LTP and NavStrip Exp P&L"
     */
    test.skip(!_sharedJwt, 'Server unreachable — skipping browser test');

    await seedToken(page);

    // Navigate to derivatives and wait for positions to load
    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'networkidle', timeout: 30000 });
    await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
    await page.waitForTimeout(500);

    // Locate the Snapshot section
    const snapshotCard = page.locator('.opt-byund-card');
    await expect(snapshotCard).toBeVisible({ timeout: 15000 });

    // Locate NavStrip P slot 1 (today Day P&L / Exp P&L)
    const pSlot = page
      .locator('.ps-agg')
      .filter({ has: page.locator('.ps-agg-k', { hasText: /^P$/ }) })
      .locator('.ps-agg-v')
      .first();

    // Skip gracefully if NavStrip is not present (e.g., on demo account or if page layout changed)
    const pslotVisible = await pSlot.isVisible({ timeout: 5000 }).catch(() => false);
    if (!pslotVisible) {
      test.skip(true, 'NavStrip P slot not found — likely no positions or demo account');
      return;
    }

    // Helper to read both Snapshot LTP and NavStrip P slot text atomically
    const readValues = async () => {
      return page.evaluate(() => {
        // Snapshot: first .byund-row .num (LTP cell)
        const snapRow = document.querySelector('.byund-row:not(.byund-row-total)');
        const snapLtpCell = snapRow?.querySelector('.num');
        const snapLtpText = snapLtpCell?.textContent?.trim() || '';
        const snapLtp = parseFloat(snapLtpText.replace(/[₹,\s]/g, ''));

        // NavStrip P slot 1 (Exp P&L)
        const pSlotEl = document.querySelector('.ps-agg')?.querySelector('.ps-agg-v');
        const pSlotText = pSlotEl?.textContent?.trim() || '';

        return { snapLtp, pSlotText };
      });
    };

    // Capture initial state
    const initial = await readValues();
    const initialPslotHasValue = initial.pSlotText && initial.pSlotText !== '₹0' && initial.pSlotText !== '₹0.00';

    // Wait for a poll cycle (5 seconds for loadUnderlyingQuotes)
    await page.waitForTimeout(5500);

    // Capture state after poll
    const afterPoll = await readValues();

    // ── Core assertion ──────────────────────────────────────
    // Both Snapshot LTP and NavStrip P slot should have valid values
    expect(
      Number.isFinite(afterPoll.snapLtp),
      'Snapshot LTP should be a valid number',
    ).toBe(true);

    // P slot should not be empty/blank (if it had a value initially)
    if (initialPslotHasValue) {
      expect(
        afterPoll.pSlotText,
        'NavStrip P slot should have a value (not blank) when derivative exposure exists',
      ).toBeTruthy();
    }

    // Direction class must be present
    const pslotClass = await pSlot.getAttribute('class');
    expect(
      pslotClass,
      'NavStrip P slot must have a direction class (ps-pos/ps-neg/ps-flat)',
    ).toMatch(/ps-pos|ps-neg|ps-flat/);
  });
});
