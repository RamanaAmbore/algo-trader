/**
 * spreadGate.js — reusable pre-submission bid/ask spread-threshold gate.
 *
 * Built for the Chain tab's "original + offset leg" spread check
 * (operator: "it should be in a loop until the conditions are satisfied
 * before placing the order"), but deliberately framework-agnostic —
 * no Svelte imports, no DOM access — so future agent/automation code
 * can reuse the same loop + spread math without pulling in UI.
 *
 * Two independent pieces:
 *   1. `evaluateLegSpread()`     — pure bid/ask → spread% / ok calc.
 *      Matches the AUTHORITATIVE backend formula
 *      (`backend.api.algo.spread_check.evaluate_spread`, mirroring
 *      `template_attach._ta_wing_depth_spread`'s existing liquidity
 *      filter): `ltp` is preferred as the denominator when positive,
 *      falling back to the bid/ask midpoint otherwise. The live Chain
 *      tab gate does NOT call this — it trusts `GET /api/orders/
 *      spread-check`'s own `ok`/`spread_pct` fields directly (that's
 *      the authoritative calc, run against a live broker quote this
 *      module has no access to). This export exists so a future
 *      offline/local-fallback caller computes the SAME number the
 *      backend would, rather than inventing a second formula.
 *   2. `resolveWingTradingsymbol()` — same strike-selection math as the
 *      backend's `_wing_symbol()` (template_attach.py): CE wing is
 *      +offset, PE wing is -offset, in strike points. Offset of 0 is a
 *      valid ATM wing (matches backend's `is not None` check, not
 *      truthiness). Manual-offset wings only — a `wing_premium_pct`
 *      (chain-scan) wing's real strike depends on a server-side scan
 *      against the parent's fill/reference price; callers needing that
 *      resolved symbol pre-fill must ask the backend (e.g. `POST
 *      /api/orders/ticket/preview`'s `plan.wing.tradingsymbol`), not
 *      this function.
 *   3. `createSpreadGate()`      — the poll/retry/timeout state machine.
 *
 * `createSpreadGate` never auto-submits an order itself — it only
 * reports phase transitions via `onUpdate`; the caller (OptionChainTab)
 * owns what "passed" actually does.
 */

/** Default re-check interval while the gate is open (ms). */
export const SPREAD_GATE_POLL_MS = 4000;
/** Per-attempt network timeout (ms) — mirrors the Chain tab's own
 *  `_refreshChainQuotes` 10s AbortController convention. */
export const SPREAD_GATE_FETCH_TIMEOUT_MS = 10_000;
/** Consecutive failures before the gate reports a bounded 'error' state
 *  instead of retrying forever. */
export const SPREAD_GATE_MAX_ERRORS = 3;
/** Total wall-clock budget before the gate reports 'timeout' and stops
 *  polling on its own — operator must Retry / Place anyway / Cancel.
 *  Never auto-submits past this point. */
export const SPREAD_GATE_MAX_WAIT_MS = 120_000;

/** Matches the backend's `_OPT_SYM_RE` shape closely enough for the
 *  frontend's own strike-offset math (root + expiry token + strike + CE/PE). */
const _OPT_SYM_RE = /^([A-Z]+?)(\d{2}[A-Z]{3}|\d{4,5})(\d+(?:\.\d+)?)(CE|PE)$/;

/**
 * Pure bid/ask → spread%/ok calculation — mirrors the backend's
 * `evaluate_spread()` exactly: `ltp` is the preferred denominator
 * (same reference price the wing-scan's own liquidity filter scores
 * against); falls back to the bid/ask midpoint only when `ltp` is
 * absent/non-positive. `basis` reports which one was actually used,
 * same as the backend response's own `basis` field.
 *
 * `ok` is `null` (not yet known) when bid/ask aren't a valid crossed
 * quote yet (e.g. poll hasn't landed) — callers should keep the gate
 * open and retry rather than treating that as a pass OR a hard failure.
 *
 * @param {{bid?: number|null, ask?: number|null, ltp?: number|null, maxSpreadPct: number}} args
 * @returns {{ok: boolean|null, spread_pct: number|null, bid: number|null, ask: number|null, basis: 'ltp'|'mid'|null}}
 */
