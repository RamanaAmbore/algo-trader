/**
 * cardheader_sweep_round2.spec.js
 *
 * 2026-10-13 "CardHeader sweep, round 2" — items 1, 2, and 4 only
 * (items 3 and 5 are owned by companion agents working the same plan
 * section in parallel; this spec does not touch their files/tests).
 *
 * Item 1 — header bottom-border contrast. Investigation found the
 *   plan's own premise understated the bug: the border wasn't merely
 *   "subtle" on non-grid panels, it never rendered AT ALL, anywhere in
 *   the algo app, since the shared `--ch-border-bottom` mechanism was
 *   introduced (2026-10-12) — `--card-divider` is declared only inside
 *   `.card-theme-dark`/`.card-theme-cream` (app.css), a class on a DIV
 *   that is a CHILD of `<body>`, but the `:global(body) { --ch-border-
 *   bottom: 1px solid var(--card-divider); }` rule that feeds every
 *   CardHeader is declared ON body itself — custom properties fully
 *   substitute their own var() references at their OWN declaration
 *   site using THAT element's inheritance chain, and body has no
 *   access to a descendant's custom property. Confirmed via direct
 *   `getComputedStyle` measurement (`borderBottomWidth: 0px` on
 *   HeldOrdersCard's CardHeader) BEFORE fixing. Real fix has two parts:
 *   (a) bump `--card-divider`'s dark-theme alpha 0.20 → 0.40 (app.css),
 *   (b) ALSO declare `--card-divider` directly inside `(algo)/
 *   +layout.svelte`'s own `:global(body)` block, so the value is
 *   actually reachable from body's own cascade (see that file's
 *   comment for the full root-cause writeup). HeldOrdersCard (a
 *   non-grid panel, confirmed via round-1's own test convention) is
 *   the verification target.
 *
 * Item 2 — `.ch-sep` (CardHeader's internal vertical divider, rendered
 *   whenever a `middle` snippet is supplied — e.g. every LogPanel
 *   instance, which always renders its tab strip in `middle`) switched
 *   from `var(--sep-color)` (rgba(...,0.10), a token shared by dozens of
 *   unrelated dividers app-wide, deliberately NOT bumped) to
 *   `var(--card-divider)` — the SAME token the header's own bottom
 *   border uses, for internal consistency.
 *
 * Item 4 — investigated whether Automation's `multiColumn={true}` prop
 *   on its bottom-strip ActivityLogSurface mount causes a real width/
 *   density difference vs. ActivityLogModal (which passes no explicit
 *   `multiColumn` at all). Finding (confirmed by source-reading
 *   ActivityLogSurface.svelte's own context-derived default BEFORE
 *   writing this test): `context="modal"` is already in the
 *   context-derived true-list (`context === 'page' || 'modal' ||
 *   'card-wide'`), so the modal's own OMITTED `multiColumn` resolves to
 *   `true` via that default — identically to Automation's explicit
 *   `multiColumn={true}` (itself redundant, since Automation's
 *   `context="page"` is also in that true-list). Both surfaces were
 *   ALREADY computing the identical `_multiColumn` value before this
 *   round even started. This test proves that empirically rather than
 *   just by source-reading: no code change was made for item 4.
 *
 * Run:
 *   cd frontend && npx playwright test e2e/cardheader_sweep_round2.spec.js \
 *     --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

/** Parses a computed `rgb(...)`/`rgba(...)` color string into
 *  {r,g,b,a} (a defaults to 1 for 3-channel `rgb(...)`). */
function parseRGBA(str) {
  const m = /rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)/.exec(str || '');
  if (!m) return null;
  return { r: Number(m[1]), g: Number(m[2]), b: Number(m[3]), a: m[4] !== undefined ? Number(m[4]) : 1 };
}

