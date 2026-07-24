<script>
  // CandidateLegRow — renders one row inside the Candidates / Legs grid.
  // Extracted from +page.svelte (Phase 3 P2 refactor). CSS lives in the
  // parent (cand-grid subgrid context) so all cand-* classes are applied
  // as plain strings; no scoped styles here.
  //
  // Props:
  //   c              — candidate leg object
  //   ci             — iteration index from the parent {#each}
  //   prevBand       — band value of the previous row (null if first)
  //   bandCount      — count of rows sharing c._band + c._segment (for band header)
  //   legsTab        — 'legs' | 'expiry'
  //   legAnalytics   — analytics object for c.symbol (legAnalyticsBySymbol[c.symbol])
  //   enabled        — result of _isLegEnabled(c) computed by parent
  //   dayPnl         — _dayPnlForLeg(c, liveSpot) precomputed by parent
  //   expPnl         — _expiryPnl(c, liveSpot) precomputed by parent
  //   legExpired     — _isLegExpired(c) boolean precomputed by parent
  //   strategy       — current strategy object (for proxy spot math)
  //   flash          — createTickFlash() instance from parent
  //   onToggleEnabled(c, checked) — parent handles enKey keying + _persistEqMemory
  //   onExecuteDraft(c)
  //   onClosePosition(c)
  //   onRemoveDraft(draftId)
  //   onOpenChartTicket(c)  — fired for non-actionable rows (opens Chart tab)
  //   onContextMenu(c, ev)  — parent sets _ctxMenu from right-click / long-press

  import { rootOfLabel }            from '$lib/data/rootOf.js';
  import { formatSymbol }           from '$lib/data/decomposeSymbol';
  import { decomposeSymbol }        from '$lib/data/decomposeSymbol';
  import { getInstrument, getOptionUnderlyingLot } from '$lib/data/instruments';
  import { acctColor }              from '$lib/account';
  import { lotsForRow, fmtLots }    from '$lib/data/lotsForRow';
  import { getProxyRow }            from '$lib/data/hedgeProxies';
  import { priceFmt, pctFmt, aggCompact } from '$lib/format';
  import { longPress }              from '$lib/actions/longPress.js';

  const BAND_LABELS = { close: 'ITM ON EXPIRY', netted: 'NETTED', otm: 'OUT OF THE MONEY' };

  /**
   * @type {{
   *   c: any,
   *   ci: number,
   *   prevBand: string | null,
   *   bandCount: number,
   *   legsTab: string,
   *   legAnalytics: any,
   *   enabled: boolean,
   *   dayPnl: number | null,
   *   expPnl: number | null,
   *   legExpired: boolean,
   *   strategy: any,
   *   flash: any,
   *   stripe?: string,
   *   onToggleEnabled: (c: any, checked: boolean) => void,
   *   onExecuteDraft: (c: any) => void,
   *   onClosePosition: (c: any) => void,
   *   onRemoveDraft: (draftId: any) => void,
   *   onOpenChartTicket: (c: any) => void,
   *   onContextMenu: (c: any, ev: MouseEvent | PointerEvent) => void,
   *   pendingQty?: number,
   * }}
   */
  let {
    c,
    ci,
    prevBand,
    bandCount,
    legsTab,
    legAnalytics: lg,
    enabled,
    dayPnl: _dayPnl,
    expPnl: _expPnlLeg,
    legExpired: _legExp,
    strategy,
    flash,
    stripe = '',
    pendingQty = 0,
    onToggleEnabled,
    onExecuteDraft,
    onClosePosition,
    onRemoveDraft,
    onOpenChartTicket,
    onContextMenu,
  } = $props();

  // ── Derived locals (identical logic to what was in {@const} blocks) ───

  const ltp       = $derived(lg && lg.ltp != null ? lg.ltp : c.ltp);
  const cost      = $derived(c.avg_cost != null ? c.avg_cost : (lg ? lg.avg_cost : null));
  const isClosed  = $derived(Number(c.qty || 0) === 0);

  // Sold-today eq rows: surface opening_qty so Lots column shows original size.
  const _eqDisplayQty = $derived(
    c.kind === 'eq' && Number(c.qty || 0) === 0 && Number(c.opening_qty || 0) !== 0
      ? Number(c.opening_qty)
      : null
  );
  const displayQty = $derived(
    c._residualQty != null
      ? Number(c._residualQty)
      : (_eqDisplayQty != null ? _eqDisplayQty : Number(c.qty || 0))
  );

  const _ltpFromFallback = $derived(!!(lg && lg.ltp_source === 'avg_cost'));
  const pnl = $derived(
    c._residualQty != null
      ? ((ltp != null && cost != null && !_ltpFromFallback)
          ? (ltp - cost) * displayQty + Number(c.realised || 0)
          : null)
      : (c.pnl != null
          ? Number(c.pnl)
          : (ltp != null && cost != null && !_ltpFromFallback
              ? (ltp - cost) * displayQty + Number(c.realised || 0)
              : null))
  );

  const dir        = $derived(displayQty < 0 ? 'short' : displayQty > 0 ? 'long' : 'flat');
  const isClosable = $derived(!isClosed && c.source !== 'draft');
  const isDraft    = $derived(c.source === 'draft');

  const _decomp    = $derived(decomposeSymbol(c.symbol));
  const _optClass  = $derived(
    _decomp.optType === 'CE' ? 'sym-ce' : _decomp.optType === 'PE' ? 'sym-pe' : ''
  );
  const _acctColor = $derived(c.account ? acctColor(c.account) : null);
  const _legFlashKey = $derived(`leg:${c.account ?? ''}|${c.symbol ?? ''}`);

  // Band-header visibility — show when this row is first of its band in expiry view.
  const _showBandHeader = $derived(
    legsTab === 'expiry' && !!c._band && (ci === 0 || prevBand !== c._band)
  );

  // Row click handler — shared logic for onclick + onkeydown.
  function _handleRowActivate() {
    if (isDraft)         onExecuteDraft(c);
    else if (isClosable) onClosePosition(c);
    else                 onOpenChartTicket(c);
  }
