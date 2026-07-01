<script>
  // Broker accounts admin page (/admin/brokers).
  //
  // CRUD over the `broker_accounts` DB table. Operators add/edit/delete
  // Kite accounts here without ever opening secrets.yaml. Every
  // mutation triggers a Connections.rebuild_from_db() on the server so
  // subsequent broker calls (holdings/positions/quotes/orders) pick up
  // the new state without a service restart.
  //
  // SECURITY MODEL
  //   - api_key shows in plaintext (it's not credential-grade alone).
  //   - api_secret / password / TOTP seed are write-only here:
  //       - on Create, operator types them once, server encrypts and
  //         stores;
  //       - on Update, blank fields mean "leave unchanged" — operator
  //         only re-types when they want to rotate a specific cred.
  //   - The page never reads decrypted secrets back.
  //   - "Test" button hits broker.profile() to confirm the credential
  //     pipeline works end-to-end.

  import { onDestroy } from 'svelte';
  import { nowStamp, visibleInterval } from '$lib/stores';
  import { userRole, userCaps, userCapsReady, hasCap } from '$lib/rbac';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import {
    fetchBrokerAccounts, createBrokerAccount, updateBrokerAccount,
    deleteBrokerAccount, testBrokerAccount,
  } from '$lib/api';
  import StaleBanner    from '$lib/StaleBanner.svelte';
  import Select         from '$lib/Select.svelte';
  import ConfirmModal   from '$lib/ConfirmModal.svelte';
  import LoadingSkeleton from '$lib/LoadingSkeleton.svelte';
  import EmptyState from '$lib/EmptyState.svelte';
  import { toast } from '$lib/data/toastStore.svelte.js';

  /** @type {Array<{id:number,account:string,broker_id:string,api_key:string,
   *   source_ip:string|null,is_active:boolean,historical_data_enabled:boolean,
   *   notes:string|null,created_at:string,updated_at:string,loaded:boolean}>} */
  let accounts = $state([]);
  let loading  = $state(true);
  let error    = $state('');

  /** @type {{ ask: (opts: any) => Promise<boolean> } | null} */
  let _confirmRef = $state(null);

  // Supported broker vendors. Adding a new entry here makes it
  // selectable in the form; the adapter must be registered in
  // `backend/shared/brokers/registry.py::_ADAPTERS` and have a
  // matching credential-shape entry in CREDENTIAL_SCHEMA below.
  const BROKER_OPTIONS = [
    { value: 'zerodha_kite', label: 'Zerodha Kite' },
    { value: 'groww',        label: 'Groww' },
    { value: 'dhan',         label: 'Dhan' },
    // { value: 'upstox',      label: 'Upstox' },
    // { value: 'angel_one',   label: 'Angel One' },
    // { value: 'fyers',       label: 'Fyers' },
  ];

  /** Normalise legacy "kite" to the canonical "zerodha_kite" for display. */
  function normaliseBrokerId(/** @type {string|null|undefined} */ id) {
    return (id === 'kite' || !id) ? 'zerodha_kite' : id;
  }

  // Per-broker credential schema. The form renders one input per entry
  // for the currently-selected broker_id. `secret: true` means the
  // value is encrypted at rest on the backend and never read back (so
  // the input stays blank on Edit; blank = "leave unchanged").
  const CREDENTIAL_SCHEMA = {
    zerodha_kite: [
      { key: 'api_key',    label: 'API key',     secret: false, required: true },
      { key: 'api_secret', label: 'API secret',  secret: true,  required: true },
      { key: 'password',   label: 'Password',    secret: true,  required: true },
      { key: 'totp_token', label: 'TOTP seed',   secret: true,  required: true },
    ],
    groww: [
      // Groww's only unattended auth path is the TOTP flow:
      //   GrowwAPI.get_access_token(api_key, totp=<code from totp_seed>)
      // The 'api_key + api_secret' approval flow was retired (required
      // manual daily approval on Groww's web UI) and the 24h access_token
      // fallback was a stopgap when TOTP failed. Both are gone — operator
      // confirmed: "you are supposed to use TOTP flow."
      //
      // Field mapping for Groww:
      //   - "TOTP token" (the long JWT from Groww's developer dashboard)
      //     maps to BrokerAccount.api_key on the backend.
      //   - "TOTP key" (the base32 seed paired with that JWT in the same
      //     dashboard panel) maps to BrokerAccount.totp_token_enc.
      // Operator names them "totp token" / "totp key" to match Groww's
      // dev portal labelling, NOT our internal model field names.
      { key: 'api_key',    label: 'TOTP token (JWT)', secret: true, required: true },
      { key: 'totp_token', label: 'TOTP key (base32 seed)', secret: true, required: true },
    ],
    dhan: [
      { key: 'client_id',  label: 'Client ID',  secret: false, required: true },
      { key: 'api_key',    label: 'API key',    secret: false, required: true },
      { key: 'api_secret', label: 'API secret', secret: true,  required: true },
      { key: 'password',   label: 'Trading PIN', secret: true, required: true },
      { key: 'totp_token', label: 'TOTP seed',  secret: true,  required: true },
    ],
  };

  /** Fields applicable to the currently-selected broker. */
  function credentialFields(/** @type {string} */ brokerId) {
    return CREDENTIAL_SCHEMA[brokerId] || CREDENTIAL_SCHEMA.zerodha_kite;
  }

  // Historical-data toggle — separate from the main `form` object so it
  // doesn't participate in the broker_id-keyed credential reset logic.
  let _formHistEnabled = $state(true);

  // Form state — reused for Create + Edit. Three modes:
  //   editing = ''     · idle (form hidden when accounts exist)
  //   editing = '__new__' · create mode (form visible, account input editable)
  //   editing = 'ZG0790'  · edit mode (form visible, account input disabled)
  // The sentinel keeps the existing `if (editing)` save-path branch
  // (truthy = mutate existing row) simple — see save().
  const NEW_SENTINEL = '__new__';
  let editing = $state(/** @type {string} */ (''));
  // Derived: in Edit mode (mutating an existing row) — used by every
  // template check that previously treated `editing` as a binary
  // (truthy = edit). Without this distinction, NEW_SENTINEL leaks
  // "edit" semantics into the create form (Save button reads "Save
  // changes" instead of "Create", secret-field placeholders say
  // "(leave blank to keep)", etc.), making the new-account form
  // visually indistinguishable from an edit of the wrong row.
  const isEditing = $derived(editing !== '' && editing !== NEW_SENTINEL);
  let form    = $state({
    account: '', broker_id: 'zerodha_kite',
    // Plaintext fields (returned by the API in clear)
    api_key: '', client_id: '',
    // Secret fields — sent only when non-empty on Edit ("leave unchanged"
    // semantics). On Create they're required (the backend validates).
    api_secret: '', password: '', totp_function: '', totp_token: '',
    access_token: '',
    // Operational fields
    source_ip: '', is_active: true, notes: '',
    priority: 100,
    // JSON text bound to a <textarea>; parsed at save time. Bound here as
    // a string so the operator's in-progress typing isn't constantly
    // re-validated. Empty = `{}` server-side.
    extra_config_text: '{}',
  });

  /** @type {Record<string, {ok:boolean, detail:string} | undefined>} */
  let testResults = $state({});
  let testInFlight = $state(/** @type {string} */ (''));
  let refreshTeardown;

  function resetForm(/** @type {string} */ acct = '') {
    editing = acct;
    // When entering create mode (acct === NEW_SENTINEL), the form's
    // `account` input must start blank so the operator can type the
    // code; for Edit, callers pass the row's actual account code.
    const accountInput = (acct === NEW_SENTINEL) ? '' : acct;
    form = {
      account: accountInput, broker_id: 'zerodha_kite',
      api_key: '', client_id: '',
      api_secret: '', password: '', totp_function: '', totp_token: '',
      access_token: '',
      source_ip: '', is_active: true, notes: '',
      priority: 100,
      extra_config_text: '{}',
    };
    _formHistEnabled = true;
    error = '';
  }

  function startEdit(/** @type {any} */ row) {
    editing = row.account;
    form = {
      account:    row.account,
      broker_id:  normaliseBrokerId(row.broker_id),
      api_key:    row.api_key || '',
      client_id:  row.client_id || '',
      // Secret fields stay blank — backend treats blank as
      // "leave unchanged" so a partial form doesn't clear them.
      api_secret: '', password: '', totp_function: '', totp_token: '',
      access_token: '',
      source_ip:  row.source_ip || '',
      is_active:  !!row.is_active,
      notes:      row.notes || '',
      priority:   typeof row.priority === 'number' ? row.priority : 100,
      extra_config_text: JSON.stringify(row.extra_config || {}, null, 2),
    };
    // Default true for rows pre-dating the column (undefined → ON).
    _formHistEnabled = row.historical_data_enabled !== false;
    error = '';
  }

  async function load() {
    try {
      accounts = await fetchBrokerAccounts() || [];
      error = '';
    } catch (e) { error = e.message; }
    finally { loading = false; }
  }

  async function save() {
    error = '';
    try {
      // Parse + validate Advanced JSON. Bad JSON aborts the save so the
      // operator never accidentally persists a malformed config.
      let parsedExtra;
      try {
        parsedExtra = JSON.parse(form.extra_config_text || '{}');
        if (!parsedExtra || typeof parsedExtra !== 'object' || Array.isArray(parsedExtra)) {
          error = 'Advanced settings must be a JSON object.';
          return;
        }
      } catch (parseErr) {
        error = `Advanced settings JSON invalid: ${parseErr.message}`;
        return;
      }
      const fieldsForThisBroker = credentialFields(form.broker_id);

      if (isEditing) {
        // PATCH — only send fields with values; empty secret fields are
        // explicitly omitted so the backend's "leave unchanged" logic
        // gets the right signal.
        const payload = {
          broker_id: form.broker_id,
          api_key: form.api_key,
          client_id: form.client_id || '',
          source_ip: form.source_ip,
          is_active: form.is_active,
          historical_data_enabled: _formHistEnabled,
          notes: form.notes,
          priority: Number(form.priority) || 100,
          extra_config: parsedExtra,
        };
        // Only send each secret if the operator typed a new value AND
        // that field is actually relevant to the selected broker.
        for (const f of fieldsForThisBroker) {
          if (f.secret && form[f.key]) payload[f.key] = form[f.key];
        }
        await updateBrokerAccount(editing, payload);
        toast.success(`Updated ${editing}`);
      } else {
        if (!form.account) { error = 'Account code is required.'; return; }
        // Broker-aware required-field check. Each broker schema declares
        // which fields are mandatory at create time (Kite needs 4 fields,
        // Dhan only 2). Missing any one of them aborts the save with a
        // specific error so the operator knows what to fill in.
        const missing = fieldsForThisBroker
          .filter(f => f.required && !form[f.key])
          .map(f => f.label);
        if (missing.length) {
          error = `Required for ${form.broker_id}: ${missing.join(', ')}.`;
          return;
        }
        const payload = {
          account:     form.account,
          broker_id:   form.broker_id,
          api_key:     form.api_key || '',
          api_secret:  form.api_secret || '',
          password:    form.password || '',
          totp_token:  form.totp_token || '',
          client_id:   form.client_id || '',
          access_token: form.access_token || '',
          source_ip:   form.source_ip,
          is_active:   form.is_active,
          historical_data_enabled: _formHistEnabled,
          notes:       form.notes,
          priority:    Number(form.priority) || 100,
          extra_config: parsedExtra,
        };
        await createBrokerAccount(payload);
        toast.success(`Created ${form.account}`);
      }
      resetForm();
      await load();
    } catch (e) {
      error = `Save failed: ${e.message}`;
    }
  }

  async function destroy(/** @type {any} */ row) {
    const ok = await _confirmRef?.ask({
      title: 'Delete broker account?',
      message: `Delete <b>${row.account}</b>? This is irreversible.`,
      danger: true,
      confirmLabel: 'Delete',
    });
    if (!ok) return;
    try {
      await deleteBrokerAccount(row.account);
      toast.success(`Deleted ${row.account}`);
      delete testResults[row.account];
      testResults = { ...testResults };
      if (editing === row.account) resetForm();
      await load();
    } catch (e) { error = `Delete failed: ${e.message}`; }
  }

  async function runTest(/** @type {any} */ row) {
    testInFlight = row.account;
    try {
      const r = await testBrokerAccount(row.account);
      testResults[row.account] = { ok: r.ok, detail: r.detail };
      testResults = { ...testResults };
      if (r.ok) {
        toast.success(`${row.account} — broker test passed`);
      } else {
        toast.error(`${row.account} — test failed: ${r.detail || 'unknown'}`);
      }
    } catch (e) {
      testResults[row.account] = { ok: false, detail: e.message };
      testResults = { ...testResults };
      toast.error(`${row.account} — test failed: ${e.message}`);
    } finally {
      testInFlight = '';
    }
  }

  // Canonical $effect-gated auth. manage_brokers admits designated + admin.
  // Bridge legacy stores into Svelte-5 $state so $derived doesn't
  // stale-cache the initial [] / 'partner' boot values (feedback note:
  // "$derived reading $store.x can stale-cache; bridge via $effect + $state").
  let _caps = $state(/** @type {string[]} */ ([]));
  let _role = $state(/** @type {string} */ ('partner'));
  $effect(() => { _caps = $userCaps; });
  $effect(() => { _role = $userRole; });
  const _canView = $derived(hasCap('manage_brokers', _caps, _role));
  let _loadedOnce = false;
  $effect(() => {
    if (_canView && !_loadedOnce) {
      _loadedOnce = true;
      load();
      // Throttle to 60 s on hidden — broker LOADED/PENDING status is
      // critical for operator awareness; keep a slow heartbeat alive.
      refreshTeardown = visibleInterval(load, 15000, 'throttle:60000');
    }
  });
  onDestroy(() => { refreshTeardown?.(); });
