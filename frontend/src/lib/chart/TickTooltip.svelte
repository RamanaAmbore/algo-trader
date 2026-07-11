<!--
  TickTooltip — absolute-positioned hover popup for the intraday tick chart.

  Shows LTP and, when available, Bid/Ask spread.
  When pinned (after a click) a × button lets the operator dismiss.
  All tooltip styles are global in app.css (.chart-tooltip family).
-->
<script>
  import { priceFmt } from '$lib/format';

  let {
    /** @type {object} Tick object — must have ltp/ts; bid and ask are optional. */
    tick,
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
   * Format a tick timestamp string as "HH:MM:SS IST".
   * @param {string} ts
   */
  function _fmtTickTs(ts) {
    if (!ts) return '';
    const d = new Date(ts);
    if (!Number.isFinite(d.getTime())) return ts.slice(11, 19) || ts;
    const hh = String(d.getHours()).padStart(2, '0');
    const mm = String(d.getMinutes()).padStart(2, '0');
    const ss = String(d.getSeconds()).padStart(2, '0');
    return `${hh}:${mm}:${ss} IST`;
  }
</script>

<div class="chart-tooltip" class:chart-tooltip-pinned={pinned}
     style="left: {pxLeft}px; top: {pxTop}px;">
  {#if pinned}
    <button type="button" class="chart-tooltip-close"
            aria-label="Close pinned popup"
            title="Close (or click the chart again)"
            onclick={(e) => { e.stopPropagation(); onClose(); }}>×</button>
  {/if}
  <div class="chart-tooltip-ts">{_fmtTickTs(tick.ts)}</div>
  <div class="chart-tooltip-row">
    <span class="chart-tooltip-label">LTP</span>
    <span class="chart-tooltip-value">₹{priceFmt(tick.ltp)}</span>
  </div>
  {#if tick.bid != null && tick.ask != null}
    <div class="chart-tooltip-row">
      <span class="chart-tooltip-label">Bid</span>
      <span class="chart-tooltip-value">₹{priceFmt(tick.bid)}</span>
      <span class="chart-tooltip-label">Ask</span>
      <span class="chart-tooltip-value">₹{priceFmt(tick.ask)}</span>
    </div>
  {/if}
</div>
