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
   *   allowedKinds — Sprint B (2026-10, Phase 2) — optional array
   *                restricting compiled statement kinds (see
   *                `compileStatement`'s own docstring in agents.js for
   *                the full contract). Threaded straight into this
   *                component's own debounced compile call so an order-
   *                scoped mount's error list shows the real scope
   *                rejection live, not just at submit time. `undefined`
   *                (the default) means no restriction.
   *   placeholder — textarea placeholder text. Defaults to the full
   *                WHEN/ALERT/DO example; order-scoped mounts override
   *                this with a plain `order(...)` example so the
   *                placeholder itself signals the narrower scope.
   *   info       — Sprint C (2026-10, Phase 2 "Automation redesign") —
   *                `string[]`, zero or more non-fatal hint/status lines
   *                rendered ABOVE the compile-error/preview area —
   *                e.g. "Editing agent 'foo-bar' — Save will update, not
   *                create." when the host has entered edit mode. Purely
   *                presentational; this component never populates it
   *                itself. Omit/empty = no info block rendered.
   *   submitStatus — Sprint C — `{ok: boolean, text: string} | null`, the
   *                outcome of the LAST actual backend submit attempt (as
   *                opposed to client-side compile errors, which are
   *                always shown via the existing `.agent-cli-errors`
   *                block regardless of this prop). Rendered as a small
   *                persistent status line — distinct from (and in
   *                addition to) any toast the host also fires, since a
   *                toast alone disappears too fast to serve as a record
   *                of "did this actually save." The host sets/clears this
   *                — this component never calls a submit endpoint itself.
   */

  import { tick } from 'svelte';
  import {
    compileAgentCliStatement, suggestAgentCliAt, describeErrorPosition,
    decompileAgent, decompileEventAgent, decompileBracket, decompileToken,
  } from '$lib/command/grammars/agents.js';

  /** @type {{
   *   value: string, catalog: any, lotSizeOf?: (sym: string) => number|null,
   *   mode?: 'paper'|'live',
   *   onCompile?: (result: any) => void,
   *   onOrderSubmit?: (result: any) => void,
   *   allowedKinds?: string[],
   *   placeholder?: string,
   *   info?: string[],
   *   submitStatus?: {ok: boolean, text: string} | null,
   * }} */
  let {
    value = $bindable(''), catalog = null, lotSizeOf = undefined,
    mode = 'paper', onCompile = undefined, onOrderSubmit = undefined,
    allowedKinds = undefined,
    placeholder = 'WHEN pnl_pct@positions.total <= -5 ALERT telegram DO order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1)',
    info = [], submitStatus = null,
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
        result = compileAgentCliStatement(text, cat, { mode: m, lotSizeOf, allowedKinds });
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
    if (!catalog) return { suggestions: [], descriptions: [], labels: [], replaceRange: [cursorPos, cursorPos], kind: null };
    try { return suggestAgentCliAt(value, cursorPos, catalog); }
    catch { return { suggestions: [], descriptions: [], labels: [], replaceRange: [cursorPos, cursorPos], kind: null }; }
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

  // Sprint 20 (item 5) — visual position indicator for a compile/parse
  // error that carries a real `position` (an ever-growing share of them,
  // after this sprint's `_err()` position-threading work). `\n` is
  // swapped 1-for-1 for `⏎` so the caret's character-offset alignment
  // under the snippet stays correct even when the error sits near a
  // line break in a multi-line statement (`describeErrorPosition` itself
  // stays UI-agnostic — this swap is presentation-only, done here).
  function _posIndicator(err) {
    if (typeof err.position !== 'number') return null;
    const d = describeErrorPosition(value, err.position);
    if (!d) return null;
    return { ...d, snippet: d.snippet.replace(/\n/g, '⏎') };
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

  // Sprint C (2026-10, Phase 2 "Automation redesign") — the "Generated
  // statement(s)" section, item 4 of the result-area redesign. Re-runs the
  // compiled JSON back through the matching DECOMPILER so the operator
  // sees the canonical, normalized CLI text the compiler actually resolved
  // to (e.g. `mean_pnl(minutes=30)` -> `mean_pnl(30)` normalization),
  // never just the raw text they typed. For `kind:'order'` there is no
  // decompiler at all (a bare order/basket statement has no agent/bracket
  // JSON shape to decompile back FROM) — shown instead as a compact
  // `order(...)`-shaped SUMMARY of each leg that will actually be
  // submitted, built locally here rather than inventing a real order
  // decompiler for a display-only purpose. A basket renders one summary
  // line PER LEG, not one combined blob, per the redesign's own
  // instruction. Best-effort: any decompile failure here silently yields
  // no canonical-text section (the JSON preview below still renders) —
  // this is a display nicety, never a gate on submit.
  function _legSummaryText(/** @type {any} */ leg) {
    if (!leg) return '';
    const side = leg.side || leg.transaction_type || '?';
    const parts = [`account=${JSON.stringify(leg.account ?? '')}`, `symbol=${JSON.stringify(leg.symbol ?? '')}`, `side=${side}`];
    if (leg.lots != null) parts.push(`lots=${leg.lots}`);
    else if (leg.qty != null) parts.push(`qty=${leg.qty}`);
    if (leg.price != null) parts.push(`price=${leg.price}`);
    if (leg.order_type != null) parts.push(`order_type=${leg.order_type}`);
    return `order(${parts.join(', ')})`;
  }

  const canonicalLines = $derived.by(() => {
    if (!compiled?.ok || !catalog) return null;
    try {
      if (compiled.kind === 'agent') {
        const r = decompileAgent(compiled.agent, catalog, { lotSizeOf });
        return r.text ? [r.text] : null;
      }
      if (compiled.kind === 'event') {
        const r = decompileEventAgent(compiled.agent, catalog, { lotSizeOf });
        return r.text ? [r.text] : null;
      }
      if (compiled.kind === 'bracket') {
        const r = decompileBracket(compiled.template, catalog, { lotSizeOf });
        return r.text ? [r.text] : null;
      }
      if (compiled.kind === 'token') {
        const r = decompileToken(compiled.fragment, catalog, { lotSizeOf });
        return r.text ? [r.text] : null;
      }
      if (compiled.kind === 'order') {
        if (compiled.ticket) return [_legSummaryText(compiled.ticket)];
        if (compiled.basket) {
          const lines = (compiled.basket.groups || []).flatMap((g) => (g.legs || []).map(_legSummaryText));
          return lines.length ? lines : null;
        }
      }
    } catch { /* display-only — never block the JSON preview on this */ }
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
    {placeholder}
  ></textarea>

  {#if suggestResult.suggestions.length}
    <div class="agent-cli-suggest-row" role="listbox" aria-label="CLI suggestions">
      {#each suggestResult.suggestions.slice(0, 12) as s, i (s)}
        <!-- Sprint 15 — `labels[i]` (set only for a 'column' suggestion,
             e.g. "P&L (₹)") is shown INSTEAD of the literal insertable
             text `s` (e.g. "positions.total.pnl") when present — the
             operator reads the short label, but `applySuggestion` below
             always inserts the real `s` string, never the label. -->
        <button type="button" class="agent-cli-suggest-pill"
                onclick={() => applySuggestion(s)}
                onmouseenter={() => { _hoverLabel = s; }}
                onmouseleave={() => { _hoverLabel = null; }}
                onfocus={() => { _hoverLabel = s; }}
                onblur={() => { _hoverLabel = null; }}>{suggestResult.labels?.[i] || s}</button>
      {/each}
    </div>
    {#if _hoverDesc}
      <div class="agent-cli-suggest-desc">{_hoverDesc}</div>
    {/if}
  {/if}

  <!-- Sprint C (2026-10, Phase 2 "Automation redesign") — the result area
       now holds up to four distinct sections: (1) compile errors [1] —
       unchanged markup/classes, existing specs select on them; (2) info/
       hint lines [1] (editing-mode banner, future normalization notes);
       (3) the compiled JSON preview + newly-added canonical generated-
       statement text [1]; (4) the post-SUBMIT (backend call) result,
       distinct from client-side compile errors above. Wrapped in ONE
       scrollable container so a long statement's full result never
       pushes the rest of the page down — same `max-height` + `overflow-y:
       auto` convention /automation's own `.agent-group-grid`/
       `.automation-mini-grid` scroll caps already use. -->
  <div class="agent-cli-result">
    {#if info && info.length}
      <div class="agent-cli-info">
        {#each info as line}<div class="agent-cli-info-line">{line}</div>{/each}
      </div>
    {/if}

    {#if compiled && !compiled.ok}
      <div class="agent-cli-errors">
        <div class="agent-cli-errors-title">Errors:</div>
        <ul class="list-disc ml-4">
          {#each compiled.errors as err}
            <li>
              {err.message}{typeof err.position === 'number' ? ` (line ${_posIndicator(err)?.line}, col ${_posIndicator(err)?.column})` : ''}
              {#if _posIndicator(err)}
                {@const p = _posIndicator(err)}
                <div class="agent-cli-error-pos">
                  <div class="agent-cli-error-pos-snippet">{p.truncatedStart ? '…' : ''}{p.snippet}{p.truncatedEnd ? '…' : ''}</div>
                  <div class="agent-cli-error-pos-caret" style="--caret-col: {p.caretOffset + (p.truncatedStart ? 1 : 0)}">^</div>
                </div>
              {/if}
            </li>
          {/each}
        </ul>
      </div>
    {:else if previewPayload != null}
      <div class="agent-cli-preview">
        {#if canonicalLines?.length}
          <div class="agent-cli-canonical">
            <div class="agent-cli-canonical-title">Generated statement{canonicalLines.length > 1 ? 's' : ''}:</div>
            {#each canonicalLines as line, i (i)}
              <pre class="agent-cli-canonical-line">{line}</pre>
            {/each}
          </div>
        {/if}
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

    {#if submitStatus}
      <div class="agent-cli-submit-status" class:is-error={!submitStatus.ok}>{submitStatus.text}</div>
    {/if}
  </div>
</div>

<style>
  .agent-cli-editor { display: flex; flex-direction: column; gap: 0.4rem; }
  /* Sprint C — scrolls internally (never grows unbounded) once a long
     decompiled/typed statement exceeds the visible rows — same
     overflow-y: auto convention the result area below uses. */
  .agent-cli-textarea { width: 100%; resize: vertical; overflow-y: auto; }
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
    /* Sprint 15 — a param's description can carry a generated
       required_if/mutually-exclusive line appended on its own newline
       (see _paramConstraintText in agents.js) — preserve it. */
    white-space: pre-line;
  }
  .agent-cli-errors {
    padding: 0.4rem 0.6rem; border-radius: 0.3rem;
    background: var(--algo-red-bg-mid);
    border: 1px solid var(--algo-red-border-soft);
    color: var(--algo-red-text-bright);
    font-size: var(--fs-sm);
  }
  .agent-cli-errors-title { font-weight: 600; margin-bottom: 0.2rem; }
  /* Sprint C — the shared result container: errors / info / preview /
     submit-status sections all live inside this one scrollable box so
     a long result never pushes the rest of the host page down. Modest
     height bump over the old single-section layout (was implicitly
     capped at .agent-cli-preview-json's own 220px) since up to 4
     sections can now stack; scrollbar only appears once content
     actually overflows. */
  .agent-cli-result {
    display: flex; flex-direction: column; gap: 0.4rem;
    max-height: 26rem;
    overflow-y: auto;
  }
  .agent-cli-info {
    padding: 0.35rem 0.6rem; border-radius: 0.3rem;
    background: var(--algo-sky-bg-soft);
    border: 1px solid var(--algo-sky-border-soft);
    color: var(--algo-sky);
    font-size: var(--fs-sm);
  }
  .agent-cli-info-line + .agent-cli-info-line { margin-top: 0.15rem; }
  .agent-cli-canonical {
    margin-bottom: 0.4rem;
    padding-bottom: 0.4rem;
    border-bottom: 1px dashed var(--algo-green-border-soft);
  }
  .agent-cli-canonical-title {
    font-weight: 600;
    font-size: var(--fs-xs);
    color: var(--algo-muted);
    margin-bottom: 0.15rem;
  }
  .agent-cli-canonical-line {
    font-family: var(--font-mono, monospace);
    font-size: var(--fs-sm);
    white-space: pre-wrap;
    margin: 0 0 0.2rem 0;
  }
  .agent-cli-submit-status {
    padding: 0.35rem 0.6rem; border-radius: 0.3rem;
    background: var(--algo-green-bg);
    border: 1px solid var(--algo-green-border-soft);
    color: var(--c-long, #4ade80);
    font-size: var(--fs-sm);
    font-weight: 600;
  }
  .agent-cli-submit-status.is-error {
    background: var(--algo-red-bg-mid);
    border-color: var(--algo-red-border-soft);
    color: var(--algo-red-text-bright);
  }
  /* Sprint 20 (item 5) — visual position indicator: a horizontally-
     scrollable monospace snippet window with a `^` caret aligned under
     the exact error offset via a `ch`-unit margin (one `ch` ≈ one
     monospace character width, so the caret lines up with the snippet
     above regardless of the surrounding font-size) — `overflow-x: auto`
     keeps this readable on a <600px phone viewport without wrapping
     and losing caret alignment. */
  .agent-cli-error-pos {
    margin: 0.15rem 0 0.3rem 0;
    overflow-x: auto;
    font-family: var(--font-mono, monospace);
    font-size: var(--fs-xs);
  }
  .agent-cli-error-pos-snippet { white-space: pre; }
  .agent-cli-error-pos-caret { white-space: pre; margin-left: calc(var(--caret-col) * 1ch); color: var(--algo-amber); }
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
    /* Sprint C — no own max-height/overflow here anymore: the outer
       `.agent-cli-result` is now the single scroll container (avoids a
       nested-scrollbar-inside-a-scrollbar layout). */
  }
  .agent-cli-place-order { font-size: var(--fs-md); padding: 0.25rem 0.9rem; }
</style>
