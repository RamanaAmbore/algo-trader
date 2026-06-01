/**
 * quoteStream.js — SSE-backed live LTP feed.
 *
 * Connects to GET /api/quote/stream and maintains two reactive primitives:
 *   liveLtp     — writable store: tradingsymbol → latest LTP
 *   streamOpen  — writable store: true when the SSE connection has received
 *                 at least one snapshot/tick without a subsequent error
 *
 * Usage (inside a Svelte component):
 *   import { liveLtp, streamOpen, startQuoteStream, stopQuoteStream } from '$lib/data/quoteStream';
 *   onMount(() => startQuoteStream());
 *   onDestroy(() => stopQuoteStream());
 *   // in a cell renderer or $derived: $liveLtp['RELIANCE'] ?? fallback
 *
 * Singleton pattern — multiple components calling startQuoteStream() share one
 * connection. stopQuoteStream() closes it and resets so the next startQuoteStream()
 * call opens a fresh connection.
 */

import { writable } from 'svelte/store';
import { browser } from '$app/environment';

/** Map of tradingsymbol → live LTP, updated as SSE ticks arrive. */
export const liveLtp = writable(/** @type {Record<string, number>} */ ({}));

/**
 * True when the SSE connection has been acknowledged by the server (snapshot
 * or first tick received). Flips to false on a persistent error / explicit
 * stop so callers can fall back to the polling path gracefully.
 */
export const streamOpen = writable(false);

/** @type {EventSource | null} */
let _es = null;
let _opened = false;

/**
 * Open the SSE connection (idempotent — safe to call multiple times).
 * Must only be called in a browser context (onMount or behind `if (browser)`).
 */
export function startQuoteStream() {
  if (!browser || _opened) return;
  _opened = true;

  // withCredentials sends the auth cookie — same auth path as every other
  // /api/* call. EventSource does not support arbitrary request headers so
  // bearer-token auth cannot be used here; cookie auth is the fallback the
  // backend already supports for SSE.
  _es = new EventSource('/api/quote/stream', { withCredentials: true });

  _es.addEventListener('snapshot', (e) => {
    try {
      const snap = JSON.parse(e.data);
      // The snapshot event is keyed by tradingsymbol directly (backend
      // includes sym alongside token in tick events; snapshot uses the
      // same sym-keyed shape). If the backend sends {token: ltp} instead,
      // the individual tick events will still populate liveLtp correctly
      // and the snapshot becomes a no-op.
      if (snap && typeof snap === 'object') {
        liveLtp.update(prev => ({ ...prev, ...snap }));
        streamOpen.set(true);
      }
    } catch (_) { /* malformed JSON — ignore */ }
  });

  _es.addEventListener('tick', (e) => {
    try {
      const t = JSON.parse(e.data);
      if (t && t.sym && t.ltp != null) {
        liveLtp.update(prev => {
          // Avoid allocating a new object for every tick when the value
          // hasn't changed — keeps ag-Grid cell-level diffs minimal.
          if (prev[t.sym] === t.ltp) return prev;
          return { ...prev, [t.sym]: t.ltp };
        });
        streamOpen.set(true);
      }
    } catch (_) { /* malformed JSON — ignore */ }
  });

  // Heartbeat keeps the connection alive through proxies/firewalls that
  // close idle TCP connections. No state update needed.
  _es.addEventListener('heartbeat', () => { /* noop */ });

  _es.onerror = () => {
    // EventSource auto-reconnects on error — the browser will retry with
    // exponential back-off. We only flip streamOpen false here so
    // MarketPulse can widen its polling cadence until the stream resumes.
    // The store is flipped back to true by the next snapshot/tick that
    // arrives after reconnection.
    streamOpen.set(false);
    // _opened stays true so that a transient network blip doesn't
    // cause a second EventSource to be created if startQuoteStream()
    // is called again before the auto-reconnect fires.
  };
}

/**
 * Close the SSE connection and reset state.
 * Safe to call even when the stream was never started.
 */
export function stopQuoteStream() {
  if (_es) {
    try { _es.close(); } catch (_) {}
    _es = null;
  }
  _opened = false;
  streamOpen.set(false);
  // liveLtp is intentionally NOT reset — stale values are better than
  // blanks while the component is tearing down (grid cells flash to "—").
}
