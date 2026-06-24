<script>
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { changePassword } from '$lib/api';
  import { authStore } from '$lib/stores';

  let pw         = $state('');
  let confirm    = $state('');
  let loading    = $state(false);
  let error      = $state('');
  let showPw     = $state(false);
  let showConfirm = $state(false);

  // Belt-and-braces: if there's no JWT in sessionStorage we can't even
  // call the endpoint. Bounce to /signin in that case.
  onMount(() => {
    if (!$authStore.user || !$authStore.token) {
      goto('/signin');
    }
  });

  async function submit() {
    error = '';
    if (pw.length < 8)   { error = 'Password must be at least 8 characters.'; return; }
    if (pw !== confirm)  { error = 'Passwords do not match.'; return; }
    loading = true;
    try {
      const data = await changePassword(pw);
      // Backend bumps token_version so the JWT we used to call this
      // endpoint is now invalid. Replace it with the fresh one before
      // navigating anywhere else.
      authStore.login(data.access_token, {
        username:     data.username,
        role:         data.role,
        display_name: data.display_name,
      });
      // Route post-change based on tier: firm owner + operational tier
      // land on the algo dashboard; trader gets dashboard too; everyone
      // else (partner / LP) lands on /performance.
      const isAdminTier = data.role === 'designated'
                       || data.role === 'admin'
                       || data.role === 'trader'
                       || data.role === 'risk';
      goto(isAdminTier ? '/dashboard' : '/performance');
    } catch (e) {
      error = e.message;
    } finally { loading = false; }
  }
</script>

<svelte:head>
  <title>Change Password | RamboQuant Analytics</title>
  <meta name="description" content="Set a new password on your RamboQuant account." />
  <meta name="robots" content="noindex,nofollow" />
</svelte:head>

<div class="max-w-sm mx-auto mt-4">
  <div class="signin-panel">
    <div class="signin-header">
      <div class="signin-header-title">Set New Password</div>
    </div>
    <div class="signin-body">
      {#if error}
        <div class="pub-banner-error mb-3 p-2 rounded text-xs">{error}</div>
      {/if}

      <p class="text-[0.7rem] text-muted mb-3">
        An admin reset your password. Set a new one of your own before
        you can use the platform.
      </p>

      <div class="space-y-3">
        <div>
          <label class="field-label" for="cp-new">New password</label>
          <div class="pw-wrap">
            <input id="cp-new" type={showPw ? 'text' : 'password'} bind:value={pw}
              class="field-input pw-input"
              placeholder="Min 8 chars, mixed case + digit + symbol"
              autocomplete="new-password"
              onkeydown={(e) => e.key === 'Enter' && submit()} />
            <button type="button" class="pw-toggle" tabindex="-1"
              onclick={() => showPw = !showPw}>{showPw ? 'Hide' : 'Show'}</button>
          </div>
        </div>
        <div>
          <label class="field-label" for="cp-confirm">Confirm password</label>
          <div class="pw-wrap">
            <input id="cp-confirm" type={showConfirm ? 'text' : 'password'} bind:value={confirm}
              class="field-input pw-input"
              placeholder="Repeat password"
              autocomplete="new-password"
              onkeydown={(e) => e.key === 'Enter' && submit()} />
            <button type="button" class="pw-toggle" tabindex="-1"
              onclick={() => showConfirm = !showConfirm}>{showConfirm ? 'Hide' : 'Show'}</button>
          </div>
        </div>
        <button onclick={submit} disabled={loading || !pw || !confirm}
          class="btn-primary w-full disabled:opacity-50 mt-1">
          {loading ? 'Setting…' : 'Set password'}
        </button>
      </div>
    </div>
  </div>
</div>

<style>
  .signin-panel { border-radius: 6px; overflow: hidden; border: 1px solid #b4c0bc;
    box-shadow: 0 4px 20px rgba(22,53,53,0.12); }
  .signin-header { background: #0c1830; padding: 1.5rem 1.5rem 1.25rem;
    border-bottom: 2px solid #c8a84b; }
  .signin-header-title { font-size: 1rem; font-weight: 800; color: #fff;
    letter-spacing: 0.05em; text-transform: uppercase; }
  .signin-body { background: #fffdf8; padding: 1.25rem 1.5rem 1.5rem; }
  .pw-wrap { position: relative; }
  .pw-input { padding-right: 3.2rem; }
  .pw-toggle {
    position: absolute; right: 0.5rem; top: 50%; transform: translateY(-50%);
    font-size: 0.65rem; font-weight: 600; letter-spacing: 0.03em;
    color: #6b7a8c; background: transparent; border: none; cursor: pointer;
    padding: 0.15rem 0.35rem;
  }
  .pw-toggle:hover { color: #1e3050; }
</style>
