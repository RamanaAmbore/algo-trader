<script>
  // Checkbox-style multi-select that mirrors the Select.svelte palette
  // (linear-gradient(#273552 → #1d2a44), amber accents, monospace). Binds
  // to an array of selected values; the trigger shows a comma-joined
  // summary or the placeholder when nothing is picked.
  //
  // Props mirror Select.svelte for drop-in substitution:
  //   value (Array<string>)  bindable
  //   options  Array<{ value, label, hint? }>
  //   placeholder?, id?, disabled?, ariaLabel?

  import { onMount, onDestroy } from 'svelte';
  import { fitFixedPanel, watchFloatingPanel } from '$lib/utils/floatingPanel.js';

  let {
    value = $bindable(/** @type {string[]} */([])),
    options = [],
    placeholder = '',
    id = '',
    disabled = false,
    ariaLabel = '',
    // 'dark'  — algo console (navy gradient, amber accents)
    // 'light' — public site (cream + champagne gold, navy text)
    theme = 'dark',
    // Bindable so parents can observe or control the dropdown open state,
    // e.g. to suppress hover popups while the dropdown is open.
    open = $bindable(false),
    // When true (default), the trigger shows a "×" clear-all button
    // once any option is picked. Set to false to suppress it for
    // pickers where the operator should toggle each option from the
    // dropdown panel instead — e.g. AccountMultiSelect, where the
    // "remove account ×" affordance was found confusing because it
    // jumped the operator back to the empty-set ("All accounts") state
    // in one click without confirming intent.
    allowClear = true,
    // When true, picking a new option AUTO-DEselects every other
    // currently-selected option — radio behaviour with the checkbox
    // visuals. Re-picking the SAME option flips it off (back to
    // empty selection). Use for pickers where the operator wants
    // single-scope semantics even though the visual is a checkbox
    // list (e.g. AccountMultiSelect on Pulse — "when you select any
    // other value all [accounts] should be deselected automatically").
    // Default false keeps existing multi-pick behaviour everywhere
    // else (Expiry picker, Watchlist picker, etc.).
    singleSelect = false,
  } = $props();
  let triggerEl;
  /** @type {HTMLElement | undefined} */
  let wrapEl = $state();
  let panelEl = $state(/** @type {HTMLElement | undefined} */ (undefined));

  // Panel is `position: fixed` (see floatingPanel.js) so it can never be
  // clipped by an ancestor's `overflow` (e.g. CardHeader's `.ch-left` /
  // `.ch-middle`, which set `overflow-x: auto` and — per the CSS
  // Overflow spec — the OTHER axis computes to `auto` too, clipping the
  // panel to zero visible pixels). Measure `.rbq-multi-trigger-wrap`
  // (not the bare trigger button) so the panel's width matches the full
  // control, including the adjacent clear button.
  $effect(() => {
    if (!open || !panelEl || !wrapEl || typeof window === 'undefined') return;
    const fit = () => fitFixedPanel({ triggerEl: wrapEl, panelEl });
    fit();
    return watchFloatingPanel(fit, panelEl);
  });

  const displayLabel = $derived.by(() => {
    if (!value || !value.length) return placeholder || '';
    // Show option LABELS (not raw values) in the trigger summary —
    // values may be tokens like "src:positions" / "wl:42" which would
    // read as opaque garbage. Falls back to the value itself if the
    // option list doesn't carry it (defensive against stale state).
    const labels = value.map(v => {
      const opt = options.find(o => o.value === v);
      return opt?.label ?? v;
    });
    if (labels.length <= 2) return labels.join(', ');
    return `${labels.length} selected`;
  });

  function toggle() {
    if (disabled) return;
    open = !open;
  }

  function toggleOption(/** @type {any} */ opt) {
    const v = opt.value;
    const cur = Array.isArray(value) ? [...value] : [];
    const i = cur.indexOf(v);
    if (singleSelect) {
      // Radio behaviour over a checkbox UI. Tapping the currently-
      // selected option clears the selection (back to empty = "all");
      // tapping any other option REPLACES the selection with just
      // that one — no multi-pick across this picker.
      if (i >= 0 && cur.length === 1) value = [];
      else value = [v];
      return;
    }
    if (i >= 0) cur.splice(i, 1);
    else cur.push(v);
    value = cur;
  }

  function clearAll(/** @type {Event} */ e) {
    e.stopPropagation();
    value = [];
  }

  function onKey(/** @type {KeyboardEvent} */ e) {
    if (disabled) return;
    if (!open) {
      if (e.key === 'Enter' || e.key === ' ' || e.key === 'ArrowDown') {
        e.preventDefault();
        toggle();
      }
      return;
    }
    if (e.key === 'Escape') { open = false; e.preventDefault(); }
  }

  /** @type {(e: MouseEvent) => void} */
  function onDocClick(e) {
    if (!open) return;
    const t = /** @type {Node} */ (e.target);
    if (triggerEl?.contains(t) || panelEl?.contains(t)) return;
    open = false;
  }
  onMount(() => { document.addEventListener('mousedown', onDocClick); });
  onDestroy(() => { document.removeEventListener('mousedown', onDocClick); });
