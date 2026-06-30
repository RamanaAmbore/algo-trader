/**
 * quoteStream.js — SSE-backed live LTP feed.
 *
 * Connects to GET /api/quote/stream and maintains one reactive primitive:
 *   streamOpen  — writable store: true when the SSE connection has received
 *                 at least one snapshot/tick without a subsequent error
 *
 * SSE ticks + snapshots are written into the symbol-centric symbolStore
 * via mergeSymbolUpdate / mergeSymbolBatch. Consumers read live LTPs via
 * `getSnapshot(sym).ltp` instead of subscribing to a separate `liveLtp`
 * writable. The legacy `liveLtp` store was deleted in BH3 — its only
 * remaining consumer (MarketPulse's throttled cell-renderer mirror)
 * was migrated to read from symbolStore + symbolTickCount.
 *
 * Usage (inside a Svelte component):
 *   import { streamOpen, startQuoteStream, stopQuoteStream } from '$lib/data/quoteStream';
 *   onMount(() => startQuoteStream());
 *   onDestroy(() => stopQuoteStream());
 *   import { getSnapshot } from '$lib/data/symbolStore.svelte.js';
 *   // in a cell renderer: getSnapshot('RELIANCE')?.ltp ?? fallback
 *
 * Singleton pattern — multiple components calling startQuoteStream() share one
 * connection. stopQuoteStream() closes it and resets so the next startQuoteStream()
 * call opens a fresh connection.
 */

import { writable, get } from 'svelte/store';
import { browser } from '$app/environment';
import { mergeSymbolUpdate, mergeSymbolBatch } from './symbolStore.svelte.js';
import { isNseOpen, isMcxOpen, fetchMarketStatus } from '../marketHours.js';

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
        /** @type {Array<{sym: string, fields: {ltp: number}, ts: {ltp_ts: number}}>} */
        const symbolUpdates = [];
        const ts = Date.now();
        // Backend ticker.snapshot() returns `{token: {ltp, sym}}` keyed
        // by integer token. Transform to per-sym updates so the central
        // store joins on tradingsymbol like every other consumer.
        for (const v of Object.values(snap)) {
          if (!v || typeof v !== 'object' || !v.sym || v.ltp == null) continue;
          // LTP flicker fix (Jun 2026): skip non-positive ltp values
          // BEFORE they hit symbolStore. The backend's _on_ticks now
          // filters at the source but this defensive guard protects
          // against any future SSE payload regression that re-introduces
          // 0/negative prices. Critical because the ltp_ts arbitration
          // in symbolStore would otherwise block subsequent positive
          // polls from overwriting a 0-stamped-fresh entry.
          const v_ltp = Number(v.ltp);
          if (!Number.isFinite(v_ltp) || v_ltp <= 0) continue;
          symbolUpdates.push({
            sym: v.sym, fields: { ltp: v_ltp }, ts: { ltp_ts: ts },
          });
        }
        // ltp_ts arbitration means a tick already newer-by-ms can't be
        // clobbered by a re-snapshot landing later.
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
        // LTP flicker fix (Jun 2026): same zero-guard as snapshot path.
        // Skip non-positive LTP at SSE intake; the backend already
        // filters but the frontend is the last line of defence and
        // cheap to check.
        const t_ltp = Number(t.ltp);
        if (!Number.isFinite(t_ltp) || t_ltp <= 0) return;
        // BH3: writes only land in symbolStore. ltp_ts = Date.now() at
        // receive time arbitrates correctly against any poll that
        // lands afterward carrying older broker-side LTP.
        mergeSymbolUpdate(t.sym, { ltp: t_ltp }, { ltp_ts: Date.now() });
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

// ---------------------------------------------------------------------------
// Per-exchange polling gate (slice Market-Lifecycle).
//
// Operator: "when the market is closed, the frontend should not even
// bother to refresh ticks. broker updates position data, cash, etc. that
// is the only that needs to be updated. no tick data refresh."
//
// Implementation: a separate 30s watcher checks isNseOpen() || isMcxOpen()
// + fetches the server-side market status every 30s. When NEITHER is open
// we close the SSE (stop ticks); when transitioning to open we reopen it.
// SSE auto-reconnect handles re-subscription internally.
// ---------------------------------------------------------------------------

/** @type {ReturnType<typeof setInterval> | null} */
let _gateInterval = null;
let _gateActive = false;

/**
 * Start the per-exchange gate. Idempotent. Call once from the algo
 * layout `onMount`. When called, ALSO starts the quote stream if any
 * market segment is open.
 */
export function startMarketGatedQuoteStream() {
  if (!browser || _gateActive) return;
  _gateActive = true;
  // Boot decision: if anything is open right now, start the stream.
  if (isNseOpen() || isMcxOpen()) {
    startQuoteStream();
  }
  // Watcher — 30s cadence (cheap; just isNseOpen/isMcxOpen reads after
  // fetchMarketStatus). Re-checks on every tick whether we should be
  // streaming or paused.
  let _prevAny = isNseOpen() || isMcxOpen();
  _gateInterval = setInterval(async () => {
    try {
      try { await fetchMarketStatus(); } catch { /* silent */ }
      const anyOpen = isNseOpen() || isMcxOpen();
      if (anyOpen && !_prevAny) {
        // Closed → open transition: reopen the stream.
        startQuoteStream();
      } else if (!anyOpen && _prevAny) {
        // Open → closed transition: stop ticks. Broker fetches for
        // positions / cash / holdings continue via their own poll
        // paths; only the live LTP SSE is gated here.
        stopQuoteStream();
      }
      _prevAny = anyOpen;
    } catch (_) {
      /* silent — next tick retries */
    }
  }, 30_000);
}

/** Tear down the gate watcher (companion to stopQuoteStream). */
export function stopMarketGatedQuoteStream() {
  if (_gateInterval != null) {
    clearInterval(_gateInterval);
    _gateInterval = null;
  }
  _gateActive = false;
  stopQuoteStream();
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
  // symbolStore is intentionally NOT reset — stale values are better
  // than blanks while the component is tearing down (grid cells flash
  // to "—" otherwise).
}
