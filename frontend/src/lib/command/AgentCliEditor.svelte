<script>
  /**
   * AgentCliEditor — Sprint 4 UI for the agent CLI grammar
   * (frontend/src/lib/command/grammars/agents.js, Sprints 1-3). Renders a
   * single textarea + a below-the-textarea suggestion row (NOT caret-
   * anchored — deliberate scope decision, see .claude/PLAN.md) + either a
   * red error list or a green-bordered read-only JSON preview of the
   * compiled payload.
   *
   * Props:
   *   value      — bindable CLI text.
   *   catalog    — already-fetched catalog from `fetchAgentCatalog()`
   *                (this component NEVER calls it itself — the parent
   *                fetches once and passes it down so toggling
   *                Structured↔CLI repeatedly never refetches).
   *   lotSizeOf  — `(symbol) => number|null` lot-size resolver, threaded
   *                into `compileAgentCliStatement`'s opts.
   *   mode       — 'paper'|'live', threaded as opts.mode for a bare
   *                order statement.
   *   onCompile  — `(result) => void`, fired on every successful compile
   *                (debounced ~150ms) AFTER the operator has actually
   *                edited the text — never on mount/prop-reseed, so
   *                switching tabs can't silently overwrite whatever the
   *                Structured tab's own JSON textareas currently hold.
   *   onOrderSubmit — `(result) => void`, fired when the operator clicks
   *                "Place order" for a `kind:'order'` compile result.
   */

  import { tick } from 'svelte';
  import {
    compileAgentCliStatement, suggestAgentCliAt,
  } from '$lib/command/grammars/agents.js';

  /** @type {{
   *   value: string, catalog: any, lotSizeOf?: (sym: string) => number|null,
   *   mode?: 'paper'|'live',
   *   onCompile?: (result: any) => void,
   *   onOrderSubmit?: (result: any) => void,
   * }} */
  let {
    value = $bindable(''), catalog = null, lotSizeOf = undefined,
    mode = 'paper', onCompile = undefined, onOrderSubmit = undefined,
  } = $props();

  /** @type {HTMLTextAreaElement|null} */
  let textareaEl = $state(null);
  let cursorPos = $state(0);
  // LABEL (not index) of whichever suggestion pill is currently hovered or
  // keyboard-focused — drives the small description detail line below the
  // row (Sprint 11). Deliberately keyed by the suggestion's own string,
  // not its position: the suggestion list re-filters on every keystroke
  // (same `suggestResult` derived below), so an index captured while the
  // mouse rested on pill 3 would silently point at whatever suggestion
  // happens to occupy slot 3 AFTER a re-filter — including a completely
  // different token — the moment the operator keeps typing without the
  // mouse ever leaving the pill (no `mouseleave` fires in that case).
  // Looking the label back up in the CURRENT `suggestResult.suggestions`
  // below means a label that has since dropped out of the list simply
  // shows no description, never a stale/wrong one. `null` when nothing
  // is hovered/focused.
  let _hoverLabel = $state(null);
  // Flips true on the FIRST real operator edit (typed char or applied
  // suggestion) after mount/prop-reseed — gates `onCompile` so a freshly
  // decompiled/seeded `value` never fires a write-back into the parent's
  // JSON fields before the operator has touched anything.
  let dirty = $state(false);

  /** @type {{ok:boolean, errors:any[], kind:string|null, agent:any, ticket:any, basket:any, template:any, fragment:any}|null} */
  let compiled = $state(null);

  $effect(() => {
    const text = value;
    const cat = catalog;
    const m = mode === 'live' ? 'live' : 'paper';
    const wasDirty = dirty;
    const timer = setTimeout(() => {
      if (!cat) { compiled = null; return; }
      let result;
      try {
        result = compileAgentCliStatement(text, cat, { mode: m, lotSizeOf });
      } catch (e) {
        result = { ok: false, errors: [{ message: e?.message || 'compile failed' }], kind: null, agent: null, ticket: null, basket: null, template: null, fragment: null };
      }
      compiled = result;
      if (result.ok && wasDirty) onCompile?.(result);
    }, 150);
    return () => clearTimeout(timer);
  });

  function _updateCursor() {
    cursorPos = textareaEl ? (textareaEl.selectionStart ?? value.length) : value.length;
  }

  function handleInput() {
    dirty = true;
    _updateCursor();
  }

  const suggestResult = $derived.by(() => {
    if (!catalog) return { suggestions: [], descriptions: [], replaceRange: [cursorPos, cursorPos], kind: null };
    try { return suggestAgentCliAt(value, cursorPos, catalog); }
    catch { return { suggestions: [], descriptions: [], replaceRange: [cursorPos, cursorPos], kind: null }; }
  });

  async function applySuggestion(s) {
    const [start, end] = suggestResult.replaceRange || [cursorPos, cursorPos];
    const before = value.slice(0, start);
    const after = value.slice(end);
    value = before + s + after;
    dirty = true;
    const newCursor = start + s.length;
    await tick();
    textareaEl?.focus();
    textareaEl?.setSelectionRange(newCursor, newCursor);
    cursorPos = newCursor;
  }

  function handlePlaceOrder() {
    if (compiled?.ok && compiled.kind === 'order') onOrderSubmit?.(compiled);
  }

  // Description of whichever suggestion pill is currently hovered/focused,
  // or '' when nothing is hovered or the hovered suggestion has no
  // backend-provided description (e.g. an enum value, or an undescribed
  // token/param) — callers never render an empty detail box for ''.
  const _hoverDesc = $derived.by(() => {
    if (_hoverLabel == null) return '';
    const i = suggestResult.suggestions.indexOf(_hoverLabel);
    return i >= 0 ? (suggestResult.descriptions?.[i] || '') : '';
  });

  // Sprint 14 — previously returned null for 'event'/'bracket'/'token',
  // so the JSON preview silently stayed blank for those compile kinds
  // even though they'd compiled successfully (only 'agent'/'order' had a
  // preview). Now uniform across every compile kind this editor can
  // produce.
  const previewPayload = $derived.by(() => {
    if (!compiled?.ok) return null;
    if (compiled.kind === 'agent' || compiled.kind === 'event') return compiled.agent;
    if (compiled.kind === 'order') return compiled.ticket || { groups: compiled.basket?.groups };
    if (compiled.kind === 'bracket') return compiled.template;
    if (compiled.kind === 'token') return compiled.fragment;
    return null;
  });
