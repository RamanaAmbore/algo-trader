<script>
  // Console — plain command-line terminal + the canonical 5-tab activity
  // log. The page's primary input is CommandLineTab (typed grammar
  // commands); the inline LogPanel below carries the same 5-tab surface
  // the page-header Log icon opens in a modal — operator can use either.
  //
  // A parsed command opens SymbolPanel as a modal (defaultTab='ticket')
  // pre-filled with the parsed values, so the operator can review +
  // Submit. Keeps the safety net while making plain-text the primary UI.

  import { get as getStore } from 'svelte/store';
  import { authStore, executionMode } from '$lib/stores';
  import { toast } from '$lib/data/toastStore.svelte.js';
  import { safeRead } from '$lib/utils/safeRead.js';
  import AlgoTimestamp from '$lib/AlgoTimestamp.svelte';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import ActivityLogSurface from '$lib/ActivityLogSurface.svelte';
  import ConfirmModal from '$lib/ConfirmModal.svelte';
  import CommandLineTab from '$lib/order/CommandLineTab.svelte';
  import SymbolPanel from '$lib/SymbolPanel.svelte';
  import AgentCliEditor from '$lib/command/AgentCliEditor.svelte';
  import { fetchAgentCatalog, compileAgentCliStatement } from '$lib/command/grammars/agents.js';
  import {
    submitConsoleAgent, submitConsoleBracket, submitConsoleToken,
    resolveConsoleOrder, submitResolvedConsoleOrder,
  } from '$lib/command/consoleDispatch';

  const isDemo = $derived(!$authStore.user);

  // ── Agent-family CLI panel (Sprint 9) — WHEN/ALERT/DO, bare order(...)/
  // place_order(...)/bracket(...). Reuses AgentCliEditor's existing
  // textarea/suggestion-row/error-list/preview shell unchanged. Fed
  // either directly by the operator typing here, OR by CommandLineTab's
  // dispatch hand-off (`_onAgentStatement` below) when the operator types
  // agent-family syntax into the orders-grammar bar instead. -----------
  let _agentCliText = $state('');
  let _agentCliCatalog = $state(/** @type {any} */ (null));
  let _agentCliCatalogError = $state('');

  $effect(() => {
    if (isDemo) return; // /admin/grammar/tokens requires auth
    fetchAgentCatalog()
      .then((cat) => { _agentCliCatalog = cat; _agentCliCatalogError = ''; })
      .catch(() => { _agentCliCatalogError = 'CLI catalog unavailable'; });
  });

  /** Fired by CommandLineTab when the operator typed agent-family syntax
   *  into the orders-grammar bar and pressed Enter — hands the raw text
   *  to the agent panel for review/compile/submit instead of letting it
   *  fall through to the shell-exec fallback. */
  function _onAgentStatement(/** @type {string} */ raw) {
    _agentCliText = raw;
  }

  // `safeRead` (not a plain `get()`) — this is called from inside
  // `$derived.by(_recompileAgentCli)` below, where a raw `get(store)`
  // risks a `state_unsafe_mutation` warning (project-wide reactive-safety
  // rule — see CLAUDE.md). The template's own `mode={_execModeForCompile()}`
  // binding on AgentCliEditor is likewise an implicitly-tracked context,
  // so this function always uses the safe form regardless of caller.
  function _execModeForCompile() {
    return safeRead(executionMode) === 'live' ? 'live' : 'paper';
  }

  /** Recompiles `_agentCliText` synchronously at submit time — never
   *  trusts a possibly-stale cached result (AgentCliEditor's own debounced
   *  preview may be up to 150ms behind, or mid-invalid). Mirrors
   *  /automation's `saveEdit` recompile pattern. */
  function _recompileAgentCli() {
    if (!_agentCliCatalog) {
      return { ok: false, errors: [{ message: 'CLI catalog unavailable' }], kind: null, agent: null, ticket: null, basket: null, template: null, fragment: null };
    }
    try {
      return compileAgentCliStatement(_agentCliText, _agentCliCatalog, { mode: _execModeForCompile() });
    } catch (e) {
      return { ok: false, errors: [{ message: e?.message || 'compile failed' }], kind: null, agent: null, ticket: null, basket: null, template: null, fragment: null };
    }
  }

  // Live preview of the current compile — drives which submit button
  // shows below AgentCliEditor's own shell (it only renders a submit
  // button for kind:'order'; agent/bracket need their own here).
  const _agentCliCompiled = $derived.by(_recompileAgentCli);

  let _liveConfirmRef = $state(/** @type {{ ask: (opts: any) => Promise<boolean> } | null} */ (null));

  function _short(msg) { return String(msg || '').slice(0, 32); }

  async function _submitAgentCliOrder(/** @type {any} */ result) {
    // Resolve any bracket="slug" reference BEFORE the live-confirm dialog,
    // so a no-match / fetch-failure error surfaces without ever popping it.
    const resolved = await resolveConsoleOrder(result);
    // svelte2tsx doesn't carry control-flow narrowing through an awaited
    // discriminated-union return across this if-guard — `any` cast is the
    // pragmatic escape (same pattern used elsewhere in this codebase for
    // this exact TS-in-Svelte limitation); runtime behavior is unaffected.
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
      _agentCliText = '';
    } else {
      toast.error(`Order failed: ${_short(/** @type {any} */ (r).error)}`);
    }
  }

  // Handles BOTH kind:'agent' (threshold) and kind:'event' (Sprint 12
  // notification agent) compile results — `submitConsoleAgent` itself
  // already branches on `result.agent.kind === 'event'` (stamped by
  // `compileEventStmt`) vs the ordinary threshold shape, so no separate
  // event-specific submit function is needed here (Sprint 14 fix: the
  // button that calls this was previously only wired for kind:'agent').
  async function _submitAgentCliAgent(/** @type {any} */ result) {
    const r = await submitConsoleAgent(result.agent, _agentCliText);
    if (r.ok) { toast.success(`Agent created: ${r.slug}`); _agentCliText = ''; }
    else toast.error(`Agent failed: ${_short(r.error)}`);
  }

  async function _submitAgentCliBracket(/** @type {any} */ result) {
    const r = await submitConsoleBracket(result.template);
    if (r.ok) { toast.success(`Bracket saved: ${result.template?.name || ''}`); _agentCliText = ''; }
    else toast.error(`Bracket failed: ${_short(r.error)}`);
  }

  // Sprint 14 — `token(kind=notify) ...` had no submit path from this page
  // at all (no button, no dispatch function) despite compiling cleanly.
  async function _submitAgentCliToken(/** @type {any} */ result) {
    const r = await submitConsoleToken(result.fragment);
    if (r.ok) { toast.success(`Token created: ${result.fragment?.name || ''}`); _agentCliText = ''; }
    else toast.error(`Token failed: ${_short(r.error)}`);
  }

  /** `kind:'order'` has its own submit button rendered BY AgentCliEditor —
   *  routed here via its `onOrderSubmit` prop. Agent/bracket kinds have no
   *  equivalent button inside that shared component, so this page renders
   *  its own, driven by `_agentCliCompiled`. Recompiles fresh rather than
   *  trusting the (possibly 150ms-stale) `result` AgentCliEditor passes
   *  in — same "never trust the debounced preview at submit time" rule
   *  `_recompileAgentCli` exists for. */
  async function _onAgentCliOrderSubmit(/** @type {any} */ _result) {
    const fresh = _recompileAgentCli();
    if (!fresh.ok || fresh.kind !== 'order') { toast.error('CLI changed — recheck before submit'); return; }
    await _submitAgentCliOrder(fresh);
  }

  let logTab = $state('terminal');
  // Manual-refresh bump for the inline LogPanel — clicking the page-
  // header Refresh icon rotates the badge + asks LogPanel to re-poll
  // immediately rather than waiting for its next tick.
  let _refreshKey = $state(0);
  let _refreshing = $state(false);
  function _refresh() {
    _refreshing = true;
    _refreshKey++;
    setTimeout(() => { _refreshing = false; }, 400);
  }

  // SymbolPanel-as-modal state — opened when CommandLineTab fires a
  // parsed order. Operator confirms / edits / submits from the modal.
  let _ticketProps = $state(/** @type {any} */ (null));
  function _onParsedOrder(/** @type {any} */ props) {
    _ticketProps = { ...props, defaultTab: 'ticket' };
  }
  function _closeTicket() { _ticketProps = null; }
