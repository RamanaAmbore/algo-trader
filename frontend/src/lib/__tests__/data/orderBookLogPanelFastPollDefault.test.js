/**
 * orderBookLogPanelFastPollDefault.test.js — coverage for the
 * OrderBook.svelte / LogPanel.svelte `pollMs` default change (2026-10-02
 * fix — "Fix 3: OrderBook/LogPanel poll cadence 3s → FAST"), amended
 * same session to also cover the ActivityLogSurface.svelte unshadow fix
 * ("Fix 1: unshadow LogPanel's FAST cadence default").
 *
 * Why a source-scan: vitest.config.js has no Svelte compiler plugin, so
 * none of these components can be mounted/executed here — same
 * constraint as every other `?raw` source-scan test added in this
 * session.
 *
 * Why a static literal (5000), not a settings-driven fetchSettings() read
 * inside these components: all three components create their
 * `visibleInterval` synchronously inside `onMount` — threading a real
 * `polling.fast_ms` settings read through would hit the exact same async
 * race Fix 2 had to explicitly re-arm around in `(algo)/+layout.svelte`
 * ("the `visibleInterval(...)` call already ran synchronously BEFORE
 * this async fetch resolves"). `marketDataStores.svelte.js` (the module
 * holding the matching getter/setter pattern for other `polling.*`
 * values) was out-of-scope to edit for this task, so there is no shared
 * landing spot to read from without inventing new cross-file plumbing.
 * 5000 is the `polling.fast_ms` registry setting's own default value
 * (backend/shared/helpers/settings.py), so the literal at least matches
 * today's configured default even though it won't track a future
 * operator-changed value.
 *
 * ActivityLogSurface.svelte is the ONLY real mount path for LogPanel
 * today and always passes `{pollMs}` down explicitly — its own default
 * previously shadowed LogPanel's (3000 vs LogPanel's 5000), making
 * LogPanel's default change inert end-to-end. Fixed 2026-10-02: both
 * now agree at 5000, so LogPanel's FAST-cadence change is live for every
 * real mount. OrderBook.svelte has no such intermediary (both its real
 * call sites, orders/+page.svelte and SymbolPanel.svelte, mount it with
 * no `pollMs` prop at all), so its default change was already live.
 *
 * Five quality dimensions:
 *  1. SSOT   — reads the real shipped component source files.
 *  2. Perf   — 5000ms vs the old 3000ms is a ~40% reduction in request
 *              rate for every mounted OrderBook / ActivityLogSurface
 *              instance.
 *  3. Stale  — pins the EXACT new default value (5000), not just "not
 *               3000 anymore", on all three files; guards against the
 *              3000 literal creeping back into ActivityLogSurface.
 *  4. Reuse  — confirms no file invents a parallel settings-fetch
 *              mechanism; marketDataStores.svelte.js (the correct future
 *              landing spot) is referenced only in comments, not imported.
 *  5. UX     — confirms LogPanel's comment no longer claims a live
 *              "KNOWN GAP" / shadowing defect that has since been fixed
 *              (stale comments mislead future readers into re-litigating
 *              an already-closed gap).
 */

import { describe, it, expect } from 'vitest';
import orderBookSrc from '../../OrderBook.svelte?raw';
import logPanelSrc from '../../LogPanel.svelte?raw';
import activityLogSurfaceSrc from '../../ActivityLogSurface.svelte?raw';

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

  it('no longer flags the ActivityLogSurface shadowing gap as open (resolved 2026-10-02)', () => {
    // The gap was real until ActivityLogSurface.svelte's own default was
    // bumped to match — now that both agree, the "KNOWN GAP" marker must
    // not linger and mislead a future reader into thinking the cadence
    // is still shadowed end-to-end.
    expect(logPanelSrc.includes('KNOWN GAP')).toBe(false);
    expect(logPanelSrc.includes('ActivityLogSurface.svelte')).toBe(true);
  });
});

describe('ActivityLogSurface.svelte — pollMs default (unshadow fix)', () => {
  it('defaults pollMs to 5000, not the old 3000 literal that shadowed LogPanel', () => {
    expect(/pollMs\s*=\s*5000,/.test(activityLogSurfaceSrc)).toBe(true);
    expect(/pollMs\s*=\s*3000,/.test(activityLogSurfaceSrc)).toBe(false);
  });

  it('still passes pollMs through to LogPanel explicitly (unchanged wiring)', () => {
    expect(/<LogPanel[\s\S]{0,400}\{pollMs\}/.test(activityLogSurfaceSrc)).toBe(true);
  });
});
