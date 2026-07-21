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
  import { goto } from '$app/navigation';
  import { chipsAsTextFromJson } from '$lib/logChips';
  import ModalShell from '$lib/ModalShell.svelte';

  /** @type {{
   *   fire: {
   *     slug?: string, name?: string, tier?: string, topic?: string,
   *     condition?: string, detail?: any, when?: string,
   *     sim_mode?: boolean, branch?: string,
   *   },
   *   onClose: () => void,
   * }} */
  let { fire, onClose } = $props();

  // Canonical tier palette — bg 0.10 (green/red) / 0.14 (amber/sky/violet),
  // border 0.55. Matches AgentToast tier pills so the modal + toast read
  // identically. Pre-fix bg was 0.15 + border 0.45 (off-palette).
  const TIER_PALETTE = {
    critical: { color: 'var(--c-short)', bg: 'var(--c-short-10)', border: 'rgba(248,113,113,0.55)' },
    high:     { color: 'var(--c-action)', bg: 'var(--c-action-14)',  border: 'rgba(251,191,36,0.55)' },
    medium:   { color: '#7dd3fc', bg: 'rgba(125,211,252,0.14)', border: 'rgba(125,211,252,0.55)' },
    info:     { color: '#a78bfa', bg: 'rgba(167,139,250,0.14)', border: 'rgba(167,139,250,0.55)' },
  };
  const tier = $derived(fire?.tier || 'info');
  const palette = $derived(TIER_PALETTE[/** @type {keyof typeof TIER_PALETTE} */ (tier)] || TIER_PALETTE.info);
  const detailText = $derived(chipsAsTextFromJson(fire?.detail));

  function openAgent() {
    if (fire?.slug) {
      goto(`/automation?q=${encodeURIComponent(fire.slug)}`);
      onClose();
    }
  }
</script>

<ModalShell open={true} {onClose} zIndex={9998} clickOutside={true} ariaLabel="Agent fire details">
  <div class="afm-modal algo-modal" role="document"
       style="border-color: {palette.border}">
    <div class="afm-header canonical-modal-header">
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
</ModalShell>

<style>
  .afm-modal {
    /* Composes .algo-modal chrome. Overrides:
       - background: elevated navy gradient (lifts above already-dark
         page surface, matches ConfirmModal elevation).
       - border: default red placeholder — runtime inline style=
         `border-color: {palette.border}` swaps to the actual tier
         colour once the fire tier is known.
       - border-radius: 8px (chunkier than the 6px canonical — this
         modal is more emotional so operator wanted softer corners).
       - overflow: auto (algo-modal sets hidden) — long agent-fire
         reasoning can scroll. */
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    border: 1px solid rgba(248,113,113,0.45);
    border-radius: 8px;
    padding: 0.85rem 1rem;
    width: min(30rem, calc(100vw - 2rem));
    max-height: calc(100vh - 2rem);
    overflow-y: auto;
    color: var(--algo-slate);
    font-family: var(--font-numeric);
  }
  /* afm-header adopts canonical-modal-header gradient via class in markup.
     Local overrides: align-items (flex-start for multi-line content),
     justify-content (title-block + close button at opposite ends),
     margin-bottom (gap before body sections). */
  .afm-header {
    align-items: flex-start;
    justify-content: space-between;
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
    font-size: var(--fs-xs);
    font-weight: 800;
    letter-spacing: 0.08em;
  }
  .afm-sim {
    padding: 0.05rem 0.35rem;
    border-radius: 3px;
    background: rgba(251,113,133,0.16);
    color: var(--algo-rose);
    border: 1px solid rgba(251,113,133,0.42);
    font-size: var(--fs-2xs);
    font-weight: 800;
    letter-spacing: 0.06em;
  }
  .afm-branch {
    font-size: var(--fs-xs);
    color: var(--algo-amber);
    font-weight: 700;
  }
  .afm-topic {
    font-size: var(--fs-xs);
    color: var(--algo-muted);
    background: rgba(255,255,255,0.06);
    padding: 0.05rem 0.35rem;
    border-radius: 3px;
  }
  .afm-name {
    font-size: var(--fs-xl);
    font-weight: 700;
    color: var(--algo-slate);
    line-height: 1.2;
  }
  .afm-when {
    font-size: var(--fs-sm);
    color: rgba(200,216,240,0.65);
    margin-top: 0.2rem;
  }
  .afm-close {
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
    background: transparent;
    cursor: pointer;
    transition: background 0.1s;
    flex-shrink: 0;
  }
  .afm-close:hover { background: rgba(248, 113, 113, 0.15); }

  .afm-section {
    margin-bottom: 0.6rem;
  }
  .afm-label {
    font-size: var(--fs-xs);
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: rgba(200,216,240,0.55);
    margin-bottom: 0.15rem;
  }
  .afm-body {
    font-size: var(--fs-lg);
    color: var(--algo-slate);
    line-height: 1.4;
    overflow-wrap: anywhere;
  }
  .afm-detail {
    background: rgba(0,0,0,0.22);
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 4px;
    padding: 0.45rem 0.55rem;
    font-size: var(--fs-md);
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
    font-size: var(--fs-lg);
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
    background: var(--algo-amber-bg);
    border-color: var(--algo-amber-border);
    color: var(--algo-amber);
  }
  .afm-btn-primary:hover {
    background: var(--algo-amber-bg-strong);
    border-color: rgba(251,191,36,0.75);
  }
</style>
