/**
 * infohint_singleton.spec.js
 *
 * Only one InfoHint tooltip is ever visible at a time, app-wide. Before this
 * fix, every InfoHint instance managed its own `open`/`hovered` $state
 * entirely independently (frontend/src/lib/InfoHint.svelte), so opening a
 * second tooltip anywhere else on the page never closed one already open
 * elsewhere — two (or more) popovers could be visible simultaneously.
 *
 * Fix: a module-level `$state` singleton (`_activeInfoHintId`, declared in
 * InfoHint.svelte's `<script module>` block) is shared across every
 * component instance. Whichever instance's own popout becomes visible
 * (click OR hover) claims the singleton; every other instance watches it
 * and closes itself the moment some other instance claims it.
 *
 * This spec is deliberately cross-component (two SEPARATE InfoHint
 * instances on the same page, each independently owning its own `open`/
 * `hovered` state before this fix) and deliberately avoids the
 * `mousedown`-driven click-outside-closes listener InfoHint already had
 * before this fix (see InfoHint.svelte's `onDocClick` effect) — that
 * listener already closed a click-pinned popover whenever a later CLICK
 * landed outside it, which would make a click-then-click test pass on old
 * code and prove nothing about the new singleton. Every test here pins
 * tooltip A open via CLICK, then opens tooltip B via HOVER ONLY (hover
 * never fires `mousedown`), so only the new singleton effect can be
 * responsible for closing A.
 *
 * Covers both InfoHint display modes per the operator's ask:
 *   - default chip mode (visible `.info-btn`, popup=true, no hideButton) —
 *     /automation/templates header hints, always rendered (no backend data
 *     dependency), two independent instances on one page.
 *   - hideButton+anchor mode (bound `open` prop, externally-triggered) is
 *     covered by the sibling test added to
 *     e2e/derivatives_greek_header_chip_infohint.spec.js, which also proves
 *     the `bind:open` propagation back to the parent's own state —
 *     the case most likely to have a subtle bug per the task brief.
 *
 * Run:
 *   npx playwright test e2e/infohint_singleton.spec.js --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.describe('InfoHint — app-wide single-tooltip-at-a-time singleton (default chip mode)', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/automation/templates', { waitUntil: 'domcontentloaded', timeout: 30_000 });
  });

  test('click-pinning the "Templates" title hint then HOVERING the unrelated "Side-default coverage" hint closes the first and shows only the second', async ({ page }) => {
    // Two independent default-chip-mode InfoHint instances, both always
    // rendered (neither gated behind template data), so this test never
    // skips. Scoped by distinct ancestor containers so each resolves to
    // exactly one `.info-btn`.
    const titleWrap = page.locator('.algo-title-group .info-wrap');
    const coverageWrap = page.locator('.tpl-matrix-head .info-wrap');

    const titleBtn = titleWrap.locator('button.info-btn');
    const coverageBtn = coverageWrap.locator('button.info-btn');
    await expect(titleBtn).toBeVisible();
    await expect(coverageBtn).toBeVisible();

    // Pin A open via CLICK (open=true, not merely hovered).
    await titleBtn.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(1);
    await expect(page.locator('[role="tooltip"]').first()).toContainText('Order templates');
    await expect(titleBtn).toHaveAttribute('aria-expanded', 'true');

    // Open B via HOVER ONLY — no mousedown anywhere on the page. A's own
    // click-pinned popout is `position: fixed` and, on this page, renders
    // directly over the coverage button's screen coordinates (both sit
    // near the top of a short page), so a real `.hover()` mouse move gets
    // blocked by Playwright's actionability check ("intercepts pointer
    // events") before it ever reaches B. `dispatchEvent('mouseenter')`
    // fires the exact same native event InfoHint's `onmouseenter` listens
    // for, directly on B, without needing real cursor travel through the
    // occluded screen region — still exercises the real hover code path.
    await coverageBtn.dispatchEvent('mouseenter');
    const tooltips = page.locator('[role="tooltip"]');
    await expect(tooltips).toHaveCount(1, { timeout: 2000 });
    await expect(tooltips.first()).toContainText('is_default template');
    await expect(tooltips.first()).not.toContainText('Order templates');

    // A's own internal `open` state must have actually flipped to false
    // (not just be visually hidden some other way) — its own button
    // reflects its own `open` via aria-expanded.
    await expect(titleBtn).toHaveAttribute('aria-expanded', 'false');

    // Close B via hover-out, then A must re-open on a single click — if A's
    // `open` had silently stayed true while hidden, this click would
    // toggle it to false and A would need a second click to reopen.
    await coverageBtn.dispatchEvent('mouseleave');
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0, { timeout: 500 });

    await titleBtn.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(1);
    await expect(page.locator('[role="tooltip"]').first()).toContainText('Order templates');
    await expect(titleBtn).toHaveAttribute('aria-expanded', 'true');
  });

  test('the reverse order also holds: pinning "Side-default coverage" then hovering "Templates" closes the first', async ({ page }) => {
    // Guards against an id-ordering / first-claimed-wins bug — the
    // singleton must work symmetrically regardless of which instance
    // claimed it first.
    const titleBtn = page.locator('.algo-title-group .info-wrap button.info-btn');
    const coverageBtn = page.locator('.tpl-matrix-head .info-wrap button.info-btn');

    await coverageBtn.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(1);
    await expect(page.locator('[role="tooltip"]').first()).toContainText('is_default template');

    // Same occlusion reasoning as the first test — B's own click-pinned
    // popout can cover A's screen position, so hover is dispatched
    // directly rather than via real cursor movement.
    await titleBtn.dispatchEvent('mouseenter');
    const tooltips = page.locator('[role="tooltip"]');
    await expect(tooltips).toHaveCount(1, { timeout: 2000 });
    await expect(tooltips.first()).toContainText('Order templates');
    await expect(coverageBtn).toHaveAttribute('aria-expanded', 'false');
  });
});
