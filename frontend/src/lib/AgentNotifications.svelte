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
  import { chipsAsTextFromJson } from '$lib/logChips';
  import AgentFireModal from '$lib/AgentFireModal.svelte';
  import { soundMuted, playAgentBeep } from '$lib/sound';

  let open = $state(false);
  /** @type {object | null} — fire payload pinned to the modal */
  let _fireForModal = $state(null);
  /** @type {HTMLElement|null} */
  let panelEl = /** @type {HTMLElement|null} */ ($state(null));
  /** @type {HTMLElement|null} */
  let btnEl   = /** @type {HTMLElement|null} */ ($state(null));
  // Dynamic anchor — when the icon is in the LEFT half of the viewport
  // we anchor the panel's LEFT edge to the icon (panel extends right);
  // when on the RIGHT half we anchor the RIGHT edge (panel extends
  // left). Earlier the panel was hard-right-anchored, so on /admin/options
  // where the header is narrow + the icons sit in the left half of the
  // viewport, the panel extended off-screen to the left and the
  // operator couldn't read the agent log.
  let _anchorRight = $state(true);
  function _recomputeAnchor() {
    if (!btnEl || typeof window === 'undefined') return;
    const r = btnEl.getBoundingClientRect();
    _anchorRight = (r.left + r.right) / 2 > window.innerWidth / 2;
  }

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

  function _close() {
    open = false;
    // Mark seen on CLOSE — the badge stays visible while the panel
    // is open so the operator can read the items and still see the
    // unread count for context. Clearing on open made the number
    // vanish before the operator had a chance to register it.
    markAgentEventsSeen();
  }

  function onDocClick(/** @type {MouseEvent} */ e) {
    if (!open) return;
    const t = /** @type {Node} */ (e.target);
    if (panelEl?.contains(t) || btnEl?.contains(t)) return;
    _close();
  }

  function toggle() {
    if (open) {
      _close();
    } else {
      open = true;
      _recomputeAnchor();
    }
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

  // Build a fire-shaped object the AgentFireModal can render.
  // agent_events rows carry slightly different field names than the
  // live `agent_inapp_notify` WS payload (event_type vs tier, etc),
  // so we adapt before opening the modal.
  function openRow(/** @type {any} */ e) {
    /** @type {any} */
    let detail = e.detail;
    try { if (typeof detail === 'string') detail = JSON.parse(detail); } catch {}
    _fireForModal = {
      slug:      e.agent_slug || `agent#${e.agent_id}`,
      name:      e.agent_name || detail?.agent_name || (e.agent_slug || `agent#${e.agent_id}`),
      tier:      detail?.tier || (e.event_type === 'agent_action_error' ? 'critical' : 'info'),
      topic:     detail?.topic,
      condition: e.trigger_condition || detail?.condition || '',
      detail,
      when:      e.timestamp || '',
      sim_mode:  !!e.sim_mode,
      branch:    detail?.branch,
    };
    // Closing the bell first keeps focus on the modal — otherwise the
    // outside-click handler on this popover would also fire and the
    // operator sees a flicker. Opening a row counts as engagement,
    // so we mark events seen here too.
    _close();
  }
</script>

<span class="anb-wrap">
  <button type="button" bind:this={btnEl}
          class="anb-btn" class:anb-btn-active={open}
          title={unread ? `${unread} new agent event${unread > 1 ? 's' : ''}` : 'Agent log'}
          aria-haspopup="dialog" aria-expanded={open}
          onclick={toggle}>
    <!-- Heroicons "shield-check" outline (shield silhouette + inner
         check). Reads as a guardrail watching the book — the
         semantic match for the agent system (loss / expiry / chase
         rules that auto-fire on threat). Replaced the earlier
         cpu-chip which read as "AI" generically rather than "guard".
         Stroke + viewBox match the order bell so the two icons sit
         visually balanced side-by-side in the page header. -->
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
         stroke-linecap="round" stroke-linejoin="round" class="anb-icon">
      <path d="M9 12.75 11.25 15 15 9.75M12 2.714c-2.15 2.037-5.054 3.286-8.25 3.286h-.152a11.99 11.99 0 0 0-.598 3.75c0 5.592 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.57-.598-3.75H21c-3.196 0-6.1-1.249-8.25-3.286Z"/>
    </svg>
    {#if unread > 0}
      <span class="anb-badge">{unread > 99 ? '99+' : unread}</span>
    {/if}
  </button>

  {#if open}
    <div bind:this={panelEl}
         class="anb-panel"
         class:anb-panel-left={!_anchorRight}
         role="dialog" aria-label="Agent log">
      <div class="anb-head">
        <span class="anb-title">Agent Log</span>
        <span class="anb-head-spacer"></span>
        <button type="button" class="anb-mute"
                aria-pressed={$soundMuted}
                title={$soundMuted ? 'Sound off — click to enable' : 'Sound on — click to mute'}
                aria-label={$soundMuted ? 'Enable sound' : 'Mute sound'}
                onclick={() => {
                  const next = !$soundMuted;
                  soundMuted.set(next);
                  // When unmuting, play a tiny test chirp so the operator
                  // confirms sound is working + their browser has unlocked
                  // autoplay (first audible chirp also "primes" the
                  // AudioContext on Safari).
                  if (!next) playAgentBeep('info');
                }}>
          {#if $soundMuted}
            <!-- Speaker-off (muted) -->
            <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
              <path d="M3 5.5h2L8.5 3v10L5 10.5H3v-5z" fill="currentColor" />
              <path d="M11 5l4 6M15 5l-4 6" fill="none" stroke="currentColor"
                stroke-width="1.5" stroke-linecap="round" />
            </svg>
          {:else}
            <!-- Speaker-on (with sound waves) -->
            <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
              <path d="M3 5.5h2L8.5 3v10L5 10.5H3v-5z" fill="currentColor" />
              <path d="M11 6c.8.8.8 3.2 0 4M13 4.5c1.6 1.6 1.6 5.4 0 7"
                fill="none" stroke="currentColor"
                stroke-width="1.5" stroke-linecap="round" />
            </svg>
          {/if}
        </button>
        <button type="button" class="anb-close" aria-label="Close"
                onclick={_close}>×</button>
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
              <!-- Whole row is now a button: click opens the rich
                   AgentFireModal pinned to this event. The old
                   passive log-line behaviour is preserved for
                   read-only viewing — content + chips are unchanged. -->
              <button type="button" class="anb-row-btn"
                      onclick={() => openRow(e)}
                      title="Click for full detail">
                <div class="anb-row-meta">
                  <span class="anb-ts">{logTime(e.timestamp)}</span>
                  <span class="anb-kind anb-kind-{e.event_type}">{kindLabel(e.event_type)}</span>
                  {#if e.sim_mode}<span class="anb-sim">SIM</span>{/if}
                  <span class="anb-agent-id">#{e.agent_id}</span>
                </div>
                <!-- detail / trigger_condition are usually JSON objects.
                     chipsAsTextFromJson renders them as
                     `[metric:pnl, scope:total, op:<=, value:-50000]`. -->
                <div class="anb-msg">{chipsAsTextFromJson(e.detail) || chipsAsTextFromJson(e.trigger_condition) || ''}</div>
              </button>
            </li>
          {/each}
        </ul>
      {/if}
    </div>
  {/if}
</span>

{#if _fireForModal}
  <AgentFireModal fire={_fireForModal} onClose={() => { _fireForModal = null; }} />
{/if}

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
    background: rgba(167, 139, 250, 0.10);
    border: 1px solid rgba(167, 139, 250, 0.32);
    border-radius: 0.25rem;
    color: #a78bfa;
    cursor: pointer;
    transition: background-color 0.08s, color 0.08s, border-color 0.08s;
  }
  .anb-btn:hover {
    color: #c4b5fd;
    background: rgba(167, 139, 250, 0.22);
    border-color: rgba(167, 139, 250, 0.6);
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

  /* Default: anchor panel's RIGHT edge to the icon, panel extends
     LEFT. When `_anchorRight` is false (icon sits in the left half of
     the viewport) we flip via .anb-panel-left so the panel extends
     RIGHT instead — keeps it inside the viewport regardless of icon
     placement. */
  .anb-panel {
    position: absolute;
    top: calc(100% + 0.35rem);
    right: 0;
    left: auto;
    /* High z-index so the panel overlaps the OptionsPayoff chart (and
       any other absolutely-positioned page content). Was 45 — the
       payoff chart card's stacking context outranked it and the
       operator couldn't read the agent log on /admin/options. 9999
       matches the FullscreenButton backdrop level. */
    z-index: 9999;
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
  .anb-head-spacer { flex: 1 1 0; }
  /* Mute toggle — sits between the title and the × close button.
     Speaker-on icon is purple to match the agent palette; speaker-off
     drops to slate so the muted state reads at a glance. Persists to
     localStorage via $lib/sound (soundMuted store). */
  .anb-mute {
    width: 1.3rem; height: 1.3rem;
    background: transparent;
    border: 1px solid rgba(167, 139, 250, 0.35);
    border-radius: 0.2rem;
    color: #a78bfa;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 0;
    margin-right: 0.3rem;
    transition: background 0.12s, color 0.12s, border-color 0.12s;
  }
  .anb-mute:hover {
    background: rgba(167, 139, 250, 0.14);
    border-color: rgba(167, 139, 250, 0.6);
    color: #c4b5fd;
  }
  .anb-mute[aria-pressed="true"] {
    color: rgba(200, 216, 240, 0.5);
    border-color: rgba(200, 216, 240, 0.22);
  }
  .anb-mute[aria-pressed="true"]:hover {
    color: rgba(200, 216, 240, 0.85);
    border-color: rgba(200, 216, 240, 0.4);
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
    border-bottom: 1px solid rgba(255, 255, 255, 0.04);
  }
  .anb-row:last-child { border-bottom: 0; }
  /* Row button — full-row click target. Styled to look like the
     original flat <li> row but with a subtle hover affordance so
     operators discover it's interactive. */
  .anb-row-btn {
    width: 100%;
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    padding: 0.3rem 0.65rem;
    background: transparent;
    border: 0;
    color: inherit;
    font: inherit;
    text-align: left;
    cursor: pointer;
  }
  .anb-row-btn:hover {
    background: rgba(167, 139, 250, 0.06);
  }
  .anb-panel-left { left: 0; right: auto; }
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
