<!--
  LoadingSkeleton — animated shimmer block for loading states.

  Replaces bare "Loading…" text with a visually structured placeholder
  that matches the card layout the real content will occupy. The shimmer
  animation gives ambient liveness feedback without demanding attention.

  Variants:
    block     — stacked horizontal bars (default). Use inside cards
                that show key-value rows or text content.
    grid-row  — single-height bar; stack multiple for a table skeleton.
    card      — full card shell: title bar + N content rows + border.

  Props:
    rows?     number = 3      — how many shimmer lines
    height?   string = '1rem' — line height (CSS value)
    width?    string = '100%' — line width (CSS value); shorter values
                                simulate ragged-right text.
    rounded?  boolean = true  — rounded corners on bars
    variant?  'block' | 'grid-row' | 'card' = 'block'

  Palette: slate base (#1d2a44 → #152033) with a faint cyan sweep
  matching the algo theme. The shimmer moves left-to-right at ~1.5 s
  so it reads as "in progress" rather than "broken".

  Usage:
    <LoadingSkeleton />
    <LoadingSkeleton variant="card" rows={4} />
    <LoadingSkeleton variant="grid-row" rows={6} height="0.65rem" />
-->
<script>
  let {
    rows    = 3,
    height  = '1rem',
    width   = '100%',
    rounded = true,
    variant = 'block',
  } = $props();

  // Vary widths slightly across rows so the skeleton feels organic
  // rather than a mechanical repeating block.
  const _widths = ['100%', '85%', '92%', '78%', '96%', '88%', '80%'];

  /**
   * Pick a width for row i, respecting the passed `width` override.
   * If `width` !== '100%' the caller wants a specific fixed width —
   * respect it exactly. Otherwise vary to match the canonical skeleton
   * design.
   * @param {number} i
   */
  function _rowWidth(i) {
    if (width !== '100%') return width;
    return _widths[i % _widths.length];
  }
</script>

{#if variant === 'card'}
  <!-- Card variant: mimics the hcard structure on /admin/health -->
  <div class="skel-card" aria-busy="true" aria-label="Loading">
    <!-- title bar -->
    <div class="skel-bar skel-title" class:skel-rounded={rounded}
         style="width: 40%; height: 0.55rem;"></div>
    <!-- content rows -->
    {#each Array(rows) as _, i}
      <div class="skel-row">
        <div class="skel-bar" class:skel-rounded={rounded}
             style="width: 35%; height: {height};"></div>
        <div class="skel-bar" class:skel-rounded={rounded}
             style="width: 28%; height: {height};"></div>
      </div>
    {/each}
  </div>
{:else if variant === 'grid-row'}
  <!-- Grid-row variant: single bar per row, tight spacing -->
  <div class="skel-block" aria-busy="true" aria-label="Loading">
    {#each Array(rows) as _, i}
      <div class="skel-bar skel-bar-grid" class:skel-rounded={rounded}
           style="width: {_rowWidth(i)}; height: {height};"></div>
    {/each}
  </div>
{:else}
  <!-- Block variant (default): stacked bars with decreasing widths -->
  <div class="skel-block" aria-busy="true" aria-label="Loading">
    {#each Array(rows) as _, i}
      <div class="skel-bar" class:skel-rounded={rounded}
           style="width: {_rowWidth(i)}; height: {height};"></div>
    {/each}
  </div>
{/if}

<style>
  /* ── Shimmer keyframe ─────────────────────────────────────────── */
  @keyframes shimmer {
    0%   { background-position: -400px 0; }
    100% { background-position:  400px 0; }
  }

  /* ── Shared bar appearance ────────────────────────────────────── */
  .skel-bar {
    display: block;
    background: linear-gradient(
      90deg,
      #1a2640 0%,
      #243352 30%,
      /* faint cyan highlight */
      rgba(34, 211, 238, 0.07) 50%,
      #243352 70%,
      #1a2640 100%
    );
    background-size: 800px 100%;
    animation: shimmer 1.5s ease-in-out infinite;
    flex-shrink: 0;
  }
  @media (prefers-reduced-motion: reduce) {
    .skel-bar { animation: none; }
  }
  .skel-rounded { border-radius: 3px; }

  /* ── Block variant ────────────────────────────────────────────── */
  .skel-block {
    display: flex;
    flex-direction: column;
    gap: 0.45rem;
    padding: 0.5rem 0;
  }

  /* ── Grid-row variant ─────────────────────────────────────────── */
  .skel-bar-grid {
    margin: 0;
  }
  /* tighter row spacing for grid variant */
  .skel-block:has(.skel-bar-grid) {
    gap: 0.25rem;
    padding: 0.25rem 0;
  }

  /* ── Card variant ─────────────────────────────────────────────── */
  .skel-card {
    background: linear-gradient(180deg, #1d2a44 0%, #152033 100%);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 4px;
    padding: 0.55rem 0.7rem;
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
  }
  .skel-title {
    margin-bottom: 0.3rem;
  }
  .skel-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.4rem;
    padding: 0.18rem 0;
    border-bottom: 1px solid rgba(255, 255, 255, 0.04);
  }
  .skel-row:last-child {
    border-bottom: none;
  }
</style>
