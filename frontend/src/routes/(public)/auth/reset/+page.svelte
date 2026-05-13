<script>
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { resetPassword } from '$lib/api';

  let token       = $state('');
  let pw          = $state('');
  let confirm     = $state('');
  let loading     = $state(false);
  let error       = $state('');
  let showPw      = $state(false);
  let showConfirm = $state(false);

  $effect(() => {
    token = page.url.searchParams.get('token') || '';
    if (!token) error = 'Reset link is missing the token. Request a new one from the sign-in page.';
  });

  async function submit() {
    error = '';
    if (!token) { error = 'No reset token in URL.'; return; }
    if (pw.length < 8) { error = 'Password must be at least 8 characters.'; return; }
    if (pw !== confirm) { error = 'Passwords do not match.'; return; }
    loading = true;
    try {
      await resetPassword(token, pw);
      goto('/signin?reset=1');
    } catch (e) {
      error = e.message;
    } finally { loading = false; }
  }
</script>

<svelte:head>
  <title>Reset Password | RamboQuant Analytics</title>
  <meta name="description" content="Set a new password on your RamboQuant account." />
  <meta name="robots" content="noindex,nofollow" />
</svelte:head>

<div class="max-w-sm mx-auto mt-4">
  <div class="signin-panel">
    <div class="signin-header">
      <div class="signin-header-title">Reset Password</div>
    </div>
    <div class="signin-body">
      {#if error}
        <div class="pub-banner-error mb-3 p-2 rounded text-xs">{error}</div>
      {/if}

      <p class="text-[0.7rem] text-muted mb-3">
        Enter a new password. The reset link is valid for 30 minutes from
        when you requested it.
      </p>

      <div class="space-y-3">
        <div>
          <label class="field-label" for="new-pw">New password</label>
          <div class="pw-wrap">
            <input id="new-pw" type={showPw ? 'text' : 'password'} bind:value={pw}
              class="field-input pw-input"
              placeholder="Min 8 chars, mixed case + digit + symbol"
              autocomplete="new-password"
              onkeydown={(e) => e.key === 'Enter' && submit()} />
            <button type="button" class="pw-toggle" tabindex="-1"
              onclick={() => showPw = !showPw}>{showPw ? 'Hide' : 'Show'}</button>
          </div>
        </div>
        <div>
          <label class="field-label" for="confirm-pw">Confirm password</label>
          <div class="pw-wrap">
            <input id="confirm-pw" type={showConfirm ? 'text' : 'password'} bind:value={confirm}
              class="field-input pw-input"
              placeholder="Repeat password"
              autocomplete="new-password"
              onkeydown={(e) => e.key === 'Enter' && submit()} />
            <button type="button" class="pw-toggle" tabindex="-1"
              onclick={() => showConfirm = !showConfirm}>{showConfirm ? 'Hide' : 'Show'}</button>
          </div>
        </div>
        <button onclick={submit} disabled={loading || !token || !pw || !confirm}
          class="btn-primary w-full disabled:opacity-50 mt-1">
          {loading ? 'Resetting…' : 'Reset password'}
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
