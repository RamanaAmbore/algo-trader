/**
 * optionsPayoffStatInfoHint.test.js
 *
 * OptionsPayoff.svelte's top-left stat overlay (LTP/CHG%/CLOSE/
 * DAY P&L/P&L/ADJ/Exp P&L/DTE/σ) used bare `title=` attributes on
 * each `.ps-row` even though the file already imports InfoHint
 * elsewhere. Fix adds an `<InfoHint popup>` chip inside each row's
 * `.ps-k` label span, carrying the same wording, and removes the old
 * `title=` attribute (removing it prevents a double-tooltip: native
 * title + InfoHint popover stacked on the same hover target).
 *
 * The companion Playwright spec
 * (`e2e/options_payoff_stat_infohints.spec.js`) exercises the live
 * open/close + `[role="tooltip"]` contract against a real position,
 * but this source audit is the durable, always-green guard —
 * independent of whatever F&O book happens to be open when CI runs.
 * LEGS (line ~1013) is deliberately NOT converted (out of scope —
 * the task's named rows are LTP/CHG%/P.Close/Day/Adj/DTE/IV, plus
 * P&L and Exp P&L for overlay consistency).
 */

import { describe, it, expect } from 'vitest';
import SRC from '../OptionsPayoff.svelte?raw';

const scriptBlock = SRC.slice(SRC.indexOf('<script>'), SRC.indexOf('</script>'));
const markupBlock = SRC.slice(SRC.indexOf('</script>'), SRC.lastIndexOf('<style>'));

describe('OptionsPayoff.svelte — stat overlay rows wire InfoHint instead of title=', () => {
  it('imports InfoHint', () => {
    expect(scriptBlock).toMatch(/import InfoHint from '\$lib\/InfoHint\.svelte';/);
  });

  it('the .payoff-stats overlay block has no .ps-row carrying a title= attribute, except the untouched LEGS row', () => {
    const overlayStart = markupBlock.indexOf('<div class="payoff-stats">');
    const overlayEnd = markupBlock.indexOf('</div>\n\n', overlayStart); // closes payoff-stats
    const overlay = markupBlock.slice(overlayStart, overlayEnd === -1 ? undefined : overlayEnd);
    const titleRows = overlay.match(/<div class="ps-row"[^>]*title=/g) || [];
    // Only the LEGS row (deliberately out of scope) may still carry title=.
    expect(titleRows.length).toBeLessThanOrEqual(1);
    if (titleRows.length === 1) {
      const legsIdx = overlay.indexOf('Number of legs in the strategy basket');
      expect(legsIdx).toBeGreaterThan(-1);
    }
  });

  it('LTP, CHG%, CLOSE, DAY P&L, P&L, ADJ, Exp P&L, DTE, and IV rows each have an InfoHint popup (hideButton mode)', () => {
    const labels = ['LTP', 'CHG%', 'CLOSE', 'DAY P&amp;L', 'P&amp;L', 'ADJ', 'Exp P&amp;L', 'DTE'];
    for (const label of labels) {
      // In hideButton mode, the .ps-k span has additional attributes (bind:this, role, tabindex, aria-expanded, onclick, onkeydown)
      // spread across multiple lines. Look for class="ps-k" followed by the closing > and label text, then InfoHint hideButton.
      // Increased char limit to 400 to account for the multiline attributes.
      const pattern = new RegExp(`class="ps-k"[\\s\\S]{0,400}>${label}[\\s\\S]{0,500}<InfoHint[\\s\\S]{0,300}hideButton`);
      expect(markupBlock, `label "${label}" not found with hideButton InfoHint pattern`).toMatch(pattern);
    }
    // IV row label is "σ" in a .ps-k span with InfoHint hideButton.
    const ivPattern = /class="ps-k"[\s\S]{0,400}>σ[\s\S]{0,500}<InfoHint[\s\S]{0,300}hideButton/;
    expect(markupBlock).toMatch(ivPattern);
  });

  it('InfoHint text props escape & as &amp; (payload goes through {@html})', () => {
    // Every literal "P&L" inside an InfoHint text prop must be escaped;
    // a raw "P&L" fed to {@html} would render fine in most browsers but
    // is invalid HTML and inconsistent with every other InfoHint call
    // site in the codebase (Greeks strip, etc.).
    const infoHintBlocks = markupBlock.match(/<InfoHint popup[\s\S]*?\/>/g) || [];
    expect(infoHintBlocks.length).toBeGreaterThanOrEqual(8);
    for (const block of infoHintBlocks) {
      expect(block).not.toMatch(/P&L(?!amp;)/); // "P&L" not followed by "amp;" would mean unescaped
    }
  });
});
