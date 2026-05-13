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

  import { onMount, onDestroy } from 'svelte';
  import { goto } from '$app/navigation';
  import { authStore, clientTimestamp, visibleInterval } from '$lib/stores';
  import {
    fetchBrokerAccounts, createBrokerAccount, updateBrokerAccount,
    deleteBrokerAccount, testBrokerAccount,
  } from '$lib/api';
  import InfoHint from '$lib/InfoHint.svelte';

  /** @type {Array<{id:number,account:string,broker_id:string,api_key:string,
   *   source_ip:string|null,is_active:boolean,notes:string|null,
   *   created_at:string,updated_at:string,loaded:boolean}>} */
  let accounts = $state([]);
  let loading  = $state(true);
  let error    = $state('');
  let note     = $state('');

  // Form state — reused for Create + Edit. `editing = ''` means we're
  // in Create mode (account code editable); `editing = 'ZG0790'` puts
  // us in Edit mode for that row.
  let editing = $state(/** @type {string} */ (''));
  let form    = $state({
    account: '', broker_id: 'kite', api_key: '',
    api_secret: '', password: '', totp_token: '',
    source_ip: '', is_active: true, notes: '',
  });

  /** @type {Record<string, {ok:boolean, detail:string} | undefined>} */
  let testResults = $state({});
  let testInFlight = $state(/** @type {string} */ (''));
  let refreshTeardown;

  function resetForm(/** @type {string} */ acct = '') {
    editing = acct;
    form = {
      account: acct, broker_id: 'kite', api_key: '',
      api_secret: '', password: '', totp_token: '',
      source_ip: '', is_active: true, notes: '',
    };
    error = ''; note = '';
  }

  function startEdit(/** @type {any} */ row) {
    editing = row.account;
    form = {
      account:    row.account,
      broker_id:  row.broker_id,
      api_key:    row.api_key,
      api_secret: '',                  // blank = don't change
      password:   '',
      totp_token: '',
      source_ip:  row.source_ip || '',
      is_active:  !!row.is_active,
      notes:      row.notes || '',
    };
    error = ''; note = '';
  }

  async function load() {
    try {
      accounts = await fetchBrokerAccounts() || [];
      error = '';
    } catch (e) { error = e.message; }
    finally { loading = false; }
  }

  async function save() {
    error = ''; note = '';
    try {
      if (editing) {
        // PATCH — only send fields with values; empty secret fields are
        // explicitly omitted so the backend's "leave unchanged" logic
        // gets the right signal.
        const payload = {
          broker_id: form.broker_id, api_key: form.api_key,
          source_ip: form.source_ip, is_active: form.is_active,
          notes: form.notes,
        };
        if (form.api_secret) payload.api_secret = form.api_secret;
        if (form.password)   payload.password   = form.password;
        if (form.totp_token) payload.totp_token = form.totp_token;
        await updateBrokerAccount(editing, payload);
        note = `Updated ${editing}`;
      } else {
        if (!form.account) { error = 'Account code is required.'; return; }
        if (!form.api_key || !form.api_secret || !form.password || !form.totp_token) {
          error = 'api_key, api_secret, password, and totp_token are all required for new accounts.';
          return;
        }
        await createBrokerAccount(form);
        note = `Created ${form.account}`;
      }
      resetForm();
      await load();
    } catch (e) {
      error = `Save failed: ${e.message}`;
    }
  }

  async function destroy(/** @type {any} */ row) {
    if (!confirm(`Delete broker account ${row.account}? This is irreversible.`)) return;
    try {
      await deleteBrokerAccount(row.account);
      note = `Deleted ${row.account}`;
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
    } catch (e) {
      testResults[row.account] = { ok: false, detail: e.message };
      testResults = { ...testResults };
    } finally {
      testInFlight = '';
    }
  }

  onMount(() => {
    if (!$authStore.user || $authStore.user.role !== 'admin') {
      goto('/signin'); return;
    }
    load();
    // Mild polling so the "loaded" pill keeps up if Connections
    // singleton picks up a row a few seconds after the operator saves.
    refreshTeardown = visibleInterval(load, 15000);
  });
  onDestroy(() => { refreshTeardown?.(); });
</script>

<svelte:head><title>Brokers | RamboQuant Analytics</title></svelte:head>

<div class="page-header">
  <h1 class="page-title-chip">Brokers</h1>
  <InfoHint popup text={'CRUD over the broker-accounts table. New accounts go live immediately — the Connections singleton reloads on every save, so the next broker call uses the new credentials without a service restart. Secrets (<span class="font-mono">api_secret</span>, <span class="font-mono">password</span>, <span class="font-mono">totp_token</span>) are encrypted at rest with a key derived from <span class="font-mono">cookie_secret</span> via HKDF, never readable from the API. On Edit, leave a secret field blank to keep the existing value.'} />
  <span class="algo-ts">{clientTimestamp()}</span>
</div>

