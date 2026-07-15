<script>
  import { aggCompact, priceFmt, qtyFmt } from '$lib/format';
  import { baseDayPnlForPosition, aggregateDayPnlForPositions } from '$lib/data/nav';

  let { open, positions, onClose } = $props();

  function fmtMoney(/** @type {number|null|undefined} */ v) {
    if (v == null || !isFinite(v)) return '—';
    return aggCompact(v);
  }

  function _zeroReason(p) {
    const qty   = Number(p?.quantity ?? 0);
    const ltp   = Number(p?.last_price ?? 0);
    const close = Number(p?.close_price ?? 0);
    const oq    = Number(p?.overnight_quantity ?? 0);
    const pnl   = Number(p?.pnl ?? 0);
    const prev  = p?.prev_settlement_pnl;
    if (qty === 0) return 'Flat — closed intraday';
    if (ltp > 0 && close > 0 && Math.abs(ltp - close) < 0.005) return 'LTP equals prev close (stale price)';
    if (prev != null && Math.abs(pnl - Number(prev)) < 0.5) return 'No move from yesterday settlement';
    if (oq === 0 && Math.abs(pnl) < 0.5) return 'Opened today, at cost — no movement yet';
    return 'Day P&L is zero';
  }

  let _panel = $state(null);

  $effect(() => {
    if (open) _panel?.focus();
  });

  let _expanded = $state(new Set());

  function _toggleRow(key) {
    const s = new Set(_expanded);
    if (s.has(key)) { s.delete(key); } else { s.add(key); }
    _expanded = s;
  }

  const _grouped = $derived.by(() => {
    const rows = (positions ?? []).slice();
    const byAcct = new Map();
    for (const p of rows) {
      const acct = String(p?.account ?? '—');
      if (!byAcct.has(acct)) byAcct.set(acct, []);
      byAcct.get(acct).push(p);
    }
    const groups = [];
    for (const [acct, acctRows] of byAcct) {
      const sorted = acctRows.slice().sort(
        (a, b) => Math.abs(baseDayPnlForPosition(b)) - Math.abs(baseDayPnlForPosition(a))
      );
      const subtotal = sorted.reduce((s, p) => s + baseDayPnlForPosition(p), 0);
      groups.push({ acct, rows: sorted, subtotal });
    }
    groups.sort((a, b) => Math.abs(b.subtotal) - Math.abs(a.subtotal));
    return groups;
  });

  const _total = $derived(aggregateDayPnlForPositions(positions ?? []));

  $effect(() => {
    if (!open) return;
    function _onKey(e) {
      if (e.key === 'Escape') onClose();
    }
    window.addEventListener('keydown', _onKey);
    return () => window.removeEventListener('keydown', _onKey);
  });
</script>

