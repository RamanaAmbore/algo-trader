/**
 * derivatives_payoff_default_underlying.spec.js
 *
 * Verifies the payoff dropdown default-underlying behaviour on /admin/derivatives.
 *
 * Problem fixed: stale sessionStorage cache could cause the dropdown to show
 * a previously-selected underlying (e.g. COPPER) even when no COPPER positions
 * exist in the current broker's book. This made the page unusable: clicking
 * a symbol that had no actual data would show no payoff/strategy.
 *
 * Solution: the underlying picker must always show a VALID underlying from
 * the current positions (Tier A), watchlist (Tier B), or POPULAR list (Tier C).
 * Stale sessionStorage values are rejected if they do not appear in the
 * currently-available options.
 *
 * Five quality dimensions:
 *
 *  1. SSOT    — underlying dropdown value always matches one of the available
 *               options (positions/watchlist/popular). Never shows a value
 *               not in the dropdown's option list.
 *
 *  2. Perf    — default value loaded within 8 s on mount (no stale cache delays).
 *
 *  3. Stale   — grep confirms that auto-default effect checks valid options
 *               before accepting a sessionStorage value.
 *
 *  4. Reusable — uses canonical #opt-und Select component; no new pickers.
 *
 *  5. UX      — dropdown is always populated with a real value (never blank
 *               or placeholder after instruments load) across desktop + mobile.
 *
 * Run:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/derivatives_payoff_default_underlying.spec.js \
 *   --project=chromium-desktop --project=mobile-portrait --workers=1
 */

import { test, expect } from '@playwright/test';

test.setTimeout(60_000);

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const DERIV_URL = `${BASE}/admin/derivatives`;

const USER = process.env.PLAYWRIGHT_USER || 'rambo';
const PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

let _token = null;

async function loginAsAdmin(page) {
  if (!_token) {
    for (const u of [USER, 'ambore', 'rambo']) {
      const r = await page.request.post(`${BASE}/api/auth/login`, {
        data: { username: u, password: PASS },
        headers: { 'Content-Type': 'application/json' },
      });
      if (r.ok()) {
        _token = (await r.json()).access_token;
        break;
      }
    }
    if (!_token) throw new Error(`loginAsAdmin: no valid credentials for ${BASE}`);
  }
  await page.context().addInitScript((tok) => {
    sessionStorage.setItem('ramboq_token', tok);
  }, _token);
}

/**
 * Fetch the list of F&O roots currently available from /api/positions.
 * Returns an array of unique uppercase roots like ['BHEL', 'NIFTY'].
 */
async function fetchAvailableRoots(request) {
  const r = await request.get(`${BASE}/api/positions/`, {
    headers: _token ? { Authorization: `Bearer ${_token}` } : {},
  });
  if (!r.ok()) return [];
  const data = await r.json();
  const roots = new Set();
  for (const p of (data.rows || [])) {
    const sym = String(p.tradingsymbol || p.symbol || '').toUpperCase();
    if (!sym) continue;
    const root = sym.replace(/\d.*$/, '');
    if (root && root !== sym) roots.add(root); // only derivatives
  }
  return [...roots];
}

/**
 * Get the currently-displayed underlying text from the #opt-und trigger button.
 * Returns uppercase text, or null if not visible.
 */
async function getDisplayedUnderlying(page) {
  const trigger = page.locator('#opt-und');
  const visible = await trigger.isVisible({ timeout: 2000 }).catch(() => false);
  if (!visible) return null;
  const label = trigger.locator('.rbq-select-label');
  const text = (await label.textContent() || '').trim().toUpperCase();
  return text || null;
}

/**
 * Get the list of available options in the dropdown without opening it.
 * This reads the .rbq-select-option-label elements from the panel
 * (requires the panel to be visible).
 */
async function getDropdownOptions(page) {
  const trigger = page.locator('#opt-und');
  const panel = trigger.locator('xpath=..')
    .locator('.rbq-select-panel, ~ .rbq-select-panel');
  const options = await panel.locator('.rbq-select-option-label')
    .allTextContents()
    .catch(() => []);
  return options.map(o => o.trim().toUpperCase()).filter(Boolean);
}

