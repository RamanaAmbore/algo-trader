<!--
  AgentFireModal — rich overlay popup for a single agent fire.

  Same visual idiom as OrderTicket (gradient navy modal, fixed
  overlay, ESC + click-outside to close), restyled for an
  alerting context (red/amber accent for critical/high tier,
  blue for info, sim/branch tags).

  Props:
    fire   — the inapp_notify payload (or any object with the
             same shape: slug, name, tier, topic, condition,
             detail, when, sim_mode, branch).
    onClose — close handler from the caller.

  Used by:
    - AgentToast (click → opens modal at that fire)
    - AgentNotifications bell rows (click → opens modal)
-->
<script>
  import { onMount, onDestroy } from 'svelte';
  import { goto } from '$app/navigation';
  import { chipsAsTextFromJson } from '$lib/logChips';

  /** @type {{
   *   fire: {
   *     slug?: string, name?: string, tier?: string, topic?: string,
   *     condition?: string, detail?: any, when?: string,
   *     sim_mode?: boolean, branch?: string,
   *   },
   *   onClose: () => void,
   * }} */
  let { fire, onClose } = $props();

  const TIER_PALETTE = {
    critical: { color: '#f87171', bg: 'rgba(248,113,113,0.15)', border: 'rgba(248,113,113,0.45)' },
    high:     { color: '#fbbf24', bg: 'rgba(251,191,36,0.15)',  border: 'rgba(251,191,36,0.45)' },
    medium:   { color: '#7dd3fc', bg: 'rgba(125,211,252,0.15)', border: 'rgba(125,211,252,0.45)' },
    info:     { color: '#a78bfa', bg: 'rgba(167,139,250,0.15)', border: 'rgba(167,139,250,0.45)' },
  };
  const tier = $derived(fire?.tier || 'info');
  const palette = $derived(TIER_PALETTE[/** @type {keyof typeof TIER_PALETTE} */ (tier)] || TIER_PALETTE.info);
  const detailText = $derived(chipsAsTextFromJson(fire?.detail));

  function onEsc(/** @type {KeyboardEvent} */ e) {
    if (e.key === 'Escape') onClose();
  }
  onMount(() => {
    if (typeof document !== 'undefined') {
      document.addEventListener('keydown', onEsc);
    }
  });
  onDestroy(() => {
    if (typeof document !== 'undefined') {
      document.removeEventListener('keydown', onEsc);
    }
  });

  function openAgent() {
    if (fire?.slug) {
      goto(`/automation?q=${encodeURIComponent(fire.slug)}`);
      onClose();
    }
  }
</script>

<!-- Overlay close fires only when the click target IS the overlay
     element (not a bubbled child click). That removes the need for
     a stopPropagation onclick on the modal wrapper, keeping the
     inner div free of interaction listeners (a11y). -->
