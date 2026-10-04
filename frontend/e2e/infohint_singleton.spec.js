/**
 * infohint_singleton.spec.js
 *
 * Only one InfoHint tooltip is ever visible at a time, app-wide. Before the
 * singleton fix, every InfoHint instance managed its own `open` $state
 * entirely independently (frontend/src/lib/InfoHint.svelte), so opening a
 * second tooltip anywhere else on the page never closed one already open
 * elsewhere — two (or more) popovers could be visible simultaneously.
 *
 * Fix: a module-level `$state` singleton (`_activeInfoHintId`, declared in
 * InfoHint.svelte's `<script module>` block) is shared across every
 * component instance. Whichever instance's own popout becomes visible
 * (via `open`) claims the singleton; every other instance watches it and
 * closes itself the moment some other instance claims it.
 *
 * Hover-preview + click-to-pin (2026-10 reintroduction, amends the prior
 * hover-removal pass): hovering a trigger (desktop, non-touch) now shows
 * a transient PREVIEW again, modeled on OptionsPayoff.svelte's own
 * hover/pin tooltip. Critically, a hover preview NEVER claims the
 * app-wide singleton — only a CLICK (which pins `open = true`) does.
 * This file's single most important regression test is therefore:
 * hovering trigger B while trigger A is click-pinned-open must leave A
 * open (see "HOVER-PREVIEW never evicts a click-PINNED popup" below).
 *
 * This spec originally proved the singleton with a CLICK-then-HOVER
 * sequence specifically to rule out the pre-existing `mousedown`-driven
 * click-outside-closes listener (hover never fires `mousedown`, so only
 * the singleton effect could close A). That isolation trick is no
 * longer needed for the keyboard-activation tests below, but the same
 * underlying fact (keyboard-triggered `click` fires no `mousedown`) is
 * still exploited to isolate the singleton-claim effect from the
 * click-outside listener.
 *
 * Fix for this revision: pin tooltip A open via a real mouse CLICK, then
 * open tooltip B via KEYBOARD activation (Tab to focus the real `<button>`,
 * then Enter/Space) instead of a mouse click. Keyboard activation of a
 * native `<button>` fires a `click` event with NO preceding `mousedown`
 * (browsers only synthesize `mousedown`+`mouseup`+`click` for an actual
 * pointer press), so the click-outside listener's `mousedown` handler never
 * fires for B's keyboard activation — only the new singleton effect can be
 * responsible for closing A. This also sidesteps the occlusion problem
 * `.hover()` used to hit (A's own click-pinned popout can render directly
 * over B's screen coordinates on this page, blocking a real pointer hover
 * via Playwright's actionability check) since keyboard activation needs no
 * cursor travel at all.
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

  test('click-pinning the "Templates" title hint then KEYBOARD-activating the unrelated "Side-default coverage" hint closes the first and shows only the second', async ({ page }) => {
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

    // Pin A open via CLICK (open=true).
    await titleBtn.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(1);
    await expect(page.locator('[role="tooltip"]').first()).toContainText('Order templates');
    await expect(titleBtn).toHaveAttribute('aria-expanded', 'true');

    // Open B via KEYBOARD activation, not a mouse click. A's own
    // click-pinned popout is `position: fixed` and, on this page, renders
    // directly over the coverage button's screen coordinates (both sit
    // near the top of a short page), so a real pointer click/hover on B
    // gets blocked by Playwright's actionability check ("intercepts
    // pointer events"). Keyboard activation needs no cursor travel through
    // the occluded region AND — the real point of this test — a native
    // `<button>`'s keyboard-triggered `click` event fires with no
    // preceding `mousedown`, so InfoHint's `mousedown`-driven
    // click-outside-closes listener cannot be what closes A here. Only the
    // app-wide singleton effect can.
    await coverageBtn.focus();
    await page.keyboard.press('Enter');
    const tooltips = page.locator('[role="tooltip"]');
    await expect(tooltips).toHaveCount(1, { timeout: 2000 });
    await expect(tooltips.first()).toContainText('is_default template');
    await expect(tooltips.first()).not.toContainText('Order templates');

    // A's own internal `open` state must have actually flipped to false
    // (not just be visually hidden some other way) — its own button
    // reflects its own `open` via aria-expanded.
    await expect(titleBtn).toHaveAttribute('aria-expanded', 'false');

    // Close B via a second keyboard activation, then A must re-open on a
    // single click — if A's `open` had silently stayed true while hidden,
    // this click would toggle it to false and A would need a second click
    // to reopen.
    await page.keyboard.press('Enter');
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0);

    await titleBtn.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(1);
    await expect(page.locator('[role="tooltip"]').first()).toContainText('Order templates');
    await expect(titleBtn).toHaveAttribute('aria-expanded', 'true');
  });

  test('the reverse order also holds: pinning "Side-default coverage" then keyboard-activating "Templates" closes the first', async ({ page }) => {
    // Guards against an id-ordering / first-claimed-wins bug — the
    // singleton must work symmetrically regardless of which instance
    // claimed it first.
    const titleBtn = page.locator('.algo-title-group .info-wrap button.info-btn');
    const coverageBtn = page.locator('.tpl-matrix-head .info-wrap button.info-btn');

    await coverageBtn.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(1);
    await expect(page.locator('[role="tooltip"]').first()).toContainText('is_default template');

    // Same occlusion + mousedown-isolation reasoning as the first test —
    // B's own click-pinned popout can cover A's screen position, and a
    // keyboard-triggered click fires no `mousedown` to trip the
    // click-outside listener.
    await titleBtn.focus();
    await page.keyboard.press('Enter');
    const tooltips = page.locator('[role="tooltip"]');
    await expect(tooltips).toHaveCount(1, { timeout: 2000 });
    await expect(tooltips.first()).toContainText('Order templates');
    await expect(coverageBtn).toHaveAttribute('aria-expanded', 'false');
  });

  test('HOVER shows a preview but never pins — moving away hides it, and it never claims the singleton', async ({ page }) => {
    const titleBtn = page.locator('.algo-title-group .info-wrap button.info-btn');
    await expect(titleBtn).toBeVisible();

    // Hover shows a preview (`visible = open || _hoverPreview`), even
    // though `open` (pinned) stays false the whole time.
    await titleBtn.hover();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(1);
    await expect(titleBtn).toHaveAttribute('aria-expanded', 'false');

    // Moving the mouse away hides the preview again — it never pinned.
    await page.mouse.move(5, 5);
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0);

    // Positive control on the same element — click still pins it open.
    await titleBtn.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(1);
    await expect(titleBtn).toHaveAttribute('aria-expanded', 'true');

    await titleBtn.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0);
  });

  test('HOVER-PREVIEW never evicts a click-PINNED popup — the single most important regression in this file', async ({ page }) => {
    const titleBtn = page.locator('.algo-title-group .info-wrap button.info-btn');
    const coverageBtn = page.locator('.tpl-matrix-head .info-wrap button.info-btn');

    // Pin A open via a real click.
    await titleBtn.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(1);
    await expect(page.locator('[role="tooltip"]').first()).toContainText('Order templates');
    await expect(titleBtn).toHaveAttribute('aria-expanded', 'true');

    // Hover B via a dispatched pointerenter rather than a real
    // `.hover()` — A's own click-pinned popout is `position: fixed` and
    // renders directly over B's screen coordinates on this page (same
    // occlusion noted elsewhere in this file for clicks), which fails
    // Playwright's actionability check for a genuine pointer move.
    // Dispatching the event directly targets B regardless of what's
    // visually on top, which is exactly what's needed here — this test
    // is about InfoHint's own pointerenter handler, not real cursor
    // travel. If hover wrongly claimed the singleton (gating on
    // `visible` instead of `open`), A would close here — that's exactly
    // the regression this fix prevents.
    await coverageBtn.dispatchEvent('pointerenter', { pointerType: 'mouse' });
    await page.waitForTimeout(150);
    await expect(titleBtn, 'A must stay pinned — a hover preview on B must never evict it').toHaveAttribute('aria-expanded', 'true');
    await expect(page.locator('[role="tooltip"]'), 'both A (pinned) and B (preview) may render at once').toHaveCount(2);
    const tooltipTexts = await page.locator('[role="tooltip"]').allTextContents();
    expect(tooltipTexts.some((t) => t.includes('Order templates'))).toBe(true);

    // Leaving B drops its preview; A is still pinned, untouched.
    await coverageBtn.dispatchEvent('pointerleave', { pointerType: 'mouse' });
    await expect(page.locator('[role="tooltip"]')).toHaveCount(1);
    await expect(titleBtn).toHaveAttribute('aria-expanded', 'true');
  });

  test('clicking the same field twice opens then closes it (singleton claim does not break same-instance toggle)', async ({ page }) => {
    // The singleton's "close effect" only fires for an instance when
    // _activeInfoHintId points to a DIFFERENT instance's uid. Closing via
    // a second click on the SAME field sets `open = false` directly and
    // never changes _activeInfoHintId, so this must keep working exactly
    // as it did before the singleton fix — explicit regression coverage
    // for the operator's direct follow-up question about this.
    const titleBtn = page.locator('.algo-title-group .info-wrap button.info-btn');

    await titleBtn.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(1);
    await expect(titleBtn).toHaveAttribute('aria-expanded', 'true');

    await titleBtn.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0);
    await expect(titleBtn).toHaveAttribute('aria-expanded', 'false');

    // And a third click reopens it — proves the toggle keeps cycling, not
    // just a one-way close.
    await titleBtn.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(1);
    await expect(titleBtn).toHaveAttribute('aria-expanded', 'true');
  });
});