{#if error}<div class="mb-3 p-2 rounded bg-red-500/15 text-red-300 text-[0.65rem] border border-red-500/40">{error}</div>{/if}
{#if note}<div class="mb-3 p-2 rounded bg-emerald-500/10 text-emerald-300 text-[0.65rem] border border-emerald-500/30">{note}</div>{/if}

<!-- Account list -->
<div class="algo-status-card p-2 mb-3" data-status="inactive">
  <div class="brokers-list-header">
    <h2 class="brokers-h">
      Accounts <span class="opacity-60 font-normal ml-1">({accounts.length})</span>
    </h2>
    <button type="button" class="btn-primary text-[0.6rem] py-1 px-3"
            onclick={() => resetForm('')}
            disabled={editing === ''}>+ New account</button>
  </div>
  {#if loading}
    <div class="text-[0.6rem] text-[#7e97b8] italic">Loading…</div>
  {:else if !accounts.length}
    <div class="text-[0.6rem] text-[#7e97b8] italic">
      No broker accounts yet. Use <b>+ New account</b> to add one
      (or seed <span class="font-mono">secrets.yaml::kite_accounts</span> on the
      server and restart — the table will auto-seed from YAML on first run).
    </div>
  {:else}
    <div class="brokers-scroll">
    <table class="brokers-table">
      <thead>
        <tr>
          <th>Account</th>
          <th>Broker</th>
          <th>API key</th>
          <th>Source IP</th>
          <th>Status</th>
          <th>Notes</th>
          <th>Test</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {#each accounts as row}
          <tr class:row-inactive={!row.is_active}>
            <td class="font-mono">{row.account}</td>
            <td>{row.broker_id}</td>
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
      {editing ? `Edit ${editing}` : 'New account'}
    </h2>
    <div class="brokers-form">
      <div class="bf-field">
        <label class="field-label" for="bf-acct">Account code</label>
        <input id="bf-acct" type="text" class="field-input font-mono"
               placeholder="ZG0790"
               disabled={!!editing}
               bind:value={form.account} />
      </div>
      <div class="bf-field">
        <label class="field-label" for="bf-broker">Broker</label>
        <input id="bf-broker" type="text" class="field-input font-mono"
               placeholder="kite"
               bind:value={form.broker_id} />
      </div>
      <div class="bf-field bf-field-wide">
        <label class="field-label" for="bf-key">API key</label>
        <input id="bf-key" type="text" class="field-input font-mono"
               placeholder="kite api_key"
               bind:value={form.api_key} />
      </div>
      <div class="bf-field bf-field-wide">
        <label class="field-label" for="bf-secret">
          API secret {#if editing}<span class="bf-hint">(blank = unchanged)</span>{/if}
        </label>
        <input id="bf-secret" type="password" class="field-input font-mono"
               placeholder={editing ? '••••••• (leave blank to keep)' : 'kite api_secret'}
               bind:value={form.api_secret} />
      </div>
      <div class="bf-field">
        <label class="field-label" for="bf-pwd">
          Password {#if editing}<span class="bf-hint">(blank = unchanged)</span>{/if}
        </label>
        <input id="bf-pwd" type="password" class="field-input font-mono"
               placeholder={editing ? '••••••• (leave blank)' : 'login password'}
               bind:value={form.password} />
      </div>
      <div class="bf-field">
        <label class="field-label" for="bf-totp">
          TOTP seed {#if editing}<span class="bf-hint">(blank = unchanged)</span>{/if}
        </label>
        <input id="bf-totp" type="password" class="field-input font-mono"
               placeholder={editing ? '••••••• (leave blank)' : 'TOTP secret seed'}
               bind:value={form.totp_token} />
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
    </div>

    <div class="bf-actions">
      <button type="button" class="btn-primary text-[0.6rem] py-1 px-3"
              onclick={save}>{editing ? 'Save changes' : 'Create'}</button>
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

<style>
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
    font-size: 0.6rem;
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
    font-size: 0.62rem;
    table-layout: auto;
  }
  .brokers-table td:nth-child(5) { width: 1%; white-space: nowrap; }   /* status */
  .brokers-table td:nth-child(7),
  .brokers-table td:nth-child(8) { width: 1%; white-space: nowrap; }   /* test, actions */
  .brokers-table th {
    text-align: left;
    color: #7e97b8;
    font-weight: 700;
    padding: 0.25rem 0.4rem;
    border-bottom: 1px solid rgba(255,255,255,0.06);
    font-size: 0.55rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .brokers-table td {
    padding: 0.3rem 0.4rem;
    color: #c8d8f0;
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
    color: #7e97b8;
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
    font-size: 0.5rem;
    font-weight: 700;
    padding: 1px 5px;
    border-radius: 2px;
    border: 1px solid currentColor;
    letter-spacing: 0.04em;
  }
  .status-loaded   { color: #4ade80; background: rgba(74,222,128,0.10); }
  .status-pending  { color: #fbbf24; background: rgba(251,191,36,0.10); }
  .status-inactive { color: #7e97b8; background: rgba(126,151,184,0.10); }

  .test-result {
    font-family: monospace;
    font-weight: 700;
    font-size: 0.85rem;
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
    font-size: 0.62rem;
    font-family: monospace;
    color: #c8d8f0;
  }
  .bf-hint {
    color: #7e97b8;
    font-size: 0.5rem;
    font-weight: 400;
    margin-left: 0.3rem;
  }
  .bf-actions {
    display: flex;
    gap: 0.4rem;
    margin-top: 0.6rem;
  }
</style>
