/**
 * WebSocket client — connects to /ws/performance and /ws/algo and emits
 * update events.
 *
 * Usage (Svelte component):
 *   import { createPerformanceSocket } from '$lib/ws';
 *
 *   let unsub;
 *   onMount(() => {
 *     unsub = createPerformanceSocket((msg) => {
 *       // msg.event === 'performance_updated'
 *       // msg.refreshed_at — display timestamp
 *       invalidateAll(); // or call queryClient.invalidateQueries()
 *     });
 *     return () => unsub();
 *   });
 *
 * Singleton fan-out (Perf audit Jul 2026)
 * ---------------------------------------
 * Each `createPerformanceSocket` / `createAlgoSocket` call USED to open a
 * fresh `new WebSocket(...)` — algo pages with three independent
 * subscribers (MarketPulse, derivatives, bookChanged singleton, orders,
 * performance) wound up with 3-5 parallel WS connections to the same
 * endpoint, each running its own 25 s heartbeat ping and triggering its
 * own onmessage parse path. On dev the server-side resource cost was
 * cheap, but in the browser the fan-out wasted scheduler slots on
 * redundant `JSON.parse` work per tick.
 *
 * Now: one shared socket per endpoint, ref-counted subscribers. The
 * socket opens on the first subscribe and closes once the last
 * subscriber unsubs. The reconnect / backoff / heartbeat code lives in
 * the shared connection — every subscriber sees the same stream.
 *
 * Contract preserved: `createPerformanceSocket(fn)` returns an `unsub`.
 * Calling unsub releases one reference; the socket itself stays open
 * while any other subscriber holds a reference.
 */

const WS_PATH      = '/ws/performance';
const WS_ALGO_PATH = '/ws/algo';

/**
 * @typedef {object} SocketPool
 * @property {Set<(msg: object) => void>} subs
 * @property {WebSocket | null} socket
 * @property {ReturnType<typeof setInterval> | null} pingInterval
 * @property {ReturnType<typeof setTimeout> | null} reconnectTimer
 * @property {number} reconnectDelayMs
 * @property {boolean} closed
 */

/** @type {Map<string, SocketPool>} */
const _pools = new Map();

function _poolFor(/** @type {string} */ path) {
  let pool = _pools.get(path);
  if (pool) return pool;
  pool = /** @type {SocketPool} */ ({
    subs: new Set(),
    socket: null,
    pingInterval: null,
    reconnectTimer: null,
    reconnectDelayMs: 2_000,
    closed: false,
  });
  _pools.set(path, pool);
  return pool;
}

function _ensureConnected(/** @type {string} */ path) {
  const pool = _poolFor(path);
  if (pool.socket || pool.closed) return;
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const host  = import.meta.env.DEV
    ? 'localhost:8000'   // bypass Vite WS proxy limitation — connect directly
    : location.host;
  const url = `${proto}://${host}${path}`;

  const socket = new WebSocket(url);
  pool.socket = socket;

  socket.addEventListener('open', () => {
    // Heartbeat: send ping every 25 s. ONE per shared socket — previously
    // each subscriber's own socket ran its own heartbeat (3-5 pings/25s
    // per tab on a typical algo page).
    pool.pingInterval = setInterval(() => {
      if (pool.socket?.readyState === WebSocket.OPEN) pool.socket.send('ping');
    }, 25_000);
    pool.reconnectDelayMs = 2_000;  // reset back-off on successful connect
  });

  socket.addEventListener('message', (e) => {
    if (e.data === 'pong') return; // heartbeat reply — ignore
    let msg;
    try {
      msg = JSON.parse(e.data);
    } catch {
      return; // non-JSON frame
    }
    if (!msg?.event) return;
    // Fan-out to every subscriber. defensive copy so a subscriber that
    // calls unsub during dispatch doesn't mutate the Set mid-iteration.
    const subs = Array.from(pool.subs);
    for (const fn of subs) {
      try { fn(msg); }
      catch (err) { console.warn('[ws] subscriber threw:', err); }
    }
  });

  socket.addEventListener('close', () => {
    if (pool.pingInterval) { clearInterval(pool.pingInterval); pool.pingInterval = null; }
    pool.socket = null;
    if (pool.closed) return;
    // Only reconnect while we still have at least one subscriber.
    if (pool.subs.size === 0) return;
    pool.reconnectTimer = setTimeout(() => _ensureConnected(path), pool.reconnectDelayMs);
    pool.reconnectDelayMs = Math.min(pool.reconnectDelayMs * 2, 60_000);
  });

  socket.addEventListener('error', () => {
    socket?.close();
  });
}

function _subscribe(/** @type {string} */ path, /** @type {(msg: object) => void} */ fn) {
  const pool = _poolFor(path);
  pool.closed = false;        // re-arm if a prior teardown nuked us
  pool.subs.add(fn);
  _ensureConnected(path);
  return function unsub() {
    pool.subs.delete(fn);
    if (pool.subs.size === 0) {
      // Last subscriber gone — close the socket to free server resources
      // (the conn-service tracks open WS connections + would otherwise
      // hold this slot until process restart). The pool record stays so
      // a future subscriber can resurrect it without ceremony.
      pool.closed = true;
      if (pool.reconnectTimer) { clearTimeout(pool.reconnectTimer); pool.reconnectTimer = null; }
      if (pool.pingInterval) { clearInterval(pool.pingInterval); pool.pingInterval = null; }
      try { pool.socket?.close(); } catch {}
      pool.socket = null;
    }
  };
}

/**
 * Subscribe to /ws/performance. Returns an unsub function.
 *
 * @param {(msg: object) => void} onMessage
 * @returns {() => void} cleanup function
 */
export function createPerformanceSocket(onMessage) {
  return _subscribe(WS_PATH, onMessage);
}

/**
 * Subscribe to /ws/algo (agent engine events). Returns an unsub function.
 *
 * @param {(msg: object) => void} onMessage
 * @returns {() => void} cleanup function
 */
export function createAlgoSocket(onMessage) {
  return _subscribe(WS_ALGO_PATH, onMessage);
}