{#if open}
  <div class="dpb-overlay"
       role="dialog"
       aria-modal="true"
       aria-label="Day P&L Breakup"
       tabindex="-1"
       onclick={onClose}
       onkeydown={(e) => { if (e.key === 'Escape') onClose(); }}>
    <div class="dpb-panel"
         role="presentation"
         bind:this={_panel}
         tabindex="-1"
         onclick={(e) => e.stopPropagation()}>

      <div class="dpb-header">
        <span class="dpb-title">Day P&amp;L Breakup</span>
        <span class={'dpb-total ' + (_total > 0 ? 'dpb-pos' : _total < 0 ? 'dpb-neg' : 'dpb-flat')}>
          {fmtMoney(_total)}
        </span>
        <button class="dpb-close" type="button" onclick={onClose} aria-label="Close">✕</button>
      </div>

      <div class="dpb-scroll">
        {#if !positions?.length}
          <p style="text-align:center;color:rgba(200,216,240,0.5);padding:1.5rem 0;font-size:0.8rem;">No positions</p>
        {:else}
        <table class="dpb-table">
          <thead>
            <tr>
              <th></th>
              <th class="dpb-th-left">Symbol</th>
              <th class="dpb-th-left">Account</th>
              <th class="dpb-th-right">Prev Close</th>
              <th class="dpb-th-right">LTP</th>
              <th class="dpb-th-right">O/N Qty</th>
              <th class="dpb-th-right">Buy</th>
              <th class="dpb-th-right">Sell</th>
              <th class="dpb-th-right">PnL</th>
              <th class="dpb-th-right">Settle PnL</th>
              <th class="dpb-th-right">Day P&amp;L</th>
            </tr>
          </thead>
          <tbody>
            {#each _grouped as group (group.acct)}
              {#each group.rows as p (String(p?.account ?? '') + '|' + String(p?.tradingsymbol ?? ''))}
                {@const dayPnl    = baseDayPnlForPosition(p)}
                {@const rowKey    = String(p?.account ?? '') + '|' + String(p?.tradingsymbol ?? '')}
                {@const isZero    = Math.abs(dayPnl) < 0.5}
                {@const isExpanded = _expanded.has(rowKey)}
                {@const pnl       = Number(p?.pnl ?? 0)}
                {@const prevPnl   = p?.prev_settlement_pnl}
                {@const oq        = Number(p?.overnight_quantity ?? 0)}
                {@const close     = Number(p?.close_price ?? 0)}
                {@const avg       = Number(p?.average_price ?? p?.avg_cost ?? 0)}
                {@const hasPrev   = prevPnl != null && isFinite(Number(prevPnl))}
                <tr class="dpb-row">
                  <td class="dpb-td-chevron">
                    <button class="dpb-chevron" type="button"
                            onclick={() => _toggleRow(rowKey)}
                            aria-label={isExpanded ? 'Collapse formula' : 'Expand formula'}>
                      {isExpanded ? '▾' : '▸'}
                    </button>
                  </td>
                  <td class="dpb-td-sym">{p?.tradingsymbol ?? '—'}</td>
                  <td class="dpb-td-acct">{p?.account ?? '—'}</td>
                  <td class="dpb-td-num">{priceFmt(p?.close_price)}</td>
                  <td class="dpb-td-num">{priceFmt(p?.last_price)}</td>
                  <td class="dpb-td-num">{qtyFmt(p?.overnight_quantity)}</td>
                  <td class="dpb-td-num">{qtyFmt(p?.day_buy_quantity)}@{fmtMoney(p?.day_buy_value)}</td>
                  <td class="dpb-td-num">{qtyFmt(p?.day_sell_quantity)}@{fmtMoney(p?.day_sell_value)}</td>
                  <td class="dpb-td-num">{fmtMoney(p?.pnl)}</td>
                  <td class="dpb-td-num">{p?.prev_settlement_pnl != null ? fmtMoney(p.prev_settlement_pnl) : '—'}</td>
                  <td class={'dpb-td-num dpb-td-daypnl ' + (dayPnl > 0 ? 'dpb-pos' : dayPnl < 0 ? 'dpb-neg' : 'dpb-flat')}>
                    {fmtMoney(dayPnl)}
                    {#if isZero}
                      <span class="dpb-warn" title={_zeroReason(p)}>⚠</span>
                    {/if}
                  </td>
                </tr>
                {#if isExpanded}
                  <tr class="dpb-row-formula">
                    <td></td>
                    <td colspan="10" class="dpb-formula">
                      {#if hasPrev}
                        Day P&L = PnL − Settlement = {fmtMoney(pnl)} − {fmtMoney(Number(prevPnl))} = {fmtMoney(pnl - Number(prevPnl))}
                      {:else}
                        Day P&L = PnL − (O/N Qty × (Close − Avg)) = {fmtMoney(pnl)} − ({qtyFmt(oq)} × ({priceFmt(close)} − {priceFmt(avg)})) = {fmtMoney(dayPnl)}
                      {/if}
                    </td>
                  </tr>
                {/if}
              {/each}
              <tr class="dpb-row-subtotal">
                <td></td>
                <td colspan="9" class="dpb-subtotal-label">{group.acct}</td>
                <td class={'dpb-td-num ' + (group.subtotal > 0 ? 'dpb-pos' : group.subtotal < 0 ? 'dpb-neg' : 'dpb-flat')}>
                  {fmtMoney(group.subtotal)}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
        {/if}
      </div>

    </div>
  </div>
{/if}

<style>
  .dpb-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.65);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 400;
    padding: 1rem;
  }
  .dpb-panel {
    background: linear-gradient(180deg, #1c2840 0%, #141e33 100%);
    border: 1px solid var(--algo-amber-border-soft);
    border-radius: 4px;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.6);
    width: min(860px, calc(100vw - 2rem));
    max-height: calc(100vh - 4rem);
    display: flex;
    flex-direction: column;
    color: var(--algo-slate);
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
  }
  .dpb-header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.55rem 0.75rem;
    border-bottom: 1px solid var(--algo-amber-border-soft);
    flex-shrink: 0;
  }
  .dpb-title {
    font-size: var(--fs-lg);
    font-weight: 700;
    color: var(--algo-amber);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    flex: 1;
  }
  .dpb-total {
    font-size: var(--fs-lg);
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }
  .dpb-close {
    background: transparent;
    border: none;
    color: rgba(200, 216, 240, 0.5);
    cursor: pointer;
    font-size: var(--fs-lg);
    padding: 0.1rem 0.3rem;
    line-height: 1;
    margin-left: 0.25rem;
  }
  .dpb-close:hover { color: rgba(200, 216, 240, 0.9); }
  .dpb-scroll {
    overflow: auto;
    flex: 1;
    min-height: 0;
  }
  .dpb-table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--fs-sm);
    font-variant-numeric: tabular-nums;
  }
  .dpb-table thead tr {
    position: sticky;
    top: 0;
    background: #131c33;
    z-index: 1;
  }
  .dpb-th-left,
  .dpb-th-right {
    padding: 0.25rem 0.4rem;
    font-size: 0.65rem;
    font-weight: 600;
    color: rgba(200, 216, 240, 0.55);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    white-space: nowrap;
    border-bottom: 1px solid rgba(255, 255, 255, 0.07);
  }
  .dpb-th-left { text-align: left; }
  .dpb-th-right { text-align: right; }
  .dpb-row td {
    padding: 0.18rem 0.4rem;
    border-bottom: 1px solid rgba(255, 255, 255, 0.04);
  }
  .dpb-row:hover td { background: rgba(255, 255, 255, 0.03); }
  .dpb-td-chevron {
    width: 1.2rem;
    text-align: center;
    padding: 0 0.15rem !important;
  }
  .dpb-chevron {
    background: transparent;
    border: none;
    color: rgba(200, 216, 240, 0.4);
    cursor: pointer;
    font-size: 0.65rem;
    padding: 0;
    line-height: 1;
  }
  .dpb-chevron:hover { color: rgba(200, 216, 240, 0.85); }
  .dpb-td-sym {
    font-size: var(--fs-sm);
    color: rgba(200, 216, 240, 0.9);
    white-space: nowrap;
  }
  .dpb-td-acct {
    font-size: 0.65rem;
    color: rgba(200, 216, 240, 0.5);
    white-space: nowrap;
  }
  .dpb-td-num {
    text-align: right;
    white-space: nowrap;
    color: rgba(200, 216, 240, 0.75);
  }
  .dpb-td-daypnl {
    font-weight: 600;
  }
  .dpb-warn {
    margin-left: 0.2rem;
    color: #f59e0b;
    font-size: 0.6rem;
    cursor: help;
  }
  .dpb-row-formula td {
    padding: 0.15rem 0.4rem 0.3rem;
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
  }
  .dpb-formula {
    font-size: 0.65rem;
    color: rgba(200, 216, 240, 0.5);
    font-variant-numeric: tabular-nums;
    padding-left: 1.8rem !important;
  }
  .dpb-row-subtotal td {
    padding: 0.22rem 0.4rem;
    background: rgba(251, 191, 36, 0.06);
    border-top: 1px solid rgba(251, 191, 36, 0.15);
    border-bottom: 1px solid rgba(251, 191, 36, 0.15);
  }
  .dpb-subtotal-label {
    font-size: 0.65rem;
    font-weight: 700;
    color: rgba(251, 191, 36, 0.75);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .dpb-pos { color: #4ade80; }
  .dpb-neg { color: #f87171; }
  .dpb-flat { color: rgba(200, 216, 240, 0.55); }
</style>
