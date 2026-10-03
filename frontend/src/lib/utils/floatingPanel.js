/**
 * floatingPanel.js — shared placement engine for `position: fixed`
 * dropdown/popover panels that must never be clipped by an ancestor's
 * `overflow` or stacking context.
 *
 * Motivating defect (2026-10): CardHeader's `.ch-left` / `.ch-middle`
 * zones set `overflow-x: auto` for mobile horizontal scroll. Per the CSS
 * Overflow spec, once one axis is non-`visible` the OTHER axis computes
 * to `auto` too, even when left unset or explicitly set to `visible` —
 * so any `position: absolute` dropdown panel mounted inside one of those
 * zones (MultiSelect / Select) got clipped to zero visible pixels the
 * moment it opened. Fix lives in the PANEL itself (this util), not in
 * CardHeader's CSS — see MultiSelect.svelte / Select.svelte, both of
 * which now render their panel with `position: fixed` and call this
 * function to compute viewport-relative coordinates, same strategy as
 * InfoHint.svelte's popup mode.
 *
 * Call synchronously inside an `$effect` right after the panel mounts —
 * do NOT hide-then-rAF-then-reveal (InfoHint does this for its own
 * flash-prevention reasons, but `Select.svelte`'s searchable variant
 * autofocuses its search input synchronously on open; a transiently
 * `visibility: hidden` panel silently eats that `.focus()` call).
 * `getBoundingClientRect()` forces layout, so a single synchronous
 * measurement here is enough — no flash, because we only ever write the
 * FINAL computed position, never an intermediate wrong one.
 */

/**
 * @param {{
 *   triggerEl: HTMLElement,
 *   panelEl: HTMLElement,
 *   margin?: number,
 *   gutter?: number,
 *   matchWidth?: boolean,
 *   clampMaxHeight?: boolean,
 * }} opts
 */
export function fitFixedPanel({
  triggerEl,
  panelEl,
  margin = 4,
  gutter = 8,
  matchWidth = true,
  clampMaxHeight = true,
}) {
  if (!triggerEl || !panelEl || typeof window === 'undefined') return;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const trigRect = triggerEl.getBoundingClientRect();

  // Read the component's OWN designed cap (e.g. `.rbq-select-panel`'s
  // `max-height: 16rem`) BEFORE we write any inline style — this is a
  // ceiling, not a floor. Without it, a page with lots of vertical
  // room below the trigger (e.g. a picker near the top of a tall page)
  // would stretch the panel far taller than its design ever intended,
  // just because the viewport had the room. Read once, before any
  // inline override, so the cascade's real authored value comes
  // through (inline styles set later would otherwise shadow it).
  const _cssMaxHeight = parseFloat(getComputedStyle(panelEl).maxHeight);
  const designCap = Number.isFinite(_cssMaxHeight) ? _cssMaxHeight : Infinity;

  // Width: `left: 0; right: 0` (relative to a `position: relative`
  // ancestor) stops meaning "same width as the trigger" once the panel
  // is `position: fixed` — set it explicitly from the trigger's own
  // rect so the panel's footprint is unchanged from the old absolute
  // layout.
  if (matchWidth) panelEl.style.width = `${trigRect.width}px`;

  // Provisional placement so the rect we measure next reflects real
  // content size (search box, option count, etc) at roughly the right
  // width — matters for panels whose height depends on wrapping.
  let left = trigRect.left;
  let top = trigRect.bottom + margin;
  panelEl.style.left = `${left}px`;
  panelEl.style.top = `${top}px`;

  const panelRect = panelEl.getBoundingClientRect();

  // Vertical: flip above the trigger when there isn't room below;
  // clamp available height to whichever side wins so a short phone
  // viewport never pushes the panel off-screen entirely.
  const spaceBelow = vh - trigRect.bottom - margin - gutter;
  const spaceAbove = trigRect.top - margin - gutter;
  let available = spaceBelow;
  if (panelRect.height > spaceBelow && spaceAbove > spaceBelow) {
    available = spaceAbove;
    top = Math.max(gutter, trigRect.top - margin - Math.min(panelRect.height, available));
  }
  if (clampMaxHeight) panelEl.style.maxHeight = `${Math.max(80, Math.min(available, designCap))}px`;

  // Horizontal clamp.
  if (left + panelRect.width > vw - gutter) left = vw - gutter - panelRect.width;
  if (left < gutter) left = gutter;

  panelEl.style.left = `${left}px`;
  panelEl.style.top = `${top}px`;

  // Delta-correction for ancestors that establish their OWN containing
  // block for fixed descendants (`transform`, `filter`,
  // `backdrop-filter`, `perspective`, `will-change: transform`,
  // `contain: paint|layout`). Per spec, `position: fixed` resolves
  // against such an ancestor, not the viewport — a naive
  // viewport-relative left/top lands offset inside one (e.g.
  // OrderTicket's `.ot-overlay` backdrop-filter). Re-measure after the
  // write above and nudge by the observed delta so the panel lands at
  // the INTENDED viewport coordinates regardless of containing block.
  const actual = panelEl.getBoundingClientRect();
  const dx = left - actual.left;
  const dy = top - actual.top;
  if (dx || dy) {
    panelEl.style.left = `${left + dx}px`;
    panelEl.style.top = `${top + dy}px`;
  }
}

/**
 * Wires resize/scroll-driven refitting for an open floating panel.
 * Returns a cleanup function — call from the owning `$effect`'s return.
 * Scroll events whose target is inside the panel itself are ignored
 * (the panel has its own internal `overflow-y: auto` option list;
 * refitting on that scroll would jitter the panel under the cursor).
 *
 * @param {() => void} fit
 * @param {HTMLElement | undefined} panelEl optional; when provided,
 *   scroll events originating inside this element are ignored.
 * @returns {() => void}
 */
export function watchFloatingPanel(fit, panelEl) {
  /** @type {number | null} */
  let raf = null;
  function schedule() {
    if (raf != null) return;
    raf = requestAnimationFrame(() => {
      raf = null;
      fit();
    });
  }
  /** @param {Event} e */
  function onScroll(e) {
    const t = /** @type {Node} */ (e.target);
    if (panelEl && t && panelEl.contains(t)) return;
    schedule();
  }
  window.addEventListener('resize', schedule);
  window.addEventListener('scroll', onScroll, true);
  return () => {
    if (raf != null) cancelAnimationFrame(raf);
    window.removeEventListener('resize', schedule);
    window.removeEventListener('scroll', onScroll, true);
  };
}