export function evaluateLegSpread({ bid, ask, ltp, maxSpreadPct }) {
  const b = Number(bid);
  const a = Number(ask);
  if (!(b > 0) || !(a > 0) || a < b) {
    return {
      ok: null, spread_pct: null, basis: null,
      bid: Number.isFinite(b) && b > 0 ? b : null,
      ask: Number.isFinite(a) && a > 0 ? a : null,
    };
  }
  const l = Number(ltp);
  const useLtp = Number.isFinite(l) && l > 0;
  const basis = useLtp ? 'ltp' : 'mid';
  const denom = useLtp ? l : (a + b) / 2;
  const spread_pct = denom > 0 ? ((a - b) / denom) * 100 : null;
  const threshold = Number(maxSpreadPct);
  const ok = spread_pct == null || !Number.isFinite(threshold) ? null : spread_pct <= threshold;
  return { ok, spread_pct, bid: b, ask: a, basis: spread_pct == null ? null : basis };
}

/**
 * Compute the protective/offset wing's tradingsymbol from the parent's
 * option tradingsymbol + a manual strike offset (points). Returns null
 * when the parent symbol isn't a recognisable option contract, the
 * offset isn't a finite number, or the resulting strike would be <= 0.
 *
 * Mirrors `_wing_symbol()` in `backend/api/algo/template_attach.py` —
 * keep both in sync if the strike-selection convention ever changes.
 *
 * @param {string} parentSymbol
 * @param {number|null|undefined} offset
 * @returns {string|null}
 */
export function resolveWingTradingsymbol(parentSymbol, offset) {
  if (offset == null) return null;
  const off = Number(offset);
  if (!Number.isFinite(off)) return null;
  const sym = String(parentSymbol || '').toUpperCase();
  const m = sym.match(_OPT_SYM_RE);
  if (!m) return null;
  const [, root, expTok, strikeStr, opt] = m;
  const strike = parseFloat(strikeStr);
  if (!Number.isFinite(strike)) return null;
  const wingStrike = opt === 'CE' ? strike + off : strike - off;
  if (wingStrike <= 0) return null;
  const strikeOut = Number.isInteger(wingStrike) ? String(wingStrike) : String(wingStrike);
  return `${root}${expTok}${strikeOut}${opt}`;
}

/**
 * @typedef {Object} SpreadGateLegResult
 * @property {string} label
 * @property {string} tradingsymbol
 * @property {boolean|null} ok
 * @property {number|null} spread_pct
 * @property {number|null} bid
 * @property {number|null} ask
 * @property {number} maxSpreadPct
 */

/**
 * @typedef {Object} SpreadGateState
 * @property {'idle'|'checking'|'wide'|'error'|'timeout'|'passed'|'overridden'|'cancelled'} phase
 * @property {SpreadGateLegResult[]} legs
 * @property {number} attempts
 * @property {string} lastError
 */

/**
 * Create a bounded poll/retry loop around an async `checkLegs()` call.
 *
 * `checkLegs({signal}) => Promise<{ok: boolean, legs: SpreadGateLegResult[]}>`
 * — caller supplies the actual network calls (one per leg); this module
 * only owns the scheduling/bounds/staleness logic around it.
 *
 * Contract:
 *   - Never overlaps requests (setTimeout chain, not setInterval).
 *   - Each attempt gets its own AbortSignal, timed out at `fetchTimeoutMs`.
 *   - `maxErrors` consecutive failures → phase 'error' (loop stops;
 *     caller must call `retry()`, `confirmOverride()`, or `cancel()`).
 *   - `maxWaitMs` total elapsed → phase 'timeout' (loop stops; NEVER
 *     auto-submits past this point — same three options as 'error').
 *   - A generation counter discards any in-flight response after
 *     `recheck()`, `confirmOverride()`, or `cancel()` is called — no
 *     stale result can land after the caller has moved on.
 *
 * @param {{
 *   checkLegs: (opts: {signal: AbortSignal}) => Promise<{ok: boolean, legs: SpreadGateLegResult[]}>,
 *   onUpdate?: (state: SpreadGateState) => void,
 *   pollMs?: number,
 *   fetchTimeoutMs?: number,
 *   maxErrors?: number,
 *   maxWaitMs?: number,
 * }} opts
 */
