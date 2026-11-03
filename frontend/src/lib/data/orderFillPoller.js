/**
 * orderFillPoller.js — layout-resident (mount-independent) order-fill
 * watcher.
 *
 * Why this exists (2026-10-01, follow-up to 0b862409 / c90a9d04):
 * `noteOrderPollFills()` (orderFillDetector.js) is only ever called from
 * inside OrderBook.svelte's and LogPanel.svelte's own `_loadOrders()`
 * poll loops. Any page that never mounts either component has NO path
 * to the detector at all. Confirmed gap: `/admin/derivatives` mounts
 * neither OrderBook nor LogPanel unless the operator happens to open the
 * embedded ticket (SymbolPanel), so a fill delivered only through one of
 * the three silent channels orderFillDetector.js documents (live
 * broker-order-book TTL read, the 5-min open_order_watchdog sweep, or an
 * admin reconcile) left the Legs grid / Payoff chart stale until a
 * manual refresh — exactly the reported symptom.
 *
 * This is a BACKSTOP, not the primary fill-notification path. A genuine
 * broker postback (Kite, reliable per CLAUDE.md "Dhan/Groww order
 * detection") already reaches most pages directly via their own
 * `position_filled` / `order_update` WS handlers (e.g. the derivatives
 * page's own listener) the moment the postback fires — this poller only
 * matters when no WS event was ever broadcast for the fill, or the
 * specific client's WS connection missed it, and until now, the
 * mount-dependent OrderBook/LogPanel-only backstop didn't help if
 * neither was on screen either.
 *
 * Started ONCE from the (algo) layout root — same "layout-resident
 * singleton" pattern `startBookPollers()` already uses for positions /
 * holdings / funds — so detection no longer depends on which components
 * happen to be mounted on the current page.
 *
 * Deliberately duplicates OrderBook.svelte's / LogPanel.svelte's own
 * fetch+merge rather than extracting a shared helper — this exact merge
 * (fetchOrders() + fetchAlgoOrdersRecent(), broker rows win on duplicate
 * order_id) already lives independently in both of those components by
 * design (each keeps its own try/catch + freeze-to-last-good so one
 * surface's fetch hiccup never blanks another); a third independent
 * copy here follows the same precedent rather than introducing a new
 * shared dependency three call sites would need to agree on.
 *
 * `noteOrderPollFills`'s `_lastStatus`/`_seeded` state is module-global
 * (shared by every caller in the tab), so this poller's ticks and
 * OrderBook's/LogPanel's ticks all feed the SAME edge-detector — a fill
 * is only ever reported once regardless of how many pollers observed it.
 */
import { fetchOrders, fetchAlgoOrdersRecent } from '$lib/api';
import { noteOrderPollFills } from './orderFillDetector.js';

/** Guards against overlapping polls if one call stalls past the next tick. */
let _inFlight = false;

export async function pollOrderFillWatch() {
  if (_inFlight) return;
  _inFlight = true;
  try {
    const [brokerResp, algoResp] = await Promise.allSettled([
      fetchOrders(),
      fetchAlgoOrdersRecent(100, 'all'),
    ]);
    // Freeze-to-last-good convention (matches OrderBook.svelte's
    // _loadOrders): a rejected broker fetch must NOT be treated as "the
    // broker book is empty" — skip this tick entirely rather than
    // feeding a partial/empty row set into the shared detector, which
    // would otherwise "unsee" every currently-filled order_id and
    // spuriously refire on the next good poll.
    if (brokerResp.status !== 'fulfilled') return;
    const brokerRows = Array.isArray(brokerResp.value?.rows) ? brokerResp.value.rows : [];
    const algoRows = (algoResp.status === 'fulfilled' && Array.isArray(algoResp.value))
      ? algoResp.value
      : [];
    const brokerIds = new Set(brokerRows.map(o => String(o?.order_id || '')));
    const algoOnly = algoRows.filter(o => {
      const oid = String(o?.order_id || o?.id || '');
      return !brokerIds.has(oid);
    });
    noteOrderPollFills([...brokerRows, ...algoOnly]);
  } catch (_) {
    // Never let this watcher break the layout — detection degrades to
    // the next tick, same staleness-tolerant convention used elsewhere.
  } finally {
    _inFlight = false;
  }
}