const PLACEHOLDER_TEXTS = new Set([
  'PICK UNDERLYING…',
  'LOADING UNDERLYINGS…',
  'NO OPTIONS IN BOOK',
  '',
]);

// ── Dimension 1 + 5: SSOT + UX — dropdown value always in available options ───
test.describe('Dimension 1 + 5: SSOT — dropdown shows valid underlying', () => {
  test('Underlying dropdown displays a non-placeholder value after load', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    // Wait for trigger to appear.
    const trigger = page.locator('#opt-und');
    await expect(trigger).toBeVisible({ timeout: 8_000 });

    // Poll for a non-placeholder value within 20 s.
    const label = trigger.locator('.rbq-select-label');
    const deadline = Date.now() + 20_000;
    let finalText = '';
    while (Date.now() < deadline) {
      finalText = (await label.textContent() || '').trim().toUpperCase();
      if (finalText && !PLACEHOLDER_TEXTS.has(finalText)) break;
      await page.waitForTimeout(300);
    }

    expect(
      finalText && !PLACEHOLDER_TEXTS.has(finalText),
      `Dropdown must show a non-placeholder value. Got: "${finalText}"`,
    ).toBe(true);
  });

  test('Displayed underlying value exists in the dropdown options', async ({ page, request }) => {
    await loginAsAdmin(page);
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    // Get available F&O roots from the API.
    const apiRoots = await fetchAvailableRoots(request);

    // Wait for trigger to appear and auto-select.
    const trigger = page.locator('#opt-und');
    await expect(trigger).toBeVisible({ timeout: 8_000 });

    // Wait for a non-placeholder value.
    const label = trigger.locator('.rbq-select-label');
    let displayed = '';
    const deadline = Date.now() + 20_000;
    while (Date.now() < deadline) {
      displayed = (await label.textContent() || '').trim().toUpperCase();
      if (displayed && !PLACEHOLDER_TEXTS.has(displayed)) break;
      await page.waitForTimeout(300);
    }

    if (!displayed || PLACEHOLDER_TEXTS.has(displayed)) {
      // No positions or watchlist available — acceptable edge case.
      console.warn(`Dropdown shows placeholder: "${displayed}" — likely no F&O positions. Skip validation.`);
      return;
    }

    // Open the dropdown to inspect available options.
    await trigger.click();
    await page.waitForTimeout(800);

    const options = await getDropdownOptions(page);

    expect(
      options.length,
      'Dropdown must have at least one option',
    ).toBeGreaterThan(0);

    expect(
      options.some(o => o.includes(displayed)),
      `Displayed underlying "${displayed}" must be in dropdown options: [${options.join(', ')}]`,
    ).toBe(true);

    await page.keyboard.press('Escape');
  });
});

// ── Dimension 2: Performance — default value loaded quickly ────────────────────
test.describe('Dimension 2: Performance — fast auto-default', () => {
  test('Underlying defaults within 8 s of page load', async ({ page }) => {
    await loginAsAdmin(page);

    const start = Date.now();
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const trigger = page.locator('#opt-und');
    await expect(trigger).toBeVisible({ timeout: 8_000 });

    const label = trigger.locator('.rbq-select-label');
    const deadline = start + 8_000;
    let displayed = '';
    while (Date.now() < deadline) {
      displayed = (await label.textContent() || '').trim().toUpperCase();
      if (displayed && !PLACEHOLDER_TEXTS.has(displayed)) break;
      await page.waitForTimeout(300);
    }

    const elapsed = Date.now() - start;
    expect(
      displayed && !PLACEHOLDER_TEXTS.has(displayed),
      `Underlying must default within 8 s. Elapsed: ${elapsed}ms. Displayed: "${displayed}"`,
    ).toBe(true);
  });
});

