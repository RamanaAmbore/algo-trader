<script>
  import { onMount, onDestroy } from 'svelte';
  import { goto } from '$app/navigation';
  import { authStore } from '$lib/stores';
  import AlgoTimestamp from '$lib/AlgoTimestamp.svelte';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import DisclosureChevron from '$lib/DisclosureChevron.svelte';
  import {
    fetchUsers, approveUser, rejectUser, updateUser, createUser,
    suspendUser, reinstateUser, terminateUser, toggleDesignated, adminResetPassword,
    resendVerification, markVerified,
    sendPartnerEmail, fetchEmailEvents,
    impersonateUser,
    fetchInvestorTokens, mintInvestorToken, revokeInvestorToken,
    fetchInvestorEvents, createInvestorEvent, deleteInvestorEvent,
  } from '$lib/api';
  import MultiSelect from '$lib/MultiSelect.svelte';
  import Select   from '$lib/Select.svelte';
  import ConfirmModal from '$lib/ConfirmModal.svelte';
  import AlgoTabs from '$lib/AlgoTabs.svelte';
  import LoadingSkeleton from '$lib/LoadingSkeleton.svelte';
  import ModalShell from '$lib/ModalShell.svelte';

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

  // Investor portal — token mint/list/revoke modal state. Opened
  // via the per-user "Portal" button; the operator mints a long-
  // lived URL and forwards it to the LP through their own channel.
  /** @type {{id:number, username:string}|null} */
  let portalUser   = $state(null);
  /** @type {any[]} */
  let portalTokens = $state([]);
  let portalLoading = $state(false);
  let portalMintBusy = $state(false);
  let portalMintDays = $state(90);
  let portalMintNote = $state('');
  /** Freshly-minted token. Surfaces ONCE — closed/replaced on the
   *  next mint or modal close. Includes the resolved full URL so
   *  the operator clicks Copy and pastes into WhatsApp/email. */
  /** @type {{token:string, url:string, expires_at:string}|null} */
  let portalFresh  = $state(null);
  let portalError  = $state('');

  async function openPortal(/** @type {any} */ user) {
    portalUser   = { id: user.id, username: user.username };
    portalTokens = []; portalFresh = null; portalError = '';
    portalMintDays = 90; portalMintNote = '';
    portalLoading = true;
    try {
      const r = await fetchInvestorTokens(user.id);
      portalTokens = r?.rows ?? [];
    } catch (e) { portalError = e?.message || 'Failed to load tokens'; }
    finally { portalLoading = false; }
  }

  function closePortal() {
    portalUser = null;
    portalTokens = []; portalFresh = null; portalError = '';
  }

  async function mintPortal() {
    if (!portalUser || portalMintBusy) return;
    portalMintBusy = true; portalError = '';
    try {
      const r = await mintInvestorToken(portalUser.id, {
        expires_in_days: portalMintDays,
        note: portalMintNote || undefined,
      });
      const origin = (typeof window !== 'undefined' && window.location?.origin) || '';
      portalFresh = {
        token: r.token,
        url: origin + r.portal_url,
        expires_at: r.expires_at,
      };
      portalMintNote = '';
      // Re-list so the new row shows in the table immediately.
      try {
        const list = await fetchInvestorTokens(portalUser.id);
        portalTokens = list?.rows ?? [];
      } catch (_) {}
    } catch (e) { portalError = e?.message || 'Failed to mint token'; }
    finally { portalMintBusy = false; }
  }

  async function revokePortal(/** @type {number} */ tokenId) {
    if (!portalUser) return;
    if (!await confirmRef.ask({
      title: 'Revoke investor portal token?',
      body: 'The URL will stop working immediately. The LP will see an "expired" error on their next visit.',
      confirmLabel: 'Revoke',
      kind: 'danger',
    })) return;
    portalError = '';
    try {
      await revokeInvestorToken(portalUser.id, tokenId);
      const list = await fetchInvestorTokens(portalUser.id);
      portalTokens = list?.rows ?? [];
    } catch (e) { portalError = e?.message || 'Failed to revoke'; }
  }

  // Statement preview — admin-side check of what the LP will see.
  // Uses fetch + blob because the admin endpoint is auth-gated; a
  // plain anchor would 401 (no Bearer header sent on direct nav).
  let stmtBusy = $state(false);
  let stmtYear  = $state(/** @type {number} */ (new Date().getFullYear()));
  let stmtMonth = $state(/** @type {number} */ (
    new Date().getMonth() === 0 ? 12 : new Date().getMonth()
  ));
  async function previewStatement() {
    if (!portalUser || stmtBusy) return;
    stmtBusy = true; portalError = '';
    try {
      const tok = localStorage.getItem('ramboq.token') || '';
      const url = `/api/admin/users/${portalUser.id}/statement/${stmtYear}/${stmtMonth}`;
      const res = await fetch(url, {
        headers: tok ? { Authorization: `Bearer ${tok}` } : {},
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body?.detail || `Preview failed (${res.status})`);
      }
      const blob = await res.blob();
      const dlUrl = URL.createObjectURL(blob);
      // Open in new tab so the admin can quickly scan + close;
      // download attribute would force save-as which is more friction
      // for a preview workflow.
      window.open(dlUrl, '_blank');
      setTimeout(() => URL.revokeObjectURL(dlUrl), 60_000);
    } catch (e) {
      portalError = e?.message || 'Preview failed';
    } finally {
      stmtBusy = false;
    }
  }

  // Investor events — subscription / redemption / bootstrap log.
  // Passive today; the next slice flips NAV math to consume events.
  /** @type {any[]} */
  let evRows = $state([]);
  let evTotals = $state({ total_units: 0, total_in: 0, total_out: 0 });
  let evLoading = $state(false);
  let evBusy = $state(false);
  let evForm = $state({
    event_type: 'subscription',
    event_date: new Date().toISOString().slice(0, 10),
    amount: 0,
    nav_per_unit: 1,
    note: '',
  });
  let portalTab = $state(/** @type {'tokens' | 'statement' | 'events'} */ ('tokens'));

  async function loadEvents() {
    if (!portalUser) return;
    evLoading = true; portalError = '';
    try {
      const r = await fetchInvestorEvents(portalUser.id);
      evRows = r.rows ?? [];
      evTotals = {
        total_units: r.total_units ?? 0,
        total_in:    r.total_in    ?? 0,
        total_out:   r.total_out   ?? 0,
      };
    } catch (e) { portalError = e?.message || 'Failed to load events'; }
    finally { evLoading = false; }
  }

  async function addEvent() {
    if (!portalUser || evBusy) return;
    if (!evForm.amount || evForm.amount <= 0) {
      portalError = 'Amount must be > 0'; return;
    }
    if (!evForm.nav_per_unit || evForm.nav_per_unit <= 0) {
      portalError = 'NAV per unit must be > 0'; return;
    }
    evBusy = true; portalError = '';
    try {
      await createInvestorEvent(portalUser.id, {
        event_type:   evForm.event_type,
        event_date:   evForm.event_date,
        amount:       Number(evForm.amount),
        nav_per_unit: Number(evForm.nav_per_unit),
        note:         evForm.note || undefined,
      });
      evForm = { ...evForm, amount: 0, note: '' };
      await loadEvents();
    } catch (e) { portalError = e?.message || 'Failed to add event'; }
    finally { evBusy = false; }
  }

  async function removeEvent(/** @type {number} */ id) {
    if (!portalUser || evBusy) return;
    if (!await confirmRef.ask({
      title: 'Delete event?',
      body: 'This removes the event from the LP\'s journal. No undo — once deleted, the row is gone.',
      confirmLabel: 'Delete',
      kind: 'danger',
    })) return;
    evBusy = true; portalError = '';
    try {
      await deleteInvestorEvent(portalUser.id, id);
      await loadEvents();
    } catch (e) { portalError = e?.message || 'Failed to delete event'; }
    finally { evBusy = false; }
  }

  // Auto-load events on tab switch (lazy: only first visit per modal
  // open hits the endpoint).
  $effect(() => {
    if (portalUser && portalTab === 'events' && evRows.length === 0 && !evLoading) {
      loadEvents();
    }
  });
  // Reset tab + rows on new modal open.
  $effect(() => {
    if (portalUser) {
      portalTab = 'tokens'; evRows = [];
      evTotals = { total_units: 0, total_in: 0, total_out: 0 };
    }
  });

  async function copyPortalUrl() {
    if (!portalFresh) return;
    try { await navigator.clipboard.writeText(portalFresh.url); success = 'Portal URL copied to clipboard'; }
    catch { /* clipboard API may be unavailable on http:// — operator can still select + copy */ }
  }

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
      // Slice 5 — RBAC horizontal scope. Designated-only fields; the
      // backend silently drops them for non-designated actors so it's
      // OK to seed the form unconditionally — the role-aware UI hides
      // the inputs for non-designated.
      assigned_accounts:   [...(user.assigned_accounts || [])],
      assigned_strategies: [...(user.assigned_strategies || [])],
      compliance_designated: !!user.compliance_designated,
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
  // Preset values MUST match the backend `EmailPartnersRequest.recipients`
  // preset strings — `all-partners`, `all-designated`, `all`. Using
  // underscore variants silently fails the backend preset lookup and
  // falls through to the "recipients must be a list…" 422 branch.
  let emailPreset      = $state('');   // '' | 'all-partners' | 'all-designated' | 'all'
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
    { value: '',                label: 'Pick recipients…' },
    { value: 'all-partners',    label: 'All partners'     },
    { value: 'all-designated',  label: 'All designated'   },
    { value: 'all',             label: 'All users'        },
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

  /** Resolved email addresses for the selected preset (or empty array when manual).
   * email_verified is deliberately NOT filtered here — operator-directed blasts
   * should reach all partners with an email on file, regardless of whether they
   * completed the self-registration verification flow. */
  const emailResolvedRecipients = $derived(
    emailPreset === 'all-partners'
      ? users.filter(u => (u.role === 'partner' || u.role === 'designated') && (u.share_pct || 0) > 0 && u.is_active && u.email).map(u => u.email)
      : emailPreset === 'all-designated'
      ? users.filter(u => u.role === 'designated' && (u.share_pct || 0) > 0 && u.is_active && u.email).map(u => u.email)
      : emailPreset === 'all'
      ? users.filter(u => u.is_active && u.email).map(u => u.email)
      : []
  );

  /** Count label shown below the preset dropdown. */
  const emailPresetCount = $derived(
    emailPreset !== '' ? emailResolvedRecipients.length : emailPicked.length
  );

  /** Computed recipient count label for the confirm dialog. */
  const emailRecipientLabel = $derived(() => {
    if (emailPreset === 'all-partners')   return `all partners (${emailResolvedRecipients.length})`;
    if (emailPreset === 'all-designated') return `all designated (${emailResolvedRecipients.length})`;
    if (emailPreset === 'all')            return `all users (${emailResolvedRecipients.length})`;
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
        recipients: emailPreset ? emailResolvedRecipients : emailPicked,
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

  onDestroy(() => {
    if (emailResultTimer) clearTimeout(emailResultTimer);
  });
</script>

<svelte:head><title>Users | RamboQuant Analytics</title></svelte:head>

<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">Users</h1>
  </span>
  <AlgoTimestamp />
  <!-- Content-action button is LEFT-aligned per canonical header rule
       (only Refresh + Order + Chart + Activity + Collapse + Fullscreen
       + Default-size icons sit RIGHT of the ml-auto spacer). -->
  <button onclick={() => showCreate = !showCreate}
    class="text-[0.65rem] py-1 px-3 rounded border border-emerald-500/50 bg-emerald-500/15 text-emerald-300 hover:bg-emerald-500/25 font-semibold">
    {showCreate ? 'Cancel' : '+ Create User'}
  </button>
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <RefreshButton onClick={load} {loading} label="users" />
    <PageHeaderActions />
  </span>
</div>

<div class="algo-card" data-status="inactive">

  {#if success}
    <div class="mb-3 p-2 rounded bg-green-500/15 text-green-400 text-xs border border-green-500/40">{success}</div>
  {/if}
  {#if error}
    <div class="mb-3 p-2 rounded bg-red-500/15 text-red-300 text-xs border border-red-500/40">{error}</div>
  {/if}

  <!-- Create User Form -->
  {#if showCreate}
    <div class="algo-status-card p-3 mb-3" data-status="running">
      <h3 class="section-heading">New User</h3>
      <div class="grid grid-cols-2 md:grid-cols-3 gap-3">
        <div><span class="field-label">Username</span><input bind:value={createForm.username} class="field-input" placeholder="login username" /></div>
        <div><span class="field-label">Password</span><input type="password" bind:value={createForm.password} class="field-input" placeholder="min 8 chars" /></div>
        <div><span class="field-label">Full Name</span><input bind:value={createForm.display_name} class="field-input" /></div>
        <div><span class="field-label">Email</span><input type="email" bind:value={createForm.email} class="field-input" /></div>
        <div><span class="field-label">Phone</span><input bind:value={createForm.phone} class="field-input" /></div>
        <!-- Role picker — operational admin can only create partners.
             Designated (firm owner) can pick any of the 5 canonical
             roles. Backend (admin.py::create_user) coerces non-
             designated attempts to 'partner' anyway, but the UI
             hides the elevated choices to match the mental model.
             Canonical roles: designated / trader / risk / admin / partner. -->
        <div><span class="field-label">Role</span>
          {#if $authStore.user?.role === 'designated'}
            <Select ariaLabel="Role" bind:value={createForm.role}
              options={[
                { value: 'designated', label: 'Designated — firm owner (full access)' },
                { value: 'trader',     label: 'Trader — PM who self-executes' },
                { value: 'risk',       label: 'Risk — read + kill-switch'     },
                { value: 'admin',      label: 'Admin — broker / user ops'     },
                { value: 'partner',    label: 'Partner — LP read-only'        },
              ]} />
          {:else}
            <div class="field-input field-input-readonly">Partner</div>
          {/if}
        </div>
        <!-- Capital + share % — designated-only. Backend silently
             coerces non-designated attempts to 0.0, but the inputs
             are hidden for admin so the form matches the policy. -->
        {#if $authStore.user?.role === 'designated'}
          <div><span class="field-label">Contribution (₹)</span><input type="number" bind:value={createForm.contribution} class="field-input" /></div>
          <div><span class="field-label">Profit Share (%)</span><input type="number" step="0.1" bind:value={createForm.share_pct} class="field-input" /></div>
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
    <LoadingSkeleton variant="block" rows={5} height="2.5rem" />
  {:else if !users.length}
    <p class="text-xs text-[var(--c-muted)]">No users registered.</p>
  {:else}
    <div class="space-y-3 content-fade-in">
      {#each users as user}
        {@const isSelf = user.username === $authStore.user?.username}
        {@const iAmDesignated = $authStore.user?.role === 'designated'}
        {@const iAmAdmin = $authStore.user?.role === 'admin'}
        {@const targetIsPartner = user.role === 'partner'}
        <div class="algo-status-card p-3" data-status={user.is_active ? (user.is_approved ? 'active' : 'running') : 'error'}>
          <!-- Header row -->
          <div class="flex items-center justify-between mb-2">
            <div class="flex items-center flex-wrap gap-1.5">
              <span class="font-semibold text-xs text-[var(--c-action)]">{user.display_name}</span>
              <span class="text-xs text-[#c8d8f0]/70">@{user.username}</span>
              {#if isSelf}
                <span class="px-1.5 py-0.5 rounded bg-sky-500/15 text-sky-300 text-[0.6rem] font-semibold uppercase border border-sky-500/40">You</span>
              {/if}
              <span class="text-[0.6rem] text-[var(--c-muted)] font-mono">{user.account_id}</span>
              <span class="px-1.5 py-0.5 rounded text-[0.6rem] font-semibold uppercase border
                {user.role === 'designated'
                  ? 'bg-violet-500/15 text-[#c084fc] border-violet-500/40'
                  : user.role === 'admin'
                    ? 'bg-amber-500/15 text-amber-400 border-amber-500/40'
                    : user.role === 'trader'
                      ? 'bg-green-500/15 text-[var(--c-long)] border-green-500/40'
                      : user.role === 'risk'
                        ? 'bg-amber-500/15 text-amber-400/75 border-amber-500/30'
                        : user.role === 'partner'
                          ? 'bg-green-500/10 text-[var(--c-long)]/60 border-green-500/25'
                          : 'bg-slate-500/15 text-slate-400 border-slate-500/40'}">
                {user.role}
              </span>
              {#if user.terminated_at}
                <span class="px-1.5 py-0.5 rounded bg-zinc-500/20 text-zinc-300 text-[0.6rem] font-semibold uppercase border border-zinc-500/50">Terminated</span>
              {:else if user.suspended_at}
                <span class="px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-400 text-[0.6rem] font-semibold uppercase border border-amber-500/40">Suspended</span>
              {:else if !user.is_approved}
                <span class="px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-400 text-[0.6rem] font-semibold uppercase border border-amber-500/40">Pending</span>
              {:else if !user.is_active}
                <span class="px-1.5 py-0.5 rounded bg-red-500/15 text-red-300 text-[0.6rem] font-semibold uppercase border border-red-500/40">Inactive</span>
              {/if}
              {#if user.email_verified}
                <span class="px-1.5 py-0.5 rounded bg-sky-500/15 text-sky-300 text-[0.6rem] font-semibold uppercase border border-sky-500/40" title="Email verified">✉ Verified</span>
              {/if}
              {#if user.kyc_verified}
                <span class="px-1.5 py-0.5 rounded bg-green-500/15 text-green-400 text-[0.6rem] font-semibold uppercase border border-green-500/40">KYC</span>
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
                  <button onclick={() => reinstate(user.username)} class="btn-secondary text-[0.65rem] py-1 px-2 border-amber-400/50 text-amber-400">Reinstate</button>
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
                <button onclick={() => flipDesignated(user)} class="btn-secondary text-[0.65rem] py-1 px-2 border-violet-400/50 text-[#c084fc]">
                  {user.role === 'designated' ? 'Demote' : 'Promote'}
                </button>
              {/if}
              <!-- Edit profile — designated on anyone, admin on self
                   or partner targets. Mirrors the backend gate
                   _check_action(admin_self_ok=True, admin_partner_ok=True). -->
              {#if editing !== user.username && !user.terminated_at && (iAmDesignated || isSelf || (iAmAdmin && targetIsPartner))}
                <button onclick={() => startEdit(user)} class="btn-secondary text-[0.65rem] py-1 px-2">Edit</button>
              {/if}
              <!-- Investor portal — token mint/revoke for LP read-
                   only NAV access. Designated only; only meaningful
                   when the user has a real contribution. We don't
                   gate on share_pct so the operator can also pre-
                   mint a link for an LP whose row is queued. -->
              {#if iAmDesignated && !user.terminated_at && !isSelf}
                <button
                  onclick={() => openPortal(user)}
                  class="btn-secondary text-[0.65rem] py-1 px-2 border-cyan-400/50 text-cyan-300"
                  title="Mint or revoke this LP's investor-portal URL"
                >Portal</button>
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
                  class="btn-secondary text-[0.65rem] py-1 px-2 border-amber-400/50 text-amber-400"
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
                <h3 class="section-heading">Personal</h3>
                <div class="grid grid-cols-2 md:grid-cols-3 gap-3">
                  <div><span class="field-label">Display Name</span><input bind:value={editForm.display_name} class="field-input" /></div>
                  <div><span class="field-label">Email</span><input type="email" bind:value={editForm.email} class="field-input" /></div>
                  <div><span class="field-label">Phone</span><input bind:value={editForm.phone} class="field-input" /></div>
                  <div><span class="field-label">PAN</span><input bind:value={editForm.pan} class="field-input" maxlength="10" style="text-transform:uppercase" /></div>
                  <div><span class="field-label">Date of Birth</span><input type="date" bind:value={editForm.date_of_birth} class="field-input" /></div>
                  <div class="flex items-end gap-2">
                    <span class="field-label">KYC Verified</span>
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
                    <span class="field-label" title="Designated only — manually flips email_verified without going through the email-token flow.">Email Verified</span>
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
                      <span class="field-label" title="Send platform alerts (loss, agent fires, summaries) to this user's email.">Receive Alerts</span>
                      <input type="checkbox"
                             bind:checked={editForm.receive_alerts}
                             disabled={user.role === 'designated'}
                             class="mt-1" />
                    </div>
                  {/if}
                  <!-- Compliance officer designation — orthogonal to
                       role. SEBI Cat-III requires a designated
                       compliance officer; the in-app flag tracks the
                       legal title without bloating the role enum.
                       Designated only. -->
                  <div class="flex items-end gap-2">
                    <span class="field-label" title="SEBI Cat-III compliance officer designation. Orthogonal to role — usually flipped on a risk or admin user.">Compliance Officer</span>
                    <input type="checkbox"
                           bind:checked={editForm.compliance_designated}
                           disabled={!iAmDesignated}
                           class="mt-1" />
                  </div>
                </div>
              </div>

              <!-- RBAC horizontal scope (slice 5). Designated-only;
                   admin/risk/ops/observer/demo ignore the assigned
                   list and see all accounts firm-wide. Trader uses
                   the list to limit which broker accounts they can
                   see + place orders against. Empty = no accounts
                   assigned (fail-safe for new trader users). -->
              {#if iAmDesignated}
                <div>
                  <h3 class="section-heading"
                      title="Horizontal scope — only applied to trader role; firm-wide roles see all accounts regardless.">
                    RBAC Scope
                    {#if user.role !== 'trader'}<span class="text-[0.55rem] opacity-60 font-normal">(advisory for non-trader)</span>{/if}
                  </h3>
                  <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
                    <div>
                      <span class="field-label" title="Broker-account codes (one per line) this user can trade through. Trader-only.">Assigned Accounts</span>
                      <textarea class="field-input font-mono text-[0.7rem]"
                                rows="3"
                                placeholder="ZG0790&#10;DH3747"
                                value={(editForm.assigned_accounts || []).join('\n')}
                                oninput={(e) => editForm.assigned_accounts = e.currentTarget.value
                                  .split(/\s+/).map(s => s.trim().toUpperCase()).filter(Boolean)}></textarea>
                    </div>
                    <div>
                      <span class="field-label" title="Strategy ids this user manages (slice 6 — strategies don't exist yet). Trader-only.">Assigned Strategies</span>
                      <textarea class="field-input font-mono text-[0.7rem]"
                                rows="3"
                                placeholder="(strategy IDs, comma or newline separated)"
                                value={(editForm.assigned_strategies || []).join(', ')}
                                oninput={(e) => editForm.assigned_strategies = e.currentTarget.value
                                  .split(/[\s,]+/).map(s => parseInt(s, 10)).filter(n => Number.isFinite(n))}></textarea>
                    </div>
                  </div>
                </div>
              {/if}
              <div>
                <h3 class="section-heading">Address</h3>
                <div class="grid grid-cols-2 md:grid-cols-3 gap-3">
                  <div class="col-span-2 md:col-span-3"><span class="field-label">Address Line 1</span><input bind:value={editForm.address_line1} class="field-input" /></div>
                  <div class="col-span-2 md:col-span-3"><span class="field-label">Address Line 2</span><input bind:value={editForm.address_line2} class="field-input" /></div>
                  <div><span class="field-label">City</span><input bind:value={editForm.city} class="field-input" /></div>
                  <div><span class="field-label">State</span><input bind:value={editForm.state} class="field-input" /></div>
                  <div><span class="field-label">Pincode</span><input bind:value={editForm.pincode} class="field-input" maxlength="6" /></div>
                </div>
              </div>
              <div>
                <h3 class="section-heading">Investment</h3>
                <div class="grid grid-cols-2 md:grid-cols-3 gap-3">
                  <div><span class="field-label">Role</span>
                    {#if iAmDesignated}
                      <Select ariaLabel="Role" bind:value={editForm.role}
                        options={[
                          { value: 'designated', label: 'Designated — firm owner (full access)' },
                          { value: 'trader',     label: 'Trader — PM who self-executes'        },
                          { value: 'risk',       label: 'Risk — read + kill-switch'            },
                          { value: 'admin',      label: 'Admin — broker / user ops'            },
                          { value: 'partner',    label: 'Partner — LP read-only'               },
                        ]} />
                    {:else}
                      <Select ariaLabel="Role" bind:value={editForm.role}
                        options={[
                          { value: 'partner', label: 'Partner — LP read-only'  },
                          { value: 'admin',   label: 'Admin — broker / user ops' },
                        ]} />
                    {/if}
                  </div>
                  <!-- Capital + share % + contribution date — designated-
                       only. Backend (admin.py::update_user) silently
                       drops these from non-designated PATCH bodies; the
                       UI hides the inputs to match the policy. Admin
                       still SEES the values in the user-card display
                       above (read-only). -->
                  {#if iAmDesignated}
                    <div><span class="field-label">Contribution (₹)</span><input type="number" bind:value={editForm.contribution} class="field-input" /></div>
                    <div><span class="field-label">Contribution Date</span><input type="date" bind:value={editForm.contribution_date} class="field-input" /></div>
                    <div><span class="field-label">Profit Share (%)</span><input type="number" step="0.1" bind:value={editForm.share_pct} class="field-input" /></div>
                  {/if}
                  <div><span class="field-label">Join Date</span><input type="date" bind:value={editForm.join_date} class="field-input" /></div>
                </div>
              </div>
              <div>
                <h3 class="section-heading">Bank Details</h3>
                <div class="grid grid-cols-2 md:grid-cols-3 gap-3">
                  <div><span class="field-label">Bank Name</span><input bind:value={editForm.bank_name} class="field-input" /></div>
                  <div><span class="field-label">Account Number</span><input bind:value={editForm.bank_account} class="field-input" /></div>
                  <div><span class="field-label">IFSC</span><input bind:value={editForm.bank_ifsc} class="field-input" /></div>
                </div>
              </div>
              <div>
                <h3 class="section-heading">Nominee</h3>
                <div class="grid grid-cols-2 md:grid-cols-3 gap-3">
                  <div><span class="field-label">Name</span><input bind:value={editForm.nominee_name} class="field-input" /></div>
                  <div><span class="field-label">Relation</span><input bind:value={editForm.nominee_relation} class="field-input" placeholder="Spouse, Child, etc." /></div>
                  <div><span class="field-label">Phone</span><input bind:value={editForm.nominee_phone} class="field-input" /></div>
                </div>
              </div>
              <div>
                <h3 class="section-heading">Notes</h3>
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
<section class="email-panel algo-card mt-3" data-status="inactive">
  <!-- Header -->
  <div class="flex items-center justify-between mb-1 gap-2 flex-wrap">
    <h2 class="algo-card-title mb-0">Email Partners</h2>
    <button
      onclick={() => { showEmailHistory = !showEmailHistory; if (showEmailHistory) loadEmailEvents(); }}
      class="text-[0.62rem] text-[#7dd3fc] hover:text-[#bae6fd] font-mono flex items-center gap-1 transition-colors">
      {showEmailHistory ? 'hide history' : 'show history'}
      <DisclosureChevron open={showEmailHistory} />
    </button>
  </div>
  <div class="border-b border-[rgba(251,191,36,0.25)] mb-3"></div>

  <!-- History panel -->
  {#if showEmailHistory}
    <div class="mb-4 rounded border border-[rgba(125,211,252,0.25)] bg-[rgba(14,22,44,0.5)] p-3">
      <div class="text-[0.6rem] text-[var(--c-muted)] uppercase font-bold mb-2 tracking-wider">Recent send history</div>
      {#if emailEventsLoading}
        <div class="text-xs text-[var(--c-muted)] animate-pulse">Loading…</div>
      {:else if emailEvents.length === 0}
        <div class="text-xs text-[var(--c-muted)]">No sends yet.</div>
      {:else}
        <div class="space-y-1.5">
          {#each emailEvents as ev}
            {@const hasFail = (ev.failed_count ?? 0) > 0}
            <div class="font-mono text-[0.6rem] flex flex-wrap gap-x-2 gap-y-0.5 leading-relaxed
              {hasFail ? 'text-red-300' : 'text-[#c8d8f0]/80'}">
              <span class="tabular-nums opacity-70">{_relTime(ev.created_at)}</span>
              <span>·</span>
              <span class="text-[var(--c-action)]/80">{ev.actor ?? '—'}</span>
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
      <span class="field-label">Preset recipients</span>
      <Select ariaLabel="Recipient preset" bind:value={emailPreset}
        options={PRESET_OPTIONS} />
      {#if emailPreset !== ''}
        <p class="text-[0.58rem] text-[var(--c-muted)] mt-0.5 tabular-nums">{emailPresetCount} email address{emailPresetCount === 1 ? '' : 'es'}</p>
      {/if}
    </div>
    <div>
      <span class="field-label" class:opacity-40={emailPreset !== ''}>Or pick specific</span>
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
    <span class="field-label">Subject</span>
    <input
      bind:value={emailSubject}
      maxlength="200"
      class="field-input w-full"
      placeholder="Email subject…" />
    <div class="text-[0.58rem] text-[var(--c-muted)] text-right tabular-nums mt-0.5">{emailSubject.length}/200</div>
  </div>

  <!-- Body -->
  <div class="mb-3">
    <span class="field-label">Body</span>
    <textarea
      bind:value={emailBody}
      rows="8"
      maxlength="50000"
      class="field-input w-full resize-y font-mono text-[0.65rem]"
      placeholder="Message body…"></textarea>
    <div class="text-[0.58rem] text-[var(--c-muted)] flex justify-between tabular-nums mt-0.5">
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
        <span class="inline-block h-3 w-3 rounded-full border-2 border-[var(--c-action)] border-t-transparent animate-spin"></span>
        Sending…
      {:else}
        Send to {
          emailPreset === 'all-partners'   ? `${users.filter(u=>(u.role==='partner'||u.role==='designated')&&(u.share_pct||0)>0&&u.is_active&&u.email).length} partner(s)` :
          emailPreset === 'all-designated' ? `${users.filter(u=>u.role==='designated'&&(u.share_pct||0)>0&&u.is_active&&u.email).length} designated` :
          emailPreset === 'all'            ? `${users.filter(u=>u.is_active&&u.email).length} user(s)` :
          emailPicked.length > 0           ? `${emailPicked.length} partner(s)` :
          'partners'
        }
      {/if}
    </button>
    {#if lastSentSummary && !emailResult}
      <span class="text-[0.6rem] text-[var(--c-muted)] tabular-nums font-mono">
        last sent: {_relTime(lastSentSummary.at.toISOString())} — sent {lastSentSummary.sent}/{lastSentSummary.total}
      </span>
    {/if}
  </div>

  <!-- Result strip -->
  {#if emailResult}
    <div class="mt-3 p-2 rounded text-xs border font-mono tabular-nums
      {emailResult.kind === 'ok'      ? 'bg-green-500/15 text-green-400 border-green-500/40' :
       emailResult.kind === 'partial' ? 'bg-amber-500/15 text-amber-400 border-amber-500/40' :
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

<!-- Investor portal modal — minted token URL + token list. Opens via
     the per-user Portal button. The modal is the entirety of the
     interaction surface; admin closes it when done. -->
<ModalShell
  open={!!portalUser}
  onClose={closePortal}
  ariaLabel="Investor Portal"
  zIndex={200}
>
  <div class="ip-modal" onclick={(e) => e.stopPropagation()}>
      <header class="ip-modal-head">
        <div>
          <div class="ip-modal-title">Investor portal</div>
          <div class="ip-modal-subtitle">{portalUser?.username}</div>
        </div>
        <button class="ip-modal-x" onclick={closePortal} aria-label="Close">×</button>
      </header>

      <div class="ip-modal-tabs">
        <AlgoTabs
          tabs={[
            { id: 'tokens',    label: 'URL access'         },
            { id: 'statement', label: 'Statement preview'  },
            { id: 'events',    label: 'Events'             },
          ]}
          value={portalTab}
          onChange={(id) => { portalTab = /** @type {'tokens'|'statement'|'events'} */ (id); }}
          compact={true}
        />
      </div>

      {#if portalError}
        <div class="ip-modal-error">{portalError}</div>
      {/if}

      {#if portalTab === 'tokens'}
      {#if portalFresh}
        <section class="ip-modal-fresh">
          <div class="ip-modal-fresh-lbl">New URL minted — copy now</div>
          <div class="ip-modal-fresh-url-row">
            <input type="text" class="ip-modal-fresh-url"
                   readonly value={portalFresh?.url}
                   onclick={(e) => e.currentTarget.select()} />
            <button class="btn-primary text-[0.65rem] py-1 px-2"
                    onclick={copyPortalUrl}>Copy</button>
          </div>
          <div class="ip-modal-fresh-hint">
            Expires {portalFresh?.expires_at?.slice(0, 10)}. This URL is
            the credential — forward via your trusted channel
            (WhatsApp / email). Shown only once.
          </div>
        </section>
      {/if}

      <section class="ip-modal-mint">
        <div class="ip-modal-section-head">Mint a new token</div>
        <div class="ip-modal-mint-row">
          <label class="ip-modal-field">
            <span class="ip-modal-field-lbl">Expires in</span>
            <input type="number" min="1" max="3650"
                   bind:value={portalMintDays}
                   class="field-input"/>
            <span class="ip-modal-field-suffix">days</span>
          </label>
          <label class="ip-modal-field grow">
            <span class="ip-modal-field-lbl">Note (optional)</span>
            <input type="text" maxlength="120"
                   bind:value={portalMintNote}
                   placeholder="e.g. WhatsApp to LP"
                   class="field-input"/>
          </label>
          <button class="btn-primary text-[0.7rem] py-1.5 px-3"
                  disabled={portalMintBusy}
                  onclick={mintPortal}>
            {portalMintBusy ? 'Minting…' : 'Mint'}
          </button>
        </div>
      </section>

      <section class="ip-modal-list">
        <div class="ip-modal-section-head">
          Existing tokens
          {#if portalLoading}<span class="text-[0.6rem] text-[var(--c-muted)]"> · loading…</span>{/if}
        </div>
        {#if !portalLoading && portalTokens.length === 0}
          <div class="ip-modal-empty">No tokens minted yet.</div>
        {:else}
          <div class="ip-modal-tbl-wrap">
            <table class="algo-table ip-modal-tbl">
              <thead>
                <tr>
                  <th>Token</th>
                  <th>Status</th>
                  <th>Expires</th>
                  <th>Last visit</th>
                  <th class="th-num">Visits</th>
                  <th>Note</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {#each portalTokens as t (t.id)}
                  <tr class:revoked={!t.is_active}>
                    <td class="td-mono">{t.token_preview}</td>
                    <td>
                      {#if t.revoked_at}
                        <span class="ip-pill ip-pill-revoked">Revoked</span>
                      {:else if !t.is_active}
                        <span class="ip-pill ip-pill-expired">Expired</span>
                      {:else}
                        <span class="ip-pill ip-pill-active">Active</span>
                      {/if}
                    </td>
                    <td class="td-mono">{t.expires_at?.slice(0, 10) ?? '—'}</td>
                    <td class="td-mono">{t.last_visit_at?.slice(0, 10) ?? '—'}</td>
                    <td class="td-num">{t.visit_count}</td>
                    <td class="ip-modal-note" title={t.note || ''}>{t.note || '—'}</td>
                    <td class="td-actions">
                      {#if t.is_active}
                        <button class="btn-secondary text-[0.6rem] py-0.5 px-1.5 border-red-400/50 text-red-300"
                                onclick={() => revokePortal(t.id)}>Revoke</button>
                      {/if}
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
      </section>
      {/if}

      {#if portalTab === 'statement'}
      <section class="ip-modal-mint">
        <div class="ip-modal-section-head">Preview monthly statement</div>
        <div class="ip-modal-mint-row">
          <label class="ip-modal-field">
            <span class="ip-modal-field-lbl">Year</span>
            <input type="number" min="2020" max="2100" step="1"
                   bind:value={stmtYear} class="field-input ip-modal-yr"/>
          </label>
          <label class="ip-modal-field">
            <span class="ip-modal-field-lbl">Month</span>
            <input type="number" min="1" max="12" step="1"
                   bind:value={stmtMonth} class="field-input ip-modal-yr"/>
          </label>
          <button class="btn-secondary text-[0.7rem] py-1.5 px-3 border-cyan-400/50 text-cyan-300"
                  disabled={stmtBusy}
                  onclick={previewStatement}>
            {stmtBusy ? 'Generating…' : 'Preview PDF'}
          </button>
        </div>
        <div class="ip-modal-fresh-hint">
          Opens in a new tab. Latest NAV snapshot determines the
          closing slice; log any pending subscription / redemption
          events in the Events tab before previewing if the LP
          joined mid-period.
        </div>
      </section>
      {/if}

      {#if portalTab === 'events'}
      <section class="ip-modal-mint">
        <div class="ip-modal-section-head">Add event</div>
        <div class="ip-modal-mint-row">
          <label class="ip-modal-field">
            <span class="ip-modal-field-lbl">Type</span>
            <select class="field-input" bind:value={evForm.event_type}>
              <option value="subscription">Subscription</option>
              <option value="redemption">Redemption</option>
              <option value="bootstrap">Bootstrap</option>
            </select>
          </label>
          <label class="ip-modal-field">
            <span class="ip-modal-field-lbl">Date</span>
            <input type="date" bind:value={evForm.event_date} class="field-input"/>
          </label>
          <label class="ip-modal-field">
            <span class="ip-modal-field-lbl">Amount (₹)</span>
            <input type="number" min="0" step="any"
                   bind:value={evForm.amount} class="field-input ip-modal-yr"/>
          </label>
          <label class="ip-modal-field">
            <span class="ip-modal-field-lbl">NAV / unit</span>
            <input type="number" min="0" step="any"
                   bind:value={evForm.nav_per_unit} class="field-input ip-modal-yr"/>
          </label>
          <label class="ip-modal-field grow">
            <span class="ip-modal-field-lbl">Note (optional)</span>
            <input type="text" maxlength="120"
                   bind:value={evForm.note}
                   placeholder="e.g. Wire ref 12345"
                   class="field-input"/>
          </label>
          <button class="btn-primary text-[0.7rem] py-1.5 px-3"
                  disabled={evBusy}
                  onclick={addEvent}>
            {evBusy ? 'Adding…' : 'Add'}
          </button>
        </div>
        <div class="ip-modal-fresh-hint">
          NAV/unit is the per-unit value at the event date.
          <strong>Subscription:</strong> capital in, units increase.
          <strong>Redemption:</strong> capital out, units decrease.
          <strong>Bootstrap:</strong> a one-time seed event to
          migrate the LP into the units model — set NAV/unit to
          1.0 if you don't have a per-unit history yet. Events are
          a passive journal today; the next slice flips NAV math
          to consume them.
        </div>
      </section>

      <section class="ip-modal-list">
        <div class="ip-modal-section-head">
          Event log
          {#if evLoading}<span class="text-[0.6rem] text-[var(--c-muted)]"> · loading…</span>{/if}
        </div>
        <div class="ip-modal-mint-row" style="margin-bottom:0.5rem;">
          <div class="ip-modal-field">
            <span class="ip-modal-field-lbl">Units balance</span>
            <span class="text-[0.78rem] font-mono">{evTotals.total_units.toFixed(4)}</span>
          </div>
          <div class="ip-modal-field">
            <span class="ip-modal-field-lbl">Capital in</span>
            <span class="text-[0.78rem] font-mono">₹{Math.round(evTotals.total_in).toLocaleString('en-IN')}</span>
          </div>
          <div class="ip-modal-field">
            <span class="ip-modal-field-lbl">Capital out</span>
            <span class="text-[0.78rem] font-mono">₹{Math.round(evTotals.total_out).toLocaleString('en-IN')}</span>
          </div>
        </div>
        {#if !evLoading && evRows.length === 0}
          <div class="ip-modal-empty">No events logged yet.</div>
        {:else}
          <div class="ip-modal-tbl-wrap">
            <table class="algo-table ip-modal-tbl">
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Type</th>
                  <th class="th-num">Amount</th>
                  <th class="th-num">NAV/unit</th>
                  <th class="th-num">Units Δ</th>
                  <th>Note</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {#each evRows as e (e.id)}
                  <tr>
                    <td class="td-mono">{e.event_date}</td>
                    <td>
                      {#if e.event_type === 'subscription'}
                        <span class="ip-pill ip-pill-active">Subscribe</span>
                      {:else if e.event_type === 'redemption'}
                        <span class="ip-pill ip-pill-revoked">Redeem</span>
                      {:else}
                        <span class="ip-pill ip-pill-expired">Bootstrap</span>
                      {/if}
                    </td>
                    <td class="td-num td-mono">₹{Math.round(e.amount).toLocaleString('en-IN')}</td>
                    <td class="td-num td-mono">{e.nav_per_unit.toFixed(4)}</td>
                    <td class="td-num td-mono">{e.units_delta.toFixed(4)}</td>
                    <td class="ip-modal-note" title={e.note || ''}>{e.note || '—'}</td>
                    <td class="td-actions">
                      <button class="btn-secondary text-[0.6rem] py-0.5 px-1.5 border-red-400/50 text-red-300"
                              onclick={() => removeEvent(e.id)}>Delete</button>
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
      </section>
      {/if}
    </div>
</ModalShell>

<style>
  .ip-modal {
    background: #0f172a;
    border: 1px solid rgba(126, 151, 184, 0.30);
    border-radius: 8px;
    width: 100%;
    max-width: 720px;
    max-height: 90vh;
    overflow-y: auto;
    padding: 1rem 1.2rem 1.3rem;
    color: #c8d8f0;
    box-shadow: 0 18px 36px rgba(0,0,0,0.45);
  }
  .ip-modal-head {
    display: flex; align-items: flex-start; justify-content: space-between;
    margin-bottom: 0.8rem;
    border-bottom: 1px solid rgba(126, 151, 184, 0.18);
    padding-bottom: 0.6rem;
  }
  .ip-modal-title {
    font-size: var(--fs-lg); font-weight: 800; letter-spacing: 0.06em;
    text-transform: uppercase; color: #67e8f9;
    font-family: var(--font-numeric);
  }
  .ip-modal-subtitle {
    font-size: var(--fs-md); color: var(--c-muted); margin-top: 0.15rem;
    font-family: var(--font-numeric);
  }
  /* Thin wrapper for the AlgoTabs strip inside the portal modal.
     Hand-rolled `.ip-modal-tab` / `.ip-modal-tab.active` retired —
     AlgoTabs supplies canonical underline, font, and active-state
     decoration matching every other tab strip in the workspace. */
  .ip-modal-tabs {
    margin-bottom: 0.8rem;
    border-bottom: 1px solid rgba(126, 151, 184, 0.18);
  }

  .ip-modal-x {
    background: transparent; border: 1px solid rgba(126, 151, 184, 0.30);
    color: #c8d8f0; border-radius: 4px;
    width: 1.6rem; height: 1.6rem;
    font-size: 1.1rem; line-height: 1; cursor: pointer;
  }
  .ip-modal-x:hover { background: rgba(248, 113, 113, 0.12); color: #fca5a5; }

  .ip-modal-error {
    padding: 0.5rem 0.7rem;
    background: rgba(248,113,113,0.12); border: 1px solid rgba(248,113,113,0.4);
    color: #fca5a5; border-radius: 4px;
    font-size: var(--fs-lg); margin-bottom: 0.7rem;
  }
  .ip-modal-fresh {
    background: var(--c-long-10);
    border: 1px solid rgba(74, 222, 128, 0.35);
    border-radius: 6px;
    padding: 0.7rem 0.9rem;
    margin-bottom: 0.9rem;
  }
  .ip-modal-fresh-lbl {
    font-size: var(--fs-xs); font-weight: 800; letter-spacing: 0.06em;
    text-transform: uppercase; color: var(--c-long);
    margin-bottom: 0.35rem;
  }
  .ip-modal-fresh-url-row {
    display: flex; gap: 0.4rem; align-items: center;
  }
  .ip-modal-fresh-url {
    flex: 1; min-width: 0;
    padding: 0.3rem 0.5rem;
    background: rgba(15, 23, 42, 0.65);
    border: 1px solid rgba(126, 151, 184, 0.30);
    border-radius: 4px;
    color: #c8d8f0;
    font-family: var(--font-numeric);
    font-size: var(--fs-lg);
  }
  .ip-modal-fresh-hint {
    margin-top: 0.4rem;
    font-size: var(--fs-sm); color: var(--c-muted); line-height: 1.5;
  }

  .ip-modal-section-head {
    font-size: var(--fs-xs); font-weight: 800; letter-spacing: 0.06em;
    text-transform: uppercase; color: var(--text-muted);
    margin-bottom: 0.4rem;
    font-family: var(--font-numeric);
  }
  .ip-modal-mint { margin-bottom: 1rem; }
  .ip-modal-mint-row {
    display: flex; gap: 0.5rem; align-items: flex-end; flex-wrap: wrap;
  }
  .ip-modal-field { display: flex; flex-direction: column; gap: 0.2rem; }
  .ip-modal-field.grow { flex: 1 1 12rem; }
  .ip-modal-yr { width: 6rem; }
  .ip-modal-field-lbl {
    font-size: var(--fs-xs); color: var(--c-muted); letter-spacing: 0.04em;
    text-transform: uppercase; font-weight: 700;
  }
  .ip-modal-field-suffix {
    font-size: var(--fs-sm); color: var(--c-muted);
  }

  .ip-modal-empty {
    padding: 1rem; text-align: center; color: var(--c-muted);
    font-size: var(--fs-lg); font-style: italic;
  }
  .ip-modal-tbl-wrap {
    overflow-x: auto;
    border: 1px solid rgba(126, 151, 184, 0.18);
    border-radius: 4px;
  }
  .ip-modal-tbl { width: 100%; }
  .ip-modal-tbl th {
    text-align: left; padding: 0.3rem 0.5rem;
    background: rgba(15, 23, 42, 0.65);
    color: var(--text-muted); letter-spacing: 0.06em;
    text-transform: uppercase; font-weight: 800;
    border-bottom: 1px solid rgba(126, 151, 184, 0.30);
  }
  .ip-modal-tbl th.th-num { text-align: right; }
  .ip-modal-tbl td {
    padding: 0.3rem 0.5rem;
  }
  .ip-modal-tbl td.td-mono { font-family: var(--font-numeric); font-size: var(--fs-md); }
  .ip-modal-tbl td.td-num  { text-align: right; font-variant-numeric: tabular-nums; }
  .ip-modal-tbl td.td-actions { text-align: right; }
  .ip-modal-tbl tr.revoked td { color: var(--c-muted); }
  .ip-modal-note { max-width: 12rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  .ip-pill {
    display: inline-block; padding: 0.05rem 0.4rem;
    font-size: var(--fs-2xs); font-weight: 800; letter-spacing: 0.06em;
    text-transform: uppercase; border-radius: 2px;
  }
  .ip-pill-active  { background: rgba(74, 222, 128, 0.15); color: var(--c-long); border: 1px solid rgba(74,222,128,0.4); }
  .ip-pill-revoked { background: rgba(248, 113, 113, 0.12); color: #fca5a5; border: 1px solid rgba(248,113,113,0.35); }
  .ip-pill-expired { background: rgba(126, 151, 184, 0.12); color: #c8d8f0; border: 1px solid rgba(126,151,184,0.3); }

  .section-heading { font-size: var(--fs-sm, 0.6rem); font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; color: var(--c-action, #fbbf24); padding-bottom: 0.3rem; margin-bottom: 0.5rem; border-bottom: 1px solid rgba(255,255,255,0.10); }
</style>
