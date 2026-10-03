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

  // Guards for the two Bug 1 / Bug 2 fixes (2026-10):
  //   Bug 1 — a hideButton+anchor site's `open` prop is owned by the
  //   parent page; the parent has no way to clear InfoHint's own internal
  //   `hovered` state, so a second click while the mouse still rests on
  //   the trigger used to leave the popup stuck open (`visible = open ||
  //   hovered` never both false). Fixed by an `$effect` that clears
  //   `hovered` whenever `open` transitions to false.
  //   Bug 2 — additive `hoverPreview` prop (default true, backward-
  //   compatible for every existing caller) lets a specific hideButton+
  //   anchor site opt out of hover-triggering entirely (click-only),
  //   for sites whose popup would otherwise flicker open on the way to
  //   a denser row of child InfoHint anchors just below it.
  it('hoverPreview prop defaults to true (backward-compatible for every existing hideButton+anchor caller)', () => {
    expect(scriptBlock).toMatch(/hoverPreview\s*=\s*true/);
  });

  it('the hideButton hover-wiring effect is gated on hoverPreview', () => {
    expect(scriptBlock).toMatch(/!hideButton\s*\|\|\s*!anchor\s*\|\|\s*!hoverPreview/);
  });

  it('an $effect clears `hovered` whenever `open` becomes false (Bug 1 fix)', () => {
    expect(scriptBlock).toMatch(/if\s*\(!open\)\s*hovered\s*=\s*false;/);
  });
});
