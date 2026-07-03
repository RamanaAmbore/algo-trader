<script>
  // Generic grammar-driven command bar with token-by-token autocomplete.
  // Usage:
  //   <CommandBar {grammar} context={{...}} onsubmit={(parsed)=>{...}} placeholder="…" />
  //
  // Props:
  //   grammar  — grammar object from command/engine.js conventions
  //   context  — extra ctx passed to suggesters (e.g. { openOrderIds: [...] })
  //   onsubmit — called with `parse(line, grammar, context)` on Enter (if errors.length === 0)
  //   onsubmitRaw — called with raw parse output regardless of errors (optional)
  //   placeholder, rows, showHelp, disabled — passthrough
  //
  // Keys: Arrow up/down to navigate suggestions, Tab or Enter to complete,
  //       Enter on empty suggestion or after valid command → submit.
  //       Escape closes the dropdown.

  import { suggestAt, applySuggestion, parse } from '$lib/command/engine';

  let {
    grammar,
    context = {},
    onsubmit = () => {},
    onsubmitRaw = null,
    placeholder = '',
    rows = 2,
    showHelp = true,
    disabled = false,
    initialValue = '',
    minPrefixLen = 3,
    previewFn = null,
    enrichPairs = null,
    class: cls = '',
  } = $props();

  let symbolPreview = $state('');

  // intentional: initialValue seeds the textarea once; operator owns it thereafter
  // svelte-ignore state_referenced_locally
  let value     = $state($state.snapshot(initialValue));
  let cursor    = $state(0);
  let suggOpen  = $state(false);
  let suggIdx   = $state(0);
  let suggList  = $state(/** @type {string[]} */([]));
  let role      = $state(/** @type {string|null} */(null));
  let hint      = $state(/** @type {string|null} */(null));
  let parsedPairs = $state(/** @type {{role:string, value:string, status:string}[]} */([]));
  let errors    = $state(/** @type {string[]} */([]));
  let taEl;
  let suggListEl = $state(/** @type {HTMLDivElement | null} */(null));
  let prevRole = '';
  let prevFocus = -1;
  // Shortcut kwargs tracked separately — not in textarea
  let _shortcutKwargs = $state(/** @type {Record<string,string>} */ ({}));
  let _pendingKwarg = $state(/** @type {string|null} */ (null));

  function _currentPrefix() {
    // The text of the token currently under the cursor (or empty if at a space).
    const before = value.slice(0, cursor);
    const lastSpace = before.lastIndexOf(' ');
    const tok = lastSpace >= 0 ? before.slice(lastSpace + 1) : before;
    // Strip any kwarg-key portion (chase=) — we want just the value part
    const eq = tok.indexOf('=');
    return eq >= 0 ? tok.slice(eq + 1) : tok;
  }

  function _computeParsedPairs(currentRole) {
    if (!grammar) return [];
    const verbName = value.trim().split(/\s+/)[0]?.toLowerCase();
    if (!verbName) return [];
    const verb = grammar.verbs?.[verbName];
    if (!verb?.tokens) return [];
    let args = {}, kwargs = {}, ctx = { ...context };
    try {
      const p = parse(value, grammar, context);
      args = p.args || {}; kwargs = p.kwargs || {};
      ctx = { ...context, ...args, ...kwargs };
    } catch {}
    const pairs = [];
    for (const spec of verb.tokens) {
      const isFilled = spec.role in args;
      if (typeof spec.required === 'function' && !spec.required(ctx) && !isFilled) continue;
      const val = args[spec.role];
      let status = 'pending';
      if (val !== undefined) status = 'filled';
      else if (spec.role === currentRole) status = 'current';
      pairs.push({ role: spec.role, value: val !== undefined ? String(val) : '', status });
    }
    for (const [k, v] of Object.entries(kwargs)) {
      pairs.push({ role: k, value: String(v), status: v ? 'filled' : 'current' });
    }
    // Show shortcut kwargs as chips
    for (const [k, v] of Object.entries(_shortcutKwargs)) {
      if (!(k in kwargs)) pairs.push({ role: k, value: v, status: 'filled' });
    }
    // Show pending kwarg being selected
    if (_pendingKwarg && !(_pendingKwarg in kwargs) && !(_pendingKwarg in _shortcutKwargs)) {
      pairs.push({ role: _pendingKwarg, value: '', status: 'current' });
    }
    ctx._verb = verbName;
    return enrichPairs ? enrichPairs(pairs, ctx) : pairs;
  }

  function refreshSuggestions() {
    if (_pendingKwarg) { parsedPairs = _computeParsedPairs(`kwarg:${_pendingKwarg}`); return; }
    if (!grammar) { suggList = []; parsedPairs = []; return; }
    try {
      const result = suggestAt(value, cursor, grammar, context);
      let newList = result.suggestions || [];
      const newRole = result.role;
      const roleChanged = newRole !== prevRole;
      // Enforce minimum prefix length for large-list roles (symbol/strike/etc.)
      // Verb, account, kwarg-key, and fixed-value roles (orderType, instType, chase)
      // always show. Free-text roles require `minPrefixLen` chars typed.
      const alwaysShowRoles = new Set(['verb', 'account', 'orderType', 'instType', 'order_id', 'qty', 'price', 'strike', 'expiry', 'chase']);
      const currentPrefix = _currentPrefix();
      const isGatedRole = !alwaysShowRoles.has(newRole) && !newRole?.startsWith('kwarg:');
      if (isGatedRole && currentPrefix.length < minPrefixLen) {
        newList = [];
      }
      suggList = newList;
      role = newRole;
      hint = result.hint;
      parsedPairs = _computeParsedPairs(newRole);
      // Apply _focusIndex on role change OR when it changes (e.g. ATM
      // recalculated after async equity quote arrives).
      const focus = /** @type {any} */ (newList)._focusIndex;
      const hasFocus = typeof focus === 'number' && focus >= 0 && focus < newList.length;
      if (roleChanged) {
        suggIdx = hasFocus ? focus : 0;
        prevFocus = hasFocus ? focus : -1;
      } else if (hasFocus && focus !== prevFocus) {
        suggIdx = focus;
        prevFocus = focus;
      } else {
        suggIdx = Math.min(suggIdx, Math.max(0, newList.length - 1));
      }
      prevRole = newRole;
      if (!suggOpen && newList.length > 0) suggOpen = true;
      if (newList.length === 0) suggOpen = false;
      // Scroll active item into view after DOM updates
      if (suggOpen && newList.length > 0) {
        queueMicrotask(_scrollActiveIntoView);
      }
    } catch (e) {
      suggList = []; suggOpen = false; parsedPairs = [];
    }
  }

  function _scrollActiveIntoView() {
    if (!suggListEl) return;
    const active = suggListEl.querySelector('.cmd-suggest-item.active');
    if (active) active.scrollIntoView({ block: 'center' });
  }

  function refreshErrors() {
    if (!grammar || !value.trim()) { errors = []; symbolPreview = ''; return; }
    try {
      const p = parse(value, grammar, context);
      // Only show errors that are NOT "missing X" — those are expected while
      // the user is still typing. Show only validation errors (wrong value, etc.)
      // Filter: "missing X" (still typing) and "unexpected token: VALUE" for shortcut kwargs
      const kwVals = new Set(Object.values(_shortcutKwargs).map(v => v.toUpperCase()));
      errors = (p.errors || []).filter(e =>
        !e.startsWith('missing ') &&
        !(e.startsWith('unexpected token: ') && kwVals.has(e.slice(18).toUpperCase()))
      );
      // Inject shortcut kwargs for preview
      if (Object.keys(_shortcutKwargs).length > 0) Object.assign(p.kwargs, _shortcutKwargs);
      symbolPreview = (previewFn && (p.errors || []).filter(e => !e.startsWith('missing ') && !e.startsWith('unexpected token:')).length === 0) ? (previewFn(p) || '') : '';
    } catch (e) {
      errors = [e.message]; symbolPreview = '';
    }
  }

  function applyCurrent() {
    if (suggList.length === 0) return false;
    const pick = suggList[suggIdx];

    // If pending kwarg shortcut, store value and show it in textarea
    if (_pendingKwarg) {
      const val = pick.includes('=') ? pick.split('=')[1] : pick;
      _shortcutKwargs = { ..._shortcutKwargs, [_pendingKwarg]: val };
      _pendingKwarg = null;
      // Append bare value to textarea
      const needsSpace = value.length > 0 && !value.endsWith(' ');
      value = value + (needsSpace ? ' ' : '') + val + ' ';
      cursor = value.length;
      suggOpen = false;
      queueMicrotask(() => {
        if (taEl) {
          taEl.focus();
          taEl.value = value;
          taEl.setSelectionRange(cursor, cursor);
        }
        refreshSuggestions();
        refreshErrors();
      });
      return true;
    }

    const result = applySuggestion(value, cursor, pick);
    value = result.line;
    cursor = result.cursor;
    suggOpen = false;
    queueMicrotask(() => {
      if (taEl) {
        taEl.focus();
        taEl.setSelectionRange(cursor, cursor);
      }
      refreshSuggestions();
      refreshErrors();
    });
    return true;
  }

  function submit() {
    if (!value.trim()) return;
    const p = parse(value, grammar, context);
    // Inject shortcut kwargs into parsed result
    if (Object.keys(_shortcutKwargs).length > 0) {
      Object.assign(p.kwargs, _shortcutKwargs);
    }
    if (onsubmitRaw) onsubmitRaw({ ...p, _line: value });
    if (!p.errors || p.errors.length === 0) {
      onsubmit(p);
    } else {
      errors = p.errors || [];
    }
  }

  function onKeydown(e) {
    if (e.key === 'ArrowDown' && suggOpen) {
      e.preventDefault();
      suggIdx = (suggIdx + 1) % suggList.length;
      queueMicrotask(_scrollActiveIntoView);
    } else if (e.key === 'ArrowUp' && suggOpen) {
      e.preventDefault();
      suggIdx = (suggIdx - 1 + suggList.length) % suggList.length;
      queueMicrotask(_scrollActiveIntoView);
    } else if (e.key === 'Tab') {
      if (applyCurrent()) e.preventDefault();
    } else if (e.key === 'Escape') {
      if (_pendingKwarg) _pendingKwarg = null;
      suggOpen = false;
    } else if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  }

  function onInput(e) {
    value = e.target.value;
    cursor = e.target.selectionStart;

    // Kwarg shortcut: typing 'p'/'P' at end → activate kwarg popup (not in textarea)
    const shortcuts = grammar?.kwargShortcuts;
    if (shortcuts && cursor === value.length && cursor >= 2 && !_pendingKwarg) {
      const lastChar = value.slice(-1);
      const prevChar = value.slice(-2, -1);
      if ((prevChar === ' ' || prevChar === '') && shortcuts[lastChar]) {
        const kwarg = shortcuts[lastChar];
        if (!(kwarg in _shortcutKwargs)) {
          // Erase the typed char
          value = value.slice(0, -1);
          if (!value.endsWith(' ')) value = value.trimEnd() + ' ';
          cursor = value.length;
          _pendingKwarg = kwarg;
          queueMicrotask(() => {
            if (taEl) {
              taEl.value = value;
              taEl.setSelectionRange(cursor, cursor);
            }
            // Show kwarg values as suggestions
            const verb = grammar.verbs?.[value.trim().split(/\s+/)[0]?.toLowerCase()];
            const spec = verb?.kwargs?.[kwarg];
            if (spec?.values) {
              suggList = spec.values;
              role = `kwarg:${kwarg}`;
              suggIdx = 0;
              suggOpen = true;
              parsedPairs = _computeParsedPairs(role);
            }
          });
          return;
        }
      }
    }

    // Clean up shortcut kwargs if their value was deleted from textarea
    if (Object.keys(_shortcutKwargs).length > 0) {
      const tokens = value.trim().split(/\s+/).map(t => t.toUpperCase());
      const updated = /** @type {Record<string,string>} */ ({});
      for (const [k, v] of Object.entries(_shortcutKwargs)) {
        if (tokens.includes(v.toUpperCase())) updated[k] = v;
      }
      if (Object.keys(updated).length !== Object.keys(_shortcutKwargs).length) {
        _shortcutKwargs = updated;
      }
    }
    // Reset pending kwarg if user backspaces
    if (_pendingKwarg) { _pendingKwarg = null; }

    refreshSuggestions();
    refreshErrors();
  }

  function onSelect(e) {
    cursor = e.target.selectionStart;
    refreshSuggestions();
  }

  function onFocus() {
    refreshSuggestions();
  }

  function onBlur() {
    // Delay so click on suggestion still fires
    setTimeout(() => { suggOpen = false; }, 120);
  }

  function pickSuggestion(i) {
    suggIdx = i;
    applyCurrent();
  }

  // NOTE: intentionally no $effect on `context` — suggestions and errors are
  // recomputed only on actual keystrokes / focus / cursor moves. Reacting to
  // every context change would re-run the (expensive) symbol suggester every
  // time the parent polls orders/websocket, freezing the input.

  export function clear() { value = ''; cursor = 0; suggOpen = false; errors = []; parsedPairs = []; symbolPreview = ''; _shortcutKwargs = {}; _pendingKwarg = null; }
  export function setValue(v) { value = v; cursor = v.length; refreshSuggestions(); refreshErrors(); }
  export function setValueQuiet(v) { value = v; cursor = v.length; suggOpen = false; parsedPairs = _computeParsedPairs(null); refreshErrors(); }
  export function refresh() { refreshSuggestions(); refreshErrors(); }
  export { submit };
