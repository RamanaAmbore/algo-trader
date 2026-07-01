<!--
  DisclosureChevron — small inline expand/collapse triangle for table-row
  level disclosure (NOT card-level collapse — that's CollapseButton).

  Visual contract:
    - 0.65rem, muted slate-blue (#7e97b8), flex-shrink-0
    - Right-pointing triangle when collapsed (▸), down when expanded (▾)
    - Sized + coloured identically across agents, fragments, anywhere
      else a row toggles open inline

  Why a component: previously two pages hand-rolled the same ▾/▸ span
  with different colours and font sizes. One component = one visual
  identity for row-disclosure across the workspace.
-->
<script>
  /** @type {{ open: boolean, ariaLabel?: string }} */
  let { open = false, ariaLabel = undefined } = $props();
</script>

<span class="disclosure-chevron" aria-label={ariaLabel} aria-hidden={ariaLabel ? undefined : true}>
  {open ? '▾' : '▸'}
</span>

<style>
  .disclosure-chevron {
    /* Single sky-300 palette across every controller-style icon
       (RefreshButton, FullscreenButton, DisclosureChevron, agent
       notify icons). Reads as one consistent affordance family. */
    color: #7dd3fc;
    font-size: var(--fs-xl);
    line-height: 1;
    flex-shrink: 0;
    user-select: none;
    /* A bit of breathing room so the chevron reads as a clear
       affordance instead of disappearing among the row pills. */
    padding: 0 0.15rem;
    transition: color 0.12s;
    opacity: 0.85;
  }
  .disclosure-chevron:hover {
    color: #bae6fd;
    opacity: 1;
  }
</style>
