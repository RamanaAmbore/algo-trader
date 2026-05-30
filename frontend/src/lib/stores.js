/**
 * Shared app state — auth + client-side data cache.
 *
 * Auth store: persists JWT + user profile in sessionStorage so page reloads
 * keep the session alive. Writable store so any component can react to login/logout.
 *
 * Data cache: plain module-level object (not reactive). Pages write to it after
 * a successful fetch and read from it on mount to show stale-while-revalidate data
 * immediately when navigating back.
 */

import { writable, derived } from 'svelte/store';
import { browser } from '$app/environment';
import { isMarketOpen } from '$lib/marketHours';

// ---------------------------------------------------------------------------
// Auth store
// ---------------------------------------------------------------------------

function _decodeJwt(/** @type {string|null} */ tok) {
  // Decode the payload of a JWT without verification — we only consume
  // claims that downstream UI code already trusts because the JWT was
  // server-signed and accepted at login. Used to refresh `is_super` on
  // every session read, so a stale `ramboq_user` blob (from a login
  // that pre-dated the is_super claim) can't pin the role chip wrong.
  if (!tok) return null;
  try {
    const part = tok.split('.')[1];
    if (!part) return null;
    const padded = part + '='.repeat((4 - part.length % 4) % 4);
    const json   = atob(padded.replace(/-/g, '+').replace(/_/g, '/'));
    return JSON.parse(json);
  } catch { return null; }
}

function _readSession() {
  if (!browser) return { token: null, user: null, impBy: null };
  try {
    const token = sessionStorage.getItem('ramboq_token');
    const raw   = sessionStorage.getItem('ramboq_user');
    let user    = raw ? JSON.parse(raw) : null;
    // impBy is read from the JWT each time so an admin who refreshes
    // a tab mid-impersonation still sees the banner. Set by the
    // claim, not by a separate storage key.
    const _claims = _decodeJwt(token);
    const impBy   = _claims?.imp_by || null;
    if (token && !user) {
      // sessionStorage half-cleared (devtools / partial logout / browser
      // cache hiccup): the token survived but `ramboq_user` is gone.
      // Rebuild `user` from the JWT's own claims so the layout's
      // role-check effect doesn't bounce the operator to /signin on
      // every navbar click. Claims carry sub / role / display_name —
      // enough to reconstruct what the login response would have stored.
      const claims = _decodeJwt(token);
      if (claims) {
        user = {
          username:     claims.sub,
          role:         claims.role,
          display_name: claims.display_name,
        };
        // Re-persist so subsequent reads (and any code that reads
        // sessionStorage directly) see the rebuilt blob too.
        try {
          sessionStorage.setItem('ramboq_user', JSON.stringify(user));
        } catch { /* quota / privacy mode — non-fatal */ }
      }
    } else if (token && user) {
      const claims = _decodeJwt(token);
      if (claims) {
        // JWT wins for role so a stale `ramboq_user` blob can't pin the
        // role chip wrong. role values: 'partner' | 'admin' | 'designated'.
        user.role = claims.role ?? user.role;
        // Defensive: legacy sessions persisted is_super; drop it.
        delete user.is_super;
      }
    }
    return { token, user, impBy };
  } catch {
    return { token: null, user: null, impBy: null };
  }
}

