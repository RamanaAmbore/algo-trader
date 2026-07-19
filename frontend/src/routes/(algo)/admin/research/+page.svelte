<script>
  // Research workspace — chat-driven stock research via Claude Code + MCP.
  //
  // The actual chat happens IN Claude Code (your terminal), not in this
  // page. The MCP server (backend/mcp/kite_server.py) writes its session
  // back to /api/research/threads as the operator works; the page is the
  // read/review layer + the operator's bootstrap surface (Settings tab
  // generates the .mcp.json snippet and a fresh JWT to paste into env).
  //
  // Three tabs:
  //   Research — list of saved threads + selected-thread transcript viewer
  //   Drafts   — agents created from a research thesis (status=inactive)
  //   Settings — .mcp.json template + JWT helper + tool inventory
  //
  // No GenAI is invoked from this page. The Lab is a thin shell over the
  // MCP pipeline — Claude Code (subscription) is the only LLM in the loop.

  import { onMount, onDestroy } from 'svelte';
  import { authStore, nowStamp, lastRefreshAt, formatDualTz, branchLabel, visibleInterval } from '$lib/stores';
  import { userRole, userCaps, userCapsReady, hasCap } from '$lib/rbac';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import {
    fetchResearchThreads, fetchResearchThread,
    deleteResearchThread, fetchResearchDrafts,
    mintConfirmToken, fetchResearchAudit,
  } from '$lib/api';
  import { toast } from '$lib/data/toastStore.svelte.js';
  import InfoHint from '$lib/InfoHint.svelte';
  import Select from '$lib/Select.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import AlgoTabs from '$lib/AlgoTabs.svelte';
  import AutomationTabs from '$lib/AutomationTabs.svelte';
  import ConfirmModal from '$lib/ConfirmModal.svelte';
  import LoadingSkeleton from '$lib/LoadingSkeleton.svelte';
  import EmptyState from '$lib/EmptyState.svelte';

  /** @type {any[]} */
  let threads     = $state([]);
  /** @type {any|null} */
  let selected    = $state(null);
  let error       = $state('');
  let _showLiveTs = $state(false);
  let loading     = $state(true);
  let teardown;
  let activeTab   = $state(/** @type {'research'|'drafts'|'audit'|'settings'} */ ('research'));

  /** ConfirmModal binding — used for destructive actions (delete thread). */
  /** @type {{ ask: (opts: any) => Promise<boolean> } | null} */
  let confirmRef  = $state(null);

  /** Joined-view rows from GET /api/research/drafts — one per
   *  research thread with a linked inactive Agent. Activating the
   *  agent on /automation naturally graduates it out of this list. */
  /** @type {any[]} */
  let drafts = $state([]);

  /** Phase 3b — forensic trail of every MCP-initiated mutation
   *  (currently just place_order). Tool + status filters drive the
   *  query string; the rail above the table lets the operator slice
   *  the view ("show every denied call", "every place_order today"). */
  /** @type {any[]} */
  let audit = $state([]);
  let auditFilterTool   = $state('');
  let auditFilterStatus = $state('');
  /** Time-window filter for the Audit tab. Values are option keys
   *  ('1h' / 'today' / '7d' / ''); the `_sinceIso` derivation turns
   *  them into ISO timestamps the backend understands. Empty = no
   *  since filter (all-time view, default). */
  let auditFilterSince  = $state('');
  /** Phase 18 — request_id exact-match filter. Set by the
   *  Telegram-deep-link handler (?audit_request=<id>) so the operator
   *  lands on the Audit tab showing only the row they tapped. */
  let auditFilterRequestId = $state('');

  async function loadThreads() {
    try {
      threads     = await fetchResearchThreads();
      error       = '';
    } catch (e) {
      error = e.message;
    } finally {
      loading = false;
    }
  }

  async function loadDrafts() {
    try {
      const rows = await fetchResearchDrafts();
      if (Array.isArray(rows)) drafts = rows;
    } catch (_) { /* keep last-good drafts on transient failure */ }
  }

  /** Resolve the operator's "Since" pick to an ISO timestamp.
   *  Computed on each loadAudit() call so the window walks forward
   *  with wall-clock time (a "Last hour" filter at 10:00 covers
   *  09:00→10:00; the same filter triggered at 10:30 covers
   *  09:30→10:30). */
  function _sinceIso() {
    const now = new Date();
    if (auditFilterSince === '1h') {
      return new Date(now.getTime() - 3600 * 1000).toISOString();
    }
    if (auditFilterSince === 'today') {
      const d = new Date(now);
      d.setHours(0, 0, 0, 0);
      return d.toISOString();
    }
    if (auditFilterSince === '7d') {
      return new Date(now.getTime() - 7 * 86400 * 1000).toISOString();
    }
    return undefined;     // 'all time'
  }

  let auditLoading = $state(false);
  async function loadAudit() {
    auditLoading = true;
    try {
      const rows = await fetchResearchAudit({
        tool:       auditFilterTool       || undefined,
        status:     auditFilterStatus     || undefined,
        request_id: auditFilterRequestId  || undefined,
        since:      _sinceIso(),
        limit:      200,
      });
      if (Array.isArray(rows)) audit = rows;
    } catch (_) { /* keep last-good audit rows; banner not yet wired */ }
    finally { auditLoading = false; }
  }
  // Re-fetch when filters change; only fires when the Audit tab is in view
  // (the panel mounts conditionally so the $effect dependency is naturally gated).
  $effect(() => {
    if (activeTab === 'audit') {
      void auditFilterTool; void auditFilterStatus;
      void auditFilterSince; void auditFilterRequestId;
      loadAudit();
    }
  });

  async function selectThread(/** @type {number} */ id) {
    try {
      selected = await fetchResearchThread(id);
    } catch (e) {
      toast.error(`Failed to load thread: ${e.message}`);
    }
  }

  async function removeThread(/** @type {number} */ id) {
    const ok = await confirmRef?.ask({
      title:        'Delete research thread?',
      message:      'This removes the transcript and thesis permanently.',
      danger:       true,
      confirmLabel: 'Delete',
      cancelLabel:  'Cancel',
    });
    if (!ok) return;
    try {
      await deleteResearchThread(id);
      if (selected?.id === id) selected = null;
      toast.success('Thread deleted');
      await loadThreads();
    } catch (e) {
      toast.error(`Delete failed: ${e.message}`);
    }
  }

  // Canonical $effect-gated auth (slice N4). view_lab admits
  // admin + trader + risk + demo.
  // Bridge legacy stores into Svelte-5 $state so $derived doesn't
  // stale-cache the initial [] / 'partner' boot values — without
  // this the access-denied EmptyState rendered on first paint for
  // legitimately-authorised operators (designated, admin, etc).
  let _caps = $state(/** @type {string[]} */ ([]));
  let _role = $state(/** @type {string} */ ('partner'));
  $effect(() => { _caps = $userCaps; });
  $effect(() => { _role = $userRole; });
  const _canView = $derived(hasCap('view_lab', _caps, _role));
  let _loadedOnce = false;
  $effect(() => {
    if (_canView && !_loadedOnce) {
      _loadedOnce = true;
      loadThreads();
      loadDrafts();
      teardown = visibleInterval(() => { loadThreads(); loadDrafts(); }, 30000);
    }
  });

  onMount(() => {
    // Phase 18 — Telegram deep-link handler. When the page is loaded
    // via /admin/research?audit_request=<id> (the link in every
    // request_id Telegram ping), jump straight to the Audit tab
    // pre-filtered to that exact row. The operator on their phone
    // gets a one-tap forensic drill-down. Runs regardless of
    // _canView so the URL-param pre-fill is ready when access
    // hydrates.
    try {
      const url = new URL(window.location.href);
      const rid = url.searchParams.get('audit_request');
      if (rid) {
        auditFilterRequestId = rid.trim();
        activeTab = 'audit';
      }
    } catch (_) { /* SSR / window unavailable */ }
  });
  onDestroy(() => teardown?.());

  // ── Helpers ────────────────────────────────────────────────────────
  function _confColor(/** @type {string} */ c) {
    if (c === 'bull')    return 'pill-bull';
    if (c === 'bear')    return 'pill-bear';
    if (c === 'neutral') return 'pill-neutral';
    return 'pill-unsure';
  }
  function _fmtDate(/** @type {string} */ iso) {
    if (!iso) return '—';
    try {
      const d = new Date(iso);
      return d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short' })
        + ' ' + d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: false });
    } catch (_) { return iso; }
  }
  function _roleLabel(/** @type {string} */ role) {
    if (role === 'user')       return 'You';
    if (role === 'assistant')  return 'Claude';
    if (role === 'tool')       return 'Tool';
    return role || '—';
  }

  // ── Settings tab — .mcp.json + JWT bootstrap ───────────────────────
  const branch = $derived($authStore.user ? ($authStore.user.branch || '') : '');
  const baseUrl = $derived(
    (typeof window !== 'undefined' && window.location?.origin) || 'https://dev.ramboq.com'
  );

  /** Live session JWT — the operator is already authenticated on this
   *  page (admin_guard), so their token is in sessionStorage. Surface
   *  it so the JWT bootstrap is a one-click copy instead of the
   *  curl + jq + password-re-entry dance.
   *  Read on mount (sessionStorage is browser-only). */
  let _sessionToken = $state(/** @type {string} */ (''));
  onMount(() => {
    try {
      _sessionToken = sessionStorage.getItem('ramboq_token') || '';
    } catch (_) { /* SSR or storage blocked */ }
  });

  /** Shell-quoted export line ready to paste into a terminal. Wrapped
   *  in single quotes so any base64 padding (=) survives Zsh's history
   *  expansion. Empty when sessionStorage has nothing yet. */
  const exportLine = $derived(
    _sessionToken
      ? `export RAMBOQ_TOKEN='${_sessionToken}'`
      : ''
  );
  const mcpJson = $derived(JSON.stringify({
    mcpServers: {
      'ramboq-research': {
        command: 'venv/bin/python',
        args: ['-m', 'backend.mcp.kite_server'],
        env: {
          RAMBOQ_BASE:  baseUrl,
          RAMBOQ_TOKEN: '${RAMBOQ_TOKEN}',
        },
      },
    },
  }, null, 2));

  async function copy(/** @type {string} */ s, /** @type {string} */ label) {
    try {
      await navigator.clipboard.writeText(s);
      toast.success(`${label} copied`, { timeoutMs: 1500 });
    } catch (_) {
      toast.error('Copy failed — clipboard not available');
    }
  }

  // ── Phase-3 confirm-token mint widget (Settings tab) ───────────────
  // Operator types order details + mints a single-use 60s token.
  // Token is then pasted into Claude Code so the LLM's place_order
  // call carries the matching authorisation.
  let mintForm = $state({
    kind: /** @type {'place'|'cancel'|'modify'|'activate'|'deactivate'|'update'} */ ('place'),
    account: '', tradingsymbol: '', side: 'SELL', quantity: 1,
    mode: 'paper', order_type: 'LIMIT', price: null, trigger_price: null,
    order_id: '',
    agent_slug: '',
    // Phase 14 — JSON blob the operator pastes for kind='update'.
    // Server hashes the canonical JSON into the purpose hash so the
    // LLM must replay byte-identical proposed_changes.
    proposed_changes_json: '',
  });
  /** @type {any} */
  let mintedToken = $state(null);    // {token, expires_in, purpose, ...}
  let mintError   = $state('');
  let mintSecondsLeft = $state(0);
  /** @type {(() => void) | null} */
  let mintTicker = null;
  async function mint() {
    mintError = '';
    try {
      // The server takes the same shape for all kinds; irrelevant
      // fields are simply ignored for the chosen kind.
      // Typed `any` because update kind adds a `proposed_changes`
      // field that isn't on the source mintForm shape (the source has
      // proposed_changes_json which we strip just before send).
      /** @type {any} */
      const payload = {
        ...mintForm,
        quantity: Number(mintForm.quantity) || 0,
        price:         mintForm.price         === null || mintForm.price         === '' ? null : Number(mintForm.price),
        trigger_price: mintForm.trigger_price === null || mintForm.trigger_price === '' ? null : Number(mintForm.trigger_price),
      };
      // Phase 14 — parse the proposed-changes JSON when kind=update.
      // Empty JSON is intentional (operator may want to mint with no
      // changes for a probe); server returns 400 in that case.
      if (mintForm.kind === 'update') {
        const raw = (mintForm.proposed_changes_json || '').trim();
        if (raw) {
          try {
            payload.proposed_changes = JSON.parse(raw);
          } catch (e) {
            mintError = `Proposed changes is not valid JSON: ${e.message}`;
            mintedToken = null;
            return;
          }
        } else {
          payload.proposed_changes = {};
        }
      }
      // Strip the textarea string before sending so it doesn't end up
      // in the audit-redacted args.
      delete payload.proposed_changes_json;
      const res = await mintConfirmToken(payload);
      mintedToken = res;
      mintSecondsLeft = res.expires_in;
      if (mintTicker) mintTicker();
      mintTicker = visibleInterval(() => {
        mintSecondsLeft = Math.max(0, mintSecondsLeft - 1);
        if (mintSecondsLeft === 0) {
          if (mintTicker) { mintTicker(); mintTicker = null; }
        }
      }, 1000);
    } catch (e) {
      mintError = e.message;
      mintedToken = null;
    }
  }
  onDestroy(() => { if (mintTicker) mintTicker(); });

  // Option arrays for the custom Select dropdowns (replaces native
  // <select> so the dropdowns inherit the algo terminal's navy + amber
  // palette instead of the OS-native styling).
  const KIND_OPTIONS = [
    { value: 'place',      label: 'PLACE' },
    { value: 'cancel',     label: 'CANCEL' },
    { value: 'modify',     label: 'MODIFY' },
    { value: 'activate',   label: 'ACTIVATE agent' },
    { value: 'deactivate', label: 'DEACTIVATE agent' },
    { value: 'update',     label: 'UPDATE agent' },
  ];
  const SIDE_OPTIONS = [
    { value: 'BUY',  label: 'BUY' },
    { value: 'SELL', label: 'SELL' },
  ];
  const MODE_OPTIONS = [
    { value: 'paper', label: 'PAPER' },
    { value: 'live',  label: 'LIVE' },
  ];
  const MODE_OPTIONS_LIVE_DEFAULT = [
    { value: 'live',  label: 'LIVE (broker)' },
    { value: 'paper', label: 'PAPER (engine)' },
  ];
  const ORDER_TYPE_OPTIONS = [
    { value: 'LIMIT',  label: 'LIMIT' },
    { value: 'MARKET', label: 'MARKET' },
    { value: 'SL',     label: 'SL' },
    { value: 'SL-M',   label: 'SL-M' },
  ];
  const AUDIT_TOOL_OPTIONS = [
    { value: '',                 label: 'All tools' },
    { value: 'place_order',      label: 'place_order' },
    { value: 'cancel_order',     label: 'cancel_order' },
    { value: 'modify_order',     label: 'modify_order' },
    { value: 'activate_agent',   label: 'activate_agent' },
    { value: 'deactivate_agent', label: 'deactivate_agent' },
    { value: 'update_agent',     label: 'update_agent' },
  ];
  const AUDIT_STATUS_OPTIONS = [
    { value: '',       label: 'All' },
    { value: 'ok',     label: 'ok' },
    { value: 'denied', label: 'denied' },
    { value: 'error',  label: 'error' },
  ];
  const AUDIT_SINCE_OPTIONS = [
    { value: '',      label: 'All time' },
    { value: '1h',    label: 'Last hour' },
    { value: 'today', label: 'Today' },
    { value: '7d',    label: 'Last 7 days' },
  ];

  // Phase-1 tool inventory — mirrors backend/mcp/kite_server.py
  const TOOLS = [
    { name: 'get_positions',         summary: 'Current intraday positions (optionally filtered by account)' },
    { name: 'get_holdings',          summary: 'Long-term holdings (optionally filtered by account)' },
    { name: 'get_quote',             summary: 'Live LTP / OHLC / change% for up to 300 symbols' },
    { name: 'get_ohlcv',             summary: 'Historical daily candles (up to 365 days)' },
    { name: 'get_recent_news',       summary: 'Recent Indian-market headlines (optional title filter)' },
    { name: 'get_option_analytics',  summary: 'Greeks + payoff + risk for one option leg' },
    { name: 'get_options_chain_snapshot', summary: 'Bulk chain — LTP + Greeks for ATM ± N strikes in one call' },
    { name: 'get_economic_snapshot', summary: 'India macros — repo rate, CPI, IIP, GDP, USD/INR' },
    { name: 'get_funds_summary',     summary: 'Cash + available/used margin per account (size legs against this)' },
    { name: 'get_watchlist',         summary: 'Symbols in a named watchlist (scope research to a curated set)' },
    { name: 'get_pnl_attribution',   summary: 'P&L grouped by agent — which rules are making money this period' },
    { name: 'list_agents',           summary: 'List existing agents (optionally by status)' },
    { name: 'save_research_thread',  summary: 'Persist a thesis + transcript to the Lab page' },
    { name: 'save_agent_draft',      summary: 'Promote a thread to an inactive draft Agent (paper-mode)' },
    { name: 'place_order',           summary: 'Gated order placement — requires operator-minted confirm token' },
    { name: 'cancel_order',          summary: 'Gated cancel — requires confirm token bound to (account, order_id)' },
    { name: 'modify_order',          summary: 'Gated modify — token binds to new qty/price/trigger as well' },
    { name: 'activate_agent',        summary: 'Gated activate — flips agent to status=active (highest-stakes write)' },
    { name: 'deactivate_agent',      summary: 'Gated deactivate — flips agent back to inactive' },
    { name: 'update_agent',          summary: 'Gated edit — change conditions/cooldown/events/etc. on an existing agent' },
    { name: 'get_audit_recent',      summary: 'Reverse-chrono trail of your MCP actions — self-check after writes' },
    { name: 'get_research_thread',   summary: 'Fetch a saved thread by id' },
    { name: 'list_research_threads', summary: 'List recent threads (optional symbol filter)' },
    { name: 'get_server_info',       summary: 'Diagnostic — base URL + token presence' },
  ];
