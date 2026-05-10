<script>
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { login as apiLogin, register as apiRegister, forgotPassword } from '$lib/api';
  import { authStore } from '$lib/stores';

  let tab       = $state('signin');   // 'signin' | 'register' | 'forgot'
  let loading   = $state(false);
  let error     = $state('');
  let info      = $state('');         // green success / pending banner text

  let signinForm = $state({ username: '', password: '' });
  let regForm    = $state({ username: '', password: '', confirm: '', display_name: '', email: '', phone: '' });
  let forgotForm = $state({ identifier: '' });

  // Read ?verified=1|0 / ?reset=1 / ?registered=1 / ?pending_verify=1 once
  // on mount and surface a one-line banner. The query string is the
  // backend's only way to talk to this page (verify-email redirects
  // here), so the parsing is page-local.
  $effect(() => {
    const q = $page.url.searchParams;
    if (q.get('verified') === '1') info = 'Email verified. Once an admin approves your account you can sign in.';
    else if (q.get('verified') === '0') error = 'Verification link is invalid or expired. Try registering again.';
    else if (q.get('reset')    === '1') info = 'Password reset. Sign in with your new password.';
    else if (q.get('registered') === '1') info = 'Account created. Check your email for the verification link.';
  });

  async function signin() {
    loading = true; error = ''; info = '';
    try {
      const data = await apiLogin(signinForm.username, signinForm.password);
      authStore.login(data.access_token, {
        username:     data.username,
        role:         data.role,
        display_name: data.display_name,
      });
      const isAdmin = data.role === 'admin' || data.role === 'designated';
      goto(isAdmin ? '/dashboard' : '/performance');
    } catch (e) {
      error = e.message;
    } finally { loading = false; }
  }

  async function register() {
    loading = true; error = ''; info = '';
    if (regForm.password !== regForm.confirm) { error = 'Passwords do not match'; loading = false; return; }
    try {
      await apiRegister({
        username:     regForm.username,
        password:     regForm.password,
        display_name: regForm.display_name || regForm.username,
        email:        regForm.email,
        phone:        regForm.phone,
      });
      // Backend no longer returns a JWT after register — user must verify
      // email + wait for admin approval. Switch to signin tab and show a
      // confirmation banner so they know what to do next.
      tab = 'signin';
      info = 'Account created. Check your email for the verification link, then wait for admin approval.';
      regForm = { username: '', password: '', confirm: '', display_name: '', email: '', phone: '' };
    } catch (e) {
      error = e.message;
    } finally { loading = false; }
  }

  async function forgot() {
    loading = true; error = ''; info = '';
    try {
      const r = await forgotPassword(forgotForm.identifier);
      info = r?.detail || 'If the account exists, a reset link has been emailed.';
      forgotForm.identifier = '';
      tab = 'signin';
    } catch (e) {
      error = e.message;
    } finally { loading = false; }
  }
</script>
<svelte:head>
  <title>Sign In | RamboQuant Analytics</title>
  <meta name="description" content="Sign in to your RamboQuant partner account." />

  <!-- Open Graph -->
  <meta property="og:title" content="Sign In | RamboQuant Analytics" />
  <meta property="og:description" content="Sign in to your RamboQuant partner account." />
  <meta property="og:url" content="https://ramboq.com/signin" />
  <meta property="og:type" content="website" />
  <meta property="og:image" content="https://ramboq.com/og-image.svg" />
  <meta property="og:site_name" content="RamboQuant Analytics" />

  <!-- Twitter -->
  <meta name="twitter:card" content="summary_large_image" />
  <meta name="twitter:title" content="Sign In | RamboQuant Analytics" />
  <meta name="twitter:description" content="Sign in to your RamboQuant partner account." />
  <meta name="twitter:image" content="https://ramboq.com/og-image.svg" />
</svelte:head>