</script>

<div class="rbq-multi rbq-multi-{theme} {open ? 'rbq-multi-open' : ''}">
  <!-- Trigger wrap keeps the clear button as a sibling (not nested inside)
       the trigger button, avoiding an invalid button-in-button DOM structure. -->
  <div class="rbq-multi-trigger-wrap" bind:this={wrapEl}>
    <button type="button" bind:this={triggerEl}
      {id} {disabled} aria-haspopup="listbox" aria-expanded={open} aria-label={ariaLabel}
      class="rbq-multi-trigger"
      onclick={toggle}
      onkeydown={onKey}>
      <span class="rbq-multi-label {!value?.length ? 'rbq-multi-placeholder' : ''}">
        {displayLabel || '\u00a0'}
      </span>
      <span class="rbq-multi-caret" aria-hidden="true">▾</span>
    </button>
    {#if value?.length && allowClear}
      <button type="button" class="rbq-multi-clear" onclick={clearAll}
              aria-label="Clear selection">×</button>
    {/if}
  </div>

  {#if open}
    <ul class="rbq-multi-panel" role="listbox" aria-multiselectable="true" bind:this={panelEl}>
      {#each options as opt}
        {@const selected = (value || []).includes(opt.value)}
        <li role="option" aria-selected={selected}
            class="rbq-multi-option {selected ? 'rbq-multi-option-selected' : ''}"
            onmousedown={() => toggleOption(opt)}>
          <span class="rbq-multi-check">{selected ? '✓' : ' '}</span>
          <span class="rbq-multi-option-label">{opt.label}</span>
          {#if opt.hint}<span class="rbq-multi-option-hint">{opt.hint}</span>{/if}
        </li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .rbq-multi {
    position: relative;
    display: block;
    width: 100%;
    font-family: var(--font-numeric);
    color: var(--algo-slate);
  }

  .rbq-multi-trigger-wrap {
    position: relative;
    display: flex;
    align-items: center;
    width: 100%;
  }
  .rbq-multi-trigger-wrap .rbq-multi-trigger {
    flex: 1 1 auto;
  }
  .rbq-multi-trigger-wrap .rbq-multi-clear {
    position: absolute;
    right: 1.4rem; /* leave room for the caret */
    flex: none;
  }

  .rbq-multi-trigger {
    width: 100%;
    display: flex;
    align-items: center;
    gap: 0.4rem;
    min-height: 1.55rem;
    padding: 0.25rem 0.5rem 0.25rem 0.4rem;
    background-image: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    background-color: #1d2a44;
    border: 1px solid rgba(251,191,36,0.25);
    border-radius: 3px;
    color: var(--algo-slate);
    font-size: var(--fs-sm);
    font-family: inherit;
    cursor: pointer;
    text-align: left;
    transition: border-color 0.08s;
  }
  .rbq-multi-trigger:hover:not(:disabled)  { border-color: rgba(251,191,36,0.6); }
  .rbq-multi-trigger:focus                 { outline: none; border-color: var(--c-action); }
  .rbq-multi-trigger:disabled              { opacity: 0.45; cursor: not-allowed; }
  .rbq-multi-open .rbq-multi-trigger       { border-color: var(--c-action); }

  .rbq-multi-label { flex: 1 1 auto; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .rbq-multi-placeholder { color: rgba(180,200,230,0.55); }

  .rbq-multi-clear {
    flex: 0 0 auto;
    padding: 0 0.3rem;
    margin: 0;
    background: transparent;
    border: none;
    color: rgba(251,191,36,0.7);
    font-size: var(--fs-xl);
    line-height: 1;
    cursor: pointer;
  }
  .rbq-multi-clear:hover { color: var(--c-action); }
  .rbq-multi-caret {
    flex: 0 0 auto;
    /* Match Select.svelte's caret treatment: 0.95rem amber `▾` with
       weight 700 + smooth rotate transition. Earlier this was 0.55rem
       which made the dropdown affordance read as a stray period. */
    color: var(--c-action);
    font-size: var(--fs-xl);
    line-height: 1;
    font-weight: 700;
    transform: translateY(-1px);
    transition: transform 0.12s ease;
  }
  .rbq-multi-open .rbq-multi-caret { transform: translateY(-1px) rotate(180deg); }

  /* `position: fixed` + JS-computed left/top/width (floatingPanel.js,
     wired in the `$effect` above) — NOT `position: absolute` with
     `left: 0; right: 0`. An absolute panel is clipped to zero visible
     pixels by any ancestor with `overflow-x: auto` (CardHeader's
     `.ch-left` / `.ch-middle`), because the CSS Overflow spec computes
     the OTHER axis to `auto` too once one axis is non-`visible`. Fixed
     positioning escapes that clipping entirely; `max-height` here is a
     fallback cap, overridden per-open by the JS fit to the actual
     available viewport space. */
  .rbq-multi-panel {
    position: fixed;
    top: 0;
    left: 0;
    z-index: var(--z-dropdown);
    margin: 0;
    padding: 0.2rem 0;
    list-style: none;
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    border: 1.5px solid rgba(251,191,36,0.35);
    border-radius: 4px;
    box-shadow: 0 10px 28px rgba(0,0,0,0.55), inset 0 1px 0 rgba(255,255,255,0.06);
    max-height: 14rem;
    overflow-y: auto;
    font-size: var(--fs-sm);
    color: var(--algo-slate);
  }

  .rbq-multi-option {
    padding: 0.3rem 0.55rem;
    display: flex;
    align-items: baseline;
    gap: 0.4rem;
    cursor: pointer;
    transition: background 0.06s, color 0.06s;
    border-left: 2px solid transparent;
  }
  .rbq-multi-option:hover {
    background: rgba(251,191,36,0.15);
    color: var(--c-action);
    border-left-color: var(--c-action);
  }
  .rbq-multi-option-selected { color: var(--c-action); font-weight: 700; }
  .rbq-multi-check {
    flex: 0 0 0.8rem;
    width: 0.8rem;
    color: var(--c-long);
    font-family: var(--font-numeric);
  }
  .rbq-multi-option-label { flex: 1 1 auto; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .rbq-multi-option-hint {
    flex: 0 0 auto;
    font-size: var(--fs-xs);
    color: rgba(180,200,230,0.55);
  }

  /* ── Light theme (public palette — cream body + champagne gold) ─────── */
  .rbq-multi-light { color: #1a1e35; }
  .rbq-multi-light .rbq-multi-trigger {
    background-image: none;
    background-color: #ffffff;
    border: 1px solid #c0ccdc;
    color: #1e3050;
  }
  .rbq-multi-light .rbq-multi-trigger:hover:not(:disabled) { border-color: #c8a84b; }
  .rbq-multi-light .rbq-multi-trigger:focus { border-color: #c8a84b; }
  .rbq-multi-light.rbq-multi-open .rbq-multi-trigger      { border-color: #c8a84b; }
  .rbq-multi-light .rbq-multi-placeholder { color: #64748b; }
  .rbq-multi-light .rbq-multi-clear       { color: rgba(200,168,75,0.8); }
  .rbq-multi-light .rbq-multi-clear:hover { color: #c8a84b; }
  .rbq-multi-light .rbq-multi-caret       { color: #c8a84b; }

  .rbq-multi-light .rbq-multi-panel {
    background: #ffffff;
    border: 1.5px solid #c8a84b;
    box-shadow: 0 10px 24px rgba(12,24,48,0.18);
    color: #1a1e35;
  }
  .rbq-multi-light .rbq-multi-option {
    border-left-color: transparent;
  }
  .rbq-multi-light .rbq-multi-option:hover {
    background: rgba(200,168,75,0.18);
    color: #0c1830;
    border-left-color: #c8a84b;
  }
  .rbq-multi-light .rbq-multi-option-selected {
    color: #0c1830;
    font-weight: 700;
  }
  .rbq-multi-light .rbq-multi-check { color: #15803d; }
  .rbq-multi-light .rbq-multi-option-hint { color: #64748b; }
</style>
