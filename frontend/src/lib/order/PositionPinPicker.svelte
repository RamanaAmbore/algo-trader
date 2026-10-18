<script>
  // PositionPinPicker — lightweight picker opened from the position-state
  // badge (CandidateLegRow.svelte / MarketPulse's Positions bucket) or
  // from SymbolContextMenu's "Pair with…" entry. Two modes, chosen by
  // whether the row is already pinned:
  //
  //   Not pinned  — shows a scrollable list of the account's other open
  //                 positions (caller-supplied `candidates`, already
  //                 filtered to exclude the source row); clicking one
  //                 calls POST /api/positions/pin.
  //   Pinned      — shows the current pin group id + an Unpin action,
  //                 calling DELETE /api/positions/pin/{id}.
  //
  // RESOLVED (was flagged as an assumption in both this file and api.js's
  // unpinPosition docstring): the backend's `position_pairs` table id and
  // its DISPLAY-only `pin_group_key` are different values —
  // `_apply_pin_overlay()` (backend/api/routes/positions.py) formats
  // `pin_group_key` as `f"PIN{row.id}"` for the badge glyph, but
  // `DELETE /api/positions/pin/{pair_id:int}` takes the bare numeric row
  // id, not that formatted string. `_unpin()` below strips the "PIN"
  // prefix back off before calling `unpinPosition()`. If the backend's
  // formatting prefix ever changes, this is the one place to update.
  //
  // Caller owns: resolving `account`/`candidates` (this component is
  // presentational + API-calling only, no position-list derivation),
  // and refetching/toasting after `onDone` fires.
  //
  // `stateTooltip` (2026-10-17) — the clicked badge's generic
  // "what does this state mean" text (positionStateBadge.js's
  // badge.tooltip, e.g. "Auto-paired P1 — detected automatically...").
  // Rendered as a one-line hint above the pairing controls so this one
  // popup covers both "what does the label mean" and "do something
  // about it" — no separate info icon/tooltip.

  import { pinPositions, unpinPosition } from '$lib/api.js';
  import { rawPinId } from '$lib/data/positionStateBadge.js';
  import { portal } from '$lib/portal';
  import { pushLayer, popLayer } from '$lib/utils/layerStack.js';

  /** @type {{
   *   open: boolean,
   *   account: string,
   *   sourceSymbol: string,
   *   pinGroupKey?: string | null,
   *   stateTooltip?: string,
   *   candidates?: Array<{symbol: string, account?: string, qty?: number}>,
   *   onDone?: (action: 'pinned'|'unpinned') => void,
   *   onClose?: () => void,
   * }} */
  let {
    open = $bindable(false),
    account = '',
    sourceSymbol = '',
    pinGroupKey = null,
    stateTooltip = '',
    candidates = [],
    onDone = null,
    onClose = null,
  } = $props();

  let submitting = $state(false);
  let error = $state('');

  const _isPinned = $derived(!!pinGroupKey);

  // Teardown form (push on open, pop via the effect's own cleanup) —
  // same Escape-stack convention as OrderPairModal.svelte. This
  // component's host wraps it in `{#if open}` so `open=false` unmounts
  // in the same reactive flush; relying on a later re-run to catch
  // `open===false` would race the unmount.
  $effect(() => {
    if (!open) return;
    error = '';
    const id = pushLayer(() => { open = false; onClose?.(); });
    return () => popLayer(id);
  });

  function _close() {
    open = false;
    onClose?.();
  }

  async function _pinWith(/** @type {string} */ symbolB) {
    if (!account || !sourceSymbol || !symbolB || submitting) return;
    submitting = true; error = '';
    try {
      await pinPositions({ account, symbolA: sourceSymbol, symbolB });
      onDone?.('pinned');
      open = false;
    } catch (e) {
      error = /** @type {any} */ (e)?.message || 'Pin failed';
    } finally {
      submitting = false;
    }
  }

  async function _unpin() {
    if (!pinGroupKey || submitting) return;
    submitting = true; error = '';
    try {
      await unpinPosition(rawPinId(pinGroupKey));
      onDone?.('unpinned');
      open = false;
    } catch (e) {
      error = /** @type {any} */ (e)?.message || 'Unpin failed';
    } finally {
      submitting = false;
    }
  }
</script>

