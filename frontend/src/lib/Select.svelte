<script>
  // Custom dropdown that mirrors the OrderPopup modal's palette —
  // linear-gradient(#273552 → #1d2a44), amber accents, monospace,
  // rounded corners. Replaces the native <select> on pages where the
  // OS-rendered popup looks foreign on the dark theme.
  //
  // Props:
  //   value         — currently-selected option value (bindable)
  //   options       — Array<{ value, label, hint? }>
  //   placeholder?  — shown when value is empty
  //   id?           — for <label for="...">
  //   disabled?     — bool
  //   ariaLabel?    — a11y fallback if no <label> is in scope

  import { onMount, onDestroy } from 'svelte';

  let {
    value = $bindable(/** @type {any} */ ('')),
    /** @type {Array<{ value: any, label: string, hint?: string }>} */
    options = [],
    placeholder = '',
    id = '',
    disabled = false,
    ariaLabel = '',
    // 'dark'  — algo console (navy gradient + amber)
    // 'light' — public site (cream + champagne gold)
    theme = 'dark',
    // When true, render a search input at the top of the dropdown
    // panel. The list shows in full until the operator types at
    // least `searchMinChars` characters; from that point onward
    // options are filtered by case-insensitive substring match.
    searchable = false,
    searchMinChars = 3,
    searchPlaceholder = 'Type to filter…',
    // Optional callback fired AFTER `value` updates from a user pick.
    // Use when the parent doesn't want to keep its own bindable state
    // (e.g. derived/server-backed forms like /admin/settings, where
    // the canonical value is the server's stored row and any pick
    // triggers an update API call rather than mutating a local var).
    /** @type {((v: any) => void) | undefined} */
    onValueChange = undefined,
  } = $props();

  let open = $state(false);
  let triggerEl;
  let panelEl = $state(/** @type {HTMLElement | undefined} */ (undefined));
  /** @type {HTMLInputElement | undefined} */
  let searchInputEl = $state();
  let highlighted   = $state(-1);
  let searchTerm    = $state('');

  // Filtered option list — when search is active and the term has
  // ≥searchMinChars characters, filter by case-insensitive substring
  // match against label OR value. Otherwise show every option.
  // Defensive de-dup. Even though parent callers should send unique
  // values, a transient race (e.g. underlyingOptionsForPicker on
  // /admin/derivatives during the instrumentsReady flip) can pass
  // duplicates through for a single Svelte flush. The keyed each-
  // block then throws `each_key_duplicate` and aborts the reactive
  // microtask, swallowing other state assignments — most visibly
  // the strategy=null → strategy=<response> transition that gates
  // the payoff chart. Defending here means every caller is safe by
  // construction without needing to coordinate timing themselves.
  function _dedupByValue(/** @type {any[]} */ src) {
    const seen = new Set();
    const out = [];
    for (const o of src || []) {
      const k = String(o?.value ?? '');
      if (seen.has(k)) continue;
      seen.add(k);
      out.push(o);
    }
    return out;
  }
  const filteredOptions = $derived.by(() => {
    const base = _dedupByValue(options);
    if (!searchable) return base;
    const q = searchTerm.trim().toUpperCase();
    if (q.length < searchMinChars) return base;
    return base.filter(o => {
      const l = String(o.label ?? '').toUpperCase();
      const v = String(o.value ?? '').toUpperCase();
      return l.includes(q) || v.includes(q);
    });
  });

  const current = $derived(
    options.find(o => String(o.value) === String(value)) || null
  );
  const displayLabel = $derived(current?.label ?? placeholder ?? '');

  function toggle() {
    if (disabled) return;
    open = !open;
    if (open) {
      // Reset search every open so reopen always shows the full list.
      searchTerm  = '';
      highlighted = Math.max(0, filteredOptions.findIndex(o => String(o.value) === String(value)));
      // Focus the search input after the panel mounts so the operator
      // can start typing immediately. Falls through silently when the
      // panel isn't searchable.
      if (searchable) {
        queueMicrotask(() => { searchInputEl?.focus(); });
      }
    }
  }

  function pick(/** @type {any} */ opt) {
    value = opt.value;
    open = false;
    triggerEl?.focus();
    onValueChange?.(opt.value);
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
    if (e.key === 'Escape') { open = false; e.preventDefault(); return; }
    if (e.key === 'ArrowDown') { highlighted = Math.min(filteredOptions.length - 1, highlighted + 1); e.preventDefault(); return; }
    if (e.key === 'ArrowUp')   { highlighted = Math.max(0, highlighted - 1); e.preventDefault(); return; }
    if (e.key === 'Enter') {
      if (highlighted >= 0 && filteredOptions[highlighted]) pick(filteredOptions[highlighted]);
      e.preventDefault();
      return;
    }
  }

  // Reset highlight to the top whenever the filter changes — otherwise
  // a leftover index could point past the new array's end.
  $effect(() => {
    void searchTerm;
    if (open) highlighted = filteredOptions.length ? 0 : -1;
  });

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

