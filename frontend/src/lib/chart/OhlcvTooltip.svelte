<!--
  OhlcvTooltip — absolute-positioned hover popup for the OHLCV price chart.

  Shows Open / High / Low / Close, optional Volume, and the bar's change (Δ).
  When pinned (after a chart click) a × button lets the operator dismiss.
  All tooltip styles are global in app.css (.chart-tooltip family).
-->
<script>
  import { priceFmt } from '$lib/format';

  let {
    /** @type {object} OHLCV bar — must have open/high/low/close/ts; volume optional. */
    bar,
    /** @type {number} CSS left offset in pixels, relative to the chart container. */
    pxLeft,
    /** @type {number} CSS top offset in pixels, relative to the chart container. */
    pxTop,
    /** @type {boolean} Whether the tooltip is pinned (click-locked). */
    pinned,
    /** @type {() => void} Called when the × close button is clicked. */
    onClose,
  } = $props();

  /**
   * Format a bar timestamp string as "D Mon YYYY".
   * @param {string} ts
   */
  function _fmtBarTs(ts) {
    if (!ts) return '';
    const d = new Date(ts);
    if (!Number.isFinite(d.getTime())) return ts.slice(0, 10);
    const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return `${d.getDate()} ${MON[d.getMonth()]} ${d.getFullYear()}`;
  }
</script>

{#snippet _renderTooltip()}
  {@const ch = Number(bar.close) - Number(bar.open)}
  {@const pct = Number(bar.open) ? (ch / Number(bar.open)) * 100 : 0}
  <div class="chart-tooltip" class:chart-tooltip-pinned={pinned}
       style="left: {pxLeft}px; top: {pxTop}px;">
    {#if pinned}
      <button type="button" class="chart-tooltip-close"
              aria-label="Close pinned popup"
              title="Close (or click the chart again)"
              onclick={(e) => { e.stopPropagation(); onClose(); }}>×</button>
    {/if}
    <div class="chart-tooltip-ts">{_fmtBarTs(bar.ts)}</div>
    <div class="chart-tooltip-row">
      <span class="chart-tooltip-label">O</span>
      <span class="chart-tooltip-value">₹{priceFmt(bar.open)}</span>
      <span class="chart-tooltip-label">H</span>
      <span class="chart-tooltip-value">₹{priceFmt(bar.high)}</span>
    </div>
    <div class="chart-tooltip-row">
      <span class="chart-tooltip-label">L</span>
      <span class="chart-tooltip-value">₹{priceFmt(bar.low)}</span>
      <span class="chart-tooltip-label">C</span>
      <span class="chart-tooltip-value">₹{priceFmt(bar.close)}</span>
    </div>
    {#if bar.volume}
      <div class="chart-tooltip-row">
        <span class="chart-tooltip-label">Vol</span>
        <span class="chart-tooltip-value">{Number(bar.volume).toLocaleString()}</span>
      </div>
    {/if}
    <div class="chart-tooltip-row">
      <span class="chart-tooltip-label">Δ</span>
      <span class="chart-tooltip-value" class:up={ch >= 0} class:down={ch < 0}>
        {ch >= 0 ? '+' : ''}{ch.toFixed(2)} ({pct.toFixed(2)}%)
      </span>
    </div>
  </div>
{/snippet}

{@render _renderTooltip()}
