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
