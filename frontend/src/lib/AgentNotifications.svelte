<!--
  AgentNotifications — sibling of OrderNotifications, scoped to
  agent fires + action success/error events. Same shape: bell icon
  with unread badge, click-to-open popover panel listing the recent
  agent log. Anchored after the OrderNotifications bell in every
  algo page header.

  Data source: global agentEventsStore polled by stores.js every 8 s
  from GET /api/agents/events/recent?n=50. lastSeen ts stored in
  localStorage under a separate key so the agent bell's unread
  count moves independently of the order bell's.

  Event types rendered: agent_fire / agent_action_success /
  agent_action_error / agent_state / agent_dryrun (each gets its
  own colour-coded chip — see CSS below).
-->
<script>
  import { onMount, onDestroy } from 'svelte';
  import {
    agentEventsStore, agentUnreadCount, markAgentEventsSeen,
    startAgentEventsPoller, logTime,
  } from '$lib/stores';

  let open = $state(false);
  /** @type {HTMLElement|null} */
  let panelEl = /** @type {HTMLElement|null} */ ($state(null));
  /** @type {HTMLElement|null} */
  let btnEl   = /** @type {HTMLElement|null} */ ($state(null));

  let events = $state(/** @type {any[]} */ ([]));
  let unread = $state(0);
  /** @type {(() => void) | null} */
  let _unsubEvents = null;
  /** @type {(() => void) | null} */
  let _unsubUnread = null;

  onMount(() => {
    startAgentEventsPoller();
    _unsubEvents = agentEventsStore.subscribe(v => { events = v || []; });
    _unsubUnread = agentUnreadCount.subscribe(v => { unread = v || 0; });
    if (typeof document !== 'undefined') {
      document.addEventListener('click', onDocClick, true);
    }
  });

  onDestroy(() => {
    _unsubEvents?.();
    _unsubUnread?.();
    if (typeof document !== 'undefined') {
      document.removeEventListener('click', onDocClick, true);
    }
  });

  function onDocClick(/** @type {MouseEvent} */ e) {
    if (!open) return;
    const t = /** @type {Node} */ (e.target);
    if (panelEl?.contains(t) || btnEl?.contains(t)) return;
    open = false;
  }

  function toggle() {
    open = !open;
    if (open) markAgentEventsSeen();
  }

  // agent_events endpoint returns newest-first already; just slice
  // to the last 30 for the panel.
  const display = $derived((events || []).slice(0, 30));

  const KIND_LABEL = {
    agent_fire:           'FIRED',
    agent_action_success: 'ACTION OK',
    agent_action_error:   'ACTION ERR',
    agent_state:          'STATE',
    agent_dryrun:         'DRY-RUN',
  };
  /** @param {string} k */
  function kindLabel(k) { return /** @type {any} */ (KIND_LABEL)[k] || (k || '').toUpperCase(); }
</script>

