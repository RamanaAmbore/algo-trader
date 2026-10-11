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
  import CardHeader from '$lib/CardHeader.svelte';
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

  // Sprint I Phase 1 follow-up (operator-requested kind selector) — a
  // small, secondary visual hint row above the textbox mirroring the
  // Phase 1 dispatch rule's own leading-token buckets (order(/bracket(/
  // WHEN-or-GIVEN/bare ALERT — `token(` deliberately omitted, it has no
  // slot in the operator's 4-option ask and stays fully typeable without
  // a shortcut). Interaction model (operator left this to our judgment):
  // NOT a hard mode-lock — the real dispatch decision always comes from
  // `_classify`/`compileAgentCliStatement` on the typed text, never from
  // this selector. It (a) reflects whatever kind the typed text already,
  // confidently resolved to (synced below), and (b) when the box is
  // EMPTY, clicking an option seeds a short starter snippet for that
  // kind — a convenience, never a destructive overwrite of real typed
  // text.
  const _KIND_OPTIONS = ['order', 'bracket', 'agent', 'alert'];
  const _KIND_LABELS = { order: 'Order', bracket: 'Bracket', agent: 'Agent', alert: 'Alert' };
  const _KIND_SEED = {
    order: 'order(account="", symbol="", side="BUY", lots=1)',
    bracket: 'bracket(applies_to=buy_any, tp_pct=30, sl_pct=-20) #name=""',
    agent: 'WHEN pnl_pct@positions.total <= -5 ALERT telegram DO nop',
    alert: 'ALERT telegram',
  };
  let _kindHint = $state('order');

  function _pickKind(/** @type {string} */ k) {
    _kindHint = k;
    if (!value || !value.trim()) value = _KIND_SEED[k] || '';
  }

  /** Leading-TOKEN detector for the kind-selector sync below — deliberately
   *  reads the raw text's own first word, NOT `_agentCliCompiled.kind`.
   *  Those two disagree for the "Alert" bucket: a bare `ALERT telegram`
   *  statement (no WHEN, no event-meta leading kwarg) compiles to
   *  `kind:'agent'` (compileStatement's own dispatch — see agents.js),
   *  the SAME compiled kind an ordinary `WHEN...ALERT...DO...` statement
   *  gets, even though textually it's the "Alert" bucket the operator
   *  picked. The selector's job is to reflect what the operator TYPED,
   *  not which JSON shape it happens to compile to — same split the
   *  Phase 1 dispatch rule itself draws (first-token peek, not compiled
   *  result). Mirrors (but does not reuse) `classifyConsoleStatement`'s
   *  own regexes — scoped to UI-hint purposes only; the real dispatch
   *  decision is still `_classify`. */
  function _detectKindHint(/** @type {string} */ text) {
    const t = String(text || '').trimStart();
    if (/^(order|place_order)\s*\(/i.test(t)) return 'order';
    if (/^bracket\s*\(/i.test(t)) return 'bracket';
    if (/^(when|given|do)\b/i.test(t)) return 'agent';
    if (/^alert\b/i.test(t)) return 'alert';
    return null;
  }

  // Keeps the selector in sync with whatever the typed text's leading
  // token actually is. Intentionally does nothing when the text doesn't
  // start with one of these five keywords (mid-typing, a bare
  // `token(...)`, or legacy/empty text) — the selector simply keeps
  // showing whatever the operator last picked.
  $effect(() => {
    const k = _detectKindHint(value);
    if (k) _kindHint = k;
  });

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
  let _fsConsole = $state(false);

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

<section class="console-card" class:fs-card-on={_fsConsole}>
  <CardHeader
    title="Console"
    label="Console"
    bind:isFullscreen={_fsConsole}
    showSearch={false}
    showCollapse={false}
    detectOverflow={false}
  />
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

    <!-- "CLI" label + Guide button, same row, directly above the one
         textbox (operator-requested relocation, 2026-10-11). The tab's
         OWN nav-strip label stays "Console" (AutomationTabs.svelte) —
         this is a separate, body-level heading. -->
    <div class="console-tab-guide console-cli-label-row">
      <span class="console-cli-label">CLI</span>
      <a href="/automation/guide" title="CLI grammar guide" class="btn-secondary console-guide-btn">Guide: CLI grammar reference</a>
    </div>

    <!-- Statement-kind hint row (operator-requested, 2026-10-11) — see
         the `_kindHint`/`_pickKind` doc above: a secondary, non-blocking
         affordance, not a parsing mode-lock. -->
    <div class="console-kind-row" role="group" aria-label="Statement kind">
      {#each _KIND_OPTIONS as k (k)}
        <button type="button" class="console-kind-pill" class:is-active={_kindHint === k}
          onclick={() => _pickKind(k)}>{_KIND_LABELS[k]}</button>
      {/each}
    </div>

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
             the operator always sees edit-mode is active and has an escape
             hatch (Cancel edit) regardless of catalog fetch timing. -->
        <div class="console-edit-banner">
          <span>Editing {_ROW_KIND_LABEL[editTarget.rowKind] || editTarget.rowKind} "{editTarget.label}"</span>
          <button type="button" class="console-edit-cancel" onclick={cancelEdit}>Cancel edit</button>
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
      {/if}
    </div>
  </div>
</section>

<ConfirmModal bind:this={_liveConfirmRef} />

<style>
  /* Sprint C follow-up (2026-10) — Console tab's own card chrome. Wraps
     the CLI entry + result area so the standard CardControls group
     (via CardHeader) applies, matching every other operator-action
     card in the app. Only Fullscreen is wired (showSearch/showCollapse
     false) — there is no grid to search, and collapsing a tab's only
     content is a no-op the operator would just re-expand; Download
     doesn't apply either (no tabular export), left unset so
     GridDownloadButton hides itself. */
  .console-card {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
  }
  .console-card-body {
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  .console-card.fs-card-on .console-card-body {
    flex: 1 1 auto;
    overflow-y: auto;
  }
  .console-tab-guide {
    flex-shrink: 0;
    margin-bottom: 0.3rem;
    font-size: var(--fs-xs);
  }
  /* Guide link — styled as a small pill button (operator request,
     2026-10) rather than a bare underlined text link. `.btn-secondary`
     supplies the shared navy/amber-hover chrome ((algo)/+layout.svelte's
     `.algo-content .btn-secondary` override); this scoped class only
     fixes up anchor-specific box rendering (inline-flex + no implicit
     underline) — same reuse pattern as OrderCliTab's matching link. */
  .console-guide-btn {
    display: inline-flex;
    align-items: center;
    white-space: nowrap;
    text-decoration: none;
  }
  /* "CLI" label + Guide button row (operator-requested relocation,
     2026-10-11) — replaces the old standalone `.console-tab-guide` block
     (class name KEPT for an existing Playwright selector,
     console_guide_page.spec.js's `.console-tab-guide a`); the row's own
     flex layout is new. */
  .console-cli-label-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
  }
  .console-cli-label {
    font-size: var(--fs-sm);
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--algo-muted);
  }
  /* Statement-kind hint pills (operator-requested, 2026-10-11) — see the
     `_kindHint`/`_pickKind` doc in <script>. */
  .console-kind-row {
    display: flex;
    gap: 0.3rem;
    margin: 0.3rem 0 0.4rem;
    flex-wrap: wrap;
  }
  .console-kind-pill {
    font-size: var(--fs-xs);
    font-weight: 600;
    padding: 0.15rem 0.6rem;
    border-radius: 999px;
    border: 1px solid rgba(126, 151, 184, 0.30);
    background: rgba(126, 151, 184, 0.08);
    color: var(--algo-muted);
    cursor: pointer;
  }
  .console-kind-pill:hover { border-color: rgba(126, 151, 184, 0.55); }
  .console-kind-pill.is-active {
    background: var(--algo-amber-bg);
    border-color: var(--algo-amber-border-soft);
    color: var(--algo-amber);
  }
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
    margin-top: 0.3rem;
    font-size: var(--fs-md);
    padding: 0.25rem 0.9rem;
  }
  .console-edit-banner {
    display: flex;
    align-items: center;
    justify-content: space-between;
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
