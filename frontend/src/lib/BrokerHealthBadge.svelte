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
  import { createGrid, ModuleRegistry, AllCommunityModule } from 'ag-grid-community';
  import { mkBaseGridOpts } from '$lib/data/algoGridUtils.js';
  import { brokerHealthStore, openActivityModal } from '$lib/stores';
  ModuleRegistry.registerModules([AllCommunityModule]);
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
      return d.toLocaleTimeString('en-IN', {
        timeZone: 'Asia/Kolkata',
        hour: '2-digit', minute: '2-digit', hour12: false,
      }) + ' IST';
    } catch (_) {
      return iso;
    }
  }

  // ── ag-Grid ────────────────────────────────────────────────────────────
  let _gridEl = $state(null);
  let _gridApi = null;

  const _colDefs = [
    {
      field: 'state', headerName: '', colId: 'dot',
      width: 30, minWidth: 30, maxWidth: 30,
      cellRenderer: p => {
        const el = document.createElement('span');
        el.className = `bh-dot bh-dot-${p.value ?? 'inactive'}`;
        return el;
      },
    },
    {
      field: 'account', headerName: 'Account', width: 115, minWidth: 80,
      cellRenderer: p => {
        const acct = p.data ?? {};
        const state = acct.state ?? '';
        const accCls = state === 'red'       ? 'bh-acct-red'
                     : state === 'amber'     ? 'bh-acct-amber'
                     : state === 'inactive'  ? 'bh-acct-inactive'
                     : acct.is_active_ticker ? 'bh-acct-active'
                     : 'bh-acct-spare';
        const cbOptIn = !!acct.circuit_breaker_enabled;
        const title = cbOptIn && acct.circuit_state === 'open'
          ? `${acct.account} — circuit open until ${_fmtIso(acct.circuit_open_until)}`
          : state === 'red' ? `${acct.account} — connection problem (${acct.reason})`
          : state === 'amber' ? `${acct.account} — stale (${acct.reason})`
          : acct.is_active_ticker ? `${acct.account} — active (KiteTicker)`
          : `${acct.account} — warm spare`;
        const wrap = document.createElement('span');
        wrap.className = `bh-row-account ${accCls}`;
        wrap.title = title;
        wrap.textContent = p.value ?? '';
        if (cbOptIn && acct.circuit_state === 'open') {
          const chip = document.createElement('span');
          chip.className = 'bh-circuit-chip';
          chip.title = 'Circuit breaker open';
          chip.textContent = 'OPEN';
          wrap.appendChild(chip);
        } else if (cbOptIn && acct.circuit_state === 'half-open') {
          const chip = document.createElement('span');
          chip.className = 'bh-circuit-chip bh-circuit-half';
          chip.title = 'Circuit half-open — probing';
          chip.textContent = 'PROBE';
          wrap.appendChild(chip);
        }
        return wrap;
      },
    },
    {
      field: 'broker', headerName: 'Broker', width: 70,
      valueFormatter: p => (p.value || 'kite').toUpperCase(),
      cellClass: 'bh-col-broker',
    },
    {
      field: 'state', headerName: 'Status', colId: 'stateBadge', width: 80,
      cellRenderer: p => {
        const el = document.createElement('span');
        el.className = `bh-row-state bh-row-state-${p.value ?? 'inactive'}`;
        el.textContent = (p.value ?? '').toUpperCase();
        return el;
      },
    },
    {
      field: 'reason', headerName: 'Reason', flex: 1, minWidth: 80,
      cellClass: 'bh-col-reason',
    },
    {
      field: 'last_good_at', headerName: 'Last Good', width: 105,
      valueFormatter: p => _fmtIso(p.value),
      cellClass: 'bh-col-ts',
    },
  ];

  $effect(() => {
    if (!_gridEl) return;
    _gridApi?.destroy();
    _gridApi = createGrid(_gridEl, {
      ...mkBaseGridOpts(),
      columnDefs: _colDefs,
      rowData: [],
      domLayout: 'autoHeight',
      suppressCellFocus: true,
      onRowClicked: () => { open = false; openActivityModal('conn'); },
    });
  });

  $effect(() => {
    _gridApi?.setGridOption('rowData', accounts);
  });

  onDestroy(() => { _gridApi?.destroy(); _gridApi = null; });
