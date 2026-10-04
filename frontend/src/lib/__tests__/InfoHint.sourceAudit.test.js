/**
 * InfoHint.sourceAudit.test.js
 *
 * InfoHint.svelte has no existing component-mount test harness in this
 * repo (no @testing-library/svelte, no `mount()` helper anywhere under
 * src/lib/__tests__) — vitest here runs in the `node` environment per
 * vitest.config.js, and adding a new mounting dependency is out of scope
 * for this change. Full interactive behavior (open/close, click-outside
 * exemption via `anchor`, popover wording) is covered live in
 * e2e/derivatives_greek_header_chip_infohint.spec.js via Playwright.
 *
 * This source-audit test is the vitest-level guard for the two additive
 * props added to support an external click trigger (`hideButton` +
 * bindable `open` + `anchor`):
 *   1. `hideButton` defaults to `false` — every pre-existing caller that
 *      doesn't pass it keeps rendering its own `.info-btn` button exactly
 *      as before this change.
 *   2. `open` is declared via `$bindable(...)`, not a plain `$state(...)`
 *      — required for an external trigger (e.g. a Greek chip's value
 *      text) to open/close the popover via `bind:open`.
 *   3. The button render is now conditional (`{#if !hideButton}`), so a
 *      `hideButton` caller renders no `.info-btn` at all.
 *   4. `anchor` prop exists and is consulted by both the click-outside
 *      listener and the popup position `fit()` — without this, a
 *      `hideButton` instance's popover could never be closed by
 *      re-clicking its own external trigger (mousedown-vs-click race).
 *
 * Hover-preview + click-to-pin (2026-10 reintroduction, amends the prior
 * "hover removal" guard below). Operator feedback after the strict
 * click-only pass: hovering a trigger should show a transient PREVIEW
 * (not pin it), modeled on OptionsPayoff.svelte's own hover/pin tooltip.
 * The invariants that matter here, since this is the only test the
 * repo's final gate actually runs for this component:
 *   - the singleton claim gates on `open` (pinned), never on `visible`
 *     or hover state — hovering one instance must never evict another
 *     instance's pinned popover
 *   - `_hoverPreview` is cleared on every close transition (open:
 *     true→false) and on pointerleave, so a dismissed pin never leaves
 *     a ghost preview behind
 *   - the 350ms re-hover suppression constant matches
 *     OptionsPayoff.svelte's own `_dismissHover()` window
 *   - touch (`pointerType === 'touch'`) skips the preview phase
 *     entirely
 *   - Escape dismisses a pinned popup (new — InfoHint previously had no
 *     Esc handling at all)
 */

import { describe, it, expect } from 'vitest';
import SRC from '../InfoHint.svelte?raw';

// InfoHint.svelte now opens with a `<script module>` block (the app-wide
// single-tooltip-at-a-time singleton) BEFORE the main `<script>` block —
// `SRC.indexOf('</script>')` alone would find the MODULE block's closing
// tag (which appears first in the file), not the main block's, producing
// an empty/inverted slice. Anchor both searches to start from the main
// `<script>` tag's own position so the module block is skipped entirely.
const scriptStart = SRC.indexOf('<script>');
const scriptBlock = SRC.slice(scriptStart, SRC.indexOf('</script>', scriptStart));
const markupBlock = SRC.slice(SRC.indexOf('</script>', scriptStart), SRC.indexOf('<style>'));

