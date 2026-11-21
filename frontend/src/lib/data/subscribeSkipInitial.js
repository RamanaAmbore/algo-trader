/**
 * subscribeSkipInitial — subscribe to a Svelte store while skipping the
 * synchronous initial-value callback every `store.subscribe()` fires
 * immediately with the store's CURRENT value at subscribe time.
 *
 * Svelte's writable/readable contract always invokes a new subscriber
 * once, synchronously, with whatever value the store currently holds —
 * even when that value hasn't "changed" since any baseline the caller
 * cares about. A consumer that wants to react only to a REAL subsequent
 * update (e.g. `bookChanged`'s monotonic counter — see
 * `frontend/src/lib/data/bookChanged.js` — which starts at 0, and a
 * late-mounting tab's subscribe() call echoes back whatever value it has
 * already reached) must not treat that first echo as a real event, or it
 * fires a spurious action on every mount / resubscribe.
 *
 * `fn` is called with the store's value on every invocation EXCEPT the
 * first. Returns the store's own unsubscribe function.
 *
 * Extracted as a plain (no-runes) module so it can be exercised directly
 * against a real `svelte/store` `writable` in Vitest — the file it's
 * primarily used from, `marketDataStores.svelte.js`, has top-level
 * `$state(...)` calls that Vitest can't import without a Svelte-compiler
 * plugin (see `frontend/src/lib/__tests__/data/bookPollerClosedWindow.test.js`'s
 * header comment for the same constraint).
 *
 * @template T
 * @param {{ subscribe: (run: (value: T) => void) => (() => void) }} store
 * @param {(value: T) => void} fn
 * @returns {() => void} unsubscribe
 */
export function subscribeSkipInitial(store, fn) {
  let primed = false;
  return store.subscribe((value) => {
    if (!primed) {
      primed = true;
      return;
    }
    fn(value);
  });
}
