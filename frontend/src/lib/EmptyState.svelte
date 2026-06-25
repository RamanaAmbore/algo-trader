<!--
  EmptyState — canonical "no data" component used by algo cards.

  Two usage modes, both supported:

  1. Compact message (backward-compatible with existing callers):
       <EmptyState message="No open positions" />
       <EmptyState message="No data" variant="compact" />

  2. Rich mode with icon, title, hint, optional CTA:
       <EmptyState
         title="No candidates"
         hint="Pick an underlying to surface candidates."
         icon="inbox"
       />
       <EmptyState
         title="No strategies"
         icon="chart"
         action={{ label: "New strategy", onClick: () => goto('/strategies/new') }}
       />

  Props:
    message?  string              — simple one-liner (compact mode)
    variant?  'card' | 'compact' — layout size for message mode
    title?    string              — primary empty-state headline (rich mode)
    hint?     string              — explanatory subtext (rich mode)
    hintBody? snippet             — rich HTML hint (overrides `hint`).
                                    Use for access-denied panels needing
                                    <code> / <strong> markup.
    icon?     'inbox' | 'chart' | 'search' | 'lock' = 'inbox'
    action?   { label: string, onClick: () => void }

  Visual treatment: muted slate palette, centered layout. Rich mode adds
  a ~1.2 rem SVG icon above the title. Compact/message mode keeps the
  existing monospace italic style that fits inside card bodies.
-->
<script>
  let {
    /** Compact mode: show a single muted line. */
    message = '',
    /** Size variant for compact message mode. */
    variant = 'card',
    /** Rich mode: primary headline. */
    title = '',
    /** Rich mode: explanatory subtext. */
    hint = '',
    /**
     * Rich mode: hint body snippet for HTML-rich content.
     * When provided, it renders in place of the `hint` string —
     * lets callers embed <code>, <strong>, <a>, etc. without
     * exposing a generic content slot.
     * @type {import('svelte').Snippet | null}
     */
    hintBody = null,
    /**
     * Rich mode: icon key.
     * @type {'inbox' | 'chart' | 'search' | 'lock'}
     */
    icon = 'inbox',
    /** Rich mode: optional CTA button. */
    action = /** @type {{ label: string, onClick: () => void } | null} */ (null),
  } = $props();

  // Rich mode = title is provided.
  const _rich = $derived(!!title);
</script>

{#if _rich}
  <div class="es-rich" role="status" aria-live="polite">
    <span class="es-icon" aria-hidden="true">
      {#if icon === 'chart'}
        <!-- Simple bar-chart outline -->
        <svg width="20" height="20" viewBox="0 0 20 20" fill="none"
             stroke="currentColor" stroke-width="1.5" stroke-linecap="round"
             stroke-linejoin="round">
          <rect x="2"  y="10" width="3" height="8" rx="0.5"/>
          <rect x="8"  y="6"  width="3" height="12" rx="0.5"/>
          <rect x="14" y="2"  width="3" height="16" rx="0.5"/>
        </svg>
      {:else if icon === 'search'}
        <!-- Magnifying glass -->
        <svg width="20" height="20" viewBox="0 0 20 20" fill="none"
             stroke="currentColor" stroke-width="1.5" stroke-linecap="round">
          <circle cx="9" cy="9" r="6"/>
          <line x1="14" y1="14" x2="18" y2="18"/>
        </svg>
      {:else if icon === 'lock'}
        <!-- Padlock -->
        <svg width="20" height="20" viewBox="0 0 20 20" fill="none"
             stroke="currentColor" stroke-width="1.5" stroke-linecap="round"
             stroke-linejoin="round">
          <rect x="4" y="9" width="12" height="9" rx="1.5"/>
          <path d="M7 9V6a3 3 0 0 1 6 0v3"/>
        </svg>
      {:else}
        <!-- inbox (default) — tray with down-arrow -->
        <svg width="20" height="20" viewBox="0 0 20 20" fill="none"
             stroke="currentColor" stroke-width="1.5" stroke-linecap="round"
             stroke-linejoin="round">
          <path d="M2 13l2-7h12l2 7"/>
          <path d="M2 13h4l1.5 2h5L14 13h4"/>
          <line x1="10" y1="5" x2="10" y2="11"/>
          <polyline points="7,8 10,11 13,8"/>
        </svg>
      {/if}
    </span>
    <span class="es-title">{title}</span>
    {#if hintBody}
      <span class="es-hint">{@render hintBody()}</span>
    {:else if hint}
      <span class="es-hint">{hint}</span>
    {/if}
    {#if action}
      <button type="button" class="es-action" onclick={action.onClick}>
        {action.label}
      </button>
    {/if}
  </div>
{:else}
  <!-- Compact / message mode — backward-compatible with existing callers -->
  <div class="es" class:es-compact={variant === 'compact'}
       role="status" aria-live="polite">
    {message || 'No data'}
  </div>
{/if}

<style>
  /* ── Compact / message mode (existing style, unchanged) ──────── */
  .es {
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 0.65rem 0.5rem;
    color: var(--algo-muted);
    font-family: ui-monospace, monospace;
    font-size: 0.68rem;
    letter-spacing: 0.05em;
    font-style: italic;
    text-align: center;
  }
  .es-compact {
    padding: 0.35rem 0.5rem;
    font-size: 0.62rem;
  }

  /* ── Rich mode ────────────────────────────────────────────────── */
  .es-rich {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 0.4rem;
    padding: 1.4rem 1rem;
    text-align: center;
  }
  .es-icon {
    color: #334155; /* slate-700, very muted */
    display: flex;
    align-items: center;
    justify-content: center;
    margin-bottom: 0.2rem;
  }
  .es-title {
    font-size: 0.72rem;
    font-weight: 600;
    color: #64748b; /* slate-500 */
    letter-spacing: 0.02em;
  }
  .es-hint {
    font-size: 0.62rem;
    color: #475569; /* slate-600 */
    font-style: italic;
    max-width: 24rem;
    line-height: 1.5;
  }
  /* When hintBody is used, embedded code / strong elements need
     readable styling against the muted slate background. */
  .es-hint :global(code) {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.92em;
    padding: 0.05rem 0.25rem;
    background: rgba(148, 163, 184, 0.10);
    border: 1px solid rgba(148, 163, 184, 0.18);
    border-radius: 2px;
    color: #94a3b8;
    font-style: normal;
  }
  .es-hint :global(strong) {
    color: #94a3b8;
    font-weight: 700;
    font-style: normal;
  }
  .es-hint :global(a) {
    color: #7dd3fc;
    text-decoration: underline;
    text-underline-offset: 2px;
  }
  .es-action {
    margin-top: 0.4rem;
    padding: 0.3rem 0.8rem;
    font-size: 0.62rem;
    font-weight: 600;
    color: #fbbf24;
    background: rgba(251, 191, 36, 0.08);
    border: 1px solid rgba(251, 191, 36, 0.28);
    border-radius: 3px;
    cursor: pointer;
    letter-spacing: 0.04em;
    transition: background 0.15s, border-color 0.15s;
  }
  .es-action:hover {
    background: rgba(251, 191, 36, 0.15);
    border-color: rgba(251, 191, 36, 0.5);
  }
</style>
