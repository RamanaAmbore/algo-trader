<script>
  // SymbolSearchInput — reusable symbol search dropdown.
  //
  // Mirrors the /orders page symbol-picker pattern (proven shipped code)
  // with an optional pinned-symbols section (from ChartWorkspace) on top.
  //
  // Props:
  //   value         — bound current tradingsymbol ($bindable)
  //   pins          — optional pinned labels shown before search results
  //   resolvePin    — optional: pin label → tradeable tradingsymbol (or null)
  //   type          — restrict search results: ALL|EQ|FUT|OPT (default ALL)
  //   placeholder   — input placeholder text
  //   minChars      — minimum chars before search fires (default 3)
  //   onPick        — fires when a row is chosen (sym, meta?)
  //   ariaLabel     — accessible label for the input

  import { untrack, onMount } from 'svelte';
  import { loadInstruments, searchByPrefix } from '$lib/data/instruments';
  import { loadWatchlistSymbols } from '$lib/data/watchlistSymbols';
  import { _BARE_UNDERLYINGS } from '$lib/data/accounts';
  import { displaySymbol } from '$lib/data/displaySymbol.js';

  /** True when sym is a bare non-tradable underlying (CRUDEOIL, NIFTY, …). */
  function _isBareRoot(/** @type {string} */ sym) {
    return _BARE_UNDERLYINGS.has(String(sym || '').toUpperCase());
  }

  let {
    value      = $bindable(/** @type {string} */ ('')),
    pins       = /** @type {string[]} */ ([]),
    resolvePin = /** @type {((pin: string) => string | null) | null} */ (null),
    type       = /** @type {'ALL'|'EQ'|'FUT'|'OPT'} */ ('ALL'),
    placeholder = 'Type 3+ chars…',
    minChars   = 3,
    onPick     = /** @type {(sym: string, meta?: {pinLabel?: string, exchange?: string, type?: string}) => void} */ ((_sym, _meta) => {}),
    ariaLabel  = 'Symbol search',
  } = $props();

  let _symQuery       = $state('');
  let _symOpen        = $state(false);
  let _symSuggestions = $state(/** @type {any[]} */ ([]));
  let _symDebounce    = /** @type {any} */ (null);
  // Tracks the last pin the operator picked so the row gets an active highlight.
  let _activePin      = $state('');
  // True while the async debounce + IDB-backed searchByPrefix is in flight
  // and the sync fast-path (suggestUnderlyings) returned nothing. Drives
  // a "Searching…" hint so the operator doesn't see an empty dropdown
  // and assume the search broke.
  let _searching      = $state(false);

  // Warm the instruments cache as soon as the component mounts. Without
  // this, the FIRST keystrokes hit suggestUnderlyings before the IDB
  // dump is loaded, the sync path returns empty, and the dropdown
  // looks broken until the 50 ms debounce fires.
  onMount(() => {
    loadInstruments().catch(() => {});
    // Auto-fetch pinned watchlist items when the caller didn't supply
    // a `pins` prop. Operator request: "in symbol dropdown, when until
    // 3 chars are entered, the pinned symbols should always show first."
    // Every callsite (Orders entry, Pulse search, etc.) now gets pinned
    // suggestions for free without having to plumb the prop.
    if (!pins.length) _autoLoadPins();
  });

  async function _autoLoadPins() {
    try {
      // Single source of truth — see loadWatchlistSymbols. Operator:
      // "Symbol dropdown is not showing all the pinned symbols from
      // pulse. every symbol dropdown should be consistent and use
      // the same code". The loader returns EVERY tradingsymbol the
      // operator has watchlisted (pinned + user-created lists), in
      // the same order Pulse renders them — pinned/global first.
      // Filter bare underlying roots (CRUDEOIL, NIFTY, …) — they're
      // not directly tradable and confuse the picker.
      const { syms } = await loadWatchlistSymbols();
      const tradable = syms.filter(s => !_isBareRoot(s));
      if (tradable.length) pins = tradable;
    } catch (_) { /* leave pins empty */ }
  }

  // Sync _symQuery when the `value` prop changes externally (mount or
  // parent-driven swap). Reads _symQuery via `untrack` so the operator's
  // own typing doesn't re-trigger the effect (which would revert the
  // input back to `value` on every keystroke — the source of the
  // "symbol not updatable" bug).
  $effect(() => {
    const v = value;
    if (v && displaySymbol(v) !== untrack(() => _symQuery)) _symQuery = displaySymbol(v);
  });

  // Re-run search when the type filter changes from the outside.
  $effect(() => {
    void type;
    if (_symQuery.length >= minChars) _runSearch(_symQuery);
  });

  /** Filter a list of instrument rows by the active type prop. */
  function _filterByType(/** @type {any[]} */ rows) {
    if (type === 'ALL') return rows;
    return rows.filter(r => {
      const t = String(r?.t || '').toUpperCase();
      if (type === 'EQ')  return t === 'EQ' || t === '';
      if (type === 'FUT') return t === 'FUT';
      if (type === 'OPT') return t === 'CE' || t === 'PE';
      return true;
    });
  }

  /** Core search — debounced async full-instrument search.
   *  The former sync fast-path via suggestUnderlyings() is removed:
   *  that function returns bare underlying NAMES (CRUDEOIL, NIFTY, …),
   *  not tradable tradingsymbols, so it produced unplaceable rows in
   *  the dropdown. searchByPrefix() returns real instrument-cache rows. */
  async function _runSearch(/** @type {string} */ v) {
    _symSuggestions = [];
    _searching = true;
    if (_symDebounce) clearTimeout(_symDebounce);
    _symDebounce = setTimeout(async () => {
      try {
        await loadInstruments();
        // Fetch wide (80) then filter — searchByPrefix front-loads EQ rows
        // so a small limit would starve OPT/FUT picks on heavy-volume names.
        const full = await searchByPrefix(v, 80);
        const typed = _filterByType(Array.isArray(full) ? full : []);
        // Drop bare underlying roots — they're not directly tradable.
        // EQ rows whose tradingsymbol exactly matches a bare root are
        // excluded (e.g. the equity "NIFTY" entry for the index name).
        // Virtual roots (GOLD, GOLD_NEXT) injected by searchByPrefix are
        // intentionally tradable picks — do NOT drop them via _isBareRoot.
        const tradable = typed.filter(r => {
          if (r?.virtual) return true;
          const sym = String(r?.s || r?.sym || r?.tradingsymbol || '').toUpperCase();
          return !_isBareRoot(sym);
        });
        if (tradable.length) _symSuggestions = tradable.slice(0, 14);
        else _symSuggestions = [];
      } catch (_) { /* leave empty */ }
      finally { _searching = false; }
    }, 50);
  }

  function _onInput(/** @type {string} */ v) {
    _symQuery = v;
    _symOpen = true;
    _searching = false;
    if (!v) { _symSuggestions = []; return; }
    if (v.length < minChars) { _symSuggestions = []; return; }
    _runSearch(v);
  }

  /** Pick a result-row instrument. */
  function _pickInst(/** @type {any} */ inst) {
    // Kite instrument-cache rows expose the symbol as `s` (compact);
    // older shapes used `sym`/`tradingsymbol`. Without the `inst.s`
    // fallback the bound `value` got the typed query (e.g. "REL")
    // instead of the picked symbol (e.g. "RELIANCE") and the chart
    // returned "no data available".
    const sym = String(inst?.s || inst?.sym || inst?.tradingsymbol || _symQuery).toUpperCase();
    value = sym;
    // Show display form in the text box (GOLD.NEXT); store the internal key
    // (GOLD_NEXT) in `value` so callers receive the canonical identifier.
    _symQuery = displaySymbol(sym);
    _symOpen = false;
    _symSuggestions = [];
    _activePin = '';
    onPick(sym, { exchange: inst?.e || '', type: inst?.t || '', virtual: inst?.virtual ?? false });
  }

  /** Pick a pinned label — optionally resolved to a tradeable symbol. */
  function _pickPin(/** @type {string} */ pin) {
    let resolved = pin;
    if (resolvePin) {
      const r = resolvePin(pin);
      if (r) resolved = r;
    }
    const upper = String(resolved || '').toUpperCase();
    if (!upper) return;
    value = upper;
    _symQuery = pin;   // input shows the friendly pin label
    _symOpen = false;
    _symSuggestions = [];
    _activePin = pin;
    onPick(upper, { pinLabel: pin });
  }

  function _onKeydown(/** @type {KeyboardEvent} */ e) {
    if (e.key === 'Escape') { _symOpen = false; _symSuggestions = []; }
    if (e.key === 'Enter') {
      e.preventDefault();
      // Prefer first result row; fall back to first pin if no results.
      if (_symSuggestions.length) { _pickInst(_symSuggestions[0]); return; }
      if (_symQuery.length < minChars && pins.length) { _pickPin(pins[0]); }
    }
  }
