<script>
  // ConsoleTab — Automation's Console tab (Sprint B, 2026-10, Phase 2
  // "Console retirement"; re-unified Sprint I Phase 1, 2026-10-11, "unify
  // console's two CLI boxes into one window via a dispatch layer").
  // Ported body of the retired `/console` route
  // (frontend/src/routes/(algo)/console/+page.svelte, now a redirect to
  // `/automation?tab=console`) — ONE visible CLI textbox (AgentCliEditor,
  // agents.js grammar — FULL statement scope: order, bracket, agent,
  // event, token — no `allowedKinds` restriction, unlike the two
  // order-scoped mounts added in Sprint B on /orders and the SymbolPanel
  // order-ticket modal, see OrderCliTab.svelte), backed by a dispatch
  // layer (`consoleDispatch.js:dispatchConsoleStatement`) that tries the
  // generic grammar first and falls back to the legacy positional
  // orders.js grammar (CommandLineTab, mounted `headless` — see that
  // component's own prop doc) for anything it doesn't recognize
  // (buy/sell/cancel/modify, `agent ...` interpret, raw shell).
  //
  // Phase 1 of 2, explicitly: the legacy parser is NOT deleted or
  // retired — it becomes invisible fallback machinery behind the one
  // visible box. Full retirement (Phase 2) is gated on a parity
  // checklist that hasn't passed yet (cancel/modify verbs ACTUALLY doing
  // something — see CommandLineTab.svelte's `runExternal` doc, they are
  // pre-existing no-ops today — agent-ai /interpret, /api/admin/exec
  // passthrough, up-arrow recall — which does not exist anywhere in this
  // CLI stack today, Phase 1 or before; there is nothing to lose here).
  //
  // The page-level Activity log (5-tab ActivityLogSurface) that used to
  // render below the command bars on /console is NOT re-ported here —
  // /automation already has its own bottom LogPanel for that; porting a
  // second copy would duplicate the surface CLAUDE.md's "Activity
  // surface architecture" section describes as a single shared store.

  import { get as getStore } from 'svelte/store';
  import { authStore, executionMode } from '$lib/stores';
  import { toast } from '$lib/data/toastStore.svelte.js';
  import { safeRead } from '$lib/utils/safeRead.js';
  import CommandLineTab from '$lib/order/CommandLineTab.svelte';
  import ConfirmModal from '$lib/ConfirmModal.svelte';
  import AgentCliEditor from '$lib/command/AgentCliEditor.svelte';
  import { fetchAgentCatalog, compileAgentCliStatement } from '$lib/command/grammars/agents.js';
  import {
    submitConsoleAgent, submitConsoleBracket, submitConsoleToken,
    updateConsoleAgent, updateConsoleBracket, updateConsoleToken,
    resolveConsoleOrder, submitResolvedConsoleOrder,
    classifyConsoleStatement, dispatchConsoleStatement,
  } from '$lib/command/consoleDispatch';

  /** @type {{
   *   value: string,
   *   editTarget?: {rowKind: 'cycle'|'event'|'template'|'bracket', ref: string|number, label: string} | null,
   * }} */
  let { value = $bindable(''), editTarget = $bindable(null) } = $props();

  // Sprint C (2026-10, Phase 2 "Automation redesign") — `editTarget` is
  // set by the Agents-tab row list's Edit action (`/automation/+page.svelte`)
  // right before it decompiles a row into `value` and switches to this
  // tab. Maps the row's own kind to the ONE compiled statement `kind`
  // that row can legally resave as — a cycle-kind Agent must recompile
  // as `agent`, an event-kind Agent as `event`, a Token as `token`, a
  // Bracket as `bracket`. Never falls back to CREATE on a mismatch (e.g.
  // the operator rewrote a decompiled agent statement into a bracket
  // statement while still "in" an agent's edit session) — Save simply
  // refuses with a clear error in that case; the operator must either
  // fix the text back to the expected kind or explicitly Cancel edit
  // first (which is the only way to fall through to the ordinary
  // create-only buttons below).
  const _EDIT_KIND_FOR_ROW_KIND = { cycle: 'agent', event: 'event', template: 'token', bracket: 'bracket' };
  const _ROW_KIND_LABEL = { cycle: 'agent', event: 'alert', template: 'token', bracket: 'bracket' };

  function cancelEdit() {
    editTarget = null;
    value = '';
    _cliSubmitStatus = null;
  }

  const isDemo = $derived(!$authStore.user);

  let _agentCliCatalog = $state(/** @type {any} */ (null));
  let _agentCliCatalogError = $state('');

  $effect(() => {
    if (isDemo) return; // /admin/grammar/tokens requires auth
    if (_agentCliCatalog || _agentCliCatalogError) return;
    fetchAgentCatalog()
      .then((cat) => { _agentCliCatalog = cat; _agentCliCatalogError = ''; })
      .catch(() => { _agentCliCatalogError = 'CLI catalog unavailable'; });
  });

  // Sprint I Phase 1 (2026-10-11) — the ONE typed textbox is now always
  // AgentCliEditor's own `value`; CommandLineTab is mounted `headless`
  // (see below) and driven programmatically via `runExternal`, so there
  // is no longer a separate bar an operator could type agent-family
  // syntax into for a hand-off — the old `_onAgentStatement` hand-off
  // (`onAgentStatement` prop) is unreachable from this mount and was
  // removed; CommandLineTab.svelte keeps the prop itself as a generic,
  // self-contained extension point (same idiom as its unused
  // `onParsedOrder`/`onAddToBasket` props) for any future caller that
  // mounts it visibly again.

  /** Live classification of the single textbox's current text — 'agents'
   *  routes through the existing compileAgentCliStatement flow below
   *  unchanged; 'orders'/'legacy' route through the CommandLineTab
   *  fallback; 'empty' shows neither action button. */
  const _classify = $derived(classifyConsoleStatement(value));

  // Sprint I Phase 1 follow-up's statement-kind hint selector (pills) was
  // relocated to the page level (2026-10, `/automation/+page.svelte`) as
  // a dropdown sharing the Console/Agents tab row — it only ever operated
  // on `value` (bindable, already owned by the page) with zero other
  // consumers in this component, so nothing else here changed.

  function _execModeForCompile() {
    return safeRead(executionMode) === 'live' ? 'live' : 'paper';
  }

  /** Recompiles `value` synchronously at submit time — never trusts a
   *  possibly-stale cached result (AgentCliEditor's own debounced
   *  preview may be up to 150ms behind, or mid-invalid). Full statement
   *  scope — no `allowedKinds` — this tab is the un-restricted mount. */
  function _recompileAgentCli() {
    if (!_agentCliCatalog) {
      return { ok: false, errors: [{ message: 'CLI catalog unavailable' }], kind: null, agent: null, ticket: null, basket: null, template: null, fragment: null };
    }
    try {
      return compileAgentCliStatement(value, _agentCliCatalog, { mode: _execModeForCompile() });
    } catch (e) {
      return { ok: false, errors: [{ message: e?.message || 'compile failed' }], kind: null, agent: null, ticket: null, basket: null, template: null, fragment: null };
    }
  }

  const _agentCliCompiled = $derived.by(_recompileAgentCli);

  let _liveConfirmRef = $state(/** @type {{ ask: (opts: any) => Promise<boolean> } | null} */ (null));

  function _short(/** @type {any} */ msg) { return String(msg || '').slice(0, 32); }

  async function _submitAgentCliOrder(/** @type {any} */ result) {
    const resolved = await resolveConsoleOrder(result);
    if (!resolved.ok) {
      const _err = /** @type {any} */ (resolved).error;
      _cliSubmitStatus = { ok: false, text: `Order failed: ${_err}` };
      toast.error(_short(_err));
      return;
    }
    const isLive = result.ticket?.mode === 'live' || !!result.basket;
    if (isLive) {
      const ok = await _liveConfirmRef?.ask({
        title: 'Place LIVE order?',
        message: 'This CLI statement will place a real order against the broker.',
        danger: true,
        confirmLabel: 'Place LIVE order',
        cancelLabel: 'Cancel',
      });
      if (!ok) return;
    }
    const mode = getStore(executionMode) || 'paper';
    const r = await submitResolvedConsoleOrder(resolved, { mode });
    if (r.ok) {
      _cliSubmitStatus = { ok: true, text: r.kind === 'basket' ? 'Basket order placed' : 'Order placed' };
      toast.success(r.kind === 'basket' ? 'Basket order placed' : 'Order placed');
      value = '';
    } else {
      _cliSubmitStatus = { ok: false, text: `Order failed: ${/** @type {any} */ (r).error}` };
      toast.error(`Order failed: ${_short(/** @type {any} */ (r).error)}`);
    }
  }

  // Sprint C — post-SUBMIT (real backend call) result, distinct from
  // AgentCliEditor's own client-side compile errors. Shown as a small
  // persistent status line in the result area (toast alone disappears
  // too fast to serve as a record of "did this actually save") — see
  // AgentCliEditor's own `submitStatus` prop doc.
  let _cliSubmitStatus = $state(/** @type {{ok: boolean, text: string} | null} */ (null));

  // Sprint C — the "Editing X — Save will update, not create" banner,
  // rendered via AgentCliEditor's `info` prop. Clears automatically once
  // `editTarget` clears (Save success or explicit Cancel edit).
  const _cliInfo = $derived.by(() => {
    if (!editTarget) return [];
    const label = _ROW_KIND_LABEL[editTarget.rowKind] || editTarget.rowKind;
    return [`Editing ${label} "${editTarget.label}" — Save will update, not create.`];
  });

  /** True iff the CURRENT compiled result's kind matches what the row
   *  under edit must recompile as — see `_EDIT_KIND_FOR_ROW_KIND`'s own
   *  doc. `false` (not an error) when `editTarget` is null — callers use
   *  this only to decide update-vs-create routing, never to gate
   *  compiling itself. */
  function _editKindMatches(/** @type {any} */ compiled) {
    if (!editTarget) return false;
    return compiled?.kind === _EDIT_KIND_FOR_ROW_KIND[editTarget.rowKind];
  }

  async function _submitAgentCliAgent(/** @type {any} */ result) {
    if (editTarget && (editTarget.rowKind === 'cycle' || editTarget.rowKind === 'event')) {
      if (!_editKindMatches(result)) {
        toast.error(`Editing ${_ROW_KIND_LABEL[editTarget.rowKind]} — statement no longer compiles as that kind`);
        return;
      }
      const r = await updateConsoleAgent(/** @type {string} */ (editTarget.ref), result.agent, value);
      if (r.ok) {
        _cliSubmitStatus = { ok: true, text: `Agent '${r.slug}' updated` };
        toast.success(`Agent updated: ${r.slug}`);
        editTarget = null; value = '';
      } else {
        _cliSubmitStatus = { ok: false, text: `Update failed: ${r.error}` };
        toast.error(`Agent update failed: ${_short(r.error)}`);
      }
      return;
    }
    const r = await submitConsoleAgent(result.agent, value);
    if (r.ok) {
      _cliSubmitStatus = { ok: true, text: `Agent '${r.slug}' created` };
      toast.success(`Agent created: ${r.slug}`); value = '';
    } else {
      _cliSubmitStatus = { ok: false, text: `Create failed: ${r.error}` };
      toast.error(`Agent failed: ${_short(r.error)}`);
    }
  }

  async function _submitAgentCliBracket(/** @type {any} */ result) {
    if (editTarget && editTarget.rowKind === 'bracket') {
      if (!_editKindMatches(result)) {
        toast.error('Editing bracket — statement no longer compiles as a bracket');
        return;
      }
      const r = await updateConsoleBracket(/** @type {number} */ (editTarget.ref), result.template);
      if (r.ok) {
        _cliSubmitStatus = { ok: true, text: `Bracket '${result.template?.name || ''}' updated` };
        toast.success(`Bracket updated: ${result.template?.name || ''}`);
        editTarget = null; value = '';
      } else {
        _cliSubmitStatus = { ok: false, text: `Update failed: ${r.error}` };
        toast.error(`Bracket update failed: ${_short(r.error)}`);
      }
      return;
    }
    const r = await submitConsoleBracket(result.template);
    if (r.ok) {
      _cliSubmitStatus = { ok: true, text: `Bracket '${result.template?.name || ''}' created` };
      toast.success(`Bracket saved: ${result.template?.name || ''}`); value = '';
    } else {
      _cliSubmitStatus = { ok: false, text: `Create failed: ${r.error}` };
      toast.error(`Bracket failed: ${_short(r.error)}`);
    }
  }

  async function _submitAgentCliToken(/** @type {any} */ result) {
    if (editTarget && editTarget.rowKind === 'template') {
      if (!_editKindMatches(result)) {
        toast.error('Editing token — statement no longer compiles as a token');
        return;
      }
      const r = await updateConsoleToken(/** @type {string} */ (editTarget.ref), result.fragment, value);
      if (r.ok) {
        _cliSubmitStatus = { ok: true, text: `Token '${result.fragment?.name || ''}' updated` };
        toast.success(`Token updated: ${result.fragment?.name || ''}`);
        editTarget = null; value = '';
      } else {
        _cliSubmitStatus = { ok: false, text: `Update failed: ${r.error}` };
        toast.error(`Token update failed: ${_short(r.error)}`);
      }
      return;
    }
    const r = await submitConsoleToken(result.fragment, value);
    if (r.ok) {
      _cliSubmitStatus = { ok: true, text: `Token '${result.fragment?.name || ''}' created` };
      toast.success(`Token created: ${result.fragment?.name || ''}`); value = '';
    } else {
      _cliSubmitStatus = { ok: false, text: `Create failed: ${r.error}` };
      toast.error(`Token failed: ${_short(r.error)}`);
    }
  }

  async function _onAgentCliOrderSubmit(/** @type {any} */ _result) {
    const fresh = _recompileAgentCli();
    if (!fresh.ok || fresh.kind !== 'order') { toast.error('CLI changed — recheck before submit'); return; }
    await _submitAgentCliOrder(fresh);
  }

  // Sprint C follow-up (2026-10) — CLI order placement stays inline in
  // this panel, matching agent/bracket/token submission (see
  // `_submitAgentCliOrder` above). A prior SymbolPanel-ticket-MODAL hand-
  // off (`onParsedOrder` → `_ticketProps`) was removed here: audit found
  // `CommandLineTab`'s own `runParsed` never actually calls its
  // `onParsedOrder` prop (buy/sell submits directly via
  // `_submitPlaceOrder`, writing to its own `cmdHistory`) — the modal
  // mount was dead code, unreachable from any real operator action.

  // Sprint I Phase 1 (2026-10-11) — the hidden (`headless`) legacy
  // CommandLineTab instance, driven programmatically. `bind:this` gives
  // us its exported `runExternal`.
  let _cltRef = $state(/** @type {{ runExternal: (raw: string) => Promise<{ok:boolean,error?:string}> } | null} */ (null));

  /** Button label for the legacy-fallback action — '' when the current
   *  text isn't fallback-eligible (agent-family or empty), which also
   *  gates the button's visibility in the markup below. */
  const _fallbackLabel = $derived(
    _classify === 'orders' ? 'Submit order' : _classify === 'legacy' ? 'Run shell command' : ''
  );

  /** Fires the legacy orders.js parser on the current `value` via the
   *  hidden CommandLineTab's `runExternal`. Mirrors the agent-family
   *  submit functions' own conventions: clear + success toast + status
   *  line on success; preserve the text (so the operator can fix +
   *  retry) + error toast + status line on failure. The actual
   *  success/failure DETAIL for buy/sell/shell/`agent ...` output always
   *  lands in the still-visible `clt-history` list below — this status
   *  line is a short, consistent confirmation that something happened,
   *  not a duplicate of that detail. */
  async function _submitLegacyFallback() {
    const raw = value;
    if (!raw || !raw.trim()) return;
    const r = await _cltRef?.runExternal(raw);
    if (r && r.ok === false) {
      _cliSubmitStatus = { ok: false, text: `Command failed: ${_short(r.error)}` };
      toast.error(_short(r.error));
      return;
    }
    _cliSubmitStatus = { ok: true, text: 'Sent — see command history below.' };
    toast.success('Command sent');
    value = '';
  }

  /** Single entry point for "do whatever the current text's own action
   *  button would do" — shared by every kind-specific button's onclick
   *  AND the Ctrl/Cmd+Enter keyboard shortcut below, via
   *  `dispatchConsoleStatement` (consoleDispatch.js) so the keyboard
   *  path can never drift from the button-click path. */
  function _primaryAction() {
    if (isDemo || editTarget) return;
    dispatchConsoleStatement(value, {
      onAgentFamily: () => {
        if (!_agentCliCompiled?.ok) return;
        if (_agentCliCompiled.kind === 'order') return _onAgentCliOrderSubmit(_agentCliCompiled);
        if (_agentCliCompiled.kind === 'agent' || _agentCliCompiled.kind === 'event') return _submitAgentCliAgent(_agentCliCompiled);
        if (_agentCliCompiled.kind === 'bracket') return _submitAgentCliBracket(_agentCliCompiled);
        if (_agentCliCompiled.kind === 'token') return _submitAgentCliToken(_agentCliCompiled);
      },
      onLegacyFallback: () => _submitLegacyFallback(),
    });
  }

  // Sprint I Phase 1 — Ctrl/Cmd+Enter keyboard submit. AgentCliEditor's
  // own textarea deliberately treats a bare Enter as a literal newline
  // (multi-line statements are valid grammar — see agents.js's own
  // "Newlines count as ordinary whitespace" lexer rule), so submission
  // has always been button-driven for the agent-family path; this adds
  // a keyboard EQUIVALENT of whichever button is currently showing,
  // rather than changing Enter's own meaning. Attached on this
  // component's own wrapper div (NOT inside AgentCliEditor, which is
  // also used, unmodified, by the order-scoped OrderCliTab mounts) so
  // keydown simply bubbles up from the textarea.
  function _onWrapperKeydown(/** @type {KeyboardEvent} */ e) {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      _primaryAction();
    }
  }
