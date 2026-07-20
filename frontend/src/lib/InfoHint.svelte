<script>
  // Compact (i) chip with a click-toggle / hover-preview popover.
  // Used across the algo admin pages to gloss page sections, stats,
  // and form fields without taking up screen real estate.
  //
  // Two display modes:
  //   - default (popup=false) — inline expansion below the chip
  //   - popup (popup=true)    — floating absolute-positioned tooltip
  //                            (preferred for compact stat panels)
  //
  // Content delivery (priority order):
  //   1. content prop — structured {what, ideal, impact, fix} object,
  //      rendered as a 4-row grid.  Preferred for metric tooltips.
  //   2. text prop — raw HTML string.
  //   3. children snippet.
  // The text-prop path is the safer one for stable rendering across
  // SvelteKit hydration / SSR — children snippets occasionally lose
  // their content during the SSR → CSR handoff in this codebase.

  import { onMount } from 'svelte';

  /** @type {{
   *   children?: any,
   *   text?: string,
   *   content?: { what: string, ideal: string, impact: string, fix: string },
   *   label?: string,
   *   maxWidth?: string,
   *   align?: 'left'|'right',
   *   defaultOpen?: boolean,
   *   popup?: boolean,
   *   id?: string,
   *   panel?: boolean,
   *   accentColor?: string,
   *   title?: string,
   *   showOnHover?: boolean,
   * }} */
  let {
    children,
    text = '',
    content = null,
    label = 'i',
    maxWidth = content ? '28rem' : '36rem',
    align = 'left',
    defaultOpen = false,
    popup = false,
    id = '',
    panel = false,
    accentColor = 'var(--algo-amber)',
    title = '',
    showOnHover = false,
  } = $props();

  // Unique id for aria-describedby. Generated in onMount to avoid
  // SSR/CSR id mismatch (server and client would use different counters).
  let _uid = $state('');
  onMount(() => {
    _uid = id || `infohint-${Math.random().toString(36).slice(2, 8)}`;
  });
  const _popoutId = $derived(_uid || 'infohint-pending');

  // intentional: defaultOpen is a one-time seed; operator toggles thereafter
  // svelte-ignore state_referenced_locally
  let open = $state($state.snapshot(defaultOpen));
  let hovered = $state(false);
  /** @type {HTMLSpanElement | undefined} */
  let wrap;
  /** @type {HTMLSpanElement | undefined} */
  let popoutEl = $state();

  // Close on click-outside when in popup mode so the tooltip doesn't
  // get stranded mid-page after the operator's attention has moved on.
  $effect(() => {
    if (!popup || !open) return;
    function onDocClick(/** @type {MouseEvent} */ e) {
      if (wrap && !wrap.contains(/** @type {Node} */ (e.target))) open = false;
    }
    document.addEventListener('mousedown', onDocClick);
    return () => document.removeEventListener('mousedown', onDocClick);
  });

  // Whether to render the popout right now.
  const visible = $derived(popup ? (open || hovered) : open);

  // Viewport-bound the popup. Strategy:
  //   - the popup is `position: fixed` so its coordinates are
  //     viewport-relative and unaffected by ancestor transforms,
  //     overflow:hidden, etc.
  //   - on open / resize / scroll we recompute (left, top) from the
  //     chip's getBoundingClientRect(), then clamp left to keep the
  //     entire popup on screen with an 8px gutter.
  // This is more robust than transform-based nudging because we
  // ignore any parent positioning context and just place the popup
  // exactly where it fits in the viewport.
  $effect(() => {
    if (!popup || !visible || !popoutEl || !wrap || typeof window === 'undefined') return;
    /** @type {number} */ let raf;
    function fit() {
      if (!popoutEl || !wrap) return;
      const margin = 8;
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      // Width clamp first — JS-side guarantee that the popup never
      // exceeds viewport width regardless of CSS / max-content
      // calculations. Setting maxWidth as inline style overrides
      // any CSS rule. Skip for panel mode — CSS governs max-width there.
      if (!panel) popoutEl.style.maxWidth = `${Math.max(120, vw - margin * 2)}px`;
      // Reset position before measuring so the previous fit doesn't
      // bias the natural rect.
      popoutEl.style.left = '';
      popoutEl.style.top  = '';
      const chipRect = wrap.getBoundingClientRect();
      const popRect  = popoutEl.getBoundingClientRect();
      // Anchor at chip's left, clamp to viewport with 8px gutters.
      let left = chipRect.left;
      if (left + popRect.width > vw - margin) {
        left = vw - margin - popRect.width;
      }
      if (left < margin) left = margin;
      // Vertical: under chip; flip above if no room.
      let top = chipRect.bottom + 6;
      if (top + popRect.height > vh - margin) {
        const above = chipRect.top - 6 - popRect.height;
        if (above >= margin) top = above;
      }
      popoutEl.style.left = `${left}px`;
      popoutEl.style.top  = `${top}px`;
      // Reveal AFTER positioning to avoid the brief flash at top-left
      // that an unpositioned `position:fixed` element shows on mount.
      popoutEl.style.visibility = 'visible';
    }
    // Hide before measurement so the unpositioned mount frame
    // doesn't paint at top-left of the viewport.
    if (popoutEl) popoutEl.style.visibility = 'hidden';
    raf = requestAnimationFrame(fit);
    window.addEventListener('resize', fit);
    window.addEventListener('scroll', fit, true);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener('resize', fit);
      window.removeEventListener('scroll', fit, true);
    };
  });
