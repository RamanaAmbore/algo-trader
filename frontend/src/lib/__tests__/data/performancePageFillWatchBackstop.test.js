/**
 * performancePageFillWatchBackstop.test.js — coverage for the auth-gated
 * fill-watch backstop poller added to `PerformancePage.svelte` (2026-10-02
 * fix — "Fix 1: PerformancePage refresh backstop").
 *
 * Context: PerformancePage.svelte already runs `createPerformanceSocket`
 * (WS, primary) and reacts to `$bookChanged` — the real residual gap was
 * three silent non-WS channels (broker-book TTL cache, the 5-min
 * `_task_performance` watchdog, admin reconcile sweeps) having no backstop
 * poller on this page at all. `/performance` is also a PUBLIC page, so the
 * backstop must be gated on an authenticated session — `pollOrderFillWatch()`
 * hits admin-guarded endpoints that would just 401 silently for an
 * anonymous visitor (wasted backend load, zero benefit).
 *
 * Why a source-scan: vitest.config.js has no Svelte compiler plugin, so
 * PerformancePage.svelte can't be mounted/executed here — same documented
 * constraint as performancePageColumns.test.js (inline arrays can't be
 * imported) and the (algo)/+layout.svelte wiring tests in this same
 * directory.
 *
 * Five quality dimensions:
 *  1. SSOT   — reads the real shipped PerformancePage.svelte file.
 *  2. Perf   — reuses the SAME layout-resident `pollOrderFillWatch` +
 *              settings-driven `getFillWatchIntervalMs()` cadence every
 *              (algo) page already gets, rather than a second bespoke
 *              poller/cadence for this one page.
 *  3. Stale  — regression-guards the auth-gate living INSIDE the poll
 *              callback (not at setup time) — the documented reason:
 *              a mid-session login must start the backstop on the very
 *              next tick, no reload required (same pattern
 *              startBrokerHealthPoller in stores.js already uses).
 *  4. Reuse  — confirms no duplicate fetch/merge logic was invented;
 *              `pollOrderFillWatch` is imported, not reimplemented.
 *  5. UX     — confirms the teardown is wired into onDestroy so repeated
 *              mount/unmount (e.g. SPA nav away and back) never leaks a
 *              running interval.
 */

import { describe, it, expect } from 'vitest';
import src from '../../PerformancePage.svelte?raw';

describe('PerformancePage.svelte — auth-gated fill-watch backstop poller', () => {
  it('imports pollOrderFillWatch and getFillWatchIntervalMs (reuse, not reimplementation)', () => {
    expect(src.includes("import { pollOrderFillWatch } from '$lib/data/orderFillPoller.js';"))
      .toBe(true);
    expect(/getFillWatchIntervalMs/.test(src)).toBe(true);
  });

  it('imports visibleInterval from $lib/stores', () => {
    expect(/import\s*\{[^}]*\bvisibleInterval\b[^}]*\}\s*from\s*'\$lib\/stores'/.test(src))
      .toBe(true);
  });

  it('gates pollOrderFillWatch() on authStore.getToken() INSIDE the poll callback, not at setup time', () => {
    const idx = src.indexOf('const _fwPoll = () =>');
    expect(idx).toBeGreaterThan(-1);
    const line = src.slice(idx, src.indexOf('\n', idx) + 1);
    expect(line.includes('authStore.getToken()')).toBe(true);
    expect(line.includes('pollOrderFillWatch()')).toBe(true);
  });

  it('fires the gated poll once immediately, then arms a settings-driven visibleInterval', () => {
    expect(src.includes('_fwPoll();')).toBe(true);
    expect(src.includes('_fillWatchTeardown = visibleInterval(_fwPoll, getFillWatchIntervalMs());'))
      .toBe(true);
  });

  it('declares _fillWatchTeardown and tears it down in onDestroy (no interval leak on unmount)', () => {
    expect(/let _fillWatchTeardown;/.test(src)).toBe(true);
    const destroyStart = src.indexOf('onDestroy(() => {');
    expect(destroyStart).toBeGreaterThan(-1);
    const destroyBody = src.slice(destroyStart, destroyStart + 400);
    expect(destroyBody.includes('_fillWatchTeardown?.();')).toBe(true);
  });

  it('never wires pollOrderFillWatch directly into visibleInterval (would bypass the auth gate)', () => {
    // Guards against a future edit accidentally doing
    // `visibleInterval(pollOrderFillWatch, ...)` directly (the layout's
    // own pattern) instead of going through the gated `_fwPoll` wrapper —
    // that would defeat the anonymous-visitor cost guard this fix exists
    // for, since the gate lives inside `_fwPoll`, not `pollOrderFillWatch`
    // itself.
    expect(/visibleInterval\(\s*pollOrderFillWatch\b/.test(src)).toBe(false);
    expect(src.includes('visibleInterval(_fwPoll, getFillWatchIntervalMs())')).toBe(true);
  });
});
