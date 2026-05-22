/**
 * ag-Grid shared utilities.
 *
 * The platform-wide standard is that every grid uses the full
 * horizontal space the container offers — when the total column
 * width is less than the viewport, the columns stretch to fill the
 * gap instead of leaving blank space at the right edge.
 *
 * Implementation notes:
 *   • `sizeColumnsToFit()` stretches columns proportionally to fill
 *     the container, respecting per-column `minWidth` / `maxWidth`.
 *   • Before calling it we LOCK each column's current `actualWidth`
 *     as its `minWidth`, so the call only ever EXPANDS — never
 *     compresses operator-set widths. Without the lock, narrow fixed
 *     columns (account-code, qty, etc.) get redistributed proportional
 *     to their current width and look balloon-y.
 *   • We re-run on viewport resize so columns re-flow when the user
 *     resizes the window or rotates the device.
 *
 * Industry analogue: IB TWS / NinjaTrader auto-fit grid columns when
 * the workspace docks. ag-Grid's `sizeColumnsToFit` is the official
 * helper for the same behaviour.
 */

/**
 * Pre-process column defs so each column's declared `width` is mirrored
 * into its `minWidth`. This is the project-wide standard: operator-set
 * column widths are the FLOOR, never the cap. `sizeColumnsToFit` can
 * stretch columns above this floor when the container has spare
 * width, but ag-Grid honours minWidth as the lower bound on every
 * subsequent shrink (e.g. a viewport resize from desktop to mobile).
 *
 * Earlier we tried locking minWidth at runtime from `actualWidth` on
 * grid-ready — but `defaultColDef.flex: 1` made columns shrink to
 * `defaultColDef.minWidth` on the first fit (before our lock ran), and
 * mobile-first sessions ended up with columns at 55 px regardless of
 * their declared width. Pre-processing the column defs at construction
 * time bypasses that race.
 *
 * @template {{ width?: number, minWidth?: number }} ColDef
 * @param {ColDef[]} defs
 * @returns {ColDef[]}
 */
export function enrichColDefs(defs) {
  return (defs || []).map((c) => ({
    ...c,
    minWidth: c.minWidth ?? c.width,
  }));
}

/**
 * Lock each visible column's CURRENT actualWidth as its `minWidth`.
 *
 * Intent: capture the operator's DECLARED widths (the values set in
 * the column definitions) as a permanent floor BEFORE the first
 * `sizeColumnsToFit` call runs. Once we lock these declared widths,
 * subsequent calls to `sizeColumnsToFit` will stretch the columns
 * upward when the container is wider than the sum, but never shrink
 * them below the floors — and equally importantly, won't widen them
 * past the operator's intent on a subsequent narrower-viewport fit.
 *
 * Must be called ONCE on `onGridReady` BEFORE any fit. Calling it
 * again after a fit would lock the stretched (post-fit) widths,
 * which is exactly the regression we're avoiding.
 *
 * @param {import('ag-grid-community').GridApi | null | undefined} api
 */
function lockDeclaredWidths(api) {
  if (!api || api.isDestroyed?.()) return;
  try {
    api.getColumns?.()?.forEach((col) => {
      const w = col.getActualWidth();
      const def = col.getColDef();
      if (w && (!def.minWidth || def.minWidth < w)) {
        def.minWidth = w;
      }
    });
  } catch (e) {
    if (typeof console !== 'undefined') {
      console.debug('[agGridUtils] lockDeclaredWidths skipped:', e?.message);
    }
  }
}

/**
 * Stretch the grid's columns to fill the available container width.
 *
 * Pure call to `sizeColumnsToFit` — does NOT touch `minWidth`. Earlier
 * versions locked `actualWidth` as `minWidth` on every call, which
 * caused desktop-first sessions to permanently lock columns at
 * desktop-fitted widths and break the mobile re-fit when the
 * viewport shrank. Now the floors are captured ONCE via
 * `lockDeclaredWidths` and every subsequent fit respects them.
 *
 * @param {import('ag-grid-community').GridApi | null | undefined} api
 */
export function fitGridColumns(api) {
  if (!api || api.isDestroyed?.()) return;
  try {
    api.sizeColumnsToFit();
  } catch (e) {
    // ag-Grid occasionally throws if called during destroy or
    // before the first render frame settled — non-fatal, just skip.
    if (typeof console !== 'undefined') {
      console.debug('[agGridUtils] fitGridColumns skipped:', e?.message);
    }
  }
}

/**
 * Wire fit-to-fill on a freshly created grid:
 *   1. Lock each column's declared width as `minWidth` (one-time).
 *   2. Run an initial `sizeColumnsToFit`.
 *   3. Re-run `sizeColumnsToFit` on every viewport resize until the
 *      returned teardown callback fires (collected in onDestroy).
 *
 * Usage:
 *   const grid = createGrid(el, {
 *     ...,
 *     onGridReady: ({ api }) => { fitDetach = attachGridFit(api); },
 *     onFirstDataRendered: ({ api }) => fitGridColumns(api),
 *   });
 *   onDestroy(() => fitDetach?.());
 *
 * @param {import('ag-grid-community').GridApi} api
 * @returns {() => void} teardown callback
 */
export function attachGridFit(api) {
  lockDeclaredWidths(api);
  fitGridColumns(api);
  const onResize = () => fitGridColumns(api);
  if (typeof window !== 'undefined') {
    window.addEventListener('resize', onResize);
  }
  return () => {
    if (typeof window !== 'undefined') {
      window.removeEventListener('resize', onResize);
    }
  };
}
