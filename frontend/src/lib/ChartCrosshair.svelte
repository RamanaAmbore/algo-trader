<!--
  ChartCrosshair.svelte

  Canonical hover-crosshair for every hand-rolled SVG chart in this app
  (no charting library — ChartWorkspace, OptionsPayoff, PriceChart,
  EquityCurve, MultiPriceChart, dashboard, SimulatorPanel). Before this
  component existed, each chart wrote its own inline crosshair <line>/
  <circle> markup — a 2026-10 audit found 4 different stroke colors, 2
  widths, and 3 dash patterns across the 7 charts. This component is the
  single source of truth for that look.

  DESIGN INTENT — READ BEFORE ADDING A PROP:
  The crosshair LINE styling (stroke rgba(251,191,36,0.5), stroke-width 1,
  stroke-dasharray "3 2" — ChartWorkspace's own pre-existing look, named
  canonical by the operator) is HARDCODED below and is NOT a prop. Every
  chart's crosshair line must look pixel-identical; only the following
  vary per call site:
    - which axis line(s) to draw                 (`mode`)
    - whether a dot marks the hover point         (`showDot`)
    - the dot's own fill/outline colors           (`dotColor` / `dotStroke`)
  A future caller with a P&L-colored dot (e.g. EquityCurve, green/red by
  sign) should use `dotColor`/`dotStroke` — do NOT add a prop that lets
  the LINE's color/width/dash vary. That would silently reintroduce the
  exact per-chart drift this component exists to eliminate.

  RENDER CONTRACT: no own <svg> wrapper — bare <line>/<circle> elements
  inside a <g>, meant to be rendered as a direct child of the CALLER's
  own <svg> so it inherits that SVG's coordinate system untouched.
-->
<script>
  /**
   * @typedef {{ top: number, bottom: number, left: number, right: number }} Bounds
   */
  let {
    /** @type {number | null} */
    x = null,
    /** @type {number | null} */
    y = null,
    /** @type {Bounds} */
    bounds,
    /** @type {'vertical' | 'horizontal' | 'both'} */
    mode = 'vertical',
    showDot = true,
    dotColor = '#fbbf24',
    dotStroke = '#fff',
  } = $props();
</script>

<g class="chart-crosshair" aria-hidden="true">
  {#if (mode === 'vertical' || mode === 'both') && x != null}
    <line x1={x} x2={x} y1={bounds.top} y2={bounds.bottom}
          stroke="rgba(251,191,36,0.5)" stroke-width="1" stroke-dasharray="3 2"/>
  {/if}
  {#if (mode === 'horizontal' || mode === 'both') && y != null}
    <line x1={bounds.left} x2={bounds.right} y1={y} y2={y}
          stroke="rgba(251,191,36,0.5)" stroke-width="1" stroke-dasharray="3 2"/>
  {/if}
  {#if showDot && x != null && y != null}
    <circle cx={x} cy={y} r="3" fill={dotColor} stroke={dotStroke} stroke-width="1"/>
  {/if}
</g>