describe('InfoHint.svelte — hideButton / bindable open / anchor source audit', () => {
  it('hideButton prop defaults to false (backward-compatible for every existing caller)', () => {
    expect(scriptBlock).toMatch(/hideButton\s*=\s*false/);
  });

  it('open is declared bindable, not a plain $state', () => {
    expect(scriptBlock).toMatch(/open\s*=\s*\$bindable\(/);
    // Guard against a regression reintroducing the old plain-$state
    // declaration alongside the new bindable one.
    expect(scriptBlock).not.toMatch(/let open = \$state\(/);
  });

  it('anchor prop exists, defaulting to undefined (ignored unless hideButton is used)', () => {
    expect(scriptBlock).toMatch(/anchor\s*=\s*undefined/);
  });

  it('the click-outside listener exempts clicks inside `anchor`', () => {
    expect(scriptBlock).toMatch(/anchor\s*&&\s*anchor\.contains/);
  });

  it('the popup-position fit() anchors to `anchor ?? wrap`, not wrap alone', () => {
    expect(scriptBlock).toMatch(/\(anchor\s*\?\?\s*wrap\)\.getBoundingClientRect\(\)/);
  });

  it('the info-btn button render is now conditional on !hideButton', () => {
    expect(markupBlock).toMatch(/\{#if !hideButton\}/);
    expect(markupBlock).toMatch(/class="info-btn"/);
  });
});

describe('InfoHint.svelte — hover-preview + click-to-pin (2026-10)', () => {
  it('visible is derived from open OR the local hover-preview flag', () => {
    expect(scriptBlock).toMatch(/const\s+visible\s*=\s*\$derived\(open\s*\|\|\s*_hoverPreview\);/);
  });

  it('_hoverPreview is local $state, not part of the module-level singleton', () => {
    expect(scriptBlock).toMatch(/let\s+_hoverPreview\s*=\s*\$state\(false\);/);
    // The module block (the actual singleton) must not declare it.
    const moduleBlock = SRC.slice(0, scriptStart);
    expect(moduleBlock).not.toMatch(/_hoverPreview/);
  });

  it('the singleton claim effect gates on `open` (pinned), not `visible`', () => {
    expect(scriptBlock).toMatch(/if\s*\(open\s*&&\s*_uid\)\s*_activeInfoHintId\s*=\s*_uid;/);
    // Guard against regressing back to the old `visible`-gated claim,
    // which would let a mere hover preview evict another instance's
    // pinned popover.
    expect(scriptBlock).not.toMatch(/if\s*\(visible\s*&&\s*_uid\)/);
  });

  it('pointerenter sets the preview only for non-touch pointers', () => {
    expect(scriptBlock).toMatch(/function _onHoverEnter\(/);
    expect(scriptBlock).toMatch(/e\.pointerType\s*===\s*'touch'/);
    expect(scriptBlock).toMatch(/_hoverPreview\s*=\s*true;/);
  });

  it('pointerleave unconditionally clears the preview', () => {
    expect(scriptBlock).toMatch(/function _onHoverLeave\(\)\s*\{[\s\S]*?_hoverPreview\s*=\s*false;/);
  });

  it('a close transition (open true→false) clears the preview and arms the 350ms re-hover suppression, matching OptionsPayoff.svelte\'s own constant', () => {
    expect(scriptBlock).toMatch(/_prevOpen\s*&&\s*!isOpen/);
    expect(scriptBlock).toMatch(/_hoverSuppressUntil\s*=\s*Date\.now\(\)\s*\+\s*350;/);
  });

  it('the hover-preview entry guard checks the suppression window', () => {
    expect(scriptBlock).toMatch(/Date\.now\(\)\s*<\s*_hoverSuppressUntil/);
  });

  it('the default chip button is wired with onpointerenter/onpointerleave alongside its click toggle', () => {
    expect(markupBlock).toMatch(/onpointerenter=\{_onHoverEnter\}/);
    expect(markupBlock).toMatch(/onpointerleave=\{_onHoverLeave\}/);
    expect(markupBlock).toMatch(/onclick=\{\(\)\s*=>\s*\{\s*open\s*=\s*!open;\s*\}\}/);
  });

  it('hideButton mode wires the same hover listeners onto the external `anchor` via an $effect', () => {
    expect(scriptBlock).toMatch(/if\s*\(!hideButton\s*\|\|\s*!anchor\)\s*return;/);
    expect(scriptBlock).toMatch(/anchor\.addEventListener\('pointerenter',\s*_onHoverEnter\)/);
    expect(scriptBlock).toMatch(/anchor\.addEventListener\('pointerleave',\s*_onHoverLeave\)/);
  });

  it('Escape dismisses a pinned popup (new — previously no Esc handling existed at all)', () => {
    expect(scriptBlock).toMatch(/if\s*\(!open\)\s*return;[\s\S]*?e\.key\s*===\s*'Escape'[\s\S]*?open\s*=\s*false;/);
  });

  it('the hover-preview entry is gated on popup mode (no layout-shifting hover in default inline mode)', () => {
    expect(scriptBlock).toMatch(/function _onHoverEnter[\s\S]*?if\s*\(!popup\)\s*return;/);
  });
});
