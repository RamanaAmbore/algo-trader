<!--
  /automation/guide — operator-facing CLI reference for the agent-family
  grammar parsed by `$lib/command/grammars/agents.js` and typed into
  the Agent CLI panel on the Console tab of /automation.

  Moved here 2026-10 (Phase 2, Sprint B — "Console retirement") from
  /console/guide, which now redirects here (`/console/guide/+page.js`).
  Content otherwise unchanged except for the /console → /automation
  links below; every verified example citation still applies.

  Pure static documentation — no data fetch, no mutation, so this page
  carries none of the five Card-action buttons (search/expand/
  fullscreen/default-size/download) the CLAUDE.md button-group rule
  requires for operator-action cards; there is nothing here to act on.

  Every example statement below is one of: (a) lifted VERBATIM from
  frontend/src/lib/__tests__/agentsGrammar.test.js, (b) a trivial
  rename of a verbatim example's #name="..." placeholder value for
  readability, or (c) the exact text produced by the grammar's own
  decompiler for a real BUILTIN_AGENTS entry (agent_engine.py) and
  independently re-verified to compile via the SAME compiler used in
  (a)/(b) — none hand-invented. Every statement was confirmed against
  the last-committed (HEAD) version of agents.js, since a concurrent
  in-flight edit left the live working-tree copy transiently broken
  at the time this page was authored. See the task report for the
  full per-example citation list. Account IDs (ZG0790/ZJ6294) and
  expiry-dated contract symbols (NIFTY25JULFUT/BANKNIFTY25JULFUT) are
  REAL shapes but will be stale by the time you read this — substitute
  your own account and a currently-listed contract.
-->
<script>
  import AlgoTimestamp from '$lib/AlgoTimestamp.svelte';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
</script>

<svelte:head><title>Console CLI Guide | RamboQuant Analytics</title></svelte:head>

<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">Console CLI Guide</h1>
  </span>
  <AlgoTimestamp />
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <a href="/automation?tab=console" class="cg-back-link">&larr; Back to Console</a>
    <PageHeaderActions />
  </span>
</div>

<div class="cg-wrap">
  <p class="cg-intro">
    The Agent CLI panel on the <a href="/automation?tab=console">Console tab</a> of
    /automation (below the plain orders bar) parses one statement at a time — it's a real multi-line textarea, so a
    statement can wrap across lines — into a live JSON preview, then shows a kind-appropriate
    submit button — <strong>Place order</strong>, <strong>Create agent</strong>,
    <strong>Save bracket</strong>, or <strong>Create token</strong> — depending on what you typed.
    This page is a fast "how do I…" reference for that grammar, organised by what each
    statement shape is <em>for</em>. Every example below compiles against the grammar's own
    test suite (see the page source comment for how each was verified) — but swap in your own
    account and a currently-listed contract symbol before you type one in for real; the
    account/symbol values shown are real shapes, not live-valid today.
  </p>

  <!-- ══════════════════════════ ORDERS ══════════════════════════ -->
  <section class="algo-card cg-section">
    <h2 class="section-heading">Orders — <code>order(...)</code></h2>
    <p class="cg-lede">
      A bare <code>order(...)</code> (or two or more, comma-separated) places a REAL order
      immediately on submit — there's no WHEN/condition at all. Use this when you know
      exactly what you want to trade right now and don't want to open the ticket modal.
    </p>

    <div class="cg-example">
      <p class="cg-why">A routine one-off sell, 1 lot, nothing fancy.</p>
      <pre class="cg-code">order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1)</pre>
      <p class="cg-what">
        Compiles to a single ticket (LIMIT/paper by default). The submit button reads
        <strong>Place order (PAPER)</strong> or <strong>(LIVE)</strong> depending on your navbar
        execution mode — clicking it hits the exact same <code>POST /api/orders/ticket</code>
        endpoint the Order ticket modal uses. If mode is LIVE, a confirm dialog pops up first.
      </p>
    </div>

    <div class="cg-example">
      <p class="cg-why">
        A one-off trade where you want tighter exits than your usual saved Bracket —
        skip the preset entirely and dial in a specific take-profit / stop-loss inline.
      </p>
      <pre class="cg-code">order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1, tp_pct=30, sl_pct=-20)</pre>
      <p class="cg-what">
        <code>tp_pct</code>/<code>sl_pct</code> compile straight to the ticket's
        <code>tp_pct_override</code>/<code>sl_pct_override</code> fields. Once the order fills,
        the backend auto-arms a +30% take-profit GTT and a −20% stop-loss against your entry —
        no bracket needed for a single trade.
      </p>
    </div>

    <div class="cg-example">
      <p class="cg-why">
        Mirror the same kind of trade across two broker accounts in one line, instead of
        opening the ticket modal twice.
      </p>
      <pre class="cg-code">order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1),
