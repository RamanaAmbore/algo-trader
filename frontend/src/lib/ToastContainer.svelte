<!--
  ToastContainer.svelte — fixed-position stack that renders active toasts.

  Mount ONCE in the algo layout (after AgentToast, not competing with it
  for z-index). AgentToast uses z-index 9997; this container uses 80 so
  it sits below the agent interrupt toasts and below modals (z ~9998).

  Positioning:
    Desktop: top: 4.5rem, right: 1rem (clears the 3.8rem navbar)
    Mobile:  top: 3.8rem, right: 0.5rem, max-width: calc(100vw - 1rem)

  The container itself is pointer-events: none so underlying UI stays
  reachable; each individual Toast re-enables pointer-events.
-->
<script>
  import { toasts } from '$lib/data/toastStore.svelte.js';
  import Toast from '$lib/Toast.svelte';
</script>

{#if toasts.length > 0}
  <div class="rbq-toast-container" aria-label="Notifications">
    {#each toasts as t (t.id)}
      <Toast item={t} />
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

  @media (max-width: 600px) {
    .rbq-toast-container {
      top: 3.8rem;
      right: 0.5rem;
      left: 0.5rem;
    }
  }
</style>