</script>

<!-- Band section header injected before the row when this is the first of its band. -->
{#if _showBandHeader}
  <div class="expiry-band-header expiry-band-header-{c._band}"
       aria-label="{BAND_LABELS[c._band] ?? c._band} — {c._segment}">
    <span class="expiry-band-pill">
      <span class="expiry-band-dot" aria-hidden="true">
        {#if c._band === 'close'}●{:else if c._band === 'netted'}⊗{:else}○{/if}
      </span>
      <span class="expiry-band-label">{BAND_LABELS[c._band] ?? c._band}</span>
      <span class="expiry-band-count">{bandCount}</span>
    </span>
    {#if c._band === 'close'}<span class="expiry-band-hint">action required before expiry</span>
    {:else if c._band === 'netted'}<span class="expiry-band-hint">broker nets at settlement — no action needed</span>
    {:else if c._band === 'otm'}<span class="expiry-band-hint">expires worthless — monitor only</span>
    {/if}
  </div>
{/if}

<div class="cand-row {stripe} cand-row-{dir}"
     style={_acctColor ? `--cand-acct-color: ${_acctColor};` : ''}
     class:cand-disabled={!enabled}
     class:cand-closed={isClosed}
     class:cand-draft={isDraft}
     class:cand-eq={c.kind === 'eq'}
     class:expiry-band-close={legsTab === 'expiry' && c._band === 'close'}
     class:expiry-band-netted={legsTab === 'expiry' && c._band === 'netted'}
     class:expiry-band-otm={legsTab === 'expiry' && c._band === 'otm'}
     data-pair-tint={legsTab === 'expiry' && c._band === 'netted' ? (c._pairTint ?? 0) : null}
     class:cand-row-equity-close={c._expiryStatus === 'equity-close'}
     class:cand-row-commodity-close={c._expiryStatus === 'commodity-close'}
     role="button"
     tabindex="0"
     title={isDraft
       ? `Execute draft — open SymbolPanel on Ticket tab pre-filled`
       : isClosable
         ? `Close ${Math.abs(displayQty)} ${c.symbol} — SymbolPanel on Ticket tab`
         : `${c.symbol} — open SymbolPanel on Chart tab`}
     onclick={_handleRowActivate}
     onkeydown={(e) => {
       if (e.key === 'Enter' || e.key === ' ') {
         e.preventDefault();
         _handleRowActivate();
       }
     }}>
  <input type="checkbox"
         checked={enabled}
         onclick={(e) => e.stopPropagation()}
         onchange={(e) => {
           const checked = /** @type {HTMLInputElement} */ (e.currentTarget).checked;
           onToggleEnabled(c, checked);
         }} />
  <!-- svelte-ignore a11y_interactive_supports_focus -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <!-- Context-menu is a secondary affordance (right-click / long-press).
       Primary symbol selection is via checkbox. No keyboard equivalent
       needed here — the context-menu items are also accessible via row buttons. -->
  <span class="font-mono cand-sym cand-sym-acct"
    oncontextmenu={(ev) => { ev.preventDefault(); onContextMenu(c, ev); }}
    use:longPress={(ev) => { onContextMenu(c, ev); }}>
    {#if (() => { const rl = rootOfLabel(c.symbol, c.exchange || ''); return rl !== c.symbol; })()}
      <span class="sym-main {_optClass}" title={c.symbol}>{rootOfLabel(c.symbol, c.exchange || '')}</span>
    {:else}
      <span class="sym-main {_optClass}">{formatSymbol(c.symbol)}</span>
    {/if}
    {#if c.kind === 'eq'}
      <!-- Equity-holding leg tag — operator scanning the
           Candidates panel sees at a glance which row is
           the cash-stock layer behind the option strategy. -->
      <span class="cand-split-tag cand-eq-tag"
            title={c.proxy_for
              ? `Proxy hedge — ${c.symbol} ETF tracks ${c.proxy_for}; converted to target units at runtime via current LTPs`
              : `Cash equity holding of the underlying — adds (S − cost) × qty per spot to the payoff curve`}>STOCK</span>
      {#if c.proxy_for}
        {@const _spotForChip    = Number(strategy?.spot) || 0}
        {@const _proxyLtpChip  = Number(c.ltp) || 0}
        {@const _rawQtyChip    = Number(c.qty || 0) || Number(c.opening_qty || 0) || 0}
        {@const _rowChip       = getProxyRow(c.symbol, c.proxy_for)}
        {@const _betaChip      = _rowChip?.beta != null ? Number(_rowChip.beta) : 1.0}
        {@const _r2Chip        = _rowChip?.correlation != null ? Number(_rowChip.correlation) : 1.0}
        {@const _marketValChip = _rawQtyChip * _proxyLtpChip}
        {@const _effQtyChip    = _spotForChip > 0 ? (_betaChip * _marketValChip) / _spotForChip : 0}
        {@const _targetLot     = getOptionUnderlyingLot(c.proxy_for)}
        {@const _targetLots    = _targetLot > 0 ? _effQtyChip / _targetLot : 0}
        {@const _hasBeta       = _rowChip?.beta != null}
        {@const _regAtChip     = _rowChip?.regression_at ? new Date(_rowChip.regression_at) : null}
        {@const _regAgeDays    = _regAtChip ? Math.floor((Date.now() - _regAtChip.getTime()) / 86400000) : null}
        {@const _regErrChip    = _rowChip?.regression_error || null}
        {@const _regStaleHi    = _regAgeDays != null && _regAgeDays > 7}
        {@const _regStaleMid   = _regAgeDays != null && _regAgeDays > 2 && _regAgeDays <= 7}
        {@const _regSuffix     = _regErrChip
                                   ? ` · ⚠ regression failed: ${_regErrChip}`
                                   : (_regAgeDays != null
                                       ? ` · β computed ${_regAgeDays}d ago${_regStaleHi ? ' (STALE)' : ''}`
                                       : '')}
        <span class="cand-split-tag cand-proxy-tag"
              class:cand-proxy-stale={_regStaleHi || _regErrChip}
              class:cand-proxy-staleish={_regStaleMid}
              title={_effQtyChip > 0
                ? (_hasBeta
                    ? `β=${_betaChip.toFixed(3)} × market value ₹${_marketValChip.toFixed(0)} ÷ ${c.proxy_for} spot ₹${_spotForChip.toFixed(0)} ≈ ${_effQtyChip.toFixed(2)} ${c.proxy_for}-equiv${_targetLot > 0 ? ` ≈ ${_targetLots.toFixed(2)} ${c.proxy_for} lot${_targetLots === 1 ? '' : 's'} (lot=${_targetLot})` : ''} · R²=${_r2Chip.toFixed(2)}${_regSuffix}`
                    : `Market value ₹${_marketValChip.toFixed(0)} ÷ ${c.proxy_for} spot ₹${_spotForChip.toFixed(0)} ≈ ${_effQtyChip.toFixed(2)} ${c.proxy_for}-equivalent${_targetLot > 0 ? ` ≈ ${_targetLots.toFixed(2)} ${c.proxy_for} lot${_targetLots === 1 ? '' : 's'} (lot=${_targetLot})` : ''}${_regSuffix}`)
                : `Proxy of ${c.proxy_for} — waiting on live ${c.proxy_for} spot${_regSuffix}`}>PROXY{_targetLot > 0 && _effQtyChip > 0 ? ` ${_targetLots.toFixed(2)}×` : ''}{_hasBeta ? ` β${_betaChip.toFixed(2)}` : ''}{_regErrChip ? ' ⚠' : (_regStaleHi ? ' ◷' : '')}</span>
      {/if}
    {/if}
    {#if c._splitTag === 'closed'}
      <!-- Split-row tag: this row represents the portion of
           the overnight position that was CLOSED today.
           The sibling row (without the tag) represents
           what's still OPEN after the round-trip. -->
      <span class="cand-split-tag cand-split-closed"
            title="Closed portion of an intraday round-trip on this leg">CLOSED</span>
    {:else if c._splitTag === 'open'}
      <span class="cand-split-tag cand-split-open"
            title="Currently open portion after today's close-and-reopen">OPEN</span>
    {/if}
    {#if isDraft}
      <!-- Draft remove button — page-local removal only,
           NO order placed. Clicking the row body still
           opens the OrderTicket pre-filled to PLACE the
           draft as a real order; this × is the
           "discard" affordance. Stops propagation so
           the row's executeDraft handler doesn't fire. -->
      <button type="button" class="cand-draft-x"
              title="Remove this draft (no order placed)"
              aria-label="Remove draft"
              onclick={(e) => {
                e.stopPropagation();
                if (c.draftId != null) onRemoveDraft(c.draftId);
              }}>×</button>
    {/if}
    {#if legsTab === 'expiry' && c._band}
      {#if c._band === 'close' && c._closeId}
        <span class="expiry-id-chip expiry-id-close" title={c._reason}>{c._closeId}</span>
      {:else if c._band === 'netted' && c._pairId}
        <span class="expiry-id-chip expiry-id-netted" title={c._reason ?? ''}>{c._pairId}</span>
      {/if}
    {/if}
  </span>
  <!-- Expiry cell removed — the hyphenated symbol shows it. -->
  <span class="font-mono">{c.account}</span>
  {#if isClosed}
    <span class="cand-status-chip cand-closed-chip">closed</span>
  {:else if pendingQty > 0}
    <span class="num kv-pos">{pendingQty}</span>
    <span class="cand-status-chip cand-open-chip">open</span>
    {#if Math.abs(displayQty) - pendingQty > 0}
      <span class="num {displayQty < 0 ? 'kv-neg' : 'kv-pos'}">{Math.abs(displayQty) - pendingQty}</span>
    {/if}
  {:else}
    <span class="num {displayQty < 0 ? 'kv-neg' : 'kv-pos'}">{displayQty}</span>
  {/if}
  <!-- Lots column. For proxy eq rows the lot count is in
       TARGET units (e.g. 1500 GOLDBEES ≈ 0.15 GOLD lots),
       so the math derives from the same market_value /
       target_spot / target_lot_size chain the PROXY chip
       tooltip surfaces. Plain rows pass through to the
       shared lotsForRow helper (per-symbol inference: EQ
       → qHold, CE/PE/FUT → qPos). -->
  {#if c.proxy_for}
    {@const _lotsTargetSpot = Number(strategy?.spot) || 0}
    {@const _lotsProxyLtp   = Number(c.ltp) || 0}
    {@const _lotsRow        = getProxyRow(c.symbol, c.proxy_for)}
    {@const _lotsBeta       = _lotsRow?.beta != null ? Number(_lotsRow.beta) : 1.0}
    {@const _lotsTargetLot  = getOptionUnderlyingLot(c.proxy_for)}
    {@const _lotsEffQty     = (_lotsTargetSpot > 0 && _lotsProxyLtp > 0)
        ? (_lotsBeta * Math.abs(displayQty) * _lotsProxyLtp) / _lotsTargetSpot : 0}
    {@const _lotsTargetLots = _lotsTargetLot > 0 ? _lotsEffQty / _lotsTargetLot : 0}
    <span class="num"
          title={_lotsTargetLots > 0
            ? `${_lotsTargetLots.toFixed(2)} ${c.proxy_for} lot${_lotsTargetLots === 1 ? '' : 's'} (β=${_lotsBeta.toFixed(2)}, ${_lotsEffQty.toFixed(2)} target units ÷ ${_lotsTargetLot}/lot)`
            : `Waiting on ${c.proxy_for} spot`}>
      {_lotsTargetLots > 0 ? fmtLots(_lotsTargetLots) : '—'}
    </span>
  {:else}
    <span class="num">{fmtLots(lotsForRow({ tradingsymbol: c.symbol, quantity: displayQty }))}</span>
  {/if}
  <span class="num
    {typeof ltp === 'number' && typeof cost === 'number' && cost > 0
      ? (ltp > cost ? 'ltp-vs-avg-up' : ltp < cost ? 'ltp-vs-avg-down' : 'ltp-vs-avg-flat')
      : ''}
    {typeof ltp === 'number' && typeof c.prev_close === 'number' && c.prev_close > 0
      ? (ltp > c.prev_close ? 'ltp-vs-prev-up' : ltp < c.prev_close ? 'ltp-vs-prev-down' : 'ltp-vs-prev-flat')
      : ''}">{ltp != null ? priceFmt(ltp) : '—'}</span>
  <span class="num">{c.prev_close != null ? priceFmt(c.prev_close) : '—'}</span>
  <span class="num {displayQty > 0 ? 'cell-pos' : displayQty < 0 ? 'cell-neg' : 'cell-flat'}">{cost != null ? priceFmt(cost) : '—'}</span>
  <span class="num tf-cell cand-pnl {pnl == null ? '' : pnl > 0 ? 'cell-pos' : pnl < 0 ? 'cell-neg' : 'cell-flat'} {pnl == null ? '' : flash.classOf(`${_legFlashKey}:pnl`)}">
    {pnl == null ? '—' : aggCompact(pnl)}
  </span>
  <span class="num tf-cell cand-pnl {_dayPnl == null ? 'cell-flat' : _dayPnl > 0 ? 'cell-pos' : _dayPnl < 0 ? 'cell-neg' : 'cell-flat'} {_dayPnl == null ? '' : flash.classOf(`${_legFlashKey}:day`)}"
        title={_legExp ? 'Day P&L promoted to Exp P&L on expiry day — settlement realized today.' : "Day P&L = today's intraday move × qty"}>
    {_dayPnl == null ? '—' : aggCompact(Number(_dayPnl))}
  </span>
  <span class="num tf-cell cand-pnl {_expPnlLeg == null ? '' : _expPnlLeg > 0 ? 'cell-pos' : _expPnlLeg < 0 ? 'cell-neg' : 'cell-flat'} {_expPnlLeg == null ? '' : flash.classOf(`${_legFlashKey}:exp`)}"
        title="P&L if expired now at spot. Intrinsic value minus cost basis × qty.">
    {_expPnlLeg == null ? '—' : aggCompact(_expPnlLeg)}
  </span>
  <span class="num">{lg ? pctFmt(lg.iv * 100) + '%' : '—'}</span>
  <span class="num">{lg ? pctFmt(lg.greeks.delta) : '—'}</span>
  <span class="num">{lg ? pctFmt(lg.greeks.gamma) : '—'}</span>
  <span class="num {lg && lg.greeks.theta < 0 ? 'kv-neg' : ''}">{lg ? aggCompact(lg.greeks.theta) : '—'}</span>
  <span class="num">{lg ? aggCompact(lg.greeks.vega) : '—'}</span>
  <!-- Per-leg EV — placeholder; backend ships aggregate
       EV only today. The TOTAL row picks up _mergedEv. -->
  <span class="num cell-muted">—</span>
</div>

<style>
  /* ── Row layout — subgrid so columns align with parent .cand-grid ─── */
  /* Single parent grid via subgrid. Each row inherits the parent's
     column tracks — so headers and data cells line up exactly,
     regardless of which row has the longest content per column. */
  .cand-row {
    display: grid;
    grid-template-columns: subgrid;
    grid-column: 1 / -1;
    /* Subgrid inherits column-gap from .cand-grid (0.6rem). Don't
       set `gap` here — that overrides the parent and decouples the
       rows' spacing from the header's. */
    padding: 0.2rem 0.3rem;
    align-items: center;
    font-size: var(--fs-sm);
    font-family: monospace;
    font-variant-numeric: tabular-nums;
    border-radius: 3px;
    cursor: pointer;
    transition: background 0.1s;
    border-bottom: 1px solid rgba(126,151,184,0.10);
  }
  .cand-row:hover { background: rgba(34,211,238,0.05); }  /* cyan — matches History hover */

  /* Numeric column cells — right-aligned + truncation. */
  .cand-row > .num {
    text-align: right;
    justify-self: end;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* ── Closed positions ──────────────────────────────────────────────── */
  /* Closed positions (qty=0) — sorted to end of list, kept
     visible for context. Dim them so live rows pop, and disable
     the click-to-close affordance (no exposure to close). */
  .cand-row.cand-closed {
    opacity: 0.45;
    cursor: default;
  }
  .cand-row.cand-closed:hover { background: transparent; }

  /* ── Direction cue — long/short ───────────────────────────────────── */
  /* Long / short direction cue — encoded solely via the
     .cand-sym-acct::after right-border (green/red 2px). No row
     background-color or inset bars so the band-tint (expiry-band-*)
     and account tint on the symbol cell are the only background
     layers on the row. Hover falls through to the neutral .cand-row:hover.
     No background-color set so row-tint-odd alternating stripe shows through. */
  .cand-row-long:hover  { background: rgba(34,211,238,0.05); }
  .cand-row-short:hover { background: rgba(34,211,238,0.05); }

  /* ── Equity row tint ──────────────────────────────────────────────── */
  /* Soft sky tint on the whole eq row so it reads as a different
     layer from the option/futures legs without competing with the
     pos-long/pos-short direction tints. */
  .cand-row.cand-eq {
    background: rgba(56, 189, 248, 0.05) !important;
  }

  /* ── Draft rows ───────────────────────────────────────────────────── */
  /* Draft rows — distinct from live / sim positions: dashed
     magenta inset bar on the LEFT only (not both edges like
     long/short), faint magenta-tinted background, and a slim
     row-level dashed left border so even a flat-zero draft
     reads as "this isn't a real position". Magenta matches the
     `leg-source-draft` text colour `#f0abfc` used on the leg
     panel + the draft input rows above. */
  .cand-row.cand-draft {
    background-color: rgba(240,171,252,0.06);
    box-shadow: inset 2px 0 0 rgba(240,171,252,0.85);
    /* Override the long/short tint so the draft cue wins. */
  }
  .cand-row.cand-draft.cand-row-long,
  .cand-row.cand-draft.cand-row-short {
    background-color: rgba(240,171,252,0.06);
    box-shadow: inset 2px 0 0 rgba(240,171,252,0.85);
  }
  .cand-row.cand-draft:hover {
    background-color: rgba(240,171,252,0.14);
  }

  /* ── Disabled (unchecked) rows ────────────────────────────────────── */
  .cand-disabled {
    opacity: 0.45;
  }
  .cand-disabled:hover { background: rgba(248,113,113,0.05); }

  /* ── Checkbox ─────────────────────────────────────────────────────── */
  .cand-row input[type="checkbox"] {
    accent-color: var(--c-action);
    width: 0.9rem;
    height: 0.9rem;
    cursor: pointer;
  }

  /* ── Symbol cell ──────────────────────────────────────────────────── */
  /* Symbol-cell treatment ported from the Pulse Positions grid so the
     two surfaces look identical at a glance. ONE vertical right border
     per symbol cell encoding TODAY's P&L direction (day-pnl mini-bar).
     `--cand-acct-color` is set per-row via inline style from the
     account's hash colour (acctColor from $lib/account). */
  .cand-sym {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
  }
  .cand-sym-acct {
    position: relative;
    background-color: color-mix(in srgb, var(--cand-acct-color, transparent) 14%, transparent);
  }
  /* CE / PE text tint on the symbol main (Sensibull / Streak convention). */
  :global(.cand-sym .sym-main)        { color: #e2e8f0; font-weight: 600; }
  :global(.cand-sym .sym-main.sym-ce) { color: var(--c-long); }
  :global(.cand-sym .sym-main.sym-pe) { color: var(--c-short); }
  /* SINGLE vertical right border on the symbol cell, encoding
     POSITION DIRECTION (long vs short). 2 px wide, flush against the
     right edge. Green when qty > 0 (long), red when qty < 0 (short),
     NO border when qty = 0 (flat). */
  .cand-row.cand-row-long  .cand-sym-acct::after,
  .cand-row.cand-row-short .cand-sym-acct::after {
    content: '';
    position: absolute;
    right: 0;
    top: 0;
    bottom: 0;
    width: 2px;
    pointer-events: none;
  }
  .cand-row.cand-row-long  .cand-sym-acct::after { background: rgba(74, 222, 128, 0.85); }
  .cand-row.cand-row-short .cand-sym-acct::after { background: rgba(248, 113, 113, 0.85); }

  /* ── Draft × button ───────────────────────────────────────────────── */
  /* Draft × — sits inline with the symbol, lets operator discard
     a draft without going through the OrderTicket modal. Magenta
     to match the draft row identity. */
  .cand-draft-x {
    flex: 0 0 auto;
    width: 1.1rem;
    height: 1.1rem;
    padding: 0;
    border-radius: 2px;
    border: 1px solid rgba(240,171,252,0.45);
    background: rgba(240,171,252,0.10);
    color: #f0abfc;
    font-family: monospace;
    font-size: var(--fs-xl);
    font-weight: 700;
    line-height: 1;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }
  .cand-draft-x:hover {
    background: rgba(240,171,252,0.22);
    border-color: rgba(240,171,252,0.75);
    color: #fff;
  }

  /* ── P&L cell ─────────────────────────────────────────────────────── */
  /* P&L cell — same green/red scheme as /dashboard's pnl-gain /
     pnl-loss classes. Subtle background tint for a glanceable
     "win or lose?" cue at row-scan speed; bold weight so the
     numbers pop alongside the otherwise-muted row content. */
  .cand-pnl {
    border-radius: 2px;
    padding: 0 0.25rem;
    font-weight: 700;
  }
  /* Background tint for P&L cells (colour comes from the global
     cell-pos / cell-neg / cell-flat rules in MarketPulse). */
  :global(.cand-pnl.cell-pos)  { background-color: rgba(74,222,128,0.08); }
  :global(.cand-pnl.cell-neg)  { background-color: rgba(248,113,113,0.08); }
  :global(.cand-pnl.cell-flat) { background-color: rgba(148,163,184,0.06); }

  /* ── LTP heat encoding ────────────────────────────────────────────── */
  /* LTP heat encoding — mirrors the ag-theme-algo rules in app.css
     but scoped to this component (which isn't an ag-Grid surface). */
  .ltp-vs-avg-up   { background-color: var(--algo-green-bg); }
  .ltp-vs-avg-down { background-color: var(--algo-red-bg); }
  .ltp-vs-prev-up   { box-shadow: inset 1px 0 0 0 rgba(74,222,128,0.85); }
  .ltp-vs-prev-down { box-shadow: inset 1px 0 0 0 rgba(248,113,113,0.85); }
  .ltp-vs-prev-flat { box-shadow: inset 1px 0 0 0 rgba(126,151,184,0.50); }

  /* ── Split-row tags ───────────────────────────────────────────────── */
  /* Split-row tags — small chip beside the symbol, indicates whether
     this row is the closed half or the open half of a close-and-
     reopen sequence today. */
  .cand-split-tag {
    display: inline-block;
    margin-left: 0.35rem;
    padding: 0 0.3rem;
    font-size: var(--fs-2xs);
    font-weight: 800;
    letter-spacing: 0.06em;
    border-radius: 2px;
    font-family: var(--font-numeric);
    vertical-align: middle;
  }
  .cand-split-closed {
    color: var(--c-short);
    background: var(--algo-red-bg);
    border: 1px solid rgba(248, 113, 113, 0.45);
  }
  .cand-split-open {
    color: var(--c-long);
    background: var(--algo-green-bg);
    border: 1px solid rgba(74, 222, 128, 0.45);
  }
  /* Equity-leg tag — sky-blue family, distinct from the green/red
     split tags so the operator can scan the panel and instantly tell
     "this row is the stock layer behind my option strategy". */
  .cand-eq-tag {
    color: #38bdf8;
    background: rgba(56, 189, 248, 0.18);
    border: 1px solid rgba(56, 189, 248, 0.45);
  }
  /* Proxy-hedge chip — magenta tint distinguishes a proxy leg (GOLDBEES
     hedging GOLD, NIFTYBEES hedging NIFTY etc.) from a direct STOCK leg. */
  .cand-proxy-tag {
    color: #c084fc;
    background: rgba(192, 132, 252, 0.16);
    border: 1px solid rgba(192, 132, 252, 0.45);
  }
  /* Sprint D — stale β surfaces in two intensities. `staleish` (2-7d
     since last regression) is amber: operator should be aware but
     the β is probably still useful. `stale` (> 7d or last attempt
     errored) is red: the freshness window expired. */
  .cand-proxy-tag.cand-proxy-staleish {
    color: var(--c-action);
    background: var(--algo-amber-bg);
    border-color: var(--algo-amber-border);
  }
  .cand-proxy-tag.cand-proxy-stale {
    color: var(--c-short);
    background: rgba(248, 113, 113, 0.16);
    border-color: var(--algo-red-border);
  }

  /* ── Expiry tab — three-band row tints ───────────────────────────── */
  /* Band semantics:
       close  → amber accent — operator action required
       netted → slate/cool — broker settles, no action needed
       otm    → faded/muted — expires worthless, monitor only
     Legacy cand-row-equity-close / cand-row-commodity-close are kept
     so the existing _expiryStatus-based class assignments still work;
     the new band classes are the canonical path going forward. */
  .cand-row.expiry-band-close {
    background-color: var(--algo-amber-bg-soft);
    box-shadow: inset 2px 0 0 rgba(251, 191, 36, 0.65);
  }
  .cand-row.expiry-band-netted {
    /* No background-color: let row-tint-odd alternating stripe show through. */
    box-shadow: inset 2px 0 0 rgba(125, 145, 184, 0.55);
  }
  /* Per-pair tint — each pair of opposite positions inside the
     NETTED band gets one of 5 alternating LEFT inset bars so the
     operator can visually map "this row cancels that one".
     No background-color set: row-tint-odd stripe shows on alternating rows.
     Bar alpha raised to 0.75 so the colour cue is legible without
     a background fill. */
  .cand-row.expiry-band-netted[data-pair-tint="0"] {
    box-shadow: inset 2px 0 0 rgba(125, 211, 252, 0.75);  /* sky */
  }
  .cand-row.expiry-band-netted[data-pair-tint="1"] {
    box-shadow: inset 2px 0 0 rgba(168, 85, 247, 0.75);   /* violet */
  }
  .cand-row.expiry-band-netted[data-pair-tint="2"] {
    box-shadow: inset 2px 0 0 rgba(45, 212, 191, 0.75);   /* teal */
  }
  .cand-row.expiry-band-netted[data-pair-tint="3"] {
    box-shadow: inset 2px 0 0 rgba(244, 114, 182, 0.75);  /* pink */
  }
  .cand-row.expiry-band-netted[data-pair-tint="4"] {
    box-shadow: inset 2px 0 0 rgba(132, 204, 22, 0.75);   /* lime */
  }
  .cand-row.expiry-band-otm {
    /* No background-color: let row-tint-odd alternating stripe show through. */
    box-shadow: none;
    opacity: 0.55;
  }
  /* In the Exp Close tab direction is already communicated by the
     cand-sym-acct::after right-border on the symbol cell. Strip the
     double-side (left+right) inset bars from cand-row-long/short so
     they don't stack on top of band tints and create clutter.
     Combined selectors (3 classes) outrank cand-row-long/short alone,
     so no !important needed. Band background wins; box-shadow reduces
     to single-left (inherited from band rule). */
  .cand-row.expiry-band-close.cand-row-long,
  .cand-row.expiry-band-close.cand-row-short {
    background-color: var(--algo-amber-bg-soft);
    box-shadow: inset 2px 0 0 rgba(251, 191, 36, 0.65);
  }
  .cand-row.expiry-band-netted.cand-row-long,
  .cand-row.expiry-band-netted.cand-row-short {
    /* No background-color: let row-tint-odd show through on alternating rows. */
    box-shadow: inset 2px 0 0 rgba(125, 145, 184, 0.55);
  }
  .cand-row.expiry-band-otm.cand-row-long,
  .cand-row.expiry-band-otm.cand-row-short {
    /* No background-color: let row-tint-odd show through on alternating rows. */
    box-shadow: none;
  }
  .cand-row.expiry-band-close.cand-row-long:hover,
  .cand-row.expiry-band-close.cand-row-short:hover {
    background-color: var(--algo-amber-bg-soft);
  }
  .cand-row.expiry-band-netted.cand-row-long:hover,
  .cand-row.expiry-band-netted.cand-row-short:hover {
    background-color: rgba(34,211,238,0.05);
  }
  /* Legacy band aliases — keep while _expiryStatus still references them.
     Background stripped so they no longer override expiry-band-close amber;
     box-shadow kept as a minimal fallback cue in the (unlikely) case the
     band class is absent. In practice every equity-close / commodity-close
     row in the Exp Close tab also carries expiry-band-close which owns the
     amber-soft background and amber left bar. */
  .cand-row.cand-row-equity-close {
    background-color: transparent;
    box-shadow: inset 2px 0 0 rgba(248, 113, 113, 0.50);
  }
  .cand-row.cand-row-commodity-close {
    background-color: transparent;
    box-shadow: inset 2px 0 0 rgba(251, 191, 36, 0.50);
  }

  /* ── Expiry band section header ───────────────────────────────────── */
  /* Band section header — full-width row containing the section
     identity pill + a muted hint to the right. The pill itself
     does the heavy visual work; the row chrome stays minimal. */
  .expiry-band-header {
    grid-column: 1 / -1;
    display: flex;
    align-items: center;
    gap: 0.55rem;
    padding: 0.55rem 0.45rem 0.4rem;
    margin-top: 0.6rem;
    border-bottom: 1px solid rgba(200, 216, 240, 0.08);
  }
  .expiry-band-header:first-of-type,
  .expiry-band-header-close:first-child {
    margin-top: 0;
  }
  /* Section pill — colored background + border + leading dot glyph
     + label + count badge, all as a single inline-flex chunk so
     the section identity reads at a glance. Each band gets its
     own palette via the modifier rules below. */
  .expiry-band-pill {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.32rem 0.7rem 0.32rem 0.55rem;
    border-radius: 9999px;
    font-family: var(--font-numeric);
    line-height: 1;
    border: 1px solid transparent;
  }
  .expiry-band-dot {
    font-size: var(--fs-lg);
    line-height: 1;
    flex-shrink: 0;
  }
  .expiry-band-label {
    font-size: var(--fs-sm);
    font-weight: 800;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }
  .expiry-band-count {
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    font-weight: 700;
    padding: 0.12rem 0.45rem;
    border-radius: 9999px;
    line-height: 1;
    font-variant-numeric: tabular-nums;
  }
  .expiry-band-hint {
    font-size: var(--fs-xs);
    opacity: 0.55;
    font-style: italic;
    color: var(--c-muted);
  }
  /* TO CLOSE — amber pill, glowing. Highest-attention band:
     these positions need broker action before expiry. */
  .expiry-band-header-close .expiry-band-pill {
    background: var(--algo-amber-bg-strong);
    border-color: var(--algo-amber-border);
    color: var(--c-action);
    box-shadow: 0 0 6px var(--algo-amber-border-soft);
  }
  .expiry-band-header-close .expiry-band-count {
    background: var(--algo-amber-border-soft);
    color: #fed7aa;
    border: 1px solid var(--algo-amber-border);
  }
  /* NETTED — slate pill, balanced. Mid-attention band: positions
     cancel each other at settlement, operator should see the pair
     structure but no action needed. */
  .expiry-band-header-netted .expiry-band-pill {
    background: rgba(125, 145, 184, 0.18);
    border-color: rgba(125, 145, 184, 0.42);
    color: #c8d8f0;
  }
  .expiry-band-header-netted .expiry-band-count {
    background: rgba(125, 145, 184, 0.30);
    color: #c8d8f0;
    border: 1px solid rgba(125, 145, 184, 0.45);
  }
  /* OUT OF THE MONEY — muted pill, lowest visual weight.
     Traceability only; these expire worthless. */
  .expiry-band-header-otm .expiry-band-pill {
    background: rgba(126, 151, 184, 0.10);
    border-color: rgba(126, 151, 184, 0.28);
    color: var(--c-muted);
  }
  .expiry-band-header-otm .expiry-band-count {
    background: rgba(126, 151, 184, 0.22);
    color: var(--c-muted);
    border: 1px solid rgba(126, 151, 184, 0.35);
  }

  /* ── Expiry ID chip (inside symbol cell) ─────────────────────────── */
  /* Tag chip inside the symbol cell — #N1 / #C1. */
  .expiry-id-chip {
    display: inline-flex;
    align-items: center;
    padding: 0.05rem 0.3rem;
    border-radius: 3px;
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.05em;
    margin-left: 0.25rem;
    line-height: 1;
    vertical-align: middle;
  }
  .expiry-id-close {
    background: var(--algo-amber-bg-strong);
    color: var(--c-action);
    border: 1px solid var(--algo-amber-border-soft);
  }
  .expiry-id-netted {
    background: rgba(125, 145, 184, 0.15);
    color: #94a3b8;
    border: 1px solid rgba(125, 145, 184, 0.3);
  }
</style>