<div class="afm-overlay" role="dialog" aria-modal="true" aria-label="Agent fire"
     tabindex="-1"
     onclick={(e) => { if (e.target === e.currentTarget) onClose(); }}
     onkeydown={(e) => { if (e.key === 'Enter' && e.target === e.currentTarget) onClose(); }}>
  <div class="afm-modal" role="document"
       style="border-color: {palette.border}">
    <div class="afm-header">
      <div>
        <div class="afm-tier-row">
          <span class="afm-tier"
                style="color: {palette.color}; background: {palette.bg}; border-color: {palette.border}">
            {(tier || 'info').toUpperCase()}
          </span>
          {#if fire?.sim_mode}
            <span class="afm-sim">SIMULATOR</span>
          {/if}
          {#if fire?.branch && fire.branch !== 'main'}
            <span class="afm-branch">[{fire.branch}]</span>
          {/if}
          {#if fire?.topic}
            <span class="afm-topic">{fire.topic}</span>
          {/if}
        </div>
        <div class="afm-name">{fire?.name || fire?.slug || 'Agent fired'}</div>
        {#if fire?.when}
          <div class="afm-when">{fire.when}</div>
        {/if}
      </div>
      <button type="button" class="afm-close" title="Close" aria-label="Close"
              onclick={onClose}>×</button>
    </div>

    {#if fire?.condition}
      <div class="afm-section">
        <div class="afm-label">Condition</div>
        <div class="afm-body">{fire.condition}</div>
      </div>
    {/if}

    {#if detailText}
      <div class="afm-section">
        <div class="afm-label">Matched</div>
        <div class="afm-body afm-detail">{detailText}</div>
      </div>
    {/if}

    <div class="afm-actions">
      <button type="button" class="afm-btn afm-btn-primary" onclick={openAgent}
              disabled={!fire?.slug}>
        Open in /automation
      </button>
      <button type="button" class="afm-btn" onclick={onClose}>Acknowledge</button>
    </div>
  </div>
</div>

<style>
  .afm-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.55);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 9998;
    padding: 1rem;
  }
  .afm-modal {
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    border: 1px solid rgba(248,113,113,0.45);
    border-radius: 8px;
    padding: 0.85rem 1rem;
    width: min(30rem, calc(100vw - 2rem));
    max-height: calc(100vh - 2rem);
    overflow-y: auto;
    color: var(--algo-slate);
    font-family: ui-monospace, monospace;
    box-shadow: 0 12px 32px rgba(0,0,0,0.6);
  }
  .afm-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 0.5rem;
    padding-bottom: 0.55rem;
    border-bottom: 1px solid rgba(255,255,255,0.08);
    margin-bottom: 0.6rem;
  }
  .afm-tier-row {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    flex-wrap: wrap;
    margin-bottom: 0.3rem;
  }
  .afm-tier {
    padding: 0.05rem 0.4rem;
    border-radius: 3px;
    border: 1px solid;
    font-size: 0.55rem;
    font-weight: 800;
    letter-spacing: 0.08em;
  }
  .afm-sim {
    padding: 0.05rem 0.35rem;
    border-radius: 3px;
    background: rgba(251,113,133,0.16);
    color: #fb7185;
    border: 1px solid rgba(251,113,133,0.42);
    font-size: 0.5rem;
    font-weight: 800;
    letter-spacing: 0.06em;
  }
  .afm-branch {
    font-size: 0.55rem;
    color: #fbbf24;
    font-weight: 700;
  }
  .afm-topic {
    font-size: 0.55rem;
    color: #a3b9d0;
    background: rgba(255,255,255,0.06);
    padding: 0.05rem 0.35rem;
    border-radius: 3px;
  }
  .afm-name {
    font-size: 0.9rem;
    font-weight: 700;
    color: #e5edf7;
    line-height: 1.2;
  }
  .afm-when {
    font-size: 0.6rem;
    color: rgba(200,216,240,0.65);
    margin-top: 0.2rem;
  }
  .afm-close {
    background: transparent;
    border: 1px solid rgba(255,255,255,0.15);
    color: var(--algo-slate);
    width: 1.55rem;
    height: 1.55rem;
    border-radius: 3px;
    cursor: pointer;
    font-size: 1rem;
    line-height: 1;
    flex-shrink: 0;
  }
  .afm-close:hover { border-color: #f87171; color: #f87171; }

  .afm-section {
    margin-bottom: 0.6rem;
  }
  .afm-label {
    font-size: 0.55rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: rgba(200,216,240,0.55);
    margin-bottom: 0.15rem;
  }
  .afm-body {
    font-size: 0.7rem;
    color: #e5edf7;
    line-height: 1.4;
    overflow-wrap: anywhere;
  }
  .afm-detail {
    background: rgba(0,0,0,0.22);
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 4px;
    padding: 0.45rem 0.55rem;
    font-size: 0.65rem;
  }

  .afm-actions {
    display: flex;
    gap: 0.45rem;
    justify-content: flex-end;
    margin-top: 0.7rem;
    padding-top: 0.55rem;
    border-top: 1px solid rgba(255,255,255,0.08);
  }
  .afm-btn {
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.18);
    color: var(--algo-slate);
    padding: 0.35rem 0.7rem;
    border-radius: 3px;
    font-size: 0.7rem;
    font-weight: 700;
    cursor: pointer;
  }
  .afm-btn:hover {
    background: rgba(255,255,255,0.12);
    border-color: rgba(255,255,255,0.3);
  }
  .afm-btn:disabled {
    cursor: not-allowed;
    opacity: 0.5;
  }
  .afm-btn-primary {
    background: rgba(251,191,36,0.18);
    border-color: rgba(251,191,36,0.55);
    color: #fbbf24;
  }
  .afm-btn-primary:hover {
    background: rgba(251,191,36,0.28);
    border-color: rgba(251,191,36,0.75);
  }
</style>