</script>

<ConfirmModal bind:this={_confirmRef} />

<svelte:head><title>Brokers | RamboQuant Analytics</title></svelte:head>

<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">Brokers</h1>
  </span>
  <span class="algo-ts">{$nowStamp}</span>
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <RefreshButton onClick={load} loading={loading} label="brokers" />
    <PageHeaderActions />
  </span>
</div>

{#if !$userCapsReady}
  <!-- RBAC bootstrap still in-flight — show a skeleton so a legitimate
       operator never sees the access-denied panel as a false-positive. -->
  <LoadingSkeleton variant="card" rows={3} />
{:else if !_canView}
  <EmptyState title="Access denied" icon="lock">
    {#snippet hintBody()}
      Broker administration requires the <code>manage_brokers</code> capability
      (designated or admin role). Your current role is
      <strong>{$userRole}</strong> — contact an admin to request access.
    {/snippet}
  </EmptyState>
{:else}

<StaleBanner {error} hasData={accounts.length > 0} label="Broker accounts" />

<!-- Account list -->
<div class="algo-card mb-3" data-status="inactive">
  <div class="brokers-list-header">
    <h2 class="brokers-h">
      Accounts <span class="opacity-60 font-normal ml-1">({accounts.length})</span>
    </h2>
    <button type="button" class="btn-primary text-[0.6rem] py-1 px-3"
            onclick={() => resetForm(NEW_SENTINEL)}
            disabled={editing !== ''}>+ New account</button>
  </div>
  {#if loading}
    <LoadingSkeleton variant="grid-row" rows={3} height="1.8rem" />
  {:else if !accounts.length}
    <EmptyState
      title="No broker accounts"
      hint="Add one with + New account, or seed secrets.yaml::kite_accounts on the server."
      icon="inbox"
      action={{ label: '+ New account', onClick: () => resetForm(NEW_SENTINEL) }}
    />
  {:else}
    <div class="brokers-scroll content-fade-in">
    <table class="brokers-table">
      <thead>
        <tr>
          <th>Account</th>
          <th>Broker</th>
          <th>API key</th>
          <th>Source IP</th>
          <th>Status</th>
          <th>Historical</th>
          <th>Notes</th>
          <th>Test</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {#each accounts as row}
          <tr class:row-inactive={!row.is_active}>
            <td class="font-mono">{row.account}</td>
            <td>{BROKER_OPTIONS.find(o => o.value === normaliseBrokerId(row.broker_id))?.label ?? row.broker_id}</td>
            <td class="font-mono mono-trunc" title={row.api_key}>{row.api_key || '—'}</td>
            <td class="font-mono mono-trunc" title={row.source_ip}>{row.source_ip || '—'}</td>
            <td>
              {#if !row.is_active}
                <span class="status-pill status-inactive" title="is_active = false">OFF</span>
              {:else if row.loaded}
                <span class="status-pill status-loaded" title="account is in the live Connections map">ON</span>
              {:else}
                <span class="status-pill status-pending" title="row exists but Connections hasn't picked it up yet — will refresh on the next 15 s poll">…</span>
              {/if}
            </td>
            <td class="brokers-hist-cell">
              <span class="brokers-hist-pill"
                    class:hist-on={row.historical_data_enabled !== false}
                    class:hist-off={row.historical_data_enabled === false}>
                {row.historical_data_enabled === false ? 'OFF' : 'ON'}
              </span>
            </td>
            <td class="notes" title={row.notes}>{row.notes || ''}</td>
            <td class="test-cell">
              <button type="button"
                      class="btn-secondary text-[0.55rem] py-0.5 px-2"
                      disabled={testInFlight === row.account || !row.is_active}
                      onclick={() => runTest(row)}>
                {testInFlight === row.account ? '…' : 'Test'}
              </button>
              {#if testResults[row.account]}
                <span class="test-result {testResults[row.account].ok ? 'ok' : 'fail'}"
                      title={testResults[row.account].detail}>
                  {testResults[row.account].ok ? '✓' : '✗'}
                </span>
              {/if}
            </td>
            <td class="action-cell">
              <button type="button" class="btn-secondary text-[0.55rem] py-0.5 px-2"
                      onclick={() => startEdit(row)}>Edit</button>
              <button type="button" class="btn-secondary text-[0.55rem] py-0.5 px-2 destructive"
                      onclick={() => destroy(row)}>Del</button>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
    </div>
  {/if}
</div>

<!-- Create / Edit form -->
{#if editing !== '' || !accounts.length}
  <div class="algo-status-card cmd-surface p-3 mb-3" data-status="inactive">
    <h2 class="brokers-h" style="border-bottom:1px solid rgba(251,191,36,0.18); padding-bottom:0.3rem; margin-bottom:0.5rem;">
      {isEditing ? `Edit ${editing}` : 'New account'}
    </h2>
    <div class="brokers-form">
      <div class="bf-field">
        <label class="field-label" for="bf-acct">Account code</label>
        <input id="bf-acct" type="text" class="field-input font-mono"
               placeholder="ZG0790"
               disabled={isEditing}
               bind:value={form.account} />
      </div>
      <div class="bf-field">
        <label class="field-label" for="bf-broker">Broker</label>
        <Select id="bf-broker" ariaLabel="Broker"
                bind:value={form.broker_id}
                options={BROKER_OPTIONS} />
      </div>
      <!-- Credential fields — driven by CREDENTIAL_SCHEMA so each broker
           renders exactly the fields it needs. Kite gets 4 (api_key +
           api_secret + password + totp_token); Groww gets 3 (no
           password); Dhan gets 2 (client_id + access_token). Operator
           changing the Broker dropdown swaps the field set instantly. -->
      {#each credentialFields(form.broker_id) as f (f.key)}
        <div class={'bf-field' + (f.key === 'api_key' || f.key === 'client_id' || f.key === 'access_token' || f.key === 'api_secret' ? ' bf-field-wide' : '')}>
          <label class="field-label" for={'bf-' + f.key}>
            {f.label}
            {#if isEditing && f.secret}<span class="bf-hint">(blank = unchanged)</span>{/if}
          </label>
          <input id={'bf-' + f.key}
                 type={f.secret ? 'password' : 'text'}
                 class="field-input font-mono"
                 placeholder={isEditing && f.secret
                              ? '••••••• (leave blank to keep)'
                              : f.label.toLowerCase()}
                 bind:value={form[f.key]} />
        </div>
      {/each}
      <div class="bf-field">
        <label class="field-label" for="bf-priority">Priority</label>
        <input id="bf-priority" type="number" class="field-input font-mono"
               min="0" max="999" step="1"
               placeholder="100"
               title="PriceBroker fallback order — lower = tried first for shared market data. Default 100; ties broken by insertion order."
               bind:value={form.priority} />
      </div>
      <div class="bf-field bf-field-wide">
        <label class="field-label" for="bf-ip">Source IP (optional)</label>
        <input id="bf-ip" type="text" class="field-input font-mono"
               placeholder="2a02:4780:12:9e1d::N"
               bind:value={form.source_ip} />
      </div>
      <div class="bf-field bf-field-wide">
        <label class="field-label" for="bf-notes">Notes (optional)</label>
        <input id="bf-notes" type="text" class="field-input"
               placeholder="anything you want to remember about this account"
               bind:value={form.notes} />
      </div>
      <div class="bf-field bf-field-toggle">
        <label class="field-label" for="bf-active">Status</label>
        <label class="bf-toggle">
          <input id="bf-active" type="checkbox" bind:checked={form.is_active} />
          <span>active</span>
        </label>
      </div>
      <div class="bf-field bf-field-toggle">
        <span class="field-label">Historical data</span>
        <button type="button"
                class="brokers-form-toggle"
                class:active={_formHistEnabled}
                aria-pressed={_formHistEnabled}
                onclick={() => _formHistEnabled = !_formHistEnabled}>
          {_formHistEnabled ? 'Enabled' : 'Disabled'}
        </button>
        <span class="bf-hint bf-hint-block">Eligible to serve /api/options/historical when others are rate-limited.</span>
      </div>
      <div class="bf-field bf-field-wide">
        <label class="field-label" for="bf-extra">
          Advanced settings (JSON)
          <span class="bf-hint">— per-broker tuning knobs</span>
        </label>
        <textarea id="bf-extra" class="field-input font-mono text-[0.6rem]"
                  rows="3"
                  placeholder="{'{}'}"
                  title='Free-form JSON object for per-broker config (rate limit overrides, custom endpoints, etc.). Adapters read what they need; unknown keys are ignored.'
                  bind:value={form.extra_config_text}></textarea>
      </div>
    </div>

    <div class="bf-actions">
      <button type="button" class="btn-primary text-[0.6rem] py-1 px-3"
              onclick={save}>{isEditing ? 'Save changes' : 'Create'}</button>
      <button type="button" class="btn-secondary text-[0.6rem] py-1 px-3"
              onclick={() => resetForm()}>Cancel</button>
    </div>

    <div class="text-[0.55rem] text-[#7e97b8] italic mt-2">
      Encryption: secrets are Fernet-encrypted at rest with a key derived
      from <span class="font-mono">cookie_secret</span> via HKDF. Never
      stored in plaintext, never returned by the API. After saving, click
      <b>Test</b> on the row to confirm the credential chain authenticates.
    </div>
  </div>
{/if}

{/if}

<style>
  /* .empty-state rules removed — access-denied panel migrated to
     EmptyState component (slice AE). */
  .brokers-list-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    padding: 0 0.25rem 0.4rem;
    border-bottom: 1px solid rgba(251,191,36,0.18);
    margin-bottom: 0.4rem;
  }
  .brokers-h {
    font-size: var(--fs-sm);
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: #fbbf24;
    margin: 0;
  }

  /* Horizontal scroll wrapper — narrow viewports otherwise push the
     status pill (and the action buttons) out past the card edge. */
  .brokers-scroll {
    width: 100%;
    overflow-x: auto;
  }
  .brokers-table {
    width: 100%;
    min-width: 720px;
    border-collapse: collapse;
    font-family: monospace;
    font-size: var(--fs-sm);
    table-layout: auto;
  }
  .brokers-table td:nth-child(5),
  .brokers-table td:nth-child(6) { width: 1%; white-space: nowrap; }   /* status, historical */
  .brokers-table td:nth-child(8),
  .brokers-table td:nth-child(9) { width: 1%; white-space: nowrap; }   /* test, actions */
  .brokers-table th {
    text-align: left;
    color: var(--algo-muted);
    font-weight: 700;
    padding: 0.25rem 0.4rem;
    border-bottom: 1px solid rgba(255,255,255,0.06);
    font-size: var(--fs-xs);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .brokers-table td {
    padding: 0.3rem 0.4rem;
    color: var(--algo-slate);
    border-bottom: 1px solid rgba(255,255,255,0.04);
  }
  .brokers-table tr.row-inactive td { opacity: 0.5; }
  .mono-trunc {
    max-width: 220px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .notes {
    color: var(--algo-muted);
    font-style: italic;
    max-width: 200px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .test-cell, .action-cell {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    white-space: nowrap;
  }

  .status-pill {
    font-family: monospace;
    font-size: var(--fs-2xs);
    font-weight: 700;
    padding: 1px 5px;
    border-radius: 2px;
    border: 1px solid currentColor;
    letter-spacing: 0.04em;
  }
  .status-loaded   { color: #4ade80; background: rgba(74,222,128,0.10); }
  .status-pending  { color: #fbbf24; background: rgba(251,191,36,0.10); }
  .status-inactive { color: var(--algo-muted); background: rgba(126,151,184,0.10); }

  .brokers-hist-cell { text-align: center; }
  .brokers-hist-pill {
    display: inline-block;
    padding: 1px 6px;
    border-radius: 3px;
    font-size: var(--fs-xs);
    font-weight: 800;
    font-family: monospace;
    letter-spacing: 0.06em;
  }
  .brokers-hist-pill.hist-on  { background: rgba(34,211,238,0.18); border: 1px solid rgba(103,232,249,0.55); color: #22d3ee; }
  .brokers-hist-pill.hist-off { background: rgba(126,151,184,0.10); border: 1px solid rgba(126,151,184,0.30); color: var(--algo-muted); }

  .brokers-form-toggle {
    padding: 0.25rem 0.6rem;
    background: rgba(34,211,238,0.10);
    border: 1px solid rgba(34,211,238,0.45);
    border-radius: 3px;
    color: #22d3ee;
    font-family: monospace;
    font-size: var(--fs-md);
    font-weight: 700;
    cursor: pointer;
    white-space: nowrap;
  }
  .brokers-form-toggle:hover  { background: rgba(34,211,238,0.20); }
  .brokers-form-toggle.active { background: rgba(34,211,238,0.25); color: #67e8f9; border-color: rgba(103,232,249,0.75); }
  .brokers-form-toggle:not(.active) { color: var(--algo-muted); border-color: rgba(126,151,184,0.30); background: rgba(126,151,184,0.08); }

  .bf-hint-block {
    display: block;
    margin-left: 0;
    margin-top: 0.1rem;
    line-height: 1.3;
  }

  .test-result {
    font-family: monospace;
    font-weight: 700;
    font-size: var(--fs-xl);
    line-height: 1;
    cursor: help;
  }
  .test-result.ok   { color: #4ade80; }
  .test-result.fail { color: #f87171; }

  :global(.brokers-table .destructive) {
    border-color: rgba(248,113,113,0.45) !important;
    color: #f87171 !important;
  }
  :global(.brokers-table .destructive:hover:not(:disabled)) {
    background: rgba(248,113,113,0.10) !important;
  }

  /* Form layout — two-column grid that collapses on narrow viewports. */
  .brokers-form {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 0.5rem 0.6rem;
  }
  .bf-field {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
  }
  .bf-field-wide { grid-column: span 2; }
  @media (max-width: 600px) {
    .bf-field-wide { grid-column: span 1; }
  }
  .bf-field-toggle {
    flex-direction: row;
    align-items: flex-end;
    gap: 0.5rem;
  }
  .bf-toggle {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    font-size: var(--fs-sm);
    font-family: monospace;
    color: var(--algo-slate);
  }
  .bf-hint {
    color: var(--algo-muted);
    font-size: var(--fs-2xs);
    font-weight: 400;
    margin-left: 0.3rem;
  }
  .bf-actions {
    display: flex;
    gap: 0.4rem;
    margin-top: 0.6rem;
  }
</style>
