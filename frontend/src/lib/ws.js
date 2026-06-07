/**
 * WebSocket client — connects to /ws/performance and emits update events.
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
 */

const WS_PATH = '/ws/performance';
const WS_ALGO_PATH = '/ws/algo';

/**
 * Opens a WebSocket connection and calls `onMessage` for each performance
 * update event. Returns an unsub function that closes the socket.
 *
 * @param {(msg: object) => void} onMessage
 * @returns {() => void} cleanup function
 */
export function createPerformanceSocket(onMessage) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const host  = import.meta.env.DEV
    ? 'localhost:8000'   // bypass Vite WS proxy limitation — connect directly
    : location.host;
  const url = `${proto}://${host}${WS_PATH}`;

  let socket = null;
  let pingInterval = null;
  let closed = false;
  let reconnectTimer = null;
  // Exponential back-off: start at 2s, double on each failure up
  // to 60s. Resets to 2s after a successful `open`. Earlier code
  // hard-coded 2s — when the server returned 405 (e.g. dev
  // mistakenly orange-clouded behind Cloudflare which blocks raw
  // WS upgrades), the close handler immediately re-fired every 2s,
  // producing ~30 errors/min, constant console-noise, and steady
  // background CPU. Backoff caps the storm without giving up on
  // recovery.
  let reconnectDelayMs = 2_000;

  function connect() {
    if (closed) return;
    socket = new WebSocket(url);

    socket.addEventListener('open', () => {
      // Heartbeat: send ping every 25 s
      pingInterval = setInterval(() => {
        if (socket?.readyState === WebSocket.OPEN) socket.send('ping');
      }, 25_000);
      reconnectDelayMs = 2_000;  // reset back-off on successful connect
    });

    socket.addEventListener('message', (e) => {
      if (e.data === 'pong') return; // heartbeat reply — ignore
      try {
        const msg = JSON.parse(e.data);
        if (msg?.event) onMessage(msg);
      } catch {
        // ignore non-JSON frames
      }
    });

    socket.addEventListener('close', () => {
      clearInterval(pingInterval);
      if (!closed) {
        reconnectTimer = setTimeout(connect, reconnectDelayMs);
        // Double for next attempt (cap 60s)
        reconnectDelayMs = Math.min(reconnectDelayMs * 2, 60_000);
      }
    });

    socket.addEventListener('error', () => {
      socket?.close();
    });
  }

  connect();

  return function unsub() {
    closed = true;
    clearInterval(pingInterval);
    clearTimeout(reconnectTimer);
    socket?.close();
  };
}

/**
 * Opens a WebSocket connection to /ws/algo and calls `onMessage` for each
 * event the agent engine broadcasts. Same reconnect / heartbeat behaviour
 * as createPerformanceSocket — pulled out so the in-app notification
 * surface (AgentToast / AgentFireModal) can subscribe independently of
 * the performance feed.
 *
 * @param {(msg: object) => void} onMessage
 * @returns {() => void} cleanup function
 */
export function createAlgoSocket(onMessage) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const host  = import.meta.env.DEV
    ? 'localhost:8000'
    : location.host;
  const url = `${proto}://${host}${WS_ALGO_PATH}`;

  let socket = null;
  let pingInterval = null;
  let closed = false;
  let reconnectTimer = null;
  // Exponential back-off — see comment above the primary subscriber.
  let reconnectDelayMs = 2_000;

  function connect() {
    if (closed) return;
    socket = new WebSocket(url);

    socket.addEventListener('open', () => {
      pingInterval = setInterval(() => {
        if (socket?.readyState === WebSocket.OPEN) socket.send('ping');
      }, 25_000);
      reconnectDelayMs = 2_000;
    });

    socket.addEventListener('message', (e) => {
      if (e.data === 'pong') return;
      try {
        const msg = JSON.parse(e.data);
        if (msg?.event) onMessage(msg);
      } catch {}
    });

    socket.addEventListener('close', () => {
      clearInterval(pingInterval);
      if (!closed) {
        reconnectTimer = setTimeout(connect, reconnectDelayMs);
        reconnectDelayMs = Math.min(reconnectDelayMs * 2, 60_000);
      }
    });

    socket.addEventListener('error', () => { socket?.close(); });
  }

  connect();

  return function unsub() {
    closed = true;
    clearInterval(pingInterval);
    clearTimeout(reconnectTimer);
    socket?.close();
  };
}
