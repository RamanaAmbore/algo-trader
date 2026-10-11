<script>
  // ConsoleTab — Automation's Console tab (Sprint B, 2026-10, Phase 2
  // "Console retirement"). Ported body of the retired `/console` route
  // (frontend/src/routes/(algo)/console/+page.svelte, now a redirect to
  // `/automation?tab=console`) — same plain orders bar (CommandLineTab,
  // the legacy positional orders.js grammar) + agent-family CLI
  // (AgentCliEditor, agents.js grammar — FULL statement scope: order,
  // bracket, agent, event, token — no `allowedKinds` restriction, unlike
  // the two order-scoped mounts added this same phase on /orders and the
  // SymbolPanel order-ticket modal, see OrderCliTab.svelte).
  //
  // Deliberately mounts CommandLineTab (not just AgentCliEditor) — the
  // legacy orders.js grammar's own retirement is Sprint F, gated on a
  // parity checklist that hasn't passed yet (cancel/modify verbs,
  // agent-ai /interpret, /api/admin/exec passthrough, up-arrow recall —
  // see CLAUDE.md's Phase 2 Sprint F note). Dropping CommandLineTab here
  // would have unmounted its ONLY remaining callsite in the app and
  // silently executed that retirement early.
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
  import SymbolPanel from '$lib/SymbolPanel.svelte';
  import ConfirmModal from '$lib/ConfirmModal.svelte';
  import AgentCliEditor from '$lib/command/AgentCliEditor.svelte';
  import { fetchAgentCatalog, compileAgentCliStatement } from '$lib/command/grammars/agents.js';
  import {
    submitConsoleAgent, submitConsoleBracket, submitConsoleToken,
    updateConsoleAgent, updateConsoleBracket, updateConsoleToken,
    resolveConsoleOrder, submitResolvedConsoleOrder,
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

  /** Fired by CommandLineTab when the operator typed agent-family syntax
   *  into the orders-grammar bar and pressed Enter — hands the raw text
   *  to the agent panel for review/compile/submit instead of letting it
   *  fall through to the shell-exec fallback. */
  function _onAgentStatement(/** @type {string} */ raw) {
    value = raw;
  }

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
    if (!resolved.ok) { toast.error(_short(/** @type {any} */ (resolved).error)); return; }
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
      toast.success(r.kind === 'basket' ? 'Basket order placed' : 'Order placed');
      value = '';
    } else {
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
      const r = await updateConsoleToken(/** @type {number} */ (editTarget.ref), result.fragment);
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
    const r = await submitConsoleToken(result.fragment);
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

  // SymbolPanel-as-modal state — opened when CommandLineTab fires a
  // parsed order. Operator confirms / edits / submits from the modal.
  let _ticketProps = $state(/** @type {any} */ (null));
  function _onParsedOrder(/** @type {any} */ props) {
    _ticketProps = { ...props, defaultTab: 'ticket' };
  }
  function _closeTicket() { _ticketProps = null; }
</script>

{#if isDemo}
  <div class="mb-2 p-2 rounded bg-purple-500/10 border border-purple-500/30 text-[0.65rem] text-purple-200">
    <strong class="text-purple-100">Demo view.</strong>
    Browse the command grammar + autocomplete. Submit + write paths are
    disabled — see <a href="/showcase" class="underline hover:text-purple-50">the tour</a>
    for what this terminal does in production.
  </div>
{/if}

<div class="console-tab-guide">
  <a href="/automation/guide" title="CLI grammar guide">Guide: CLI grammar reference &rarr;</a>
</div>

<!-- Plain command-line entry — buy/sell/modify/cancel stay on this bar;
     WHEN/ALERT/DO and bare order(/place_order(/bracket( hand off to the
     agent-family panel below instead of falling through to this bar's
     own shell-exec fallback. -->
<div class="console-cmd-wrap">
  <CommandLineTab
    onParsedOrder={_onParsedOrder}
    onAgentStatement={_onAgentStatement}
    prefillSide=""
    prefillAccount=""
    prefillSymbol=""
    prefillQty={0}
    prefillPrice={0}
    prefillOrderType="LIMIT" />
</div>

<!-- Agent-family CLI — WHEN/ALERT/DO agents, bare order(...)/
     place_order(...), bracket(...), and token(...) — full statement
     scope, no allowedKinds restriction (this is the un-restricted
     Console mount; the order-only mounts live on /orders and the
     SymbolPanel modal via OrderCliTab). -->
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
    {/if}
  {/if}
</div>

{#if _ticketProps}
  <SymbolPanel
    symbol={_ticketProps.symbol || ''}
    exchange={_ticketProps.exchange || ''}
    defaultTab="ticket"
    side={_ticketProps.side || 'BUY'}
    qty={_ticketProps.qty || 0}
    price={_ticketProps.price || 0}
    orderType={_ticketProps.orderType || 'LIMIT'}
    account={_ticketProps.account || ''}
    accounts={[]}
    onSubmit={_closeTicket}
    onClose={_closeTicket}
  />
{/if}

<ConfirmModal bind:this={_liveConfirmRef} />

<style>
  .console-tab-guide {
    flex-shrink: 0;
    margin-bottom: 0.3rem;
    font-size: var(--fs-xs);
  }
  .console-tab-guide a { color: var(--algo-sky); text-decoration: underline; }
  .console-cmd-wrap {
    flex-shrink: 0;
    margin-bottom: 0.4rem;
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
