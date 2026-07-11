<!--
  ModalShell — reusable overlay shell for all modal dialogs.
  Owns: fixed inset backdrop, Esc key close, optional click-outside
  close, optional portal to document.body.
  Panel content goes in the children snippet slot.

  Usage:
    <ModalShell {open} {onClose} ariaLabel="My dialog">
      <div class="my-panel">...</div>
    </ModalShell>

  Props:
    open         — whether the overlay is rendered
    onClose      — () => void called on Esc or backdrop click
    ariaLabel    — accessible name for the dialog (default 'Dialog')
    usePortal    — portal node to document.body (default true)
    clickOutside — backdrop click closes (default true)
    zIndex       — CSS z-index; override for stacking (default 200)
    dim          — show dark backdrop (default true); false = transparent
    passthrough  — pointer-events:none on overlay (default false); panel
                   content restores pointer-events automatically
    children     — panel content snippet
-->
<script>
  import { portal } from '$lib/portal';

  let {
    open         = false,
    onClose      = null,      // () => void
    ariaLabel    = 'Dialog',  // accessible name — callers should override
    usePortal    = true,      // portal to document.body
    clickOutside = true,      // click on backdrop closes
    zIndex       = 200,       // caller overrides for stacking
    dim          = true,      // false = transparent backdrop
    passthrough  = false,     // true = pointer-events:none on overlay
    children,
  } = $props();

  // Use svelte:window so Esc fires regardless of where focus sits —
  // portaled overlays are outside the normal DOM tree and won't receive
  // keydown unless focus is explicitly moved into them.
  function handleWindowKey(e) {
    if (open && e.key === 'Escape') onClose?.();
  }

  function handleBackdrop(e) {
    if (clickOutside && e.target === e.currentTarget) onClose?.();
  }
</script>

<svelte:window onkeydown={handleWindowKey} />

{#if open}
  <!-- svelte-ignore a11y-no-noninteractive-element-interactions -->
  <div
    class="ms-overlay"
    class:ms-dim={dim}
    class:ms-passthrough={passthrough}
    style="z-index:{zIndex}"
    use:portal={usePortal}
    role="dialog"
    aria-modal="true"
    aria-label={ariaLabel}
    onclick={handleBackdrop}
  >
    {@render children()}
  </div>
{/if}

<style>
  .ms-overlay {
    position: fixed;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    /* background and backdrop-filter controlled by modifier classes */
  }
  .ms-dim         { background: rgba(8, 12, 20, 0.72); backdrop-filter: blur(2px); }
  .ms-passthrough { pointer-events: none; }
  /* Panel content must restore pointer events when overlay is passthrough */
  :global(.ms-passthrough) > * { pointer-events: auto; }
</style>