test.describe('CardHeader sweep round 2 — item 1: --card-divider bottom-border contrast', () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    // Quiet the rest of /orders' data surfaces — HeldOrdersCard's own
    // CardHeader is the only thing under test (same mocking convention
    // as cardheader_adoption_sweep.spec.js's HeldOrdersCard describe).
    // HeldOrdersCard only renders its `.held-card` section when there's
    // at least one held row — same fixture shape cardheader_adoption_
    // sweep.spec.js's HeldOrdersCard describe already uses.
    await page.route('**/api/orders/held/**', (route) => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ held: [{
        id: 'held-1', side: 'BUY', qty: 50, symbol: 'RBQ-HELDTEST', account: 'T1',
        hold: JSON.stringify({ category: 'expiry_close' }),
      }] }),
    }));
    await page.route('**/api/orders/chases/active**', (route) => route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify([]),
    }));
    await page.goto('/orders', { waitUntil: 'domcontentloaded' });
  });

  test('HeldOrdersCard (non-grid CardHeader panel) bottom border is now visibly distinct, not a near-invisible 0.20 alpha', async ({ page }) => {
    const header = page.locator('.held-card .card-header');
    await expect(header).toBeVisible({ timeout: 20_000 });

    const { color, width } = await header.evaluate((el) => {
      const s = getComputedStyle(el);
      return { color: s.borderBottomColor, width: s.borderBottomWidth };
    });

    expect(width, 'bottom border must actually be rendered (non-zero width)').not.toBe('0px');
    const rgba = parseRGBA(color);
    expect(rgba, `border-bottom-color should be an rgba() value, got "${color}"`).not.toBeNull();

    // Same blue-grey triple as before the bump (126,151,184) — only the
    // alpha changed.
    expect(rgba.r).toBeCloseTo(126, 0);
    expect(rgba.g).toBeCloseTo(151, 0);
    expect(rgba.b).toBeCloseTo(184, 0);

    // The real assertion: alpha must be the NEW, more-visible value
    // (0.40 at time of writing), not the old near-invisible 0.20 — and
    // must clearly exceed the old value regardless of the exact number
    // chosen, so this test doesn't need updating if the number is
    // fine-tuned slightly in a future pass.
    expect(rgba.a, `alpha ${rgba.a} must be visibly bumped above the old 0.20`).toBeGreaterThan(0.30);
  });
});

