<script>
  // Execution mode dashboard (/admin/execution).
  //
  // Consolidates all five execution modes:
  //   SIM    — fabricated positions, scenario-driven (dev default on)
  //   REPLAY — historical candle backtest
  //   PAPER  — real quotes, paper fills (prod default)
  //   SHADOW — validated via basket_margin, never placed
  //   LIVE   — real broker orders
  //
  // The global `executionMode` store (driven by the navbar combobox) is the
  // single source of truth. On mount, ?mode= in the URL seeds the store via
  // setExecutionMode() so deep-links still work. All sub-sections are rendered
  // inline; switching is an instant repaint with no dynamic imports.

  import { onMount, onDestroy } from 'svelte';
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { authStore, clientTimestamp, visibleInterval, branchLabel, executionMode } from '$lib/stores';
  import {
    fetchPaperStatus, fetchChartSymbols, fetchChartBatch, fetchAlgoOrdersRecent,
    fetchShadowStatus, fetchShadowOrders, promoteShadowToLive, clearShadowData,
    fetchLiveStatus, updateSetting, setExecutionMode,
  } from '$lib/api';
  import LogPanel       from '$lib/LogPanel.svelte';
  import PriceChart     from '$lib/PriceChart.svelte';
  import InfoHint       from '$lib/InfoHint.svelte';
  import SimulatorPanel from '$lib/execution/SimulatorPanel.svelte';
  import ReplayPanel    from '$lib/execution/ReplayPanel.svelte';
  import { priceFmt, qtyFmt } from '$lib/format';

  // ── Mode selection ───────────────────────────────────────────────────
  /** @type {Array<'sim'|'replay'|'paper'|'shadow'|'live'>} */
  const ALL_MODES = ['sim', 'replay', 'paper', 'shadow', 'live'];

  // Mode colours — single source of truth across the algo site lives in
  // LogPanel's .mode-pill-* CSS + the navbar's .algo-mode-* badges.
  // SIM = rose, REPLAY = pos-green, PAPER = sky, SHADOW = short-orange,
  // LIVE = live-emerald. Tailwind defaults (`#ec4899`, `#4ade80`, etc.)
  // do not match the rest of the algo palette — keep these aligned.
  const MODE_META = {
    sim:    { label: 'SIM',    color: '#fb7185', bg: 'rgba(251,113,133,0.12)', border: 'rgba(251,113,133,0.35)' },
    replay: { label: 'REPLAY', color: '#4ade80', bg: 'rgba(74,222,128,0.12)',  border: 'rgba(74,222,128,0.35)'  },
    paper:  { label: 'PAPER',  color: '#7dd3fc', bg: 'rgba(125,211,252,0.12)', border: 'rgba(125,211,252,0.35)' },
    shadow: { label: 'SHADOW', color: '#fb923c', bg: 'rgba(251,146,60,0.12)',  border: 'rgba(251,146,60,0.35)'  },
    live:   { label: 'LIVE',   color: '#6ee7b7', bg: 'rgba(110,231,183,0.12)', border: 'rgba(110,231,183,0.35)' },
  };

  // ── Shared state ─────────────────────────────────────────────────────
  let branch = $state('');   // resolved after first paper-status poll
  let prodBranch = $derived(branch === 'main');

  // Branch-filtered mode list.
  // dev:  sim + replay (+ paper for visibility, gated inside panel)
  // prod: replay + paper + shadow + live
  /** @type {Array<'sim'|'replay'|'paper'|'shadow'|'live'>} */
  const availableModes = $derived(
    prodBranch
      ? /** @type {Array<'replay'|'paper'|'shadow'|'live'>} */ (['replay', 'paper', 'shadow', 'live'])
      : /** @type {Array<'sim'|'replay'|'paper'>} */ (['sim', 'replay', 'paper'])
  );

  let error  = $state('');
  let loading = $state(true);
  let _loadFails = 0;

  // The active mode is driven by the global executionMode store.
  // The local `mode` getter just aliases the store value for template use.
  const mode = $derived($executionMode);

  // On mount, read ?mode= from the URL and push it into the global store so
  // deep-links (/admin/execution?mode=sim) work after the navbar combobox
  // was introduced. This is a one-time sync on navigation; subsequent mode
  // changes go through the navbar combobox → setExecutionMode().
  onMount(async () => {
    const r = $authStore.user?.role;
    if (!$authStore.user || (r !== 'admin' && r !== 'designated')) { goto('/signin'); return; }
    const param = page.url.searchParams.get('mode');
    const valid = /** @type {const} */ (['sim', 'replay', 'paper', 'shadow', 'live']);
    if (param && valid.includes(/** @type {any} */ (param))) {
      try { await setExecutionMode(param); } catch (_) { executionMode.set(/** @type {any} */ (param)); }
    }
    document.addEventListener('click', onDocClick, true);
  });

  /** Push the selected mode to the URL and the global store. */
  async function selectMode(/** @type {string} */ m) {
    if (m === mode) { comboOpen = false; return; }
    comboOpen = false;
    try { await setExecutionMode(m); } catch (_) { executionMode.set(/** @type {any} */ (m)); }
    goto(`/admin/execution?mode=${m}`, { replaceState: false, noScroll: true });
  }

  // ── Combo-box state ──────────────────────────────────────────────────
  let comboOpen = $state(false);
  let comboTrigger = /** @type {HTMLButtonElement|null} */ (null);

  function toggleCombo() { comboOpen = !comboOpen; }

  function onComboKeydown(/** @type {KeyboardEvent} */ e) {
    if (e.key === 'Escape') { comboOpen = false; return; }
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      const idx = availableModes.indexOf(mode);
      const next = availableModes[(idx + 1) % availableModes.length];
      if (next) selectMode(next);
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      const idx = availableModes.indexOf(mode);
      const prev = availableModes[(idx - 1 + availableModes.length) % availableModes.length];
      if (prev) selectMode(prev);
    }
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      comboOpen = !comboOpen;
    }
  }

  // Click-outside closes the combo.
  function onDocClick(/** @type {MouseEvent} */ e) {
    if (!comboOpen) return;
    const target = /** @type {Node} */ (e.target);
    if (!comboTrigger?.closest('.exec-combo')?.contains(target)) {
      comboOpen = false;
    }
  }

  // Per-mode teardown handle — only one mode's poller runs at a time.
  let modeTeardown = /** @type {(() => void)|undefined} */ (undefined);

  // ── PAPER state ──────────────────────────────────────────────────────
  let paperStatus       = $state(/** @type {any} */ ({}));
  let paperOrderRows    = $state(/** @type {any[]} */ ([]));
  let paperChartSymbols = $state(/** @type {string[]} */ ([]));
  /** @type {Array<{symbol:string, kind:string, underlying:string|null}>} */
  let paperChartItems   = $state([]);
  let paperChartsBySymbol = $state(/** @type {Record<string, any>} */ ({}));
  let paperLogTab       = $state('order');

  async function loadPaper() {
    try {
      const [stat, syms, rows] = await Promise.all([
        fetchPaperStatus(),
        fetchChartSymbols('paper').catch(() => ({ items: [], symbols: [] })),
        fetchAlgoOrdersRecent(100, 'paper').catch(() => []),
      ]);
      paperStatus       = stat;
      paperChartSymbols = syms?.symbols || [];
      paperChartItems   = syms?.items   || [];
      paperOrderRows    = rows || [];
      branch = stat?.branch || branch;
      if (paperChartSymbols.length) {
        try {
          const batch = await fetchChartBatch('paper', paperChartSymbols);
          const map = /** @type {Record<string, any>} */ ({});
          for (const c of (batch?.charts || [])) map[c.symbol] = c;
          paperChartsBySymbol = map;
        } catch (_) { /* charts fall back to self-poll */ }
      } else {
        paperChartsBySymbol = {};
      }
      error = '';
      _loadFails = 0;
    } catch (e) {
      _loadFails++;
      if (!branch && _loadFails >= 2) error = e.message;
    } finally {
      loading = false;
    }
  }

  // ── SHADOW state ─────────────────────────────────────────────────────
  let shadowStatus   = $state(/** @type {any} */ ({}));
  let shadowOrders   = $state(/** @type {any[]} */ ([]));
  let shadowPromoting = $state(false);
  let shadowExpandId = $state(/** @type {number|null} */ (null));

  async function loadShadow() {
    try {
      const [stat, ord] = await Promise.all([
        fetchShadowStatus(),
        fetchShadowOrders(50).catch(() => []),
      ]);
      shadowStatus = stat;
      shadowOrders = ord || [];
      branch = stat?.branch || branch;
      error  = '';
      _loadFails = 0;
    } catch (e) {
      _loadFails++;
      if (!branch && _loadFails >= 2) error = e.message;
    } finally {
      loading = false;
    }
  }

  async function handlePromote() {
    if (!confirm(
      'This will enable ALL live execution flags and disable shadow mode.\n\n' +
      'Real broker orders will be placed. Are you sure?'
    )) return;
    shadowPromoting = true;
    try {
      const result = await promoteShadowToLive();
      alert('Promoted to live:\n' + (result?.promoted || []).join('\n'));
      await loadShadow();
    } catch (e) {
      error = e.message;
    } finally {
      shadowPromoting = false;
    }
  }

  async function handleClearShadow() {
    if (!confirm('Delete all shadow orders?')) return;
    try {
      await clearShadowData();
      await loadShadow();
    } catch (e) { error = e.message; }
  }

  function parsePayload(detail) {
    if (!detail) return null;
    const marker = '--- KITE PAYLOAD ---';
    const idx = detail.indexOf(marker);
    if (idx < 0) return null;
    try { return JSON.parse(detail.slice(idx + marker.length)); }
    catch { return null; }
  }

  const shadowEnabled    = $derived(shadowStatus?.enabled !== false);
  const shadowActive     = $derived(shadowStatus?.shadow_active === true);
  const shadowBranchLabel = $derived(branchLabel(shadowStatus?.branch || ''));

  // ── LIVE state ───────────────────────────────────────────────────────
  let liveStatus        = $state(/** @type {any} */ ({}));
  let liveOrders        = $state(/** @type {any[]} */ ([]));
  let liveChartSymbols  = $state(/** @type {string[]} */ ([]));
  /** @type {Array<{symbol:string, kind:string, underlying:string|null}>} */
  let liveChartItems    = $state([]);
  let liveChartsBySymbol = $state(/** @type {Record<string, any>} */ ({}));
  let liveSaving        = $state(false);
  let liveNote          = $state('');

  async function loadLive() {
    try {
      const [stat, ord, syms] = await Promise.all([
        fetchLiveStatus(),
        fetchAlgoOrdersRecent(50, 'live').catch(() => []),
        fetchChartSymbols('live').catch(() => ({ items: [], symbols: [] })),
      ]);
      liveStatus       = stat;
      liveOrders       = ord || [];
      liveChartSymbols = syms?.symbols || [];
      liveChartItems   = syms?.items   || [];
      branch = stat?.branch || branch;
      if (liveChartSymbols.length) {
        try {
          const batch = await fetchChartBatch('live', liveChartSymbols);
          const map = /** @type {Record<string, any>} */ ({});
          for (const c of (batch?.charts || [])) map[c.symbol] = c;
          liveChartsBySymbol = map;
        } catch (_) { /* charts fall back to per-chart self-poll */ }
      } else {
        liveChartsBySymbol = {};
      }
      error = '';
      _loadFails = 0;
    } catch (e) {
      _loadFails++;
      if (!branch && _loadFails >= 2) error = e.message;
    } finally {
      loading = false;
    }
  }

  const liveEnabled   = $derived(liveStatus?.enabled !== false);
  const liveBranchLabel = $derived(branchLabel(liveStatus?.branch || ''));
  const isPaper       = $derived(liveStatus?.paper_trading_mode !== false);

  const effectiveLabel = $derived({
    dev_paper: 'DEV PAPER',
    paper:     'PAPER',
    shadow:    'SHADOW',
    live:      'LIVE',
    mixed:     'MIXED',
  }[liveStatus?.effective_mode] || 'UNKNOWN');

  const effectiveColor = $derived({
    dev_paper: '#94a3b8',
    paper:     '#38bdf8',
    shadow:    '#fb923c',
    live:      '#4ade80',
    mixed:     '#fbbf24',
  }[liveStatus?.effective_mode] || '#94a3b8');

  // Toggle flips paper_trading_mode immediately on click.
  // Backend `is_prod_branch()` is the hard outer gate — even with the
  // toggle flipped to LIVE on dev, _resolve_mode() forces every action
  // to paper. So removing the UI-side confirmation modal and the dev
  // branch lockout doesn't loosen any real safety, just removes friction.
  function handleToggleClick() {
    commitToggle(!isPaper);
  }

  async function commitToggle(/** @type {boolean} */ newPaperMode) {
    liveSaving = true;
    error      = '';
    liveNote   = '';
    try {
      await updateSetting('execution.paper_trading_mode', newPaperMode);
      await loadLive();
      liveNote = newPaperMode ? 'Switched to PAPER mode.' : 'Switched to LIVE mode.';
      setTimeout(() => { liveNote = ''; }, 3000);
    } catch (e) {
      error = e.message;
    } finally {
      liveSaving = false;
    }
  }

  // ── Polling lifecycle ─────────────────────────────────────────────────
  // sim and replay panels own their own internal pollers; we only drive
  // paper / shadow / live from here.
  const POLL_INTERVALS = { paper: 5000, shadow: 5000, live: 5000 };
  const LOAD_FNS = /** @type {Record<string, () => Promise<void>>} */ ({
    paper: loadPaper, shadow: loadShadow, live: loadLive,
  });

  $effect(() => {
    const currentMode = mode;
    modeTeardown?.();
    modeTeardown = undefined;
    const fn = LOAD_FNS[currentMode];
    if (fn) {
      loading = true;
      _loadFails = 0;
      error = '';
      fn();
      modeTeardown = visibleInterval(fn, POLL_INTERVALS[currentMode]);
    }
    return () => {
      modeTeardown?.();
      modeTeardown = undefined;
    };
  });

  // onMount is defined above (handles ?mode= URL seed + docClick listener).
  onDestroy(() => {
    modeTeardown?.();
    document.removeEventListener('click', onDocClick, true);
  });

  const meta = $derived(MODE_META[mode]);
