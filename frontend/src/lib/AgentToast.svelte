<!--
  AgentToast — listens on /ws/algo for `agent_inapp_notify` events
  and stacks toasts top-right. Each toast auto-dismisses after 8 s.
  Click a toast → opens AgentFireModal pinned to that fire.

  Mount ONCE in the algo layout. The component owns its own WS
  subscription, toast queue, and modal state — no props.

  Why a toast layer separately from the AgentNotifications bell:
   - bell is a passive log surface (operator opens it on demand)
   - toast is an INTERRUPT — same intent as Slack notifications.
  Both consume the same agent_events feed; the toast is gated by
  the `inapp` channel being enabled on the agent (the bell shows
  everything).
-->
<script>
  import { onMount, onDestroy } from 'svelte';
  import { createAlgoSocket } from '$lib/ws';
  import AgentFireModal from '$lib/AgentFireModal.svelte';
  import { playAgentBeep } from '$lib/sound';

  /** @typedef {{
   *   id: number,
   *   slug?: string, name?: string, tier?: string, topic?: string,
   *   condition?: string, detail?: any, when?: string,
   *   sim_mode?: boolean, branch?: string,
   * }} Toast */

  /** @type {Toast[]} */
  let toasts = $state([]);
  /** @type {Toast | null} */
  let activeFire = $state(null);
  /** @type {(() => void) | null} */
  let _unsub = null;
  let _nextId = 1;

  const TIER_BORDER = {
    critical: 'rgba(248,113,113,0.55)',
    high:     'rgba(251,191,36,0.55)',
    medium:   'rgba(125,211,252,0.55)',
    info:     'rgba(167,139,250,0.55)',
  };

  function tierBorder(/** @type {string|undefined} */ tier) {
    return TIER_BORDER[/** @type {keyof typeof TIER_BORDER} */ (tier || 'info')] || TIER_BORDER.info;
  }

  function dismiss(/** @type {number} */ id) {
    toasts = toasts.filter(t => t.id !== id);
  }

  function expand(/** @type {Toast} */ t) {
    activeFire = t;
    dismiss(t.id);
  }

  function closeModal() {
    activeFire = null;
  }

  onMount(() => {
    _unsub = createAlgoSocket((msg) => {
      if (msg?.event !== 'agent_inapp_notify') return;
      const id = _nextId++;
      const toast = { ...msg, id };
      // Newest on top; cap the stack at 5 so a runaway fire storm
      // doesn't paint the whole right edge of the viewport.
      toasts = [toast, ...toasts].slice(0, 5);
      // Tier-aware audio chirp — silent no-op when the operator
      // has muted via the speaker icon on the bell panel, or when
      // the browser's autoplay policy hasn't unlocked yet (first
      // user gesture flips it). See $lib/sound.
      playAgentBeep(msg?.tier);
      // Auto-dismiss after 8 s unless the operator interacts.
      setTimeout(() => dismiss(id), 8_000);
    });
  });

  onDestroy(() => {
    _unsub?.();
  });
</script>

