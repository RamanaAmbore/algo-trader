<!--
  ToastContainer.svelte — fixed-position stack that renders active toasts.

  Mount ONCE in the algo layout. Toasts are tier 9 (the absolute top) of
  the app-wide z-index scale documented in app.css — `var(--z-toast)`,
  currently 21000 — above every modal tier (--z-command, --z-modal-nested,
  --z-modal-in-command, --z-modal-critical), the shortcut cheatsheet, and
  AgentToast's own agent-alert tier. Nothing should ever be able to hide
  a toast. (2026-09-30 stacking-defect audit, Wave A — was z-index 80,
  which sat below essentially everything; see app.css's z-index scale
  comment block for the full tier ladder.)

  Positioning:
    Desktop: top: 4.5rem, right: 1rem (clears the 3.8rem navbar)
    Mobile:  top: 3.8rem, right: 0.5rem, max-width: calc(100vw - 1rem)

  The container itself is pointer-events: none so underlying UI stays
  reachable; each individual Toast re-enables pointer-events.

  Mobile burst cap (2026-10, fixes toast stack burying the page on
  narrow viewports): on mobile the container spans nearly full width
  by design (left:0.5rem/right:0.5rem, for text readability), so a
  burst of MAX_TOASTS=5 toasts firing in the same tick (e.g. 5
  template-attach-fail warnings from one backstop poll) stacked at
  full height for their whole 5000ms lifetime covered whatever was
  underneath (confirmed: account/expiry picker + payoff chart on the
  Derivatives page). Every toast still mounts and queues normally —
  all 5 timers run exactly as before — only the RENDERING is capped:
  past `MOBILE_VISIBLE` (2), older toasts get `.rbq-toast-overflow`
  and are hidden via `display:none` inside the existing
  `max-width:600px` media query only (desktop is untouched). Hiding
  via CSS rather than slicing the `{#each}` array keeps every toast
  mounted so `Toast.svelte`'s own dismiss timer (started once on
  mount, `$effect` in that file) is never delayed or reset by a toast
  entering/leaving the visible set. A "+N more" chip (mobile-only,
  `display:none` on desktop) surfaces the hidden count and toggles
  `_expanded` to reveal all toasts in place — nothing is ever
  silently dropped, just not all rendered at full height at once.
-->
<script>
  import { toasts } from '$lib/data/toastStore.svelte.js';
  import Toast from '$lib/Toast.svelte';

  // Mobile-only visible cap — see the comment block above. Desktop
  // always renders every toast up to toastStore's own MAX_TOASTS.
  const MOBILE_VISIBLE = 2;

  let _expanded = $state(false);

  // Collapse back once the burst drains under the cap so the NEXT
  // independent burst doesn't inherit a stale expanded state.
  $effect(() => {
    if (toasts.length <= MOBILE_VISIBLE) _expanded = false;
  });

  const _overflowCount = $derived(Math.max(0, toasts.length - MOBILE_VISIBLE));
  // Oldest toasts are rendered first (array order, flex-direction:column) —
  // those are the ones capped out of view, so they're the ones checked here.
  const _hasErrorOverflow = $derived(
    toasts.slice(0, _overflowCount).some((t) => t.kind === 'error')
  );
</script>

{#if toasts.length > 0}
  <div class="rbq-toast-container" class:rbq-toast-expanded={_expanded} aria-label="Notifications">
    {#if _overflowCount > 0}
      <button
        type="button"
        class="rbq-toast-overflow-chip"
        class:rbq-toast-overflow-chip-error={_hasErrorOverflow}
        onclick={() => (_expanded = !_expanded)}
      >
        {_expanded ? 'Show less' : `+${_overflowCount} more`}
      </button>
    {/if}
    {#each toasts as t, i (t.id)}
      <div class="rbq-toast-slot" class:rbq-toast-overflow={i < _overflowCount}>
        <Toast item={t} />
      </div>
    {/each}
  </div>
{/if}

<style>
  .rbq-toast-container {
    position: fixed;
    top: 4.5rem;
    right: 1rem;
    z-index: var(--z-toast);
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    pointer-events: none;
    /* width is constrained per-Toast via min() */
  }

  /* Overflow chip — hidden on desktop; only shown inside the mobile
     media query below. Never affects desktop layout/behaviour. */
  .rbq-toast-overflow-chip {
    display: none;
  }

  @media (max-width: 600px) {
    .rbq-toast-container {
      top: 3.8rem;
      right: 0.5rem;
      left: 0.5rem;
    }

    /* Cap simultaneously-VISIBLE toasts on mobile — see the file-header
       comment. The toast itself still mounts (timer running normally);
       only its rendering is suppressed. */
    .rbq-toast-slot.rbq-toast-overflow {
      display: none;
    }

    /* Tapping the chip reveals everything in place. */
    .rbq-toast-container.rbq-toast-expanded .rbq-toast-slot.rbq-toast-overflow {
      display: block;
    }

    .rbq-toast-overflow-chip {
      display: flex;
      align-items: center;
      justify-content: center;
      width: 100%;
      pointer-events: auto;
      padding: 0.35rem 0.5rem;
      border-radius: 4px;
      border: 1px solid var(--c-action);
      background: rgba(251, 191, 36, 0.12);
      color: var(--c-action);
      font-size: var(--fs-sm);
      font-weight: 600;
      font-family: var(--font-numeric);
      cursor: pointer;
    }

    .rbq-toast-overflow-chip-error {
      border-color: var(--c-short);
      background: var(--c-short-10);
      color: var(--c-short);
    }
  }
</style>