</script>

<svelte:head><title>Execution | RamboQuant Analytics</title></svelte:head>

<!-- Page header with custom combo-box mode selector -->
<div class="page-header">
  <div class="exec-header-left">
    <!-- Combo-box trigger -->
    <div class="exec-combo" class:exec-combo-open={comboOpen}>
      <button
        bind:this={comboTrigger}
        type="button"
        class="exec-combo-trigger"
        onclick={toggleCombo}
        onkeydown={onComboKeydown}
        aria-haspopup="listbox"
        aria-expanded={comboOpen}
        aria-controls="exec-mode-list"
        aria-label="Select execution mode: {meta.label}"
      >
        <span class="exec-combo-word">EXECUTION</span>
        <svg class="exec-combo-chevron" class:rotated={comboOpen}
             viewBox="0 0 16 16" fill="none" width="10" height="10">
          <path d="M4 6l4 4 4-4" stroke="currentColor" stroke-width="1.5"
                stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </button>

      <!-- Mode pill showing current selection -->
      <span class="exec-mode-pill"
            style="color:{meta.color}; background:{meta.bg}; border-color:{meta.border}">
        {meta.label}
      </span>

      <!-- Dropdown panel -->
      {#if comboOpen}
        <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
        <div class="exec-dropdown" role="listbox" id="exec-mode-list"
             aria-label="Execution modes">
          {#each availableModes as m}
            {@const mmeta = MODE_META[m]}
            {@const isSelected = m === mode}
            <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
            <div
              class="exec-option"
              class:exec-option-selected={isSelected}
              role="option"
              tabindex="0"
              aria-selected={isSelected}
              onclick={() => selectMode(m)}
            >
              <span class="exec-opt-pill"
                    style="color:{mmeta.color}; background:{mmeta.bg}; border-color:{mmeta.border}">
                {mmeta.label}
              </span>
              <span class="exec-opt-desc">
                {#if m === 'sim'}Fabricated positions · scenario-driven{/if}
                {#if m === 'replay'}Historical candles · backtest{/if}
                {#if m === 'paper'}Real quotes · paper fills{/if}
                {#if m === 'shadow'}Validated · never executed{/if}
                {#if m === 'live'}Real broker orders{/if}
              </span>
              {#if isSelected}
                <span class="exec-opt-check" aria-hidden="true">✓</span>
              {/if}
            </div>
          {/each}
        </div>
      {/if}
    </div>

    <InfoHint popup text="All five execution modes in one place. <b>SIM</b> — fabricated positions, scenario-driven; exercises the full agent engine without touching the broker. <b>REPLAY</b> — historical Kite OHLCV candles fed through the agent engine at accelerated speed. <b>PAPER</b> — real Kite quotes, paper fills; monitor open chase orders and charts. <b>SHADOW</b> — orders validated via basket_margin but never placed; promote to live when ready. <b>LIVE</b> — master toggle that switches the whole engine to real Kite orders." />
  </div>

  <span class="algo-ts">{clientTimestamp()}</span>
</div>

{#if error}
  <div class="mb-3 p-2 rounded bg-red-500/15 text-red-300 text-[0.65rem] border border-red-500/40">{error}</div>
{/if}

<!-- ═══════════════════════════════════════════════════════════════════
     SIM MODE
     ═══════════════════════════════════════════════════════════════════ -->
{#if mode === 'sim'}
  <SimulatorPanel />
{/if}

<!-- ═══════════════════════════════════════════════════════════════════
     REPLAY MODE
     ═══════════════════════════════════════════════════════════════════ -->
{#if mode === 'replay'}
  <ReplayPanel />
{/if}

<!-- ═══════════════════════════════════════════════════════════════════
     PAPER MODE
     ═══════════════════════════════════════════════════════════════════ -->
{#if mode === 'paper'}
  <!-- Status banner -->
  <div class="paper-banner {paperStatus?.enabled ? 'banner-active' : 'banner-disabled'}"
       data-status={paperStatus?.enabled ? 'active' : 'inactive'}>
    {#if !paperStatus?.enabled}
      <span class="paper-banner-tag">DEV</span>
      <span>
        Paper engine is gated on this branch (<span class="font-mono">{branchLabel(paperStatus?.branch) || '?'}</span>).
        No tick_loop is running. Promote to <span class="font-mono">prod</span> to see live paper activity.
      </span>
    {:else if (paperStatus?.open_order_count ?? 0) > 0}
      <span class="paper-banner-tag tag-active">CHASING</span>
      <span>
        <b>{paperStatus.open_order_count}</b>
        open paper order{paperStatus.open_order_count === 1 ? '' : 's'} on
        <span class="font-mono">{branchLabel(paperStatus.branch)}</span> ·
        {paperStatus.captured_symbols.length} symbol{paperStatus.captured_symbols.length === 1 ? '' : 's'}
        tracked, {paperStatus.captured_underlyings.length} underlying{paperStatus.captured_underlyings.length === 1 ? '' : 's'}
      </span>
    {:else}
      <span class="paper-banner-tag tag-idle">IDLE</span>
      <span>
        Paper engine is enabled on <span class="font-mono">{branchLabel(paperStatus?.branch)}</span>
        but no orders are currently in flight. Charts populate as soon as an agent fires a broker action.
      </span>
    {/if}
  </div>

  <!-- Open-order pills -->
  {#if paperStatus?.open_order_details?.length}
    <div class="paper-pills mb-3">
      <span class="paper-pills-label">Chasing ({paperStatus.open_order_details.length}):</span>
      {#each paperStatus.open_order_details as o}
        <span class="paper-pill">
          <span class="paper-pill-side paper-pill-side-{o.side === 'BUY' ? 'buy' : 'sell'}">{o.side}</span>
          <span class="paper-pill-sym">{o.symbol}</span>
          <span class="paper-pill-qty">{qtyFmt(o.qty)}</span>
          <span class="paper-pill-limit">@₹{priceFmt(o.limit_price)}</span>
          <span class="paper-pill-attempts">#{o.attempts}</span>
        </span>
      {/each}
    </div>
  {/if}

  <!-- Chart grid -->
  {#if paperChartSymbols.length}
    <div class="exec-charts mb-3">
      {#each paperChartSymbols as sym (sym)}
        <PriceChart mode="paper" symbol={sym} height={170}
                    data={paperChartsBySymbol[sym]}
                    chartsBySymbol={paperChartsBySymbol} />
      {/each}
    </div>
  {:else if !loading && paperStatus?.enabled}
    <div class="text-[0.65rem] text-[#7e97b8] mb-3 italic">
      No symbols with captured ticks yet. Charts populate as soon as the chase loop sees its first quote.
    </div>
  {/if}

  <LogPanel
    heightClass="h-[40vh]"
    defaultTab={paperLogTab}
    onTabChange={(id) => { paperLogTab = id; }}
  />
{/if}


<!-- ═══════════════════════════════════════════════════════════════════
     SHADOW MODE
     ═══════════════════════════════════════════════════════════════════ -->
{#if mode === 'shadow'}
  <!-- Branch gate -->
  {#if !shadowEnabled}
    <div class="sim-banner sim-banner-warn">
      Shadow mode is only available on <strong>prod</strong>. Current branch: <strong>{shadowBranchLabel}</strong>.
    </div>
  {/if}

  <!-- Status + controls card -->
  <div class="sim-controls">
    <div class="shadow-status-row">
      <div class="shadow-stat">
        <span class="sim-label">Branch</span>
        <span class="shadow-val">{shadowBranchLabel}</span>
      </div>
      <div class="shadow-stat">
        <span class="sim-label">Shadow orders</span>
        <span class="shadow-val">{shadowStatus?.order_count ?? 0}</span>
      </div>
      <div class="shadow-stat">
        <span class="sim-label">Status</span>
        <span class="shadow-val" class:shadow-on={shadowActive} class:shadow-off={!shadowActive}>
          {shadowActive ? 'Active' : 'Inactive'}
        </span>
      </div>
    </div>
    <div class="sim-btn-row">
      <button class="sim-btn sim-btn-promote" onclick={handlePromote}
              disabled={!shadowEnabled || shadowPromoting || !shadowActive}>
        {shadowPromoting ? 'Promoting…' : 'Promote to Live'}
      </button>
      <button class="sim-btn sim-btn-clear" onclick={handleClearShadow}>Clear</button>
    </div>
    {#if shadowEnabled && !shadowActive}
      <p class="shadow-hint">
        Enable shadow mode in <a href="/admin/settings">Settings</a> → <code>execution.shadow_mode</code>
      </p>
    {/if}
  </div>

  <!-- Shadow orders table -->
  {#if shadowOrders.length > 0}
    <section class="sim-section">
      <h3>Shadow Orders</h3>
      <div class="sim-table-wrap">
        <table class="sim-table">
          <thead>
            <tr><th>ID</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Price</th><th>Status</th><th>Time</th><th></th></tr>
          </thead>
          <tbody>
            {#each shadowOrders as o}
              <tr>
                <td>{o.id}</td>
                <td>{o.symbol}</td>
                <td class={o.side === 'BUY' ? 'sim-buy' : 'sim-sell'}>{o.side}</td>
                <td>{qtyFmt(o.quantity)}</td>
                <td class="sim-td-mono">{o.initial_price != null ? `₹${priceFmt(o.initial_price)}` : '—'}</td>
                <td>
                  <span class="sim-pill"
                        class:sim-pill-ok={o.status === 'SHADOW_OK'}
                        class:sim-pill-rej={o.status === 'SHADOW_REJECTED'}>
                    {o.status === 'SHADOW_OK' ? 'OK' : o.status === 'SHADOW_REJECTED' ? 'REJECTED' : o.status}
                  </span>
                </td>
                <td class="sim-td-mono">{o.created_at ? new Date(o.created_at).toLocaleString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false, timeZone: 'Asia/Kolkata' }) + ' IST' : '—'}</td>
                <td>
                  <button class="sim-btn-xs"
                          onclick={() => shadowExpandId = shadowExpandId === o.id ? null : o.id}>
                    {shadowExpandId === o.id ? '▾' : '▸'} Payload
                  </button>
                </td>
              </tr>
              {#if shadowExpandId === o.id}
                <tr class="shadow-detail-row">
                  <td colspan="8">
                    <pre class="shadow-payload">{JSON.stringify(parsePayload(o.detail), null, 2) || o.detail}</pre>
                  </td>
                </tr>
              {/if}
            {/each}
          </tbody>
        </table>
      </div>
    </section>
  {:else if !loading}
    <p class="sim-empty">No shadow orders yet.</p>
  {/if}
{/if}


<!-- ═══════════════════════════════════════════════════════════════════
     LIVE MODE
     ═══════════════════════════════════════════════════════════════════ -->
{#if mode === 'live'}
  <!-- Safety banner -->
  <div class="exec-banner {isPaper ? 'exec-banner-safe' : 'exec-banner-live'}">
    {#if isPaper}
      Every broker action is in <strong>PAPER</strong> mode — no real orders will hit the broker.
    {:else}
      <span class="exec-warn-icon">⚠</span>
      <strong>Live mode</strong> — real Kite orders fire on every agent rule and operator ticket submit.
    {/if}
  </div>

  {#if !liveEnabled}
    <div class="sim-banner sim-banner-warn">
      Live execution is only available on <strong>prod</strong>. Current branch: <strong>{liveBranchLabel}</strong>.
      All actions are paper on dev.
    </div>
  {/if}

  {#if liveNote}
    <div class="sim-banner sim-banner-note">{liveNote}</div>
  {/if}

  <!-- Master toggle card -->
  <div class="sim-controls">
    <div class="live-grid">
      <div class="live-stat">
        <span class="sim-label">Branch</span>
        <span class="live-val">{liveBranchLabel}</span>
      </div>
      <div class="live-stat">
        <span class="sim-label">Shadow mode</span>
        <span class="live-val" class:shadow-on={liveStatus?.shadow_mode}>
          {liveStatus?.shadow_mode ? 'ON (orders → shadow log)' : 'OFF'}
        </span>
      </div>
      <div class="live-stat">
        <span class="sim-label">Effective mode</span>
        <span class="live-val" style="color: {effectiveColor}">{effectiveLabel}</span>
      </div>
    </div>

    <!-- Hero toggle -->
    <div class="master-toggle-wrap">
      <div class="master-toggle-label">
        <span class="master-toggle-title">Paper trading mode</span>
        <span class="master-toggle-sub {isPaper ? 'sub-paper' : 'sub-live'}">
          {#if isPaper}
            <span class="sub-icon">✓</span> PAPER — all actions safe
          {:else}
            <span class="sub-icon">⚠</span> LIVE — real broker orders
          {/if}
        </span>
      </div>
      <button
        type="button"
        class="master-toggle-btn"
        class:toggle-paper={isPaper}
        class:toggle-live={!isPaper}
        disabled={liveSaving}
        onclick={handleToggleClick}
        title={isPaper ? 'Click to switch to LIVE mode' : 'Click to switch back to PAPER mode'}
      >
        {#if liveSaving}
          <span class="master-dot master-dot-saving"></span>
        {:else}
          <span class="master-dot" class:dot-paper={isPaper} class:dot-live={!isPaper}></span>
        {/if}
      </button>
    </div>
  </div>

<!-- Recent live orders table -->
  {#if liveOrders.length > 0}
    <section class="sim-section">
      <h3>Recent Live Orders ({liveOrders.length})</h3>
      <div class="sim-table-wrap">
        <table class="sim-table">
          <thead>
            <tr><th>ID</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Price</th><th>Status</th><th>Broker ID</th><th>Time</th></tr>
          </thead>
          <tbody>
            {#each liveOrders as o}
              <tr>
                <td>{o.id}</td>
                <td>{o.symbol}</td>
                <td class={o.transaction_type === 'BUY' ? 'sim-buy' : 'sim-sell'}>{o.transaction_type}</td>
                <td>{qtyFmt(o.quantity)}</td>
                <td class="sim-td-mono">{o.initial_price != null ? `₹${priceFmt(o.initial_price)}` : '—'}</td>
                <td><span class="sim-pill sim-pill-live">{o.status}</span></td>
                <td class="sim-td-mono">{o.broker_order_id || '—'}</td>
                <td class="sim-td-mono">{o.created_at ? new Date(o.created_at).toLocaleString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false, timeZone: 'Asia/Kolkata' }) + ' IST' : '—'}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </section>
  {:else if !loading}
    <p class="sim-empty">No live orders yet.</p>
  {/if}

  <!-- Chart grid -->
  {#if liveChartSymbols.length}
    <div class="exec-charts mb-3">
      {#each liveChartSymbols as sym (sym)}
        <PriceChart mode="live" symbol={sym} height={170}
                    data={liveChartsBySymbol[sym]}
                    chartsBySymbol={liveChartsBySymbol} />
      {/each}
    </div>
  {:else if !loading}
    <div class="sim-empty-charts">
      No symbols with captured ticks yet. Charts populate when live orders are placed
      and the chase loop records quotes.
    </div>
  {/if}

  <LogPanel
    heightClass="h-[40vh]"
    defaultTab="order"
  />
{/if}

<style>
  /* ── Combo box ──────────────────────────────────────────────────────── */
  .exec-header-left {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
  }

  .exec-combo {
    position: relative;
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
  }

  .exec-combo-trigger {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    padding: 0.2rem 0.55rem;
    border-radius: 0.25rem;
    background: rgba(15,23,42,0.7);
    border: 1px solid rgba(125,211,252,0.25);
    color: #7dd3fc;
    font-family: ui-monospace, monospace;
    font-size: 0.68rem;
    font-weight: 800;
    letter-spacing: 0.1em;
    cursor: pointer;
    transition: border-color 0.1s, background 0.1s;
    outline: none;
  }
  .exec-combo-trigger:hover,
  .exec-combo-open .exec-combo-trigger {
    border-color: rgba(125,211,252,0.55);
    background: rgba(125,211,252,0.08);
  }
  .exec-combo-trigger:focus-visible {
    outline: 2px solid #7dd3fc;
    outline-offset: 2px;
  }

  .exec-combo-word {
    letter-spacing: 0.1em;
    font-weight: 800;
  }

  .exec-combo-chevron {
    color: rgba(125,211,252,0.7);
    transition: transform 0.15s;
    flex-shrink: 0;
  }
  .exec-combo-chevron.rotated { transform: rotate(180deg); }

  /* Current-mode pill next to the trigger */
  .exec-mode-pill {
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    padding: 0.15rem 0.5rem;
    border-radius: 9999px;
    border: 1px solid;
    pointer-events: none;
  }

  /* Dropdown panel */
  .exec-dropdown {
    position: absolute;
    top: calc(100% + 4px);
    left: 0;
    z-index: 100;
    min-width: 14rem;
    background: #0d1829;
    border: 1px solid rgba(125,211,252,0.22);
    border-radius: 0.375rem;
    box-shadow: 0 8px 24px rgba(0,0,0,0.55);
    overflow: hidden;
  }

  .exec-option {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.45rem 0.65rem;
    cursor: pointer;
    transition: background 0.08s;
    border-bottom: 1px solid rgba(148,163,184,0.06);
    user-select: none;
  }
  .exec-option:last-child { border-bottom: none; }
  .exec-option:hover { background: rgba(125,211,252,0.07); }
  .exec-option-selected { background: rgba(125,211,252,0.05); }

  .exec-opt-pill {
    font-family: ui-monospace, monospace;
    font-size: 0.58rem;
    font-weight: 700;
    letter-spacing: 0.07em;
    padding: 0.1rem 0.45rem;
    border-radius: 9999px;
    border: 1px solid;
    flex-shrink: 0;
  }

  .exec-opt-desc {
    font-size: 0.65rem;
    color: #7e97b8;
    flex: 1;
  }

  .exec-opt-check {
    font-size: 0.65rem;
    color: #fbbf24;
    margin-left: auto;
  }

  /* (Dev-branch gate banner CSS removed with the guard.) */

  /* ── PAPER sub-section ──────────────────────────────────────────────── */
  .paper-banner {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.5rem 0.75rem;
    border-radius: 4px;
    border: 1px solid rgba(251,191,36,0.2);
    border-left: 3px solid #fbbf24;
    background: linear-gradient(180deg, #1d2a44 0%, #152033 100%);
    margin-bottom: 0.65rem;
    font-size: 0.65rem;
    color: #c8d8f0;
  }
  .paper-banner.banner-disabled {
    border-color: rgba(255,255,255,0.10);
    border-left-color: rgba(255,255,255,0.25);
    color: #7e97b8;
  }
  .paper-banner-tag {
    font-family: monospace;
    font-size: 0.55rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    padding: 1px 6px;
    border-radius: 2px;
    border: 1px solid currentColor;
    color: #7e97b8;
  }
  .paper-banner-tag.tag-active {
    color: #7dd3fc;
    background: rgba(125,211,252,0.10);
  }
  .paper-banner-tag.tag-idle {
    color: #fbbf24;
    background: rgba(251,191,36,0.10);
  }

  .paper-pills {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
    align-items: center;
  }
  .paper-pills-label {
    font-family: monospace;
    font-size: 0.6rem;
    color: #7e97b8;
    margin-right: 0.25rem;
  }
  .paper-pill {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    padding: 0.15rem 0.5rem;
    border-radius: 3px;
    border: 1px solid rgba(125,211,252,0.3);
    background: rgba(125,211,252,0.08);
    font-family: monospace;
    font-size: 0.6rem;
  }
  .paper-pill-side {
    font-weight: 700;
    padding: 0 0.25rem;
    border-radius: 2px;
    font-size: 0.55rem;
  }
  .paper-pill-side-buy  { color: #4ade80; background: rgba(74,222,128,0.15); }
  .paper-pill-side-sell { color: #f87171; background: rgba(248,113,113,0.15); }
  .paper-pill-sym  { color: #c8d8f0; }
  .paper-pill-qty  { color: #fbbf24; font-weight: 700; }
  .paper-pill-limit { color: #7dd3fc; }
  .paper-pill-attempts {
    color: #fbbf24;
    font-weight: 700;
    border-left: 1px solid rgba(251,191,36,0.35);
    padding-left: 0.35rem;
    margin-left: 0.1rem;
  }

  /* Shared chart grid for paper + live */
  .exec-charts {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
    gap: 0.5rem;
  }

  /* ── Shared sim/shadow/live table styles ────────────────────────────── */
  .sim-controls {
    background: rgba(15,23,42,0.6);
    border: 1px solid rgba(148,163,184,0.12);
    border-radius: 0.5rem;
    padding: 1rem;
    margin-bottom: 1rem;
  }
  .shadow-status-row { display: flex; gap: 2rem; margin-bottom: 0.75rem; flex-wrap: wrap; }
  .shadow-stat       { display: flex; flex-direction: column; gap: 0.15rem; }
  .sim-label         { font-size: 0.6rem; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.04em; }
  .shadow-val        { font-size: 0.85rem; color: #c8d8f0; font-weight: 600; }
  .shadow-on         { color: #fb923c !important; }
  .shadow-off        { color: #94a3b8 !important; }
  .shadow-hint       { font-size: 0.7rem; color: #64748b; margin-top: 0.5rem; }
  .shadow-hint a     { color: #38bdf8; text-decoration: underline; }
  .shadow-hint code  { background: rgba(148,163,184,0.12); padding: 0.1rem 0.3rem; border-radius: 0.2rem; font-size: 0.65rem; }

  .sim-btn-row { display: flex; gap: 0.5rem; }
  .sim-btn {
    padding: 0.4rem 1rem;
    border-radius: 0.375rem;
    font-size: 0.75rem;
    font-weight: 600;
    cursor: pointer;
    border: 1px solid transparent;
    transition: all 0.15s;
  }
  .sim-btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .sim-btn-promote   { background: rgba(248,113,113,0.15); color: #f87171; border-color: rgba(248,113,113,0.3); }
  .sim-btn-promote:hover:not(:disabled) { background: rgba(248,113,113,0.25); }
  .sim-btn-clear     { background: rgba(148,163,184,0.10); color: #94a3b8; border-color: rgba(148,163,184,0.2); }
  .sim-btn-xs {
    font-size: 0.6rem;
    padding: 0.15rem 0.4rem;
    background: rgba(148,163,184,0.08);
    color: #94a3b8;
    border: 1px solid rgba(148,163,184,0.15);
    border-radius: 0.25rem;
    cursor: pointer;
  }

  .sim-section        { margin-bottom: 1.5rem; }
  .sim-section h3     { font-size: 0.85rem; font-weight: 600; color: #c8d8f0; margin-bottom: 0.5rem; }
  .sim-table-wrap     { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  .sim-table          { width: 100%; border-collapse: collapse; font-size: 0.72rem; }
  .sim-table th       { text-align: left; padding: 0.35rem 0.5rem; color: #94a3b8; border-bottom: 1px solid rgba(148,163,184,0.15); font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; font-size: 0.6rem; }
  .sim-table td       { padding: 0.3rem 0.5rem; color: #c8d8f0; border-bottom: 1px solid rgba(148,163,184,0.06); }
  .sim-td-mono        { font-family: 'JetBrains Mono', monospace; font-size: 0.65rem; }
  .sim-buy            { color: #38bdf8; }
  .sim-sell           { color: #fb923c; }
  .sim-pill           { font-size: 0.6rem; font-weight: 700; padding: 0.1rem 0.4rem; border-radius: 9999px; }
  .sim-pill-ok        { color: #4ade80; background: rgba(74,222,128,0.12); }
  .sim-pill-rej       { color: #f87171; background: rgba(248,113,113,0.12); }
  .sim-pill-live      { color: #4ade80; background: rgba(74,222,128,0.12); }
  .sim-empty          { font-size: 0.75rem; color: #64748b; text-align: center; padding: 2rem; }
  .sim-empty-charts   { font-size: 0.65rem; color: #64748b; font-style: italic; margin-bottom: 0.75rem; }

  .shadow-detail-row td { padding: 0; background: rgba(15,23,42,0.8); }
  .shadow-payload {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.65rem;
    color: #94a3b8;
    padding: 0.75rem 1rem;
    margin: 0;
    white-space: pre-wrap;
    line-height: 1.5;
  }

  .sim-banner        { padding: 0.5rem 0.75rem; border-radius: 0.375rem; font-size: 0.75rem; margin-bottom: 0.75rem; }
  .sim-banner-warn   { background: rgba(251,191,36,0.10); color: #fbbf24; border: 1px solid rgba(251,191,36,0.20); }
  .sim-banner-note   { background: rgba(74,222,128,0.08); color: #4ade80; border: 1px solid rgba(74,222,128,0.25); }

  /* ── LIVE sub-section ───────────────────────────────────────────────── */
  .exec-banner {
    padding: 0.5rem 0.75rem;
    border-radius: 0.375rem;
    font-size: 0.72rem;
    margin-bottom: 0.75rem;
    border: 1px solid;
    display: flex;
    align-items: center;
    gap: 0.35rem;
    flex-wrap: wrap;
  }
  .exec-banner-safe { background: rgba(74,222,128,0.08); color: #4ade80; border-color: rgba(74,222,128,0.25); }
  .exec-banner-live { background: rgba(248,113,113,0.10); color: #f87171; border-color: rgba(248,113,113,0.30); }
  .exec-warn-icon   { font-style: normal; font-weight: 700; }

  .live-grid  { display: flex; gap: 2rem; margin-bottom: 1.25rem; flex-wrap: wrap; }
  .live-stat  { display: flex; flex-direction: column; gap: 0.15rem; }
  .live-val   { font-size: 0.85rem; color: #c8d8f0; font-weight: 600; }

  /* Hero master toggle */
  .master-toggle-wrap {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.75rem 1rem;
    background: rgba(15,23,42,0.5);
    border-radius: 0.5rem;
    border: 1px solid rgba(148,163,184,0.12);
  }
  .master-toggle-label { display: flex; flex-direction: column; gap: 0.3rem; }
  .master-toggle-title { font-size: 0.85rem; font-weight: 700; color: #c8d8f0; letter-spacing: 0.02em; }
  .master-toggle-sub   { font-size: 0.72rem; font-weight: 600; display: flex; align-items: center; gap: 0.25rem; }
  .sub-paper  { color: #4ade80; }
  .sub-live   { color: #f87171; }
  .sub-icon   { font-style: normal; }

  .master-toggle-btn {
    position: relative;
    width: 3.25rem;
    height: 1.75rem;
    border-radius: 9999px;
    border: 2px solid;
    cursor: pointer;
    transition: background 0.2s, border-color 0.2s;
    padding: 0;
    flex-shrink: 0;
  }
  .master-toggle-btn.toggle-paper { background: rgba(56,189,248,0.12); border-color: rgba(56,189,248,0.35); }
  .master-toggle-btn.toggle-live  { background: rgba(248,113,113,0.18); border-color: rgba(248,113,113,0.50); }
  .master-toggle-btn:disabled     { opacity: 0.40; cursor: not-allowed; }

  .master-dot {
    position: absolute;
    top: 50%;
    transform: translateY(-50%);
    width: 1.1rem;
    height: 1.1rem;
    border-radius: 9999px;
    transition: left 0.2s, background 0.2s;
  }
  /* Dots use the canonical PAPER (sky #7dd3fc) and LIVE (emerald
     #6ee7b7) palette tokens — the danger affordance for LIVE comes
     from the confirm-modal flow, not by overloading the dot with
     error-red which conflicts with LIVE = emerald everywhere else. */
  .master-dot.dot-paper  { left: 3px; background: #7dd3fc; box-shadow: 0 0 8px rgba(125,211,252,0.6); }
  .master-dot.dot-live   { left: calc(100% - 1.1rem - 3px); background: #6ee7b7; box-shadow: 0 0 8px rgba(110,231,183,0.6); }
  .master-dot-saving {
    left: 50%;
    transform: translate(-50%, -50%);
    background: #fbbf24;
    animation: pulse-dot 0.7s ease-in-out infinite alternate;
  }
  @keyframes pulse-dot { from { opacity: 0.4; } to { opacity: 1.0; } }

  /* (Confirmation modal CSS lived here — removed with the LIVE-typed prompt.) */

  /* Mobile */
  @media (max-width: 640px) {
    .exec-combo-trigger { font-size: 0.62rem; padding: 0.18rem 0.4rem; }
    .exec-dropdown      { min-width: 12rem; }
    .sim-controls       { padding: 0.75rem; }
    .shadow-status-row  { gap: 1rem; }
    .live-grid          { gap: 1rem; }
    .master-toggle-wrap { flex-direction: column; align-items: flex-start; gap: 0.75rem; }
    .exec-charts        { grid-template-columns: 1fr; }
    .sim-table td,
    .sim-table th       { padding: 0.25rem 0.35rem; font-size: 0.65rem; }
    .shadow-payload     { font-size: 0.6rem; padding: 0.5rem 0.6rem; }
  }
</style>
