import { untrack } from 'svelte';
import { get } from 'svelte/store';

/**
 * Read a Svelte store safely inside $derived / $derived.by().
 * Wraps get() in untrack() — the store's reactive write path
 * cannot trigger state_unsafe_mutation during derivation.
 * @template T
 * @param {import('svelte/store').Readable<T>} store
 * @returns {T}
 */
export function safeRead(store) {
  return untrack(() => get(store));
}
