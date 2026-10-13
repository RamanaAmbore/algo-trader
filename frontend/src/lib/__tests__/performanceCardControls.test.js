/**
 * performanceCardControls.test.js
 *
 * PerformancePage.svelte's four grid cards (Positions/Holdings ×
 * Summary/Breakdown) used a hand-rolled `.perf-grid-headrow` (title +
 * spacer + a bare GridDownloadButton, or GridSearchButton+
 * GridDownloadButton for Breakdown) instead of the canonical
 * CardHeader/CardControls cluster that MarketPulse, /dashboard, and
 * /admin/derivatives already use — missing Search + Fullscreen on the
 * Summary cards, and missing Fullscreen on the Breakdown cards.
 *
 * `(public)/performance/+page.svelte` (the only current mount point)
 * pins `showGridControls={false}`, so a live Playwright page cannot
 * observe the POSITIVE path (cluster actually rendering) — this
 * source-audit test is the vitest-level guard that the wiring itself
 * is correct: all four cards import CardHeader, bind the state
 * CardHeader needs (isFullscreen/filter), and pass
 * `showControls={showGridControls}` rather than a hardcoded value
 * (which would either permanently hide the cluster everywhere, or —
 * worse — leak it onto the public page regardless of the prop).
 *
 * Collapse removed 2026-10-13 (header button-group standardization) —
 * `cardId`/`isCollapsed`/`class:is-collapsed` wiring was exclusively
 * for the now-deleted CollapseButton's localStorage persistence and
 * has been stripped from all four cards.
 */

import { describe, it, expect } from 'vitest';
import SRC from '../PerformancePage.svelte?raw';

const scriptBlock = SRC.slice(SRC.indexOf('<script>'), SRC.indexOf('</script>'));
const markupBlock = SRC.slice(SRC.indexOf('</script>'), SRC.indexOf('<style>'));

describe('PerformancePage.svelte — grid cards wire CardHeader, not the old hand-rolled headrow', () => {
  it('imports CardHeader and no longer imports the retired GridSearchButton/GridDownloadButton', () => {
    expect(scriptBlock).toMatch(/import CardHeader from '\$lib\/CardHeader\.svelte';/);
    expect(scriptBlock).not.toMatch(/import GridSearchButton/);
    expect(scriptBlock).not.toMatch(/import GridDownloadButton/);
  });

  it('declares independent fullscreen state for all four cards (no collapse state — removed)', () => {
    for (const name of [
      '_fsPositionsSummary', '_fsHoldingsSummary',
      '_fsPositionsDetail', '_fsHoldingsDetail',
    ]) {
      expect(scriptBlock).toMatch(new RegExp(`let ${name}\\s*=\\s*\\$state\\(false\\)`));
    }
    for (const name of [
      '_colPositionsSummary', '_colHoldingsSummary',
      '_colPositionsDetail', '_colHoldingsDetail',
    ]) {
      expect(scriptBlock).not.toMatch(new RegExp(`let ${name}\\s*=`));
    }
  });

  it('declares filter state for the two Summary cards (Breakdown cards reuse the pre-existing _filterPositions/_filterHoldings)', () => {
    expect(scriptBlock).toMatch(/let _filterPositionsSummary\s*=\s*\$state\(''\)/);
    expect(scriptBlock).toMatch(/let _filterHoldingsSummary\s*=\s*\$state\(''\)/);
  });

  it('wires quickFilterText effects for the two Summary grids', () => {
    expect(scriptBlock).toMatch(/positionsSummaryGrid\?\.setGridOption\('quickFilterText', v\)/);
    expect(scriptBlock).toMatch(/holdingsSummaryGrid\?\.setGridOption\('quickFilterText', v\)/);
  });

  it('every CardHeader instance for the four grid cards binds isFullscreen/filter and forwards showGridControls via showControls (not hardcoded), with no isCollapsed/cardId wiring', () => {
    // Match each <CardHeader ... /> block (self-closing, as used here)
    // and check each one individually so a single mis-wired card can't
    // hide behind an aggregate pass.
    const blocks = /** @type {string[]} */ (markupBlock.match(/<CardHeader[\s\S]*?\/>/g) || []);
    // Four grid cards use CardHeader this way in PerformancePage (the
    // Funds/NAV tab card and others, if any, use AlgoTabs instead —
    // scope this test to the four known labels, which replace the
    // retired cardId identifiers as the unique match key).
    const targetLabels = ['Positions Summary', 'Holdings Summary', 'Positions', 'Holdings'];
    const matched = blocks.filter((b) => targetLabels.some((label) => b.includes(`label="${label}"`)));
    expect(matched.length).toBe(4);
    for (const block of matched) {
      expect(block).toMatch(/bind:isFullscreen=\{/);
      expect(block).toMatch(/bind:filter=\{/);
      expect(block).toMatch(/showControls=\{showGridControls\}/);
      expect(block).toMatch(/onDownload=\{/);
      // Must NOT hardcode showControls to a literal — that would
      // either always show or always hide the cluster regardless of
      // the showGridControls prop callers pass in.
      expect(block).not.toMatch(/showControls=\{(true|false)\}/);
      // Collapse removal — no card re-wires isCollapsed or cardId.
      expect(block).not.toMatch(/bind:isCollapsed=\{/);
      expect(block).not.toMatch(/cardId=/);
    }
  });

  it('wraps each card with class:fs-card-on bound to that card\'s own state, with no class:is-collapsed left behind', () => {
    expect(markupBlock).toMatch(/class:fs-card-on=\{_fsPositionsSummary\}/);
    expect(markupBlock).toMatch(/class:fs-card-on=\{_fsHoldingsSummary\}/);
    expect(markupBlock).toMatch(/class:fs-card-on=\{_fsPositionsDetail\}/);
    expect(markupBlock).toMatch(/class:fs-card-on=\{_fsHoldingsDetail\}/);
    expect(markupBlock).not.toMatch(/class:is-collapsed=/);
  });

  it('the old hand-rolled .perf-grid-headrow markup/CSS is gone', () => {
    expect(SRC).not.toMatch(/perf-grid-headrow/);
  });
});