function createAuthStore() {
  const { subscribe, set } = writable(_readSession());

  return {
    subscribe,

    /** Call after successful login. */
    login(token, user) {
      if (browser) {
        sessionStorage.setItem('ramboq_token', token);
        sessionStorage.setItem('ramboq_user', JSON.stringify(user));
      }
      const claims = _decodeJwt(token);
      set({ token, user, impBy: claims?.imp_by || null });
    },

    /** Call on logout or 401. */
    logout() {
      if (browser) {
        sessionStorage.removeItem('ramboq_token');
        sessionStorage.removeItem('ramboq_user');
        sessionStorage.removeItem('ramboq_orig_token');
        sessionStorage.removeItem('ramboq_orig_user');
      }
      set({ token: null, user: null, impBy: null });
    },

    /** Read token directly (non-reactive). */
    getToken() {
      return browser ? sessionStorage.getItem('ramboq_token') : null;
    },

    /** Start an impersonation session. Stashes the current admin
     *  token/user under separate keys so a tab refresh during
     *  impersonation can recover both ends. */
    startImpersonation(token, user) {
      if (browser) {
        const origTok  = sessionStorage.getItem('ramboq_token');
        const origUser = sessionStorage.getItem('ramboq_user');
        if (origTok)  sessionStorage.setItem('ramboq_orig_token', origTok);
        if (origUser) sessionStorage.setItem('ramboq_orig_user', origUser);
        sessionStorage.setItem('ramboq_token', token);
        sessionStorage.setItem('ramboq_user', JSON.stringify(user));
      }
      const claims = _decodeJwt(token);
      set({ token, user, impBy: claims?.imp_by || null });
    },

    /** End the current impersonation. The caller has already POSTed
     *  /stop-impersonate and received a fresh JWT for the original
     *  actor — pass it in. Clears the stashed orig_* keys. */
    stopImpersonation(token, user) {
      if (browser) {
        sessionStorage.setItem('ramboq_token', token);
        sessionStorage.setItem('ramboq_user', JSON.stringify(user));
        sessionStorage.removeItem('ramboq_orig_token');
        sessionStorage.removeItem('ramboq_orig_user');
      }
      set({ token, user, impBy: null });
    },
  };
}

export const authStore = createAuthStore();

// ---------------------------------------------------------------------------
// Data cache — stale-while-revalidate for all data pages
// Each entry: { data, refreshed_at } or null before first fetch.
// ---------------------------------------------------------------------------

export const dataCache = {
  market:    null,   // { content, cycle_date, refreshed_at }
  holdings:  null,   // { rows, summary, refreshed_at }
  positions: null,   // { rows, summary, refreshed_at }
  funds:     null,   // { rows, refreshed_at }
  insights:  null,   // { content }
};

/**
 * Compact page-top timestamp banner. Day-first (20 Apr) to match
 * Indian / British convention; 3-letter weekday + 3-letter month;
 * year dropped; 24-hour time. Example:
 *   "Mon 20 Apr  23:06 IST | Mon 20 Apr  13:36 EDT"
 * Both date halves repeated so IST/EST day-boundary cases stay
 * unambiguous. Auto-resolves EST/EDT by season.
 */
/**
 * setInterval variant that pauses its callback while the browser tab is
 * backgrounded. Use this for every algo-page polling interval — it cuts
 * the per-tab HTTP noise on Dashboard / Agents / Orders / Simulator /
 * Terminal to zero when the user switches away.
 *
 * Returns a teardown function: call it from onDestroy (no separate
 * clearInterval needed).
 *
 * @param {() => void} fn   callback to run on each tick
 * @param {number}      ms  interval in milliseconds
 * @returns {() => void}    teardown; clears the interval + removes the listener
 */
export function visibleInterval(fn, ms) {
  let id = null;
  const start = () => { if (id == null) id = setInterval(fn, ms); };
  const stop  = () => { if (id != null) { clearInterval(id); id = null; } };
  const onVis = () => { document.hidden ? stop() : start(); };
  if (typeof document !== 'undefined') {
    if (!document.hidden) start();
    document.addEventListener('visibilitychange', onVis);
  } else {
    start();
  }
  return () => {
    stop();
    if (typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', onVis);
    }
  };
}

/**
 * visibleInterval + market-hours gate. Use for auto-refresh of any
 * data that ONLY moves during NSE/MCX hours (LTPs, positions P&L,
 * agent fires) — outside the combined 09:00-23:30 IST Mon-Fri window
 * the callback is a no-op so we don't flood the API with refreshes
 * that return identical cached values.
 *
 * The interval timer still runs (we just skip the callback) so the
 * gate re-engages naturally at the next minute boundary as the market
 * opens — no special cross-boundary scheduling needed.
 *
 * The visibility gate (pause when tab hidden) layers on top, same as
 * `visibleInterval`. Manual refresh buttons keep working always —
 * operators clicking a button are explicitly asking for a fetch.
 *
 * @param {() => void} fn   callback to run on each tick
 * @param {number}      ms  interval in milliseconds
 * @returns {() => void}    teardown
 */