</script>

<div class="cmdbar {cls}">
  <div class="cmd-chips-area">
    {#if parsedPairs.length > 0}
      <div class="cmd-pairs">
        {#each parsedPairs as p}
          <span class="pair pair-{p.status}">
            <span class="pair-key">{p.role}:</span>
            {#if p.value}
              <span class="pair-val">{p.value}</span>
            {:else if p.status === 'current'}
              <span class="pair-cursor">_</span>
            {/if}
          </span>
        {/each}
        {#if symbolPreview}<span class="symbol-preview">{symbolPreview}</span>{/if}
      </div>
    {/if}
  </div>
  <div class="cmd-container" style="position:relative">
    <textarea
      bind:this={taEl}
      {placeholder}
      {rows}
      {disabled}
      class="cmd-input-inner font-mono text-xs w-full"
      value={value}
      oninput={onInput}
      onkeydown={onKeydown}
      onselect={onSelect}
      onfocus={onFocus}
      onblur={onBlur}
    ></textarea>
    {#if suggOpen && suggList.length > 0}
    <div class="cmd-suggest" bind:this={suggListEl}>
      {#each suggList as item, i}
        <button
          type="button"
          tabindex="-1"
          onmousedown={(e) => { e.preventDefault(); pickSuggestion(i); }}
          class="cmd-suggest-item {i === suggIdx ? 'active' : ''}"
        >{role === 'kwarg-key' && item.endsWith('=') ? item.slice(0, -1).toUpperCase()
          : role?.startsWith('kwarg:') && item.includes('=') ? item.split('=')[1]
          : item}</button>
      {/each}
    </div>
  {/if}
  </div>

  {#if showHelp && (hint || errors.length > 0)}
    <div class="text-[0.55rem] flex gap-3 items-center">
      {#if hint}<span class="text-muted opacity-70">{hint}</span>{/if}
      {#if errors.length > 0}<span class="text-red-500">{errors.join(' · ')}</span>{/if}
    </div>
  {/if}
</div>

<style>
  .cmdbar {
    position: relative;
    width: 100%;
  }
  .cmd-chips-area {
    background: #152033;
    border: 1px solid #334155;
    border-left: 2px solid var(--c-action);
    border-bottom: 1px solid #1e2d42;
    border-radius: 0.375rem 0.375rem 0 0;
    height: 4.5rem;
    padding: 0.2rem 0;
    overflow-y: auto;
    box-shadow: inset 0 1px 2px rgba(0,0,0,0.3);
  }
  .cmd-container {
    border: 1px solid #334155;
    border-left: 2px solid var(--c-action);
    border-radius: 0 0 0.375rem 0.375rem;
    background: #152033;
    color: #f1f5f9;
    position: relative;
    overflow: visible;
    box-shadow: inset 0 1px 2px rgba(0,0,0,0.3);
  }
  .cmd-container:focus-within {
    border-color: #fbbf2466;
    border-left: 2px solid var(--c-action);
  }
  .cmd-pairs {
    display: flex;
    flex-wrap: wrap;
    gap: 0.25rem;
    padding: 0.15rem 0.5rem;
    align-items: center;
  }
  .cmd-input-inner {
    caret-color: var(--c-action);
    border: none !important;
    outline: none !important;
    background: transparent !important;
    resize: none;
    padding: 0.35rem 0.5rem 1.5rem;
    color: var(--algo-slate);
    width: 100%;
    display: block;
    overflow-y: auto;
  }
  .cmd-suggest {
    position: absolute;
    top: 0;
    left: 0;
    min-width: 160px;
    max-width: 100%;
    width: max-content;
    max-height: 220px;
    overflow-y: auto;
    background: #1a2332;
    border: 1px solid #334155;
    border-radius: 0.375rem;
    box-shadow: 0 4px 12px rgba(0,0,0,0.25);
    z-index: 40;
    font-size: var(--fs-lg);
  }
  .cmd-suggest-item {
    display: block;
    width: 100%;
    text-align: left;
    padding: 0.2rem 0.7rem;
    background: transparent;
    border: none;
    color: var(--algo-slate);
    font-family: var(--font-numeric);
    cursor: pointer;
    white-space: nowrap;
  }
  .cmd-suggest-item:hover { background: rgba(255,255,255,0.06); }
  .cmd-suggest-item.active { background: rgba(251,191,36,0.2); color: var(--c-action); }
  :global(.text-accent) { color: var(--c-action); }

  .symbol-preview {
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    font-weight: 600;
    color: var(--c-info);
    background: var(--c-info-14);
    padding: 0.1rem 0.4rem;
    border-radius: 0.25rem;
  }
  .pair {
    display: inline-flex;
    align-items: center;
    gap: 0.15rem;
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    padding: 0.05rem 0.3rem;
    border-radius: 0.2rem;
    border: 1px solid transparent;
  }
  .pair-filled {
    background: var(--c-long-10);
    border-color: rgba(74,222,128,0.25);
  }
  .pair-filled .pair-key { color: var(--c-long); }
  .pair-current {
    background: var(--algo-amber-bg);
    border-color: var(--algo-amber-border-soft);
  }
  .pair-current .pair-key { color: var(--c-action); }
  .pair-pending {
    background: rgba(100,116,139,0.06);
    border-color: rgba(100,116,139,0.12);
  }
  .pair-pending .pair-key { color: #475569; }
  .pair-key {
    font-weight: 600;
    text-transform: uppercase;
    font-size: var(--fs-2xs);
  }
  .pair-val {
    color: var(--algo-slate);
    font-weight: 500;
  }
  .pair-cursor {
    color: var(--c-action);
    animation: blink 1s step-end infinite;
  }
  @keyframes blink { 50% { opacity: 0; } }
  @media (prefers-reduced-motion: reduce) {
    .pair-cursor { animation: none; }
  }
</style>