</script>

<!-- Header button-group standardization, Part 8 (2026-10-13) — this
     component's own CardHeader/Fullscreen wrapper (`.console-card`,
     `_fsConsole`) was REMOVED; the shared Console/Agents tab-strip
     CardHeader at the page level (`automation/+page.svelte`'s
     `.aw-view`) now carries the ONE Fullscreen control for this whole
     area. ConsoleTab is only ever mounted inside that page-level
     wrapper (confirmed via grep — no standalone mount exists), so it
     doesn't need its own fullscreen awareness at all; the global
     `.fs-card-on`/`.fs-content-fill` CSS on the ancestor handles all
     the visual sizing. -->
<!-- svelte-ignore a11y_no_static_element_interactions -- this div only
     listens for Ctrl/Cmd+Enter as a keyboard-shortcut convenience over
     the real interactive elements it contains (the textarea + action
     buttons below); it is not itself an interactive control. -->
<div class="console-card-body" onkeydown={_onWrapperKeydown}>
    {#if isDemo}
      <div class="mb-2 p-2 rounded bg-purple-500/10 border border-purple-500/30 text-[0.65rem] text-purple-200">
        <strong class="text-purple-100">Demo view.</strong>
        Browse the command grammar + autocomplete. Submit + write paths are
        disabled — see <a href="/showcase" class="underline hover:text-purple-50">the tour</a>
        for what this terminal does in production.
      </div>
    {/if}

    <!-- "CLI" label, Guide link, and the statement-kind selector all
         relocated to the page level (2026-10) — they now share the
         Console/Agents tab row as a decorated CardHeader (see
         `/automation/+page.svelte`) instead of two separate rows here. -->

    <!-- Sprint I Phase 1 — the legacy positional orders.js parser, kept
         mounted `headless` (no visible input of its own) purely so its
         `runExternal()` and result/history list stay available as the
         fallback path for anything the generic grammar below doesn't
         recognize (buy/sell/modify/cancel, `agent ...` interpret, raw
         shell). See CommandLineTab.svelte's own `headless` prop doc. -->
    <CommandLineTab
      bind:this={_cltRef}
      headless
      prefillSide=""
      prefillAccount=""
      prefillSymbol=""
      prefillQty={0}
      prefillPrice={0}
      prefillOrderType="LIMIT" />

    <!-- The ONE visible CLI textbox — WHEN/GIVEN/ALERT/DO agents, bare
         order(...)/place_order(...), bracket(...), and token(...) — full
         statement scope, no allowedKinds restriction (this is the
         un-restricted Console mount; the order-only mounts live on
         /orders and the SymbolPanel modal via OrderCliTab). Anything
         that doesn't classify as agent-family falls back to the hidden
         CommandLineTab above via `_submitLegacyFallback`/`_primaryAction`. -->
    <div class="console-agent-cli-wrap">
      {#if editTarget}
        <!-- Sprint C — visible even while the catalog is still loading, so
             the operator always sees edit-mode is active. The Cancel
             action itself moved down into the shared bottom-right
             button row below (2026-10-13, operator request — Save and
             Cancel now live together, right-aligned, under the editor;
             this banner keeps only the informational "Editing ..." text). -->
        <div class="console-edit-banner">
          <span>Editing {_ROW_KIND_LABEL[editTarget.rowKind] || editTarget.rowKind} "{editTarget.label}"</span>
        </div>
      {/if}
      {#if _agentCliCatalogError}
        <div class="console-agent-cli-err">{_agentCliCatalogError}</div>
      {:else}
        <AgentCliEditor
          bind:value
          catalog={_agentCliCatalog}
          mode={_execModeForCompile()}
          onOrderSubmit={_onAgentCliOrderSubmit}
          info={_cliInfo}
          submitStatus={_cliSubmitStatus}
          showCompileErrors={_classify === 'agents'}
        />
        <!-- Save/Create + Cancel edit share one right-aligned button row
             at the bottom of the editor (2026-10-13, operator request). -->
        <div class="console-agent-cli-actions">
          {#if editTarget}
            <button type="button" class="console-edit-cancel" onclick={cancelEdit}>Cancel edit</button>
          {/if}
          {#if !isDemo && _agentCliCompiled?.ok && (_agentCliCompiled.kind === 'agent' || _agentCliCompiled.kind === 'event')}
            <button type="button" class="btn-primary console-agent-cli-btn"
              onclick={() => _submitAgentCliAgent(_agentCliCompiled)}>
              {editTarget && (editTarget.rowKind === 'cycle' || editTarget.rowKind === 'event') ? 'Save' : 'Create agent'}
            </button>
          {:else if !isDemo && _agentCliCompiled?.ok && _agentCliCompiled.kind === 'bracket'}
            <button type="button" class="btn-primary console-agent-cli-btn"
              onclick={() => _submitAgentCliBracket(_agentCliCompiled)}>
              {editTarget && editTarget.rowKind === 'bracket' ? 'Save' : 'Save bracket'}
            </button>
          {:else if !isDemo && _agentCliCompiled?.ok && _agentCliCompiled.kind === 'token'}
            <button type="button" class="btn-primary console-agent-cli-btn"
              onclick={() => _submitAgentCliToken(_agentCliCompiled)}>
              {editTarget && editTarget.rowKind === 'template' ? 'Save' : 'Create token'}
            </button>
          {:else if !isDemo && !editTarget && _fallbackLabel}
            <!-- Sprint I Phase 1 — legacy-grammar fallback action. Gated on
                 !editTarget: editing an existing row only ever recompiles
                 as agent/event/bracket/token, never a raw legacy command. -->
            <button type="button" class="btn-secondary console-agent-cli-btn console-fallback-btn"
              onclick={_submitLegacyFallback}>{_fallbackLabel}</button>
          {/if}
        </div>
      {/if}
    </div>
  </div>

<ConfirmModal bind:this={_liveConfirmRef} />

<style>
  /* Header button-group standardization, Part 8 (2026-10-13) — this
     component's own card chrome (`.console-card` section + CardHeader)
     was removed; Fullscreen now lives on the shared tab-strip header
     at the page level (`automation/+page.svelte`'s `.aw-view`), which
     also owns the `.fs-card-on`-driven sizing for this body. This div
     keeps only its own plain flex layout, independent of fullscreen
     state. */
  .console-card-body {
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  /* "CLI" label row, Guide link, and statement-kind pills all relocated
     to the page level (2026-10) — see `/automation/+page.svelte`'s
     CardHeader-wrapped tab row. */
  .console-agent-cli-wrap {
    flex-shrink: 0;
    margin-bottom: 0.4rem;
  }
  .console-agent-cli-err {
    padding: 0.4rem 0.6rem;
    border-radius: 0.3rem;
    background: var(--algo-red-bg-mid);
    border: 1px solid var(--algo-red-border-soft);
    color: var(--algo-red-text-bright);
    font-size: var(--fs-sm);
  }
  .console-agent-cli-btn {
    font-size: var(--fs-md);
    padding: 0.25rem 0.9rem;
  }
  .console-edit-banner {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.35rem 0.6rem;
    margin-bottom: 0.4rem;
    border-radius: 0.3rem;
    background: var(--algo-amber-bg);
    border: 1px solid var(--algo-amber-border-soft);
    color: var(--algo-amber);
    font-size: var(--fs-sm);
    font-weight: 600;
  }
  /* Shared bottom-right button row for Save/Create + Cancel edit
     (2026-10-13, operator request — both actions live together at the
     bottom of the editor, right-aligned, instead of Cancel sitting in
     the top banner and Save left-aligned below the editor). */
  .console-agent-cli-actions {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 0.75rem;
    margin-top: 0.3rem;
  }
  .console-edit-cancel {
    font-size: var(--fs-xs);
    font-weight: 600;
    text-decoration: underline;
    color: var(--algo-amber);
    background: none;
    border: none;
    cursor: pointer;
    padding: 0;
  }
</style>
