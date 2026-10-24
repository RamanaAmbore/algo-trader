/**
 * brokerHealthState.js — pure, framework-free helpers for broker-health
 * chip color + the `ticker_idle` calendar-gated-connect flag (2026-10-24).
 *
 * Deliberately has no `$app`/svelte-store imports so it can be unit-tested
 * directly (see `frontend/src/lib/__tests__/data/brokerHealthState.test.js`)
 * instead of only via a hand-maintained replica — `stores.js` and both the
 * desktop/mobile broker-chip sites in `+layout.svelte` call these exports,
 * so a test against this module exercises the real production logic.
 *
 * `ticker_idle` is a NEW, SEPARATE concept from each account's own `state`
 * (green/amber/red/inactive — REST-fetch auth health, deliberately NOT
 * market-gated). `ticker_idle=true` means "the KiteTicker WebSocket was
 * never attempted today, by calendar design (market confirmed closed)" —
 * it is not a failure signal and must never be folded into `worstState`.
 * See CLAUDE.md's "Broker auth health badge" entry and the backend
 * `get_ticker_calendar_status()` docstring (backend/api/background.py).
 */

/**
 * @typedef {{ account: string, broker: string, state: string, reason: string, last_good_at: string|null, last_check_at: string|null, is_active_ticker?: boolean, circuit_state?: string, consecutive_fail_count?: number, circuit_open_until?: string|null, circuit_breaker_enabled?: boolean, poll_priority?: string, auto_downgrade_enabled?: boolean, auto_downgraded_at?: string|null, auto_downgrade_reason?: string|null }} BrokerAccountHealth
 */

/**
 * @typedef {{ accounts: BrokerAccountHealth[], worstState: 'green'|'amber'|'red', tickerIdle: boolean, tickerIdleReason: string }} BrokerHealthState
 */

/**
 * Derive worst state across all accounts.
 * red > amber > green; returns 'amber' for empty (never grey).
 * Moved verbatim from `stores.js` (2026-10-24) — the sole owner of this
 * logic now; `stores.js` imports it rather than keeping its own copy.
 *
 * Param type is deliberately the minimal `{ state?: string }[]` (only
 * field this function reads), not the full `BrokerAccountHealth[]` — lets
 * callers/tests pass bare `{ state }` fixtures without needing every
 * other account field.
 * @param {Array<{ state?: string }>} accounts
 * @returns {'green'|'amber'|'red'}
 */
export function brokerHealthWorstState(accounts) {
  if (!accounts || !accounts.length) return 'green';
  const active = accounts.filter(a => a.state !== 'inactive');
  const base = active.length ? active : accounts;
  if (base.some(a => a.state === 'red'))   return 'red';
  if (base.some(a => a.state === 'amber')) return 'amber';
  if (base.every(a => a.state === 'green' || a.state === 'inactive')) return 'green';
  // all inactive
  return 'green';
}

/**
 * Map a raw `GET /api/admin/broker-health` response into the
 * `brokerHealthStore` shape. Missing `ticker_idle`/`ticker_idle_reason`
 * (older cached response shape, or a transport error swallowed upstream)
 * default to not-idle / empty reason — absence must never read as "idle".
 * @param {any} data
 * @returns {BrokerHealthState}
 */
export function toBrokerHealthState(data) {
  const accounts = /** @type {BrokerAccountHealth[]} */ (data?.accounts ?? []);
  return {
    accounts,
    worstState: brokerHealthWorstState(accounts),
    tickerIdle: !!data?.ticker_idle,
    tickerIdleReason: String(data?.ticker_idle_reason ?? ''),
  };
}

/**
 * Resolve the broker-chip's visual state, combining `worstState` (account
 * auth health) with `tickerIdle` (calendar-gated ticker-connect status).
 *
 * Precedence: red > idle > amber > green.
 *   - red always wins — "idle must never mask a real red failure on
 *     another account" (explicit invariant, see test file).
 *   - idle beats amber: on a confirmed-closed day, every account reads
 *     amber by design (stale — nothing is actively polling), which is
 *     EXPECTED staleness, not a problem. If amber beat idle, the idle
 *     chip would never render in the one scenario it exists for.
 *   - amber beats green as before.
 *
 * `worstState` outside the known {red,amber,green} set (defensive —
 * mirrors the layout's own ternary fallback) resolves to 'partial'.
 * @param {'green'|'amber'|'red'|string} worstState
 * @param {boolean} tickerIdle
 * @returns {'down'|'idle'|'partial'|'ok'}
 */
export function brokerChipState(worstState, tickerIdle) {
  if (worstState === 'red') return 'down';
  if (tickerIdle) return 'idle';
  if (worstState === 'green') return 'ok';
  // 'amber' and any unrecognized value.
  return 'partial';
}