export function createSpreadGate({
  checkLegs,
  onUpdate,
  pollMs = SPREAD_GATE_POLL_MS,
  fetchTimeoutMs = SPREAD_GATE_FETCH_TIMEOUT_MS,
  maxErrors = SPREAD_GATE_MAX_ERRORS,
  maxWaitMs = SPREAD_GATE_MAX_WAIT_MS,
}) {
  let _gen = 0;
  let _timer = /** @type {any} */ (null);
  let _controller = /** @type {AbortController|null} */ (null);
  let _startedAt = 0;
  let _errorCount = 0;
  /** @type {SpreadGateState} */
  let _state = { phase: 'idle', legs: [], attempts: 0, lastError: '' };

  function _emit(/** @type {Partial<SpreadGateState>} */ partial) {
    _state = { ..._state, ...partial };
    onUpdate?.(_state);
  }

  function _clearTimer() {
    if (_timer) { clearTimeout(_timer); _timer = null; }
  }

  function _abortInFlight() {
    _controller?.abort();
    _controller = null;
  }

  function _isTerminal() {
    return ['passed', 'overridden', 'cancelled'].includes(_state.phase);
  }

  async function _tick(/** @type {number} */ gen) {
    if (gen !== _gen) return;
    const ac = new AbortController();
    _controller = ac;
    const to = setTimeout(() => ac.abort(), fetchTimeoutMs);
    _emit({ phase: 'checking', attempts: _state.attempts + 1 });
    let result;
    try {
      result = await checkLegs({ signal: ac.signal });
    } catch (e) {
      clearTimeout(to);
      if (gen !== _gen) return; // superseded while awaiting
      _errorCount += 1;
      const msg = String(/** @type {any} */ (e)?.message || e || 'spread check failed');
      if (_errorCount >= maxErrors) {
        _emit({ phase: 'error', lastError: msg });
        _clearTimer();
        return;
      }
      _emit({ lastError: msg });
      _scheduleNext(gen);
      return;
    }
    clearTimeout(to);
    if (gen !== _gen) return; // superseded while awaiting
    _errorCount = 0;
    if (result?.ok) {
      _emit({ phase: 'passed', legs: result.legs || [], lastError: '' });
      _clearTimer();
      return;
    }
    _emit({ phase: 'wide', legs: result?.legs || [], lastError: '' });
    _scheduleNext(gen);
  }

  function _scheduleNext(/** @type {number} */ gen) {
    if (gen !== _gen) return;
    if (Date.now() - _startedAt >= maxWaitMs) {
      _emit({ phase: 'timeout' });
      _clearTimer();
      return;
    }
    _clearTimer();
    _timer = setTimeout(() => _tick(gen), pollMs);
  }

  /** Begin (or restart from scratch) the check loop. */
  function start() {
    _gen += 1;
    const gen = _gen;
    _startedAt = Date.now();
    _errorCount = 0;
    _state = { phase: 'checking', legs: [], attempts: 0, lastError: '' };
    _tick(gen);
  }

  /** Immediate re-check with current inputs (TP%/SL%/Spread%/wing
   *  params changed) — does NOT reset the overall `maxWaitMs` budget,
   *  so a flurry of edits can't indefinitely extend the gate. */
  function recheck() {
    if (_isTerminal()) return;
    _gen += 1;
    const gen = _gen;
    _clearTimer();
    _abortInFlight();
    _tick(gen);
  }

  /** Operator explicitly confirms "place anyway" (or disabled the
   *  template mid-wait, which has the same "stop blocking" effect). */
  function confirmOverride() {
    _gen += 1;
    _clearTimer();
    _abortInFlight();
    _emit({ phase: 'overridden' });
  }

  /** Operator cancels the submission, or the caller is tearing down
   *  (component destroy / navigated away) — no dangling timers after. */
  function cancel() {
    _gen += 1;
    _clearTimer();
    _abortInFlight();
    _emit({ phase: 'cancelled' });
  }

  /** Resume polling from the bounded 'error' / 'timeout' state. */
  function retry() {
    if (_state.phase !== 'error' && _state.phase !== 'timeout') return;
    _errorCount = 0;
    _startedAt = Date.now();
    _gen += 1;
    const gen = _gen;
    _clearTimer();
    _abortInFlight();
    _tick(gen);
  }

  function getState() { return _state; }

  return { start, recheck, confirmOverride, cancel, retry, getState };
}
