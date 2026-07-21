<script>
  /**
   * BrokerHealthBadge — modal-only broker auth/freshness panel.
   *
   * Operator consolidation: the navbar's 5/5 broker-chip is the single
   * entry point; clicking it sets `open=true` and this component renders
   * the per-account auth modal. The standalone AUTH badge button was
   * removed because two chips for adjacent concepts felt redundant.
   *
   * State semantics (per account):
   *   red    — last fetch returned an auth failure
   *   amber  — last good > 5 min ago (stale) but no active failure
   *   green  — healthy + fresh
   *
   * Data source: shared `brokerHealthStore` in stores.js, polled every
   * 30 s by startBrokerHealthPoller() (started from the layout onMount).
   * This component is modal-only — no local polling needed.
   */
  import { onDestroy } from 'svelte';
  import { brokerHealthStore, openActivityModal } from '$lib/stores';
  import { accountDisplayOrder, sortAccountsBy } from '$lib/data/accountSort.js';

  /** Bindable: parent (algo layout) toggles this from the 5/5 chip. */
  let { open = $bindable(false) } = $props();

  // Consume the shared broker-health store (populated by startBrokerHealthPoller
  // in the layout). No local fetch needed — the store already polls at 30 s
  // continuously so the popup shows current data immediately on open.
  let _rawAccounts = $state(/** @type {Array<{account:string,broker:string,state:string,reason:string,last_good_at:string|null,last_check_at:string|null,is_active_ticker?:boolean,circuit_state?:string,consecutive_fail_count?:number,circuit_open_until?:string|null,circuit_breaker_enabled?:boolean,poll_priority?:string,auto_downgrade_enabled?:boolean,auto_downgraded_at?:string|null,auto_downgrade_reason?:string|null}>} */ ([]));
  const _unsubHealth = brokerHealthStore.subscribe(v => { _rawAccounts = v.accounts; });
  onDestroy(() => _unsubHealth());

  // Subscribe to the canonical order map so the chip popup re-sorts
  // immediately when the operator patches display_order in /admin/brokers.
  let _orderMap = $state(/** @type {Record<string,number>} */ ({}));
  const _unsubOrder = accountDisplayOrder.subscribe(m => { _orderMap = m; });
  onDestroy(() => _unsubOrder());

  // Client-side sort mirrors the backend sort so even a stale store
  // response renders in the right order.
  const accounts = $derived(
    sortAccountsBy(_rawAccounts.map(a => a.account), _orderMap)
      .map(id => _rawAccounts.find(a => a.account === id))
      .filter(Boolean)
  );

  function _fmtIso(iso) {
    if (!iso) return '—';
    try {
      const d = new Date(iso);
      // Show IST timestamp: HH:MM IST
      return d.toLocaleTimeString('en-IN', {
        timeZone: 'Asia/Kolkata',
        hour: '2-digit', minute: '2-digit', hour12: false,
      }) + ' IST';
    } catch (_) {
      return iso;
    }
  }
</script>

<svelte:window onkeydown={open ? (e) => { if (e.key === 'Escape') { e.preventDefault(); open = false; } } : null} />

