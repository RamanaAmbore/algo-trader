<!--
  NavBreakdown — per-account NAV breakdown table.

  Same arithmetic as PerformancePage's `navByAcct` derivation and
  backend/api/algo/nav.py:compute_firm_nav (v4 formula):

      NAV = (cash_sod + option_premium)     ← "Cash" column
          + Σ position.unrealised           ← "Pos M2M" column
          + Σ holdings.cur_val              ← "Holdings" column

  Sources funds + positions + holdings from the module-level
  marketDataStores singletons (three-tier: memory → localStorage →
  broker fetch). No extra fetch — the dashboard's loadHero already
  warms these stores. TOTAL row matches PerformancePage's TOTAL row
  exactly so the dashboard NAV tab and /performance NAV grid can't
  drift.

  Reused by:
    - dashboard NAV tab (algo dark palette)
    - any future surface that needs the per-account NAV breakdown

  No ag-Grid — the row count is tiny (one per broker account, plus
  TOTAL). Plain HTML table keeps the bundle light and the markup
  testable from the e2e suite without ag-Grid plumbing.
-->
<script>
  import { onDestroy, untrack } from 'svelte';
  import { aggCompact } from '$lib/format';
  import { fundsStore, holdingsStore, positionsStore } from '$lib/data/marketDataStores.svelte.js';
  import { navByAccount, navTotalRow } from '$lib/data/nav';
  import { accountDisplayOrder, sortAccountsBy } from '$lib/data/accountSort.js';
  import { exportRowsToCsv } from '$lib/utils/csvExport.js';

  /** @type {{
   *   accountFilter?: string[],
   *   activeSlot?: 'P'|'M'|'C'|'H',
   * }} */
  let {
    // Empty = all accounts (no filter). When set, the table only
    // shows the picked accounts and the TOTAL row sums over the
    // filtered subset.
    accountFilter = /** @type {string[]} */ ([]),
    // When set, highlights the column group relevant to the clicked
    // pill so the operator knows which values they're looking at.
    activeSlot = /** @type {'P'|'M'|'C'|'H'} */ ('P'),
  } = $props();

  /**
   * Public method — lets a parent component (e.g. dashboard CardControls
   * toolbar) trigger the CSV download without needing to own the data.
   * Usage: bind:this={ref} then ref?.downloadCsv?.()
   */
  export function downloadCsv() {
    _downloadCsv();
  }

  // Module-level store reads — $derived so the table re-renders
  // whenever loadHero (or any other surface) writes through the
  // store singletons. No local fetch.
  /** @type {any[]} */
  const _funds      = $derived(fundsStore.value     ?? []);
  /** @type {any[]} */
  const _positions  = $derived(positionsStore.value ?? []);
  /** @type {any[]} */
  const _holdings   = $derived(holdingsStore.value  ?? []);

  // ── Loading-state machine ────────────────────────────────────────────
  // Three explicit states so the operator never sees a silent perpetual
  // spinner:
  //   1. loading  — at least one store still in-flight (or never loaded).
  //                 After 10 s flip to "timed-out" so the operator can act.
  //   2. empty    — all three stores have completed (lastFetch > 0) with no
  //                 data. Show actionable "check broker connections" message.
  //   3. error    — at least one store surfaced an error. Show message + Retry.
  //   4. ready    — table renders.

  /** True while any of the three stores is actively fetching. */
  const _inFlight = $derived(
    positionsStore.loading || holdingsStore.loading || fundsStore.loading
  );

  /** True once all three stores have completed at least one successful fetch. */
  const _allLoaded = $derived(
    positionsStore.lastFetch > 0 &&
    holdingsStore.lastFetch  > 0 &&
    fundsStore.lastFetch     > 0
  );

  /** First fetch-error string across all three stores (null when clean). */
  const _anyError = $derived(
    positionsStore.error || holdingsStore.error || fundsStore.error || null
  );

  /** Flips to true >5 s into loading: show "retrying — network slow" hint. */
  let _slowLoad  = $state(false);
  /** 10-second hard timeout — flips to true when still loading after 10 s. */
  let _timedOut  = $state(false);
  /** @type {ReturnType<typeof setTimeout>|null} */
  let _slowHandle    = null;
  /** @type {ReturnType<typeof setTimeout>|null} */
  let _timeoutHandle = null;

  $effect(() => {
    // Read reactive deps inside the effect; write via untrack() so the writes
    // don't re-trigger the effect (avoids reactive churn on every store poll).
    const shouldArm = _inFlight && !_allLoaded;
    untrack(() => {
      if (shouldArm) {
        // Arm the slow-load hint (5 s) and hard timeout (10 s).
        if (!_slowHandle) {
          _slowHandle = setTimeout(() => { _slowLoad = true; }, 5_000);
        }
        if (!_timeoutHandle) {
          _timeoutHandle = setTimeout(() => { _timedOut = true; }, 10_000);
        }
      } else {
        // Loaded or errored — cancel both clocks.
        if (_slowHandle)    { clearTimeout(_slowHandle);    _slowHandle    = null; }
        if (_timeoutHandle) { clearTimeout(_timeoutHandle); _timeoutHandle = null; }
        // Identity-guarded writes: avoids churn when already at the resting value.
        if (_slowLoad)  _slowLoad  = false;
        if (_timedOut)  _timedOut  = false;
      }
    });
  });

  onDestroy(() => {
    if (_slowHandle)    clearTimeout(_slowHandle);
    if (_timeoutHandle) clearTimeout(_timeoutHandle);
  });

  /** Force a fresh fetch on all three stores (Retry button handler). */
  function _retry() {
    _timedOut = false;
    if (_timeoutHandle) { clearTimeout(_timeoutHandle); _timeoutHandle = null; }
    positionsStore.load({ fresh: true });
    holdingsStore.load({ fresh: true });
    fundsStore.load({ fresh: true });
  }

  // Canonical account display order map — $state so _allAccounts re-derives
  // when fetchBrokerOrder() resolves after cold load.
  let _navOrderMap = $state(/** @type {Record<string,number>} */ ({}));
  const _unsubNavOrder = accountDisplayOrder.subscribe(m => { _navOrderMap = m; });
  onDestroy(() => { _unsubNavOrder(); });

  // Page-wide account union — every account with data in any of the
  // three sources. Sorted by canonical display order (Kite → DH3747 →
  // Groww → DH6847) so row order matches every other grid.
  const _allAccounts = $derived.by(() => {
    const set = new Set();
    for (const r of _funds)     if (r.account && r.account !== 'TOTAL') set.add(String(r.account));
    for (const r of _positions) if (r.account) set.add(String(r.account));
    for (const r of _holdings)  if (r.account) set.add(String(r.account));
    return sortAccountsBy([...set], _navOrderMap);
  });

  const _scopedAccounts = $derived.by(() => {
    if (!accountFilter || accountFilter.length === 0) return _allAccounts;
    const allow = new Set(accountFilter.map(String));
    return _allAccounts.filter(a => allow.has(a));
  });

  // Canonical NAV breakdown via `$lib/data/nav`. Same math as
  // PerformancePage navByAcct + backend nav.py:compute_firm_nav.
  const _navByAcct = $derived(navByAccount(_scopedAccounts, _funds, _positions, _holdings));
  const _navTotal  = $derived(navTotalRow(_navByAcct));

  function _cls(v) {
    if (v == null || !Number.isFinite(v)) return 'nav-zero';
    if (v > 0) return 'nav-up';
    if (v < 0) return 'nav-down';
    return 'nav-zero';
  }
  function _fmt(v) {
    if (v == null || !Number.isFinite(v)) return '—';
    return aggCompact(v);
  }

  /** Export NAV breakdown rows (including TOTAL) to CSV. */
  function _downloadCsv() {
    const rows = [
      ..._navByAcct,
      ...(_navTotal ? [{ account: 'TOTAL', ..._navTotal }] : []),
    ];
    exportRowsToCsv(
      rows,
      [
        { header: 'Account',  key: 'account' },
        { header: 'Cash',     key: 'cash',         format: (v) => v == null ? '' : String(v) },
        { header: 'Pos M2M',  key: 'pos_m2m',      format: (v) => v == null ? '' : String(v) },
        { header: 'Holdings', key: 'holdings_mtm',  format: (v) => v == null ? '' : String(v) },
        { header: 'NAV',      key: 'nav',           format: (v) => v == null ? '' : String(v) },
      ],
      'nav-breakdown.csv'
    );
  }
