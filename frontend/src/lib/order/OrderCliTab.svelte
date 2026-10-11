<script>
  /**
   * OrderCliTab — order-scoped mount of the generic agent-CLI grammar
   * (agents.js / AgentCliEditor), restricted to `order(...)`/
   * `place_order(...)` statements only via `allowedKinds: ['order']`,
   * enforced by the compiler itself (agents.js:compileStatement) — never
   * a hidden button faking the restriction.
   *
   * Sprint B (2026-10, Phase 2 "Console retirement") — shared by BOTH
   * order-scoped CLI surfaces added this phase: the "CLI" tab on
   * `/orders` and the SymbolPanel order-ticket MODAL's own "CLI" tab.
   * Both reach this component through ONE mount point — SymbolPanel's
   * own `_activeTab === 'cli'` tab body (`tabs.js`'s restored `ORDER_TABS`
   * entry) — since `/orders`' Order Entry card is itself an inline
   * SymbolPanel mount, not a separate implementation.
   *
   * Submits through the EXACT SAME `resolveConsoleOrder` /
   * `submitResolvedConsoleOrder` path the (now-retired-as-a-route)
   * `/console` page's own bare-order submission already used — no new
   * order-placement code path, same live-order confirm step, same
   * `isDemo` gate shape (host-supplied, mirroring how SymbolPanel itself
   * already gates demo mode elsewhere).
   */
  import { toast } from '$lib/data/toastStore.svelte.js';
  import AgentCliEditor from '$lib/command/AgentCliEditor.svelte';
  import ConfirmModal from '$lib/ConfirmModal.svelte';
  import { fetchAgentCatalog, compileAgentCliStatement } from '$lib/command/grammars/agents.js';
  import { resolveConsoleOrder, submitResolvedConsoleOrder } from '$lib/command/consoleDispatch';

  /** @type {{
   *   mode?: 'paper'|'live',
   *   isDemo?: boolean,
   *   onSubmit?: (payload: any) => void,
   * }} */
  let { mode = 'paper', isDemo = false, onSubmit = undefined } = $props();

  const _ALLOWED = /** @type {string[]} */ (['order']);

  let _text = $state('');
  let _catalog = $state(/** @type {any} */ (null));
  let _catalogError = $state('');

  $effect(() => {
    if (isDemo) return; // demo has no write-path grammar catalog endpoint access either
    if (_catalog || _catalogError) return;
    fetchAgentCatalog()
      .then((cat) => { _catalog = cat; _catalogError = ''; })
      .catch(() => { _catalogError = 'CLI catalog unavailable'; });
  });

  /** Recompiles `_text` synchronously at submit time — never trusts a
   *  possibly-stale debounced preview (same rule the retired /console
   *  page's own `_recompileAgentCli` followed). */
  function _recompile() {
    if (!_catalog) {
      return { ok: false, errors: [{ message: 'CLI catalog unavailable' }], kind: null, ticket: null, basket: null };
    }
    try {
      return compileAgentCliStatement(_text, _catalog, { mode, allowedKinds: _ALLOWED });
    } catch (e) {
      return { ok: false, errors: [{ message: e?.message || 'compile failed' }], kind: null, ticket: null, basket: null };
    }
  }

  let _confirmRef = $state(/** @type {{ ask: (opts: any) => Promise<boolean> } | null} */ (null));

  function _short(/** @type {any} */ msg) { return String(msg || '').slice(0, 32); }

  // Sprint C (2026-10, Phase 2 "Automation redesign") — post-SUBMIT
  // result, rendered via AgentCliEditor's `submitStatus` prop alongside
  // this mount's own toast (same persistent-status-line rule as
  // ConsoleTab's identical field — a toast alone disappears too fast to
  // serve as a record of "did this actually place").
  let _submitStatus = $state(/** @type {{ok: boolean, text: string} | null} */ (null));

  async function _onOrderSubmit(/** @type {any} */ _result) {
    const fresh = _recompile();
    if (!fresh.ok || fresh.kind !== 'order') { toast.error('CLI changed — recheck before submit'); return; }
    const resolved = await resolveConsoleOrder(fresh);
    if (!resolved.ok) {
      const _err = /** @type {any} */ (resolved).error;
      _submitStatus = { ok: false, text: `Order failed: ${_err}` };
      toast.error(_short(_err));
      return;
    }
    const isLive = fresh.ticket?.mode === 'live' || !!fresh.basket;
    if (isLive) {
      const ok = await _confirmRef?.ask({
        title: 'Place LIVE order?',
        message: 'This CLI statement will place a real order against the broker.',
        danger: true,
        confirmLabel: 'Place LIVE order',
        cancelLabel: 'Cancel',
      });
      if (!ok) return;
    }
    const r = await submitResolvedConsoleOrder(resolved, { mode, source: 'command' });
    if (r.ok) {
      _submitStatus = { ok: true, text: r.kind === 'basket' ? 'Basket order placed' : 'Order placed' };
      toast.success(r.kind === 'basket' ? 'Basket order placed' : 'Order placed');
      _text = '';
      onSubmit?.({ mode, kind: r.kind, response: r.response });
    } else {
      _submitStatus = { ok: false, text: `Order failed: ${/** @type {any} */ (r).error}` };
      toast.error(`Order failed: ${_short(/** @type {any} */ (r).error)}`);
    }
  }
</script>

<div class="order-cli-tab">
  <div class="order-cli-scope-label">
    CLI — orders only. Author agents/brackets on <a href="/automation?tab=console">Automation → Console</a>.
  </div>
  {#if isDemo}
    <div class="order-cli-demo">Demo view — CLI order placement disabled.</div>
  {:else if _catalogError}
    <div class="order-cli-err">{_catalogError}</div>
  {:else}
    <AgentCliEditor
      bind:value={_text}
      catalog={_catalog}
      {mode}
      allowedKinds={_ALLOWED}
      placeholder={'order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1)'}
      onOrderSubmit={_onOrderSubmit}
      submitStatus={_submitStatus}
    />
  {/if}
</div>

<ConfirmModal bind:this={_confirmRef} />

<style>
  .order-cli-tab { display: flex; flex-direction: column; gap: 0.4rem; }
  .order-cli-scope-label {
    font-size: var(--fs-xs);
    color: var(--algo-muted);
  }
  .order-cli-scope-label a { color: var(--algo-sky); text-decoration: underline; }
  .order-cli-demo,
  .order-cli-err {
    padding: 0.4rem 0.6rem;
    border-radius: 0.3rem;
    background: var(--algo-red-bg-mid);
    border: 1px solid var(--algo-red-border-soft);
    color: var(--algo-red-text-bright);
    font-size: var(--fs-sm);
  }
</style>