{#if open}
  <!-- Modal overlay -->
  <div class="bh-overlay" role="presentation" onclick={() => open = false}></div>
  <div class="bh-modal algo-modal" role="dialog" aria-label="Broker auth health">
    <div class="bh-modal-header canonical-modal-header">
      <span class="bh-modal-title">Broker Auth Health</span>
      <button class="bh-close" onclick={() => open = false} aria-label="Close">×</button>
    </div>
    <div class="bh-modal-body">
      <div class="bh-grid">
        {#if accounts.length > 0}
          <div class="bh-headrow" aria-hidden="true">
            <span></span>
            <span>Account</span>
            <span>Broker</span>
            <span>Status</span>
            <span>Reason</span>
            <span>Last Good</span>
          </div>
        {/if}
      {#each accounts as acct (acct.account)}
        {@const _accCls = acct.state === 'red'      ? 'bh-row-account-red'
                        : acct.state === 'amber'    ? 'bh-row-account-amber'
                        : acct.state === 'inactive' ? 'bh-row-account-inactive'
                        : acct.is_active_ticker     ? 'bh-row-account-active'
                        : 'bh-row-account-spare'}
        {@const _cbOptIn = !!acct.circuit_breaker_enabled}
        {@const _circuitTitle = (_cbOptIn && acct.circuit_state === 'open')
            ? `${acct.account} — circuit open until ${_fmtIso(acct.circuit_open_until)} — auto retry then`
            : null}
        {@const _redTitle = acct.state === 'red'
            ? (_cbOptIn
                ? `${acct.account} — connection problem (${acct.reason})`
                : `${acct.account} — connection problem (${acct.reason}) — retrying every poll`)
            : null}
        <div class="bh-row" role="button" tabindex="0"
             onclick={() => { open = false; openActivityModal('conn'); }}
             onkeydown={(e) => {
               if (e.key === 'Enter' || e.key === ' ') {
                 e.preventDefault(); open = false; openActivityModal('conn');
               }
             }}
             title="View connection log for {acct.account}">
          <span class="bh-row-dot bh-row-dot-{acct.state}" aria-hidden="true"></span>
          <span class="bh-row-account {_accCls}"
                title={_circuitTitle
                     ? _circuitTitle
                     : _redTitle
                     ? _redTitle
                     : acct.state === 'amber' ? `${acct.account} — stale (${acct.reason})`
                     : acct.is_active_ticker  ? `${acct.account} — active (running the KiteTicker WebSocket)`
                     : `${acct.account} — warm spare (healthy, not currently active)`}>
            {acct.account}
            {#if _cbOptIn && acct.circuit_state === 'open'}
              <span class="bh-circuit-chip" title="Circuit breaker open — SDK calls paused">OPEN</span>
            {:else if _cbOptIn && acct.circuit_state === 'half-open'}
              <span class="bh-circuit-chip bh-circuit-half" title="Circuit half-open — probing on next fetch">PROBE</span>
            {/if}
          </span>
          <span class="bh-row-broker">{acct.broker || 'kite'}</span>
          <span class="bh-row-state bh-row-state-{acct.state}">{acct.state.toUpperCase()}</span>
          <span class="bh-row-reason">{acct.reason}</span>
          <span class="bh-row-ts" title="Last good check">
            {_fmtIso(acct.last_good_at)}
          </span>
        </div>
      {/each}
      {#if accounts.length === 0}
        <p class="bh-empty">No fetch health data recorded yet.</p>
      {/if}
      </div>
    </div>
    <div class="bh-modal-footer">
      <span class="bh-footer-note">Polls every 30 s · Auth state from broker API calls</span>
    </div>
  </div>
{/if}

<style>
  /* ── Badge pill (mirrors .broker-chip palette) ── */
  .bh-badge {
    display: inline-flex;
    align-items: center;
    gap: 0.28rem;
    height: 1.4rem;
    padding: 0 0.5rem;
    border-radius: 9999px;
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    font-weight: 700;
    letter-spacing: 0.07em;
    cursor: pointer;
    white-space: nowrap;
    outline: none;
    margin-right: 0.3rem;
    transition: filter 0.08s;
    border: 1px solid transparent;
  }
  .bh-badge:hover { filter: brightness(1.15); }

  .bh-badge-green {
    color: var(--c-long);
    background: var(--c-long-10);
    border-color: rgba(74, 222, 128, 0.45);
  }
  .bh-badge-amber {
    color: var(--c-action);
    background: rgba(251, 191, 36, 0.10);
    border-color: rgba(251, 191, 36, 0.45);
  }
  .bh-badge-red {
    color: var(--c-short);
    background: var(--c-short-10);
    border-color: rgba(248, 113, 113, 0.45);
    animation: bh-pulse 2s ease-in-out infinite;
  }

  @keyframes bh-pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.65; }
  }

  .bh-dot {
    width: 0.38rem;
    height: 0.38rem;
    border-radius: 50%;
    background: currentColor;
  }
  .bh-label { font-size: var(--fs-xs); }

  /* ── Modal overlay ── */
  .bh-overlay {
    position: fixed;
    inset: 0;
    z-index: 9990;
    background: transparent;
  }
  .bh-modal {
    /* Composes .algo-modal chrome (gradient + amber halo + shadow +
       flex column + overflow hidden). Overrides:
       - positioning: fixed top-right (not centered) — this is a
         dropdown-style utility panel anchored to the navbar chip.
       - border-radius: 0.6rem (slightly softer than canonical 6px)
         — preserves the pill-drop feel.
       - dimensions: constrained to 680×480 for a compact status panel. */
    position: fixed;
    top: 3.2rem;
    right: 0.5rem;
    z-index: 9991;
    border-radius: 0.6rem;
    width: min(96vw, 680px);
    max-height: min(90vh, 480px);
  }

  /* ── Modal header — canonical-modal-header gradient applied via class.
     Local overrides: justify-content (title + close side-by-side) and
     title color (amber action token for broker auth context). ── */
  .bh-modal-header {
    justify-content: space-between;
  }
  .bh-modal-title {
    font-family: var(--font-numeric);
    font-size: var(--fs-lg);
    font-weight: 800;
    letter-spacing: 0.10em;
    color: #67e8f9;
    text-transform: uppercase;
  }
  .bh-close {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.4rem;
    height: 1.4rem;
    border: 1px solid rgba(248, 113, 113, 0.35);
    border-radius: 3px;
    color: var(--c-short);
    font-size: var(--fs-xl);
    line-height: 1;
    cursor: pointer;
    outline: none;
    background: transparent;
    transition: background 0.1s;
    flex-shrink: 0;
  }
  .bh-close:hover { background: rgba(248, 113, 113, 0.15); }

  /* ── Modal body ── */
  .bh-modal-body {
    flex: 1;
    overflow-y: auto;
    padding: 0.5rem 0.75rem;
  }

  /* ── Grid wrapper — gives the account rows a bordered container ── */
  .bh-grid {
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 4px;
    overflow: hidden;
  }

  /* ── Grid header row (Level-2: column labels, muted) ── */
  .bh-headrow {
    display: grid;
    grid-template-columns: 0.6rem 5.5rem 3.5rem 3.5rem 1fr 5rem;
    align-items: center;
    gap: 0.5rem;
    padding: 0.3rem 1rem;
    background: rgba(15, 23, 42, 0.30);
    border-bottom: 1px solid rgba(251, 191, 36, 0.20);
    font-family: var(--font-numeric);
    font-size: 0.6rem;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--text-muted);
  }
  .bh-row {
    display: grid;
    grid-template-columns: 0.6rem 5.5rem 3.5rem 3.5rem 1fr 5rem;
    align-items: center;
    gap: 0.5rem;
    padding: 0.45rem 1rem;
    border-bottom: 1px solid rgba(126, 151, 184, 0.10);
    font-size: var(--fs-md);
    font-family: var(--font-numeric);
    cursor: pointer;
    transition: background-color 0.1s;
  }
  .bh-row:last-child { border-bottom: none; }
  /* Alternating row background — matches .byund-row:nth-of-type(odd) */
  .bh-row:nth-child(odd) { background-color: var(--row-tint-odd-bg); }
  .bh-row:hover,
  .bh-row:focus-visible {
    background-color: rgba(34, 211, 238, 0.05) !important;
    outline: none;
  }

  .bh-row-dot {
    width: 0.45rem;
    height: 0.45rem;
    border-radius: 50%;
  }
  .bh-row-dot-green    { background: var(--c-long); }
  .bh-row-dot-amber    { background: var(--c-action); }
  .bh-row-dot-red      { background: var(--c-short); }
  .bh-row-dot-inactive { background: var(--text-faint); }

  .bh-row-account {
    color: #c8d8f0;
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
  }
  /* Account name is COLOR-CODED by state so the operator sees at a glance:
     - red   → has a connection problem (state=red trumps everything)
     - amber → stale (state=amber, no active failure but > 5 min old)
     - cyan  → currently running the KiteTicker WebSocket
     - slate → warm spare (healthy, not active)
     No separate "active" chip needed. Operator: "color code the account
     which is active or having problems in connection etc." */
  .bh-row-account-red      { color: var(--c-short); font-weight: 700; }
  .bh-row-account-amber    { color: var(--c-action); font-weight: 700; }
  .bh-row-account-inactive { color: var(--text-faint); font-weight: 600; }
  .bh-row-account-active   { color: var(--c-info); font-weight: 700; }
  .bh-row-account-spare    { color: #c8d8f0; font-weight: 600; }
  /* Circuit-breaker state chips inside the account cell */
  .bh-circuit-chip {
    font-size: 0.6rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    padding: 0.05rem 0.3rem;
    border-radius: 9999px;
    color: var(--c-short);
    background: rgba(248, 113, 113, 0.15);
    border: 1px solid rgba(248, 113, 113, 0.4);
    vertical-align: middle;
    flex-shrink: 0;
  }
  .bh-circuit-half {
    color: var(--c-action);
    background: rgba(251, 191, 36, 0.12);
    border-color: rgba(251, 191, 36, 0.4);
  }

  .bh-row-broker {
    color: var(--text-muted);
    text-transform: uppercase;
    font-size: var(--fs-sm);
  }
  .bh-row-state {
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.06em;
    border-radius: 9999px;
    padding: 0.05rem 0.4rem;
    text-align: center;
  }
  .bh-row-state-green    { color: var(--c-long); background: var(--c-long-10); }
  .bh-row-state-amber    { color: var(--c-action); background: rgba(251,191,36,0.10); }
  .bh-row-state-red      { color: var(--c-short); background: var(--c-short-10); }
  .bh-row-state-inactive { color: var(--text-faint); }

  .bh-row-reason {
    /* Was #64748b (WCAG 3.01:1 on --card-bg-gradient — borderline/fail).
       --text-lo (#8294a8) passes 4.60:1 on #1d2a44, 4.55:1 on #273552. */
    color: var(--text-lo);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--fs-sm);
  }
  .bh-row-ts {
    /* Was #475569 (WCAG 1.89:1 — fails). --text-lo passes on both bg layers. */
    color: var(--text-lo);
    font-size: var(--fs-sm);
    text-align: right;
    white-space: nowrap;
  }

  .bh-empty {
    /* Was #64748b (fail). --text-lo passes. */
    color: var(--text-lo);
    font-size: var(--fs-md);
    text-align: center;
    padding: 1.5rem 1rem;
  }

  /* ── Panel footer ── */
  .bh-modal-footer {
    border-top: 1px solid rgba(255, 255, 255, 0.08);
    padding: 0.45rem 0.8rem;
  }
  .bh-footer-note {
    /* Was #475569 (WCAG 1.89:1 — fails). --text-lo passes. */
    font-size: var(--fs-xs);
    color: var(--text-lo);
    font-family: var(--font-numeric);
  }

  /* ── Mobile: full-width modal, shift down from top ── */
  @media (max-width: 640px) {
    .bh-modal {
      top: 3.5rem;
      right: 0.25rem;
      left: 0.25rem;
      width: auto;
      max-height: 70vh;
    }
    .bh-row,
    .bh-headrow {
      grid-template-columns: 0.6rem 4rem 2.5rem 3rem 1fr 4rem;
      gap: 0.35rem;
    }
    .bh-row {
      font-size: var(--fs-sm);
    }
  }
</style>
