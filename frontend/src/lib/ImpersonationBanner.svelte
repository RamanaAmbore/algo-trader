<script>
  /**
   * ImpersonationBanner — persistent yellow strip across the top of
   * every page when the current JWT carries an imp_by claim. Tells
   * the impersonator they're inside a support session and offers an
   * End button that calls /api/auth/stop-impersonate, swaps the
   * authStore back to the original token, and reloads to /admin so
   * they land somewhere sensible after the session ends.
   *
   * Mounted by both (public)/+layout.svelte and (algo)/+layout.svelte
   * so the banner persists across nav contexts (admin impersonating
   * a partner may navigate public + algo while the session runs).
   */
  import { authStore } from '$lib/stores';
  import { stopImpersonation } from '$lib/api';
  import { goto } from '$app/navigation';

  let _busy   = $state(false);
  let _error  = $state('');

  async function end() {
    if (_busy) return;
    _busy = true; _error = '';
    try {
      const r = await stopImpersonation();
      authStore.stopImpersonation(r.token, {
        username: r.username,
        role: r.role,
        display_name: r.display_name,
      });
      // Land back on /admin/users where the support session was started.
      goto('/admin/users');
    } catch (e) {
      _error = e?.message || 'Failed to end session';
    } finally {
      _busy = false;
    }
  }
</script>

{#if $authStore.impBy && $authStore.user}
  <div class="imp-banner" role="status">
    <span class="imp-banner-icon" aria-hidden="true">👁</span>
    <span class="imp-banner-text">
      Support session — viewing platform as
      <strong>{$authStore.user.username}</strong>
      ({$authStore.user.role}) ·
      original actor: <strong>{$authStore.impBy}</strong>
    </span>
    {#if _error}
      <span class="imp-banner-error">{_error}</span>
    {/if}
    <button onclick={end} disabled={_busy} class="imp-banner-end">
      {_busy ? 'Ending…' : 'End session'}
    </button>
  </div>
{/if}

<style>
  .imp-banner {
    position: sticky;
    top: 0;
    z-index: 250;  /* above the navbar (z-50/100) so it never gets covered */
    width: 100%;
    background: #fef3c7;            /* amber-100 */
    border-bottom: 2px solid #fbbf24; /* amber-400 — canonical algo palette */
    color: #78350f;                 /* amber-900 */
    padding: 0.45rem 1rem;
    display: flex;
    align-items: center;
    gap: 0.6rem;
    font-size: var(--fs-lg);
    font-weight: 600;
    box-shadow: 0 1px 4px rgba(0,0,0,0.12);
  }
  .imp-banner-icon {
    font-size: 1rem;
    flex-shrink: 0;
  }
  .imp-banner-text {
    flex: 1;
    line-height: 1.35;
  }
  .imp-banner-text strong {
    color: #422006;
    font-weight: 800;
  }
  .imp-banner-error {
    color: #b91c1c;
    font-weight: 700;
    font-size: var(--fs-lg);
  }
  .imp-banner-end {
    flex-shrink: 0;
    background: #d97706;            /* amber-600 */
    color: #fff;
    border: none;
    border-radius: 0.25rem;
    padding: 0.3rem 0.8rem;
    font-size: var(--fs-lg);
    font-weight: 700;
    cursor: pointer;
    letter-spacing: 0.02em;
    transition: background-color 0.1s;
  }
  .imp-banner-end:hover:not(:disabled) {
    background: #b45309;
  }
  .imp-banner-end:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }
  @media (max-width: 600px) {
    .imp-banner {
      flex-wrap: wrap;
      font-size: var(--fs-lg);
      padding: 0.4rem 0.7rem;
    }
    .imp-banner-text { flex-basis: 100%; }
  }
</style>