<span class="anb-wrap">
  <button type="button" bind:this={btnEl}
          class="anb-btn" class:anb-btn-active={open}
          title={unread ? `${unread} new agent event${unread > 1 ? 's' : ''}` : 'Agent log'}
          aria-haspopup="dialog" aria-expanded={open}
          onclick={toggle}>
    <!-- Heroicons "cpu-chip" outline — represents the rule-evaluation
         engine. Distinct enough from the bell icon that the operator
         can tell the two bells apart at a glance even without the
         tooltip. -->
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
         stroke-linecap="round" stroke-linejoin="round" class="anb-icon">
      <path d="M8.25 7.5V6.108c0-1.135.845-2.098 1.976-2.192.373-.03.748-.057 1.123-.08M15.75 7.5V6.108c0-1.135-.845-2.098-1.976-2.192a48.424 48.424 0 0 0-1.123-.08M15.75 7.5h-7.5M21 16.5h-5.25m5.25 0v-5.25M3 16.5h5.25m-5.25 0v-5.25M3 11.25h5.25M21 11.25h-5.25m-3 0V7.5m0 8.25v-3.75m-3 0V7.5m0 0H8.25M12 7.5h3.75"/>
      <rect x="6.75" y="6.75" width="10.5" height="10.5" rx="1.5"/>
    </svg>
    {#if unread > 0}
      <span class="anb-badge">{unread > 99 ? '99+' : unread}</span>
    {/if}
  </button>

  {#if open}
    <div bind:this={panelEl} class="anb-panel" role="dialog" aria-label="Agent log">
      <div class="anb-head">
        <span class="anb-title">Agent Log</span>
        <button type="button" class="anb-close" aria-label="Close"
                onclick={() => { open = false; }}>×</button>
      </div>
      {#if display.length === 0}
        <div class="anb-empty">No agent events yet.</div>
      {:else}
        <ul class="anb-list">
          {#each display as e (e.id)}
            <!-- Two-row layout per event: top row carries timestamp +
                 kind chip + SIM marker; the message wraps under them
                 at full panel width. Previously a 4-column grid
                 squeezed the message into ~10 chars on a 22 rem
                 popover; the operator couldn't read fire-condition
                 detail without expanding. -->
            <li class="anb-row anb-row-{e.event_type}">
              <div class="anb-row-meta">
                <span class="anb-ts">{logTime(e.timestamp)}</span>
                <span class="anb-kind anb-kind-{e.event_type}">{kindLabel(e.event_type)}</span>
                {#if e.sim_mode}<span class="anb-sim">SIM</span>{/if}
                <span class="anb-agent-id">#{e.agent_id}</span>
              </div>
              <div class="anb-msg">{e.detail || e.trigger_condition || ''}</div>
            </li>
          {/each}
        </ul>
      {/if}
    </div>
  {/if}
</span>

<style>
  /* Mirrors OrderNotifications sizing so the two bells sit flush
     side-by-side. Single set of class names (anb-*) keeps the
     selector cascade isolated from the order bell's onb-*. */
  /* See OrderNotifications for why align-self: center is needed — the
     page-header parent uses align-items: baseline, which leaves the
     icon button visually low relative to the timestamp text baseline. */
  .anb-wrap { position: relative; display: inline-flex; align-items: center; align-self: center; flex-shrink: 0; }
  .anb-btn {
    position: relative;
    display: inline-flex; align-items: center; justify-content: center;
    width: 1.5rem; height: 1.5rem;
    padding: 0;
    background: transparent;
    border: 1px solid transparent;
    border-radius: 0.25rem;
    color: rgba(200, 216, 240, 0.78);
    cursor: pointer;
    transition: background-color 0.08s, color 0.08s, border-color 0.08s;
  }
  .anb-btn:hover {
    color: #a78bfa;
    background: rgba(167, 139, 250, 0.10);
    border-color: rgba(167, 139, 250, 0.32);
  }
  .anb-btn-active {
    color: #a78bfa;
    background: rgba(167, 139, 250, 0.16);
    border-color: rgba(167, 139, 250, 0.5);
  }
  .anb-icon { width: 0.95rem; height: 0.95rem; }
  .anb-badge {
    position: absolute;
    top: -2px; right: -3px;
    min-width: 0.85rem; height: 0.85rem;
    padding: 0 0.18rem;
    background: #a78bfa;
    color: #fff;
    border-radius: 999px;
    font-size: 0.5rem;
    font-weight: 800;
    line-height: 0.85rem;
    font-family: ui-monospace, monospace;
    border: 1px solid rgba(13, 21, 38, 0.85);
    display: inline-flex; align-items: center; justify-content: center;
  }

  .anb-panel {
    position: absolute;
    top: calc(100% + 0.35rem);
    right: 0;
    z-index: 45;
    width: min(24rem, 92vw);
    max-height: 70vh;
    overflow-y: auto;
    background: rgba(13, 21, 38, 0.97);
    border: 1px solid rgba(167, 139, 250, 0.4);
    border-radius: 0.4rem;
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.55);
    color: #e5edf7;
    font-family: ui-monospace, monospace;
  }
  .anb-head {
    display: flex; align-items: center; justify-content: space-between;
    padding: 0.45rem 0.65rem 0.35rem;
    border-bottom: 1px solid rgba(167, 139, 250, 0.22);
  }
  .anb-title {
    font-size: 0.65rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: #c4b5fd;
  }
  .anb-close {
    width: 1.2rem; height: 1.2rem;
    background: transparent; border: 0;
    color: rgba(200, 216, 240, 0.72);
    cursor: pointer;
    font-size: 0.95rem; line-height: 1;
    border-radius: 0.2rem;
  }
  .anb-close:hover { color: #fff; background: rgba(255, 255, 255, 0.08); }
  .anb-empty {
    padding: 0.9rem 0.65rem;
    font-size: 0.65rem;
    color: rgba(200, 216, 240, 0.55);
    text-align: center;
  }
  .anb-list { list-style: none; margin: 0; padding: 0.2rem 0; }
  /* Each event is now a vertical stack: a compact meta row (ts + kind
     + sim + agent_id) followed by the message at full popover width.
     Lets long fire-condition messages and action error details wrap
     naturally instead of being squeezed into a narrow grid column. */
  .anb-row {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    padding: 0.3rem 0.65rem;
    border-bottom: 1px solid rgba(255, 255, 255, 0.04);
  }
  .anb-row:last-child { border-bottom: 0; }
  .anb-row-meta {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    flex-wrap: wrap;
  }
  .anb-agent-id {
    color: rgba(200, 216, 240, 0.4);
    font-size: 0.5rem;
    font-family: ui-monospace, monospace;
    margin-left: auto;
  }
  .anb-ts {
    color: rgba(200, 216, 240, 0.55);
    font-size: 0.55rem;
    white-space: nowrap;
  }
  .anb-kind {
    padding: 0.02rem 0.32rem;
    border-radius: 0.2rem;
    font-size: 0.5rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    white-space: nowrap;
    border: 1px solid;
  }
  .anb-kind-agent_fire           { color: #fbbf24; background: rgba(251,191,36,0.12);  border-color: rgba(251,191,36,0.4); }
  .anb-kind-agent_action_success { color: #4ade80; background: rgba(74,222,128,0.12);  border-color: rgba(74,222,128,0.4); }
  .anb-kind-agent_action_error   { color: #f87171; background: rgba(248,113,113,0.12); border-color: rgba(248,113,113,0.4); }
  .anb-kind-agent_state          { color: #7dd3fc; background: rgba(56,189,248,0.12);  border-color: rgba(56,189,248,0.4); }
  .anb-kind-agent_dryrun         { color: #c8d8f0; background: rgba(200,216,240,0.08); border-color: rgba(200,216,240,0.3); }

  /* Simulator-mode marker so the operator can tell sim fires from
     real fires at a glance. */
  .anb-sim {
    padding: 0 0.28rem;
    border-radius: 0.18rem;
    background: rgba(251, 113, 133, 0.16);
    color: #fb7185;
    border: 1px solid rgba(251, 113, 133, 0.42);
    font-size: 0.45rem;
    font-weight: 800;
    letter-spacing: 0.06em;
  }

  .anb-msg {
    color: #e5edf7;
    overflow-wrap: anywhere;
    font-size: 0.6rem;
    line-height: 1.35;
    /* Indent slightly so the message visually sits as a continuation
       of the meta row, not a fresh row. Empty messages (rare) collapse
       to zero height. */
    padding-left: 0.05rem;
  }
  .anb-msg:empty { display: none; }
</style>
