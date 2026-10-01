/**
 * public_performance_subtle_cell_borders.spec.js
 *
 * Operator: "make cell borders a little subtle in grids in performance
 * public page." Scope: `.ag-theme-ramboq` only — the theme class exclusively
 * mounted by `(public)/performance/+page.svelte`'s `<PerformancePage>`
 * (default `theme` prop). No other route uses this class, so this is a
 * single-page change (verified via grep for `<PerformancePage` and
 * `ag-theme-ramboq` across frontend/src).
 *
 * Two border rules stepped subtler, both in app.css:
 *  1. `.ag-theme-ramboq .ag-cell` vertical hairline: 0.12 -> 0.08 alpha
 *     navy (rgba(29,41,57,...)).
 *  2. `.ag-theme-ramboq .ag-row` horizontal row divider: previously had
 *     NO explicit border-bottom, so it fell through ag-Grid's structural
 *     CSS (--ag-row-border-color -> --ag-secondary-border-color ->
 *     --ag-border-color: #c4bba8, OPAQUE) — a much heavier line than the
 *     vertical hairline. Added an explicit 0.08-alpha override so both
 *     dividers read as one consistent "whisper grid".
 *
 * Deliberately untouched (confirmed via source read, not re-asserted
 * here since they're covered by existing specs):
 *  - totals-row border-top/-bottom (0.35 alpha, amber stratum) —
 *    higher-specificity selector, unaffected by this change
 *    (see total_row_muted_colors_no_border.spec.js)
 *  - header-cell divider (rgba(232,217,168,0.30))
 *  - ltp-vs-prev-* direction stripes, lots-left-sep, chg-right-sep
 *  - `--ag-border-color` itself (feeds wrapper + header border; left
 *    opaque on purpose)
 *
 * Source-pattern (regex-on-file-content) check — low risk, no browser
 * needed, follows the established pattern in alignment_overflow_audit.spec.js.
 *
 * Run:
 *   cd frontend && npx playwright test e2e/public_performance_subtle_cell_borders.spec.js
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const readFile = (relPath) => {
  const abs = path.resolve(__dirname, '..', relPath);
  return readFileSync(abs, 'utf-8');
};

/** Extract the FIRST `.selector { ... }` rule body, or null if absent. */
function ruleBody(css, selector) {
  const re = new RegExp(
    selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*\\{([^}]*)\\}'
  );
  const m = css.match(re);
  return m ? m[1] : null;
}

test.describe('Static source checks — app.css .ag-theme-ramboq cell borders', () => {
  const content = readFile('src/app.css');

  test('vertical cell hairline stepped subtler (0.12 -> 0.08)', () => {
    const body = ruleBody(content, '.ag-theme-ramboq .ag-cell');
    expect(body).toBeTruthy();
    expect(body).toContain('border-right: 1px solid rgba(29,41,57,0.08)');
    expect(body).not.toContain('rgba(29,41,57,0.12)');
  });

  test('row divider explicitly set to the same 0.08 alpha, not left to fall through to the opaque --ag-border-color', () => {
    const body = ruleBody(content, '.ag-theme-ramboq .ag-row');
    expect(body).toBeTruthy();
    expect(body).toContain('border-bottom: 1px solid rgba(29,41,57,0.08)');
  });

  test('totals-row stratum border (0.35 alpha) left untouched', () => {
    const body = ruleBody(content, '.ag-theme-ramboq .ag-row.totals-row');
    expect(body).toBeTruthy();
    expect(body).toContain('rgba(29,41,57,0.35)');
  });

  test('--ag-border-color (wrapper/header border) left untouched', () => {
    const body = ruleBody(content, '.ag-theme-ramboq');
    expect(body).toBeTruthy();
    expect(body).toContain('--ag-border-color: #c4bba8');
  });
});