export function marketAwareInterval(fn, ms) {
  return visibleInterval(() => {
    if (!isMarketOpen()) return;
    fn();
  }, ms);
}

/** Display label for a git branch name. The `main` branch is the
 *  prod deployment target — operators think in "prod / dev" terms,
 *  not "main / non-main", so the UI surfaces the branch as `prod`
 *  whenever the raw value is `main`. Any other branch name flows
 *  through unchanged (dev branches keep their working name).
 *  Internal predicates (`paperStatus.branch === 'main'`, etc.) keep
 *  using the raw value — this helper is presentation-only. */
/** Global execution-mode store.
 *  Read by OrderTicket on open to set the default mode pill.
 *  Values: 'sim' | 'replay' | 'paper' | 'shadow' | 'live'
 *  Defaults to 'paper'; will be wired to /api/admin/execution/mode
 *  in a follow-up commit. */
export const executionMode = writable(/** @type {'sim'|'replay'|'paper'|'shadow'|'live'} */ ('paper'));

export function branchLabel(/** @type {string|null|undefined} */ name) {
  if (!name) return '';
  return name === 'main' ? 'prod' : name;
}

export function clientTimestamp() {
  const now = new Date();
  // Pull the structured parts once per zone so we can compare dates
  // and selectively elide the duplicate when both zones land on the
  // same calendar day (the common case during market hours). When
  // the dates differ (around UTC midnight) we keep the EST weekday
  // so the operator still sees the date delta — no info lost.
  const parts = (tz) => {
    const arr = new Intl.DateTimeFormat('en-GB', {
      weekday: 'short', day: '2-digit', month: 'short',
      hour: '2-digit', minute: '2-digit', hour12: false,
      timeZone: tz,
    }).formatToParts(now);
    const pick = (t) => (arr.find(p => p.type === t) || {}).value || '';
    return {
      wd: pick('weekday'),
      d:  pick('day'),
      m:  pick('month'),
      h:  pick('hour'),
      mn: pick('minute'),
    };
  };
  const ist = parts('Asia/Kolkata');
  const est = parts('America/New_York');
  const estTz = now.toLocaleTimeString('en-US', {
    timeZoneName: 'short', timeZone: 'America/New_York',
  }).split(' ').pop();   // "EST" / "EDT" by season
  const istHead  = `${ist.wd} ${ist.d} ${ist.m}`;
  const istTime  = `${ist.h}:${ist.mn} IST`;
  const sameDate = ist.d === est.d && ist.m === est.m;
  // Same calendar day → elide weekday/date for EST half.
  // Different day (EST trailing IST by a day) → keep the EST
  // weekday only (short enough to signal the date delta).
  const estHalf  = sameDate
    ? `${est.h}:${est.mn} ${estTz}`
    : `${est.wd} ${est.h}:${est.mn} ${estTz}`;
  return `${istHead} · ${istTime} · ${estHalf}`;
}

// Reactive ticking version of clientTimestamp(). Pages binding `{$nowStamp}`
// get the dual-tz clock string and it updates every 60 s automatically.
// Earlier callsites used `{clientTimestamp()}` which captured the string at
// first render and stayed frozen until the page was navigated away from.
export const nowStamp = writable(browser ? clientTimestamp() : '');
if (browser) {
  setInterval(() => nowStamp.set(clientTimestamp()), 60_000);
}

/** Short DD-MMM HH:MM:SS IST | DD-MMM HH:MM:SS EST/EDT for log entries.
 *  Input: ISO string or Date. EST vs EDT label tracks DST automatically. */
export function logTime(iso) {
  if (!iso) return '';
  const d = typeof iso === 'string' ? new Date(iso) : iso;
  if (isNaN(d)) return '';
  const fmt = (tz) => d.toLocaleString('en-GB', {
    day: '2-digit', month: 'short',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false, timeZone: tz,
  }).replace(',', '');
  // EST in winter, EDT in summer — derive from the date itself.
  const estTz = d.toLocaleTimeString('en-US', {
    timeZoneName: 'short', timeZone: 'America/New_York',
  }).split(' ').pop();
  return `${fmt('Asia/Kolkata')} IST | ${fmt('America/New_York')} ${estTz}`;
}

