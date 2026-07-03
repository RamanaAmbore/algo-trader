/**
 * total_row_opacity.spec.js
 *
 * Regression guard: TOTAL row backgrounds must be opaque so scrolled
 * rows cannot bleed through. We verify this by asserting that none of
 * the canonical TOTAL row selectors carry a bare transparent amber
 * rgba(251,191,36,0.22) as their background — they must use the layered
 * linear-gradient pattern over a solid opaque base.
 *
 * Dimensions: SSOT, stale-code, UX
 */

import { test, expect } from '@playwright/test';

// Grep source files for bare transparent amber backgrounds on TOTAL rows.
// This is a static assertion — no browser launch needed — but we use a
// Playwright test shell for consistent reporting.
import { readFileSync } from 'fs';
import { resolve } from 'path';

const SRC_ROOT = resolve(process.cwd(), 'src');

/**
 * Returns hits where a bare transparent amber background is used as the
 * TOTAL-row-level background (not a child-cell override). We track whether
 * the opening selector line targets the ROW itself (no descendant suffix
 * like .ag-cell, .ag-col-sym etc.) so child-cell tints inside the row
 * are correctly excluded — those layer over the now-opaque row bg and
 * do not cause bleed.
 */
function findBareAmberBg(filePath) {
  let src;
  try {
    src = readFileSync(filePath, 'utf8');
  } catch {
    return [];
  }
  const lines = src.split('\n');
  const hits = [];
  let insideRowSelector = false; // true only for the row-level rule
  let depth = 0;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    // Detect entry into a TOTAL-row ROW-level selector block.
    // A row-level selector ends with { or opens on the same line.
    // We exclude lines where the selector has a descendant suffix
    // (space followed by ., >, or another class after the TOTAL class).
    if (
      /totals-row|mp-total-row|cand-row-total|byund-row-total|nav-bd-total/.test(line) &&
      line.includes('{')
    ) {
      // Check if there's a descendant combinator AFTER the TOTAL class name
      const afterTotal = line.replace(
        /.*?(totals-row|mp-total-row|cand-row-total|byund-row-total|nav-bd-total)/,
        ''
      );
      const hasDescendant = /[\s>+~][\w.#\[*]/.test(afterTotal.split('{')[0]);
      if (!hasDescendant) {
        insideRowSelector = true;
        depth = 0;
      }
    }

    if (insideRowSelector) {
      depth += (line.match(/\{/g) || []).length;
      depth -= (line.match(/\}/g) || []).length;
      if (depth <= 0) {
        insideRowSelector = false;
        depth = 0;
      }

      // Flag if the line sets background to the bare transparent amber value
      // or variable aliases — must not be a linear-gradient layered pattern.
      if (
        /^\s*(background|background-color)\s*:/.test(line) &&
        /rgba\(251,\s*191,\s*36,\s*0\.2[012]\)|var\(--algo-amber-bg-strong\)|var\(--c-action-22\)/.test(line) &&
        !/linear-gradient/.test(line)
      ) {
        hits.push({ file: filePath, line: i + 1, text: line.trim() });
      }
    }
  }
  return hits;
}

const FILES_TO_CHECK = [
  resolve(SRC_ROOT, 'app.css'),
  resolve(SRC_ROOT, 'lib/MarketPulse.svelte'),
  resolve(SRC_ROOT, 'lib/NavBreakdown.svelte'),
  resolve(SRC_ROOT, 'routes/(algo)/admin/derivatives/+page.svelte'),
  resolve(SRC_ROOT, 'routes/(algo)/dashboard/+page.svelte'),
  resolve(SRC_ROOT, 'routes/(algo)/performance/+page.svelte'),
  resolve(SRC_ROOT, 'lib/PerformancePage.svelte'),
];

test.describe('TOTAL row opacity — no scroll bleed', () => {
  test('no bare transparent amber bg on any TOTAL row selector', () => {
    const allHits = FILES_TO_CHECK.flatMap(findBareAmberBg);

    if (allHits.length > 0) {
      const msg = allHits
        .map((h) => `  ${h.file}:${h.line}  ${h.text}`)
        .join('\n');
      // Fail with a detailed report
      expect(
        allHits,
        `Found bare transparent amber backgrounds on TOTAL rows — will bleed on scroll:\n${msg}\nFix: replace with linear-gradient(rgba(251,191,36,0.22),rgba(251,191,36,0.22)),#1d2a44`
      ).toHaveLength(0);
    }
  });

  test('canonical files contain the opaque layered background pattern', () => {
    // Positive assertion: each canonical TOTAL-row file must contain the
    // linear-gradient(rgba(251,191,36,0.22), rgba(251,191,36,0.22)), #1d2a44 pattern.
    const canonicalFiles = [
      resolve(SRC_ROOT, 'app.css'),
      resolve(SRC_ROOT, 'lib/MarketPulse.svelte'),
      resolve(SRC_ROOT, 'lib/NavBreakdown.svelte'),
      resolve(SRC_ROOT, 'routes/(algo)/admin/derivatives/+page.svelte'),
    ];
    for (const filePath of canonicalFiles) {
      const src = readFileSync(filePath, 'utf8');
      expect(
        src,
        `Expected opaque layered gradient pattern in ${filePath}`
      ).toContain('linear-gradient(rgba(251,191,36,0.22), rgba(251,191,36,0.22))');
    }
  });
});