</script>

<svelte:window onkeydown={open ? (e) => { if (e.key === 'Escape') { e.preventDefault(); open = false; } } : null} />

{#if open}
  <!-- Modal overlay -->
  <div class="bh-overlay" role="presentation"></div>
  <div class="bh-modal algo-modal" role="dialog" aria-label="Broker auth health">
    <div class="bh-modal-header canonical-modal-header">
      <span class="bh-modal-title">Broker Auth Health</span>
      <button class="bh-close" onclick={() => open = false} aria-label="Close">×</button>
    </div>
    <div class="bh-modal-body">
      <div bind:this={_gridEl} class="ag-theme-quartz ag-theme-algo bh-ag-grid"></div>
    </div>
    <div class="bh-modal-footer">
      <span class="bh-footer-note">Polls every 30 s · Auth state from broker API calls</span>
    </div>
  </div>
{/if}

<style>
  /* ── Modal overlay ── */
  .bh-overlay {
    position: fixed;
    inset: 0;
    z-index: 9990;
    background: transparent;
    pointer-events: none;
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
    color: var(--algo-cyan-text);
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
    padding: 0;
  }

  /* ag-Grid container — autoHeight, no fixed height needed */
  .bh-ag-grid {
    width: 100%;
  }

  /* ── Cell-renderer classes — must be :global so ag-Grid's dynamic DOM picks them up ── */
  :global(.bh-dot) {
    display: inline-block;
    width: 0.45rem;
    height: 0.45rem;
    border-radius: 50%;
  }
  :global(.bh-dot-green)    { background: var(--c-long); }
  :global(.bh-dot-amber)    { background: var(--c-action); }
  :global(.bh-dot-red)      { background: var(--c-short); }
  :global(.bh-dot-inactive) { background: var(--text-faint); }

  :global(.bh-row-account) {
    color: #c8d8f0;
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
  }
  :global(.bh-acct-red)      { color: var(--c-short) !important; font-weight: 700 !important; }
  :global(.bh-acct-amber)    { color: var(--c-action) !important; font-weight: 700 !important; }
  :global(.bh-acct-inactive) { color: var(--text-faint) !important; }
  :global(.bh-acct-active)   { color: var(--c-info) !important; font-weight: 700 !important; }
  :global(.bh-acct-spare)    { color: #c8d8f0; }

  :global(.bh-circuit-chip) {
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
  :global(.bh-circuit-half) {
    color: var(--c-action) !important;
    background: rgba(251, 191, 36, 0.12) !important;
    border-color: rgba(251, 191, 36, 0.4) !important;
  }

  :global(.bh-col-broker) {
    color: var(--text-muted);
    text-transform: uppercase;
    font-size: var(--fs-sm);
  }

  :global(.bh-row-state) {
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.06em;
    border-radius: 9999px;
    padding: 0.05rem 0.4rem;
    text-align: center;
  }
  :global(.bh-row-state-green)    { color: var(--c-long); background: var(--c-long-10); }
  :global(.bh-row-state-amber)    { color: var(--c-action); background: rgba(251,191,36,0.10); }
  :global(.bh-row-state-red)      { color: var(--c-short); background: var(--c-short-10); }
  :global(.bh-row-state-inactive) { color: var(--text-faint); }

  :global(.bh-col-reason) {
    color: var(--text-lo);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--fs-sm);
  }
  :global(.bh-col-ts) {
    color: var(--text-lo);
    font-size: var(--fs-sm);
    text-align: right;
    white-space: nowrap;
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
  }
</style>
