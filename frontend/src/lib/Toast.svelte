<!--
  Toast.svelte — renders ONE toast descriptor.

  Props:
    item: { id, kind, message, timeoutMs, dismissible, action?, createdAt }

  Visual: compact card with 4px colored left border + tinted bg.
  Slide-in from right (~200ms). Fade-out on dismiss (~150ms).
  Hover pauses the auto-dismiss timer; mouseleave restarts the
  remaining countdown from wherever it left off.
-->
<script>
  import { onDestroy, untrack } from 'svelte';
  import { dismiss } from '$lib/data/toastStore.svelte.js';

  /** @type {{ item: { id: number, kind: 'success'|'error'|'info'|'warning', message: string,
   *                    timeoutMs: number, dismissible: boolean,
   *                    action?: { label: string, onClick: () => void },
   *                    createdAt: number } }} */
  const { item } = $props();

  // Fade-out class drives the CSS animation; when it completes we call
  // dismiss() so the DOM node is removed.
  let _dismissing = $state(false);

  /** Schedule an animated dismiss. */
  function close() {
    if (_dismissing) return;
    _dismissing = true;
    setTimeout(() => dismiss(item.id), 150);
  }

  // ── Auto-dismiss timer with hover-pause ────────────────────────────
  // We track elapsed ms so hovering doesn't reset the full timer on
  // mouseleave — only the remaining portion keeps running.
  // `_initialMs` intentionally captures the timeout at mount — the prop
  // won't change during the toast's lifetime, so untrack is correct here.
  const _initialMs = untrack(() => item.timeoutMs);
  let _timerId = /** @type {ReturnType<typeof setTimeout>|null} */ (null);
  let _startedAt = 0;      // epoch ms when the current timer segment started
  let _remaining = _initialMs; // how many ms remain before dismiss

  function _startTimer() {
    if (!_remaining) return; // sticky (error) — no auto-dismiss
    _startedAt = Date.now();
    _timerId = setTimeout(close, _remaining);
  }

  function _pauseTimer() {
    if (_timerId == null) return;
    clearTimeout(_timerId);
    _timerId = null;
    _remaining = Math.max(0, _remaining - (Date.now() - _startedAt));
  }

  function _onMouseEnter() { _pauseTimer(); }
  function _onMouseLeave() { _startTimer(); }

  // Start the timer once on mount.
  $effect(() => {
    _startTimer();
    return () => {
      if (_timerId) clearTimeout(_timerId);
    };
  });

  onDestroy(() => {
    if (_timerId) clearTimeout(_timerId);
  });

  // ── Icon SVG paths by kind ─────────────────────────────────────────
  const ICONS = {
    success: `<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
               d="M5 13l4 4L19 7"/>`,
    error:   `<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
               d="M6 18L18 6M6 6l12 12"/>`,
    info:    `<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
               d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>`,
    warning: `<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
               d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732
                  4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/>`,
  };

  const COLORS = {
    success: { border: '#4ade80', bg: 'rgba(74,222,128,0.10)',  icon: '#4ade80' },
    error:   { border: '#f87171', bg: 'rgba(248,113,113,0.12)', icon: '#f87171' },
    info:    { border: '#7dd3fc', bg: 'rgba(56,189,248,0.10)',  icon: '#7dd3fc' },
    warning: { border: '#fbbf24', bg: 'rgba(251,191,36,0.10)',  icon: '#fbbf24' },
  };

  const c = $derived(COLORS[item.kind] || COLORS.info);
  const icon = $derived(ICONS[item.kind] || ICONS.info);

  // Accessibility: error = assertive (interruptive); others = polite.
  const _ariaRole = $derived(item.kind === 'error' ? 'alert' : 'status');
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  class="rbq-toast"
  class:rbq-toast-dismissing={_dismissing}
  style="border-left-color: {c.border}; background: {c.bg};"
  role={_ariaRole}
  aria-live={item.kind === 'error' ? 'assertive' : 'polite'}
  aria-atomic="true"
  onmouseenter={_onMouseEnter}
  onmouseleave={_onMouseLeave}
>
  <!-- Left icon -->
  <span class="rbq-toast-icon" style="color: {c.icon};">
    <!-- 20×20 outline icon, stroke-only, no fill -->
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
         stroke="currentColor" aria-hidden="true">
      {@html icon}
    </svg>
  </span>

  <!-- Body -->
  <div class="rbq-toast-body">
    <span class="rbq-toast-msg">{item.message}</span>
    {#if item.action}
      <button type="button" class="rbq-toast-action"
              onclick={() => { item.action?.onClick(); close(); }}>
        {item.action.label}
      </button>
    {/if}
  </div>

  <!-- Dismiss button -->
  {#if item.dismissible}
    <button type="button" class="rbq-toast-x" aria-label="Dismiss"
            onclick={close}>×</button>
  {/if}
</div>

<style>
  .rbq-toast {
    pointer-events: auto;
    display: flex;
    align-items: flex-start;
    gap: 0.45rem;
    width: min(17rem, calc(100vw - 1rem));
    padding: 0.5rem 0.55rem;
    border-radius: 4px;
    border: 1px solid rgba(255,255,255,0.09);
    border-left-width: 4px;
    background: rgba(29,42,68,0.97);
    box-shadow: 0 6px 20px rgba(0,0,0,0.50);
    animation: rbq-toast-in 0.20s ease-out both;
  }

  .rbq-toast-dismissing {
    animation: rbq-toast-out 0.15s ease-in both;
  }

  /* Icon column */
  .rbq-toast-icon {
    flex: 0 0 auto;
    margin-top: 0.05rem;
    line-height: 0;
  }

  /* Body column — grows to fill available width */
  .rbq-toast-body {
    flex: 1 1 auto;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }

  .rbq-toast-msg {
    font-size: 0.68rem;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    color: #c8d8f0;
    line-height: 1.35;
    word-break: break-word;
  }

  .rbq-toast-action {
    align-self: flex-start;
    background: transparent;
    border: none;
    padding: 0;
    font-size: 0.6rem;
    font-weight: 600;
    color: #22d3ee;
    cursor: pointer;
    text-decoration: underline;
    text-underline-offset: 2px;
    font-family: inherit;
  }
  .rbq-toast-action:hover { color: #67e8f9; }

  /* Dismiss × */
  .rbq-toast-x {
    flex: 0 0 auto;
    margin-left: 0.1rem;
    padding: 0 0.2rem;
    line-height: 1;
    font-size: 0.9rem;
    color: rgba(200,216,240,0.45);
    background: transparent;
    border: none;
    cursor: pointer;
    border-radius: 2px;
    font-family: inherit;
    margin-top: -0.05rem;
  }
  .rbq-toast-x:hover {
    color: #f87171;
    background: rgba(248,113,113,0.10);
  }

  @keyframes rbq-toast-in {
    from { transform: translateX(0.75rem); opacity: 0; }
    to   { transform: translateX(0);       opacity: 1; }
  }

  @keyframes rbq-toast-out {
    from { opacity: 1; transform: translateX(0); }
    to   { opacity: 0; transform: translateX(0.5rem); }
  }
  @media (prefers-reduced-motion: reduce) {
    .rbq-toast         { animation: none; }
    .rbq-toast-dismissing { animation: none; opacity: 0; }
  }
</style>