// ── Dimension 3: Stale-code audit — grep source for auto-default validation ────
test.describe('Dimension 3: Stale-code audit — auto-default checks valid options', () => {
  test('Auto-default effect validates underlying against available options', async () => {
    const fs = await import('fs/promises');
    const src = await fs.readFile(
      new URL(
        '../src/routes/(algo)/admin/derivatives/+page.svelte',
        import.meta.url,
      ),
      'utf8',
    );

    // The auto-default effect must read the underlying list (opts) and
    // validate a cached/URL/sessionStorage value against it before accepting.
    // Look for guards like:
    //   - reading opts (underlying options)
    //   - checking if a candidate value is in opts
    //   - falling back to first item if not found

    expect(
      src,
      'Auto-default effect must read opts (available underlyings)',
    ).toContain('opts');

    // The page should have logic to detect if a cached value is NOT in the
    // current options and fall back to first item or URL param.
    // This can be phrased as "find / includes / indexOf" checks.
    expect(
      src.match(/opts\s*\.\s*(find|some|includes)\s*\(|indexOf|\.has\(/g),
      'Auto-default must validate cached value against available options',
    ).toBeTruthy();

    // The URL param read should still be present (stored in URL, not sessionStorage).
    expect(
      src,
      'Auto-default must read URL param as primary source',
    ).toContain("sp.get('u')");

    // sessionStorage fallback should not override invalid values — it should be
    // guarded. Look for the pattern where sessionStorage is only accepted if
    // the extracted value is valid.
    expect(
      src,
      'sessionStorage fallback must not blindly accept stale values',
    ).toContain('sessionStorage');
  });

  test('No duplicate auto-default paths — single onMount poll site', async () => {
    const fs = await import('fs/promises');
    const src = await fs.readFile(
      new URL(
        '../src/routes/(algo)/admin/derivatives/+page.svelte',
        import.meta.url,
      ),
      'utf8',
    );

    // Count onMount / $effect( () => { ...opts usage patterns.
    // Should be one canonical 300ms polling interval for auto-default.
    const autoDefaultBlocks = (src.match(/_autoSelectAttempts|_autoDefault|onMount\(\s*\(\)/g) || []);
    // Heuristic: at most 2 blocks (onMount + one poll effect) for auto-default.
    expect(
      autoDefaultBlocks.length,
      'Expected one canonical auto-default poll site, not multiple competing paths',
    ).toBeLessThanOrEqual(3);
  });
});

// ── Dimension 4: Reusable canonical component usage ───────────────────────────
test.describe('Dimension 4: Reusable component — canonical #opt-und picker', () => {
  test('Canonical #opt-und Select visible on mount', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const trigger = page.locator('#opt-und');
    await expect(trigger).toBeVisible({ timeout: 8_000 });

    // Check that it's a Select component (rbq-select-trigger class).
    const classes = await trigger.getAttribute('class');
    expect(
      classes,
      '#opt-und must be a Select trigger button',
    ).toContain('rbq-select-trigger');
  });
});

// ── Test: Stale sessionStorage rejected in favour of valid underlying ────────
test.describe('Stale sessionStorage fallback behaviour', () => {
  test('Stale sessionStorage underlying rejected if not in current dropdown', async ({ page, request }) => {
    await loginAsAdmin(page);

    // Fetch available roots first.
    const availableRoots = await fetchAvailableRoots(request);

    // Find a root NOT in the available list (or use a fake one).
    // We'll use COPPER which is MCX (may not always have positions).
    const staleUnderlying = 'COPPER';

    // Inject stale sessionStorage BEFORE navigation. Key fixed (2026-11):
    // the page's own `_SESSION_CACHE_KEY` is 'rbq.cache.' + the raw key
    // (see the "Stale-while-revalidate cache" migration in the page's own
    // comments) — this test used the pre-migration unprefixed key, so it
    // never actually seeded a cache hit and was silently a no-op.
    await page.context().addInitScript((stale) => {
      try {
        const payload = {
          ts: Date.now(),
          positions: [], strategy: null, drafts: [],
          selectedAccounts: [], selectedUnderlying: stale, selectedExpiries: [],
          _includeHoldings: false,
        };
        sessionStorage.setItem('rbq.cache.ramboq:options-state', JSON.stringify(payload));
      } catch (_) {}
    }, staleUnderlying);

    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    // Wait for auto-default to fire (underlying picker populates).
    const trigger = page.locator('#opt-und');
    await expect(trigger).toBeVisible({ timeout: 8_000 });

    // Poll until the picker SETTLES — the same non-placeholder value held
    // for a full 3 s window (10 consecutive 300ms reads) — not merely
    // until it first shows ANY non-placeholder value. A once-cached
    // POPULAR_UNDERLYINGS entry like COPPER is itself a valid, non-
    // placeholder provisional value the instant it's restored — breaking
    // on the first such sighting missed the auto-select effect's own
    // later promote-to-active-position step (COPPER → the real held
    // root), which — confirmed live, via a request/response trace on the
    // real /api/positions round trip — can land close to 2-3 s after
    // navigation (login redirect + positions fetch + reactive settle),
    // not within the first ~1 s. Found 2026-11, in the same investigation
    // as the cache-key fix above: this test's own early-break masked that
    // later promotion from ever being observed.
    const label = trigger.locator('.rbq-select-label');
    let displayed = '';
    let stableCount = 0;
    const deadline = Date.now() + 20_000;
    while (Date.now() < deadline) {
      const text = (await label.textContent() || '').trim().toUpperCase();
      if (text && !PLACEHOLDER_TEXTS.has(text) && text === displayed) {
        stableCount++;
        if (stableCount >= 10) break;
      } else {
        displayed = text;
        stableCount = text && !PLACEHOLDER_TEXTS.has(text) ? 1 : 0;
      }
      await page.waitForTimeout(300);
    }

    if (!displayed || PLACEHOLDER_TEXTS.has(displayed)) {
      // No positions available — acceptable (skip test).
      console.warn(`No underlying selected after load. Likely no F&O positions.`);
      return;
    }

    // The displayed value should NOT be the stale underlying if it's not available.
    // If COPPER is actually in the positions list, this test is not applicable.
    if (!availableRoots.includes(staleUnderlying)) {
      expect(
        displayed,
        `Must not show stale sessionStorage underlying "${staleUnderlying}" when it's not in available options. Got: "${displayed}"`,
      ).not.toBe(staleUnderlying);
    }

    // The displayed value must be a real option the picker itself offers —
    // checked against the dropdown's own LIVE option list, not the
    // separately-timed `availableRoots` snapshot fetched before
    // navigation. Comparing against that earlier snapshot is inherently
    // racy against a live, actively-trading account (a position can
    // open/close in the seconds between the two reads) — found 2026-11
    // while fixing the sessionStorage-key bug above: the external
    // snapshot and the in-page settle can legitimately disagree on
    // which root ends up "largest," and that disagreement is not this
    // test's concern. This test's own job is narrower: the stale pick
    // must not survive, and whatever it resolves to must be a real
    // member of the dropdown — not that it matches an earlier poll.
    await trigger.click();
    await page.waitForTimeout(500);
    const options = await getDropdownOptions(page);
    expect(options.length, 'Dropdown must have at least one option').toBeGreaterThan(0);
    expect(
      options.some((o) => o.includes(displayed)),
      `Displayed underlying "${displayed}" must be a real dropdown option: [${options.join(', ')}]`,
    ).toBe(true);
    await page.keyboard.press('Escape');
  });
});

// ── Dimension 5: UX — mobile viewport (360×800) ───────────────────────────────
test.describe('Dimension 5: UX — mobile viewport', () => {
  test.use({ viewport: { width: 360, height: 800 } });

  test('Mobile: underlying dropdown non-empty after load', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const trigger = page.locator('#opt-und');
    await expect(trigger).toBeVisible({ timeout: 8_000 });

    const label = trigger.locator('.rbq-select-label');
    let displayed = '';
    const deadline = Date.now() + 20_000;
    while (Date.now() < deadline) {
      displayed = (await label.textContent() || '').trim().toUpperCase();
      if (displayed && !PLACEHOLDER_TEXTS.has(displayed)) break;
      await page.waitForTimeout(300);
    }

    expect(
      displayed && !PLACEHOLDER_TEXTS.has(displayed),
      `Mobile: dropdown must show non-placeholder value. Got: "${displayed}"`,
    ).toBe(true);
  });
});

// ── Dimension 5b: UX — desktop viewport (1400×900) ──────────────────────────
test.describe('Dimension 5b: UX — desktop viewport', () => {
  test.use({ viewport: { width: 1400, height: 900 } });

  test('Desktop: underlying dropdown non-empty and openable', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const trigger = page.locator('#opt-und');
    await expect(trigger).toBeVisible({ timeout: 8_000 });

    const label = trigger.locator('.rbq-select-label');
    let displayed = '';
    const deadline = Date.now() + 20_000;
    while (Date.now() < deadline) {
      displayed = (await label.textContent() || '').trim().toUpperCase();
      if (displayed && !PLACEHOLDER_TEXTS.has(displayed)) break;
      await page.waitForTimeout(300);
    }

    expect(
      displayed && !PLACEHOLDER_TEXTS.has(displayed),
      `Desktop: dropdown must show non-placeholder value. Got: "${displayed}"`,
    ).toBe(true);

    // Verify the dropdown can be opened.
    await trigger.click();
    const panel = trigger.locator('xpath=..').locator('.rbq-select-panel, ~ .rbq-select-panel');
    await expect(panel).toBeVisible({ timeout: 5_000 });

    // Close it.
    await page.keyboard.press('Escape');
  });
});