{#if open}
  <div class="pin-overlay" role="dialog" aria-modal="true" use:portal>
    <div class="pin-card">
      <div class="pin-title-row">
        <span class="pin-title">{_isPinned ? 'Pinned pair' : 'Pair with…'}</span>
        <button type="button" class="pin-close" onclick={_close} aria-label="Close">×</button>
      </div>
      <div class="pin-source">{sourceSymbol} <span class="pin-source-acct">· {account}</span></div>
      {#if stateTooltip}
        <!-- 2026-10-17 (operator: "we don't need i for tool tip...
             do you think the popup window need to show the pair
             functionality instead of just showing pair info") — folds
             the generic "what does this state mean" text
             (positionStateBadge.js's badge.tooltip) into the SAME popup
             the badge click already opens, instead of a separate static
             tooltip/icon. One click target, one popup, both jobs. -->
        <div class="pin-state-hint">{stateTooltip}</div>
      {/if}

      {#if error}<div class="pin-error">{error}</div>{/if}

      {#if _isPinned}
        <div class="pin-current">
          Pinned as <span class="pin-current-id">{pinGroupKey}</span> — won't change on next poll.
        </div>
        <div class="pin-actions">
          <button type="button" class="pin-cancel" onclick={_close}>Close</button>
          <button type="button" class="pin-unpin" onclick={_unpin} disabled={submitting}>
            {submitting ? 'Unpinning…' : 'Unpin'}
          </button>
        </div>
      {:else}
        {#if candidates.length === 0}
          <div class="pin-empty">No other open positions in {account || 'this account'}.</div>
        {:else}
          <div class="pin-list" role="list">
            {#each candidates as cand (cand.symbol + '|' + (cand.account ?? ''))}
              <button type="button" class="pin-candidate"
                disabled={submitting}
                onclick={() => _pinWith(cand.symbol)}>
                <span class="pin-candidate-sym">{cand.symbol}</span>
                {#if cand.qty != null}<span class="pin-candidate-qty">{cand.qty}</span>{/if}
              </button>
            {/each}
          </div>
        {/if}
        <div class="pin-actions">
          <button type="button" class="pin-cancel" onclick={_close}>Cancel</button>
        </div>
      {/if}
    </div>
  </div>
{/if}

<style>
  .pin-overlay {
    position: fixed; inset: 0; z-index: var(--z-modal-nested);
    background: rgba(8,12,20,0.72);
    backdrop-filter: blur(2px);
    display: flex; align-items: center; justify-content: center;
    padding: 1rem;
  }
  .pin-card {
    background: #0f1b2e; border: 1px solid color-mix(in srgb, var(--text-med) 20%, transparent);
    border-radius: 8px; padding: 1rem 1.1rem;
    min-width: 280px; max-width: 420px; width: 100%;
    max-height: 80vh; overflow-y: auto;
    display: flex; flex-direction: column; gap: 0.6rem;
  }
  .pin-title-row { display: flex; align-items: center; justify-content: space-between; }
  .pin-title { font-size: var(--fs-xl); font-weight: 600; color: var(--text-med); }
  .pin-close {
    display: inline-flex; align-items: center; justify-content: center;
    width: 1.4rem; height: 1.4rem;
    background: var(--close-btn-danger-bg); border: 1px solid rgba(248,113,113,0.35);
    border-radius: 3px; color: var(--c-short); font-size: var(--fs-xl);
    line-height: 1; cursor: pointer; flex-shrink: 0; transition: background 0.1s;
  }
  .pin-close:hover { background: var(--close-btn-danger-bg-hover); }
  .pin-source {
    font-family: monospace; font-size: var(--fs-md); color: var(--text-lo);
  }
  .pin-source-acct { color: var(--text-muted); }
  .pin-state-hint {
    font-size: var(--fs-md); color: var(--text-muted); line-height: 1.35;
  }
  .pin-error { font-size: var(--fs-lg); color: #fb7185; }
  .pin-current {
    font-size: var(--fs-md); color: var(--text-med);
    background: var(--algo-cyan-bg-soft); border: 1px solid var(--algo-cyan-border-soft);
    border-radius: 5px; padding: 0.45rem 0.6rem;
  }
  .pin-current-id { color: var(--algo-cyan-text); font-weight: 700; font-family: monospace; }
  .pin-empty { font-size: var(--fs-md); color: var(--text-muted); font-style: italic; }
  .pin-list {
    display: flex; flex-direction: column; gap: 0.3rem;
    max-height: 14rem; overflow-y: auto;
  }
  .pin-candidate {
    display: flex; align-items: center; justify-content: space-between;
    gap: 0.5rem;
    padding: 0.4rem 0.55rem;
    border-radius: 5px;
    border: 1px solid color-mix(in srgb, var(--text-med) 15%, transparent);
    background: color-mix(in srgb, var(--text-med) 4%, transparent);
    color: var(--text-med);
    font-family: monospace; font-size: var(--fs-md);
    cursor: pointer;
    text-align: left;
  }
  .pin-candidate:hover:not(:disabled) {
    background: var(--algo-cyan-bg-soft);
    border-color: var(--algo-cyan-border-soft);
  }
  .pin-candidate:disabled { opacity: 0.5; cursor: default; }
  .pin-candidate-sym { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .pin-candidate-qty {
    font-variant-numeric: tabular-nums;
    color: var(--text-muted); flex-shrink: 0;
  }
  .pin-actions { display: flex; justify-content: flex-end; gap: 0.5rem; margin-top: 0.1rem; }
  .pin-cancel {
    font-size: var(--fs-lg); padding: 0.25rem 0.75rem; border-radius: 4px;
    border: 1px solid color-mix(in srgb, var(--text-med) 30%, transparent); background: transparent;
    color: var(--text-lo); cursor: pointer;
  }
  .pin-unpin {
    font-size: var(--fs-lg); padding: 0.25rem 0.75rem; border-radius: 4px;
    border: 1px solid var(--algo-red-border); background: var(--algo-red-bg);
    color: var(--c-short); cursor: pointer;
  }
  .pin-unpin:disabled { opacity: 0.5; cursor: default; }

  @media (max-width: 600px) {
    .pin-card { max-width: 100%; }
  }
</style>