{#if toasts.length > 0}
  <div class="atst-stack" aria-live="polite" aria-atomic="false">
    {#each toasts as t (t.id)}
      <!-- Outer wrapper is a div (not a button) so the dismiss × can
           be a real <button> inside without nesting buttons. The
           div carries role="button" + keyboard handlers + tabindex
           so it stays activatable. -->
      <div class="atst-toast atst-tier-{t.tier || 'info'}"
           role="button" tabindex="0"
           style="border-color: {tierBorder(t.tier)}"
           onclick={() => expand(t)}
           onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); expand(t); } }}
           title="Click for full detail · auto-dismisses in 8s">
        <div class="atst-head">
          <span class="atst-tier-pill atst-tier-{t.tier || 'info'}">
            {(t.tier || 'info').toUpperCase()}
          </span>
          {#if t.sim_mode}<span class="atst-sim">SIM</span>{/if}
          <button type="button" class="atst-x" aria-label="Dismiss"
                  onclick={(e) => { e.stopPropagation(); dismiss(t.id); }}>×</button>
        </div>
        <div class="atst-name">{t.name || t.slug || 'Agent fired'}</div>
        {#if t.condition}
          <div class="atst-cond">{t.condition}</div>
        {/if}
      </div>
    {/each}
  </div>
{/if}

{#if activeFire}
  <AgentFireModal fire={activeFire} onClose={closeModal} />
{/if}

<style>
  .atst-stack {
    position: fixed;
    top: 4rem;          /* clears the navbar */
    right: 0.75rem;
    z-index: 9997;       /* under modal (9998) + bell popup (9999) */
    display: flex;
    flex-direction: column;
    gap: 0.45rem;
    pointer-events: none; /* let underlying UI work; toasts re-enable per-element */
  }
  .atst-toast {
    pointer-events: auto;
    background: linear-gradient(180deg, rgba(39, 53, 82, 0.97), rgba(29, 42, 68, 0.97));
    border: 1px solid rgba(248,113,113,0.55);
    border-radius: 6px;
    padding: 0.5rem 0.6rem;
    width: min(20rem, 80vw);
    text-align: left;
    color: var(--algo-slate);
    font-family: var(--font-numeric);
    box-shadow: 0 8px 24px rgba(0,0,0,0.55);
    cursor: pointer;
    animation: atst-slide-in 0.18s ease-out both;
  }
  .atst-toast:hover {
    box-shadow: 0 10px 28px rgba(0,0,0,0.7);
    transform: translateX(-2px);
    transition: transform 0.12s ease;
  }
  .atst-head {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    margin-bottom: 0.25rem;
  }
  .atst-tier-pill {
    padding: 0.05rem 0.4rem;
    border-radius: 3px;
    border: 1px solid;
    font-size: var(--fs-2xs);
    font-weight: 800;
    letter-spacing: 0.08em;
  }
  .atst-tier-critical { color: var(--c-short); background: var(--algo-red-bg);   border-color: var(--algo-red-border);   }
  .atst-tier-high     { color: var(--c-action); background: var(--algo-amber-bg); border-color: var(--algo-amber-border); }
  .atst-tier-medium   { color: #7dd3fc; background: var(--algo-sky-bg);   border-color: var(--algo-sky-border);   }
  .atst-tier-info     { color: #a78bfa; background: rgba(167,139,250,0.14); border-color: rgba(167,139,250,0.55); }
  .atst-sim {
    padding: 0 0.32rem;
    border-radius: 3px;
    background: rgba(251,113,133,0.16);
    color: #fb7185;
    border: 1px solid rgba(251,113,133,0.42);
    font-size: var(--fs-2xs);
    font-weight: 800;
  }
  .atst-x {
    margin-left: auto;
    color: rgba(200,216,240,0.55);
    font-size: var(--fs-xl);
    line-height: 1;
    padding: 0 0.18rem;
    cursor: pointer;
    border-radius: 2px;
    background: transparent;
    border: 0;
    font-family: inherit;
  }
  .atst-x:hover { color: var(--c-short); background: rgba(255,255,255,0.06); }
  .atst-name {
    font-size: var(--fs-lg);
    font-weight: 700;
    color: #e5edf7;
    line-height: 1.25;
    overflow-wrap: anywhere;
  }
  .atst-cond {
    margin-top: 0.18rem;
    font-size: var(--fs-xs);
    color: rgba(200,216,240,0.7);
    line-height: 1.3;
    overflow: hidden;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
  }
  @keyframes atst-slide-in {
    from { transform: translateX(0.5rem); opacity: 0; }
    to   { transform: translateX(0); opacity: 1; }
  }
  @media (prefers-reduced-motion: reduce) {
    .atst-toast { animation: none; }
  }
</style>
