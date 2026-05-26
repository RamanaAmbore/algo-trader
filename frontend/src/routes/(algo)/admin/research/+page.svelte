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
  let activeTab   = $state(/** @type {'research'|'drafts'|'settings'} */ ('research'));

  /** Joined-view rows from GET /api/research/drafts — one per
   *  research thread with a linked inactive Agent. Activating the
   *  agent on /agents naturally graduates it out of this list. */
  /** @type {any[]} */
  let drafts = $state([]);

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

  // Phase-1 tool inventory — mirrors backend/mcp/kite_server.py
  const TOOLS = [
    { name: 'get_positions',         summary: 'Current intraday positions (optionally filtered by account)' },
    { name: 'get_holdings',          summary: 'Long-term holdings (optionally filtered by account)' },
    { name: 'get_quote',             summary: 'Live LTP / OHLC / change% for up to 300 symbols' },
    { name: 'get_ohlcv',             summary: 'Historical daily candles (up to 365 days)' },
    { name: 'get_recent_news',       summary: 'Recent Indian-market headlines (optional title filter)' },
    { name: 'get_option_analytics',  summary: 'Greeks + payoff + risk for one option leg' },
    { name: 'list_agents',           summary: 'List existing agents (optionally by status)' },
    { name: 'save_research_thread',  summary: 'Persist a thesis + transcript to the Lab page' },
    { name: 'save_agent_draft',      summary: 'Promote a thread to an inactive draft Agent (paper-mode)' },
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
{:else}
  <!-- Settings tab — MCP bootstrap -->
  <div class="lab-settings">
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
