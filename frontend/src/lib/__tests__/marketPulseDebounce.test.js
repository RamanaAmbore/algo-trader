/**
 * Unit tests for the _scheduleFlashRefresh debounce pattern used in
 * MarketPulse.svelte's tickBus subscriber (Fix A).
 *
 * Tests the standalone debounce logic directly — no Svelte component import
 * needed. Verifies the key invariant: multiple rapid calls within the 50ms
 * window collapse into a single callback execution.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

describe('_scheduleFlashRefresh debounce logic', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  /**
   * Returns a standalone implementation of the _scheduleFlashRefresh pattern
   * with an injectable callback, mirroring the structure in MarketPulse.svelte.
   */
  function makeDebouncer(callback, delayMs = 50) {
    let timer = null;
    function schedule() {
      if (timer) return;          // guard: already pending — no-op
      timer = setTimeout(() => {
        timer = null;
        callback();
      }, delayMs);
    }
    function destroy() {
      if (timer) { clearTimeout(timer); timer = null; }
    }
    return { schedule, destroy, getTimer: () => timer };
  }

  it('fires the callback once when called once', () => {
    const cb = vi.fn();
    const { schedule } = makeDebouncer(cb);

    schedule();
    vi.advanceTimersByTime(50);

    expect(cb).toHaveBeenCalledTimes(1);
  });

  it('fires the callback only once when called rapidly multiple times', () => {
    const cb = vi.fn();
    const { schedule } = makeDebouncer(cb);

    // Simulate 10 ticks arriving in the same 50ms window
    for (let i = 0; i < 10; i++) {
      schedule();
      vi.advanceTimersByTime(4); // 4ms between ticks — still within 50ms
    }
    vi.advanceTimersByTime(50); // let the timer fire

    expect(cb).toHaveBeenCalledTimes(1);
  });

  it('fires the callback twice when two windows are separated by >50ms', () => {
    const cb = vi.fn();
    const { schedule } = makeDebouncer(cb);

    // First burst
    schedule();
    schedule();
    vi.advanceTimersByTime(50); // first callback fires

    // Second burst after timer has cleared
    schedule();
    vi.advanceTimersByTime(50); // second callback fires

    expect(cb).toHaveBeenCalledTimes(2);
  });

  it('guard (if timer) prevents re-scheduling before timer fires', () => {
    const cb = vi.fn();
    const { schedule, getTimer } = makeDebouncer(cb);

    schedule();
    const firstTimer = getTimer();
    expect(firstTimer).not.toBeNull();

    // Second call while timer is still pending — should be a no-op
    schedule();
    expect(getTimer()).toBe(firstTimer); // same timer, not replaced

    vi.advanceTimersByTime(50);
    expect(cb).toHaveBeenCalledTimes(1);
  });

  it('destroy clears the pending timer so callback never fires', () => {
    const cb = vi.fn();
    const { schedule, destroy, getTimer } = makeDebouncer(cb);

    schedule();
    expect(getTimer()).not.toBeNull();

    destroy();
    expect(getTimer()).toBeNull();

    vi.advanceTimersByTime(200); // past the delay
    expect(cb).not.toHaveBeenCalled();
  });

  it('after destroy, scheduling again starts a fresh timer', () => {
    const cb = vi.fn();
    const { schedule, destroy } = makeDebouncer(cb);

    schedule();
    destroy(); // clean up mid-flight (onDestroy scenario)

    // Component re-mounted — schedule again
    schedule();
    vi.advanceTimersByTime(50);

    expect(cb).toHaveBeenCalledTimes(1);
  });
});