</script>

<div class="agent-cli-editor">
  <textarea
    bind:this={textareaEl}
    bind:value
    oninput={handleInput}
    onclick={_updateCursor}
    onkeyup={_updateCursor}
    onfocus={_updateCursor}
    class="agent-cli-textarea field-input font-mono text-[length:var(--fs-sm)]"
    rows="6"
    spellcheck="false"
    placeholder={'WHEN mean_pnl(minutes=30)@positions.total <= -50000 ALERT telegram DO place_order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", qty=75)'}
  ></textarea>

  {#if suggestResult.suggestions.length}
    <div class="agent-cli-suggest-row" role="listbox" aria-label="CLI suggestions">
      {#each suggestResult.suggestions.slice(0, 12) as s (s)}
        <button type="button" class="agent-cli-suggest-pill"
                onclick={() => applySuggestion(s)}
                onmouseenter={() => { _hoverLabel = s; }}
                onmouseleave={() => { _hoverLabel = null; }}
                onfocus={() => { _hoverLabel = s; }}
                onblur={() => { _hoverLabel = null; }}>{s}</button>
      {/each}
    </div>
    {#if _hoverDesc}
      <div class="agent-cli-suggest-desc">{_hoverDesc}</div>
    {/if}
  {/if}

  {#if compiled && !compiled.ok}
    <div class="agent-cli-errors">
      <div class="agent-cli-errors-title">Errors:</div>
      <ul class="list-disc ml-4">
        {#each compiled.errors as err}
          <li>{err.message}{typeof err.position === 'number' ? ` (pos ${err.position})` : ''}</li>
        {/each}
      </ul>
    </div>
  {:else if previewPayload != null}
    <div class="agent-cli-preview">
      <pre class="agent-cli-preview-json">{JSON.stringify(previewPayload, null, 2)}</pre>
      {#if compiled?.kind === 'order'}
        <button type="button" class="btn-primary agent-cli-place-order" onclick={handlePlaceOrder}>
          {#if compiled.ticket}
            <!-- Single-leg ticket: `mode` IS the real client-requested
                 field (server still gates it against the kill-switch). -->
            Place order ({mode === 'live' ? 'LIVE' : 'PAPER'})
          {:else}
            <!-- Multi-leg basket: BasketOrderRequest has no client mode
                 field at all — resolved server-side, unknown here. -->
            Place basket order
          {/if}
        </button>
      {/if}
    </div>
  {/if}
</div>

<style>
  .agent-cli-editor { display: flex; flex-direction: column; gap: 0.4rem; }
  .agent-cli-textarea { width: 100%; resize: vertical; }
  .agent-cli-suggest-row {
    display: flex; flex-wrap: wrap; gap: 0.3rem;
    padding: 0.3rem; border-radius: 0.3rem;
    background: var(--algo-sky-bg-soft);
    border: 1px solid var(--algo-sky-border-soft);
  }
  .agent-cli-suggest-pill {
    font-size: var(--fs-sm);
    padding: 0.1rem 0.5rem;
    border-radius: 999px;
    border: 1px solid var(--algo-amber-border-soft);
    background: var(--algo-amber-bg);
    color: var(--algo-amber);
    font-family: var(--font-mono, monospace);
    cursor: pointer;
  }
  .agent-cli-suggest-pill:hover { background: var(--algo-amber-bg-strong); }
  .agent-cli-suggest-desc {
    font-size: var(--fs-xs);
    color: var(--algo-muted);
    padding: 0 0.3rem;
  }
  .agent-cli-errors {
    padding: 0.4rem 0.6rem; border-radius: 0.3rem;
    background: var(--algo-red-bg-mid);
    border: 1px solid var(--algo-red-border-soft);
    color: var(--algo-red-text-bright);
    font-size: var(--fs-sm);
  }
  .agent-cli-errors-title { font-weight: 600; margin-bottom: 0.2rem; }
  .agent-cli-preview {
    padding: 0.4rem 0.6rem; border-radius: 0.3rem;
    background: var(--algo-green-bg);
    border: 1px solid var(--algo-green-border-soft);
  }
  .agent-cli-preview-json {
    font-family: var(--font-mono, monospace);
    font-size: var(--fs-sm);
    white-space: pre-wrap;
    margin: 0 0 0.4rem 0;
    max-height: 220px;
    overflow: auto;
  }
  .agent-cli-place-order { font-size: var(--fs-md); padding: 0.25rem 0.9rem; }
</style>