test.describe('CardHeader sweep round 2 — item 2: .ch-sep uses --card-divider, not --sep-color', () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  /** Reads --card-divider/--sep-color as resolved at a given locator's
   *  own element (not a separate probe element) — avoids any risk of
   *  reading the wrong scope's cascade. */
  async function readTokensAt(locator) {
    return await locator.evaluate((el) => {
      const s = getComputedStyle(el);
      return {
        cardDivider: s.getPropertyValue('--card-divider').trim(),
        sepColor: s.getPropertyValue('--sep-color').trim(),
      };
    });
  }

  test('.ch-sep on a normal (non-portaled) LogPanel CardHeader — Automation strip — matches --card-divider, not the half-alpha --sep-color', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/automation', { waitUntil: 'domcontentloaded' });

    const sep = page.locator('.lp-header-wrap .ch-sep').first();
    await expect(sep).toBeAttached({ timeout: 20_000 });

    const sepBg = await sep.evaluate((el) => getComputedStyle(el).backgroundColor);
    const sepRgba = parseRGBA(sepBg);
    expect(sepRgba, `.ch-sep background should be an rgba() value, got "${sepBg}"`).not.toBeNull();

    // Read the CURRENT resolved values of both tokens at the SAME
    // element, rather than hardcoding literals — keeps this test
    // correct even if item 1's exact alpha is retuned later.
    const tokenValues = await readTokensAt(sep);
    const cardDividerRgba = parseRGBA(tokenValues.cardDivider);
    const sepColorRgba = parseRGBA(tokenValues.sepColor);
    expect(cardDividerRgba, `--card-divider should resolve to rgba(), got "${tokenValues.cardDivider}"`).not.toBeNull();
    expect(sepColorRgba, `--sep-color should resolve to rgba(), got "${tokenValues.sepColor}"`).not.toBeNull();

    // .ch-sep's alpha must match --card-divider's current alpha...
    expect(sepRgba.a).toBeCloseTo(cardDividerRgba.a, 2);
    // ...and must NOT match --sep-color's alpha (confirms the switch
    // actually took effect, not a coincidental equal value).
    expect(sepRgba.a).not.toBeCloseTo(sepColorRgba.a, 2);
  });

  test('.ch-sep AND the header bottom border both resolve correctly inside the portaled ActivityLogModal too (not just non-portaled mounts)', async ({ page }) => {
    // $lib/portal.js moves ActivityLogModal's root node to a document.body
    // child that is a SIBLING of `.algo-viewport.card-theme-dark`, not a
    // descendant — so a --card-divider declared ONLY inside that class
    // would never reach this subtree's cascade (confirmed empirically
    // while building this spec: this is the SAME root cause that broke
    // CardHeader's bottom border everywhere, see +layout.svelte's own
    // comment on the body-level --card-divider fix). Both `.ch-sep` and
    // `--ch-border-bottom` depend on --card-divider resolving at BODY
    // itself (not just under `.card-theme-dark`), which is exactly what
    // that fix provides — verified here end-to-end on the one surface
    // that actually sits outside the themed DOM subtree.
    await loginAsAdmin(page);
    await page.goto('/orders', { waitUntil: 'domcontentloaded' });
    await page.getByRole('button', { name: 'Open Activity' }).click();
    const modal = page.locator('[role="dialog"][aria-label="Activity log"]');
    await expect(modal).toBeVisible({ timeout: 10_000 });

    const header = modal.locator('.card-header').first();
    await expect(header).toBeAttached({ timeout: 10_000 });
    const headerInfo = await header.evaluate((el) => {
      const s = getComputedStyle(el);
      return { color: s.borderBottomColor, width: s.borderBottomWidth };
    });
    expect(headerInfo.width, 'modal CardHeader bottom border must actually render').not.toBe('0px');
    const headerRgba = parseRGBA(headerInfo.color);
    expect(headerRgba, `border-bottom-color should be rgba(), got "${headerInfo.color}"`).not.toBeNull();

    const sep = modal.locator('.card-header .ch-sep').first();
    await expect(sep).toBeAttached({ timeout: 10_000 });
    const sepBg = await sep.evaluate((el) => getComputedStyle(el).backgroundColor);
    const sepRgba = parseRGBA(sepBg);
    expect(sepRgba, `.ch-sep background should be rgba(), got "${sepBg}"`).not.toBeNull();

    const tokenValues = await readTokensAt(sep);
    const cardDividerRgba = parseRGBA(tokenValues.cardDivider);
    expect(cardDividerRgba, `--card-divider should resolve to rgba() even inside the portal, got "${tokenValues.cardDivider}"`).not.toBeNull();

    // Both the border and .ch-sep must match --card-divider's CURRENT
    // alpha here, not fall back to --sep-color's (half) alpha — proving
    // the body-level fix actually reaches a portaled mount, not just
    // in-viewport panels.
    expect(headerRgba.a).toBeCloseTo(cardDividerRgba.a, 2);
    expect(sepRgba.a).toBeCloseTo(cardDividerRgba.a, 2);
  });
});

