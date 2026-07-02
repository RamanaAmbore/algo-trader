// Architecture (a): createTickFlash, createFreshnessShimmer, and createTickBus
// live here as sibling exports — they share the change-detection comparator
// pattern and a common timer-cleanup idiom, so one file is cleaner than two.

/**
 * Tick-flash helper — emits transient 'up' / 'down' classes when a
 * tracked value moves by more than `threshold`. Lets poll-driven cells
 * (Spot, LTP, P&L, Day %) communicate liveness with a brief background
 * pulse instead of silently changing digits the operator misses.
 *
 *   const flash = createTickFlash({ threshold: 0.5, durationMs: 350 });
 *   $effect(() => flash.update('NIFTY', _underlyingQuotes.NIFTY?.ltp));
 *   <span class="num {flash.classOf('NIFTY')}">{ltp}</span>
 *
 * Why threshold matters — without a delta gate every tick paints the
 * grid and the operator's eye stops registering any of them (ambient
 * noise). For 30 s polling this is less acute, but a threshold still
 * suppresses re-renders that don't move the operator's number.
 */
export function createTickFlash({ threshold = 0, durationMs = 350 } = {}) {
  /** @type {Record<string, number>} */
  const prev = {};
  /** @type {Record<string, any>} */
  const timers = {};
  /** @type {Record<string, 'up'|'down'|''>} */
  let classes = $state({});

  /** @param {string} key @param {number|null|undefined} value */
  function update(key, value) {
    if (value == null) return;
    const v = Number(value);
    if (!isFinite(v)) return;
    const last = prev[key];
    prev[key] = v;
    // First sample — establish baseline, no flash. Without this every
    // mount would flash every cell from null → value.
    if (last == null) return;
    if (Math.abs(v - last) < threshold) return;
    // Tab-hidden guard: state (prev[key]) is always updated above so we
    // track the latest value even when hidden. But we skip the CSS class
    // write + timer — the animation won't run in a hidden tab, and setting
    // classes while hidden leaves orphaned 'tf-up'/'tf-down' markers that
    // never clear if the tab stays hidden past durationMs.
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
    const dir = v > last ? 'up' : 'down';
    // Mutate the $state proxy in place. The earlier `classes = { ...
    // classes, [key]: dir }` form READ `classes` inside the same call-
    // stack the consumer's $effect triggered — Svelte 5 then picked
    // up `classes` as a dep, the write rescheduled the effect, and
    // the page hung with effect_update_depth_exceeded. Direct prop
    // assignment on the $state proxy is a pure write, no implicit
    // read of the parent object.
    classes[key] = dir;
    if (timers[key]) clearTimeout(timers[key]);
    timers[key] = setTimeout(() => {
      classes[key] = '';
      delete timers[key];
    }, durationMs);
  }

  /** Return the CSS class name for a key — '', 'tf-up', or 'tf-down'. */
  function classOf(/** @type {string} */ key) {
    const c = classes[key];
    return c === 'up' ? 'tf-up' : c === 'down' ? 'tf-down' : '';
  }

  function dispose() {
    for (const t of Object.values(timers)) clearTimeout(t);
  }

  return {
    get classes() { return classes; },
    update,
    classOf,
    dispose,
  };
}

/**
 * Freshness-shimmer helper — emits a transient 'cell-freshness-pulse' class
 * on a tracked key whenever a new value arrives from the data stream,
 * regardless of whether the value actually changed. Distinct from
 * createTickFlash (directional) — this is a neutral "data just landed" signal.
 *
 * CSS contract: .cell-freshness-pulse::after renders the 1px gradient underline
 * sweep. Defined in app.css so ag-Grid :global selectors can reach it.
 *
 *   const shimmer = createFreshnessShimmer({ durationMs: 700 });
 *   // Call notify(key) whenever a fresh value arrives for that key.
 *   shimmer.notify('NIFTY');
 *   // In cellClass: `shimmer.classOf('NIFTY')`
 *
 * Tab-hidden guard: if document.visibilityState === 'hidden' when notify()
 * is called, the class is not applied — the CSS animation won't run in a
 * hidden tab, so we save the class churn and avoid orphaned classes if the
 * tab stays hidden past the cleanup window.
 *
 * @param {{ durationMs?: number }} options
 */