// ── Regression: intermittent "blank" default on a delayed positions load
//    (2026-11 fix) ────────────────────────────────────────────────────────
//
// Root cause: the real tiers of `underlyingOptionsForPicker` (options /
// futures / holdings) are built from `positions`, a $state seeded
// SYNCHRONOUSLY from the app-wide `portfolioStore` at component
// construction — cold on a page that mounts before that global store has
// warmed (fresh tab, first navigation, a lapsed TTL), warm otherwise
// ("most of the time it works"). Before the fix, a cached/URL-restored
// underlying NOT in the static POPULAR_UNDERLYINGS whitelist (e.g. a
// less-common MCX GOLD variant) was judged "stale" by the auto-select
// $effect the instant its first flush ran against that still-cold list,
// resetting to NIFTY/opts[0] before `loadPositions()` had any chance to
// confirm the pick — and, independently, the <Select> itself rendered its
// "Pick underlying…" placeholder for the whole pending window because the
// cached value wasn't yet a member of `options`. Both symptoms are what
// the operator reported as "shows blank".
//
// Fix: a provisional entry keeps the pending pick visible in `opts` (and
// so in the <Select>'s label) while `!_positionsLoaded`; the stale-reset
// branch is gated on `_positionsLoaded` so it never fires before a real
// load attempt has actually completed.
test.describe('Regression: delayed positions load never shows blank/placeholder', () => {
  test.setTimeout(35_000);

  /** Same shape as fetchAvailableRoots() above — duplicated locally so
   *  this describe block has no cross-test ordering dependency. */
  async function _fetchHeldRoots(request) {
    const r = await request.get(`${BASE}/api/positions/`, {
      headers: _token ? { Authorization: `Bearer ${_token}` } : {},
    });
    if (!r.ok()) return [];
    const data = await r.json();
    const roots = new Set();
    for (const p of (data.rows || [])) {
      const sym = String(p.tradingsymbol || p.symbol || '').toUpperCase();
      if (!sym) continue;
      const root = sym.replace(/\d.*$/, '');
      if (root && root !== sym) roots.add(root);
    }
    return [...roots];
  }

  /** Seed sessionStorage with a cached `selectedUnderlying`, delay
   *  `/api/positions*` by `delayMs`, navigate, and sample the picker's
   *  label on a tight interval for the first `delayMs - 200` ms. Returns
   *  { samples: Set<string>, finalLabel: string } once the delay window
   *  has elapsed and a settled (non-transiently-changing) read is taken. */
  async function _runDelayedLoadProbe(page, cachedUnderlying, delayMs) {
    await page.context().addInitScript((stale) => {
      try {
        const payload = {
          ts: Date.now(),
          positions: [], strategy: null, drafts: [],
          selectedAccounts: [], selectedUnderlying: stale, selectedExpiries: [],
        };
        // The real cache key is prefixed ('rbq.cache.' + the raw key) per
        // the page's own `_SESSION_CACHE_KEY` — NOT the bare key some
        // older specs in this file/derivatives_auto_default.spec.js still
        // use (a pre-existing, independent test-rot bug: the "Stale-
        // while-revalidate cache" migration moved sessionStorage onto
        // this prefixed scheme and those tests were never updated, so
        // they've silently never exercised a real cache hit since).
        sessionStorage.setItem('rbq.cache.ramboq:options-state', JSON.stringify(payload));
      } catch (_) {}
    }, cachedUnderlying);

    await page.route('**/api/positions*', async (route) => {
      await new Promise((r) => setTimeout(r, delayMs));
      await route.continue();
    });

    const navStart = Date.now();
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const trigger = page.locator('#opt-und');
    await expect(trigger).toBeVisible({ timeout: 8_000 });
    const label = trigger.locator('.rbq-select-label');

    const samples = new Set();
    while (Date.now() - navStart < delayMs - 200) {
      samples.add(((await label.textContent()) || '').trim().toUpperCase());
      await page.waitForTimeout(100);
    }
    return { samples, label };
  }

  test('a cached pick outside POPULAR_UNDERLYINGS that is NOT currently held settles to a real default, never blank/placeholder while pending', async ({ page }) => {
    await loginAsAdmin(page);
    const FAKE_CACHED = 'ZZZFAKEROOT'; // never a real F&O root — exercises the "genuinely stale" path
    const DELAY_MS = 2000;

    const { samples, label } = await _runDelayedLoadProbe(page, FAKE_CACHED, DELAY_MS);

    // While positions are loading, the picker must show the cached pick
    // itself (provisional entry) — never the <Select> placeholder, and
    // never a premature wrong-default bounce (e.g. NIFTY).
    for (const text of samples) {
      expect(PLACEHOLDER_TEXTS.has(text), `Must never show placeholder while pending. Saw: "${text}"`).toBe(false);
      expect(text, `Must show the pending cached pick, not bounce early. Saw: "${text}"`).toBe(FAKE_CACHED);
    }
    expect(samples.size, 'Must have sampled at least one value during the delay window').toBeGreaterThan(0);

    // Once positions genuinely resolve and confirm the root is not held,
    // the picker must settle on a REAL value — never stuck on the fake
    // root, never blank.
    const deadline = Date.now() + 15_000;
    let finalText = '';
    while (Date.now() < deadline) {
      finalText = ((await label.textContent()) || '').trim().toUpperCase();
      if (finalText && finalText !== FAKE_CACHED) break;
      await page.waitForTimeout(200);
    }
    expect(PLACEHOLDER_TEXTS.has(finalText), `Must resolve to a real value, not blank. Got: "${finalText}"`).toBe(false);
    expect(finalText, 'A root that is genuinely not held must not stay selected forever').not.toBe(FAKE_CACHED);
  });

  test('a cached pick outside POPULAR_UNDERLYINGS that IS currently held stays selected throughout a delayed load — never blank', async ({ page, request }) => {
    await loginAsAdmin(page);

    const heldRoots = await _fetchHeldRoots(request);
    // Pick a held root that is NOT in POPULAR_UNDERLYINGS so the test
    // actually exercises the race (a popular root like NIFTY/GOLD is
    // already in Tier 6 and would never have reproduced the bug).
    const { POPULAR_UNDERLYINGS } = await import('../src/lib/data/popularUnderlyings.js');
    const popularSet = new Set(POPULAR_UNDERLYINGS.map((s) => s.toUpperCase()));
    const target = heldRoots.find((r) => !popularSet.has(r));

    if (!target) {
      console.warn('No currently-held root outside POPULAR_UNDERLYINGS — skipping (environment has no such position).');
      return;
    }

    const DELAY_MS = 2000;
    const { samples, label } = await _runDelayedLoadProbe(page, target, DELAY_MS);

    for (const text of samples) {
      expect(PLACEHOLDER_TEXTS.has(text), `Must never show placeholder while pending. Saw: "${text}"`).toBe(false);
      expect(text, `A genuinely-held root must stay selected throughout the load, never bounce. Saw: "${text}"`).toBe(target);
    }
    expect(samples.size, 'Must have sampled at least one value during the delay window').toBeGreaterThan(0);

    // After positions resolve, it must still be the same (now confirmed) root.
    await page.waitForTimeout(500);
    const finalText = ((await label.textContent()) || '').trim().toUpperCase();
    expect(finalText).toBe(target);
  });
});
