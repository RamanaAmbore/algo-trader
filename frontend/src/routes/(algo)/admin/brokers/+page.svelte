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
  import { visibleInterval } from '$lib/stores';
  import AlgoTimestamp from '$lib/AlgoTimestamp.svelte';
  import { accountDisplayOrder } from '$lib/data/accountSort.js';
  import { userRole, userCaps, userCapsReady, hasCap } from '$lib/rbac';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import {
    fetchBrokerAccounts, createBrokerAccount, updateBrokerAccount,
    deleteBrokerAccount, testBrokerAccount, restoreBrokerPriority,
    fetchBrokerConnectionEvents,
  } from '$lib/api';
  import StaleBanner    from '$lib/StaleBanner.svelte';
  import Select         from '$lib/Select.svelte';
  import ConfirmModal   from '$lib/ConfirmModal.svelte';
  import LoadingSkeleton from '$lib/LoadingSkeleton.svelte';
  import EmptyState from '$lib/EmptyState.svelte';
  import { toast } from '$lib/data/toastStore.svelte.js';
  import { onMount } from 'svelte';

  /** @type {Array<{id:number,account:string,broker_id:string,api_key:string,
   *   source_ip:string|null,is_active:boolean,historical_data_enabled:boolean,
   *   notes:string|null,created_at:string,updated_at:string,loaded:boolean,
   *   poll_priority:string,auto_downgrade_enabled:boolean,
   *   auto_downgraded_at:string|null,auto_downgrade_reason:string|null,
   *   circuit_breaker_enabled:boolean,display_order:number}>} */
  let accounts = $state([]);

  // Per-row priority dropdown open state: { [account]: boolean }
  let priorityDropdownOpen = $state(/** @type {Record<string,boolean>} */ ({}));

  /** Whether a broker_id identifies a Dhan account. */
  function isDhan(/** @type {string} */ brokerId) {
    return (brokerId || '').toLowerCase().includes('dhan');
  }

  /** Cycle background colour for each poll_priority value (CSS vars). */
  const PRIORITY_STYLES = {
    hot:  { bg: 'rgba(74,222,128,0.18)',  border: 'rgba(74,222,128,0.55)',  color: 'var(--c-long)',  label: 'HOT' },
    warm: { bg: 'rgba(251,191,36,0.18)',  border: 'rgba(251,191,36,0.55)',  color: 'var(--c-action)',  label: 'WARM' },
    cold: { bg: 'rgba(148,163,184,0.18)', border: 'rgba(148,163,184,0.55)', color: '#94a3b8',  label: 'COLD' },
  };
  function priorityStyle(/** @type {string} */ p) {
    return PRIORITY_STYLES[p] || PRIORITY_STYLES.hot;
  }

  async function setPriority(/** @type {any} */ row, /** @type {string} */ newPriority) {
    priorityDropdownOpen[row.account] = false;
    try {
      await updateBrokerAccount(row.account, { poll_priority: newPriority });
      // Optimistic update so the chip reflects the change immediately.
      const idx = accounts.findIndex(a => a.account === row.account);
      if (idx !== -1) {
        accounts[idx] = {
          ...accounts[idx],
          poll_priority: newPriority,
          auto_downgraded_at: null,
          auto_downgrade_reason: null,
        };
      }
      toast.success(`${row.account} priority → ${newPriority}`);
    } catch (e) {
      toast.error(`Failed: ${e.message}`);
    }
  }

  async function toggleAutoDowngrade(/** @type {any} */ row) {
    const newVal = !row.auto_downgrade_enabled;
    try {
      await updateBrokerAccount(row.account, { auto_downgrade_enabled: newVal });
      const idx = accounts.findIndex(a => a.account === row.account);
      if (idx !== -1) {
        accounts[idx] = { ...accounts[idx], auto_downgrade_enabled: newVal };
      }
    } catch (e) {
      toast.error(`Failed: ${e.message}`);
    }
  }

  async function toggleCircuitBreaker(/** @type {any} */ row) {
    const newVal = !row.circuit_breaker_enabled;
    try {
      await updateBrokerAccount(row.account, { circuit_breaker_enabled: newVal });
      const idx = accounts.findIndex(a => a.account === row.account);
      if (idx !== -1) {
        accounts[idx] = { ...accounts[idx], circuit_breaker_enabled: newVal };
      }
      toast.success(`${row.account} circuit breaker ${newVal ? 'enabled' : 'disabled'}`);
    } catch (e) {
      toast.error(`Failed: ${e.message}`);
    }
  }

  async function restorePriority(/** @type {any} */ row) {
    try {
      const updated = await restoreBrokerPriority(row.account);
      const idx = accounts.findIndex(a => a.account === row.account);
      if (idx !== -1) accounts[idx] = { ...accounts[idx], ...updated };
      toast.success(`${row.account} priority restored to HOT`);
    } catch (e) {
      toast.error(`Restore failed: ${e.message}`);
    }
  }

  // broker_priority_changed WS subscription — fires toast on auto-downgrade.
  // Uses the performance socket (same endpoint used by the rest of the algo
  // layout) so no new socket is opened.
  let _wsUnsub = /** @type {(() => void) | null} */ (null);
  // Import is lazy inside onMount to keep SSR safe.
  onMount(() => {
    import('$lib/ws.js').then(({ createPerformanceSocket }) => {
      _wsUnsub = createPerformanceSocket((msg) => {
        if (msg?.type === 'broker_priority_changed' && msg.auto) {
          const acct = msg.account || '?';
          toast.info(
            `${acct} auto-downgraded to cold — click chip to restore`,
            { timeoutMs: 4000 },
          );
          // Refresh the list so the chip re-renders with the new state.
          load();
        }
      });
    }).catch(() => {});
    return () => { _wsUnsub?.(); };
  });
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
    // Display ordering (Jul 2026). Lower = shown first across all UI surfaces.
    display_order: 500,
    // JSON text bound to a <textarea>; parsed at save time. Bound here as
    // a string so the operator's in-progress typing isn't constantly
    // re-validated. Empty = `{}` server-side.
    extra_config_text: '{}',
  });

  /** @type {Record<string, {ok:boolean, detail:string} | undefined>} */
  let testResults = $state({});
  let testInFlight = $state(/** @type {string} */ (''));
  let refreshTeardown;

  // Per-account circuit state for the red dot on the priority chip.
  // Populated by the broker-health fetch; keyed by account code.
  // circuit_breaker_enabled is also tracked so the dot only shows for opt-in accounts.
  /** @type {Record<string, string>} */
  let circuitStateMap = $state({});
  /** @type {Record<string, boolean>} */
  let breakerOptinMap = $state({});

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
      display_order: 500,
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
      priority:      typeof row.priority      === 'number' ? row.priority      : 100,
      display_order: typeof row.display_order === 'number' ? row.display_order : 500,
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
    // Fetch broker-health for circuit-state dot (non-critical — never
    // blocks the main list render).
    try {
      const { fetchBrokerHealth } = await import('$lib/api');
      const health = await fetchBrokerHealth();
      const map = /** @type {Record<string,string>} */ ({});
      const optinMap = /** @type {Record<string,boolean>} */ ({});
      for (const entry of (health?.accounts || [])) {
        map[entry.account] = entry.circuit_state || 'closed';
        optinMap[entry.account] = !!entry.circuit_breaker_enabled;
      }
      circuitStateMap = map;
      breakerOptinMap = optinMap;
    } catch (_) {}
  }

  /**
   * Parse and validate the Advanced settings JSON textarea.
   * @returns {{ ok: true, parsedExtra: object } | { ok: false, error: string }}
   */
  function _parseExtraConfig() {
    try {
      const parsedExtra = JSON.parse(form.extra_config_text || '{}');
      if (!parsedExtra || typeof parsedExtra !== 'object' || Array.isArray(parsedExtra)) {
        return { ok: false, error: 'Advanced settings must be a JSON object.' };
      }
      return { ok: true, parsedExtra };
    } catch (parseErr) {
      return { ok: false, error: `Advanced settings JSON invalid: ${parseErr.message}` };
    }
  }

  /**
   * Returns labels of required fields missing from the form for the current broker.
   * @param {Array<{key:string,label:string,secret:boolean,required:boolean}>} fields
   * @returns {string[]}
   */
  function _findMissingRequiredFields(fields) {
    return fields.filter(f => f.required && !form[f.key]).map(f => f.label);
  }

  /**
   * Build the PATCH payload for an Edit save.
   * Only sends secret fields when the operator has typed a new value.
   * @param {object} parsedExtra
   * @param {Array<{key:string,secret:boolean}>} fields
   * @returns {object}
   */
  function _buildEditPayload(parsedExtra, fields) {
    const payload = /** @type {Record<string,any>} */ ({
      broker_id: form.broker_id,
      api_key: form.api_key,
      client_id: form.client_id || '',
      source_ip: form.source_ip,
      is_active: form.is_active,
      historical_data_enabled: _formHistEnabled,
      notes: form.notes,
      priority: Number(form.priority) || 100,
      display_order: Number(form.display_order) || 500,
      extra_config: parsedExtra,
    });
    // Only send each secret if the operator typed a new value AND
    // that field is actually relevant to the selected broker.
    for (const f of fields) {
      if (f.secret && form[f.key]) payload[f.key] = form[f.key];
    }
    return payload;
  }

  /**
   * Build the POST payload for a Create save.
   * @param {object} parsedExtra
   * @returns {object}
   */
  function _buildCreatePayload(parsedExtra) {
    return {
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
      priority:      Number(form.priority) || 100,
      display_order: Number(form.display_order) || 500,
      extra_config: parsedExtra,
    };
  }

  async function save() {
    error = '';
    try {
      // Parse + validate Advanced JSON. Bad JSON aborts the save so the
      // operator never accidentally persists a malformed config.
      const extraResult = _parseExtraConfig();
      if (!extraResult.ok) { error = /** @type {any} */ (extraResult).error; return; }
      const { parsedExtra } = /** @type {{ ok: true, parsedExtra: object }} */ (extraResult);
      const fieldsForThisBroker = credentialFields(form.broker_id);

      if (isEditing) {
        // PATCH — only send fields with values; empty secret fields are
        // explicitly omitted so the backend's "leave unchanged" logic
        // gets the right signal.
        await updateBrokerAccount(editing, _buildEditPayload(parsedExtra, fieldsForThisBroker));
        toast.success(`Updated ${editing}`);
      } else {
        if (!form.account) { error = 'Account code is required.'; return; }
        // Broker-aware required-field check. Each broker schema declares
        // which fields are mandatory at create time (Kite needs 4 fields,
        // Dhan only 2). Missing any one of them aborts the save with a
        // specific error so the operator knows what to fill in.
        const missing = _findMissingRequiredFields(fieldsForThisBroker);
        if (missing.length) {
          error = `Required for ${form.broker_id}: ${missing.join(', ')}.`;
          return;
        }
        await createBrokerAccount(_buildCreatePayload(parsedExtra));
        toast.success(`Created ${form.account}`);
      }
      resetForm();
      await load();
      // Refresh the canonical display_order store so all other pages
      // (dropdowns, health badge, etc.) reflect the new order immediately.
      accountDisplayOrder.refresh().catch(() => {});
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

  // ── Connection Log ─────────────────────────────────────────────────
  /** @type {Array<{id:number,account:string,broker_id:string,event_type:string,detail:string|null,event_ts:string}>} */
  let connEvents = $state([]);
  let connLoading = $state(false);
  let connError   = $state('');
  let _loadingConnEvents = $state(false);

  // Filter state
  let connFilterAccount   = $state('');
  let connFilterEventType = $state('');

  // Default "since" = 7 days ago as YYYY-MM-DD
  function _sevenDaysAgo() {
    const d = new Date();
    d.setDate(d.getDate() - 7);
    return d.toISOString().slice(0, 10);
  }
  let connFilterSince = $state(_sevenDaysAgo());

  const CONN_EVENT_TYPES = [
    { value: '',                    label: 'All events' },
    { value: 'login_ok',            label: 'login_ok' },
    { value: 'auth_fail',           label: 'auth_fail' },
    { value: 'login_fail',          label: 'login_fail' },
    { value: 'rotation_detected',   label: 'rotation_detected' },
    { value: 'circuit_open',        label: 'circuit_open' },
    { value: 'circuit_close',       label: 'circuit_close' },
    { value: 'rate_limited',        label: 'rate_limited' },
    { value: 'token_expiry',        label: 'token_expiry' },
    { value: 'fetch_ok_recovery',   label: 'fetch_ok_recovery' },
    { value: 'token_ok',            label: 'token_ok' },
  ];

  /** Map event_type to a CSS class suffix for coloring the Event cell. */
  function _connEventCls(/** @type {string} */ evType) {
    if (['login_ok', 'token_ok', 'fetch_ok_recovery', 'circuit_close'].includes(evType)) return 'conn-ev-green';
    if (['login_fail', 'auth_fail', 'circuit_open', 'rotation_detected'].includes(evType)) return 'conn-ev-red';
    if (['rate_limited', 'token_expiry'].includes(evType)) return 'conn-ev-amber';
    return 'conn-ev-muted';
  }

  /** Format ISO timestamp to HH:MM:SS IST. */
  function _fmtConnTime(/** @type {string} */ iso) {
    if (!iso) return '—';
    try {
      return new Date(iso).toLocaleTimeString('en-IN', {
        timeZone: 'Asia/Kolkata',
        hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
      }) + ' IST';
    } catch (_) { return iso; }
  }

  function _fmtConnDetail(/** @type {any} */ detail) {
    if (detail == null) return '—';
    if (typeof detail === 'string') return detail || '—';
    if (typeof detail === 'object')
      return Object.entries(detail).map(([k, v]) => `${k}: ${v}`).join(' · ') || '—';
    return String(detail);
  }

  async function loadConnEvents() {
    if (_loadingConnEvents) return;
    _loadingConnEvents = true;
    connLoading = true;
    connError = '';
    try {
      const data = await fetchBrokerConnectionEvents({
        account:    connFilterAccount   || undefined,
        event_type: connFilterEventType || undefined,
        since:      connFilterSince     || undefined,
        limit:      200,
      });
      connEvents = data?.events ?? (Array.isArray(data) ? data : []);
    } catch (e) {
      connError = String(e?.message ?? e ?? 'Failed to load connection events');
    } finally {
      connLoading = false;
      _loadingConnEvents = false;
    }
  }

  let _connRefreshTeardown = /** @type {(() => void) | undefined} */ (undefined);

  // ── Canonical $effect-gated auth. manage_brokers admits designated + admin.
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
      loadConnEvents();
      // Throttle to 60 s on hidden — broker LOADED/PENDING status is
      // critical for operator awareness; keep a slow heartbeat alive.
      refreshTeardown = visibleInterval(load, 15000, 'throttle:60000');
      _connRefreshTeardown = visibleInterval(loadConnEvents, 30000);
    }
  });
  onDestroy(() => { refreshTeardown?.(); _connRefreshTeardown?.(); });