</script>

<svelte:head><title>Lab | RamboQuant Analytics</title></svelte:head>

<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">Lab</h1>
  </span>
  <span class="algo-ts-group" onclick={() => { if ($lastRefreshAt) _showLiveTs = !_showLiveTs; }} onkeydown={(e) => { if ($lastRefreshAt && (e.key === "Enter" || e.key === " ")) _showLiveTs = !_showLiveTs; }} role="button" tabindex="0">
    <span class="algo-ts"
          class:algo-ts-hidden={!!$lastRefreshAt && _showLiveTs}
          title={$lastRefreshAt ? 'Live clock — tap to switch' : 'Live clock'}>
      {$nowStamp}
    </span>
    {#if $lastRefreshAt}
      <span class="algo-ts-vsep" aria-hidden="true">|</span>
      <span class="algo-ts algo-ts-data" class:algo-ts-hidden={!_showLiveTs}>
        {formatDualTz($lastRefreshAt)}
      </span>
    {/if}
  </span>
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <RefreshButton onClick={() => { loadThreads(); loadDrafts(); if (activeTab === 'audit') loadAudit(); }} loading={loading} label="research" />
    <PageHeaderActions symbol={selected?.symbol ?? ''} />
  </span>
</div>

{#if !$userCapsReady}
  <!-- /whoami still in flight — show skeleton, NOT access-denied.
       The bootstrap window is ~50-300ms in practice; without this
       guard a slow whoami flashes the EmptyState lock panel before
       caps land, terrifying legitimately-authorised operators. -->
  <LoadingSkeleton variant="card" rows={3} />
{:else if !_canView}
  <EmptyState title="Access denied" icon="lock">
    {#snippet hintBody()}
      The research lab requires the <code>view_lab</code> capability
      (admin, trader, or risk role). Your current role is
      <strong>{$userRole}</strong> — contact an admin to request access.
    {/snippet}
  </EmptyState>
{:else}

<AutomationTabs />

<div class="lab-tabs-wrap">
  <AlgoTabs
    tabs={[
      { id: 'research', label: 'Research', badge: threads.length || undefined },
      { id: 'drafts',   label: 'Drafts',   badge: drafts.length  || undefined },
      { id: 'audit',    label: 'Audit' },
      { id: 'settings', label: 'Settings' },
    ]}
    bind:value={activeTab}
  />
</div>

{#if activeTab === 'research'}
  <div class="lab-split">
    <!-- ── Thread list rail ────────────────────────────────────────── -->
    <aside class="lab-rail">
      <div class="lab-rail-head">
        <span class="rail-head-label">THREADS</span>
        <InfoHint popup text="Click any row to view its full transcript + thesis. Threads are created by the <code>save_research_thread</code> MCP tool — chat with Claude Code, ask it to save the thesis, and the row appears here." />
      </div>
      {#if loading && threads.length === 0}
        <div class="rail-empty"><LoadingSkeleton variant="grid-row" rows={5} height="0.75rem" /></div>
      {:else if threads.length === 0}
        <div class="rail-empty">
          <EmptyState
            title="No threads yet"
            hint={'Ask Claude Code: "Research RELIANCE and save the thesis."'}
            icon="inbox"
            action={{ label: 'Open Settings →', onClick: () => { activeTab = 'settings'; } }}
          />
        </div>
      {:else}
        <ul class="rail-list">
          {#each threads as t (t.id)}
            <li>
              <button type="button"
                      class="rail-row"
                      class:rail-row-on={selected?.id === t.id}
                      onclick={() => selectThread(t.id)}>
                <span class="rail-sym">{t.symbol}</span>
                <span class="rail-conf pill {_confColor(t.confidence)}">{t.confidence}</span>
                <span class="rail-title">{t.title || '—'}</span>
                <span class="rail-meta">{_fmtDate(t.updated_at)} · {t.transcript_len} msg</span>
              </button>
            </li>
          {/each}
        </ul>
      {/if}
    </aside>

    <!-- ── Selected-thread viewer ──────────────────────────────────── -->
    <section class="lab-main">
      {#if !selected}
        <EmptyState
          title="No thread selected"
          hint="Pick a thread from the left rail, or start a new one by chatting with Claude Code."
          icon="search"
        />
      {:else}
        <header class="thr-head">
          <div class="thr-head-l">
            <span class="thr-sym">{selected.symbol}</span>
            <span class="pill {_confColor(selected.confidence)}">{selected.confidence}</span>
            <span class="thr-title">{selected.title}</span>
          </div>
          <div class="thr-head-r">
            <span class="thr-time">{_fmtDate(selected.updated_at)}</span>
            <button class="thr-del" type="button"
                    onclick={() => removeThread(selected.id)}
                    aria-label="Delete thread">✕</button>
          </div>
        </header>

        {#if selected.thesis_text}
          <article class="thr-thesis">
            <div class="thr-section-label">THESIS</div>
            <pre class="thr-thesis-body">{selected.thesis_text}</pre>
          </article>
        {/if}

        {#if Array.isArray(selected.transcript) && selected.transcript.length > 0}
          <article class="thr-transcript">
            <div class="thr-section-label">TRANSCRIPT</div>
            {#each selected.transcript as msg, i (i)}
              <div class="msg msg-{msg.role || 'other'}">
                <div class="msg-role">{_roleLabel(msg.role)}</div>
                <div class="msg-body">{msg.content || ''}</div>
              </div>
            {/each}
          </article>
        {/if}
      {/if}
    </section>
  </div>
{:else if activeTab === 'drafts'}
  <div class="lab-drafts">
    <p class="lab-drafts-hint">
      Draft agents promoted from a research thread via the
      <code>save_agent_draft</code> MCP tool. Every draft ships
      <b>status=inactive</b> + <b>trade_mode=paper</b> — operator's
      next step is "Run in Simulator" on /automation. Activating a draft
      graduates it out of this list.
    </p>
    {#if drafts.length === 0}
      <EmptyState
        title="No draft agents yet"
        hint={'In Claude Code, after research: "Build me an agent that fires if X, save it as a draft."'}
        icon="inbox"
        action={{ label: 'Pick a thread →', onClick: () => { activeTab = 'research'; } }}
      />
    {:else}
      <table class="drafts-table">
        <thead>
          <tr>
            <th>Source</th>
            <th>Agent</th>
            <th>Scope</th>
            <th>Schedule</th>
            <th>Cooldown</th>
            <th>Mode</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {#each drafts as d (d.agent_id)}
            <tr>
              <td>
                <button type="button" class="drafts-thr"
                        onclick={() => { activeTab = 'research'; selectThread(d.thread_id); }}>
                  <span class="drafts-sym">{d.symbol}</span>
                  <span class="pill {_confColor(d.confidence)}">{d.confidence}</span>
                  <span class="drafts-thr-title">{d.title || '—'}</span>
                </button>
              </td>
              <td>
                <span class="drafts-name">{d.agent_name}</span>
                <span class="drafts-slug">{d.agent_slug}</span>
              </td>
              <td>{d.agent_scope || '—'}</td>
              <td>{d.agent_schedule || '—'}</td>
              <td>{d.agent_cooldown ?? '—'} min</td>
              <td><span class="mode-pill">{(d.agent_trade_mode || 'paper').toUpperCase()}</span></td>
              <td><a class="drafts-edit" href={`/automation`}>Open ›</a></td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </div>
{:else if activeTab === 'audit'}
  <!-- ── Audit tab — Phase 3b ────────────────────────────────────── -->
  <div class="lab-audit">
    {#if auditFilterRequestId}
      <div class="audit-deeplink-banner">
        <span>
          Showing audit row for request_id <code>{auditFilterRequestId}</code>
          (deep-link from Telegram).
        </span>
        <button type="button" class="audit-clear-link" onclick={() => auditFilterRequestId = ''}>
          Clear filter
        </button>
      </div>
    {/if}
    <header class="audit-head">
      <p class="audit-hint">
        Forensic trail of every MCP-initiated mutation. Token material is
        never persisted — args show what the LLM asked to do, not what
        authorised it. Use this to confirm post-hoc that every LLM
        action was either consciously authorised or correctly denied.
      </p>
      <div class="audit-filters">
        <label>
          <span>Since</span>
          <Select bind:value={auditFilterSince} options={AUDIT_SINCE_OPTIONS} ariaLabel="Audit time window" />
        </label>
        <label>
          <span>Tool</span>
          <Select bind:value={auditFilterTool} options={AUDIT_TOOL_OPTIONS} ariaLabel="Audit tool filter" />
        </label>
        <label>
          <span>Status</span>
          <Select bind:value={auditFilterStatus} options={AUDIT_STATUS_OPTIONS} ariaLabel="Audit status filter" />
        </label>
        <RefreshButton onClick={loadAudit} loading={auditLoading} label="audit" />
      </div>
    </header>
    {#if audit.length === 0}
      <EmptyState
        title="No audit rows"
        hint="The first MCP-initiated action (success or denied) will appear here."
        icon="inbox"
      />
    {:else}
      <table class="audit-table">
        <thead>
          <tr>
            <th>When</th>
            <th>Tool</th>
            <th>Status</th>
            <th>User</th>
            <th>Args</th>
            <th>Result</th>
            <th>Req</th>
          </tr>
        </thead>
        <tbody>
          {#each audit as a (a.id)}
            <tr>
              <td><span class="audit-when">{_fmtDate(a.created_at)}</span></td>
              <td><code class="audit-tool">{a.tool}</code></td>
              <td>
                <span class="audit-status audit-status-{a.result_status}">{a.result_status}</span>
              </td>
              <td>{a.user_id ?? '—'}</td>
              <td><code class="audit-args">{JSON.stringify(a.args_redacted)}</code></td>
              <td><span class="audit-summary">{a.result_summary}</span></td>
              <td><code class="audit-rid">{a.request_id ?? ''}</code></td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </div>
{:else}
  <!-- Settings tab — MCP bootstrap + confirm-token mint -->
  <div class="lab-settings">

    <!-- ── 0. Confirm-token mint — Phase 3 ────────────────────────── -->
    <article class="lab-card">
      <h2>0. Mint a confirm token (place_order gate)</h2>
      <p>
        The MCP write tools (<code>place_order</code> · <code>cancel_order</code>
        · <code>modify_order</code>) refuse every request without a valid
        <b>operator-minted token</b>. Pick the action, fill the form, click
        Mint, copy the token, paste into Claude Code. Token is single-use,
        expires in 60 seconds, and is bound to <i>this exact action</i> —
        the LLM can't swap the symbol / quantity / order id post-mint.
      </p>
      <div class="mint-grid">
        <label><span>Kind</span>
          <Select bind:value={mintForm.kind} options={KIND_OPTIONS} ariaLabel="Mint kind" />
        </label>
        {#if mintForm.kind === 'activate' || mintForm.kind === 'deactivate' || mintForm.kind === 'update'}
          <label><span>Agent slug</span><input bind:value={mintForm.agent_slug} placeholder="reliance-bull-21d" /></label>
        {:else}
          <label><span>Account</span><input bind:value={mintForm.account} placeholder="ZG0790" /></label>
        {/if}
        {#if mintForm.kind === 'place'}
          <label><span>Symbol</span><input bind:value={mintForm.tradingsymbol} placeholder="NIFTY25APRFUT" /></label>
          <label><span>Side</span>
            <Select bind:value={mintForm.side} options={SIDE_OPTIONS} ariaLabel="Order side" />
          </label>
          <label><span>Quantity</span><input type="number" bind:value={mintForm.quantity} min="1" /></label>
          <label><span>Mode</span>
            <Select bind:value={mintForm.mode} options={MODE_OPTIONS} ariaLabel="Execution mode" />
          </label>
          <label><span>Order type</span>
            <Select bind:value={mintForm.order_type} options={ORDER_TYPE_OPTIONS} ariaLabel="Order type" />
          </label>
          <label><span>Price</span><input type="number" step="0.05" bind:value={mintForm.price} placeholder="(LIMIT/SL)" /></label>
          <label><span>Trigger</span><input type="number" step="0.05" bind:value={mintForm.trigger_price} placeholder="(SL/SL-M)" /></label>
        {:else if mintForm.kind === 'cancel'}
          <label><span>Mode</span>
            <Select bind:value={mintForm.mode} options={MODE_OPTIONS_LIVE_DEFAULT} ariaLabel="Cancel mode" />
          </label>
          <label><span>Order ID</span>
            <input bind:value={mintForm.order_id}
                   placeholder={mintForm.mode === 'paper' ? 'AlgoOrder.id (e.g. 42)' : '251115000123456'} />
          </label>
        {:else if mintForm.kind === 'modify'}
          <label><span>Mode</span>
            <Select bind:value={mintForm.mode} options={MODE_OPTIONS_LIVE_DEFAULT} ariaLabel="Modify mode" />
          </label>
          <label><span>Order ID</span>
            <input bind:value={mintForm.order_id}
                   placeholder={mintForm.mode === 'paper' ? 'AlgoOrder.id (e.g. 42)' : '251115000123456'} />
          </label>
          <label><span>New qty</span><input type="number" bind:value={mintForm.quantity} min="0" placeholder="(unchanged)" /></label>
          <label><span>Order type</span>
            <Select bind:value={mintForm.order_type} options={ORDER_TYPE_OPTIONS} ariaLabel="New order type" />
          </label>
          <label><span>New price</span><input type="number" step="0.05" bind:value={mintForm.price} placeholder="(LIMIT/SL)" /></label>
          <label><span>New trigger</span><input type="number" step="0.05" bind:value={mintForm.trigger_price} placeholder="(SL/SL-M)" /></label>
        {:else if mintForm.kind === 'update'}
          <!-- update: agent_slug already taken in the kind-row above.
               Operator pastes the proposed-changes JSON the LLM gave
               them; the textarea spans the full grid width so it's
               actually readable. -->
          <label class="mint-update-full">
            <span>Proposed changes (JSON)</span>
            <textarea class="mint-update-textarea"
                      bind:value={mintForm.proposed_changes_json}
                      placeholder={`Paste the LLM's proposed-changes dict, e.g.\n{\n  "cooldown_minutes": 15,\n  "events": ["telegram"]\n}`}
                      rows="6"></textarea>
          </label>
        {/if}
        <!-- activate / deactivate: no extra fields beyond Kind +
             Agent slug, which already render above. -->
      </div>
      <button class="copy-btn mint-btn" type="button" onclick={mint}>Mint token</button>
      {#if mintError}
        <div class="mint-error">{mintError}</div>
      {/if}
      {#if mintedToken}
        <div class="mint-result" class:mint-expired={mintSecondsLeft === 0}>
          <div class="mint-purpose">{mintedToken?.purpose}</div>
          <div class="mint-token-row">
            <code class="mint-token">{mintedToken?.token}</code>
            <button class="copy-btn" type="button" onclick={() => copy(mintedToken?.token, 'token')}>Copy</button>
            <span class="mint-countdown">
              {#if mintSecondsLeft > 0}
                expires in {mintSecondsLeft}s
              {:else}
                EXPIRED — mint again
              {/if}
            </span>
          </div>
          <p class="mint-hint">
            Paste into Claude Code: <code>place_order with confirm_token=<i>&lt;paste&gt;</i> + the same order details</code>.
          </p>
        </div>
      {/if}
    </article>

    <article class="lab-card">
      <h2>1. Bootstrap your JWT</h2>
      <p>The MCP server authenticates against the RamboQuant API using your JWT. The fastest path is to reuse the one you're <i>already signed in with on this page</i>:</p>
      {#if exportLine}
        <pre class="code-block jwt-current">{exportLine}</pre>
        <div class="jwt-actions">
          <button class="copy-btn" type="button" onclick={() => copy(exportLine, 'export line')}>
            Copy export line
          </button>
          <span class="jwt-note">
            Paste into a shell, then launch Claude Code. JWT expires
            after 24 h — refresh this page + re-copy to extend.
          </span>
        </div>
      {:else}
        <div class="jwt-empty">
          No session token detected. Sign in again, refresh, then come back.
        </div>
      {/if}
      <p class="jwt-or">Or mint a fresh JWT non-interactively (handy for cron / CI):</p>
      <pre class="code-block">export RAMBOQ_TOKEN=$(curl -s -X POST {baseUrl}/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{`{`}"username":"&lt;your-username&gt;","password":"&lt;your-password&gt;"{`}`}' \
  | jq -r .access_token)</pre>
      <button class="copy-btn" type="button" onclick={() => copy(
        `export RAMBOQ_TOKEN=$(curl -s -X POST ${baseUrl}/api/auth/login -H 'Content-Type: application/json' -d '{"username":"<your-username>","password":"<your-password>"}' | jq -r .access_token)`,
        'Login command')}>Copy command</button>
    </article>

    <article class="lab-card">
      <h2>2. Register the MCP server</h2>
      <p>Copy this snippet into <code>.mcp.json</code> at the repo root. Claude Code reads it on startup and launches the server as a subprocess. The branch is currently <b>{branchLabel(branch) || '—'}</b>; URL above points at this same host.</p>
      <pre class="code-block">{mcpJson}</pre>
      <button class="copy-btn" type="button" onclick={() => copy(mcpJson, '.mcp.json')}>Copy JSON</button>
    </article>

    <article class="lab-card">
      <h2>3. Available tools (Phase 1)</h2>
      <p>The MCP server exposes these research tools. <code>save_agent_draft</code> is the only write — it creates an INACTIVE Agent in paper-mode (cannot activate or place orders). Phase 3 adds gated trade tools (place_order with per-call confirm token).</p>
      <table class="tools-table">
        <thead><tr><th>Tool</th><th>Purpose</th></tr></thead>
        <tbody>
          {#each TOOLS as t (t.name)}
            <tr><td><code>{t.name}</code></td><td>{t.summary}</td></tr>
          {/each}
        </tbody>
      </table>
    </article>

    <article class="lab-card">
      <h2>4. Safety</h2>
      <ul class="safety-list">
        <li>No order placement from MCP yet. The server cannot move money.</li>
        <li><code>save_agent_draft</code> creates agents that ship <b>status=inactive</b> + <b>trade_mode=paper</b>. The endpoint cannot create an active or live agent.</li>
        <li>Operator's next step on every draft: <b>Run in Simulator</b> on /automation to validate the condition tree before activating.</li>
        <li>The JWT inherits your admin role. Don't paste it into untrusted MCP servers.</li>
        <li>Industry convention (Composer / IBKR TraderGPT): every LLM-initiated order requires explicit human confirm. Phase 3 will match.</li>
      </ul>
    </article>

  </div>
{/if}

{/if}

<ConfirmModal bind:this={confirmRef} />

<style>
  .algo-ts-group { display: inline-flex; align-items: center; gap: 0.3rem; }
  .algo-ts-vsep  { color: rgba(255,255,255,0.25); font-size: var(--fs-md); }
  @media (max-width: 480px) { .algo-ts-hidden { display: none !important; } }
  /* .empty-state rules removed — access-denied panel migrated to
     EmptyState component (slice AE). */

  /* ── Tab strip ────────────────────────────────────────────────── */
  /* Tab buttons rendered by AlgoTabs via global .algo-tab in app.css. */
  .lab-tabs-wrap {
    display: flex;
    /* Bumped margin-top from 0.8rem → 1.4rem and added a faint top
       border so the page-internal AlgoTabs strip is visually distinct
       from the AutomationTabs workspace strip above it. Pre-fix the
       two strips stacked with zero separator and identical visual
       weight; operator saw two amber underline bars and couldn't tell
       which was workspace-nav vs page-internal. */
    margin: 1.4rem 0 0.4rem;
    padding-top: 0.5rem;
    border-top: 1px solid rgba(126, 151, 184, 0.08);
    border-bottom: 1px solid rgba(126, 151, 184, 0.18);
  }

  /* ── Research tab: two-column rail + main ─────────────────────── */
  .lab-split {
    display: grid;
    grid-template-columns: minmax(220px, 320px) 1fr;
    gap: 0.8rem;
    margin-top: 0.6rem;
  }
  @media (max-width: 720px) { .lab-split { grid-template-columns: 1fr; } }

  .lab-rail {
    background: rgba(15, 25, 45, 0.4);
    border: 1px solid rgba(126, 151, 184, 0.18);
    border-radius: 0.5rem;
    padding: 0.4rem 0.4rem 0.6rem;
    max-height: 70vh;
    overflow-y: auto;
  }
  .lab-rail-head {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.2rem 0.2rem 0.4rem;
    border-bottom: 1px solid rgba(126, 151, 184, 0.15);
    margin-bottom: 0.3rem;
  }
  .rail-head-label {
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    font-weight: 700;
    letter-spacing: 0.06em;
    color: var(--algo-muted);
  }
  .rail-empty {
    padding: 0.8rem 0.6rem;
    color: var(--algo-muted);
    font-size: var(--fs-lg);
    line-height: 1.5;
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }
  .empty-line-1 {
    color: var(--algo-slate);
    font-weight: 700;
  }
  .empty-line-2 { color: var(--algo-muted); }
  .empty-cta {
    align-self: flex-start;
    margin-top: 0.3rem;
    background: rgba(56, 189, 248, 0.10);
    border: 1px solid rgba(56, 189, 248, 0.35);
    color: #38bdf8;
    padding: 0.28rem 0.55rem;
    border-radius: 0.3rem;
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    font-weight: 700;
    cursor: pointer;
  }
  .empty-cta:hover { background: rgba(56, 189, 248, 0.18); }
  .rail-list { list-style: none; padding: 0; margin: 0; }
  .rail-row {
    display: grid;
    grid-template-columns: auto auto 1fr;
    grid-template-rows: auto auto;
    align-items: center;
    column-gap: 0.4rem;
    width: 100%;
    background: none;
    border: none;
    border-radius: 0.35rem;
    padding: 0.32rem 0.4rem;
    color: var(--algo-slate);
    text-align: left;
    cursor: pointer;
    transition: background 0.12s;
  }
  .rail-row:hover    { background: rgba(126, 151, 184, 0.08); }
  .rail-row-on       { background: rgba(251, 191, 36, 0.10); }
  .rail-sym {
    grid-column: 1; grid-row: 1;
    font-family: var(--font-numeric);
    font-weight: 700;
    font-size: var(--fs-lg);
    color: var(--c-action);
  }
  .rail-conf {
    grid-column: 2; grid-row: 1;
    font-size: var(--fs-xs);
  }
  .rail-title {
    grid-column: 3; grid-row: 1;
    font-size: var(--fs-md);
    color: var(--algo-slate);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .rail-meta {
    grid-column: 1 / -1; grid-row: 2;
    margin-top: 0.1rem;
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    color: rgba(126, 151, 184, 0.85);
  }

  /* ── Main pane ────────────────────────────────────────────────── */
  .lab-main {
    background: rgba(15, 25, 45, 0.4);
    border: 1px solid rgba(126, 151, 184, 0.18);
    border-radius: 0.5rem;
    padding: 0.8rem;
    min-height: 50vh;
  }
  .lab-empty {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 3rem 1rem;
    text-align: center;
    color: var(--algo-muted);
  }
  .lab-empty-title {
    font-family: var(--font-numeric);
    font-size: var(--fs-xl);
    font-weight: 700;
    color: var(--algo-slate);
    margin-bottom: 0.6rem;
  }
  .lab-empty-hint {
    font-size: var(--fs-lg);
    line-height: 1.5;
    max-width: 30rem;
  }

  /* ── Thread header ────────────────────────────────────────────── */
  .thr-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding-bottom: 0.5rem;
    border-bottom: 1px solid rgba(126, 151, 184, 0.18);
    margin-bottom: 0.7rem;
  }
  .thr-head-l { display: flex; align-items: center; gap: 0.5rem; }
  .thr-head-r { display: flex; align-items: center; gap: 0.6rem; }
  .thr-sym {
    font-family: var(--font-numeric);
    font-weight: 700;
    font-size: 1rem;
    color: var(--c-action);
  }
  .thr-title {
    color: var(--algo-slate);
    font-size: var(--fs-lg);
  }
  .thr-time {
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    color: var(--algo-muted);
  }
  .thr-del {
    background: none;
    border: 1px solid rgba(248, 113, 113, 0.32);
    color: var(--c-short);
    border-radius: 0.35rem;
    padding: 0.1rem 0.5rem;
    cursor: pointer;
    font-size: var(--fs-lg);
  }
  .thr-del:hover { background: rgba(248, 113, 113, 0.12); }
  .thr-section-label {
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.08em;
    color: var(--algo-muted);
    margin-bottom: 0.4rem;
  }
  .thr-thesis { margin-bottom: 1rem; }
  .thr-thesis-body {
    background: rgba(251, 191, 36, 0.05);
    border-left: 2px solid rgba(251, 191, 36, 0.4);
    padding: 0.6rem 0.7rem;
    color: var(--algo-slate);
    font-size: var(--fs-lg);
    line-height: 1.55;
    white-space: pre-wrap;
    font-family: -apple-system, system-ui, sans-serif;
    border-radius: 0 0.35rem 0.35rem 0;
  }
  .thr-transcript { display: flex; flex-direction: column; gap: 0.5rem; }
  .msg {
    border: 1px solid rgba(126, 151, 184, 0.15);
    border-radius: 0.4rem;
    padding: 0.4rem 0.55rem;
  }
  .msg-user      { background: rgba(56, 189, 248, 0.06);  border-color: rgba(56, 189, 248, 0.22); }
  .msg-assistant { background: rgba(251, 191, 36, 0.05);  border-color: var(--algo-amber-bg-strong); }
  .msg-tool      { background: rgba(126, 151, 184, 0.06); border-color: rgba(126, 151, 184, 0.22); }
  .msg-role {
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.06em;
    color: var(--algo-muted);
    margin-bottom: 0.2rem;
  }
  .msg-body {
    color: var(--algo-slate);
    font-size: var(--fs-lg);
    line-height: 1.5;
    white-space: pre-wrap;
  }

  /* ── Drafts tab ───────────────────────────────────────────────── */
  .lab-drafts {
    background: rgba(15, 25, 45, 0.4);
    border: 1px solid rgba(126, 151, 184, 0.18);
    border-radius: 0.5rem;
    padding: 0.8rem;
    margin-top: 0.6rem;
  }
  .lab-drafts-hint {
    color: var(--algo-muted);
    font-size: var(--fs-lg);
    line-height: 1.5;
    margin: 0 0 0.7rem;
  }
  .drafts-table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--fs-lg);
  }
  .drafts-table th {
    text-align: left;
    padding: 0.34rem 0.5rem;
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.06em;
    color: var(--algo-muted);
    border-bottom: 1px solid rgba(126, 151, 184, 0.18);
  }
  .drafts-table td {
    padding: 0.36rem 0.5rem;
    color: var(--algo-slate);
    border-bottom: 1px solid rgba(126, 151, 184, 0.08);
  }
  .drafts-name { display: block; color: var(--c-action); font-weight: 700; }
  .drafts-slug { display: block; font-family: var(--font-numeric); font-size: var(--fs-xs); color: var(--algo-muted); }
  .drafts-edit {
    color: #38bdf8;
    text-decoration: none;
    font-size: var(--fs-lg);
  }
  .drafts-edit:hover { text-decoration: underline; }
  .drafts-thr {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    background: none;
    border: none;
    padding: 0;
    color: inherit;
    cursor: pointer;
    text-align: left;
  }
  .drafts-thr:hover .drafts-thr-title { color: var(--c-action); }
  .drafts-sym {
    font-family: var(--font-numeric);
    font-weight: 700;
    font-size: var(--fs-lg);
    color: var(--c-action);
  }
  .drafts-thr-title {
    font-size: var(--fs-md);
    color: var(--algo-slate);
    transition: color 0.12s;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 16rem;
  }
  .mode-pill {
    display: inline-block;
    padding: 0.05rem 0.4rem;
    border-radius: 0.5rem;
    background: rgba(56, 189, 248, 0.15);
    border: 1px solid rgba(56, 189, 248, 0.35);
    color: #38bdf8;
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.05em;
  }

  /* ── Audit tab ───────────────────────────────────────────────── */
  .lab-audit {
    background: rgba(15, 25, 45, 0.4);
    border: 1px solid rgba(126, 151, 184, 0.18);
    border-radius: 0.5rem;
    padding: 0.8rem;
    margin-top: 0.6rem;
  }
  .audit-head {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 0.8rem;
    flex-wrap: wrap;
    margin-bottom: 0.7rem;
  }
  .audit-hint {
    flex: 1 1 320px;
    color: var(--algo-muted);
    font-size: var(--fs-lg);
    line-height: 1.5;
    margin: 0;
  }
  .audit-filters {
    display: flex;
    align-items: flex-end;
    gap: 0.6rem;
  }
  .audit-filters label {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }
  .audit-filters span {
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.06em;
    color: var(--algo-muted);
  }
  .audit-refresh { align-self: flex-end; }
  .audit-deeplink-banner {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.8rem;
    padding: 0.5rem 0.7rem;
    background: rgba(56, 189, 248, 0.10);
    border: 1px solid rgba(56, 189, 248, 0.35);
    border-radius: 0.35rem;
    color: var(--algo-slate);
    font-size: var(--fs-lg);
    line-height: 1.45;
    margin-bottom: 0.7rem;
  }
  .audit-deeplink-banner code {
    font-family: var(--font-numeric);
    background: rgba(0, 0, 0, 0.25);
    padding: 0.05rem 0.35rem;
    border-radius: 0.25rem;
    color: #38bdf8;
    font-size: var(--fs-md);
  }
  .audit-clear-link {
    background: none;
    border: 1px solid rgba(56, 189, 248, 0.45);
    color: #38bdf8;
    padding: 0.2rem 0.6rem;
    border-radius: 0.3rem;
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    font-weight: 700;
    cursor: pointer;
  }
  .audit-clear-link:hover { background: rgba(56, 189, 248, 0.12); }
  .audit-table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--fs-md);
  }
  .audit-table th {
    text-align: left;
    padding: 0.34rem 0.5rem;
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.06em;
    color: var(--algo-muted);
    border-bottom: 1px solid rgba(126, 151, 184, 0.18);
  }
  .audit-table td {
    padding: 0.34rem 0.5rem;
    color: var(--algo-slate);
    border-bottom: 1px solid rgba(126, 151, 184, 0.08);
    vertical-align: top;
  }
  .audit-when { font-family: var(--font-numeric); font-size: var(--fs-sm); color: var(--algo-muted); }
  .audit-tool { color: var(--c-action); font-size: var(--fs-md); font-weight: 700; }
  .audit-status {
    display: inline-block;
    padding: 0.05rem 0.4rem;
    border-radius: 0.5rem;
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    border: 1px solid transparent;
  }
  .audit-status-ok     { background: rgba(34, 197, 94, 0.15);  border-color: rgba(34, 197, 94, 0.4);  color: var(--c-long); }
  .audit-status-denied { background: rgba(248, 113, 113, 0.15); border-color: rgba(248, 113, 113, 0.4); color: var(--c-short); }
  .audit-status-error  { background: rgba(251, 191, 36, 0.15); border-color: rgba(251, 191, 36, 0.4); color: var(--c-action); }
  .audit-args, .audit-rid {
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    color: rgba(200, 216, 240, 0.8);
    word-break: break-all;
    max-width: 24rem;
    display: inline-block;
  }
  .audit-summary {
    font-size: var(--fs-md);
    color: var(--algo-slate);
    word-break: break-word;
    max-width: 24rem;
    display: inline-block;
  }

  /* ── Settings tab ─────────────────────────────────────────────── */
  .lab-settings {
    display: flex;
    flex-direction: column;
    gap: 0.7rem;
    margin-top: 0.6rem;
    position: relative;
  }
  .lab-card {
    background: rgba(15, 25, 45, 0.4);
    border: 1px solid rgba(126, 151, 184, 0.18);
    border-radius: 0.5rem;
    padding: 0.8rem;
  }
  .lab-card h2 {
    margin: 0 0 0.5rem;
    color: var(--c-action);
    font-size: var(--fs-xl);
    font-weight: 700;
    letter-spacing: 0.02em;
  }
  .lab-card p {
    margin: 0 0 0.5rem;
    color: var(--algo-slate);
    font-size: var(--fs-lg);
    line-height: 1.5;
  }
  .lab-card code {
    font-family: var(--font-numeric);
    background: rgba(126, 151, 184, 0.10);
    padding: 0 0.25rem;
    border-radius: 0.2rem;
    font-size: var(--fs-md);
    color: var(--c-action);
  }
  .code-block {
    background: rgba(0, 0, 0, 0.35);
    border: 1px solid rgba(126, 151, 184, 0.18);
    border-radius: 0.35rem;
    padding: 0.55rem 0.7rem;
    color: var(--algo-slate);
    font-family: var(--font-numeric);
    font-size: var(--fs-md);
    line-height: 1.4;
    overflow-x: auto;
    white-space: pre-wrap;
  }
  .copy-btn {
    margin-top: 0.4rem;
    padding: 0.25rem 0.7rem;
    background: rgba(251, 191, 36, 0.12);
    border: 1px solid rgba(251, 191, 36, 0.35);
    color: var(--c-action);
    border-radius: 0.35rem;
    font-family: var(--font-numeric);
    font-size: var(--fs-md);
    font-weight: 700;
    cursor: pointer;
  }
  .copy-btn:hover { background: var(--algo-amber-bg-strong); }

  /* JWT shortcut — show the session token + a "Copy export line"
     button so the operator skips the curl + jq dance. */
  .jwt-current {
    /* Treat the token as a single shell-pasteable blob — wrap rather
       than scroll horizontally so the operator can see it's quoted
       properly. */
    white-space: pre-wrap;
    word-break: break-all;
    border-left: 2px solid rgba(34, 197, 94, 0.55);
  }
  .jwt-actions {
    display: flex;
    align-items: center;
    gap: 0.7rem;
    flex-wrap: wrap;
    margin-top: 0.25rem;
  }
  .jwt-note {
    color: var(--algo-muted);
    font-size: var(--fs-sm);
    line-height: 1.4;
  }
  .jwt-empty {
    padding: 0.5rem 0.7rem;
    background: rgba(248, 113, 113, 0.08);
    border: 1px solid var(--algo-red-border-soft);
    border-radius: 0.35rem;
    color: var(--c-short);
    font-family: var(--font-numeric);
    font-size: var(--fs-md);
  }
  .jwt-or {
    margin-top: 0.9rem !important;
    padding-top: 0.6rem;
    border-top: 1px dashed rgba(126, 151, 184, 0.18);
    color: var(--algo-muted) !important;
    font-size: var(--fs-md) !important;
  }
  .tools-table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--fs-lg);
  }
  .tools-table th {
    text-align: left;
    padding: 0.32rem 0.5rem;
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.06em;
    color: var(--algo-muted);
    border-bottom: 1px solid rgba(126, 151, 184, 0.18);
  }
  .tools-table td {
    padding: 0.32rem 0.5rem;
    color: var(--algo-slate);
    border-bottom: 1px solid rgba(126, 151, 184, 0.08);
  }
  /* .lab-card-safety / .lab-card-mint retired — every card now
     carries the canonical 3px amber left border via .lab-card. */
  .mint-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 0.5rem 0.7rem;
    margin: 0.6rem 0 0.5rem;
  }
  .mint-grid label {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }
  .mint-grid span {
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.06em;
    color: var(--algo-muted);
  }
  .mint-grid input {
    background: rgba(0, 0, 0, 0.3);
    border: 1px solid rgba(126, 151, 184, 0.25);
    border-radius: 0.25rem;
    padding: 0.32rem 0.45rem;
    color: var(--algo-slate);
    font-family: var(--font-numeric);
    font-size: var(--fs-lg);
  }
  .mint-grid input:focus {
    outline: none;
    border-color: rgba(251, 191, 36, 0.6);
  }
  .mint-update-full {
    /* JSON textarea needs the full grid width — without this it lands
       in one of the 140px auto-fit cells and the operator can't read
       what they pasted. */
    grid-column: 1 / -1;
  }
  .mint-update-textarea {
    background: rgba(0, 0, 0, 0.3);
    border: 1px solid rgba(126, 151, 184, 0.25);
    border-radius: 0.25rem;
    padding: 0.4rem 0.5rem;
    color: var(--algo-slate);
    font-family: var(--font-numeric);
    font-size: var(--fs-md);
    line-height: 1.45;
    width: 100%;
    resize: vertical;
    min-height: 4.5rem;
  }
  .mint-update-textarea:focus {
    outline: none;
    border-color: rgba(251, 191, 36, 0.6);
  }
  .mint-btn { margin-top: 0.2rem; }
  .mint-error {
    margin-top: 0.5rem;
    padding: 0.4rem 0.6rem;
    background: var(--algo-red-bg);
    border: 1px solid rgba(248, 113, 113, 0.32);
    border-radius: 0.35rem;
    color: var(--c-short);
    font-family: var(--font-numeric);
    font-size: var(--fs-md);
  }
  .mint-result {
    margin-top: 0.6rem;
    padding: 0.5rem 0.7rem;
    background: rgba(251, 191, 36, 0.06);
    border: 1px solid rgba(251, 191, 36, 0.35);
    border-radius: 0.4rem;
  }
  .mint-result.mint-expired {
    background: rgba(126, 151, 184, 0.06);
    border-color: rgba(126, 151, 184, 0.25);
    opacity: 0.65;
  }
  .mint-purpose {
    font-family: var(--font-numeric);
    font-size: var(--fs-lg);
    color: var(--algo-slate);
    margin-bottom: 0.4rem;
  }
  .mint-token-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
  }
  .mint-token {
    background: rgba(0, 0, 0, 0.4);
    border: 1px solid rgba(126, 151, 184, 0.25);
    padding: 0.25rem 0.55rem;
    border-radius: 0.3rem;
    font-family: var(--font-numeric);
    font-size: var(--fs-lg);
    color: var(--c-action);
    letter-spacing: 0.04em;
    user-select: all;
  }
  .mint-countdown {
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    color: var(--c-action);
    font-weight: 700;
  }
  .mint-expired .mint-countdown { color: var(--c-short); }
  .mint-hint {
    margin: 0.4rem 0 0;
    color: var(--algo-muted);
    font-size: var(--fs-sm);
    line-height: 1.4;
  }
  .safety-list {
    margin: 0; padding: 0 0 0 1.2rem;
    color: var(--algo-slate);
    font-size: var(--fs-lg);
    line-height: 1.5;
  }
  .safety-list li { margin-bottom: 0.25rem; }

  /* .copy-toast removed — migrated to canonical toast system (slice AO). */

  /* ── Confidence pills ─────────────────────────────────────────── */
  .pill {
    display: inline-block;
    padding: 0.05rem 0.4rem;
    border-radius: 0.5rem;
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    border: 1px solid transparent;
  }
  .pill-bull    { background: rgba(34, 197, 94, 0.15);  border-color: rgba(34, 197, 94, 0.4);  color: var(--c-long); }
  .pill-bear    { background: rgba(248, 113, 113, 0.15); border-color: rgba(248, 113, 113, 0.4); color: var(--c-short); }
  .pill-neutral { background: rgba(251, 191, 36, 0.12); border-color: rgba(251, 191, 36, 0.35); color: var(--c-action); }
  .pill-unsure  { background: rgba(126, 151, 184, 0.15); border-color: rgba(126, 151, 184, 0.35); color: var(--algo-slate); }
</style>
