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
