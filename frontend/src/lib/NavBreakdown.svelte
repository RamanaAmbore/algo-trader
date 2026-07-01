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
  import { aggCompact } from '$lib/format';
  import { fundsStore, holdingsStore, positionsStore } from '$lib/data/marketDataStores.svelte.js';
  import { navByAccount, navTotalRow } from '$lib/data/nav';

  /** @type {{
   *   accountFilter?: string[],
   * }} */
  let {
    // Empty = all accounts (no filter). When set, the table only
    // shows the picked accounts and the TOTAL row sums over the
    // filtered subset.
    accountFilter = /** @type {string[]} */ ([]),
  } = $props();

  // Module-level store reads — $derived so the table re-renders
  // whenever loadHero (or any other surface) writes through the
  // store singletons. No local fetch.
  /** @type {any[]} */
  const _funds      = $derived(fundsStore.value     ?? []);
  /** @type {any[]} */
  const _positions  = $derived(positionsStore.value ?? []);
  /** @type {any[]} */
  const _holdings   = $derived(holdingsStore.value  ?? []);

  // Page-wide account union — every account with data in any of the
  // three sources. Sorted alphabetically so row order matches the
  // Capital + Equity tabs and PerformancePage's NAV grid.
  const _allAccounts = $derived.by(() => {
    const set = new Set();
    for (const r of _funds)     if (r.account && r.account !== 'TOTAL') set.add(String(r.account));
    for (const r of _positions) if (r.account) set.add(String(r.account));
    for (const r of _holdings)  if (r.account) set.add(String(r.account));
    return [...set].sort();
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
</script>

{#if _navByAcct.length > 0}
  <div class="nav-bd-wrap">
    <table class="nav-bd-table">
      <thead>
        <tr>
          <th scope="col" class="nav-bd-acct">Account</th>
          <th scope="col">Cash</th>
          <th scope="col">Pos M2M</th>
          <th scope="col">Holdings</th>
          <th scope="col" class="nav-bd-nav">NAV</th>
        </tr>
      </thead>
      <tbody>
        {#each _navByAcct as r (r.account)}
          <tr>
            <td class="nav-bd-acct">{r.account}</td>
            <td class="nav-num {_cls(r.cash)}">{_fmt(r.cash)}</td>
            <td class="nav-num {_cls(r.pos_m2m)}">{_fmt(r.pos_m2m)}</td>
            <td class="nav-num {_cls(r.holdings_mtm)}">{_fmt(r.holdings_mtm)}</td>
            <td class="nav-num nav-bd-nav {_cls(r.nav)}">{_fmt(r.nav)}</td>
          </tr>
        {/each}
        {#if _navTotal}
          <tr class="nav-bd-total">
            <td class="nav-bd-acct">TOTAL</td>
            <td class="nav-num {_cls(_navTotal.cash)}">{_fmt(_navTotal.cash)}</td>
            <td class="nav-num {_cls(_navTotal.pos_m2m)}">{_fmt(_navTotal.pos_m2m)}</td>
            <td class="nav-num {_cls(_navTotal.holdings_mtm)}">{_fmt(_navTotal.holdings_mtm)}</td>
            <td class="nav-num nav-bd-nav {_cls(_navTotal.nav)}">{_fmt(_navTotal.nav)}</td>
          </tr>
        {/if}
      </tbody>
    </table>
    <!-- Caption — same formula footnote PerformancePage carries inline
         in its column-header tooltip; surfaced here so the operator
         glances and knows what each column means without hovering. -->
    <div class="nav-bd-caption">
      NAV = Cash (SOD + long-option premium) + Σ position M2M + Σ holdings MTM
    </div>
  </div>
{:else}
  <div class="nav-bd-empty">
    Loading NAV breakdown… (positions + holdings + funds warming)
  </div>
{/if}

<style>
  .nav-bd-wrap {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    padding: 0.2rem 0 0.4rem;
  }

  .nav-bd-table {
    width: 100%;
    border-collapse: collapse;
    font-family: var(--font-numeric);
    font-size: var(--fs-lg);
    color: var(--algo-slate);
  }
  .nav-bd-table thead th {
    text-align: right;
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    font-size: var(--fs-xs);
    color: var(--algo-muted);
    padding: 0.3rem 0.5rem;
    border-bottom: 1px solid rgba(126, 151, 184, 0.20);
    white-space: nowrap;
  }
  .nav-bd-table thead th.nav-bd-acct {
    text-align: left;
  }
  .nav-bd-table tbody td {
    padding: 0.35rem 0.5rem;
    border-bottom: 1px solid rgba(126, 151, 184, 0.08);
    white-space: nowrap;
  }
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
  .nav-bd-nav {
    font-weight: 700;
  }

  /* Direction palette matches the algo theme tokens used across
     PerformancePage / MarketPulse. */
  .nav-up   { color: #4ade80; }
  .nav-down { color: #f87171; }
  .nav-zero { color: var(--algo-slate); }

  /* TOTAL row — same amber tint convention every algo-page summary
     uses (Funds, Margin, Positions Summary, Holdings Summary). */
  .nav-bd-total td {
    background: rgba(251, 191, 36, 0.10);
    border-top: 2px solid rgba(251, 191, 36, 0.40);
    border-bottom: 1px solid rgba(251, 191, 36, 0.25);
    font-weight: 700;
  }

  .nav-bd-caption {
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    color: var(--algo-muted);
    letter-spacing: 0.04em;
    padding: 0 0.5rem;
  }

  .nav-bd-empty {
    padding: 1.2rem 0.8rem;
    text-align: center;
    color: rgba(155, 176, 208, 0.55);
    font-family: var(--font-numeric);
    font-size: var(--fs-lg);
  }

  /* Mobile: tighten padding so 5 columns fit comfortably on a
     360px viewport without column wrap or horizontal scroll. */
  @media (max-width: 600px) {
    .nav-bd-table          { font-size: var(--fs-sm); }
    .nav-bd-table thead th { font-size: var(--fs-2xs); padding: 0.25rem 0.3rem; }
    .nav-bd-table tbody td { padding: 0.3rem 0.3rem; }
    .nav-bd-caption        { font-size: var(--fs-2xs); }
  }
</style>
