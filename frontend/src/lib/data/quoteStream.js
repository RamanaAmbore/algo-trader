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

import { writable, get } from 'svelte/store';
import { browser } from '$app/environment';
import { mergeSymbolUpdate, mergeSymbolBatch } from './symbolStore.svelte.js';

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
let _stopped = false;
/** @type {ReturnType<typeof setTimeout> | null} */
let _reconnectTimer = null;
let _backoffMs = 2_000;
const _BACKOFF_MIN = 2_000;
const _BACKOFF_MAX = 60_000;

/**
 * Open the SSE connection (idempotent — safe to call multiple times).
 * Must only be called in a browser context (onMount or behind `if (browser)`).
 *
 * Native EventSource auto-reconnects at a fixed ~3s interval. On a
 * non-retryable error (e.g. 401 after token expiry) the browser keeps that
 * loop running forever. We close + reopen manually with exponential backoff
 * capped at 60s instead.
 */
export function startQuoteStream() {
  if (!browser || _opened) return;
  _opened = true;
  _stopped = false;
  _open();
}

function _open() {
  if (_stopped) return;
  // withCredentials sends the auth cookie — same auth path as every other
  // /api/* call. EventSource does not support arbitrary request headers so
  // bearer-token auth cannot be used here; cookie auth is the fallback the
  // backend already supports for SSE.
  _es = new EventSource('/api/quote/stream', { withCredentials: true });

  _es.addEventListener('snapshot', (e) => {
    try {
      const snap = JSON.parse(e.data);
      // Backend ticker.snapshot() returns `{token: {ltp, sym}}` keyed
      // by integer token. Earlier code assumed the snap was already
      // `{sym: ltp}` and merged it directly, which polluted liveLtp
      // with stringified-token keys and the actual sym → ltp pairs
      // never landed (operator saw stale "—" on every cell until a
      // later tick caught up). Transform to `{sym: ltp}` so ag-Grid
      // can read `$liveLtp[sym]` and paint immediately on first load.
      if (snap && typeof snap === 'object') {
        /** @type {Record<string, number>} */
        const symMap = {};
        /** @type {Array<{sym: string, fields: {ltp: number}, ts: {ltp_ts: number}}>} */
        const symbolUpdates = [];
        const ts = Date.now();
        for (const v of Object.values(snap)) {
          if (v && typeof v === 'object' && v.sym && v.ltp != null) {
            symMap[v.sym] = v.ltp;
            symbolUpdates.push({
              sym: v.sym, fields: { ltp: Number(v.ltp) }, ts: { ltp_ts: ts },
            });
          }
        }
        if (Object.keys(symMap).length) {
          liveLtp.update(prev => ({ ...prev, ...symMap }));
        }
        // BH1 dual-write — symbolStore receives the same LTPs SSE
        // delivers to liveLtp. ltp_ts arbitration ensures a tick already
        // newer-by-ms can't be clobbered by a re-snapshot landing later.
        if (symbolUpdates.length) mergeSymbolBatch(symbolUpdates);
        if (!get(streamOpen)) streamOpen.set(true);
        _backoffMs = _BACKOFF_MIN;
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
        // BH1 dual-write — symbolStore.ltp gets the same tick. ltp_ts =
        // Date.now() at receive time; arbitrates correctly against any
        // poll that lands afterward carrying older broker-side LTP.
        mergeSymbolUpdate(t.sym, { ltp: Number(t.ltp) }, { ltp_ts: Date.now() });
        if (!get(streamOpen)) streamOpen.set(true);
        _backoffMs = _BACKOFF_MIN;
      }
    } catch (_) { /* malformed JSON — ignore */ }
  });

  // Heartbeat keeps the connection alive through proxies/firewalls that
  // close idle TCP connections. No state update needed.
  _es.addEventListener('heartbeat', () => { /* noop */ });

  _es.onerror = () => {
    streamOpen.set(false);
    // Close the broken EventSource and schedule a manual reopen with
    // exponential backoff. Native EventSource would otherwise retry at
    // its own ~3s cadence indefinitely, including on non-retryable
    // 401/403 responses after token expiry.
    if (_es) {
      try { _es.close(); } catch (_) {}
      _es = null;
    }
    if (_stopped) return;
    if (_reconnectTimer != null) return;
    const delay = _backoffMs;
    _backoffMs = Math.min(_backoffMs * 2, _BACKOFF_MAX);
    _reconnectTimer = setTimeout(() => {
      _reconnectTimer = null;
      _open();
    }, delay);
  };
}

/**
 * Tear down the current SSE connection and re-open a fresh one. Used by
 * the HARD refresh-cycle reset path so the backend's recycled ticker
 * delivers a clean snapshot into the freshly-cleared frontend symbolStore.
 * Safe to call while a connection is mid-open; a backoff retry is
 * cancelled if pending.
 */
export function restartQuoteStream() {
  if (!browser) return;
  if (_reconnectTimer != null) {
    clearTimeout(_reconnectTimer);
    _reconnectTimer = null;
  }
  if (_es) {
    try { _es.close(); } catch (_) {}
    _es = null;
  }
  _backoffMs = _BACKOFF_MIN;
  streamOpen.set(false);
  // _opened stays true so a concurrent startQuoteStream() doesn't open
  // a second connection alongside ours.
  if (!_opened) _opened = true;
  _stopped = false;
  _open();
}

/**
 * Close the SSE connection and reset state.
 * Safe to call even when the stream was never started.
 */
export function stopQuoteStream() {
  _stopped = true;
  if (_reconnectTimer != null) {
    clearTimeout(_reconnectTimer);
    _reconnectTimer = null;
  }
  if (_es) {
    try { _es.close(); } catch (_) {}
    _es = null;
  }
  _opened = false;
  _backoffMs = _BACKOFF_MIN;
  streamOpen.set(false);
  // liveLtp is intentionally NOT reset — stale values are better than
  // blanks while the component is tearing down (grid cells flash to "—").
}