<div class="max-w-sm mx-auto mt-4">
  <div class="signin-panel">
    <div class="signin-header">
      <div class="signin-header-title">Partner Portal</div>
    </div>
    <div class="signin-body">
      <!-- Tab selector — cream divider matches the public-card border. -->
      <div class="flex pub-divider border-b mb-4">
        <button
          onclick={() => { tab = 'signin'; error = ''; info = ''; }}
          class="flex-1 py-2 text-xs font-semibold border-b-2 transition-colors
                 {tab === 'signin' ? 'border-primary text-primary' : 'border-transparent text-muted hover:text-text'}"
        >Sign In</button>
        <button
          onclick={() => { tab = 'register'; error = ''; info = ''; }}
          class="flex-1 py-2 text-xs font-semibold border-b-2 transition-colors
                 {tab === 'register' ? 'border-primary text-primary' : 'border-transparent text-muted hover:text-text'}"
        >Register</button>
      </div>

      {#if error}
        <div class="pub-banner-error mb-3 p-2 rounded text-xs">{error}</div>
      {/if}
      {#if info}
        <div class="pub-banner-info mb-3 p-2 rounded text-xs">{info}</div>
      {/if}

      {#if tab === 'signin'}
        <div class="space-y-3">
          <div>
            <label class="field-label" for="s-user">Username</label>
            <input id="s-user" bind:value={signinForm.username} class="field-input" placeholder="Username"
              onkeydown={(e) => e.key === 'Enter' && signin()} />
          </div>
          <div>
            <label class="field-label" for="s-pass">Password</label>
            <input id="s-pass" type="password" bind:value={signinForm.password} class="field-input" placeholder="Password"
              onkeydown={(e) => e.key === 'Enter' && signin()} />
          </div>
          <button
            onclick={signin}
            disabled={loading || !signinForm.username || !signinForm.password}
            class="btn-primary w-full disabled:opacity-50 mt-1"
          >{loading ? 'Signing in…' : 'Sign In'}</button>
          <div class="flex justify-end">
            <button type="button" class="text-[0.65rem] text-primary hover:underline"
              onclick={() => { tab = 'forgot'; error = ''; info = ''; }}>Forgot password?</button>
          </div>
        </div>

      {:else if tab === 'forgot'}
        <div class="space-y-3">
          <p class="text-[0.7rem] text-muted">
            Enter your username or email. If the account exists, we'll
            email a reset link valid for 30 minutes.
          </p>
          <div>
            <label class="field-label" for="f-id">Username or email</label>
            <input id="f-id" bind:value={forgotForm.identifier} class="field-input"
              placeholder="username or email"
              onkeydown={(e) => e.key === 'Enter' && forgot()} />
          </div>
          <button
            onclick={forgot}
            disabled={loading || !forgotForm.identifier}
            class="btn-primary w-full disabled:opacity-50 mt-1"
          >{loading ? 'Sending…' : 'Send reset link'}</button>
          <div class="flex justify-start">
            <button type="button" class="text-[0.65rem] text-primary hover:underline"
              onclick={() => { tab = 'signin'; error = ''; info = ''; }}>← Back to sign in</button>
          </div>
        </div>

      {:else}
        <div class="space-y-3">
          <div>
            <label class="field-label" for="r-user">Username</label>
            <input id="r-user" bind:value={regForm.username} class="field-input" placeholder="Choose a username" />
          </div>
          <div>
            <label class="field-label" for="r-name">Full Name</label>
            <input id="r-name" bind:value={regForm.display_name} class="field-input" placeholder="Full name" />
          </div>
          <div>
            <label class="field-label" for="r-email">Email</label>
            <input id="r-email" type="email" bind:value={regForm.email} class="field-input" placeholder="email@example.com" />
          </div>
          <div>
            <label class="field-label" for="r-phone">Phone</label>
            <input id="r-phone" bind:value={regForm.phone} class="field-input" placeholder="+91 98765 43210" />
          </div>
          <div>
            <label class="field-label" for="r-pass">Password</label>
            <input id="r-pass" type="password" bind:value={regForm.password} class="field-input" placeholder="Min 8 chars, mixed case + digit + symbol" />
          </div>
          <div>
            <label class="field-label" for="r-confirm">Confirm Password</label>
            <input id="r-confirm" type="password" bind:value={regForm.confirm} class="field-input" placeholder="Repeat password" />
          </div>

          <p class="text-[0.6rem] text-muted mt-2">
            Verify your email after registering, then wait for admin approval before signing in.
          </p>

          <button
            onclick={register}
            disabled={loading || !regForm.username || !regForm.password || !regForm.confirm || !regForm.display_name || !regForm.email}
            class="btn-primary w-full disabled:opacity-50 mt-1"
          >{loading ? 'Creating account…' : 'Register'}</button>
        </div>
      {/if}
    </div>
  </div>
</div>

<style>
  .signin-panel {
    border-radius: 6px;
    overflow: hidden;
    border: 1px solid #b4c0bc;
    box-shadow: 0 4px 20px rgba(22,53,53,0.12);
  }
  .signin-header {
    background: #0c1830;
    padding: 1.5rem 1.5rem 1.25rem;
    border-bottom: 2px solid #c8a84b;
  }
  .signin-header-title {
    font-size: 1rem;
    font-weight: 800;
    color: #fff;
    letter-spacing: 0.05em;
    text-transform: uppercase;
  }
  .signin-body {
    background: #fffdf8;        /* card-cream — matches .pub-card */
    padding: 1.25rem 1.5rem 1.5rem;
  }
  /* Green success banner — sage-leaning so it sits with the cream palette. */
  .pub-banner-info {
    background: #e9f1e3;
    border: 1px solid #a8c098;
    color: #2f4a26;
  }
  /* Override browser autofill yellow background */
  .signin-body :global(input:-webkit-autofill),
  .signin-body :global(input:-webkit-autofill:hover),
  .signin-body :global(input:-webkit-autofill:focus) {
    -webkit-box-shadow: 0 0 0 1000px #fff inset !important;
    box-shadow: 0 0 0 1000px #fff inset !important;
    -webkit-text-fill-color: #1e3050 !important;
    border-color: #c0ccdc !important;
  }
</style>