/** HH:MM:SS in IST. Compact form for inline log rows; pair with
 *  `logTimeEdt` to render a stacked dual-zone cell. */
export function logTimeIst(iso) {
  if (!iso) return '';
  const d = typeof iso === 'string' ? new Date(iso) : iso;
  if (isNaN(d)) return '';
  return d.toLocaleTimeString('en-GB', {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false, timeZone: 'Asia/Kolkata',
  });
}

/** HH:MM:SS in EST/EDT. Compact form for the second line of a
 *  stacked dual-zone log cell. Tracks DST automatically. */
export function logTimeEdt(iso) {
  if (!iso) return '';
  const d = typeof iso === 'string' ? new Date(iso) : iso;
  if (isNaN(d)) return '';
  return d.toLocaleTimeString('en-GB', {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false, timeZone: 'America/New_York',
  });
}

/** Stacked IST | EDT span markup for `{@html}` blocks. Single source of
 *  truth for the dual-zone cell — any CSS class change to .log-ts /
 *  .log-ts-ist / .log-ts-sep / .log-ts-edt happens here, not in N
 *  template files. Returns '' for unparseable input. */
export function dualTsHtml(iso) {
  if (!iso) return '';
  const ist = logTimeIst(iso);
  const edt = logTimeEdt(iso);
  if (!ist && !edt) return '';
  return `<span class="log-ts log-ts-inline"><span class="log-ts-ist">${ist || '—'}</span><span class="log-ts-sep">|</span><span class="log-ts-edt">${edt || '—'}</span></span>`;
}

/**
 * Lifespan chip metadata for an agent row.
 *
 * Returns `null` for `persistent` agents (the default — no chip needed).
 * Otherwise returns `{ label, color, tooltip, expired }`:
 *   - `label`   — short text for the chip ("2/3 fires" / "Until 15 Jun · 28d")
 *   - `color`   — 'sky' / 'amber' / 'red' / 'grey' for CSS class selection
 *   - `tooltip` — full state for hover (used / max / expires / last_triggered)
 *   - `expired` — true when budget exhausted AND agent should no longer fire
 *
 * Color progression:
 *   - 'sky'   — fresh (< 50% used / > 7 days remaining)
 *   - 'amber' — getting low (≥ 50% used / ≤ 7 days remaining)
 *   - 'red'   — 1 fire / 1 day remaining (urgent)
 *   - 'grey'  — exhausted / status=completed
 *
 * @param {any} agent
 * @returns {{ label: string, color: 'sky'|'amber'|'red'|'grey', tooltip: string, expired: boolean } | null}
 */