</script>

<span class="info-wrap" class:align-right={align === 'right'}
      class:info-wrap-popup={popup}
      bind:this={wrap}>
  <button type="button"
          class="info-btn"
          class:open
          aria-expanded={open}
          aria-describedby={visible ? _popoutId : undefined}
          aria-label={open ? 'Hide details' : 'Show details'}
          title={open ? 'Hide details' : 'Show details'}
          onclick={() => { if (!showOnHover) { open = !open; if (!open) hovered = false; } }}
          onmouseenter={() => hovered = true}
          onmouseleave={() => hovered = false}
          onfocus={() => { if (showOnHover) hovered = true; }}
          onblur={() => { if (showOnHover) hovered = false; }}>{label}</button>
  {#if visible}
    <span class="info-popout"
          class:info-popout-popup={popup}
          class:info-popout-pinned={popup && open}
          class:ih-panel={panel}
          style={panel ? `--accent-color: ${accentColor}` : `max-width: ${maxWidth}`}
          id={_popoutId}
          role="tooltip"
          bind:this={popoutEl}>
      {#if panel && title}
        <div class="ih-panel-title">{title}</div>
      {/if}
      {#if content}
        <dl class="info-struct" data-testid="metric-popover">
          <div class="info-row">
            <dt class="info-dt">What</dt>
            <dd class="info-dd">{content.what}</dd>
          </div>
          <div class="info-row">
            <dt class="info-dt">Ideal</dt>
            <dd class="info-dd info-dd-mono">{content.ideal}</dd>
          </div>
          <div class="info-row">
            <dt class="info-dt">Impact</dt>
            <dd class="info-dd">{content.impact}</dd>
          </div>
          <div class="info-row">
            <dt class="info-dt">Fix</dt>
            <dd class="info-dd">{content.fix}</dd>
          </div>
        </dl>
      {:else if text}
        {@html text}
      {:else if children}
        {@render children()}
      {/if}
    </span>
  {/if}
</span>

<style>
  .info-wrap {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    flex-wrap: wrap;
  }
  .align-right { justify-content: flex-end; }
  /* Popup mode — wrap is positioned so the floating popout anchors
     to it. No flex-wrap (popup floats absolutely so it can never
     push siblings down into a new line). */
  .info-wrap-popup {
    position: relative;
    flex-wrap: nowrap;
  }

  /* Chip — subtle and small. Earlier iterations were a saturated
     amber pill; toned down to slate-blue at low alpha so the chip
     reads as "supplemental info available" without competing with
     the labels it sits next to. The amber accent only appears on
     hover / open so the chip lights up when intentionally invoked. */
  .info-btn {
    width: 0.75rem;
    height: 0.75rem;
    border-radius: 9999px;
    border: 1px solid rgba(125,151,184,0.32);
    background: rgba(125,151,184,0.06);
    color: var(--algo-muted);
    font-size: var(--fs-2xs);
    font-style: italic;
    font-weight: 600;
    line-height: 1;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    flex: 0 0 auto;
    transition: background 0.12s, border-color 0.12s, color 0.12s;
  }
  .info-btn:hover {
    background: var(--c-action-14);
    border-color: rgba(251,191,36,0.5);
    color: var(--c-action);
  }
  .info-btn.open {
    background: var(--c-action-22);
    color: var(--c-action);
    border-color: rgba(251,191,36,0.6);
  }

  /* Popover — subtle dark-blue surface with a soft border. Earlier
     iterations used a gradient + amber-accent left border; the
     accent shouted louder than the helper text it was framing. The
     subtler look is a flat slate background, faint blue-grey border,
     no left accent, and softer body type. Bold spans inside (b /
     strong) keep amber so the information hierarchy is preserved. */
  .info-popout {
    display: inline-block;
    margin: 0;
    padding: 0.5rem 0.7rem;
    border-radius: 0.3rem;
    border: 1px solid rgba(125,211,252,0.18);
    background: rgba(15, 25, 45, 0.95);
    font-size: var(--fs-lg);
    color: var(--algo-slate);
    line-height: 1.5;
    flex: 1 1 100%;
  }
  /* Popup variant — `position: fixed` so the popup is positioned
     relative to the viewport, not any ancestor. Coordinates (`left`
     / `top`) are computed by JS in the component on every open /
     resize / scroll, clamped to keep the entire popup inside the
     viewport with an 8 px gutter. CSS only sets the placeholder
     left/top (gets overridden by JS on the same frame). */
  .info-popout-popup {
    position: fixed;
    top: 0;
    left: 0;
    /* High z-index so the popout sits above absolutely-positioned
       cards (Options payoff chart, fullscreen panels, ag-Grid popups).
       Was 50 — got clipped behind the payoff chart on /admin/options
       because the chart card's stacking context outranked it. 9999
       matches the FullscreenButton backdrop level. */
    z-index: var(--z-tooltip);
    flex: none;
    width: max-content;
    min-width: min(12rem, calc(100vw - 1rem));
    max-width: min(20rem, calc(100vw - 1rem));
    box-shadow: 0 4px 14px rgba(0,0,0,0.45);
  }
  .info-popout-pinned { pointer-events: auto; }

  /* Panel mode — richer floating panel with gradient background, accent
     left border, and a titled header. Used for NavStrip pill hints where
     space permits a more detailed explanation. accentColor is supplied
     per-pill via --accent-color CSS custom property. */
  .info-popout.ih-panel {
    min-width: 16rem;
    max-width: min(22rem, calc(100vw - 1.5rem));
    padding: 0.75rem 1rem;
    background: linear-gradient(180deg, #1c2840 0%, #141e33 100%);
    border: 1px solid var(--algo-amber-border-soft);
    box-shadow: 0 8px 32px rgba(0,0,0,0.6);
    border-radius: 4px;
    border-left: 3px solid var(--accent-color, var(--algo-amber));
    font-size: var(--fs-sm);
    color: var(--algo-slate);
    line-height: 1.5;
  }
  .ih-panel-title {
    font-size: var(--fs-md);
    font-weight: 700;
    color: var(--accent-color, var(--algo-amber));
    text-transform: uppercase;
    letter-spacing: 0.05em;
    border-bottom: 1px solid rgba(255,255,255,0.07);
    padding-bottom: 0.4rem;
    margin-bottom: 0.5rem;
  }

  /* Reset margins on common children so the popover content reads
     compactly without unintended padding. */
  :global(.info-popout p)  { margin: 0 0 0.4rem; }
  :global(.info-popout p:last-child) { margin-bottom: 0; }
  :global(.info-popout code),
  :global(.info-popout .font-mono) { color: #7dd3fc; }
  :global(.info-popout b),
  :global(.info-popout strong) { color: var(--c-action); font-weight: 700; }

  /* Structured 4-row metric popover layout. */
  .info-struct {
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
  }
  .info-row {
    display: grid;
    grid-template-columns: 3.4rem 1fr;
    gap: 0 0.5rem;
    align-items: baseline;
  }
  .info-dt {
    font-size: var(--fs-md);
    font-weight: 700;
    color: var(--c-action);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    line-height: 1.5;
    white-space: nowrap;
  }
  .info-dd {
    margin: 0;
    font-size: var(--fs-lg);
    color: var(--text-hi, #f1f5f9);
    line-height: 1.5;
  }
  .info-dd-mono {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: var(--fs-md);
    color: #7dd3fc;
  }
</style>