order(account="ZJ6294", symbol="BANKNIFTY25JULFUT", side="BUY", lots=1)</pre>
      <p class="cg-what">
        Two or more comma-separated <code>order(...)</code> calls compile to a <strong>basket</strong>
        instead of a single ticket, grouped by account in the preview. Submit hits
        <code>POST /api/orders/basket</code>, which dispatches every account's leg(s) in
        parallel under one shared basket tag — same guarantee as the Basket tab in the
        ticket modal, just typed as one line. Baskets always show the LIVE confirm dialog
        before submit, even if your navbar mode is PAPER — multi-account fan-out gets the
        extra guard rail unconditionally.
      </p>
    </div>

    <p class="cg-note">
      See <a href="#brackets">Brackets</a> below for a fourth order shape —
      <code>order(..., bracket="default-bull")</code> — that attaches a saved TP/SL/Wing
      preset by name instead of typing the overrides inline (admin-seeded presets only today —
      see that section's "known gap" note).
    </p>
  </section>

  <!-- ══════════════════════════ AGENTS ══════════════════════════ -->
  <section class="algo-card cg-section">
    <h2 class="section-heading">Agents — <code>WHEN ... ALERT ... DO ...</code></h2>
    <p class="cg-lede">
      An agent is a standing rule the backend polls continuously. <code>WHEN</code>,
      <code>ALERT</code>, and <code>DO</code> are each independently optional (omit
      <code>WHEN</code> to mean "always true"; omit a clause to mean "nop") — but at least
      one real <code>ALERT</code> or <code>DO</code> is required. Clicking
      <strong>Create agent</strong> saves it as a standing rule, not a one-off action.
    </p>

    <div class="cg-example">
      <p class="cg-why">
        A loss-guard that actually closes the position — not just a notification. "If my
        30-minute average P&amp;L across all my NIFTY futures positions drops below
        −₹50,000, sell to close and tell me on Telegram."
      </p>
      <pre class="cg-code">WHEN mean_pnl(minutes=30)@positions.total &lt;= -50000
ALERT telegram
DO order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1)</pre>
      <p class="cg-what">
        <code>mean_pnl(minutes=30)@positions.total</code> is a metric@scope binding — the
        30-minute rolling mean P&amp;L across your <em>total</em> position book. When the
        condition is true, the backend fires BOTH the ALERT (Telegram push) and the DO
        (places the real sell order) — this is a genuine auto-close agent, not a dry alert.
      </p>
    </div>

    <div class="cg-example">
      <p class="cg-why">
        A pure early-warning agent with no automated action — you want to know the moment
        your book is down 5% intraday so YOU can decide, not have the system act for you.
      </p>
      <pre class="cg-code">WHEN pnl_pct@positions.total &lt;= -5 ALERT telegram DO nop</pre>
      <p class="cg-what">
        <code>DO nop</code> (or simply omitting <code>DO</code> entirely) means "notify only,
        take no action." This is the shape for a watch-and-decide-manually agent — safe to
        run even on accounts you don't want the system auto-trading.
      </p>
    </div>

    <div class="cg-example">
      <p class="cg-why">
        A margin-health warning that fires BEFORE you're actually in trouble — "tell me if
        available margin on any one account is getting low (under ₹25,000) but hasn't gone
        negative yet." This is the exact condition shape of the platform's real built-in
        <code>loss-margin-low</code> agent, reconstructed from its own JSON via the grammar's
        decompiler.
      </p>
      <pre class="cg-code">WHEN 25000 &gt; avail_margin@funds.any_acct &gt; 0 ALERT telegram, log DO nop</pre>
      <p class="cg-what">
        The <code>a &gt; metric@scope &gt; b</code> "between" form is sugar for two chained
        comparisons on the same metric (works written either direction — high-to-low or
        low-to-high). <code>funds.any_acct</code> (vs. <code>funds.total</code>) means this
        evaluates PER broker account — it fires separately naming whichever specific account
        is actually low, not just a blended firm-wide total.
      </p>
    </div>

    <p class="cg-note">
      Conditions combine with <code>&amp;</code> (AND), <code>|</code> (OR), and
      <code>~</code> (NOT) — parens are required when mixing operators, e.g.
      <code>(a &amp; b) | c</code>. See the Quick Reference table below for the full operator
      list.
    </p>
  </section>

  <!-- ══════════════════════════ EVENT AGENTS ══════════════════════════ -->
  <section class="algo-card cg-section">
    <h2 class="section-heading">Event agents — notifications on log activity</h2>
    <p class="cg-lede">
      A bare <code>ALERT</code> statement with NO <code>WHEN</code>/<code>DO</code> — and
      whose first clause is one of the four reserved keys below — is a different animal
      from an ordinary agent: instead of polling a metric, it subscribes to the
      platform's own internal log/event stream and fires whenever a matching line is
      written. Use this for "notify me the instant the system itself reports a problem,"
      not "notify me when a number crosses a threshold."
    </p>

    <div class="cg-example">
      <p class="cg-why">
        "I want to know immediately — on Telegram AND email AND the in-app log, not just
        one channel — the instant anything in the system logs an ERROR tagged 'error'."
      </p>
      <pre class="cg-code">ALERT log_tag=error, min_level=ERROR, render=error, telegram, email, log #name="Error alert"</pre>
      <p class="cg-what">
        <code>log_tag=</code> and <code>render=</code> are both required; <code>min_level=</code>
        is optional (defaults to <code>INFO</code>). There's no condition to poll at all — the
        "condition" IS the log stream itself, filtered by tag + level. <code>render=</code>
        picks which message template formats the alert text.
      </p>
    </div>
  </section>

  <!-- ══════════════════════════ BRACKETS ══════════════════════════ -->
  <section id="brackets" class="algo-card cg-section">
    <h2 class="section-heading">Brackets — <code>bracket(...)</code> (reusable Order Templates)</h2>
    <p class="cg-lede">
      A Bracket is a saved TP/SL/Wing preset — the idea is to build the exit rules ONCE, then
      attach a preset to any order going forward just by name, instead of retyping
      <code>tp_pct</code>/<code>sl_pct</code>/wing kwargs on every single order.
      <code>bracket(...)</code> used standalone (never inside a <code>DO</code> clause) saves a
      NEW preset; clicking <strong>Save bracket</strong> persists it. (See the known gap
      below — today, referencing-by-name only works for presets seeded by an admin.)
    </p>

    <div class="cg-example">
      <p class="cg-why">
        Your standard long-side exit rule: whenever you go long, take profit at +30% (as a
        LIMIT order) and cut losses at −20%. Save it once as "Default Bull" instead of
        typing those three numbers into every buy order you place.
      </p>
      <pre class="cg-code">bracket(applies_to=buy_any, tp_pct=30, sl_pct=-20, tp_order_type=LIMIT) #name="Default Bull"</pre>
      <p class="cg-what">
        Compiles to an <code>OrderTemplateCreate</code> row — <code>applies_to=buy_any</code>
        scopes it to BUY orders. <code>#name=</code> is required (a saved bracket must have a
        name); there's no condition or channel here at all, just the exit-rule shape.
      </p>
    </div>

    <div class="cg-example">
      <p class="cg-why">
        Attach an EXISTING saved bracket to a new order by name, instead of re-specifying
        TP/SL inline every time.
      </p>
      <pre class="cg-code">order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1, bracket="default-bull")</pre>
      <p class="cg-what">
        <code>bracket="default-bull"</code> compiles to a <code>template_slug</code> on the
        ticket, resolved to the real numeric <code>template_id</code> right before submit.
      </p>
      <p class="cg-what cg-warn">
        <strong>Known gap:</strong> matching is by the saved template's <code>slug</code>
        column, exact string equality only — never by display name. Presets seeded by an
        admin carry a real slug and ARE addressable this way today. A bracket you save
        yourself from this CLI (the "Default Bull" example above) does NOT currently get a
        slug written on create, so <code>bracket="..."</code> can't reference it yet — you'd
        see a clear "Unknown bracket" error, not a silent mismatch. Ask an admin which slugs
        are available, or attach a freshly-saved bracket from its own row in the template list
        (outside this CLI) until this gap closes.
      </p>
    </div>
  </section>

  <!-- ══════════════════════════ TOKENS ══════════════════════════ -->
  <section class="algo-card cg-section">
    <h2 class="section-heading">Tokens — <code>token(kind=notify) ...</code></h2>
    <p class="cg-lede">
      A Token saves a reusable group of channels (or, for <code>kind=condition</code>, a
      reusable condition) under one name — handy when several different agents should
      always notify the same way, so you don't repeat the same channel list in every
      <code>ALERT</code> clause.
    </p>

    <div class="cg-example">
      <p class="cg-why">
        You always want critical agents to notify via both Telegram and email together —
        save that pairing once under a name instead of typing <code>telegram, email</code>
        into every agent's ALERT clause.
      </p>
      <pre class="cg-code">token(kind=notify) telegram, email #name="Ops Channels"</pre>
      <p class="cg-what">
        <code>kind=notify</code> is the supported value this release; the body after the
        head call is a plain channel list, compiled the exact same way an ordinary
        <code>ALERT</code> call-list is. <code>#name=</code> is required. Once saved, a Token
        shows up in the channel/condition catalog under its own name and can be referenced
        anywhere a channel (or, for condition-kind, a metric) is expected.
      </p>
    </div>
  </section>

  <!-- ══════════════════════════ META TAGS ══════════════════════════ -->
  <section class="algo-card cg-section">
    <h2 class="section-heading">Meta tags — <code>#name</code> / <code>#desc</code> / <code>#tag</code> / <code>#group</code></h2>
    <p class="cg-lede">
      A trailing <code>#key=value</code> clause (after everything else) labels an agent,
      bracket, or token for your own organisation — never valid on a bare order. All four
      are independently optional except where noted above (brackets/tokens require
      <code>#name</code>).
    </p>
    <div class="cg-meta-grid">
      <div><code class="cg-inline">#name="NIFTY loss guard"</code><span>display name</span></div>
      <div><code class="cg-inline">#desc="Closes on -50k drawdown"</code><span>free-text description</span></div>
      <div><code class="cg-inline">#tag=risk,nifty</code><span>comma list of tags</span></div>
      <div><code class="cg-inline">#group=hedge-book</code><span>grouping label</span></div>
    </div>
    <p class="cg-what">
      Example in context: <code class="cg-inline">WHEN pnl@positions.total &lt;= -1 ALERT telegram #group=hedge-book</code>
    </p>
  </section>

  <!-- ══════════════════════════ QUICK REFERENCE ══════════════════════════ -->
  <section class="algo-card cg-section">
    <h2 class="section-heading">Quick reference</h2>
    <div class="algo-table-wrap">
      <table class="algo-table cg-ref-table">
        <thead>
          <tr><th>Syntax</th><th>Meaning</th></tr>
        </thead>
        <tbody>
          <tr><td><code>metric@scope</code></td><td>Bind a metric to where it's measured, e.g. <code>pnl@positions.total</code></td></tr>
          <tr><td><code>scope.column</code></td><td>Dot-access a scope's own column directly, e.g. <code>positions.total.pnl</code> (no <code>@</code>)</td></tr>
          <tr><td><code>&amp;</code> / <code>|</code> / <code>~</code></td><td>AND / OR (equal precedence, left-to-right) / NOT — parens required when combining: <code>(a &amp; b) | c</code></td></tr>
          <tr><td><code>&lt; &lt;= &gt; &gt;= == != in / not in</code></td><td>Comparators. <code>in</code>/<code>not in</code> take a <code>[a, b, c]</code> list literal</td></tr>
          <tr><td><code>a &lt;= metric@scope &lt;= b</code></td><td>Between-range sugar for two chained comparisons on one metric</td></tr>
          <tr><td><code>always</code></td><td>Sentinel condition that's always true (used to run a DO/ALERT unconditionally)</td></tr>
          <tr><td><code>nop</code></td><td>Explicit no-op for an ALERT or DO clause (same as omitting the clause)</td></tr>
          <tr><td><code>lots=</code> vs <code>qty=</code></td><td>F&amp;O orders take <code>lots=</code> (auto-converted via the symbol's lot size); equity uses raw <code>qty=</code></td></tr>
          <tr><td><code>tp_pct</code> / <code>sl_pct</code></td><td>Inline take-profit / stop-loss override, on <code>order(...)</code> or <code>bracket(...)</code></td></tr>
          <tr><td><code>wing_premium_pct</code> / <code>wing_strike_offset</code></td><td>Wing-leg hedge sizing — mutually exclusive, pick one</td></tr>
          <tr><td><code>bracket="slug"</code></td><td>Attach a saved Bracket by slug to an <code>order(...)</code></td></tr>
          <tr><td>Short kwarg aliases</td><td><code>acct</code>=account, <code>sym</code>=symbol, <code>exch</code>=exchange, <code>otype</code>=order_type, <code>px</code>=price, <code>trigpx</code>=trigger_price, <code>prod</code>=product, <code>var</code>=variety, <code>chase</code>=chase_level</td></tr>
          <tr><td><code>#name</code> / <code>#desc</code> / <code>#tag</code> / <code>#group</code></td><td>Trailing meta tags — agents/brackets/tokens only, never on a bare order</td></tr>
        </tbody>
      </table>
    </div>
  </section>
</div>

<style>
  .cg-back-link {
    font-size: var(--fs-sm);
    color: var(--c-info);
    text-decoration: none;
    padding: 0.15rem 0.5rem;
    border: 1px solid var(--algo-cyan-border-soft, rgba(34, 211, 238, 0.30));
    border-radius: 4px;
    white-space: nowrap;
  }
  .cg-back-link:hover { background: var(--c-info-14); }

  .cg-wrap {
    max-width: 56rem;
    margin: 0 auto;
    padding: 0.5rem 0.75rem 2rem;
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
  }

  .cg-intro {
    font-size: var(--fs-md);
    line-height: 1.5;
    color: #cbd5e1;
  }

  .cg-section {
    padding: 0.7rem 0.85rem;
  }
  .cg-section .section-heading {
    font-size: 0.72rem;
    margin-bottom: 0.45rem;
  }
  .cg-section .section-heading code {
    text-transform: none;
    letter-spacing: normal;
  }

  .cg-lede {
    font-size: var(--fs-md);
    line-height: 1.5;
    color: #a8b6ca;
    margin-bottom: 0.6rem;
  }

  .cg-example {
    border-top: 1px solid rgba(255, 255, 255, 0.07);
    padding: 0.55rem 0;
  }
  .cg-example:first-of-type { border-top: none; padding-top: 0; }

  .cg-why {
    font-size: var(--fs-md);
    color: #e2e8f0;
    font-style: italic;
    margin-bottom: 0.35rem;
  }

  .cg-code {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.68rem;
    line-height: 1.5;
    background: #152033;
    border: 1px solid #334155;
    border-left: 3px solid #fbbf24;
    color: #f1f5f9;
    padding: 0.45rem 0.6rem;
    border-radius: 0.25rem;
    overflow-x: auto;
    white-space: pre-wrap;
    word-break: break-word;
    margin: 0 0 0.35rem 0;
  }

  .cg-what {
    font-size: var(--fs-sm);
    line-height: 1.5;
    color: #8a99b3;
  }
  .cg-what code { color: #c4d4ea; }

  .cg-note {
    font-size: var(--fs-sm);
    color: #8a99b3;
    margin-top: 0.4rem;
  }

  /* Known-gap callouts — amber, matches the algo palette's "action/
     attention" accent (see CLAUDE.md palette: action = #fbbf24). */
  .cg-warn {
    margin-top: 0.4rem;
    padding: 0.4rem 0.55rem;
    border-radius: 0.25rem;
    background: var(--algo-amber-bg-soft, rgba(251, 191, 36, 0.08));
    border: 1px solid var(--algo-amber-border-soft, rgba(251, 191, 36, 0.30));
    color: #d6b06a;
  }
  .cg-warn strong { color: #fbbf24; }

  code {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }

  .cg-inline {
    background: rgba(255, 255, 255, 0.06);
    padding: 0.05rem 0.3rem;
    border-radius: 0.2rem;
    font-size: 0.68rem;
    color: #f1f5f9;
  }

  .cg-meta-grid {
    display: grid;
    gap: 0.35rem;
    margin-bottom: 0.5rem;
  }
  .cg-meta-grid > div {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
  }
  .cg-meta-grid > div > span {
    font-size: var(--fs-sm);
    color: #8a99b3;
  }

  .cg-ref-table td:first-child {
    white-space: nowrap;
  }
  .cg-ref-table code {
    font-size: 0.68rem;
    color: #c4d4ea;
  }

  /* Mobile — code blocks still need to fit <600px without forcing the
     whole page to horizontal-scroll; wrap long lines instead. */
  @media (max-width: 600px) {
    .cg-wrap { padding: 0.5rem 0.5rem 1.5rem; }
    .cg-code { font-size: 0.62rem; }
  }
</style>
