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
  import { goto } from '$app/navigation';
  import { authStore, clientTimestamp, branchLabel, visibleInterval } from '$lib/stores';
  import {
    fetchResearchThreads, fetchResearchThread,
    deleteResearchThread, fetchResearchDrafts,
    mintConfirmToken, fetchResearchAudit,
  } from '$lib/api';
  import InfoHint from '$lib/InfoHint.svelte';

  /** @type {any[]} */
  let threads     = $state([]);
  /** @type {any|null} */
  let selected    = $state(null);
  let error       = $state('');
  let loading     = $state(true);
  let refreshedAt = $state('');
  let teardown;
  let activeTab   = $state(/** @type {'research'|'drafts'|'audit'|'settings'} */ ('research'));

  /** Joined-view rows from GET /api/research/drafts — one per
   *  research thread with a linked inactive Agent. Activating the
   *  agent on /agents naturally graduates it out of this list. */
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

  async function loadThreads() {
    try {
      threads     = await fetchResearchThreads();
      refreshedAt = clientTimestamp();
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
      drafts = Array.isArray(rows) ? rows : [];
    } catch (_) {
      drafts = [];
    }
  }

  async function loadAudit() {
    try {
      const rows = await fetchResearchAudit({
        tool:   auditFilterTool   || undefined,
        status: auditFilterStatus || undefined,
        limit:  200,
      });
      audit = Array.isArray(rows) ? rows : [];
    } catch (_) {
      audit = [];
    }
  }
  // Re-fetch when filters change; only fires when the Audit tab is in view
  // (the panel mounts conditionally so the $effect dependency is naturally gated).
  $effect(() => {
    if (activeTab === 'audit') {
      void auditFilterTool; void auditFilterStatus;
      loadAudit();
    }
  });

  async function selectThread(/** @type {number} */ id) {
    try {
      selected = await fetchResearchThread(id);
    } catch (e) {
      error = e.message;
    }
  }

  async function removeThread(/** @type {number} */ id) {
    try {
      await deleteResearchThread(id);
      if (selected?.id === id) selected = null;
      await loadThreads();
    } catch (e) {
      error = e.message;
    }
  }

  onMount(() => {
    const r = $authStore.user?.role;
    if (!$authStore.user || (r !== 'admin' && r !== 'designated')) { goto('/signin'); return; }
    loadThreads();
    loadDrafts();
    teardown = visibleInterval(() => { loadThreads(); loadDrafts(); }, 30000);
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

  /** @type {string} */
  let copyToast = $state('');
  async function copy(/** @type {string} */ s, /** @type {string} */ label) {
    try {
      await navigator.clipboard.writeText(s);
      copyToast = `${label} copied`;
      setTimeout(() => { copyToast = ''; }, 1400);
    } catch (_) {
      copyToast = 'copy failed';
      setTimeout(() => { copyToast = ''; }, 1400);
    }
  }

  // ── Phase-3 confirm-token mint widget (Settings tab) ───────────────
  // Operator types order details + mints a single-use 60s token.
  // Token is then pasted into Claude Code so the LLM's place_order
  // call carries the matching authorisation.
  let mintForm = $state({
    kind: /** @type {'place'|'cancel'|'modify'} */ ('place'),
    account: '', tradingsymbol: '', side: 'SELL', quantity: 1,
    mode: 'paper', order_type: 'LIMIT', price: null, trigger_price: null,
    order_id: '',
  });
  /** @type {any} */
  let mintedToken = $state(null);    // {token, expires_in, purpose, ...}
  let mintError   = $state('');
  let mintSecondsLeft = $state(0);
  /** @type {ReturnType<typeof setInterval> | undefined} */
  let mintTicker;
  async function mint() {
    mintError = '';
    try {
      // The server takes the same shape for all kinds; irrelevant
      // fields are simply ignored for the chosen kind.
      const payload = {
        ...mintForm,
        quantity: Number(mintForm.quantity) || 0,
        price:         mintForm.price         === null || mintForm.price         === '' ? null : Number(mintForm.price),
        trigger_price: mintForm.trigger_price === null || mintForm.trigger_price === '' ? null : Number(mintForm.trigger_price),
      };
      const res = await mintConfirmToken(payload);
      mintedToken = res;
      mintSecondsLeft = res.expires_in;
      clearInterval(mintTicker);
      mintTicker = setInterval(() => {
        mintSecondsLeft = Math.max(0, mintSecondsLeft - 1);
        if (mintSecondsLeft === 0) {
          clearInterval(mintTicker);
        }
      }, 1000);
    } catch (e) {
      mintError = e.message;
      mintedToken = null;
    }
  }
  onDestroy(() => clearInterval(mintTicker));

  // Phase-1 tool inventory — mirrors backend/mcp/kite_server.py
  const TOOLS = [
    { name: 'get_positions',         summary: 'Current intraday positions (optionally filtered by account)' },
    { name: 'get_holdings',          summary: 'Long-term holdings (optionally filtered by account)' },
    { name: 'get_quote',             summary: 'Live LTP / OHLC / change% for up to 300 symbols' },
    { name: 'get_ohlcv',             summary: 'Historical daily candles (up to 365 days)' },
    { name: 'get_recent_news',       summary: 'Recent Indian-market headlines (optional title filter)' },
    { name: 'get_option_analytics',  summary: 'Greeks + payoff + risk for one option leg' },
    { name: 'get_economic_snapshot', summary: 'India macros — repo rate, CPI, IIP, GDP, USD/INR' },
    { name: 'list_agents',           summary: 'List existing agents (optionally by status)' },
    { name: 'save_research_thread',  summary: 'Persist a thesis + transcript to the Lab page' },
    { name: 'save_agent_draft',      summary: 'Promote a thread to an inactive draft Agent (paper-mode)' },
    { name: 'place_order',           summary: 'Gated order placement — requires operator-minted confirm token' },
    { name: 'cancel_order',          summary: 'Gated cancel — requires confirm token bound to (account, order_id)' },
    { name: 'modify_order',          summary: 'Gated modify — token binds to new qty/price/trigger as well' },
    { name: 'get_audit_recent',      summary: 'Reverse-chrono trail of your MCP actions — self-check after writes' },
    { name: 'get_research_thread',   summary: 'Fetch a saved thread by id' },
    { name: 'list_research_threads', summary: 'List recent threads (optional symbol filter)' },
    { name: 'get_server_info',       summary: 'Diagnostic — base URL + token presence' },
  ];
</script>

<svelte:head><title>Research | RamboQuant Analytics</title></svelte:head>

<div class="page-header">
  <h1 class="page-title-chip">Research</h1>
  <InfoHint popup text="
    <b>Lab workspace</b> for chat-driven stock research.
    <br><br>
    The chat happens in Claude Code (your terminal); this page reads back
    the persisted threads + the draft agents you've promoted out of them.
    <br><br>
    Open the <b>Settings</b> tab for the one-time .mcp.json bootstrap.
    <br><br>
    No paid GenAI in the loop — Claude Code subscription is the only LLM."
  />
  {#if refreshedAt}<span class="algo-ts">{refreshedAt}</span>{/if}
</div>

{#if error}
  <div class="err-banner">{error}</div>
{/if}

<div class="lab-tabs" role="tablist">
  <button type="button" role="tab" class="lab-tab" class:lab-tab-on={activeTab === 'research'}
          aria-selected={activeTab === 'research'} onclick={() => activeTab = 'research'}>
    Research <span class="lab-tab-count">{threads.length}</span>
  </button>
  <button type="button" role="tab" class="lab-tab" class:lab-tab-on={activeTab === 'drafts'}
          aria-selected={activeTab === 'drafts'} onclick={() => activeTab = 'drafts'}>
    Drafts <span class="lab-tab-count">{drafts.length}</span>
  </button>
  <button type="button" role="tab" class="lab-tab" class:lab-tab-on={activeTab === 'audit'}
          aria-selected={activeTab === 'audit'} onclick={() => activeTab = 'audit'}>
    Audit
  </button>
  <button type="button" role="tab" class="lab-tab" class:lab-tab-on={activeTab === 'settings'}
          aria-selected={activeTab === 'settings'} onclick={() => activeTab = 'settings'}>
    Settings
  </button>
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
        <div class="rail-empty">Loading…</div>
      {:else if threads.length === 0}
        <div class="rail-empty">
          No saved threads yet.<br>
          Open Claude Code in this repo, ask it to research a stock, then say
          <i>"save it"</i> — the MCP tool will create a row here.
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
        <div class="lab-empty">
          <div class="lab-empty-title">No thread selected</div>
          <p class="lab-empty-hint">
            Pick a thread from the left rail, or start a new one by chatting
            with Claude Code (the MCP server writes back here automatically
            when you call <code>save_research_thread</code>).
          </p>
        </div>
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
      next step is "Run in Simulator" on /agents. Activating a draft
      graduates it out of this list.
    </p>
    {#if drafts.length === 0}
      <div class="lab-empty">
        <div class="lab-empty-title">No draft agents yet</div>
        <p class="lab-empty-hint">
          When Claude Code calls <code>save_agent_draft</code> after a
          research session, the new draft appears here. Until then, this
          list stays empty — the Drafts tab won't leak unrelated
          inactive agents from /agents.
        </p>
      </div>
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
              <td><a class="drafts-edit" href={`/agents`}>Open ›</a></td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </div>
{:else if activeTab === 'audit'}
  <!-- ── Audit tab — Phase 3b ────────────────────────────────────── -->
  <div class="lab-audit">
    <header class="audit-head">
      <p class="audit-hint">
        Forensic trail of every MCP-initiated mutation. Token material is
        never persisted — args show what the LLM asked to do, not what
        authorised it. Use this to confirm post-hoc that every LLM
        action was either consciously authorised or correctly denied.
      </p>
      <div class="audit-filters">
        <label>
          <span>Tool</span>
          <select bind:value={auditFilterTool}>
            <option value="">All</option>
            <option value="place_order">place_order</option>
            <option value="cancel_order">cancel_order</option>
            <option value="modify_order">modify_order</option>
          </select>
        </label>
        <label>
          <span>Status</span>
          <select bind:value={auditFilterStatus}>
            <option value="">All</option>
            <option value="ok">ok</option>
            <option value="denied">denied</option>
            <option value="error">error</option>
          </select>
        </label>
        <button type="button" class="copy-btn audit-refresh" onclick={loadAudit}>Refresh</button>
      </div>
    </header>
    {#if audit.length === 0}
      <div class="lab-empty">
        <div class="lab-empty-title">No audit rows yet</div>
        <p class="lab-empty-hint">
          The first MCP-initiated <code>place_order</code> call (success or
          failure) will land here. Until then, the trail is empty.
        </p>
      </div>
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
    <article class="lab-card lab-card-mint">
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
          <select bind:value={mintForm.kind}>
            <option value="place">PLACE</option>
            <option value="cancel">CANCEL</option>
            <option value="modify">MODIFY</option>
          </select>
        </label>
        <label><span>Account</span><input bind:value={mintForm.account} placeholder="ZG0790" /></label>
        {#if mintForm.kind === 'place'}
          <label><span>Symbol</span><input bind:value={mintForm.tradingsymbol} placeholder="NIFTY25APRFUT" /></label>
          <label><span>Side</span>
            <select bind:value={mintForm.side}>
              <option value="BUY">BUY</option>
              <option value="SELL">SELL</option>
            </select>
          </label>
          <label><span>Quantity</span><input type="number" bind:value={mintForm.quantity} min="1" /></label>
          <label><span>Mode</span>
            <select bind:value={mintForm.mode}>
              <option value="paper">PAPER</option>
              <option value="live">LIVE</option>
            </select>
          </label>
          <label><span>Order type</span>
            <select bind:value={mintForm.order_type}>
              <option value="LIMIT">LIMIT</option>
              <option value="MARKET">MARKET</option>
              <option value="SL">SL</option>
              <option value="SL-M">SL-M</option>
            </select>
          </label>
          <label><span>Price</span><input type="number" step="0.05" bind:value={mintForm.price} placeholder="(LIMIT/SL)" /></label>
          <label><span>Trigger</span><input type="number" step="0.05" bind:value={mintForm.trigger_price} placeholder="(SL/SL-M)" /></label>
        {:else if mintForm.kind === 'cancel'}
          <label><span>Mode</span>
            <select bind:value={mintForm.mode}>
              <option value="live">LIVE (broker)</option>
              <option value="paper">PAPER (engine)</option>
            </select>
          </label>
          <label><span>Order ID</span>
            <input bind:value={mintForm.order_id}
                   placeholder={mintForm.mode === 'paper' ? 'AlgoOrder.id (e.g. 42)' : '251115000123456'} />
          </label>
        {:else}
          <!-- modify: account + order_id + the new values -->
          <label><span>Mode</span>
            <select bind:value={mintForm.mode}>
              <option value="live">LIVE (broker)</option>
              <option value="paper">PAPER (engine)</option>
            </select>
          </label>
          <label><span>Order ID</span>
            <input bind:value={mintForm.order_id}
                   placeholder={mintForm.mode === 'paper' ? 'AlgoOrder.id (e.g. 42)' : '251115000123456'} />
          </label>
          <label><span>New qty</span><input type="number" bind:value={mintForm.quantity} min="0" placeholder="(unchanged)" /></label>
          <label><span>Order type</span>
            <select bind:value={mintForm.order_type}>
              <option value="LIMIT">LIMIT</option>
              <option value="MARKET">MARKET</option>
              <option value="SL">SL</option>
              <option value="SL-M">SL-M</option>
            </select>
          </label>
          <label><span>New price</span><input type="number" step="0.05" bind:value={mintForm.price} placeholder="(LIMIT/SL)" /></label>
          <label><span>New trigger</span><input type="number" step="0.05" bind:value={mintForm.trigger_price} placeholder="(SL/SL-M)" /></label>
        {/if}
      </div>
      <button class="copy-btn mint-btn" type="button" onclick={mint}>Mint token</button>
      {#if mintError}
        <div class="mint-error">{mintError}</div>
      {/if}
      {#if mintedToken}
        <div class="mint-result" class:mint-expired={mintSecondsLeft === 0}>
          <div class="mint-purpose">{mintedToken.purpose}</div>
          <div class="mint-token-row">
            <code class="mint-token">{mintedToken.token}</code>
            <button class="copy-btn" type="button" onclick={() => copy(mintedToken.token, 'token')}>Copy</button>
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
      <p>The MCP server authenticates against the RamboQuant API using your JWT. Mint one with the existing login endpoint and export it before launching Claude Code:</p>
      <pre class="code-block">export RAMBOQ_TOKEN=$(curl -s -X POST {baseUrl}/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{`{`}"username":"ambore","password":"<your-password>"{`}`}' \
  | jq -r .access_token)</pre>
      <button class="copy-btn" type="button" onclick={() => copy(
        `export RAMBOQ_TOKEN=$(curl -s -X POST ${baseUrl}/api/auth/login -H 'Content-Type: application/json' -d '{"username":"ambore","password":"<your-password>"}' | jq -r .access_token)`,
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

    <article class="lab-card lab-card-safety">
      <h2>4. Safety</h2>
      <ul class="safety-list">
        <li>No order placement from MCP yet. The server cannot move money.</li>
        <li><code>save_agent_draft</code> creates agents that ship <b>status=inactive</b> + <b>trade_mode=paper</b>. The endpoint cannot create an active or live agent.</li>
        <li>Operator's next step on every draft: <b>Run in Simulator</b> on /agents to validate the condition tree before activating.</li>
        <li>The JWT inherits your admin role. Don't paste it into untrusted MCP servers.</li>
        <li>Industry convention (Composer / IBKR TraderGPT): every LLM-initiated order requires explicit human confirm. Phase 3 will match.</li>
      </ul>
    </article>

    {#if copyToast}
      <div class="copy-toast">{copyToast}</div>
    {/if}
  </div>
{/if}

<style>
  /* ── Tab strip ────────────────────────────────────────────────── */
  .lab-tabs {
    display: flex;
    gap: 0;
    margin: 0.8rem 0 0.4rem;
    border-bottom: 1px solid rgba(126, 151, 184, 0.18);
    padding-bottom: 0;
  }
  .lab-tab {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    background: none;
    border: none;
    padding: 0.34rem 0.8rem 0.36rem;
    color: #7e97b8;
    font-family: ui-monospace, monospace;
    font-size: 0.7rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    cursor: pointer;
    border-bottom: 2px solid transparent;
    margin-bottom: -1px;
    transition: color 0.12s, border-color 0.12s;
  }
  .lab-tab:hover { color: #c8d8f0; }
  .lab-tab-on   { color: #fbbf24; border-bottom-color: #fbbf24; }
  .lab-tab-count {
    display: inline-block;
    padding: 0 0.32rem;
    border-radius: 0.5rem;
    background: rgba(126, 151, 184, 0.18);
    font-size: 0.6rem;
    line-height: 1.2rem;
    font-weight: 700;
  }
  .lab-tab-on .lab-tab-count { background: rgba(251, 191, 36, 0.18); }

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
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    color: #7e97b8;
  }
  .rail-empty {
    padding: 0.8rem 0.6rem;
    color: #7e97b8;
    font-size: 0.7rem;
    line-height: 1.4;
  }
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
    color: #c8d8f0;
    text-align: left;
    cursor: pointer;
    transition: background 0.12s;
  }
  .rail-row:hover    { background: rgba(126, 151, 184, 0.08); }
  .rail-row-on       { background: rgba(251, 191, 36, 0.10); }
  .rail-sym {
    grid-column: 1; grid-row: 1;
    font-family: ui-monospace, monospace;
    font-weight: 700;
    font-size: 0.72rem;
    color: #fbbf24;
  }
  .rail-conf {
    grid-column: 2; grid-row: 1;
    font-size: 0.55rem;
  }
  .rail-title {
    grid-column: 3; grid-row: 1;
    font-size: 0.68rem;
    color: #c8d8f0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .rail-meta {
    grid-column: 1 / -1; grid-row: 2;
    margin-top: 0.1rem;
    font-family: ui-monospace, monospace;
    font-size: 0.55rem;
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
    color: #7e97b8;
  }
  .lab-empty-title {
    font-family: ui-monospace, monospace;
    font-size: 0.85rem;
    font-weight: 700;
    color: #c8d8f0;
    margin-bottom: 0.6rem;
  }
  .lab-empty-hint {
    font-size: 0.72rem;
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
    font-family: ui-monospace, monospace;
    font-weight: 700;
    font-size: 1rem;
    color: #fbbf24;
  }
  .thr-title {
    color: #c8d8f0;
    font-size: 0.78rem;
  }
  .thr-time {
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    color: #7e97b8;
  }
  .thr-del {
    background: none;
    border: 1px solid rgba(248, 113, 113, 0.32);
    color: #f87171;
    border-radius: 0.35rem;
    padding: 0.1rem 0.5rem;
    cursor: pointer;
    font-size: 0.7rem;
  }
  .thr-del:hover { background: rgba(248, 113, 113, 0.12); }
  .thr-section-label {
    font-family: ui-monospace, monospace;
    font-size: 0.58rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    color: #7e97b8;
    margin-bottom: 0.4rem;
  }
  .thr-thesis { margin-bottom: 1rem; }
  .thr-thesis-body {
    background: rgba(251, 191, 36, 0.05);
    border-left: 2px solid rgba(251, 191, 36, 0.4);
    padding: 0.6rem 0.7rem;
    color: #c8d8f0;
    font-size: 0.74rem;
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
  .msg-assistant { background: rgba(251, 191, 36, 0.05);  border-color: rgba(251, 191, 36, 0.22); }
  .msg-tool      { background: rgba(126, 151, 184, 0.06); border-color: rgba(126, 151, 184, 0.22); }
  .msg-role {
    font-family: ui-monospace, monospace;
    font-size: 0.55rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    color: #7e97b8;
    margin-bottom: 0.2rem;
  }
  .msg-body {
    color: #c8d8f0;
    font-size: 0.72rem;
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
    color: #7e97b8;
    font-size: 0.7rem;
    line-height: 1.5;
    margin: 0 0 0.7rem;
  }
  .drafts-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.72rem;
  }
  .drafts-table th {
    text-align: left;
    padding: 0.34rem 0.5rem;
    font-family: ui-monospace, monospace;
    font-size: 0.58rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    color: #7e97b8;
    border-bottom: 1px solid rgba(126, 151, 184, 0.18);
  }
  .drafts-table td {
    padding: 0.36rem 0.5rem;
    color: #c8d8f0;
    border-bottom: 1px solid rgba(126, 151, 184, 0.08);
  }
  .drafts-name { display: block; color: #fbbf24; font-weight: 700; }
  .drafts-slug { display: block; font-family: ui-monospace, monospace; font-size: 0.58rem; color: #7e97b8; }
  .drafts-edit {
    color: #38bdf8;
    text-decoration: none;
    font-size: 0.7rem;
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
  .drafts-thr:hover .drafts-thr-title { color: #fbbf24; }
  .drafts-sym {
    font-family: ui-monospace, monospace;
    font-weight: 700;
    font-size: 0.7rem;
    color: #fbbf24;
  }
  .drafts-thr-title {
    font-size: 0.68rem;
    color: #c8d8f0;
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
    font-family: ui-monospace, monospace;
    font-size: 0.55rem;
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
    color: #7e97b8;
    font-size: 0.7rem;
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
    font-family: ui-monospace, monospace;
    font-size: 0.55rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    color: #7e97b8;
  }
  .audit-filters select {
    background: rgba(0, 0, 0, 0.3);
    border: 1px solid rgba(126, 151, 184, 0.25);
    border-radius: 0.25rem;
    padding: 0.28rem 0.45rem;
    color: #c8d8f0;
    font-family: ui-monospace, monospace;
    font-size: 0.7rem;
  }
  .audit-refresh { align-self: flex-end; }
  .audit-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.66rem;
  }
  .audit-table th {
    text-align: left;
    padding: 0.34rem 0.5rem;
    font-family: ui-monospace, monospace;
    font-size: 0.54rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    color: #7e97b8;
    border-bottom: 1px solid rgba(126, 151, 184, 0.18);
  }
  .audit-table td {
    padding: 0.34rem 0.5rem;
    color: #c8d8f0;
    border-bottom: 1px solid rgba(126, 151, 184, 0.08);
    vertical-align: top;
  }
  .audit-when { font-family: ui-monospace, monospace; font-size: 0.6rem; color: #7e97b8; }
  .audit-tool { color: #fbbf24; font-size: 0.66rem; font-weight: 700; }
  .audit-status {
    display: inline-block;
    padding: 0.05rem 0.4rem;
    border-radius: 0.5rem;
    font-family: ui-monospace, monospace;
    font-size: 0.55rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    border: 1px solid transparent;
  }
  .audit-status-ok     { background: rgba(34, 197, 94, 0.15);  border-color: rgba(34, 197, 94, 0.4);  color: #4ade80; }
  .audit-status-denied { background: rgba(248, 113, 113, 0.15); border-color: rgba(248, 113, 113, 0.4); color: #f87171; }
  .audit-status-error  { background: rgba(251, 191, 36, 0.15); border-color: rgba(251, 191, 36, 0.4); color: #fbbf24; }
  .audit-args, .audit-rid {
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    color: rgba(200, 216, 240, 0.8);
    word-break: break-all;
    max-width: 24rem;
    display: inline-block;
  }
  .audit-summary {
    font-size: 0.66rem;
    color: #c8d8f0;
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
    color: #fbbf24;
    font-size: 0.84rem;
    font-weight: 700;
    letter-spacing: 0.02em;
  }
  .lab-card p {
    margin: 0 0 0.5rem;
    color: #c8d8f0;
    font-size: 0.72rem;
    line-height: 1.5;
  }
  .lab-card code {
    font-family: ui-monospace, monospace;
    background: rgba(126, 151, 184, 0.10);
    padding: 0 0.25rem;
    border-radius: 0.2rem;
    font-size: 0.68rem;
    color: #fbbf24;
  }
  .code-block {
    background: rgba(0, 0, 0, 0.35);
    border: 1px solid rgba(126, 151, 184, 0.18);
    border-radius: 0.35rem;
    padding: 0.55rem 0.7rem;
    color: #c8d8f0;
    font-family: ui-monospace, monospace;
    font-size: 0.65rem;
    line-height: 1.4;
    overflow-x: auto;
    white-space: pre-wrap;
  }
  .copy-btn {
    margin-top: 0.4rem;
    padding: 0.25rem 0.7rem;
    background: rgba(251, 191, 36, 0.12);
    border: 1px solid rgba(251, 191, 36, 0.35);
    color: #fbbf24;
    border-radius: 0.35rem;
    font-family: ui-monospace, monospace;
    font-size: 0.65rem;
    font-weight: 700;
    cursor: pointer;
  }
  .copy-btn:hover { background: rgba(251, 191, 36, 0.22); }
  .tools-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.7rem;
  }
  .tools-table th {
    text-align: left;
    padding: 0.32rem 0.5rem;
    font-family: ui-monospace, monospace;
    font-size: 0.58rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    color: #7e97b8;
    border-bottom: 1px solid rgba(126, 151, 184, 0.18);
  }
  .tools-table td {
    padding: 0.32rem 0.5rem;
    color: #c8d8f0;
    border-bottom: 1px solid rgba(126, 151, 184, 0.08);
  }
  .lab-card-safety { border-left: 2px solid rgba(248, 113, 113, 0.5); }
  .lab-card-mint   { border-left: 2px solid rgba(251, 191, 36, 0.65); }
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
    font-family: ui-monospace, monospace;
    font-size: 0.55rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    color: #7e97b8;
  }
  .mint-grid input,
  .mint-grid select {
    background: rgba(0, 0, 0, 0.3);
    border: 1px solid rgba(126, 151, 184, 0.25);
    border-radius: 0.25rem;
    padding: 0.32rem 0.45rem;
    color: #c8d8f0;
    font-family: ui-monospace, monospace;
    font-size: 0.7rem;
  }
  .mint-grid input:focus,
  .mint-grid select:focus {
    outline: none;
    border-color: rgba(251, 191, 36, 0.6);
  }
  .mint-btn { margin-top: 0.2rem; }
  .mint-error {
    margin-top: 0.5rem;
    padding: 0.4rem 0.6rem;
    background: rgba(248, 113, 113, 0.10);
    border: 1px solid rgba(248, 113, 113, 0.32);
    border-radius: 0.35rem;
    color: #f87171;
    font-family: ui-monospace, monospace;
    font-size: 0.68rem;
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
    font-family: ui-monospace, monospace;
    font-size: 0.7rem;
    color: #c8d8f0;
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
    font-family: ui-monospace, monospace;
    font-size: 0.7rem;
    color: #fbbf24;
    letter-spacing: 0.04em;
    user-select: all;
  }
  .mint-countdown {
    font-family: ui-monospace, monospace;
    font-size: 0.62rem;
    color: #fbbf24;
    font-weight: 700;
  }
  .mint-expired .mint-countdown { color: #f87171; }
  .mint-hint {
    margin: 0.4rem 0 0;
    color: #7e97b8;
    font-size: 0.62rem;
    line-height: 1.4;
  }
  .safety-list {
    margin: 0; padding: 0 0 0 1.2rem;
    color: #c8d8f0;
    font-size: 0.7rem;
    line-height: 1.5;
  }
  .safety-list li { margin-bottom: 0.25rem; }

  .copy-toast {
    position: fixed;
    bottom: 1.5rem;
    right: 1.5rem;
    background: rgba(34, 197, 94, 0.18);
    border: 1px solid rgba(34, 197, 94, 0.45);
    color: #4ade80;
    padding: 0.4rem 0.8rem;
    border-radius: 0.4rem;
    font-family: ui-monospace, monospace;
    font-size: 0.7rem;
    font-weight: 700;
    z-index: 100;
  }

  /* ── Confidence pills ─────────────────────────────────────────── */
  .pill {
    display: inline-block;
    padding: 0.05rem 0.4rem;
    border-radius: 0.5rem;
    font-family: ui-monospace, monospace;
    font-size: 0.55rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    border: 1px solid transparent;
  }
  .pill-bull    { background: rgba(34, 197, 94, 0.15);  border-color: rgba(34, 197, 94, 0.4);  color: #4ade80; }
  .pill-bear    { background: rgba(248, 113, 113, 0.15); border-color: rgba(248, 113, 113, 0.4); color: #f87171; }
  .pill-neutral { background: rgba(251, 191, 36, 0.12); border-color: rgba(251, 191, 36, 0.35); color: #fbbf24; }
  .pill-unsure  { background: rgba(126, 151, 184, 0.15); border-color: rgba(126, 151, 184, 0.35); color: #c8d8f0; }
</style>
