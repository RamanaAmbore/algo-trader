/**
 * orderCardOriginChip.test.js
 *
 * Sprint 2b (docs/proposals/SPRINT2_LAYER_INTEGRATION.md §3/§4.1) — the
 * new "Origin" chip on OrderCard.svelte. Deliberately labeled "Origin"
 * in the UI, not "source": UnifiedLogRow.source already means something
 * different (which table a merged log row came from) in the sibling
 * UnifiedLog.svelte surface, so reusing the word "source" as a visible
 * label here would read as the same concept when it isn't.
 *
 * OrderCard.svelte has no existing component-mount test harness in this
 * repo (no @testing-library/svelte, vitest runs in the `node`
 * environment per vitest.config.js) — same constraint as
 * InfoHint.sourceAudit.test.js, which this file follows the pattern of.
 *
 * Five quality dimensions per feedback_test_dimensions.md:
 *  1. SSOT   — one chip, same `.log-chip` family as every other chip on
 *              this row (tag/mode/engine) — not a new visual pattern
 *  2. Perf   — pure text/regex, no DOM, no network
 *  3. Stale  — explicitly asserts the UI-visible text is "origin:", not
 *              "source:" — guards against the exact naming regression
 *              §3 was written to prevent
 *  4. Reuse  — asserts the chip reuses the existing `log-chip` /
 *              `log-chip-key` classes rather than inventing new ones
 *  5. UX     — asserts the chip is conditional on `order.source` being
 *              present, so legacy rows (source=null) render nothing,
 *              not an empty/placeholder chip
 */

import { describe, it, expect } from 'vitest';
import SRC from '../order/OrderCard.svelte?raw';

const markupStart = SRC.indexOf('</script>');
const markupEnd   = SRC.indexOf('<style>');
const markup = SRC.slice(markupStart, markupEnd);

describe('OrderCard.svelte — Origin chip (Sprint 2b)', () => {
  it('renders an "Origin" chip gated on order.source being present', () => {
    expect(markup).toMatch(/\{#if order\.source\}/);
  });

  it('labels the chip "origin:" — not "source:" — per the §3 naming decision', () => {
    // Find the chip block immediately following the {#if order.source} guard.
    const idx = markup.indexOf('{#if order.source}');
    expect(idx).toBeGreaterThan(-1);
    const chipBlock = markup.slice(idx, idx + 200);
    expect(chipBlock).toMatch(/log-chip-key">origin:</);
    expect(chipBlock).not.toMatch(/log-chip-key">source:</);
  });

  it('reuses the existing log-chip / log-chip-key classes (no new chip style)', () => {
    const idx = markup.indexOf('{#if order.source}');
    const chipBlock = markup.slice(idx, idx + 200);
    expect(chipBlock).toMatch(/class="log-chip"/);
  });

  it('is placed alongside the existing mode/engine chips, not a separate row', () => {
    const modeIdx   = markup.indexOf('{#if order.mode}');
    const engineIdx = markup.indexOf('{#if order.engine}');
    const originIdx = markup.indexOf('{#if order.source}');
    expect(modeIdx).toBeGreaterThan(-1);
    expect(engineIdx).toBeGreaterThan(modeIdx);
    expect(originIdx).toBeGreaterThan(engineIdx);
    // No chip-row-closing </div> between engine and origin — same flex row.
    const between = markup.slice(engineIdx, originIdx);
    expect(between).not.toMatch(/<\/div>/);
  });
});
