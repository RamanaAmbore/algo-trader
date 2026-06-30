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
   * Poll: 30 s via visibleInterval. NOT market-gated — auth breaks
   * happen during closed hours too. Polling pauses when the modal is
   * closed AND no badge consumer needs the data; restarts on open.
   */
  import { onMount, onDestroy } from 'svelte';
  import { visibleInterval } from '$lib/stores';
  import { fetchBrokerHealth } from '$lib/api';

  /** Bindable: parent (algo layout) toggles this from the 5/5 chip. */
  let { open = $bindable(false) } = $props();

  /** @type {Array<{account:string,broker:string,state:string,reason:string,last_good_at:string|null,last_check_at:string|null}>} */
  let accounts = $state([]);
  let loading  = $state(false);

  let _teardown = () => {};

  async function load() {
    if (loading) return;
    loading = true;
    try {
      const data = await fetchBrokerHealth();
      accounts = data?.accounts ?? [];
    } catch (_) {
      // Silently suppress — badge disappears when API unreachable.
    } finally {
      loading = false;
    }
  }

  // Lazy polling: load once on mount (so the chip-driven toggle can
  // open instantly with data); then poll every 30 s ONLY while the modal
  // is open. Closed-modal background polling would burn ~120 req/hr
  // forever with no consumer reading the result.
  onMount(() => { load(); });
  $effect(() => {
    if (!open) {
      _teardown();
      _teardown = () => {};
      return;
    }
    load();
    _teardown = visibleInterval(load, 30_000);
  });
  onDestroy(() => { _teardown(); });

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

{#if open}
  <!-- Modal overlay -->
  <div class="bh-overlay" role="presentation" onclick={() => open = false}></div>
  <div class="bh-modal" role="dialog" aria-label="Broker auth health">
    <div class="bh-modal-header">
      <span class="bh-modal-title">Broker Auth Health</span>
      <button class="bh-close" onclick={() => open = false} aria-label="Close">
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
          <path d="M1 1l10 10M11 1L1 11" stroke="currentColor" stroke-width="1.8"
                stroke-linecap="round"/>
        </svg>
      </button>
    </div>
    <div class="bh-modal-body">
      {#each accounts as acct (acct.account)}
        <div class="bh-row">
          <span class="bh-row-dot bh-row-dot-{acct.state}" aria-hidden="true"></span>
          <span class="bh-row-account">{acct.account}</span>
          <span class="bh-row-broker">{acct.broker}</span>
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
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
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
    color: #4ade80;
    background: rgba(74, 222, 128, 0.10);
    border-color: rgba(74, 222, 128, 0.45);
  }
  .bh-badge-amber {
    color: #fbbf24;
    background: rgba(251, 191, 36, 0.10);
    border-color: rgba(251, 191, 36, 0.45);
  }
  .bh-badge-red {
    color: #f87171;
    background: rgba(248, 113, 113, 0.10);
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
  .bh-label { font-size: 0.58rem; }

  /* ── Modal overlay ── */
  .bh-overlay {
    position: fixed;
    inset: 0;
    z-index: 9990;
    background: transparent;
  }
  .bh-modal {
    position: fixed;
    top: 3.2rem;
    right: 0.5rem;
    z-index: 9991;
    background: #0f172a;
    border: 1px solid rgba(148, 163, 184, 0.18);
    border-radius: 0.6rem;
    width: min(96vw, 680px);
    max-height: min(90vh, 480px);
    display: flex;
    flex-direction: column;
    overflow: hidden;
    box-shadow: 0 8px 32px rgba(0,0,0,0.45);
  }

  /* ── Modal header ── */
  .bh-modal-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.7rem 1rem;
    border-bottom: 1px solid rgba(148, 163, 184, 0.12);
  }
  .bh-modal-title {
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    color: #cbd5e1;
    text-transform: uppercase;
  }
  .bh-close {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 1.4rem;
    height: 1.4rem;
    border-radius: 0.3rem;
    color: #64748b;
    cursor: pointer;
    outline: none;
    background: transparent;
    border: none;
    transition: color 0.1s;
  }
  .bh-close:hover { color: #cbd5e1; }

  /* ── Modal body ── */
  .bh-modal-body {
    flex: 1;
    overflow-y: auto;
    padding: 0.5rem 0;
  }
  .bh-row {
    display: grid;
    grid-template-columns: 0.6rem 5.5rem 3.5rem 3.5rem 1fr 5rem;
    align-items: center;
    gap: 0.5rem;
    padding: 0.45rem 1rem;
    border-bottom: 1px solid rgba(148, 163, 184, 0.07);
    font-size: 0.68rem;
    font-family: ui-monospace, monospace;
  }
  .bh-row:last-child { border-bottom: none; }

  .bh-row-dot {
    width: 0.45rem;
    height: 0.45rem;
    border-radius: 50%;
  }
  .bh-row-dot-green  { background: #4ade80; }
  .bh-row-dot-amber  { background: #fbbf24; }
  .bh-row-dot-red    { background: #f87171; }

  .bh-row-account {
    color: #e2e8f0;
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .bh-row-broker {
    color: #94a3b8;
    text-transform: uppercase;
    font-size: 0.6rem;
  }
  .bh-row-state {
    font-size: 0.58rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    border-radius: 9999px;
    padding: 0.05rem 0.4rem;
    text-align: center;
  }
  .bh-row-state-green { color: #4ade80; background: rgba(74,222,128,0.10); }
  .bh-row-state-amber { color: #fbbf24; background: rgba(251,191,36,0.10); }
  .bh-row-state-red   { color: #f87171; background: rgba(248,113,113,0.10); }

  .bh-row-reason {
    color: #64748b;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 0.63rem;
  }
  .bh-row-ts {
    color: #475569;
    font-size: 0.6rem;
    text-align: right;
    white-space: nowrap;
  }

  .bh-empty {
    color: #64748b;
    font-size: 0.68rem;
    text-align: center;
    padding: 1.5rem 1rem;
  }

  /* ── Modal footer ── */
  .bh-modal-footer {
    border-top: 1px solid rgba(148, 163, 184, 0.12);
    padding: 0.5rem 1rem;
  }
  .bh-footer-note {
    font-size: 0.58rem;
    color: #475569;
    font-family: ui-monospace, monospace;
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
    .bh-row {
      grid-template-columns: 0.6rem 4rem 2.5rem 3rem 1fr 4rem;
      gap: 0.35rem;
      font-size: 0.62rem;
    }
  }
</style>
