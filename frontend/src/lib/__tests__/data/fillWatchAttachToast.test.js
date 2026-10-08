/**
 * fillWatchAttachToast.test.js — coverage for the "template did not
 * attach" toast now firing from the mount-independent fill-watch backstop
 * in `(algo)/+layout.svelte` (2026-10-02 fix — "Fix 4").
 *
 * Confirmed premise before this fix: `noteAttachObservation()`
 * (templateAttachToast.js) ran ONLY inside OrderBook.svelte's and
 * LogPanel.svelte's own `_loadOrders()` poll loops. A page mounting
 * neither component (e.g. /admin/derivatives with no ticket open) never
 * saw the toast at all — the exact same mount-dependent gap class
 * `orderFillPoller.js` already closed for `noteOrderPollFills`/
 * `bookChanged`, just for a different downstream effect (a toast, not a
 * refetch).
 *
 * Why a source-scan: vitest.config.js has no Svelte compiler plugin, so
 * `+layout.svelte` can't be mounted/executed here — same constraint as
 * fillWatchLayoutWiring.test.js (Fix 2's test), which this file is a
 * companion to.
 *
 * Five quality dimensions:
 *  1. SSOT   — reads the real shipped `+layout.svelte` file.
 *  2. Perf   — only `fetchAlgoOrdersRecent` is fetched for this check
 *              (not a second `fetchOrders()` broker-book round trip) —
 *              regression-guarded explicitly below, since
 *              template_id/mode/attached_gtts_json are AlgoOrder-only
 *              fields broker rows never carry.
 *  3. Stale  — guards the exact toast STRING matches OrderBook.svelte's
 *              existing wording (`templateAttachToast` dedupe is keyed
 *              by order id only, not message text, but an operator
 *              seeing two different strings for the same condition
 *              across surfaces would be confusing).
 *  4. Reuse  — confirms noteAttachObservation (not a reimplementation)
 *              is the decision function, and that it's piggybacked onto
 *              the SAME re-armed interval Fix 2 wired up, not a second
 *              independent poller/cadence.
 *  5. UX     — toast.warning with the same 5s timeoutMs as OrderBook's
 *              own call, so the operator sees consistent toast duration
 *              regardless of which code path raised it.
 */

import { describe, it, expect } from 'vitest';
import src from '../../../routes/(algo)/+layout.svelte?raw';
import orderBookSrc from '../../OrderBook.svelte?raw';

describe('(algo)/+layout.svelte — mount-independent template-attach toast', () => {
  it('imports noteAttachObservation from templateAttachToast.js', () => {
    expect(src.includes("import { noteAttachObservation } from '$lib/data/templateAttachToast.js';"))
      .toBe(true);
  });

  it('imports fetchAlgoOrdersRecent from $lib/api', () => {
    expect(/fetchAlgoOrdersRecent,?\s*\n?\s*\}\s*from\s*'\$lib\/api'/.test(src)).toBe(true);
  });

  it('_fillWatchTick calls fetchAlgoOrdersRecent(100, \'all\') — NOT a second fetchOrders() round trip', () => {
    const tickStart = src.indexOf('async function _fillWatchTick()');
    expect(tickStart).toBeGreaterThan(-1);
    const tickEnd = src.indexOf('\n  }', tickStart);
    const tickBody = src.slice(tickStart, tickEnd);
    expect(tickBody.includes("fetchAlgoOrdersRecent(100, 'all')")).toBe(true);
    // Guard against scope creep: this backstop must not ALSO call the
    // broker order-book endpoint a second time per tick (pollOrderFillWatch
    // already does its own fetchOrders() internally — a second direct call
    // here would double the broker-book request rate for no benefit, since
    // broker rows never carry template_id/mode/attached_gtts_json).
    expect(tickBody.includes('fetchOrders(')).toBe(false);
  });

  it('calls noteAttachObservation on every algo row and toasts on a true return', () => {
    expect(src.includes('if (noteAttachObservation(o)) {')).toBe(true);
    expect(src.includes('toast.warning(`Order #${oid} bracket did not attach — check Order Book`, { timeoutMs: 5000 });'))
      .toBe(true);
  });

  it('uses the EXACT same toast wording OrderBook.svelte already uses (no copy-drift)', () => {
    expect(orderBookSrc.includes(
      'toast.warning(`Order #${oid} bracket did not attach — check Order Book`, { timeoutMs: 5000 });'
    )).toBe(true);
  });

  it('the attach-toast check is piggybacked on the SAME _fillWatchTick the fill-watch re-arm (Fix 2) owns, not a second poller', () => {
    const tickStart = src.indexOf('async function _fillWatchTick()');
    const tickEnd = src.indexOf('\n  }', tickStart);
    const tickBody = src.slice(tickStart, tickEnd);
    expect(tickBody.includes('await pollOrderFillWatch();')).toBe(true);
    expect(tickBody.includes('noteAttachObservation')).toBe(true);
    // Only one visibleInterval call site should exist for this tick fn.
    const armCount = src.split('visibleInterval(_fillWatchTick, getFillWatchIntervalMs())').length - 1;
    expect(armCount).toBe(2); // one initial arm + one settings-driven re-arm
  });

  it('a failed fetchAlgoOrdersRecent never throws out of _fillWatchTick (backstop freezes silently)', () => {
    const tickStart = src.indexOf('async function _fillWatchTick()');
    const tickEnd = src.indexOf('\n  }', tickStart);
    const tickBody = src.slice(tickStart, tickEnd);
    expect(/try\s*\{[\s\S]*fetchAlgoOrdersRecent[\s\S]*\}\s*catch/.test(tickBody)).toBe(true);
  });
});
