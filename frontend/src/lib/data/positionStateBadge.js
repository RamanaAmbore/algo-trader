/**
 * positionStateBadge.js — shared position-state badge logic for every
 * grid that shows the "St" / state-track column: the derivatives Legs
 * grid (CandidateLegRow.svelte, hand-rolled) and MarketPulse's Positions
 * bucket (pulseColumns.js, ag-Grid). PerformancePage.svelte carries its
 * own independent copy of this same priority chain (not converted here —
 * out of scope per the operator's scope-discipline rule; flagged in the
 * accompanying report).
 *
 * Priority order (highest wins): GTT > pinned > auto-paired > orphan.
 * A row with none of `has_gtt` / `pin_group_key` / `pair_group_key` /
 * `is_orphan` set returns state `null` — callers should render nothing
 * (no badge, no button) for e.g. watchlist/mover/holding rows that were
 * never part of the positions book.
 *
 * All colors reference the shared `app.css` algo-palette tokens (never
 * hardcoded hex/rgba) so the badge matches BUY/SELL/info/action
 * conventions documented in CLAUDE.md.
 *
 * @typedef {{
 *   state: 'gtt'|'pinned'|'paired'|'orphan'|null,
 *   glyph: string,
 *   color: string,
 *   bg: string,
 *   border: string|null,
 *   tooltip: string,
 * }} PositionStateBadge
 */

/**
 * Orphan glyph — deliberately NOT '○' (reads as the letter "O" at a
 * glance, and on some renderers/fonts carries an emoji-presentation
 * variant). U+25C7 WHITE DIAMOND + an explicit U+FE0E text-presentation
 * selector forces plain-glyph rendering even if a future font ships an
 * emoji-style variant for this codepoint — defensive, not because this
 * specific codepoint is currently known to have one.
 */
export const ORPHAN_GLYPH = '◇︎';

/**
 * rawPinId(pinGroupKey) — recovers the bare `position_pairs` table row id
 * from a DISPLAY-only `pin_group_key` string (backend's
 * `_apply_pin_overlay()` formats it as `f"PIN{id}"` — see
 * backend/api/routes/positions.py). The `DELETE /api/positions/pin/{id}`
 * route wants that bare id, not the formatted display string; callers
 * (PositionPinPicker.svelte) must pass this through before calling
 * `unpinPosition()`. Returns the input unchanged (stringified) if it
 * doesn't carry the expected "PIN" prefix — defensive, not a silent
 * failure, since `unpinPosition` will just get a 404/422 from a
 * malformed id either way.
 *
 * @param {string|number|null|undefined} pinGroupKey
 * @returns {string}
 */
export function rawPinId(pinGroupKey) {
  return String(pinGroupKey ?? '').replace(/^PIN/, '');
}

/** No state — row isn't part of the positions book (watchlist/mover/holding/total). */
const _NONE = Object.freeze({
  state: null, glyph: '', color: '', bg: '', border: null, tooltip: '',
});

/**
 * @param {{
 *   has_gtt?: boolean|null,
 *   pin_group_key?: string|null,
 *   pair_group_key?: string|null,
 *   is_orphan?: boolean|null,
 *   _isTotal?: boolean,
 * } | null | undefined} row
 * @returns {PositionStateBadge}
 */
export function positionStateBadge(row) {
  if (!row || row._isTotal) return _NONE;

  if (row.has_gtt) {
    return {
      state: 'gtt',
      glyph: 'GTT',
      color: 'var(--algo-green)',
      bg: 'var(--algo-green-badge)',
      border: null,
      tooltip: 'Protected by a GTT stop-loss / target order.',
    };
  }

  if (row.pin_group_key) {
    // Pinned (operator-set, manual) — same cyan family as auto-paired but
    // with a solid border so it's visually distinguishable from an
    // auto-detected pair even in a computed-style check, not just by
    // hover title text. Won't reshuffle on the next poll the way an
    // auto-pair's lot-waterfall match can.
    return {
      state: 'pinned',
      glyph: String(row.pin_group_key),
      color: 'var(--algo-cyan-text)',
      bg: 'var(--algo-cyan-bg-strong)',
      border: '1px solid var(--algo-cyan-border)',
      tooltip: `Pinned pair ${row.pin_group_key} — manually linked, won't change on next poll. Click to unpin.`,
    };
  }

  if (row.pair_group_key) {
    return {
      state: 'paired',
      glyph: String(row.pair_group_key),
      color: 'var(--algo-cyan-text)',
      bg: 'var(--algo-cyan-bg)',
      border: null,
      tooltip: `Auto-paired ${row.pair_group_key} — detected automatically, may change on next poll. Click to pin.`,
    };
  }

  if (row.is_orphan) {
    return {
      state: 'orphan',
      glyph: ORPHAN_GLYPH,
      color: 'var(--algo-amber)',
      bg: 'var(--algo-amber-bg)',
      border: null,
      tooltip: 'Unpaired position — no GTT, no pair. Click to pair with another leg.',
    };
  }

  return _NONE;
}