</script>

<svelte:head><title>Console | RamboQuant Analytics</title></svelte:head>

<div class="flex flex-col h-[calc(100vh-11rem)]">
  <div class="page-header">
    <span class="algo-title-group">
      <h1 class="page-title-chip">Console</h1>
    </span>
    <AlgoTimestamp />
    <span class="ml-auto"></span>
    <span class="page-header-actions">
      <a href="/console/guide" class="console-guide-link" title="CLI grammar guide" aria-label="Open CLI guide">
        <!-- Open-book glyph — "documentation" reads clearer here than a
             generic "?" circle, which this app's icon set doesn't use
             anywhere else. -->
        <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true">
          <path d="M2 3.2c1.6-.6 3.4-.6 5 0v9.6c-1.6-.6-3.4-.6-5 0V3.2Z"
                stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/>
          <path d="M14 3.2c-1.6-.6-3.4-.6-5 0v9.6c1.6-.6 3.4-.6 5 0V3.2Z"
                stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/>
        </svg>
        <span>Guide</span>
      </a>
      <RefreshButton onClick={_refresh} loading={_refreshing} label="console" />
      <PageHeaderActions />
    </span>
  </div>

  {#if isDemo}
    <div class="mb-2 p-2 rounded bg-purple-500/10 border border-purple-500/30 text-[0.65rem] text-purple-200">
      <strong class="text-purple-100">Demo view.</strong>
      Browse the command grammar + autocomplete. Submit + write paths are
      disabled — see <a href="/showcase" class="underline hover:text-purple-50">the tour</a>
      for what this terminal does in production.
    </div>
  {/if}

  <!-- Plain command-line entry — was the 3-tab SymbolPanel shell; reduced
       to just the grammar-driven command bar. Parsed commands fan out
       to the SymbolPanel ticket modal for confirmation. buy/sell/modify/
       cancel stay on this bar; WHEN/ALERT/DO and bare order(/place_order(/
       bracket( hand off to the agent-family panel below instead of
       falling through to this bar's own shell-exec fallback. -->
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
       place_order(...), and bracket(...) saved-template presets. Always
       open alongside the orders bar above (no toggle), per the Sprint 9
       "single always-open CLI surface" brief — dispatch decides which
       grammar a given statement belongs to; this panel owns the
       agents.js/Sprint 1-8 call-style half. -->
  <div class="console-agent-cli-wrap">
    {#if _agentCliCatalogError}
      <div class="console-agent-cli-err">{_agentCliCatalogError}</div>
    {:else}
      <AgentCliEditor
        bind:value={_agentCliText}
        catalog={_agentCliCatalog}
        mode={_execModeForCompile()}
        onOrderSubmit={_onAgentCliOrderSubmit}
      />
      {#if !isDemo && _agentCliCompiled?.ok && (_agentCliCompiled.kind === 'agent' || _agentCliCompiled.kind === 'event')}
        <button type="button" class="btn-primary console-agent-cli-btn"
          onclick={() => _submitAgentCliAgent(_agentCliCompiled)}>
          Create agent
        </button>
      {:else if !isDemo && _agentCliCompiled?.ok && _agentCliCompiled.kind === 'bracket'}
        <button type="button" class="btn-primary console-agent-cli-btn"
          onclick={() => _submitAgentCliBracket(_agentCliCompiled)}>
          Save bracket
        </button>
      {:else if !isDemo && _agentCliCompiled?.ok && _agentCliCompiled.kind === 'token'}
        <button type="button" class="btn-primary console-agent-cli-btn"
          onclick={() => _submitAgentCliToken(_agentCliCompiled)}>
          Create token
        </button>
      {/if}
    {/if}
  </div>

  <!-- Activity log — same 5 tabs as the modal you'd get from the Log
       icon on every other page (Order Book · Agent Log · Terminal ·
       System · News). Terminal is the default since this page is the
       Terminal. -->
  <div class="flex flex-col flex-1 min-h-0 mt-2">
    <!-- Tab list inherited from LogPanel's default — keeps every
         surface (Activity modal, Order modal bottom panel, this
         /console mount, /automation) in sync without duplicating the
         array per callsite. -->
    <ActivityLogSurface
      context="page"
      heightClass="flex-1 min-h-0"
      defaultTab={logTab}
      label="Log"
      hideInlineAccountFilter={false}
      onTabChange={(id) => { logTab = id; }}
    />
  </div>
</div>

{#if _ticketProps}
  <!-- Order modal carries its own Order Book + Agent Log bottom tabs as
       a self-contained surface — modal is its own world, distinct from
       the page-level LogPanel below the command bar. -->
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

<!-- Live-order confirm for the agent-family panel's order/basket path —
     same shared component + wording /automation's own CLI tab uses. -->
<ConfirmModal bind:this={_liveConfirmRef} />

<style>
  /* Guide link — matches PageHeaderActions' .pha-btn sizing/chrome so it
     sits flush in the same icon row, using the sky accent (info/help)
     to stay visually distinct from the amber Order / cyan Chart /
     orange Activity trio it sits next to. */
  .console-guide-link {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    height: 1.4rem;
    padding: 0 0.4rem;
    border-radius: 4px;
    border: 1px solid var(--algo-sky-border-soft);
    color: var(--algo-sky);
    font-size: var(--fs-sm);
    text-decoration: none;
    background: rgba(255, 255, 255, 0.03);
    transition: background 0.12s, border-color 0.12s, color 0.12s;
    flex-shrink: 0;
  }
  .console-guide-link:hover {
    background: var(--algo-sky-bg);
    border-color: var(--algo-sky-border);
    color: var(--algo-sky-text);
  }

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
</style>
