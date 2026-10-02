/**
 * orderBookLogPanelFastPollDefault.test.js — coverage for the
 * OrderBook.svelte / LogPanel.svelte `pollMs` default change (2026-10-02
 * fix — "Fix 3: OrderBook/LogPanel poll cadence 3s → FAST").
 *
 * Why a source-scan: vitest.config.js has no Svelte compiler plugin, so
 * neither component can be mounted/executed here — same constraint as
 * every other `?raw` source-scan test added in this session.
 *
 * Why a static literal (5000), not a settings-driven fetchSettings() read
 * inside these components: both components create their `visibleInterval`
 * synchronously inside `onMount` — threading a real `polling.fast_ms`
 * settings read through would hit the exact same async race Fix 2 had to
 * explicitly re-arm around in `(algo)/+layout.svelte` ("the
 * `visibleInterval(...)` call already ran synchronously BEFORE this async
 * fetch resolves"). `marketDataStores.svelte.js` (the module holding the
 * matching getter/setter pattern for other `polling.*` values) was
 * out-of-scope to edit for this task, so there is no shared landing spot
 * to read from without inventing new cross-file plumbing. 5000 is the
 * `polling.fast_ms` registry setting's own default value
 * (backend/shared/helpers/settings.py), so the literal at least matches
 * today's configured default even though it won't track a future
 * operator-changed value.
 *
 * Known scope gap this test documents explicitly (not silently): every
 * real-world mount of LogPanel.svelte today goes through
 * ActivityLogSurface.svelte (out of this change's file scope), which
 * declares its OWN `pollMs = 3000` default and always passes `{pollMs}`
 * down explicitly — so LogPanel's own default is currently inert end-to-
 * end. OrderBook.svelte has no such intermediary (both its real call
 * sites, orders/+page.svelte and SymbolPanel.svelte, mount it with no
 * `pollMs` prop at all), so its default change IS live.
 *
 * Five quality dimensions:
 *  1. SSOT   — reads the real shipped component source files.
 *  2. Perf   — 5000ms vs the old 3000ms is a ~40% reduction in request
 *              rate for every mounted OrderBook instance.
 *  3. Stale  — pins the EXACT new default value (5000), not just "not
 *               3000 anymore", and regression-guards the comment
 *              explaining WHY it's a static literal rather than a
 *              settings-driven read (so a future "just wire it to
 *              fetchSettings()" edit doesn't silently reintroduce the
 *              exact async race Fix 2 had to fix elsewhere).
 *  4. Reuse  — confirms neither file invents a parallel settings-fetch
 *              mechanism; marketDataStores.svelte.js (the correct future
 *              landing spot) is referenced only in comments, not imported.
 *  5. UX     — confirms the comment documenting the ActivityLogSurface
 *              gap for LogPanel wasn't silently dropped (an operator
 *              reading this code later must not be misled into thinking
 *              the fix is complete end-to-end for LogPanel).
 */

import { describe, it, expect } from 'vitest';
import orderBookSrc from '../../OrderBook.svelte?raw';
import logPanelSrc from '../../LogPanel.svelte?raw';

describe('OrderBook.svelte — pollMs default', () => {
  it('defaults pollMs to 5000, not the old 3000 literal', () => {
    expect(/pollMs\s*=\s*5000,/.test(orderBookSrc)).toBe(true);
    expect(/pollMs\s*=\s*3000,/.test(orderBookSrc)).toBe(false);
  });

  it('documents why this is a static literal, not a settings-driven read', () => {
    expect(orderBookSrc.includes('matching the')).toBe(true);
    expect(orderBookSrc.includes('polling.fast_ms')).toBe(true);
    expect(orderBookSrc.includes('marketDataStores.svelte.js')).toBe(true);
  });

  it('has no real call site overriding pollMs away from the component default', () => {
    // Confirmed via repo-wide grep before this fix: neither real mount
    // (orders/+page.svelte, SymbolPanel.svelte) passes a pollMs prop —
    // this guards that OrderBook.svelte itself doesn't start passing a
    // hardcoded override to its own internal usage that would shadow the
    // new default (it has none; this is a negative-space check against
    // the file growing one).
    expect(/<OrderBook[\s\S]{0,400}pollMs=/.test(orderBookSrc)).toBe(false);
  });
});

describe('LogPanel.svelte — pollMs default', () => {
  it('defaults pollMs to 5000, not the old 3000 literal', () => {
    expect(/pollMs\s*=\s*5000,/.test(logPanelSrc)).toBe(true);
    expect(/pollMs\s*=\s*3000,/.test(logPanelSrc)).toBe(false);
  });

  it('documents the ActivityLogSurface shadowing gap (known, not silently dropped)', () => {
    expect(logPanelSrc.includes('ActivityLogSurface.svelte')).toBe(true);
    expect(logPanelSrc.includes('KNOWN GAP')).toBe(true);
  });
});
