/**
 * chaseDisplayGrace.js
 *
 * Pure helpers for ChaseCard.svelte's poll-result display decision —
 * extracted so Vitest can cover the logic without mounting the component
 * (matches the ChaseCard.inflight / orderTimelineLogic pure-function
 * test precedent; see those files' own header comments).
 *
 * Why this exists (2026-11-25 fix): `_load()` used to set the displayed
 * chase list directly from a single poll response with no grace period —
 * `_chases = (await fetchActiveChases()) || []`. The instant that result
 * was empty, two things happened immediately: (1) the component's own
 * template gate hid the whole card, and (2) the idle flag flipped and
 * throttled the NEXT poll from the normal 3s cadence down to ~15-30s.
 * So either a single transient empty poll, or a chase that genuinely
 * finishes fast (fills, exhausts its attempts, or goes HELD) and simply
 * falls out of the backend's OPEN/CANCEL_FAILED filter, made the chase
 * data vanish abruptly — and even a transient blip then took up to 10x
 * longer than normal to reappear, because of the idle throttle.
 *
 * `reducePoll()` holds the LAST non-empty chase list across ONE
 * consecutive empty poll before actually clearing it, giving a transient
 * blip a chance to self-correct on the very next (still-fast) poll. It
 * only ever delays clearing — a sustained empty run still clears, just
 * one poll later than before.
 *
 * `buildGhosts()` / `pruneGhosts()` are a frontend-only best-effort
 * enhancement: when a previously-tracked chase disappears for a REAL
 * reason (filled / rejected / cancelled / held — not merely "this poll
 * didn't mention it"), show a brief one-line terminal-outcome row instead
 * of a silent hard-disappearance. This deliberately does NOT reuse
 * `marketDataStores.svelte.js`'s shared `ordersStore` — that module's own
 * header comment excludes ChaseCard/HeldOrdersCard as "lower-traffic
 * surfaces" out of scope for the shared poll — so this calls
 * `fetchAlgoOrdersRecent()` directly, and only on a poll where something
 * actually disappeared (not on every tick).
 */

/** Number of consecutive EMPTY poll results required before a non-empty
 *  chase list is actually cleared to empty. 2 means: the first empty
 *  poll holds the prior list steady (grace); a SECOND consecutive empty
 *  poll confirms it and clears. Any non-empty poll resets the counter to 0. */
export const GRACE_THRESHOLD = 2;

/** How long a terminal-outcome ghost row stays visible before being
 *  pruned, in ms. Matches toastStore's `DEFAULT_TIMEOUT.success`/`.info`
 *  (3000ms) — see frontend/src/lib/data/toastStore.svelte.js. */
export const GHOST_DISPLAY_MS = 3000;

/** AlgoOrder statuses worth surfacing as a ghost row, with their display
 *  label and CSS modifier class. OPEN and CANCEL_FAILED are deliberately
 *  excluded — OPEN means it's still genuinely active (shouldn't have
 *  disappeared from `_chases` in the first place), and CANCEL_FAILED is
 *  already a real, persistently-shown row via the backend's own
 *  `list_active_chases` filter, not a "just finished" transition. An id
 *  resolving to either of those, or to nothing at all, yields NO ghost —
 *  callers fall back to plain disappearance in that case. */
const _GHOST_STATUS_INFO = {
  FILLED:    { label: 'Filled',    cls: 'filled' },
  REJECTED:  { label: 'Rejected',  cls: 'rejected' },
  CANCELLED: { label: 'Cancelled', cls: 'cancelled' },
  UNFILLED:  { label: 'Unfilled',  cls: 'rejected' },
  // "Pending" per CLAUDE.md's hold-label invariant — operator-facing hold
  // text always reads "Pending", never "Held" (see OrderCard.svelte's own
  // HELD badge and order_hold_gate.py's `record_held_order` prefix).
  HELD:      { label: 'Pending',   cls: 'pending' },
};

/**
 * Decide the next displayed chase list + grace counter from one poll's
 * raw result.
 *
 * - A non-empty `rawRows` always wins immediately: swap in directly,
 *   reset the counter to 0, and report anything that was in `prevChases`
 *   but isn't in `rawRows` anymore as `disappeared` — this case is NOT
 *   ambiguous (we have a confirmed, authoritative current list), so no
 *   grace is needed for individual rows dropping out of it.
 * - An EMPTY `rawRows` against an already-empty `prevChases` is a no-op
 *   (nothing to protect, counter resets to 0 — there was no streak to
 *   begin with).
 * - An EMPTY `rawRows` against a non-empty `prevChases` increments the
 *   counter. Below `GRACE_THRESHOLD`, the prior list is held steady
 *   (nothing disappears yet). At or above the threshold, the list is
 *   cleared for real and every previously-held row is reported as
 *   `disappeared`.
 *
 * @param {any[]} prevChases — the currently displayed chase rows.
 * @param {number} consecutiveEmpty — the grace counter going in.
 * @param {any[] | null | undefined} rawRows — this poll's raw fetch result.
 * @returns {{ chases: any[], consecutiveEmpty: number, disappeared: any[] }}
 */
