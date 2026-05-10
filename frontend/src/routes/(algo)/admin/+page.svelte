<script>
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { authStore, clientTimestamp } from '$lib/stores';
  import {
    fetchUsers, approveUser, rejectUser, updateUser, createUser,
    suspendUser, reinstateUser, terminateUser, toggleDesignated, adminResetPassword,
    resendVerification,
  } from '$lib/api';

  let users    = $state([]);
  let loading  = $state(true);
  let error    = $state('');
  let success  = $state('');
  let editing  = $state(null);
  let editForm = $state(/** @type {Record<string,any>} */ ({}));
  let showCreate = $state(false);
  let createForm = $state({ username: '', password: '', display_name: '', email: '', phone: '', role: 'partner', contribution: 0, share_pct: 0, is_approved: true });
  let creating = $state(false);

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

  // Suspend / reinstate / terminate / toggle-super / reset-password.
  // Each is a single PUT; the table reloads after success so the new
  // pills + status badges flow in without a manual refresh.
  async function suspend(/** @type {string} */ username) {
    if (!confirm(`Suspend ${username}? They'll be locked out until reinstated.`)) return;
    try { await suspendUser(username); success = `${username} suspended`; await load(); }
    catch (e) { error = e.message; }
  }

  async function reinstate(/** @type {string} */ username) {
    try { await reinstateUser(username); success = `${username} reinstated`; await load(); }
    catch (e) { error = e.message; }
  }

  async function terminate(/** @type {string} */ username) {
    if (!confirm(`Terminate ${username}? This is logged + reversible only via DB.`)) return;
    try { await terminateUser(username); success = `${username} terminated`; await load(); }
    catch (e) { error = e.message; }
  }

  async function flipDesignated(/** @type {any} */ user) {
    const next = user.role !== 'designated';
    if (!confirm(`${next ? 'Promote' : 'Demote'} ${user.username} ${next ? 'to' : 'from'} designated?`)) return;
    try {
      await toggleDesignated(user.username, next);
      success = `${user.username} role=${next ? 'designated' : 'admin'}`;
      await load();
    } catch (e) { error = e.message; }
  }

  async function resetPw(/** @type {string} */ username) {
    const pw = prompt(`Set a new password for ${username}. They'll be force-logged-out.`);
    if (!pw) return;
    try { await adminResetPassword(username, pw); success = `Password reset for ${username}`; }
    catch (e) { error = e.message; }
  }

  async function resendVerify(/** @type {string} */ username) {
    if (!confirm(`Resend verification email to ${username}?`)) return;
    try {
      const r = await resendVerification(username);
      success = r?.detail || `Verification email re-sent to ${username}`;
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

  onMount(() => {
    const r = $authStore.user?.role;
    if (!$authStore.user || (r !== 'admin' && r !== 'designated')) { goto('/signin'); return; }
    load();
  });
</script>

<div class="algo-status-card p-5 pt-4" data-status="inactive">
  <div class="flex items-center justify-between mb-1 gap-2 flex-wrap">
    <h1 class="text-sm font-bold uppercase tracking-wider text-[#fbbf24] mb-0">User Management</h1>
    <span class="algo-ts">{clientTimestamp()}</span>
    <button onclick={() => showCreate = !showCreate}
      class="text-[0.65rem] py-1 px-3 rounded border border-[#fbbf24]/50 bg-[#fbbf24]/15 text-[#fbbf24] hover:bg-[#fbbf24]/25 font-semibold">
      {showCreate ? 'Cancel' : 'Create User'}
    </button>
  </div>
  <div class="border-b border-[rgba(251,191,36,0.25)] mb-4"></div>

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
        <div><label class="field-label">Role</label>
          <select bind:value={createForm.role} class="field-input">
            <option value="partner">Partner</option>
            <option value="admin">Admin</option>
          </select>
        </div>
        <div><label class="field-label">Contribution (₹)</label><input type="number" bind:value={createForm.contribution} class="field-input" /></div>
        <div><label class="field-label">Profit Share (%)</label><input type="number" step="0.1" bind:value={createForm.share_pct} class="field-input" /></div>
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
              <!-- Edit profile — designated on anyone, admin on self only. -->
              {#if editing !== user.username && !user.terminated_at && (iAmDesignated || isSelf)}
                <button onclick={() => startEdit(user)} class="btn-secondary text-[0.65rem] py-1 px-2">Edit</button>
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
                  <!-- Email Verified — designated on anyone, admin
                       on partner targets only. Disabled when the
                       actor can't toggle this row's flag (server-side
                       drops the field silently in that case anyway).
                  -->
                  <div class="flex items-end gap-2">
                    <label class="field-label" title="Manually flip email_verified without going through the email-token flow.">Email Verified</label>
                    <input type="checkbox"
                           bind:checked={editForm.email_verified}
                           disabled={!iAmDesignated && !(iAmAdmin && targetIsPartner)}
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
                    <select bind:value={editForm.role} class="field-input">
                      <option value="partner">Partner</option>
                      <option value="admin">Admin</option>
                    </select>
                  </div>
                  <div><label class="field-label">Contribution (₹)</label><input type="number" bind:value={editForm.contribution} class="field-input" /></div>
                  <div><label class="field-label">Contribution Date</label><input type="date" bind:value={editForm.contribution_date} class="field-input" /></div>
                  <div><label class="field-label">Profit Share (%)</label><input type="number" step="0.1" bind:value={editForm.share_pct} class="field-input" /></div>
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
