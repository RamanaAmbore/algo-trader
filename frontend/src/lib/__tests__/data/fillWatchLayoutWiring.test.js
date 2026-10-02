/**
 * fillWatchLayoutWiring.test.js — coverage for the `(algo)/+layout.svelte`
 * half of the fill-watch backstop cadence wiring (companion to
 * fillWatchIntervalMs.test.js, which covers the marketDataStores.svelte.js
 * getter/setter pair this file consumes).
 *
 * Why a source-scan, not a component mount: vitest.config.js has no Svelte
 * compiler plugin (same documented constraint as fillWatchIntervalMs.test.js
 * and marketDataStoresMeta.test.js) — `+layout.svelte` can't be imported and
 * executed directly in this harness. A `?raw` string-scan of the shipped
 * file is the established convention for locking down wiring that can't be
 * exercised as a real component here.
 *
 * What this guards (2026-10-02 fix — "Fix 2: wire fill-watch backstop
 * cadence to the new setting"):
 *   1. The layout reads `polling.slow_ms` from fetchSettings() and calls
 *      `setFillWatchIntervalMs()` with it — the settings-driven half.
 *   2. CRITICAL regression this fix closes: the original
 *      `visibleInterval(pollOrderFillWatch, 5000)` call ran synchronously
 *      in onMount, BEFORE the async fetchSettings() promise above it ever
 *      resolves. Calling `setFillWatchIntervalMs()` without also tearing
 *      down + recreating the already-created interval would be a dead
 *      write — the running timer would keep ticking at whatever cadence
 *      it was created with. This test greps for the re-arm
 *      (`fillWatchTeardown?.()` followed by a fresh `visibleInterval(...)`
 *      call) inside the same settings block that calls
 *      `setFillWatchIntervalMs`, not just the setter call in isolation.
 *   3. The literal `5000` hardcode is gone from the interval call site —
 *      replaced by `getFillWatchIntervalMs()`.
 *   4. A destroyed-guard prevents the re-arm from recreating an interval
 *      after the layout has already torn down (onDestroy flips
 *      `_layoutDestroyed` before the async settings fetch can race it).
 *
 * Five quality dimensions:
 *  1. SSOT   — reads the real shipped `+layout.svelte` file, not a copy.
 *  2. Perf   — pure string/regex checks, no I/O, no component mount cost.
 *  3. Stale  — regression-guards the exact defect class (settings read
 *              with no re-arm = silently inert), not just presence of a
 *              `polling.slow_ms` substring.
 *  4. Reuse  — confirms the fix reuses the already-shipped
 *              getFillWatchIntervalMs/setFillWatchIntervalMs pair instead
 *              of inventing new plumbing.
 *  5. UX     — confirms the fill-watch poller still fires once immediately
 *              on mount (no regression to "wait a full cadence before the
 *              first backstop check").
 */

import { describe, it, expect } from 'vitest';
import src from '../../../routes/(algo)/+layout.svelte?raw';

describe('(algo)/+layout.svelte — fill-watch backstop cadence wiring', () => {
  it('imports getFillWatchIntervalMs and setFillWatchIntervalMs from marketDataStores', () => {
    expect(/import\s*\{[^}]*getFillWatchIntervalMs[^}]*setFillWatchIntervalMs[^}]*\}\s*from\s*'\$lib\/data\/marketDataStores\.svelte\.js'/.test(src))
      .toBe(true);
  });

  it('reads polling.slow_ms from the fetchSettings() response', () => {
    expect(src.includes("s?.key === 'polling.slow_ms'")).toBe(true);
  });

  it('calls setFillWatchIntervalMs with the read value, guarded by the >=1000ms floor', () => {
    expect(/if \(Number\.isFinite\(slowV\) && slowV >= 1000\) \{\s*setFillWatchIntervalMs\(slowV\);/.test(src))
      .toBe(true);
  });

  it('re-arms the interval (tears down + recreates) in the SAME block that sets the cadence — ' +
     'the critical fix: a bare setter call with no re-arm would be a dead write', () => {
    // Slice from the setFillWatchIntervalMs call to the next top-level
    // "} catch" that closes the fetchSettings() async IIFE, then confirm
    // both a teardown call and a fresh visibleInterval(...) call appear
    // in that slice.
    const setterIdx = src.indexOf('setFillWatchIntervalMs(slowV);');
    expect(setterIdx).toBeGreaterThan(-1);
    const closeIdx = src.indexOf("} catch { /* anon/demo", setterIdx);
    expect(closeIdx).toBeGreaterThan(setterIdx);
    const block = src.slice(setterIdx, closeIdx);
    expect(block.includes('fillWatchTeardown?.();')).toBe(true);
    expect(/fillWatchTeardown = visibleInterval\(_fillWatchTick, getFillWatchIntervalMs\(\)\)/.test(block))
      .toBe(true);
  });

  it('guards the re-arm against firing after the layout has been destroyed', () => {
    expect(src.includes('if (!_layoutDestroyed) {')).toBe(true);
    expect(/let _layoutDestroyed = false;[\s\S]{0,40}onDestroy\(\(\) => \{\s*_layoutDestroyed = true;/.test(src))
      .toBe(true);
  });

  it('removed the hardcoded 5000ms literal from the fill-watch interval call site', () => {
    expect(src.includes('visibleInterval(pollOrderFillWatch, 5000)')).toBe(false);
  });

  it('still fires once immediately on mount before the interval starts (via _fillWatchTick)', () => {
    expect(/_fillWatchTick\(\);\s*\n\s*fillWatchTeardown = visibleInterval\(_fillWatchTick, getFillWatchIntervalMs\(\)\);/.test(src))
      .toBe(true);
  });

  it('_fillWatchTick wraps pollOrderFillWatch() so the re-arm points at a stable callback reference', () => {
    expect(/async function _fillWatchTick\(\) \{\s*await pollOrderFillWatch\(\);\s*\}/.test(src))
      .toBe(true);
  });
});