</script>

<ConfirmModal bind:this={_confirmRef} />

<svelte:head><title>Brokers | RamboQuant Analytics</title></svelte:head>

<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">Brokers</h1>
  </span>
  <AlgoTimestamp />
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
    <div class="brokers-scroll algo-grid-chrome content-fade-in">
    <table class="algo-table brokers-table">
      <thead>
        <tr>
          <th>Account</th>
          <th>Broker</th>
          <th>API key</th>
          <th>Source IP</th>
          <th>Status</th>
          <th>Historical</th>
          <th>Poll</th>
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
            <!-- Poll priority cell — Dhan only; Kite/Groww show em-dash -->
            <td class="priority-cell">
              {#if isDhan(row.broker_id)}
                {@const ps = priorityStyle(row.poll_priority || 'hot')}
                {@const _cbEnabled = row.circuit_breaker_enabled ?? breakerOptinMap[row.account] ?? false}
                {@const isOpen = (_cbEnabled && circuitStateMap[row.account] === 'open')}
                <div class="priority-wrap">
                  <div class="priority-chip-row">
                    <!-- Priority chip — click opens dropdown -->
                    <button
                      type="button"
                      class="priority-chip"
                      style="background:{ps.bg}; border-color:{ps.border}; color:{ps.color};"
                      onclick={() => priorityDropdownOpen[row.account] = !priorityDropdownOpen[row.account]}
                      title="Click to change poll priority"
                      aria-expanded={!!priorityDropdownOpen[row.account]}
                    >
                      {ps.label}
                      {#if isOpen}
                        <span class="circuit-dot" title="Circuit breaker OPEN"></span>
                      {/if}
                    </button>
                    <!-- Circuit-breaker opt-in checkbox -->
                    <label class="auto-dg-label" title="Enable circuit breaker: pause fetches for 5 min after 3 consecutive failures">
                      <input type="checkbox"
                             checked={_cbEnabled}
                             onchange={() => toggleCircuitBreaker(row)} />
                      <span class="auto-dg-text">breaker</span>
                    </label>
                    <!-- Auto-downgrade checkbox -->
                    <label class="auto-dg-label" title="Auto-downgrade to cold after 5 breaker opens in 15 min">
                      <input type="checkbox"
                             checked={row.auto_downgrade_enabled}
                             onchange={() => toggleAutoDowngrade(row)} />
                      <span class="auto-dg-text">auto</span>
                    </label>
                  </div>
                  <!-- Dropdown -->
                  {#if priorityDropdownOpen[row.account]}
                    <div class="priority-dropdown">
                      {#each Object.entries(PRIORITY_STYLES) as [key, s]}
                        <button type="button" class="priority-dropdown-item"
                                style="color:{s.color}"
                                onclick={() => setPriority(row, key)}>
                          {s.label}
                        </button>
                      {/each}
                    </div>
                  {/if}
                  <!-- Auto-downgrade annotation -->
                  {#if row.auto_downgraded_at}
                    <div class="auto-dg-annotation">
                      auto @ {new Date(row.auto_downgraded_at).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Kolkata' })} IST
                      <button type="button" class="restore-link"
                              onclick={() => restorePriority(row)}>restore</button>
                    </div>
                  {/if}
                </div>
              {:else}
                <span class="priority-na">—</span>
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
    <h3 class="section-heading">
      {isEditing ? `Edit ${editing}` : 'New account'}
    </h3>
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
      <div class="bf-field">
        <label class="field-label" for="bf-display-order">Display Order</label>
        <input id="bf-display-order" type="number" class="field-input font-mono"
               min="1" max="999" step="1"
               placeholder="500"
               title="Canonical display position across all UI surfaces (dropdowns, health badge, grids). Lower = shown first. Seeded: Kite=10–20, DH3747=100, Groww=200, DH6847=999."
               bind:value={form.display_order} />
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

    <div class="text-[0.55rem] text-[var(--c-muted)] italic mt-2">
      Encryption: secrets are Fernet-encrypted at rest with a key derived
      from <span class="font-mono">cookie_secret</span> via HKDF. Never
      stored in plaintext, never returned by the API. After saving, click
      <b>Test</b> on the row to confirm the credential chain authenticates.
    </div>
  </div>
{/if}

<!-- Connection Log -->
<div class="algo-card mb-3">
  <div class="brokers-list-header">
    <h2 class="brokers-h">Connection Log</h2>
    <button type="button" class="btn-secondary text-[0.55rem] py-0.5 px-2"
            disabled={connLoading}
            onclick={loadConnEvents}>
      {connLoading ? '…' : 'Refresh'}
    </button>
  </div>

  <!-- Filter bar -->
  <div class="conn-filter-bar">
    <div class="conn-filter-field">
      <label class="field-label" for="conn-acct">Account</label>
      <select id="conn-acct" class="field-input conn-select"
              bind:value={connFilterAccount}
              onchange={loadConnEvents}>
        <option value="">All accounts</option>
        {#each accounts as a}
          <option value={a.account}>{a.account}</option>
        {/each}
      </select>
    </div>
    <div class="conn-filter-field">
      <label class="field-label" for="conn-evtype">Event type</label>
      <select id="conn-evtype" class="field-input conn-select"
              bind:value={connFilterEventType}
              onchange={loadConnEvents}>
        {#each CONN_EVENT_TYPES as et}
          <option value={et.value}>{et.label}</option>
        {/each}
      </select>
    </div>
    <div class="conn-filter-field">
      <label class="field-label" for="conn-since">Since</label>
      <input id="conn-since" type="date" class="field-input conn-date"
             bind:value={connFilterSince}
             onchange={loadConnEvents} />
    </div>
  </div>

  {#if connError}
    <div class="text-[0.6rem] text-[var(--c-short)] mb-2">{connError}</div>
  {/if}

  <div class="brokers-scroll algo-grid-chrome">
    {#if connLoading && connEvents.length === 0}
      <div class="conn-empty">Loading…</div>
    {:else if !connLoading && connEvents.length === 0}
      <div class="conn-empty">No events found for the selected filters.</div>
    {:else}
      <table class="algo-table conn-table">
        <thead>
          <tr>
            <th>Time (IST)</th>
            <th>Account</th>
            <th>Broker</th>
            <th>Event</th>
            <th>Detail</th>
          </tr>
        </thead>
        <tbody>
          {#each connEvents as ev (ev.id ?? ev.event_ts + ev.account + ev.event_type)}
            <tr>
              <td class="conn-td-time">{_fmtConnTime(ev.event_ts)}</td>
              <td class="font-mono">{ev.account}</td>
              <td>{ev.broker_id || '—'}</td>
              <td class="font-mono {_connEventCls(ev.event_type)}">{ev.event_type}</td>
              <td class="conn-td-detail" title={ev.detail ? JSON.stringify(ev.detail, null, 2) : ''}>{_fmtConnDetail(ev.detail)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </div>
</div>

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
  /* Canonical .algo-card-title palette + typography — operator: "header
     text color is not consistent. GREEKS is good." Was slate-400 which
     read as muted next to the amber Snapshot / Order Entry headings on
     the same nav level. */
  .brokers-h {
    font-size: var(--fs-md);
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--c-action);
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    margin: 0;
  }
  .section-heading { font-size: var(--fs-sm, 0.6rem); font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; color: var(--c-action, #fbbf24); padding-bottom: 0.3rem; margin-bottom: 0.5rem; border-bottom: 1px solid rgba(255,255,255,0.10); }

  /* Horizontal scroll wrapper — narrow viewports otherwise push the
     status pill (and the action buttons) out past the card edge.
     Chrome delegated to .algo-grid-chrome class on the element. */
  .brokers-scroll {
    width: 100%;
    overflow-x: auto;
  }
  .brokers-table {
    width: 100%;
    min-width: 720px;
    table-layout: auto;
  }
  .brokers-table td:nth-child(5),
  .brokers-table td:nth-child(6) { width: 1%; white-space: nowrap; }   /* status, historical */
  .brokers-table td:nth-child(7) { width: 1%; }                         /* poll priority */
  .brokers-table td:nth-child(9),
  .brokers-table td:nth-child(10) { width: 1%; white-space: nowrap; }  /* test, actions */
  .brokers-table th {
    text-align: left;
    color: var(--algo-muted);
    font-weight: 700;
    padding: 0.25rem 0.4rem;
    border-bottom: 1px solid rgba(255,255,255,0.06);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .brokers-table td {
    padding: 0.3rem 0.4rem;
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
  .status-loaded   { color: var(--c-long); background: var(--c-long-10); }
  .status-pending  { color: var(--c-action); background: rgba(251,191,36,0.10); }
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
  .brokers-hist-pill.hist-on  { background: rgba(34,211,238,0.18); border: 1px solid rgba(103,232,249,0.55); color: var(--c-info); }
  .brokers-hist-pill.hist-off { background: rgba(126,151,184,0.10); border: 1px solid rgba(126,151,184,0.30); color: var(--algo-muted); }

  .brokers-form-toggle {
    padding: 0.25rem 0.6rem;
    background: rgba(34,211,238,0.10);
    border: 1px solid rgba(34,211,238,0.45);
    border-radius: 3px;
    color: var(--c-info);
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
  .test-result.ok   { color: var(--c-long); }
  .test-result.fail { color: var(--c-short); }

  :global(.brokers-table .destructive) {
    border-color: rgba(248,113,113,0.45) !important;
    color: var(--c-short) !important;
  }
  :global(.brokers-table .destructive:hover:not(:disabled)) {
    background: var(--c-short-10) !important;
  }

  /* ── Poll priority chip + dropdown ─────────────────────────────── */
  .priority-cell { vertical-align: middle; }
  .priority-wrap { position: relative; display: inline-flex; flex-direction: column; gap: 0.15rem; }
  .priority-chip-row { display: inline-flex; align-items: center; gap: 0.3rem; }
  .priority-chip {
    position: relative;
    display: inline-flex;
    align-items: center;
    gap: 0.2rem;
    padding: 1px 6px;
    border-radius: 3px;
    border: 1px solid;
    font-family: monospace;
    font-size: var(--fs-xs);
    font-weight: 800;
    letter-spacing: 0.06em;
    cursor: pointer;
    /* 400ms bg-color + border-color transition per spec. */
    transition: background-color 400ms ease, border-color 400ms ease;
    white-space: nowrap;
  }
  @media (prefers-reduced-motion: reduce) {
    .priority-chip { transition: none; }
  }
  .priority-chip:hover { filter: brightness(1.15); }
  /* Red dot — 4px, top-right corner, no pulse */
  .circuit-dot {
    position: absolute;
    top: -2px;
    right: -2px;
    width: 4px;
    height: 4px;
    border-radius: 50%;
    background: var(--c-short);
    pointer-events: none;
  }
  .auto-dg-label {
    display: inline-flex;
    align-items: center;
    gap: 0.15rem;
    cursor: pointer;
    white-space: nowrap;
  }
  .auto-dg-text { font-size: var(--fs-2xs); color: var(--algo-muted); font-family: monospace; }
  .auto-dg-annotation {
    font-size: var(--fs-2xs);
    color: var(--algo-muted);
    white-space: nowrap;
    display: flex;
    align-items: center;
    gap: 0.25rem;
  }
  .restore-link {
    background: none;
    border: none;
    color: var(--c-info);
    font-size: var(--fs-2xs);
    font-family: monospace;
    cursor: pointer;
    padding: 0;
    text-decoration: underline;
  }
  .restore-link:hover { color: #67e8f9; }
  .priority-dropdown {
    position: absolute;
    top: calc(100% + 2px);
    left: 0;
    z-index: 20;
    background: #1d2a44;
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 4px;
    box-shadow: 0 4px 12px rgba(0,0,0,0.5);
    display: flex;
    flex-direction: column;
    min-width: 80px;
  }
  .priority-dropdown-item {
    padding: 0.25rem 0.6rem;
    font-family: monospace;
    font-size: var(--fs-xs);
    font-weight: 800;
    letter-spacing: 0.06em;
    text-align: left;
    cursor: pointer;
    background: none;
    border: none;
    border-bottom: 1px solid rgba(255,255,255,0.06);
  }
  .priority-dropdown-item:last-child { border-bottom: none; }
  .priority-dropdown-item:hover { background: rgba(255,255,255,0.07); }
  .priority-na { color: var(--algo-muted); font-size: var(--fs-xs); }

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

  /* ── Connection Log ─────────────────────────────────────────────── */
  .conn-filter-bar {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem 0.6rem;
    margin-bottom: 0.5rem;
  }
  .conn-filter-field {
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
    min-width: 0;
  }
  .conn-select,
  .conn-date {
    font-family: monospace;
    font-size: var(--fs-sm);
    padding: 0.15rem 0.35rem;
    background: rgba(15, 23, 42, 0.6);
    border: 1px solid rgba(255, 255, 255, 0.10);
    border-radius: 3px;
    color: var(--algo-slate);
    cursor: pointer;
    outline: none;
    height: 1.6rem;
  }
  .conn-select:focus,
  .conn-date:focus { border-color: rgba(251, 191, 36, 0.45); }

  .conn-table {
    width: 100%;
    min-width: 600px;
    table-layout: auto;
  }
  .conn-table th {
    text-align: left;
    color: var(--algo-muted);
    font-weight: 700;
    padding: 0.2rem 0.4rem;
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .conn-table td {
    padding: 0.25rem 0.4rem;
    vertical-align: top;
  }

  .conn-td-time {
    white-space: nowrap;
    color: var(--text-lo);
    font-size: var(--fs-xs);
  }
  .conn-td-detail {
    max-width: 320px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--algo-muted);
    font-size: var(--fs-xs);
  }

  /* Event type color coding */
  .conn-ev-green { color: var(--c-long); }
  .conn-ev-red   { color: var(--c-short); }
  .conn-ev-amber { color: var(--c-action); }
  .conn-ev-muted { color: var(--algo-muted); }

  .conn-empty {
    padding: 1rem;
    text-align: center;
    font-size: var(--fs-sm);
    color: var(--algo-muted);
    font-family: monospace;
  }

  @media (max-width: 600px) {
    .conn-filter-bar { flex-direction: column; }
    .conn-select, .conn-date { width: 100%; }
  }
</style>
