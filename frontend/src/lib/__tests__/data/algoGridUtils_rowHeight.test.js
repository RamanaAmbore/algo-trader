/**
 * algoGridUtils_rowHeight.test.js — Vitest unit tests for the mkBaseGridOpts
 * row-height regression (2026-09-27, commit 3e0543a8).
 *
 * Bug: mkBaseGridOpts() set `rowHeight: 26` (desktop) while the algo dark
 * theme's CSS custom property `--ag-row-height` (app.css line 1179) is
 * `28px` — ag-Grid's legacy-theme cell CSS derives `line-height` for
 * vertical text centering from that CSS var, not from the JS rowHeight
 * option, so the 26-vs-28 mismatch made every mkBaseGridOpts-built grid's
 * text render off-center (top-biased). Same defect class, same fix
 * pattern, as the earlier PerformancePage.svelte fix (df43e278) for the
 * public/cream theme.
 *
 * Fix: (1) mkBaseGridOpts's rowHeight corrected to 28/36 to match the CSS
 * var; (2) new syncGridRowHeightVar(gridEl) helper writes the CSS var
 * inline on the grid element right after createGrid(), so the two values
 * can never silently drift apart again, mirroring PerformancePage's own
 * inline-setProperty pattern.
 *
 * This file replaces an earlier, weaker Playwright spec
 * (frontend/e2e/ag_grid_algo_row_centering.spec.js) that re-implemented
 * the fix's logic INLINE inside the test via page.evaluate() instead of
 * importing and exercising the real exported functions — it would still
 * pass even if mkBaseGridOpts or syncGridRowHeightVar regressed, since it
 * never actually called them. This file imports and calls the real
 * functions directly. It also lives in the location this repo's own
 * CLAUDE.md test-location table specifies for frontend/src/lib/data/*.js
 * changes (Vitest, not a Playwright e2e spec).
 *
 * `_isMobile` is a module-scope constant computed once at import time from
 * `typeof window !== 'undefined' && window.innerWidth <= 720` — to
 * exercise both the desktop and mobile branches this file uses
 * `vi.resetModules()` + a fresh dynamic import per scenario, mocking
 * `globalThis.window.innerWidth` BEFORE that import runs.
 *
 * Five quality dimensions:
 *  1. SSOT   — imports and calls the REAL mkBaseGridOpts/syncGridRowHeightVar,
 *              not a reimplementation.
 *  2. Perf   — pure unit, no browser/network.
 *  3. Stale  — directly reproduces the reported defect shape (26 vs 28
 *              mismatch) and proves both halves of the fix.
 *  4. Reuse  — same vi.resetModules()+dynamic-import pattern is the
 *              standard way this codebase tests module-scope
 *              window-derived constants (see _isMobile's own definition
 *              comment).
 *  5. UX     — this is what actually determines whether operator-visible
 *              grid text renders centered or top-biased.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

const ORIGINAL_WINDOW = globalThis.window;

afterEach(() => {
  if (ORIGINAL_WINDOW === undefined) {
    delete globalThis.window;
  } else {
    globalThis.window = ORIGINAL_WINDOW;
  }
  vi.resetModules();
});

describe('mkBaseGridOpts / syncGridRowHeightVar — desktop (no window / innerWidth > 720)', () => {
  beforeEach(() => {
    delete globalThis.window;
    vi.resetModules();
  });

  it('mkBaseGridOpts sets rowHeight: 28 on desktop', async () => {
    const { mkBaseGridOpts } = await import('$lib/data/algoGridUtils.js');
    expect(mkBaseGridOpts().rowHeight).toBe(28);
  });

  it('syncGridRowHeightVar sets --ag-row-height to 28px on desktop, matching app.css .ag-theme-algo', async () => {
    const { syncGridRowHeightVar } = await import('$lib/data/algoGridUtils.js');
    let stored = null;
    const fakeEl = /** @type {any} */ ({ style: { setProperty: (k, v) => { if (k === '--ag-row-height') stored = v; } } });
    syncGridRowHeightVar(fakeEl);
    expect(stored).toBe('28px');
  });

  it('rowHeight and syncGridRowHeightVar never diverge — the actual regression class this fix closes', async () => {
    const { mkBaseGridOpts, syncGridRowHeightVar } = await import('$lib/data/algoGridUtils.js');
    let stored = null;
    const fakeEl = /** @type {any} */ ({ style: { setProperty: (k, v) => { if (k === '--ag-row-height') stored = v; } } });
    syncGridRowHeightVar(fakeEl);
    expect(stored).toBe(`${mkBaseGridOpts().rowHeight}px`);
  });
});

describe('mkBaseGridOpts / syncGridRowHeightVar — mobile (window.innerWidth <= 720)', () => {
  beforeEach(() => {
    globalThis.window = /** @type {any} */ ({ innerWidth: 400 });
    vi.resetModules();
  });

  it('mkBaseGridOpts sets rowHeight: 36 on mobile', async () => {
    const { mkBaseGridOpts } = await import('$lib/data/algoGridUtils.js');
    expect(mkBaseGridOpts().rowHeight).toBe(36);
  });

  it('syncGridRowHeightVar sets --ag-row-height to 36px on mobile', async () => {
    const { syncGridRowHeightVar } = await import('$lib/data/algoGridUtils.js');
    let stored = null;
    const fakeEl = /** @type {any} */ ({ style: { setProperty: (k, v) => { if (k === '--ag-row-height') stored = v; } } });
    syncGridRowHeightVar(fakeEl);
    expect(stored).toBe('36px');
  });
});