export function reducePoll(prevChases, consecutiveEmpty, rawRows) {
  const prev = Array.isArray(prevChases) ? prevChases : [];
  const raw = Array.isArray(rawRows) ? rawRows : [];

  if (raw.length > 0) {
    const nextIds = new Set(raw.map((r) => r?.id));
    const disappeared = prev.filter((r) => !nextIds.has(r?.id));
    return { chases: raw, consecutiveEmpty: 0, disappeared };
  }

  if (prev.length === 0) {
    return { chases: prev, consecutiveEmpty: 0, disappeared: [] };
  }

  const nextCount = (consecutiveEmpty || 0) + 1;
  if (nextCount >= GRACE_THRESHOLD) {
    return { chases: [], consecutiveEmpty: nextCount, disappeared: [...prev] };
  }
  // Grace — hold the prior list steady for one more poll.
  return { chases: prev, consecutiveEmpty: nextCount, disappeared: [] };
}

/**
 * Build terminal-outcome ghost rows for chases that just disappeared,
 * by matching their `id` against a fresh `fetchAlgoOrdersRecent()` read.
 * Returns [] (no ghosts) when nothing disappeared, nothing matched, or a
 * match resolved to a non-terminal / unrecognized status — callers should
 * treat that as "fall back to plain disappearance", never as an error.
 *
 * @param {any[]} disappeared — rows reducePoll() reported as gone.
 * @param {any[] | null | undefined} algoRows — fetchAlgoOrdersRecent() result.
 * @param {number} nowMs — Date.now() at build time.
 * @param {number} [durationMs]
 * @returns {any[]} ghost rows, each `{ id, account, transaction_type, quantity, symbol, label, cls, expiresAt }`
 */
export function buildGhosts(disappeared, algoRows, nowMs, durationMs = GHOST_DISPLAY_MS) {
  if (!Array.isArray(disappeared) || disappeared.length === 0) return [];
  const byId = new Map();
  for (const r of (Array.isArray(algoRows) ? algoRows : [])) {
    if (r?.id != null) byId.set(r.id, r);
  }
  const ghosts = [];
  for (const row of disappeared) {
    const match = row?.id != null ? byId.get(row.id) : undefined;
    if (!match) continue;
    const info = _GHOST_STATUS_INFO[String(match.status || '').toUpperCase()];
    if (!info) continue;
    ghosts.push({
      id: row.id,
      account: row.account,
      transaction_type: row.transaction_type,
      quantity: row.quantity,
      symbol: row.symbol,
      label: info.label,
      cls: info.cls,
      expiresAt: nowMs + durationMs,
    });
  }
  return ghosts;
}

/**
 * Drop any ghost whose id has reappeared in the live chase list, or
 * whose display window has elapsed.
 *
 * @param {any[]} ghosts
 * @param {Set<any> | any[]} liveIds
 * @param {number} nowMs
 * @returns {any[]}
 */
export function pruneGhosts(ghosts, liveIds, nowMs) {
  if (!Array.isArray(ghosts) || ghosts.length === 0) return [];
  const live = liveIds instanceof Set ? liveIds : new Set(liveIds || []);
  return ghosts.filter((g) => !live.has(g.id) && g.expiresAt > nowMs);
}

/**
 * Merge a fresh batch of ghosts into the existing list, deduplicated by
 * `id` — the later entry (from `fresh`) wins, refreshing its `expiresAt`.
 *
 * Why this is needed: `_load()` can be re-entered while a prior call is
 * still awaiting the best-effort algo-orders lookup — `_reconcile()` and
 * `onMount()` both call `_load()` directly, bypassing the `_fetching`
 * in-flight guard that only wraps the recurring timer path. A second,
 * concurrent `_load()` can therefore diff against the same stale
 * `_chases` and report the SAME disappeared id a second time. Appending
 * both resulting ghost batches with no dedup would then produce two
 * `{ id: N, ... }` entries in `_ghosts`, and Svelte 5's keyed
 * `{#each _ghosts as ghost (ghost.id)}` throws `each_key_duplicate` on a
 * repeated key — this merge is what keeps that id list unique.
 *
 * @param {any[]} existing
 * @param {any[]} fresh
 * @returns {any[]}
 */
export function mergeGhosts(existing, fresh) {
  const byId = new Map();
  for (const g of (Array.isArray(existing) ? existing : [])) {
    if (g?.id != null) byId.set(g.id, g);
  }
  for (const g of (Array.isArray(fresh) ? fresh : [])) {
    if (g?.id != null) byId.set(g.id, g); // later entry wins — refreshes expiresAt
  }
  return [...byId.values()];
}
