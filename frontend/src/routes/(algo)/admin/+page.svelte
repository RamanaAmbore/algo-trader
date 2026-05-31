<script>
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { authStore, nowStamp } from '$lib/stores';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import {
    fetchUsers, approveUser, rejectUser, updateUser, createUser,
    suspendUser, reinstateUser, terminateUser, toggleDesignated, adminResetPassword,
    resendVerification, markVerified,
    sendPartnerEmail, fetchEmailEvents,
    impersonateUser,
  } from '$lib/api';
  import MultiSelect from '$lib/MultiSelect.svelte';
  import InfoHint from '$lib/InfoHint.svelte';
  import Select   from '$lib/Select.svelte';
  import ConfirmModal from '$lib/ConfirmModal.svelte';

  // Shared confirm modal — replaces native confirm() which silently
  // no-ops in iOS PWA standalone mode and looks jarring in the dark
  // UI. `confirmRef.ask(opts)` returns Promise<boolean>.
  /** @type {{
   *   ask: (opts: any) => Promise<boolean>,
   *   prompt: (opts: any) => Promise<string|null>,
   * } | null} */
  let confirmRef = $state(null);

  let users      = $state([]);
  let loading    = $state(true);
  let error      = $state('');
  let success    = $state('');
  let editing    = $state(null);
  let editForm   = $state(/** @type {Record<string,any>} */ ({}));
  let showCreate = $state(false);
  let createForm = $state({ username: '', password: '', display_name: '', email: '', phone: '', role: 'partner', contribution: 0, share_pct: 0, is_approved: true });
  let creating   = $state(false);

  async function load() {
    loading = true; error = ''; success = '';
    try {
      const data = await fetchUsers();
      users = data.users ?? [];
    } catch (e) {
      error = e.message;
    } finally { loading = false; }
  }

  async function approve(/** @type {string} */ username) {
    try { await approveUser(username); success = `${username} approved`; await load(); }
    catch (e) { error = e.message; }
  }

  async function reject(/** @type {string} */ username) {
    try { await rejectUser(username); success = `${username} rejected`; await load(); }
    catch (e) { error = e.message; }
  }

  /** Start a support session as the target user. Stashes the current
   *  admin/designated token in sessionStorage (via authStore) so the
   *  yellow banner's End button can revert without re-login. */
  async function viewAs(/** @type {any} */ user) {
    try {
      const r = await impersonateUser(user.username);
      authStore.startImpersonation(r.token, {
        username: r.username, role: r.role,
        display_name: r.display_name,
      });
      success = `Support session started as ${user.username}`;
      // Send the actor to the surface most useful for diagnosing the
      // partner's issue. /pulse is the default landing after sign-in,
      // so it's also the right place for a "view as" entry.
      goto('/pulse');
    } catch (e) {
      error = e.message;
    }
  }

  // Suspend / reinstate / terminate / toggle-super / reset-password.
  // Each is a single PUT; the table reloads after success so the new
  // pills + status badges flow in without a manual refresh.
  async function suspend(/** @type {string} */ username) {
    if (!await confirmRef.ask({
      title: 'Suspend user?',
      message: `<b>${username}</b> will be locked out until reinstated.`,
      danger: true,
      confirmLabel: 'Suspend',
    })) return;
    try { await suspendUser(username); success = `${username} suspended`; await load(); }
    catch (e) { error = e.message; }
  }

  async function reinstate(/** @type {string} */ username) {
    try { await reinstateUser(username); success = `${username} reinstated`; await load(); }
    catch (e) { error = e.message; }
  }

  async function terminate(/** @type {string} */ username) {
    if (!await confirmRef.ask({
      title: 'Terminate user?',
      message: `<b>${username}</b> will be terminated. This is logged + reversible only via direct DB intervention.`,
      danger: true,
      confirmLabel: 'Terminate',
    })) return;
    try { await terminateUser(username); success = `${username} terminated`; await load(); }
    catch (e) { error = e.message; }
  }

  async function flipDesignated(/** @type {any} */ user) {
    const next = user.role !== 'designated';
    if (!await confirmRef.ask({
      title: next ? 'Promote to designated?' : 'Demote from designated?',
      message: `<b>${user.username}</b> will ${next ? 'gain' : 'lose'} designated-partner role.`,
      confirmLabel: next ? 'Promote' : 'Demote',
    })) return;
    try {
      await toggleDesignated(user.username, next);
      success = `${user.username} role=${next ? 'designated' : 'admin'}`;
      await load();
    } catch (e) { error = e.message; }
  }

  async function resetPw(/** @type {string} */ username) {
    const pw = await confirmRef.prompt({
      title: 'Reset password',
      message: `Set a new password for <b>${username}</b>. They'll be force-logged-out from any active session.`,
      label: 'New password',
      placeholder: 'minimum 8 characters',
      inputType: 'password',
      confirmLabel: 'Reset password',
      danger: true,
    });
    if (!pw) return;
    try { await adminResetPassword(username, pw); success = `Password reset for ${username}`; }
    catch (e) { error = e.message; }
  }

  async function resendVerify(/** @type {string} */ username) {
    if (!await confirmRef.ask({
      title: 'Resend verification email?',
      message: `Send a fresh verification link to <b>${username}</b>.`,
      confirmLabel: 'Resend',
    })) return;
    try {
      const r = await resendVerification(username);
      success = r?.detail || `Verification email re-sent to ${username}`;
    } catch (e) { error = e.message; }
  }

  async function markVerifiedNow(/** @type {string} */ username) {
    if (!await confirmRef.ask({
      title: 'Mark email-verified?',
      message: `Flip <b>${username}</b>'s email-verified flag directly. No verification email is sent.`,
      confirmLabel: 'Mark verified',
    })) return;
    try {
      await markVerified(username);
      success = `${username} marked verified`;
      await load();
    } catch (e) { error = e.message; }
  }

  async function doCreate() {
    creating = true; error = '';
    if (createForm.password.length < 8) { error = 'Password must be at least 8 characters'; creating = false; return; }
    try {
      await createUser(createForm);
      success = `User ${createForm.username} created. Share the password securely.`;
      showCreate = false;
      createForm = { username: '', password: '', display_name: '', email: '', phone: '', role: 'partner', contribution: 0, share_pct: 0, is_approved: true };
      await load();
    } catch (e) { error = e.message; }
    finally { creating = false; }
  }

  function startEdit(/** @type {any} */ user) {
    editing = user.username;
    editForm = {
      display_name:    user.display_name,
      role:            user.role,
      receive_alerts:  user.receive_alerts ?? false,
      email_verified:  user.email_verified ?? false,
      email:           user.email ?? '',
      phone:           user.phone ?? '',
      pan:             user.pan ?? '',
      date_of_birth:   user.date_of_birth ?? '',
      kyc_verified:    user.kyc_verified,
      address_line1:   user.address_line1 ?? '',
      address_line2:   user.address_line2 ?? '',
      city:            user.city ?? '',
      state:           user.state ?? '',
      pincode:         user.pincode ?? '',
      contribution:    user.contribution,
      contribution_date: user.contribution_date ?? '',
      share_pct:       user.share_pct,
      bank_name:       user.bank_name ?? '',
      bank_account:    user.bank_account ?? '',
      bank_ifsc:       user.bank_ifsc ?? '',
      nominee_name:    user.nominee_name ?? '',
      nominee_relation: user.nominee_relation ?? '',
      nominee_phone:   user.nominee_phone ?? '',
      join_date:       user.join_date ?? '',
      notes:           user.notes ?? '',
    };
  }

  function cancelEdit() { editing = null; }

  async function saveEdit() {
    try {
      await updateUser(editing, editForm);
      success = `${editing} updated`;
      editing = null;
      await load();
    } catch (e) { error = e.message; }
  }

  // ── Email Partners panel ─────────────────────────────────────────────
  /** @type {'preset'|'pick'} */
  let emailMode        = $state('preset');
  let emailPreset      = $state('');   // '' | 'all_partners' | 'all_designated' | 'all_users'
  let emailPicked      = $state(/** @type {string[]} */ ([]));
  let emailSubject     = $state('');
  let emailBody        = $state('');
  let sending          = $state(false);
  let emailResult      = $state(/** @type {{kind:'ok'|'partial'|'fail', msg:string}|null} */ (null));
  let emailResultTimer = /** @type {ReturnType<typeof setTimeout>|null} */ (null);
  let showEmailHistory = $state(false);
  let emailEvents      = $state(/** @type {any[]} */ ([]));
  let emailEventsLoading = $state(false);
  let lastSentSummary  = $state(/** @type {{sent:number,total:number,at:Date}|null} */ (null));

  // When a preset is chosen, clear any specific picks (and vice-versa:
  // when the user starts picking specific users, clear the preset).
  $effect(() => {
    if (emailPreset) emailPicked = [];
  });
  $effect(() => {
    if (emailPicked.length) emailPreset = '';
  });

  const PRESET_OPTIONS = [
    { value: '',               label: 'Pick recipients…' },
    { value: 'all_partners',   label: 'All partners'     },
    { value: 'all_designated', label: 'All designated'   },
    { value: 'all_users',      label: 'All users'        },
  ];

  /** Active users with an email, available for specific-pick. */
  const pickableUsers = $derived(
    users
      .filter(u => u.is_active && u.email)
      .map(u => ({ value: u.username, label: `${u.display_name} (${u.username})`, hint: u.email }))
  );

  /** True when enough data exists to submit. */
  const emailReady = $derived(
    emailSubject.trim().length > 0 &&
    emailBody.trim().length > 0 &&
    (emailPreset !== '' || emailPicked.length > 0)
  );

  /** Computed recipient count label for the confirm dialog. */
  const emailRecipientLabel = $derived(() => {
    if (emailPreset === 'all_partners')   return `all partners (${users.filter(u=>u.role==='partner'&&u.is_active&&u.email).length})`;
    if (emailPreset === 'all_designated') return `all designated (${users.filter(u=>u.role==='designated'&&u.is_active&&u.email).length})`;
    if (emailPreset === 'all_users')      return `all users (${users.filter(u=>u.is_active&&u.email).length})`;
    return `${emailPicked.length} partner(s)`;
  });

  function _relTime(/** @type {string} */ iso) {
    const diff = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
    if (diff < 60)   return `${diff}s ago`;
    if (diff < 3600) return `${Math.floor(diff/60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff/3600)}h ago`;
    return `${Math.floor(diff/86400)}d ago`;
  }

  async function loadEmailEvents() {
    emailEventsLoading = true;
    try { emailEvents = (await fetchEmailEvents(10))?.events ?? []; }
    catch { emailEvents = []; }
    finally { emailEventsLoading = false; }
  }

  async function doSendEmail() {
    if (!emailReady) return;
    const label = emailRecipientLabel();
    if (!await confirmRef.ask({
      title: 'Send email?',
      message: `Send to <b>${label}</b>? This cannot be unsent.`,
      confirmLabel: 'Send',
      danger: false,
    })) return;

    sending = true; emailResult = null;
    if (emailResultTimer) { clearTimeout(emailResultTimer); emailResultTimer = null; }
    try {
      const body = {
        recipients: emailPreset || emailPicked,
        subject: emailSubject.trim(),
        body: emailBody.trim(),
      };
      const r = await sendPartnerEmail(body);
      const { sent_count, failed_count, total, event_id } = r;
      const eid = event_id ? ` — event #${event_id}` : '';
      if (failed_count === 0) {
        emailResult = { kind: 'ok', msg: `Sent to ${sent_count}/${total}${eid}` };
      } else if (sent_count > 0) {
        emailResult = { kind: 'partial', msg: `Sent to ${sent_count}/${total} — see history for failures${eid}` };
      } else {
        emailResult = { kind: 'fail', msg: `All ${total} failed — see history${eid}` };
      }
      lastSentSummary = { sent: sent_count, total, at: new Date() };
      await loadEmailEvents();
      emailResultTimer = setTimeout(() => { emailResult = null; }, 5000);
    } catch (e) {
      emailResult = { kind: 'fail', msg: e.message };
    } finally { sending = false; }
  }

  // ── End Email Partners panel ──────────────────────────────────────────

  onMount(() => {
    const r = $authStore.user?.role;
    if (!$authStore.user || (r !== 'admin' && r !== 'designated')) { goto('/signin'); return; }
    load();
    loadEmailEvents();
  });