</script>

{#if _navByAcct.length > 0}
  <div class="nav-bd-wrap">
    <table class="algo-table nav-bd-table">
      <thead>
        <tr>
          <th scope="col" class="nav-bd-acct">Account</th>
          <th scope="col" class:nav-bd-col-active={activeSlot === 'C' || activeSlot === 'M'}>Cash</th>
          <th scope="col" class:nav-bd-col-active={activeSlot === 'P'}>Pos M2M</th>
          <th scope="col" class:nav-bd-col-active={activeSlot === 'H'}>Holdings</th>
          <th scope="col" class="nav-bd-nav" class:nav-bd-col-active={activeSlot === 'P' || activeSlot === 'M'}>NAV</th>
        </tr>
      </thead>
      <tbody>
        {#each _navByAcct as r (r.account)}
          <tr>
            <td class="nav-bd-acct">{r.account}</td>
            <td class="nav-num {_cls(r.cash)}" class:nav-bd-col-active={activeSlot === 'C' || activeSlot === 'M'}>{_fmt(r.cash)}</td>
            <td class="nav-num {_cls(r.pos_m2m)}" class:nav-bd-col-active={activeSlot === 'P'}>{_fmt(r.pos_m2m)}</td>
            <td class="nav-num {_cls(r.holdings_mtm)}" class:nav-bd-col-active={activeSlot === 'H'}>{_fmt(r.holdings_mtm)}</td>
            <td class="nav-num nav-bd-nav {_cls(r.nav)}" class:nav-bd-col-active={activeSlot === 'P' || activeSlot === 'M'}>{_fmt(r.nav)}</td>
          </tr>
        {/each}
        {#if _navTotal}
          <tr class="nav-bd-total">
            <td class="nav-bd-acct">TOTAL</td>
            <td class="nav-num {_cls(_navTotal?.cash)}" class:nav-bd-col-active={activeSlot === 'C' || activeSlot === 'M'}>{_fmt(_navTotal?.cash)}</td>
            <td class="nav-num {_cls(_navTotal?.pos_m2m)}" class:nav-bd-col-active={activeSlot === 'P'}>{_fmt(_navTotal?.pos_m2m)}</td>
            <td class="nav-num {_cls(_navTotal?.holdings_mtm)}" class:nav-bd-col-active={activeSlot === 'H'}>{_fmt(_navTotal?.holdings_mtm)}</td>
            <td class="nav-num nav-bd-nav {_cls(_navTotal?.nav)}" class:nav-bd-col-active={activeSlot === 'P' || activeSlot === 'M'}>{_fmt(_navTotal?.nav)}</td>
          </tr>
        {/if}
      </tbody>
    </table>
    <!-- Caption — same formula footnote PerformancePage carries inline
         in its column-header tooltip; surfaced here so the operator
         glances and knows what each column means without hovering.
         Download is exposed via the exported downloadCsv() method so the
         parent can wire it into its CardControls toolbar. -->
    <div class="nav-bd-caption">
      <span>NAV = Cash (SOD + long-option premium) + Σ position M2M + Σ holdings MTM</span>
    </div>
  </div>
{:else if _anyError && !_inFlight}
  <!-- State 3: fetch error — at least one store returned an error. -->
  <div class="nav-bd-empty nav-bd-error" role="alert" data-testid="nav-bd-error">
    <span class="nav-bd-status-icon" aria-hidden="true">⚠</span>
    <span class="nav-bd-status-text">NAV data unavailable — {_anyError}</span>
    <button class="nav-bd-retry" onclick={_retry}>Retry</button>
  </div>
{:else if _timedOut}
  <!-- State 1b: hard timeout (>10 s) while loading. -->
  <div class="nav-bd-empty nav-bd-warn" role="alert" data-testid="nav-bd-timeout">
    <span class="nav-bd-status-icon" aria-hidden="true">⏳</span>
    <span class="nav-bd-status-text">Fetch timed out — click Retry</span>
    <button class="nav-bd-retry" onclick={_retry}>Retry</button>
  </div>
{:else if _allLoaded}
  <!-- State 2: loaded successfully but no data (empty broker accounts?). -->
  <div class="nav-bd-empty nav-bd-hint" data-testid="nav-bd-empty">
    <span class="nav-bd-status-icon" aria-hidden="true">—</span>
    <span class="nav-bd-status-text">No NAV data — check
      <a class="nav-bd-link" href="/admin/brokers">broker connections</a>
    </span>
  </div>
{:else}
  <!-- State 1a: loading (in-flight or stores not yet started). -->
  <div class="nav-bd-empty" data-testid="nav-bd-loading">
    Loading NAV breakdown…{_slowLoad ? ' (retrying — network slow)' : ''}
  </div>
{/if}

<style>
  /* NavBreakdown — visual rhythm matches .hist-table (History page)
     and the updated ag-theme-algo: 26px rows, deep-dark header with
     muted-slate text + amber bottom border, slate cell borders, cyan
     row-hover tint, monospace numerics. Plain HTML table avoids ag-Grid
     overhead for 2–4 rows but is visually indistinguishable. */

  .nav-bd-wrap {
    display: flex;
    flex-direction: column;
    gap: 0;
    padding: 0;
    /* Same rounded-corner + border as ag-theme-algo wrapper. */
    border-radius: 4px;
    overflow: hidden;
    border: 1.5px solid rgba(255, 255, 255, 0.10);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45),
                inset 0 1px 0 rgba(255, 255, 255, 0.08);
    /* Defensive background seal — prevents parent bleed if wrapper
       ever gains padding or a gap between child rows. Uses the
       elevated token so it matches algo-grid-chrome / ag-root-wrapper
       (the same surface family as the inner table rows). */
    background: var(--card-bg-elevated);
  }

  /* .nav-bd-table width:100% removed — algo-table global provides it. */

  /* Header — deep-dark bg + muted-slate text + amber bottom border:
     mirrors the .hist-table reference (History page). */
  .nav-bd-table thead th {
    height: 28px;              /* matches --ag-header-height: 28px in ag-theme-algo */
    text-align: right;
    font-weight: 800;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    font-size: 0.6rem;         /* matches --ag-header-font-size in ag-theme-algo */
    color: var(--c-muted);            /* --text-muted / var(--algo-muted) */
    background: rgba(15,23,42,0.30); /* matches ag-theme-algo header bg */
    padding: 0 3px;            /* matches ag-theme-algo cell padding */
    border-right: 1px solid rgba(126,151,184,0.18);
    border-bottom: 1px solid rgba(251,191,36,0.30); /* amber accent */
    white-space: nowrap;
    vertical-align: middle;
  }
  .nav-bd-table thead th:last-child { border-right: none; }
  .nav-bd-table thead th.nav-bd-acct {
    text-align: left;
  }

  /* Body rows */
  .nav-bd-table tbody td {
    height: 26px;              /* matches _baseGridOpts rowHeight: 26 */
    padding: 0 3px;            /* matches ag-theme-algo cell padding */
    border-bottom: 1px solid rgba(126,151,184,0.10); /* slate row divider */
    border-right: 1px solid rgba(126,151,184,0.10);  /* slate col divider */
    white-space: nowrap;
    vertical-align: middle;
  }
  .nav-bd-table tbody td:last-child { border-right: none; }
  .nav-bd-table tbody tr:last-child td { border-bottom: none; }

  .nav-bd-acct {
    text-align: left;
    color: var(--algo-slate);
    font-weight: 600;
  }

  /* Active column — highlights the metric group matching the clicked pill.
     Subtle amber tint + bold weight so the operator instantly spots the
     relevant column without other metrics disappearing. */
  .nav-bd-col-active {
    background: rgba(251, 191, 36, 0.07);
    font-weight: 700;
    color: var(--c-action) !important;
  }
  .nav-bd-total .nav-bd-col-active {
    color: var(--c-action) !important;
  }
  .nav-num {
    text-align: right;
    font-variant-numeric: tabular-nums;
  }
  .nav-bd-nav {
    font-weight: 700;
  }

  /* Direction palette matches the algo theme tokens used across
     PerformancePage / MarketPulse / ag-theme-algo pnl-gain/pnl-loss. */
  .nav-up   { color: var(--c-long); }
  .nav-down { color: var(--c-short); }
  .nav-zero { color: var(--algo-slate); }

  /* TOTAL row — amber tint mirrors ag-theme-algo totals-row rule.
     border-top 2px amber matches the ag-Grid TOTAL row treatment.
     Layered over opaque #1d2a44 base to prevent scroll bleed. */
  .nav-bd-total td {
    background:
      linear-gradient(rgba(251,191,36,0.22), rgba(251,191,36,0.22)),
      #1d2a44 !important;
    color: var(--c-action) !important;
    border-top: 2px solid rgba(251, 191, 36, 0.70) !important;
    border-bottom: 1px solid rgba(251, 191, 36, 0.55) !important;
    font-weight: 700;
    letter-spacing: 0.05em;
    text-transform: uppercase;
  }
  .nav-bd-total .nav-up,
  .nav-bd-total .nav-down,
  .nav-bd-total .nav-zero {
    color: var(--c-action) !important;
  }

  .nav-bd-caption {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.6rem;
    color: var(--algo-muted);
    letter-spacing: 0.04em;
    padding: 0.25rem 0.5rem;
    background: #1d2a44;
    border-top: 1px solid rgba(126,151,184,0.10);
  }
  .nav-bd-caption span {
    flex: 1;
  }

  .nav-bd-empty {
    padding: 1.2rem 0.8rem;
    text-align: center;
    color: rgba(155, 176, 208, 0.55);
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.72rem;
    background: #1d2a44;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: center;
    gap: 0.4rem;
  }

  /* Error state — red tint matching PerformancePage .perf-banner-error palette. */
  .nav-bd-error {
    background: rgba(248, 113, 113, 0.07);
    color: var(--c-short);
    border-top: 1px solid rgba(248, 113, 113, 0.25);
  }

  /* Slow/timed-out warning — amber tint matching the algo-theme warn palette. */
  .nav-bd-warn {
    background: rgba(251, 191, 36, 0.07);
    color: var(--c-action);
    border-top: 1px solid rgba(251, 191, 36, 0.25);
  }

  /* Hint (empty-but-loaded) — muted slate, same as the loading text. */
  .nav-bd-hint {
    color: rgba(155, 176, 208, 0.70);
  }

  .nav-bd-status-icon {
    font-size: 0.9rem;
    flex-shrink: 0;
  }

  .nav-bd-status-text {
    flex: 1 1 auto;
    min-width: 0;
  }

  .nav-bd-link {
    color: var(--c-info);
    text-decoration: underline;
    text-underline-offset: 2px;
  }
  .nav-bd-link:hover {
    color: #67e8f9;
  }

  /* Retry button — cyan-400 palette matching RefreshButton / PageHeaderActions
     so the operator doesn't have to relearn the "action" visual language. */
  .nav-bd-retry {
    flex-shrink: 0;
    padding: 0.15rem 0.6rem;
    border-radius: 3px;
    border: 1px solid rgba(34, 211, 238, 0.55);
    background: var(--c-info-14);
    color: var(--c-info);
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.68rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    cursor: pointer;
    transition: background 120ms, border-color 120ms;
  }
  .nav-bd-retry:hover {
    background: var(--c-info-22);
    border-color: rgba(34, 211, 238, 0.80);
    color: #67e8f9;
  }

  /* Mobile: tighten padding so 5 columns fit comfortably on a
     360px viewport without column wrap or horizontal scroll. */
  @media (max-width: 600px) {
    .nav-bd-table          { font-size: 0.65rem; }
    .nav-bd-table thead th { font-size: 0.6rem; padding: 0 2px; }
    .nav-bd-table tbody td { padding: 0 2px; }
    .nav-bd-caption        { font-size: 0.55rem; }
  }
</style>
