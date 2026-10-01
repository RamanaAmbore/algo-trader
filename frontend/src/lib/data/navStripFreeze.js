/**
 * navStripFreeze — pure helper for PositionStrip's day-P&L freeze/thaw gate.
 *
 * Bug fixed (2026-10-01, real-money bug: "NavStrip P∆ shows 0 and never
 * recovers until a full page reload, while the Pulse grid / Snapshot tab
 * keep showing the real non-zero value for the same position"):
 *
 * The freeze gate used to release only when a shared, `bookPollerTick`-
 * driven counter (`_pollCycleStamp` in PositionStrip.svelte) advanced past
 * a snapshot taken at the moment of a closed→open transition or an
 * execution-mode switch. That counter is incremented by ONE central poller
 * (`_tickBookPollers` in marketDataStores.svelte.js) shared across the
 * whole app. If that shared poller stalled or lagged for any reason
 * (closed-hours 30-min cadence, a visibility-hibernation throttle, an
 * exception swallowed inside its own `Promise.allSettled`, etc.) the gate
 * could never release — PositionStrip held day P&L frozen at 0
 * indefinitely, even though the backing store's own data had already
 * refreshed to a real, non-zero value (confirmed independently by
 * MarketPulse's Pulse grid, which computes the same Day P&L formula over
 * an independently-fetched — but otherwise equivalent — positions array
 * and was therefore unaffected by this component-local gate). A full page
 * reload "fixed" it only because reload re-initializes the transition
 * stamp to a value that is already trivially satisfied.
 *
 * Before the 2026-08 "positionsDayPnlStore SSOT + rationalize poll
 * cycles" consolidation (commit 6c66330b), PositionStrip ran its own
 * independent 30s refresh timer, which acted as a redundant safety net
 * against exactly this class of stall. Removing that timer (in favour of
 * the shared poller) removed the safety net without replacing the gate's
 * release condition with anything equally robust.
 *
 * Fix: release the gate directly off the backing store's own `lastFetch`
 * bookkeeping (bumped only on a genuine landed — not degraded-empty —
 * fetch; see dataStore.svelte.js's `_applyRaw`) instead of the shared tick
 * counter. This is a strictly stronger signal — it reflects whether THIS
 * slot's own data has actually refreshed, independent of any other
 * subsystem's health.
 */

/**
 * Should a NavStrip day-P&L slot (P or H) mirror its live derived value
 * right now, or stay frozen at the pre-transition display value?
 *
 * @param {boolean} open  Is the relevant market segment currently open?
 *   When false (market closed), the slot is never gated — it always
 *   mirrors the live derived (which itself freezes to the last snapshot
 *   per the market-close-snapshot rule elsewhere in the app).
 * @param {number} lastFetch  epoch-ms of the backing store's last
 *   successful (non-degraded-empty) fetch (`store.lastFetch`).
 * @param {number} openTransitionAt  epoch-ms snapshot taken at the moment
 *   of the closed→open transition or an execution-mode switch (0 if no
 *   transition has occurred yet this session).
 * @returns {boolean} true when the slot should mirror the live derived
 *   value now; false while still frozen waiting for a fresh fetch.
 */
export function isSlotFreshAfterTransition(open, lastFetch, openTransitionAt) {
  return !open || lastFetch > openTransitionAt;
}
