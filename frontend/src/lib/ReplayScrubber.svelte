<script>
  // ReplayScrubber — horizontal range slider that scrubs through the
  // captured tick history. Industry pattern: TradingView Strategy
  // Tester / QuantConnect Lean Live Log both let you drag a playhead
  // back through any captured moment to inspect the state at that
  // timestamp. Our equivalent: the slider drives a shared scrubbedTs
  // prop on every chart in the Lab card; each chart renders a vertical
  // anchor line + per-series value at that moment.
  //
  // Two modes:
  //   - Live  (value = null): the charts follow the most-recent tick,
  //                            polling refreshes append new points.
  //   - Scrub (value = ts):   the charts pin the anchor to that
  //                            historical timestamp; new ticks still
  //                            land but the anchor stays put until the
  //                            operator releases.

  /** @type {{
   *   timestamps?: string[],
   *   value?:      string | null,
   *   onChange?:   (ts: string | null) => void,
   *   title?:      string,
   * }} */
  const { timestamps = [], value = null, onChange = () => {},
          title = 'Replay' } = $props();

  // Slider input is index-based — easier to drive a range element
  // than ISO strings. Index = position in `timestamps` array.
  const _maxIdx = $derived(Math.max(0, timestamps.length - 1));
  const _currentIdx = $derived.by(() => {
    if (value == null) return _maxIdx;
    const i = timestamps.indexOf(value);
    return i < 0 ? _maxIdx : i;
  });

  const _liveMode = $derived(value == null);

  function _onInput(/** @type {Event} */ ev) {
    const target = /** @type {HTMLInputElement} */ (ev.currentTarget);
    const idx = Number(target.value);
    if (Number.isNaN(idx)) return;
    onChange(timestamps[idx] ?? null);
  }
  function _goLive() { onChange(null); }
  function _step(/** @type {number} */ delta) {
    const next = Math.min(_maxIdx, Math.max(0, _currentIdx + delta));
    if (next === _maxIdx) onChange(null);
    else onChange(timestamps[next]);
  }

  function _fmtTs(/** @type {string} */ ts) {
    try {
      const d = new Date(ts);
      const hh = String(d.getHours()).padStart(2, '0');
      const mm = String(d.getMinutes()).padStart(2, '0');
      const ss = String(d.getSeconds()).padStart(2, '0');
      return `${hh}:${mm}:${ss}`;
    } catch { return ts; }
  }
</script>

<div class="rs-shell">
  <div class="rs-header">
    <span class="rs-title">{title}</span>
    {#if _liveMode}
      <span class="rs-mode rs-mode-live">▶ LIVE</span>
    {:else}
      <span class="rs-mode rs-mode-scrub">⏸ SCRUB</span>
    {/if}
    <span class="rs-pos">
      {timestamps.length ? `${_currentIdx + 1} / ${timestamps.length}` : '0 / 0'}
    </span>
    <span class="rs-ts">{timestamps[_currentIdx] ? _fmtTs(timestamps[_currentIdx]) : '—'}</span>
    <div class="rs-actions">
      <button type="button" class="rs-btn"
              disabled={!timestamps.length || _currentIdx <= 0}
              onclick={() => _step(-1)} title="Step back one tick">◀</button>
      <button type="button" class="rs-btn"
              disabled={!timestamps.length || _currentIdx >= _maxIdx}
              onclick={() => _step(1)} title="Step forward one tick">▶</button>
      <button type="button" class="rs-btn rs-btn-live"
              class:on={_liveMode}
              disabled={!timestamps.length}
              onclick={_goLive}
              title="Snap back to live (most-recent tick)">LIVE</button>
    </div>
  </div>
  <input type="range" class="rs-slider"
         min="0" max={_maxIdx}
         value={_currentIdx}
         disabled={!timestamps.length}
         oninput={_onInput}
         aria-label="Scrub through captured tick history" />
</div>

<style>
  .rs-shell {
    background: rgba(15, 25, 45, 0.55);
    border: 1px solid rgba(126, 151, 184, 0.18);
    border-radius: 4px;
    padding: 0.4rem 0.6rem;
    margin: 0.3rem 0 0.4rem;
    width: 100%;
    max-width: 960px;
    box-sizing: border-box;
  }
  .rs-header {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    margin-bottom: 0.25rem;
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
  }
  .rs-title {
    color: var(--algo-slate);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-weight: 700;
  }
  .rs-mode {
    font-size: 0.55rem;
    font-weight: 800;
    letter-spacing: 0.06em;
    padding: 0.08rem 0.4rem;
    border-radius: 999px;
  }
  .rs-mode-live {
    background: rgba(74,222,128,0.15);
    border: 1px solid rgba(74,222,128,0.45);
    color: #4ade80;
  }
  .rs-mode-scrub {
    background: rgba(251,191,36,0.15);
    border: 1px solid rgba(251,191,36,0.45);
    color: #fbbf24;
  }
  .rs-pos {
    color: var(--algo-muted);
    font-variant-numeric: tabular-nums;
  }
  .rs-ts {
    color: #f1f7ff;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    margin-left: auto;
  }
  .rs-actions {
    display: inline-flex;
    gap: 0.2rem;
  }
  .rs-btn {
    padding: 0.15rem 0.45rem;
    border: 1px solid rgba(126, 151, 184, 0.32);
    background: transparent;
    color: var(--algo-slate);
    font-family: ui-monospace, monospace;
    font-size: 0.55rem;
    font-weight: 700;
    cursor: pointer;
    border-radius: 3px;
    min-width: 1.4rem;
  }
  .rs-btn:hover:not(:disabled) {
    border-color: rgba(251,191,36,0.6);
    color: #fbbf24;
  }
  .rs-btn:disabled { opacity: 0.35; cursor: not-allowed; }
  .rs-btn-live.on {
    border-color: #4ade80;
    color: #4ade80;
    background: rgba(74,222,128,0.10);
  }
  .rs-slider {
    width: 100%;
    height: 1rem;
    margin: 0;
    accent-color: #fbbf24;
  }
  .rs-slider:disabled { opacity: 0.35; cursor: not-allowed; }
</style>