</script>

<svelte:head><title>Users | RamboQuant Analytics</title></svelte:head>

<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">Users</h1>
    <InfoHint popup text="User management: approve / suspend / terminate partners. Admins and <b>designated</b> users can act; only admins can promote roles." />
  </span>
  <span class="algo-ts">{$nowStamp}</span>
  <span class="ml-auto"></span>
  <button onclick={() => showCreate = !showCreate}
    class="text-[0.65rem] py-1 px-3 rounded border border-emerald-500/50 bg-emerald-500/15 text-emerald-300 hover:bg-emerald-500/25 font-semibold">
    {showCreate ? 'Cancel' : '+ Create User'}
  </button>
  <PageHeaderActions />
</div>

<div class="algo-status-card p-5 pt-4" data-status="inactive">

  {#if success}
    <div class="mb-3 p-2 rounded bg-green-500/15 text-green-300 text-xs border border-green-500/40">{success}</div>
  {/if}
  {#if error}
    <div class="mb-3 p-2 rounded bg-red-500/15 text-red-300 text-xs border border-red-500/40">{error}</div>
  {/if}

  <!-- Create User Form -->
  {#if showCreate}
    <div class="algo-status-card p-4 mb-4" data-status="running">
      <h3 class="section-heading mb-3">New User</h3>
      <div class="grid grid-cols-2 md:grid-cols-3 gap-3">
        <div><label class="field-label">Username</label><input bind:value={createForm.username} class="field-input" placeholder="login username" /></div>
        <div><label class="field-label">Password</label><input type="password" bind:value={createForm.password} class="field-input" placeholder="min 8 chars" /></div>
        <div><label class="field-label">Full Name</label><input bind:value={createForm.display_name} class="field-input" /></div>
        <div><label class="field-label">Email</label><input type="email" bind:value={createForm.email} class="field-input" /></div>
        <div><label class="field-label">Phone</label><input bind:value={createForm.phone} class="field-input" /></div>
        <!-- Role picker — admin can only create partners. Designated
             can create any role (partner / admin / designated).
             Backend (admin.py::create_user) coerces non-designated
             attempts to 'partner' anyway, but the UI hides the
             elevated choices to match the mental model.
             {@const} is invalid here (not a direct block child) so
             the designated check is inlined in both branches. -->
        <div><label class="field-label">Role</label>
          {#if $authStore.user?.role === 'designated'}
            <Select ariaLabel="Role" bind:value={createForm.role}
              options={[
                { value: 'partner',    label: 'Partner'    },
                { value: 'admin',      label: 'Admin'      },
                { value: 'designated', label: 'Designated' },
              ]} />
          {:else}
            <div class="field-input field-input-readonly">Partner</div>
          {/if}
        </div>
        <!-- Capital + share % — designated-only. Backend silently
             coerces non-designated attempts to 0.0, but the inputs
             are hidden for admin so the form matches the policy. -->
        {#if $authStore.user?.role === 'designated'}
          <div><label class="field-label">Contribution (₹)</label><input type="number" bind:value={createForm.contribution} class="field-input" /></div>
          <div><label class="field-label">Profit Share (%)</label><input type="number" step="0.1" bind:value={createForm.share_pct} class="field-input" /></div>
        {/if}
        <div class="flex items-end">
          <button onclick={doCreate} disabled={creating || !createForm.username || !createForm.password}
            class="btn-primary text-[0.65rem] py-1.5 px-4 disabled:opacity-50">
            {creating ? 'Creating…' : 'Create'}
          </button>
        </div>
      </div>
    </div>
  {/if}

  {#if loading}
    <div class="text-center text-[#7e97b8] text-xs animate-pulse py-8">Loading users…</div>
  {:else if !users.length}
    <p class="text-xs text-[#7e97b8]">No users registered.</p>
  {:else}
    <div class="space-y-3">
      {#each users as user}
        {@const isSelf = user.username === $authStore.user?.username}
        {@const iAmDesignated = $authStore.user?.role === 'designated'}
        {@const iAmAdmin = $authStore.user?.role === 'admin'}
        {@const targetIsPartner = user.role === 'partner'}
        <div class="algo-status-card p-3" data-status={user.is_active ? (user.is_approved ? 'active' : 'running') : 'error'}>
          <!-- Header row -->
          <div class="flex items-center justify-between mb-2">
            <div class="flex items-center flex-wrap gap-1.5">
              <span class="font-semibold text-xs text-[#fbbf24]">{user.display_name}</span>
              <span class="text-xs text-[#c8d8f0]/70">@{user.username}</span>
              {#if isSelf}
                <span class="px-1.5 py-0.5 rounded bg-sky-500/15 text-sky-300 text-[0.6rem] font-semibold uppercase border border-sky-500/40">You</span>
              {/if}
              <span class="text-[0.6rem] text-[#7e97b8] font-mono">{user.account_id}</span>
              <span class="px-1.5 py-0.5 rounded text-[0.6rem] font-semibold uppercase border
                {user.role === 'designated'
                  ? 'bg-violet-500/15 text-violet-300 border-violet-500/40'
                  : user.role === 'admin'
                    ? 'bg-amber-500/15 text-amber-300 border-amber-500/40'
                    : 'bg-teal-500/15 text-teal-300 border-teal-500/40'}">
                {user.role}
              </span>
              {#if user.terminated_at}
                <span class="px-1.5 py-0.5 rounded bg-zinc-500/20 text-zinc-300 text-[0.6rem] font-semibold uppercase border border-zinc-500/50">Terminated</span>
              {:else if user.suspended_at}
                <span class="px-1.5 py-0.5 rounded bg-orange-500/15 text-orange-300 text-[0.6rem] font-semibold uppercase border border-orange-500/40">Suspended</span>
              {:else if !user.is_approved}
                <span class="px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-300 text-[0.6rem] font-semibold uppercase border border-amber-500/40">Pending</span>
              {:else if !user.is_active}
                <span class="px-1.5 py-0.5 rounded bg-red-500/15 text-red-300 text-[0.6rem] font-semibold uppercase border border-red-500/40">Inactive</span>
              {/if}
              {#if user.email_verified}
                <span class="px-1.5 py-0.5 rounded bg-sky-500/15 text-sky-300 text-[0.6rem] font-semibold uppercase border border-sky-500/40" title="Email verified">✉ Verified</span>
              {/if}
              {#if user.kyc_verified}
                <span class="px-1.5 py-0.5 rounded bg-green-500/15 text-green-300 text-[0.6rem] font-semibold uppercase border border-green-500/40">KYC</span>
              {/if}
              {#if user.role === 'designated' || (user.role === 'admin' && user.receive_alerts)}
                <span class="px-1.5 py-0.5 rounded bg-yellow-500/15 text-yellow-300 text-[0.6rem] font-semibold uppercase border border-yellow-500/40" title="Receives platform alerts (loss / agent / summary)">🔔 Alerts</span>
              {/if}
            </div>
            <div class="flex gap-1.5 flex-wrap justify-end">
              <!-- Approve / Reject — designated only, on partner-grade pending rows. -->
              {#if iAmDesignated && !user.is_approved && user.is_active && !user.terminated_at && targetIsPartner}
                <button onclick={() => approve(user.username)} class="btn-primary text-[0.65rem] py-1 px-2">Approve</button>
                <button onclick={() => reject(user.username)}  class="btn-secondary text-[0.65rem] py-1 px-2">Reject</button>
              {/if}
              <!-- Suspend / Reinstate — designated on anyone, admin
                   on partners only, never self. -->
              {#if !user.terminated_at && !isSelf && (iAmDesignated || targetIsPartner)}
                {#if user.suspended_at}
                  <button onclick={() => reinstate(user.username)} class="btn-secondary text-[0.65rem] py-1 px-2 border-orange-400/50 text-orange-300">Reinstate</button>
                {:else}
                  <button onclick={() => suspend(user.username)} class="btn-secondary text-[0.65rem] py-1 px-2">Suspend</button>
                {/if}
              {/if}
              <!-- Reset PW — designated on anyone (not self), admin on partners only. -->
              {#if !user.terminated_at && !isSelf && (iAmDesignated || targetIsPartner)}
                <button onclick={() => resetPw(user.username)} class="btn-secondary text-[0.65rem] py-1 px-2">Reset PW</button>
              {/if}
              <!-- Resend verify — only for unverified rows; same gate as Reset PW. -->
              {#if !user.terminated_at && !isSelf && !user.email_verified && user.email && (iAmDesignated || targetIsPartner)}
                <button onclick={() => resendVerify(user.username)} class="btn-secondary text-[0.65rem] py-1 px-2 border-sky-400/50 text-sky-300">Resend Verify</button>
              {/if}
              <!-- Mark verified directly — designated only, no email. -->
              {#if iAmDesignated && !user.terminated_at && !isSelf && !user.email_verified}
                <button onclick={() => markVerifiedNow(user.username)} class="btn-secondary text-[0.65rem] py-1 px-2 border-emerald-400/50 text-emerald-300">Mark Verified</button>
              {/if}
              <!-- Terminate — designated only, never self, target must not already be designated. -->
              {#if iAmDesignated && user.role !== 'designated' && !user.terminated_at && !isSelf}
                <button onclick={() => terminate(user.username)} class="btn-secondary text-[0.65rem] py-1 px-2 border-red-400/50 text-red-300">Terminate</button>
              {/if}
              <!-- Promote / Demote between admin and designated — designated only, never self, never partner. -->
              {#if iAmDesignated && !isSelf && user.role !== 'partner'}
                <button onclick={() => flipDesignated(user)} class="btn-secondary text-[0.65rem] py-1 px-2 border-violet-400/50 text-violet-300">
                  {user.role === 'designated' ? 'Demote' : 'Promote'}
                </button>
              {/if}
              <!-- Edit profile — designated on anyone, admin on self
                   or partner targets. Mirrors the backend gate
                   _check_action(admin_self_ok=True, admin_partner_ok=True). -->
              {#if editing !== user.username && !user.terminated_at && (iAmDesignated || isSelf || (iAmAdmin && targetIsPartner))}
                <button onclick={() => startEdit(user)} class="btn-secondary text-[0.65rem] py-1 px-2">Edit</button>
              {/if}
              <!-- View as — start a 30-min support session as this
                   user. Designated can view ANY user; admin can only
                   view partners. Never self (no point), never a
                   terminated/suspended row (backend rejects). Backend
                   gates duplicate this; the UI filter just avoids
                   surfacing a button that'd fail. -->
              {#if !isSelf && !user.terminated_at && !user.suspended_at
                   && (iAmDesignated || (iAmAdmin && targetIsPartner))}
                <button
                  onclick={() => viewAs(user)}
                  class="btn-secondary text-[0.65rem] py-1 px-2 border-amber-400/50 text-amber-300"
                  title="Start a 30-min support session as {user.username} — view the platform exactly as they do"
                >View as</button>
              {/if}
            </div>
          </div>

          {#if editing !== user.username}
            <!-- Read-only summary -->
            <div class="grid grid-cols-2 md:grid-cols-4 gap-x-4 gap-y-1 text-xs text-[#c8d8f0]/80">
              <div><span class="text-muted">Email:</span> {user.email || '—'}</div>
              <div><span class="text-muted">Phone:</span> {user.phone || '—'}</div>
              <div><span class="text-muted">PAN:</span> {user.pan || '—'}</div>
              <div><span class="text-muted">Contribution:</span> ₹{user.contribution.toLocaleString('en-IN')}</div>
              <div><span class="text-muted">Contributed:</span> {user.contribution_date || '—'}</div>
              <div><span class="text-muted">Share:</span> {user.share_pct}%</div>
              <div><span class="text-muted">Joined:</span> {user.join_date || '—'}</div>
              <div><span class="text-muted">Nominee:</span> {user.nominee_name || '—'}</div>
            </div>
          {:else}
            <!-- Edit form -->
            <div class="mt-3 space-y-4">
              <div>
                <h3 class="section-heading mb-2">Personal</h3>
                <div class="grid grid-cols-2 md:grid-cols-3 gap-3">
                  <div><label class="field-label">Display Name</label><input bind:value={editForm.display_name} class="field-input" /></div>
                  <div><label class="field-label">Email</label><input type="email" bind:value={editForm.email} class="field-input" /></div>
                  <div><label class="field-label">Phone</label><input bind:value={editForm.phone} class="field-input" /></div>
                  <div><label class="field-label">PAN</label><input bind:value={editForm.pan} class="field-input" maxlength="10" style="text-transform:uppercase" /></div>
                  <div><label class="field-label">Date of Birth</label><input type="date" bind:value={editForm.date_of_birth} class="field-input" /></div>
                  <div class="flex items-end gap-2">
                    <label class="field-label">KYC Verified</label>
                    <input type="checkbox" bind:checked={editForm.kyc_verified} class="mt-1" />
                  </div>
                  <!-- Email Verified — designated only. Admin actors
                       see the state but can't toggle (server-side
                       drops the field silently for non-designated).
                       Admin's path to mark a partner verified is the
                       Resend Verify button + the user clicking the
                       email link.
                  -->
                  <div class="flex items-end gap-2">
                    <label class="field-label" title="Designated only — manually flips email_verified without going through the email-token flow.">Email Verified</label>
                    <input type="checkbox"
                           bind:checked={editForm.email_verified}
                           disabled={!iAmDesignated}
                           class="mt-1" />
                  </div>
                  <!-- receive_alerts only meaningful for admin/designated;
                       for partner rows the backend ignores the value, but
                       hide the field anyway to keep the form tidy. The
                       designated tier always receives alerts and can't
                       opt out, so the checkbox is shown disabled+checked
                       as a visual hint. -->
                  {#if user.role !== 'partner'}
                    <div class="flex items-end gap-2">
                      <label class="field-label" title="Send platform alerts (loss, agent fires, summaries) to this user's email.">Receive Alerts</label>
                      <input type="checkbox"
                             bind:checked={editForm.receive_alerts}
                             disabled={user.role === 'designated'}
                             class="mt-1" />
                    </div>
                  {/if}
                </div>
              </div>
              <div>
                <h3 class="section-heading mb-2">Address</h3>
                <div class="grid grid-cols-2 md:grid-cols-3 gap-3">
                  <div class="col-span-2 md:col-span-3"><label class="field-label">Address Line 1</label><input bind:value={editForm.address_line1} class="field-input" /></div>
                  <div class="col-span-2 md:col-span-3"><label class="field-label">Address Line 2</label><input bind:value={editForm.address_line2} class="field-input" /></div>
                  <div><label class="field-label">City</label><input bind:value={editForm.city} class="field-input" /></div>
                  <div><label class="field-label">State</label><input bind:value={editForm.state} class="field-input" /></div>
                  <div><label class="field-label">Pincode</label><input bind:value={editForm.pincode} class="field-input" maxlength="6" /></div>
                </div>
              </div>
              <div>
                <h3 class="section-heading mb-2">Investment</h3>
                <div class="grid grid-cols-2 md:grid-cols-3 gap-3">
                  <div><label class="field-label">Role</label>
                    <Select ariaLabel="Role" bind:value={editForm.role}
                      options={[
                        { value: 'partner', label: 'Partner' },
                        { value: 'admin',   label: 'Admin'   },
                      ]} />
                  </div>
                  <!-- Capital + share % + contribution date — designated-
                       only. Backend (admin.py::update_user) silently
                       drops these from non-designated PATCH bodies; the
                       UI hides the inputs to match the policy. Admin
                       still SEES the values in the user-card display
                       above (read-only). -->
                  {#if iAmDesignated}
                    <div><label class="field-label">Contribution (₹)</label><input type="number" bind:value={editForm.contribution} class="field-input" /></div>
                    <div><label class="field-label">Contribution Date</label><input type="date" bind:value={editForm.contribution_date} class="field-input" /></div>
                    <div><label class="field-label">Profit Share (%)</label><input type="number" step="0.1" bind:value={editForm.share_pct} class="field-input" /></div>
                  {/if}
                  <div><label class="field-label">Join Date</label><input type="date" bind:value={editForm.join_date} class="field-input" /></div>
                </div>
              </div>
              <div>
                <h3 class="section-heading mb-2">Bank Details</h3>
                <div class="grid grid-cols-2 md:grid-cols-3 gap-3">
                  <div><label class="field-label">Bank Name</label><input bind:value={editForm.bank_name} class="field-input" /></div>
                  <div><label class="field-label">Account Number</label><input bind:value={editForm.bank_account} class="field-input" /></div>
                  <div><label class="field-label">IFSC</label><input bind:value={editForm.bank_ifsc} class="field-input" /></div>
                </div>
              </div>
              <div>
                <h3 class="section-heading mb-2">Nominee</h3>
                <div class="grid grid-cols-2 md:grid-cols-3 gap-3">
                  <div><label class="field-label">Name</label><input bind:value={editForm.nominee_name} class="field-input" /></div>
                  <div><label class="field-label">Relation</label><input bind:value={editForm.nominee_relation} class="field-input" placeholder="Spouse, Child, etc." /></div>
                  <div><label class="field-label">Phone</label><input bind:value={editForm.nominee_phone} class="field-input" /></div>
                </div>
              </div>
              <div>
                <h3 class="section-heading mb-2">Notes</h3>
                <textarea bind:value={editForm.notes} class="field-input" rows="2" placeholder="Admin notes…"></textarea>
              </div>
              <div class="flex gap-2 pt-1">
                <button onclick={saveEdit} class="btn-primary text-[0.65rem] py-1 px-4">Save</button>
                <button onclick={cancelEdit} class="btn-secondary text-[0.65rem] py-1 px-4">Cancel</button>
              </div>
            </div>
          {/if}
        </div>
      {/each}
    </div>
  {/if}
</div>

{#if users.length > 0}
<!-- ── Email Partners panel ─────────────────────────────────────────── -->
<section class="email-panel algo-status-card p-5 pt-4 mt-4" data-status="inactive">
  <!-- Header -->
  <div class="flex items-center justify-between mb-1 gap-2 flex-wrap">
    <h2 class="text-sm font-bold uppercase tracking-wider text-[#fbbf24] mb-0">Email Partners</h2>
    <button
      onclick={() => { showEmailHistory = !showEmailHistory; if (showEmailHistory) loadEmailEvents(); }}
      class="text-[0.62rem] text-[#7dd3fc] hover:text-[#bae6fd] font-mono flex items-center gap-1 transition-colors">
      {showEmailHistory ? 'hide history ▴' : 'show history ▾'}
    </button>
  </div>
  <div class="border-b border-[rgba(251,191,36,0.25)] mb-4"></div>

  <!-- History panel -->
  {#if showEmailHistory}
    <div class="mb-4 rounded border border-[rgba(125,211,252,0.25)] bg-[rgba(14,22,44,0.5)] p-3">
      <div class="text-[0.6rem] text-[#7e97b8] uppercase font-bold mb-2 tracking-wider">Recent send history</div>
      {#if emailEventsLoading}
        <div class="text-xs text-[#7e97b8] animate-pulse">Loading…</div>
      {:else if emailEvents.length === 0}
        <div class="text-xs text-[#7e97b8]">No sends yet.</div>
      {:else}
        <div class="space-y-1.5">
          {#each emailEvents as ev}
            {@const hasFail = (ev.failed_count ?? 0) > 0}
            <div class="font-mono text-[0.6rem] flex flex-wrap gap-x-2 gap-y-0.5 leading-relaxed
              {hasFail ? 'text-red-300' : 'text-[#c8d8f0]/80'}">
              <span class="tabular-nums opacity-70">{_relTime(ev.created_at)}</span>
              <span>·</span>
              <span class="text-[#fbbf24]/80">{ev.actor ?? '—'}</span>
              <span>→</span>
              <span>{ev.recipients_label ?? ev.recipients ?? '—'}</span>
              <span>·</span>
              <span class="tabular-nums">{ev.sent_count ?? 0}/{ev.total ?? 0}</span>
              <span>·</span>
              <span class="truncate max-w-[16rem]" title={ev.subject}>{ev.subject ?? '—'}</span>
            </div>
          {/each}
        </div>
      {/if}
    </div>
  {/if}

  <!-- Recipient row -->
  <div class="grid grid-cols-1 md:grid-cols-2 gap-3 mb-3">
    <div>
      <label class="field-label">Preset recipients</label>
      <Select ariaLabel="Recipient preset" bind:value={emailPreset}
        options={PRESET_OPTIONS} />
    </div>
    <div>
      <label class="field-label" class:opacity-40={emailPreset !== ''}>Or pick specific</label>
      <MultiSelect
        ariaLabel="Pick recipients"
        bind:value={emailPicked}
        options={pickableUsers}
        placeholder="Select users…"
        disabled={emailPreset !== ''} />
    </div>
  </div>

  <!-- Subject -->
  <div class="mb-1">
    <label class="field-label">Subject</label>
    <input
      bind:value={emailSubject}
      maxlength="200"
      class="field-input w-full"
      placeholder="Email subject…" />
    <div class="text-[0.58rem] text-[#7e97b8] text-right tabular-nums mt-0.5">{emailSubject.length}/200</div>
  </div>

  <!-- Body -->
  <div class="mb-3">
    <label class="field-label">Body</label>
    <textarea
      bind:value={emailBody}
      rows="8"
      maxlength="50000"
      class="field-input w-full resize-y font-mono text-[0.65rem]"
      placeholder="Message body…"></textarea>
    <div class="text-[0.58rem] text-[#7e97b8] flex justify-between tabular-nums mt-0.5">
      <span>{emailBody.split('\n').length} lines</span>
      <span>{emailBody.length}/50000</span>
    </div>
  </div>

  <!-- Footer: Send button + last-sent summary -->
  <div class="flex items-center justify-between gap-3 flex-wrap">
    <button
      onclick={doSendEmail}
      disabled={!emailReady || sending}
      class="btn-primary text-[0.65rem] py-1.5 px-5 disabled:opacity-40 flex items-center gap-2">
      {#if sending}
        <span class="inline-block h-3 w-3 rounded-full border-2 border-[#fbbf24] border-t-transparent animate-spin"></span>
        Sending…
      {:else}
        Send to {
          emailPreset === 'all_partners'   ? `${users.filter(u=>u.role==='partner'&&u.is_active&&u.email).length} partner(s)` :
          emailPreset === 'all_designated' ? `${users.filter(u=>u.role==='designated'&&u.is_active&&u.email).length} designated` :
          emailPreset === 'all_users'      ? `${users.filter(u=>u.is_active&&u.email).length} user(s)` :
          emailPicked.length > 0           ? `${emailPicked.length} partner(s)` :
          'partners'
        }
      {/if}
    </button>
    {#if lastSentSummary && !emailResult}
      <span class="text-[0.6rem] text-[#7e97b8] tabular-nums font-mono">
        last sent: {_relTime(lastSentSummary.at.toISOString())} — sent {lastSentSummary.sent}/{lastSentSummary.total}
      </span>
    {/if}
  </div>

  <!-- Result strip -->
  {#if emailResult}
    <div class="mt-3 p-2 rounded text-xs border font-mono tabular-nums
      {emailResult.kind === 'ok'      ? 'bg-green-500/15 text-green-300 border-green-500/40' :
       emailResult.kind === 'partial' ? 'bg-amber-500/15 text-amber-300 border-amber-500/40' :
                                        'bg-red-500/15 text-red-300 border-red-500/40'}">
      {emailResult.kind === 'ok' ? '✓' : emailResult.kind === 'partial' ? '⚠' : '✗'} {emailResult.msg}
    </div>
  {/if}
</section>
{/if}

<!-- Shared confirm dialog — every destructive admin action funnels
     through confirmRef.ask(). Replaces native confirm() which is
     silently no-op'd in iOS PWA standalone mode. -->
<ConfirmModal bind:this={confirmRef} />
