<!--
  ModalShell — reusable overlay shell for all modal dialogs.
  Owns: fixed inset backdrop, Esc key close, optional click-outside
  close, optional portal to document.body.
  Panel content goes in the children snippet slot.

  Usage:
    <ModalShell {open} {onClose}>
      <div class="my-panel">...</div>
    </ModalShell>

  Props:
    open         — whether the overlay is rendered
    onClose      — () => void called on Esc or backdrop click
    usePortal    — portal node to document.body (default true)
    clickOutside — backdrop click closes (default true)
    zIndex       — CSS z-index; override for stacking (default 200)
    children     — panel content snippet
-->
<script>
  import { portal } from '$lib/portal';

  let {
    open         = false,
    onClose      = null,   // () => void
    usePortal    = true,   // portal to document.body
    clickOutside = true,   // click on backdrop closes
    zIndex       = 200,    // caller overrides for stacking
    children,
  } = $props();

  function handleKey(e) {
    if (e.key === 'Escape') onClose?.();
  }

  function handleBackdrop(e) {
    if (clickOutside && e.target === e.currentTarget) onClose?.();
  }
</script>

{#if open}
  <!-- svelte-ignore a11y-no-noninteractive-element-interactions -->
  <div
    class="ms-overlay"
    style="z-index:{zIndex}"
    use:portal={usePortal}
    role="dialog"
    aria-modal="true"
    onclick={handleBackdrop}
    onkeydown={handleKey}
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
    background: rgba(0, 0, 0, 0.55);
    backdrop-filter: blur(2px);
  }
</style>
