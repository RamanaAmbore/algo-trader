// chartRefreshPulse — "data just landed" visual cue for chart containers.
//
// Wrapper div gets a CSS class that fires a cyan bg keyframe
// (rgba(125,211,252,0.10) → transparent over 300ms). Applied to the chart's
// outer HTML wrapper so the pulse reads at the card's padding edges.
//
// CSS is global (lives in app.css) so it reaches inside Svelte scoped DOM
// without :global() wrappers. This file only manages class-name state.
//
// Usage:
//   const pulse = createChartRefreshPulse();
//   // In component:
//   $effect(() => {
//     if (bars.length) pulse.notify('chart');
//   });
//   <div class="my-chart-wrapper {pulse.classOf('chart')}">…</div>
//
// Why a/b toggle instead of class-remove+re-add:
//   Browser ignores classList.add('cp-pulse-a') when the class is already
//   present — no animation restart. Alternating between 'cp-pulse-a' and
//   'cp-pulse-b' forces a class-name change on every successive notify, so
//   the keyframe always restarts.
//
// Rate-limiting (250ms):
//   A burst of rapid data-land events (e.g. SSE flush) produces at most
//   one pulse per 250ms per key. The underlying state is always updated;
//   only the visual class write is suppressed during the cooldown window.
//   This matches the tickBus throttle budget in tickFlash.svelte.js.
//
// Tab-hidden guard:
//   notify() no-ops when document.visibilityState === 'hidden'. The CSS
//   animation won't run in a hidden tab; leaving the class set produces
//   orphaned state that never clears if the tab stays hidden.

/**
 * @param {{ durationMs?: number, throttleMs?: number }} options
 */
export function createChartRefreshPulse({ durationMs = 300, throttleMs = 250 } = {}) {
  /** @type {Record<string, 'a' | 'b' | ''>} */
  let _state = $state({});
  /** @type {Record<string, ReturnType<typeof setTimeout>>} */
  const _timers = {};
  /** @type {Record<string, number>} — epoch of last accepted pulse per key */
  const _lastAt = {};

  /**
   * Signal that a chart just received new data. Applies one of two
   * alternating CSS classes to trigger the keyframe animation.
   * @param {string} key  — stable identifier for this chart instance
   */
  function notify(key) {
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
    const now = Date.now();
    const last = _lastAt[key] ?? 0;
    if (now - last < throttleMs) return;
    _lastAt[key] = now;

    // Alternate a/b to force keyframe restart even on rapid successive fires.
    const next = _state[key] === 'a' ? 'b' : 'a';
    _state[key] = next;

    if (_timers[key]) clearTimeout(_timers[key]);
    _timers[key] = setTimeout(() => {
      _state[key] = '';
      delete _timers[key];
    }, durationMs);
  }

  /**
   * Returns the CSS class to bind on the chart wrapper:
   * 'cp-pulse-a', 'cp-pulse-b', or '' (idle).
   * @param {string} key
   */
  function classOf(/** @type {string} */ key) {
    const v = _state[key];
    if (v === 'a') return 'cp-pulse-a';
    if (v === 'b') return 'cp-pulse-b';
    return '';
  }

  function dispose() {
    for (const t of Object.values(_timers)) clearTimeout(t);
  }

  return { notify, classOf, dispose };
}
