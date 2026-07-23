<!--
  NavBreakdown — slot-specific per-account breakdown table.

  Switches columns based on `activeSlot` (P/M/C/H) so the popup
  immediately shows the data that matches the NavStrip pill value the
  operator just clicked:

    P — Day P&L (baseDayPnlForPosition) + Lifetime P&L (Σ pnl) + Expiry P&L (lognormal projection)
    M — Available Margin + Total Margin (used + avail)
    C — Live Cash (live_cash ?? cash) + Total Cash (+ long-option premium)
    H — Today MTM (Σ day_change_val) + Value (Σ cur_val) + Lifetime (Σ pnl)

  TOTAL row sums the same scoped accounts and matches the NavStrip pill
  value for that slot.

  Sources funds + positions + holdings from the module-level
  marketDataStores singletons. No extra fetch.

  Reused by:
    - dashboard NAV tab (algo dark palette)
    - any future surface that needs the per-account slot breakdown
-->
<script>
  import { onDestroy, untrack } from 'svelte';
  import { aggCompact } from '$lib/format';
  import { fundsStore, holdingsStore, positionsStore } from '$lib/data/marketDataStores.svelte.js';
  import { baseDayPnlForPosition } from '$lib/data/nav';
  import { accountDisplayOrder, sortAccountsBy } from '$lib/data/accountSort.js';
  import { exportRowsToCsv } from '$lib/utils/csvExport.js';

  /** @type {{
   *   accountFilter?: string[],
   *   activeSlot?: 'P'|'M'|'C'|'H',
   *   expiryByAcct?: Map<string, number>,
   * }} */
  let {
    // Empty = all accounts (no filter). When set, the table only
    // shows the picked accounts and the TOTAL row sums over the
    // filtered subset.
    accountFilter = /** @type {string[]} */ ([]),
    // When set, the table shows the data relevant to that NavStrip pill.
    activeSlot = /** @type {'P'|'M'|'C'|'H'} */ ('P'),
    // Per-account expiry P&L map — passed from PositionStrip (which has
    // access to symbolStore spots). NavBreakdown cannot compute this itself.
    expiryByAcct = /** @type {Map<string,number>} */ (new Map()),
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
  // store singletons.
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
  // three sources. Sorted by canonical display order.
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

  // ── P slot — per-account Day P&L + Lifetime P&L + Expiry P&L ────────
  // expiryPnl comes from PositionStrip (which owns symbolStore spots).
  const _pByAcct = $derived.by(() => {
    return _scopedAccounts.map(acct => {
      const rows = _positions.filter(p => String(p.account) === acct);
      const dayPnl      = rows.reduce((s, p) => s + baseDayPnlForPosition(p), 0);
      const lifetimePnl = rows.reduce((s, p) => s + Number(p.pnl ?? 0), 0);
      const expiryPnl   = expiryByAcct.get(acct) ?? null;
      return { account: acct, dayPnl, lifetimePnl, expiryPnl };
    });
  });

  const _pTotal = $derived.by(() => ({
    dayPnl:      _pByAcct.reduce((s, r) => s + r.dayPnl, 0),
    lifetimePnl: _pByAcct.reduce((s, r) => s + r.lifetimePnl, 0),
    expiryPnl:   _pByAcct.reduce((s, r) => s + (r.expiryPnl ?? 0), 0),
  }));

  // ── M slot — per-account Avail Margin + Total Margin from funds ──────
  const _mByAcct = $derived.by(() => {
    return _scopedAccounts.map(acct => {
      const f = _funds.find(x => String(x.account) === acct);
      const availMargin = Number(f?.avail_margin ?? 0);
      const usedMargin  = Number(f?.used_margin  ?? 0);
      const totalMargin = availMargin + usedMargin;
      return { account: acct, availMargin, totalMargin };
    });
  });

  const _mTotal = $derived.by(() => ({
    availMargin: _mByAcct.reduce((s, r) => s + r.availMargin, 0),
    totalMargin: _mByAcct.reduce((s, r) => s + r.totalMargin, 0),
  }));

  // ── C slot — per-account Live Cash + Total Cash ──────────────────────
  // Total Cash = live_cash + long-option premium paid
  // Long-option premium = Σ avg_price × qty for CE/PE with qty > 0
  const _cByAcct = $derived.by(() => {
    return _scopedAccounts.map(acct => {
      const f = _funds.find(x => String(x.account) === acct);
      const liveCash = Number(f?.live_cash ?? f?.cash ?? 0);
      const optPremium = _positions
        .filter(p =>
          String(p.account) === acct &&
          Number(p.quantity ?? 0) > 0 &&
          (String(p.tradingsymbol ?? '').endsWith('CE') ||
           String(p.tradingsymbol ?? '').endsWith('PE'))
        )
        .reduce((s, p) => s + Number(p.average_price ?? 0) * Number(p.quantity ?? 0), 0);
      const totalCash = liveCash + optPremium;
      return { account: acct, liveCash, totalCash };
    });
  });

  const _cTotal = $derived.by(() => ({
    liveCash:  _cByAcct.reduce((s, r) => s + r.liveCash, 0),
    totalCash: _cByAcct.reduce((s, r) => s + r.totalCash, 0),
  }));

  // ── H slot — per-account Today MTM + Value + Lifetime from holdings ──
  const _hByAcct = $derived.by(() => {
    return _scopedAccounts.map(acct => {
      const rows = _holdings.filter(h => String(h.account) === acct);
      const todayMtm    = rows.reduce((s, h) => s + Number(h.day_change_val ?? 0), 0);
      const value       = rows.reduce((s, h) => s + Number(h.cur_val        ?? 0), 0);
      const lifetimePnl = rows.reduce((s, h) => s + Number(h.pnl            ?? 0), 0);
      return { account: acct, todayMtm, value, lifetimePnl };
    });
  });

  const _hTotal = $derived.by(() => ({
    todayMtm:    _hByAcct.reduce((s, r) => s + r.todayMtm, 0),
    value:       _hByAcct.reduce((s, r) => s + r.value, 0),
    lifetimePnl: _hByAcct.reduce((s, r) => s + r.lifetimePnl, 0),
  }));

  // ── _hasData — slot-aware gate ───────────────────────────────────────
  const _hasData = $derived.by(() => {
    if (activeSlot === 'P') return _pByAcct.length > 0;
    if (activeSlot === 'M') return _mByAcct.length > 0;
    if (activeSlot === 'C') return _cByAcct.length > 0;
    if (activeSlot === 'H') return _hByAcct.length > 0;
    return false;
  });

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

  /** Caption text per slot. */
  const _caption = $derived.by(() => {
    if (activeSlot === 'P') return 'Day P&L | Lifetime P&L (Σ pnl) | Expiry P&L (lognormal projection)';
    if (activeSlot === 'M') return 'Available = Total − used margin | Total = used + available';
    if (activeSlot === 'C') return 'Cash Avail (CA) = live deployable cash | Total = CA + long option premiums';
    if (activeSlot === 'H') return 'Today MTM | Current Value | Lifetime P&L';
    return '';
  });

  /** Export the currently visible slot's data to CSV. */
  function _downloadCsv() {
    if (activeSlot === 'P') {
      const rows = [
        ..._pByAcct,
        { account: 'TOTAL', dayPnl: _pTotal.dayPnl, lifetimePnl: _pTotal.lifetimePnl, expiryPnl: _pTotal.expiryPnl },
      ];
      exportRowsToCsv(rows, [
        { header: 'Account',      key: 'account' },
        { header: 'Day P&L',      key: 'dayPnl',      format: (v) => v == null ? '' : String(v) },
        { header: 'Lifetime P&L', key: 'lifetimePnl', format: (v) => v == null ? '' : String(v) },
        { header: 'Expiry P&L',   key: 'expiryPnl',   format: (v) => v == null ? '' : String(v) },
      ], 'nav-p-breakdown.csv');
    } else if (activeSlot === 'M') {
      const rows = [
        ..._mByAcct,
        { account: 'TOTAL', availMargin: _mTotal.availMargin, totalMargin: _mTotal.totalMargin },
      ];
      exportRowsToCsv(rows, [
        { header: 'Account',      key: 'account' },
        { header: 'Avail Margin', key: 'availMargin', format: (v) => v == null ? '' : String(v) },
        { header: 'Total Margin', key: 'totalMargin', format: (v) => v == null ? '' : String(v) },
      ], 'nav-m-breakdown.csv');
    } else if (activeSlot === 'C') {
      const rows = [
        ..._cByAcct,
        { account: 'TOTAL', liveCash: _cTotal.liveCash, totalCash: _cTotal.totalCash },
      ];
      exportRowsToCsv(rows, [
        { header: 'Account',    key: 'account' },
        { header: 'Live Cash',  key: 'liveCash',  format: (v) => v == null ? '' : String(v) },
        { header: 'Total Cash', key: 'totalCash', format: (v) => v == null ? '' : String(v) },
      ], 'nav-c-breakdown.csv');
    } else if (activeSlot === 'H') {
      const rows = [
        ..._hByAcct,
        { account: 'TOTAL', todayMtm: _hTotal.todayMtm, value: _hTotal.value, lifetimePnl: _hTotal.lifetimePnl },
      ];
      exportRowsToCsv(rows, [
        { header: 'Account',      key: 'account' },
        { header: 'Today MTM',    key: 'todayMtm',    format: (v) => v == null ? '' : String(v) },
        { header: 'Value',        key: 'value',        format: (v) => v == null ? '' : String(v) },
        { header: 'Lifetime P&L', key: 'lifetimePnl', format: (v) => v == null ? '' : String(v) },
      ], 'nav-h-breakdown.csv');
    }
  }
</script>

{#if _hasData}
  <div class="nav-bd-wrap">
    {#if activeSlot === 'P'}
      <table class="algo-table nav-bd-table">
        <thead>
          <tr>
            <th scope="col" class="nav-bd-acct">Account</th>
            <th scope="col">Day P&L</th>
            <th scope="col">Lifetime</th>
            <th scope="col">Expiry</th>
          </tr>
        </thead>
        <tbody>
          {#each _pByAcct as r (r.account)}
            <tr>
              <td class="nav-bd-acct">{r.account}</td>
              <td class="nav-num {_cls(r.dayPnl)}">{_fmt(r.dayPnl)}</td>
              <td class="nav-num {_cls(r.lifetimePnl)}">{_fmt(r.lifetimePnl)}</td>
              <td class="nav-num {_cls(r.expiryPnl)}">{_fmt(r.expiryPnl)}</td>
            </tr>
          {/each}
          <tr class="nav-bd-total">
            <td class="nav-bd-acct">TOTAL</td>
            <td class="nav-num">{_fmt(_pTotal.dayPnl)}</td>
            <td class="nav-num">{_fmt(_pTotal.lifetimePnl)}</td>
            <td class="nav-num">{_fmt(_pTotal.expiryPnl)}</td>
          </tr>
        </tbody>
      </table>
    {:else if activeSlot === 'M'}
      <table class="algo-table nav-bd-table">
        <thead>
          <tr>
            <th scope="col" class="nav-bd-acct">Account</th>
            <th scope="col">Avail Margin</th>
            <th scope="col">Total Margin</th>
          </tr>
        </thead>
        <tbody>
          {#each _mByAcct as r (r.account)}
            <tr>
              <td class="nav-bd-acct">{r.account}</td>
              <td class="nav-num {_cls(r.availMargin)}">{_fmt(r.availMargin)}</td>
              <td class="nav-num {_cls(r.totalMargin)}">{_fmt(r.totalMargin)}</td>
            </tr>
          {/each}
          <tr class="nav-bd-total">
            <td class="nav-bd-acct">TOTAL</td>
            <td class="nav-num">{_fmt(_mTotal.availMargin)}</td>
            <td class="nav-num">{_fmt(_mTotal.totalMargin)}</td>
          </tr>
        </tbody>
      </table>
    {:else if activeSlot === 'C'}
      <table class="algo-table nav-bd-table">
        <thead>
          <tr>
            <th scope="col" class="nav-bd-acct">Account</th>
            <th scope="col">Live Cash</th>
            <th scope="col">Total Cash</th>
          </tr>
        </thead>
        <tbody>
          {#each _cByAcct as r (r.account)}
            <tr>
              <td class="nav-bd-acct">{r.account}</td>
              <td class="nav-num {_cls(r.liveCash)}">{_fmt(r.liveCash)}</td>
              <td class="nav-num {_cls(r.totalCash)}">{_fmt(r.totalCash)}</td>
            </tr>
          {/each}
          <tr class="nav-bd-total">
            <td class="nav-bd-acct">TOTAL</td>
            <td class="nav-num">{_fmt(_cTotal.liveCash)}</td>
            <td class="nav-num">{_fmt(_cTotal.totalCash)}</td>
          </tr>
        </tbody>
      </table>
    {:else if activeSlot === 'H'}
      <table class="algo-table nav-bd-table">
        <thead>
          <tr>
            <th scope="col" class="nav-bd-acct">Account</th>
            <th scope="col">Today MTM</th>
            <th scope="col">Value</th>
            <th scope="col">Lifetime</th>
          </tr>
        </thead>
        <tbody>
          {#each _hByAcct as r (r.account)}
            <tr>
              <td class="nav-bd-acct">{r.account}</td>
              <td class="nav-num {_cls(r.todayMtm)}">{_fmt(r.todayMtm)}</td>
              <td class="nav-num {_cls(r.value)}">{_fmt(r.value)}</td>
              <td class="nav-num {_cls(r.lifetimePnl)}">{_fmt(r.lifetimePnl)}</td>
            </tr>
          {/each}
          <tr class="nav-bd-total">
            <td class="nav-bd-acct">TOTAL</td>
            <td class="nav-num">{_fmt(_hTotal.todayMtm)}</td>
            <td class="nav-num">{_fmt(_hTotal.value)}</td>
            <td class="nav-num">{_fmt(_hTotal.lifetimePnl)}</td>
          </tr>
        </tbody>
      </table>
    {/if}
    <!-- Caption — slot-specific formula footnote so the operator
         glances and knows what each column means without hovering. -->
    <div class="nav-bd-caption">
      <span>{_caption}</span>
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

  .nav-num {
    text-align: right;
    font-variant-numeric: tabular-nums;
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
  /* TOTAL row always renders amber — direction classes don't apply. */

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

  /* Mobile: tighten padding so columns fit comfortably on a
     360px viewport without column wrap or horizontal scroll. */
  @media (max-width: 600px) {
    .nav-bd-table          { font-size: 0.65rem; }
    .nav-bd-table thead th { font-size: 0.6rem; padding: 0 2px; }
    .nav-bd-table tbody td { padding: 0 2px; }
    .nav-bd-caption        { font-size: 0.55rem; }
  }
</style>