test.describe('CardHeader sweep round 2 — item 4: multiColumn parity, Automation strip vs Activity modal', () => {
  test.use({ viewport: { width: 1440, height: 900 } });
  test.setTimeout(60_000);

  test('both surfaces already resolve multiColumn=true identically on a shared non-order tab — no fix needed', async ({ page }) => {
    await loginAsAdmin(page);

    // ── Automation's bottom-attached strip ──────────────────────────
    await page.goto('/automation', { waitUntil: 'domcontentloaded' });
    const stripWrap = page.locator('.lp-header-wrap').first();
    await expect(stripWrap).toBeVisible({ timeout: 20_000 });
    // Strip defaults to the 'agent' tab (automation/+page.svelte:
    // `let logTab = $state('agent')`) — explicit click makes this
    // deterministic regardless of that default. Scoped to the strip's
    // OWN tab bar (`.lp-header-wrap .ch-middle`) — /automation's page-
    // level Console/Agents switcher (`AutomationTabs.svelte`) ALSO has
    // a tab literally labeled "Agents", an unrelated collision an
    // unscoped page-wide locator would strict-mode-fail on.
    await page.locator('.lp-header-wrap .ch-middle').getByRole('tab', { name: 'Agents', exact: true }).click();
    const stripRows = page.locator('.log-panel.log-rows').first();
    await expect(stripRows).toBeVisible({ timeout: 10_000 });
    const stripInfo = await stripRows.evaluate((el) => {
      const s = getComputedStyle(el);
      return {
        hasMulticolClass: el.classList.contains('lp-multicol'),
        gridTemplateColumns: s.gridTemplateColumns,
        width: el.getBoundingClientRect().width,
      };
    });

    // ── ActivityLogModal, opened from a different page (/orders) ───
    await page.goto('/orders', { waitUntil: 'domcontentloaded' });
    await page.getByRole('button', { name: 'Open Activity' }).click();
    const modal = page.locator('[role="dialog"][aria-label="Activity log"]');
    await expect(modal).toBeVisible({ timeout: 10_000 });
    await modal.locator('.lp-header-wrap .ch-middle').getByRole('tab', { name: 'Agents', exact: true }).click();
    const modalRows = modal.locator('.log-panel.log-rows').first();
    await expect(modalRows).toBeVisible({ timeout: 10_000 });
    const modalInfo = await modalRows.evaluate((el) => {
      const s = getComputedStyle(el);
      return {
        hasMulticolClass: el.classList.contains('lp-multicol'),
        gridTemplateColumns: s.gridTemplateColumns,
        width: el.getBoundingClientRect().width,
      };
    });

    // Finding: ActivityLogSurface's own context-derived default already
    // resolves modal's multiColumn to `true` (context "modal" is in the
    // same true-list as "page"/"card-wide"), identically to Automation's
    // explicit `multiColumn={true}` prop (itself redundant given
    // context="page"). Both surfaces engage the 2-column grid; this is
    // NOT the source of any real visual divergence between them. Logged
    // explicitly below (not just asserted) so the exact measured numbers
    // are visible in test output/CI logs, not just a pass/fail.
    console.log(
      `[item 4] strip: multicol=${stripInfo.hasMulticolClass} cols="${stripInfo.gridTemplateColumns}" width=${stripInfo.width.toFixed(1)}px | ` +
      `modal: multicol=${modalInfo.hasMulticolClass} cols="${modalInfo.gridTemplateColumns}" width=${modalInfo.width.toFixed(1)}px | ` +
      `delta=${Math.abs(stripInfo.width - modalInfo.width).toFixed(1)}px`
    );
    expect(stripInfo.hasMulticolClass, 'strip should engage 2-col layout on the Agents tab').toBe(true);
    expect(modalInfo.hasMulticolClass, 'modal should ALSO engage 2-col layout on the Agents tab (same as strip)').toBe(true);
    expect(modalInfo.gridTemplateColumns).toBe(stripInfo.gridTemplateColumns);

    // Outer rendered widths should be close (same site-wide gutter
    // convention round 1 already fixed for `.lp-header-wrap`) — a
    // generous tolerance here since the strip spans the full page
    // width and the modal is capped at 96vw; this is NOT re-asserting
    // round 1's tighter `.lp-header-wrap` check, just confirming
    // neither container collapses to something wildly different.
    const widthDelta = Math.abs(stripInfo.width - modalInfo.width);
    expect(widthDelta, `strip width ${stripInfo.width} vs modal width ${modalInfo.width}`).toBeLessThan(40);
  });
});