export function createFreshnessShimmer({ durationMs = 700 } = {}) {
  /** @type {Record<string, any>} */
  const timers = {};
  /** @type {Record<string, boolean>} */
  let active = $state({});

  /**
   * Signal that key just received fresh data. Applies the shimmer class
   * for durationMs, then removes it.
   * @param {string} key
   */
  function notify(key) {
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
    // Set active — triggers reactive reads in cellClass.
    active[key] = true;
    if (timers[key]) clearTimeout(timers[key]);
    timers[key] = setTimeout(() => {
      active[key] = false;
      delete timers[key];
    }, durationMs);
  }

  /**
   * Batch-notify multiple keys at once (e.g. after a snapshot diff).
   * @param {Iterable<string>} keys
   */
  function notifyAll(keys) {
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
    for (const k of keys) notify(k);
  }

  /** Return the CSS class name for a key — '' or 'cell-freshness-pulse'. */
  function classOf(/** @type {string} */ key) {
    return active[key] ? 'cell-freshness-pulse' : '';
  }

  function dispose() {
    for (const t of Object.values(timers)) clearTimeout(t);
  }

  return {
    get active() { return active; },
    notify,
    notifyAll,
    classOf,
    dispose,
  };
}

/**
 * createTickBus — single-origin fanout for directional LTP events.
 *
 * Consumers subscribe and receive {sym, dir: 'up'|'down'|'flat', at: number}
 * on every real symbol LTP change. Throttled at the bus edge to 250ms per sym
 * so a 100-tick/sec SSE burst fires at most 4 events/sec per symbol downstream.
 *
 * Direction is computed by the CALLER (symbolStore._mergeSymbolWrite knows both
 * prev and next ltp at write time — cheapest place to diff). The bus only
 * throttles, timestamps, and fans out. No LTP state duplication here.
 *
 *   const bus = createTickBus({ throttleMs: 250 });
 *   bus.subscribe(({ sym, dir, at }) => { ... });
 *   // In the write path:
 *   bus.emit('NIFTY50', 'up');
 *
 * @param {{ throttleMs?: number }} options
 */
export function createTickBus({ throttleMs = 250 } = {}) {
  /** @type {Map<string, number>} — last-emit epoch per sym (for throttle) */
  const _lastAt = new Map();
  /** @type {Set<(e: {sym: string, dir: 'up'|'down'|'flat', at: number}) => void>} */
  const _subs = new Set();

  /**
   * Emit a tick event for a symbol. Direction MUST be provided by the caller
   * (who has prev + next LTP available at write time). Dropped if the sym
   * was emitted within throttleMs — the underlying LTP state has still been
   * updated; only the flash signal is suppressed.
   *
   * Tab-hidden guard: no-op when document.visibilityState === 'hidden' so
   * the animation class never gets applied + cleared in a hidden tab.
   *
   * @param {string} sym
   * @param {'up'|'down'|'flat'} dir
   */
  function emit(sym, dir) {
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
    if (!sym) return;
    const key = String(sym).toUpperCase();
    const now  = Date.now();
    const last = _lastAt.get(key) ?? 0;
    if (now - last < throttleMs) return;
    _lastAt.set(key, now);
    const event = { sym: key, dir, at: now };
    for (const fn of _subs) {
      try { fn(event); } catch { /* subscriber errors must not break the bus */ }
    }
  }

  /**
   * Subscribe to tick events. Returns an unsub function.
   * @param {(e: {sym: string, dir: 'up'|'down'|'flat', at: number}) => void} fn
   * @returns {() => void}
   */
  function subscribe(fn) {
    _subs.add(fn);
    return () => { _subs.delete(fn); };
  }

  /** Size of the subscriber set — for diagnostics. */
  function subscriberCount() { return _subs.size; }

  return { emit, subscribe, subscriberCount };
}
