/**
 * order_ticket_margin_preview_exchange_watcher.spec.js
 *
 * 2026-10-20 fix (plan item 9.1) — the margin-preview `$effect` in
 * `OrderTicket.svelte` (the one that calls `previewOrderMargin` /
 * `/api/orders/preflight`) tracked the bare `exchange` PROP in its
 * `_watchers` dependency array but not `_exchange`/`_resolvedExchange`
 * — the resolved/operator-overridable values the request body itself
 * actually sends (`exchange: _exchange || _resolvedExchange || exchange
 * || 'NFO'`). Same bug CLASS as the `eae36fca` exchange-prop submit fix,
 * in the preview path instead of submit: switching the resolved
 * exchange without the bare prop ALSO changing would leave the margin
 * preview silently stale (re-firing only coincidentally, whenever some
 * OTHER watched field like qty/side/price also happened to change).
 *
 * NOT dynamically reachable via the real UI as of this fix: the
 * Exchange Select dropdown that would let an operator flip `_exchange`
 * directly was deliberately REMOVED on 2026-07-26 (commit 271713dd,
 * "remove exchange knob") — confirmed by the sibling
 * `order_ticket_exchange_submit_payload.spec.js`'s own investigation
 * (see its file-level comment: "There is currently no UI control that
 * can set `_exchangeTouched = true`"). `_resolvedExchange` itself is a
 * read-only instruments-cache lookup keyed off the picked symbol, not
 * independently settable either. Without a reachable trigger, a live
 * interception test proving "margin preview refetches after an exchange
 * change" cannot be constructed today — this is a source-level
 * regression guard instead, per the task's own documented fallback for
 * this exact case.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const ROOT = '/Users/ramanambore/projects/ramboq/frontend/src/lib';

test.describe('Order Ticket — margin-preview watcher list includes resolved exchange', () => {
  test('source: the margin-preview $effect watcher list tracks _exchange and _resolvedExchange', () => {
    const src = readFileSync(`${ROOT}/order/OrderTicket.svelte`, 'utf-8');

    // Locate the margin-preview effect specifically (the one whose
    // watcher array feeds `previewOrderMargin`/basket_margin), not the
    // separate template-preview effect further down the file.
    const watcherIdx = src.indexOf('const _watchers = [');
    expect(watcherIdx, '_watchers array not found').toBeGreaterThan(-1);
    const block = src.slice(watcherIdx, src.indexOf('];', watcherIdx));

    expect(block).toMatch(/_exchange\b/);
    expect(block).toMatch(/_resolvedExchange\b/);

    // Confirm this really is the margin-preview effect (sanity check —
    // the block should also track _qty/_product/_resolvedSymbol, the
    // other fields the same effect's request payload reads).
    expect(block).toMatch(/_qty\b/);
    expect(block).toMatch(/_product\b/);
    expect(block).toMatch(/_resolvedSymbol\b/);

    // The request payload itself must actually read these same resolved
    // values (otherwise tracking them would be a no-op) — confirms the
    // fallback chain established by the eae36fca exchange-prop fix is
    // what feeds `previewOrderMargin`.
    const payloadIdx = src.indexOf('await previewOrderMargin(payload)');
    expect(payloadIdx, 'previewOrderMargin call site not found').toBeGreaterThan(-1);
    const payloadBlock = src.slice(Math.max(0, payloadIdx - 800), payloadIdx);
    expect(payloadBlock).toMatch(/exchange:\s*_exchange\s*\|\|\s*_resolvedExchange\s*\|\|\s*exchange\s*\|\|\s*'NFO'/);
  });
});