<div class="rbq-select rbq-select-{theme} {open ? 'rbq-select-open' : ''}">
  <button type="button" bind:this={triggerEl}
    {id} {disabled} aria-haspopup="listbox" aria-expanded={open} aria-label={ariaLabel}
    class="rbq-select-trigger"
    onclick={toggle}
    onkeydown={onKey}>
    <span class="rbq-select-label {!current ? 'rbq-select-placeholder' : ''}">{displayLabel || '\u00a0'}</span>
    <span class="rbq-select-caret" aria-hidden="true">▾</span>
  </button>

  {#if open}
    <div class="rbq-select-panel" role="listbox" bind:this={panelEl}>
      {#if searchable}
        <div class="rbq-select-search">
          <input type="text"
                 class="rbq-select-search-input"
                 bind:this={searchInputEl}
                 bind:value={searchTerm}
                 placeholder={searchPlaceholder}
                 aria-label="Filter options"
                 onkeydown={onKey} />
          {#if searchTerm.trim().length > 0 && searchTerm.trim().length < searchMinChars}
            <span class="rbq-select-search-hint">
              type {searchMinChars - searchTerm.trim().length} more
            </span>
          {/if}
        </div>
      {/if}
      <ul class="rbq-select-options">
        <!-- Keyed by opt.value so Svelte 5's keyed reconciliation
             diffs by identity, not positional index. Without the
             key, an async update that relabels an option in place
             (e.g. MCX picker swapping CRUDEOIL → CRUDEOIL25JUNFUT
             once instrumentsReady flips true) threw
             `each_key_duplicate` and silently remounted the
             dropdown subtree, leaving the picker button visually
             present but unclickable. Same root cause behind the
             navbar hamburger drawer hang on /admin/options. -->
        {#each filteredOptions as opt, i (String(opt.value) + '|' + i)}
          {@const selected = String(opt.value) === String(value)}
          <li role="option" aria-selected={selected}
              class="rbq-select-option
                {selected ? 'rbq-select-option-selected' : ''}
                {highlighted === i ? 'rbq-select-option-hl' : ''}"
              onmousedown={() => pick(opt)}
              onmouseenter={() => { highlighted = i; }}>
            <span class="rbq-select-option-label" data-hint={opt.hint || ''}>{opt.label}</span>
            {#if opt.hint}<span class="rbq-select-option-hint" data-hint={opt.hint}>{opt.hint}</span>{/if}
          </li>
        {/each}
        {#if !filteredOptions.length}
          <li class="rbq-select-empty">No matches.</li>
        {/if}
      </ul>
    </div>
  {/if}
</div>

<style>
  .rbq-select {
    position: relative;
    display: block;
    width: 100%;
    font-family: var(--font-numeric);
    color: var(--algo-slate);
  }

  /* Trigger looks like .field-input but with the popup gradient so it
     clearly reads as "opens a popup, not a generic text input". */
  .rbq-select-trigger {
    width: 100%;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
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
  .rbq-select-trigger:hover:not(:disabled)  { border-color: rgba(251,191,36,0.6); }
  .rbq-select-trigger:focus                 { outline: none; border-color: var(--c-action); }
  .rbq-select-trigger:disabled              { opacity: 0.45; cursor: not-allowed; }
  .rbq-select-open .rbq-select-trigger      { border-color: var(--c-action); }

  .rbq-select-label { flex: 1 1 auto; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .rbq-select-placeholder { color: rgba(180,200,230,0.55); }
  .rbq-select-caret {
    flex: 0 0 auto;
    /* Bumped from 0.55rem amber `▾` to 0.95rem so the dropdown
       affordance reads at a glance — earlier the glyph was so small
       it looked like a stray period. Open state rotates 180° to
       point up (`▴`-equivalent). */
    color: var(--c-action);
    font-size: var(--fs-xl);
    line-height: 1;
    font-weight: 700;
    transform: translateY(-1px);
    transition: transform 0.12s ease;
  }
  .rbq-select-open .rbq-select-caret { transform: translateY(-1px) rotate(180deg); }

  /* Popup panel — mirrors .popup-modal from OrderPopup: same gradient,
     same amber-accent border, same box-shadow. Floats absolutely below
     the trigger. The inner .rbq-select-options scrolls; the optional
     search bar stays pinned at the top. */
  .rbq-select-panel {
    position: absolute;
    top: calc(100% + 4px);
    left: 0;
    right: 0;
    z-index: var(--z-dropdown);
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    border: 1.5px solid rgba(251,191,36,0.35);
    border-radius: 4px;
    box-shadow: 0 10px 28px rgba(0,0,0,0.55), inset 0 1px 0 rgba(255,255,255,0.06);
    font-size: var(--fs-sm);
    color: var(--algo-slate);
    display: flex;
    flex-direction: column;
    max-height: 16rem;
  }

  /* Optional search input row — pinned to the top of the panel when
     `searchable` is on. Hint pill calls out how many more characters
     the operator needs to type before filtering activates. */
  .rbq-select-search {
    position: relative;
    padding: 0.25rem 0.35rem;
    border-bottom: 1px solid var(--algo-amber-border-soft);
    flex: 0 0 auto;
  }
  .rbq-select-search-input {
    width: 100%;
    background: rgba(13,21,38,0.55);
    border: 1px solid var(--c-action-22);
    border-radius: 3px;
    padding: 0.25rem 0.45rem;
    color: var(--algo-slate);
    font-family: inherit;
    font-size: var(--fs-sm);
    outline: none;
  }
  .rbq-select-search-input:focus {
    border-color: var(--c-action);
  }
  .rbq-select-search-input::placeholder {
    color: rgba(180,200,230,0.45);
  }
  .rbq-select-search-hint {
    position: absolute;
    right: 0.55rem;
    top: 50%;
    transform: translateY(-50%);
    font-size: var(--fs-2xs);
    color: rgba(251,191,36,0.7);
    pointer-events: none;
    letter-spacing: 0.04em;
  }

  /* Options list — flex: 1 1 auto + overflow-y so the list area
     scrolls while the search bar stays pinned. */
  .rbq-select-options {
    list-style: none;
    margin: 0;
    padding: 0.2rem 0;
    overflow-y: auto;
    flex: 1 1 auto;
  }
  .rbq-select-empty {
    padding: 0.6rem 0.55rem;
    color: rgba(180,200,230,0.55);
    font-style: italic;
    font-size: var(--fs-sm);
    text-align: center;
  }

  .rbq-select-option {
    padding: 0.3rem 0.55rem;
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 0.5rem;
    cursor: pointer;
    transition: background 0.06s, color 0.06s;
    border-left: 2px solid transparent;
  }
  .rbq-select-option-hl,
  .rbq-select-option:hover {
    background: rgba(251,191,36,0.15);
    color: var(--c-action);
    border-left-color: var(--c-action);
  }
  .rbq-select-option-selected {
    color: var(--c-action);
    font-weight: 700;
  }
  .rbq-select-option-selected::before {
    content: '✓';
    color: var(--c-long);
    margin-right: 0.3rem;
  }
  .rbq-select-option-label { flex: 1 1 auto; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .rbq-select-option-hint {
    flex: 0 0 auto;
    font-size: var(--fs-xs);
    color: rgba(180,200,230,0.55);
  }

  /* ── Light theme (public palette — cream + champagne gold) ─────────── */
  .rbq-select-light { color: #1a1e35; }
  .rbq-select-light .rbq-select-trigger {
    background-image: none;
    background-color: #ffffff;
    border: 1px solid #c0ccdc;
    color: #1e3050;
  }
  .rbq-select-light .rbq-select-trigger:hover:not(:disabled) { border-color: #c8a84b; }
  .rbq-select-light .rbq-select-trigger:focus                { border-color: #c8a84b; }
  .rbq-select-light.rbq-select-open .rbq-select-trigger      { border-color: #c8a84b; }
  .rbq-select-light .rbq-select-placeholder { color: #64748b; }
  .rbq-select-light .rbq-select-caret       { color: #c8a84b; }

  .rbq-select-light .rbq-select-panel {
    background: #ffffff;
    border: 1.5px solid #c8a84b;
    box-shadow: 0 10px 24px rgba(12,24,48,0.18);
    color: #1a1e35;
  }
  .rbq-select-light .rbq-select-search {
    border-bottom: 1px solid #e7e0cf;
  }
  .rbq-select-light .rbq-select-search-input {
    background: #faf7f0;
    border-color: #c0ccdc;
    color: #1e3050;
  }
  .rbq-select-light .rbq-select-search-input:focus { border-color: #c8a84b; }
  .rbq-select-light .rbq-select-search-input::placeholder { color: #94a3b8; }
  .rbq-select-light .rbq-select-search-hint { color: #c8a84b; }
  .rbq-select-light .rbq-select-empty { color: #64748b; }
  .rbq-select-light .rbq-select-option {
    border-left-color: transparent;
  }
  .rbq-select-light .rbq-select-option-hl,
  .rbq-select-light .rbq-select-option:hover {
    background: rgba(200,168,75,0.18);
    color: #0c1830;
    border-left-color: #c8a84b;
  }
  .rbq-select-light .rbq-select-option-selected {
    color: #0c1830;
    font-weight: 700;
  }
  .rbq-select-light .rbq-select-option-selected::before { color: #15803d; }
  .rbq-select-light .rbq-select-option-hint { color: #64748b; }
</style>