export function lifespanChip(agent) {
  if (!agent) return null;
  const t = agent.lifespan_type;
  if (!t || t === 'persistent') return null;
  const isCompleted = agent.status === 'completed';

  if (t === 'one_shot') {
    if (isCompleted) {
      return {
        label: '1-shot · DONE',
        color: 'grey',
        tooltip: 'One-shot agent has fired and auto-completed.',
        expired: true,
      };
    }
    return {
      label: '1-shot',
      color: 'sky',
      tooltip: 'Fires once and auto-completes.',
      expired: false,
    };
  }

  if (t === 'n_fires') {
    const max = Number(agent.lifespan_max_fires) || 0;
    const used = Number(agent.trigger_count) || 0;
    const remaining = Math.max(0, max - used);
    if (isCompleted || remaining === 0) {
      return {
        label: `${used}/${max} · EXHAUSTED`,
        color: 'grey',
        tooltip: `Used ${used} of ${max} fires — auto-completed.`,
        expired: true,
      };
    }
    let color = /** @type {'sky'|'amber'|'red'} */ ('sky');
    if (remaining === 1)             color = 'red';
    else if (used / max >= 0.5)      color = 'amber';
    return {
      label: `${used}/${max} fires`,
      color,
      tooltip: `${remaining} of ${max} fires remaining.`,
      expired: false,
    };
  }

  if (t === 'until_date') {
    const expiresAt = agent.lifespan_expires_at;
    if (!expiresAt) {
      return {
        label: 'Until (no date)',
        color: 'grey',
        tooltip: 'until_date agent missing lifespan_expires_at — would never expire.',
        expired: false,
      };
    }
    const exp = new Date(expiresAt);
    const now = new Date();
    const msLeft = exp.getTime() - now.getTime();
    const daysLeft = Math.ceil(msLeft / 86_400_000);
    const dateStr = exp.toLocaleDateString('en-GB', {
      day: '2-digit', month: 'short', timeZone: 'Asia/Kolkata',
    });
    if (isCompleted || msLeft <= 0) {
      return {
        label: `EXPIRED ${dateStr}`,
        color: 'red',
        tooltip: `Expired on ${exp.toLocaleString('en-GB', { timeZone: 'Asia/Kolkata' })} IST.`,
        expired: true,
      };
    }
    let color = /** @type {'sky'|'amber'|'red'} */ ('sky');
    if (daysLeft <= 1)               color = 'red';
    else if (daysLeft <= 7)          color = 'amber';
    return {
      label: `Until ${dateStr} · ${daysLeft}d`,
      color,
      tooltip: `Expires on ${exp.toLocaleString('en-GB', { timeZone: 'Asia/Kolkata' })} IST (in ${daysLeft} day${daysLeft === 1 ? '' : 's'}).`,
      expired: false,
    };
  }

  return null;
}

/** Parse the leading 'YYYY-MM-DD HH:MM:SS[,ms]' timestamp from a python log line
 *  (treated as UTC) and return short IST|EST. Returns null if not found. */
export function parseLogLineTime(line) {
  const m = line?.match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})/);
  if (!m) return null;
  return logTime(`${m[1]}T${m[2]}Z`);
}

/* ── Global order-events store ────────────────────────────────────────
   One poller, one shared state, used by the OrderNotifications bell on
   every algo page. Polls /api/orders/events/recent every 8 s for
   fill / chase_modify / unfill / reject / cancel events. Tracks
   "unread since last bell-open" via a localStorage timestamp so the
   badge count survives page navigation.

   The bell component subscribes to `orderEventsStore` for the event
   list and to `orderUnreadCount` for the badge. Calling
   `markOrderEventsSeen()` clears the badge (writes a new "lastSeen"
   timestamp to localStorage). */

const _ORDER_LS_KEY = 'ramboq.orderEvents.lastSeenTs';

function _loadLastSeen() {
  if (!browser) return 0;
  try {
    const v = localStorage.getItem(_ORDER_LS_KEY);
    const n = v ? Number(v) : 0;
    return Number.isFinite(n) ? n : 0;
  } catch { return 0; }
}

function _saveLastSeen(/** @type {number} */ ts) {
  if (!browser) return;
  try { localStorage.setItem(_ORDER_LS_KEY, String(ts)); } catch { /* ignore */ }
}

/** @typedef {{
 *   id: number, order_id: number, ts: string,
 *   kind: string, message: string,
 *   payload_json: string | null,
 *   tradingsymbol?: string, side?: string, qty?: number,
 *   mode?: string, account?: string,
 * }} OrderEvent */

/** Rolling buffer of the last ~200 order events seen on this client.
 *  Oldest-first within the window. */
export const orderEventsStore = writable(/** @type {OrderEvent[]} */ ([]));

/** Last-seen ts (unix ms) — the bell click handler writes here when
 *  the operator opens the popover. Reactive so the badge updates
 *  immediately on open. */
const _lastSeenStore = writable(_loadLastSeen());

/** Unread count = events with ts > lastSeen. Computed reactively. */
export const orderUnreadCount = derived(
  [orderEventsStore, _lastSeenStore],
  ([$events, $lastSeen]) => {
    if (!$events?.length) return 0;
    let n = 0;
    for (const e of $events) {
      const t = e?.ts ? Date.parse(e.ts) : 0;
      if (t > $lastSeen) n++;
    }
    return n;
  }
);

