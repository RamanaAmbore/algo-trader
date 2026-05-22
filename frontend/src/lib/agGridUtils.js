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
 * Stretch the grid's visible columns to fill the available container
 * width. Locks each column's current actualWidth as its minWidth so
 * sizeColumnsToFit never shrinks below operator-set widths.
 *
 * @param {import('ag-grid-community').GridApi | null | undefined} api
 */
export function fitGridColumns(api) {
  if (!api || api.isDestroyed?.()) return;
  try {
    api.getColumns?.()?.forEach((col) => {
      const w = col.getActualWidth();
      const def = col.getColDef();
      if (w && (!def.minWidth || def.minWidth < w)) {
        def.minWidth = w;
      }
    });
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
 *   1. Run once now (the caller passes the api from onGridReady).
 *   2. Re-run on every window resize until the returned teardown
 *      callback fires.
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