</script>

<div class="ssi-wrap">
  <input
    class="ssi-input"
    type="text"
    {placeholder}
    bind:value={_symQuery}
    oninput={(e) => _onInput(/** @type {HTMLInputElement} */ (e.currentTarget).value)}
    onfocus={() => { _symOpen = true; }}
    onblur={() => { setTimeout(() => { _symOpen = false; }, 180); }}
    onkeydown={_onKeydown}
    aria-label={ariaLabel}
    autocomplete="off"
    spellcheck="false"
  />
  {#if _symOpen}
    <div class="ssi-drop" role="listbox">
      {#if _symQuery.length < minChars}
        <!-- Below the typing threshold — show pinned symbols if any. -->
        {#if pins.length}
          <div class="ssi-section">Pinned</div>
          {#each pins as pin}
            <button type="button"
                    class="ssi-row"
                    class:ssi-row-active={_activePin === pin}
                    role="option"
                    aria-selected={_activePin === pin}
                    onmousedown={(e) => { e.preventDefault(); _pickPin(pin); }}>
              <span class="ssi-row-sym">{pin}</span>
            </button>
          {/each}
        {/if}
        {#if _symQuery.length === 0 && !pins.length}
          <div class="ssi-hint">Type {minChars}+ chars to search…</div>
        {:else if _symQuery.length > 0 && _symQuery.length < minChars}
          <div class="ssi-hint">Type {minChars - _symQuery.length} more char{minChars - _symQuery.length === 1 ? '' : 's'}…</div>
        {/if}
      {:else if _searching && !_symSuggestions.length}
        <div class="ssi-hint">Searching…</div>
      {:else if !_symSuggestions.length}
        <div class="ssi-hint">No match{type !== 'ALL' ? ` for ${type}` : ''}.</div>
      {:else}
        {#if pins.length}
          <div class="ssi-section">Results</div>
        {/if}
        {#each _symSuggestions as inst ((inst.s ?? inst.sym ?? inst.tradingsymbol ?? '') + ':' + (inst.e ?? '') + ':' + (inst.t ?? ''))}
          <button type="button"
                  class="ssi-row"
                  role="option"
                  aria-selected="false"
                  onmousedown={(e) => { e.preventDefault(); _pickInst(inst); }}>
            <!-- Kite instrument cache uses the compact `s` field
                 (matches the key fallback above); older shapes used
                 `sym`/`tradingsymbol`. Without the `inst.s` fallback
                 the row rendered as empty + only the exchange/type
                 meta line was visible. -->
            <span class="ssi-row-sym">{displaySymbol(inst.s || inst.sym || inst.tradingsymbol || '')}</span>
            <span class="ssi-row-meta">{inst.e || ''}{inst.t ? ' · ' + inst.t : ''}{inst.virtual ? ' · virtual' : ''}</span>
          </button>
        {/each}
      {/if}
    </div>
  {/if}
</div>

<style>
  .ssi-wrap {
    position: relative;
    display: inline-flex;
    align-items: center;
  }

  .ssi-input {
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.10);
    border-radius: 3px;
    padding: 0.18rem 0.45rem;
    color: var(--c-action);
    font-size: var(--fs-lg);
    font-weight: 800;
    font-family: var(--font-numeric);
    letter-spacing: 0.04em;
    width: 11rem;
    text-transform: uppercase;
    outline: none;
  }
  .ssi-input:focus {
    border-color: var(--algo-amber-border);
    background: rgba(251, 191, 36, 0.06);
  }
  .ssi-input::placeholder {
    color: rgba(251, 191, 36, 0.40);
    font-weight: 600;
    text-transform: none;
  }

  .ssi-drop {
    position: absolute;
    top: 100%;
    left: 0;
    z-index: var(--z-search);
    margin-top: 2px;
    min-width: 14rem;
    max-height: 18rem;
    overflow-y: auto;
    background: #1b2540;
    border: 1px solid rgba(251, 191, 36, 0.35);
    border-radius: 4px;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.55);
    display: flex;
    flex-direction: column;
  }

  .ssi-section {
    font-family: monospace;
    font-size: var(--fs-xs);
    font-weight: 800;
    color: var(--algo-muted);
    letter-spacing: 0.08em;
    text-transform: uppercase;
    padding: 0.3rem 0.55rem 0.15rem;
    border-bottom: 1px solid rgba(255, 255, 255, 0.05);
  }

  .ssi-hint {
    padding: 0.3rem 0.55rem 0.4rem;
    font-family: monospace;
    font-size: var(--fs-sm);
    color: var(--algo-muted);
    border-top: 1px solid rgba(255, 255, 255, 0.05);
  }

  .ssi-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.2rem 0.4rem;
    background: transparent;
    border: 0;
    color: var(--algo-slate);
    font-size: var(--fs-md);
    font-family: var(--font-numeric);
    cursor: pointer;
    text-align: left;
    width: 100%;
  }
  .ssi-row:hover {
    background: rgba(251, 191, 36, 0.12);
    color: var(--c-action);
  }
  .ssi-row-active {
    background: rgba(251, 191, 36, 0.12);
    border-left: 2px solid rgba(251, 191, 36, 0.65);
    padding-left: calc(0.4rem - 2px);
  }

  .ssi-row-sym {
    font-weight: 700;
    letter-spacing: 0.03em;
  }
  .ssi-row-meta {
    color: var(--algo-muted);
    font-size: var(--fs-xs);
    letter-spacing: 0.06em;
  }
</style>