/** Mark every currently-visible event as seen. Bell calls this on
 *  popover open so the badge clears. */
export function markOrderEventsSeen() {
  const now = Date.now();
  _saveLastSeen(now);
  _lastSeenStore.set(now);
}

let _orderPollerStarted = false;
let _orderPollerTeardown = /** @type {(() => void) | null} */ (null);

/** Start the global poller. Idempotent — safe to call from any page's
 *  onMount; the second+ calls are no-ops. Stopped automatically on
 *  page hide via visibleInterval, restarted on visibility return. */
export function startOrderEventsPoller() {
  if (!browser || _orderPollerStarted) return;
  _orderPollerStarted = true;

  const poll = async () => {
    try {
      // Lazy import to avoid circular dependency between stores.js + api.js.
      const { fetchOrderEvents, fetchOrders } = await import('$lib/api');
      // Fetch BOTH platform chase events (algo_order_events) AND broker
      // orders (Kite order book) in parallel. The bell merges both so it
      // surfaces ANY order activity the operator should know about,
      // including orders the operator placed via Kite mobile or via a
      // path that doesn't write to algo_orders. Without the broker
      // overlay, prod sessions showed an empty bell whenever the
      // operator hadn't placed through OrderTicket since service start.
      const [evRows, brokerOrders] = await Promise.all([
        fetchOrderEvents(50, 'all').catch(() => []),
        fetchOrders().catch(() => []),
      ]);
      const algoEvents = Array.isArray(evRows) ? evRows : [];
      // Synthesise virtual events for every broker order so the panel
      // groups by Kite's order_id. status → kind mapping mirrors the
      // panel's terminal-state CSS palette.
      const synth = (Array.isArray(brokerOrders) ? brokerOrders : (brokerOrders?.rows || []))
        .map((o) => {
          const status = String(o?.status || '').toUpperCase();
          const kind = status === 'COMPLETE' ? 'fill'
                     : status === 'REJECTED' ? 'reject'
                     : status === 'CANCELLED' ? 'cancel'
                     : 'placed';
          return {
            id: `broker-${o.order_id}`,
            order_id: o.order_id,
            ts: o.order_timestamp || o.exchange_timestamp || new Date().toISOString(),
            kind,
            message: `${o.transaction_type} ${o.filled_quantity ?? o.quantity}/${o.quantity} ${o.tradingsymbol} ${o.status} @₹${o.average_price ?? o.price ?? '—'}`,
            payload_json: null,
            tradingsymbol: o.tradingsymbol,
            side: o.transaction_type,
            qty: o.quantity,
            account: o.account,
          };
        });
      // Merge algo events first (placements / chase / fills) + broker
      // orders. Dedup by order_id+kind so a broker COMPLETE doesn't
      // double-up when the platform also recorded a "fill" event.
      const seen = new Set();
      const merged = [];
      for (const e of [...algoEvents, ...synth]) {
        const key = `${e.order_id}|${e.kind}`;
        if (seen.has(key)) continue;
        seen.add(key);
        merged.push(e);
      }
      // Sort by ts ascending so the panel's "oldest-first within window"
      // contract is preserved.
      merged.sort((a, b) => String(a.ts || '').localeCompare(String(b.ts || '')));
      orderEventsStore.set(merged);
    } catch { /* swallow — no toasts on failure */ }
  };

  // Fire once immediately so the badge is correct on first paint.
  poll();
  _orderPollerTeardown = visibleInterval(poll, 8000);
}

/** Stop the poller. Only used during logout / signout to release the
 *  interval cleanly. */
export function stopOrderEventsPoller() {
  if (_orderPollerTeardown) {
    _orderPollerTeardown();
    _orderPollerTeardown = null;
  }
  _orderPollerStarted = false;
}

/* ── Global agent-events store ───────────────────────────────────────
   Mirror of the order-events plumbing for agent fires + action
   success/error events. One shared poller per session, badge counter
   keyed off a separate localStorage timestamp so order + agent
   unread counts move independently.

   Backs the AgentNotifications bell on every algo page (sibling to
   OrderNotifications). Same poll cadence (8 s) and pause-on-hide
   behaviour via visibleInterval. */

