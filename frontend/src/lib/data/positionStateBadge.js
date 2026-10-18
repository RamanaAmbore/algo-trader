/**
 * positionStateBadge.js — shared position-state badge logic for every
 * grid that shows the "St" / state-track column: the derivatives Legs
 * grid (CandidateLegRow.svelte, hand-rolled) and MarketPulse's Positions
 * bucket (pulseColumns.js, ag-Grid). PerformancePage.svelte carries its
 * own independent copy of this same priority chain (not converted here —
 * out of scope per the operator's scope-discipline rule; flagged in the
 * accompanying report).
 *
 * Priority order (highest wins): GTT > auto-paired > orphan.
 * A row with none of `has_gtt` / `pair_group_key` / `is_orphan` set
 * returns state `null` — callers should render nothing (no badge, no
 * button) for e.g. watchlist/mover/holding rows that were never part of
 * the positions book.
 *
 * PerformancePage.svelte's St column (`positionsCols`) also calls this
 * directly (converted 2026-10-18 — it previously carried its own
 * independent copy of this priority chain, now removed).
 *
 * `pair_group_key` carries TWO distinct auto-detected concepts under one
 * field, distinguished by prefix: "P{n}" = an opposite-sign spread within
 * the same (account, root, option_type) — `_auto_pair_positions()`.
 * "S{n}" = a same-side CE+PE hedge/straddle/strangle (e.g. sell CE + sell
 * PE) — `_auto_pair_same_side_hedges()`, which only ever touches rows the
 * P-pass left as pure orphans. Both share state `'paired'` and the same
 * glyph/color; only the tooltip wording differs (see below).
 *
 * All colors reference the shared `app.css` algo-palette tokens (never
 * hardcoded hex/rgba) so the badge matches BUY/SELL/info/action
 * conventions documented in CLAUDE.md.
 *
 * @typedef {{
 *   state: 'gtt'|'paired'|'orphan'|'anchor'|null,
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

/** Underlying-anchor glyph — "X" (2026-10-18, operator: "make st as x,
 *  with similar coding like o"). Same badge mechanism as the orphan "O"
 *  state (button + inline bg/color via cellStyle), distinct muted/gray
 *  color so it never reads as a real position state. */
export const ANCHOR_GLYPH = 'X';

/** No state — row isn't part of the positions book (watchlist/mover/holding/total). */
const _NONE = Object.freeze({
  state: null, glyph: '', color: '', bg: '', border: null, tooltip: '',
});

/**
 * @param {{
 *   has_gtt?: boolean|null,
 *   pair_group_key?: string|null,
 *   is_orphan?: boolean|null,
 *   _isTotal?: boolean,
 *   _isUnderlyingAnchor?: boolean,
 * } | null | undefined} row
 * @returns {PositionStateBadge}
 */
export function positionStateBadge(row) {
  if (!row || row._isTotal) return _NONE;

  // Underlying-anchor row (mergeUnderlyingAnchors / the derivatives Legs
  // grid's own anchor row) — not a real position, so it structurally
  // never carries has_gtt/pair_group_key/is_orphan, but checked first
  // regardless since "this isn't a real position at all" outranks any of
  // those. Same badge mechanism as the other three states; a distinct
  // muted/gray color so it never reads as a real position state.
  if (row._isUnderlyingAnchor) {
    return {
      state: 'anchor',
      glyph: ANCHOR_GLYPH,
      color: 'var(--algo-muted)',
      bg: 'var(--algo-muted-bg)',
      border: null,
      tooltip: 'Underlying reference — not a held position.',
    };
  }

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

  if (row.pair_group_key) {
    // "S" groups are a same-side CE+PE hedge (a straddle/strangle — e.g.
    // sell CE + sell PE), detected by `_auto_pair_same_side_hedges()`
    // (backend/api/routes/positions.py) — a different economic
    // relationship from a "P" group (an opposite-sign spread within the
    // same option_type). Distinct tooltip wording per the operator's own
    // "how do you differentiate two types of hedges" question; same
    // glyph/color/state otherwise — both are auto-detected and may
    // change on the next poll.
    const isHedge = String(row.pair_group_key).startsWith('S');
    return {
      state: 'paired',
      glyph: String(row.pair_group_key),
      color: 'var(--algo-cyan-text)',
      bg: 'var(--algo-cyan-bg)',
      border: null,
      tooltip: isHedge
        ? `Same-side hedge ${row.pair_group_key} — detected automatically, may change on next poll.`
        : `Auto-paired ${row.pair_group_key} — detected automatically, may change on next poll.`,
    };
  }

  if (row.is_orphan) {
    return {
      state: 'orphan',
      glyph: ORPHAN_GLYPH,
      color: 'var(--algo-amber)',
      bg: 'var(--algo-amber-bg)',
      border: null,
      tooltip: 'Unpaired position — no GTT, no auto-detected pair.',
    };
  }

  return _NONE;
}
