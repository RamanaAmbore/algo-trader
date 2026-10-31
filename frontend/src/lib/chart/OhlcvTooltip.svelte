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
   * Format a bar timestamp string as "D Mon YYYY", or "D Mon YYYY HH:MM"
   * for an intraday bar (5-minute/etc — carries a real time-of-day).
   * @param {string} ts
   */
  function _fmtBarTs(ts) {
    if (!ts) return '';
    // Daily bars carry a date-only string (YYYY-MM-DD, 10 chars).
    // new Date("YYYY-MM-DD") parses as UTC midnight — for IST (+5:30)
    // users this renders as the previous calendar day. Force local-time
    // parsing by appending T00:00:00 (no timezone → local midnight).
    const isDateOnly = typeof ts === 'string' && ts.length === 10;
    const d = isDateOnly ? new Date(ts + 'T00:00:00') : new Date(ts);
    if (!Number.isFinite(d.getTime())) return ts.slice(0, 10);
    const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    const datePart = `${d.getDate()} ${MON[d.getMonth()]} ${d.getFullYear()}`;
    // 2026-10-31 — intraday bars (1D range, 5-minute interval) carry a
    // full ISO timestamp, not a bare date. Dropping the time entirely
    // made every bar on a 1D chart show the same tooltip date with no
    // way to tell them apart. Shown explicitly in IST (not the browser's
    // local timezone) — trading happens in IST, same convention as
    // PriceChart's fmtTime.
    if (isDateOnly) return datePart;
    let timePart;
    try {
      timePart = d.toLocaleTimeString('en-GB', {
        hour: '2-digit', minute: '2-digit',
        hour12: false, timeZone: 'Asia/Kolkata',
      });
    } catch (_) {
      timePart = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
    }
    return `${datePart} ${timePart}`;
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