const _AGENT_LS_KEY = 'ramboq.agentEvents.lastSeenTs';

function _loadAgentLastSeen() {
  if (!browser) return 0;
  try {
    const v = localStorage.getItem(_AGENT_LS_KEY);
    const n = v ? Number(v) : 0;
    return Number.isFinite(n) ? n : 0;
  } catch { return 0; }
}

function _saveAgentLastSeen(/** @type {number} */ ts) {
  if (!browser) return;
  try { localStorage.setItem(_AGENT_LS_KEY, String(ts)); } catch { /* ignore */ }
}

/** @typedef {{
 *   id: number, agent_id: number, event_type: string,
 *   trigger_condition: string | null, detail: string | null,
 *   timestamp: string, sim_mode: boolean,
 * }} AgentEvent */

/** Rolling buffer of the last ~100 agent events. The /agents/events
 *  endpoint returns newest-first; we keep that order so the bell
 *  popover's "most recent at top" needs no reversal. */
export const agentEventsStore = writable(/** @type {AgentEvent[]} */ ([]));

const _agentLastSeenStore = writable(_loadAgentLastSeen());

export const agentUnreadCount = derived(
  [agentEventsStore, _agentLastSeenStore],
  ([$events, $lastSeen]) => {
    if (!$events?.length) return 0;
    let n = 0;
    for (const e of $events) {
      const t = e?.timestamp ? Date.parse(e.timestamp) : 0;
      if (t > $lastSeen) n++;
    }
    return n;
  }
);

export function markAgentEventsSeen() {
  const now = Date.now();
  _saveAgentLastSeen(now);
  _agentLastSeenStore.set(now);
}

let _agentPollerStarted = false;
let _agentPollerTeardown = /** @type {(() => void) | null} */ (null);

export function startAgentEventsPoller() {
  if (!browser || _agentPollerStarted) return;
  _agentPollerStarted = true;

  const poll = async () => {
    try {
      const { fetchRecentAgentEvents } = await import('$lib/api');
      const rows = await fetchRecentAgentEvents(50);
      if (Array.isArray(rows)) agentEventsStore.set(rows);
    } catch { /* swallow */ }
  };
  poll();
  _agentPollerTeardown = visibleInterval(poll, 8000);
}

export function stopAgentEventsPoller() {
  if (_agentPollerTeardown) {
    _agentPollerTeardown();
    _agentPollerTeardown = null;
  }
  _agentPollerStarted = false;
}

// ── Connection-status store ──────────────────────────────────────────
// Global broker-account loaded/total counts surfaced as a badge on
// every RefreshButton. Polled once globally (every 15 s) so each card's
// refresh icon shows the same state without per-page re-fetching.
// Earlier dashboard fetched this via `_fetchConn()` for its CONN hero
// chip; that chip is gone but the connection-state semantics live on,
// merged into the refresh affordance.
//
// Shape: { loaded: number, total: number }
//   loaded === total  → green badge
//   0 < loaded < total → amber badge
//   loaded === 0       → red badge
//   total === 0        → no badge (no broker config / demo mode)
export const connStatus = writable(/** @type {{loaded:number,total:number}} */ ({ loaded: 0, total: 0 }));

let _connPollerStarted = false;
let _connPollerTeardown = null;
export function startConnStatusPoller() {
  if (!browser || _connPollerStarted) return;
  _connPollerStarted = true;
  const poll = async () => {
    try {
      const { fetchBrokerAccounts } = await import('$lib/api');
      const accounts = await fetchBrokerAccounts();
      if (!Array.isArray(accounts)) return;
      connStatus.set({
        total:  accounts.length,
        loaded: accounts.filter((a) => a?.loaded).length,
      });
    } catch { /* leave stale */ }
  };
  poll();
  _connPollerTeardown = visibleInterval(poll, 15000);
}

export function stopConnStatusPoller() {
  if (_connPollerTeardown) {
    _connPollerTeardown();
    _connPollerTeardown = null;
  }
  _connPollerStarted = false;
}
