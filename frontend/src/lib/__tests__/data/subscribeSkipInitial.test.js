/**
 * subscribeSkipInitial.test.js
 *
 * `subscribeSkipInitial` (frontend/src/lib/data/subscribeSkipInitial.js)
 * is the plain-JS (no-runes) extraction backing the `bookChanged`
 * forced-refresh subscription wired into `marketDataStores.svelte.js`'s
 * `startBookPollers()` (2026-11 fix — "closed-hours broker-account
 * re-enable refreshes positions/holdings/funds in EVERY open browser
 * session"). Exercised here against a REAL `svelte/store` `writable`,
 * not a hand-rolled mirror — this is the critical correctness point the
 * fix depends on: svelte's subscribe contract always fires the callback
 * once, synchronously, with the store's CURRENT value, and a consumer
 * that doesn't skip that echo fires a spurious action on every mount.
 */
import { describe, it, expect } from 'vitest';
import { writable, get } from 'svelte/store';
import { subscribeSkipInitial } from '$lib/data/subscribeSkipInitial.js';

describe('subscribeSkipInitial (real svelte/store writable)', () => {
  it('does not call fn on the initial synchronous echo, even when the store is already non-zero', () => {
    const store = writable(3); // mirrors a late-mounting tab's bookChanged already > 0
    let calls = 0;
    const unsub = subscribeSkipInitial(store, () => { calls++; });
    expect(calls).toBe(0);
    unsub();
  });

  it('calls fn exactly once on the first real update after subscribing', () => {
    const store = writable(0);
    let calls = 0;
    const unsub = subscribeSkipInitial(store, () => { calls++; });
    expect(calls).toBe(0);
    store.update((n) => n + 1);
    expect(calls).toBe(1);
    unsub();
  });

  it('calls fn once per subsequent real update — not continuous/resumed, strictly edge-triggered', () => {
    const store = writable(0);
    let calls = 0;
    const unsub = subscribeSkipInitial(store, () => { calls++; });
    store.update((n) => n + 1);
    expect(calls).toBe(1);
    store.update((n) => n + 1);
    expect(calls).toBe(2);
    // No extra calls without a further real update.
    expect(calls).toBe(2);
    unsub();
  });

  it('passes the store\'s current value to fn', () => {
    const store = writable(0);
    const seen = [];
    const unsub = subscribeSkipInitial(store, (v) => seen.push(v));
    store.set(5);
    store.set(9);
    expect(seen).toEqual([5, 9]);
    unsub();
  });

  it('unsubscribe stops further calls', () => {
    const store = writable(0);
    let calls = 0;
    const unsub = subscribeSkipInitial(store, () => { calls++; });
    store.update((n) => n + 1);
    expect(calls).toBe(1);
    unsub();
    store.update((n) => n + 1);
    expect(calls).toBe(1); // unchanged after unsubscribe
  });

  it('a fresh resubscribe after unsubscribe gets its own skip-first-echo, not a spurious fire from the prior value', () => {
    const store = writable(0);
    store.update((n) => n + 1); // store is now 1, no subscriber attached
    expect(get(store)).toBe(1);

    let calls = 0;
    const unsub = subscribeSkipInitial(store, () => { calls++; });
    // The fresh subscribe's initial echo (value=1) must be skipped too.
    expect(calls).toBe(0);
    store.update((n) => n + 1);
    expect(calls).toBe(1);
    unsub();
  });

  it('returns the underlying store.subscribe unsubscribe function (store has no lingering subscriber after unsub)', () => {
    const store = writable(0);
    let calls = 0;
    const unsub = subscribeSkipInitial(store, () => { calls++; });
    unsub();
    // After unsubscribing, further updates must not invoke fn at all.
    store.update((n) => n + 1);
    store.update((n) => n + 1);
    expect(calls).toBe(0);
  });
});
