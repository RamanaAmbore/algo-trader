<script>
  import { goto, invalidateAll } from '$app/navigation';
  import { onMount } from 'svelte';
  import { nowStamp } from '$lib/stores';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';

  // Refresh on a narrative tour page just re-runs load functions so the
  // hero + section cards re-flow with fresh layout. No data is
  // fetched, so this is effectively a UI reset.
  let _refreshing = $state(false);
  async function _refresh() {
    _refreshing = true;
    await invalidateAll();
    _refreshing = false;
  }

  // Recruiter-facing narrative page. Single scrollable tour that
  // explains the platform's novel pieces (5-mode execution ladder,
  // declarative agent grammar, BS options analytics, multi-broker
  // abstraction) and links each one to the live surface where the
  // viewer can poke at it. No data fetches — pure prose + links so
  // the page loads instantly even on a recruiter's flaky phone.

  // FOUC gate. The page lives under (algo)/ which is ssr=false +
  // prerender=false; on a first-paint navigation the HTML shell + JS
  // bundle race the scoped CSS chunk, so visitors momentarily saw the
  // raw text (default browser styling) before the cards snapped into
  // their decorated form. onMount only fires once both the component
  // JS and its scoped CSS have arrived, so the gate releases at the
  // first frame where decoration is guaranteed.
  let _mounted = $state(false);
  onMount(() => { _mounted = true; });

  /** @type {Array<{
   *   title: string,
   *   tag: string,
   *   body: string,
   *   bullets: string[],
   *   link?: { href: string, label: string },
   *   color: string,
   * }>} */
  const sections = [
    {
      title: 'Multi-mode execution ladder',
      tag: 'Execution',
      color: '#fbbf24',
      body: 'Every agent and order graduates through a confidence ladder before real money moves. Mode is now set exclusively from the navbar dropdown — one click switches the entire pipeline. Same engine runs fabricated (sim), historical (replay), paper (real quotes, no execution), and live (real orders).',
      bullets: [
        'Simulator — fabricated price moves drive the same agent engine that runs live. Run-in-Simulator on any agent for instant dry-fire.',
        'Replay — historical Kite candles feed the paper trade engine for backtesting',
        'Paper — real broker quotes, validated via Kite basket_margin API, no execution',
        'Live — real broker orders. Prod default on fresh install; flip navbar to PAPER anytime for soak-testing',
        'Shadow — logs exact Kite payload + margin validation without executing. Final pre-live audit trail.',
        'Single navbar mode pill replaces per-order toggles — clearer mental model, fewer mistakes',
      ],
      link: { href: '/admin/execution?mode=sim', label: 'Open Execution' },
    },
    {
      title: 'Declarative agent grammar',
      tag: 'Risk engine',
      color: '#7dd3fc',
      body: 'Loss rules, fund-negative alerts, expiry auto-close — all expressed as a metric/scope/op/value tree in a database table. Adding a new rule type is one DB row plus one Python resolver function, never an engine change.',
      bullets: [
        '14 loss-* agents ship live (per-account + total, abs + pct + rate thresholds)',
        'Cooldown + suppression-delta gates prevent alert storms during volatile sessions',
        'Schedule-aware: market-hours agents skip when no segment is open',
        'Telegram + email + WebSocket + log channels, per-agent opt-in',
      ],
      link: { href: '/automation', label: 'Browse agents' },
    },
    {
      title: 'Derivatives analytics with σ-driven payoff',
      tag: 'Quant math',
      color: '#a5b4fc',
      body: 'Black-Scholes pricer, calibrated IV per leg, multi-leg payoff with shaded P&L zone + breakevens + POP integrated against the lognormal pdf. The Close tab now surfaces three distinct buckets — ITM-on-expiry (amber), netted pairs (MCX only), OTM positions (mute) — so you see at-a-glance what needs operator action before settlement.',
      bullets: [
        'Greeks (Δ Γ Θ V ρ) in trader-friendly units, per-leg and position-summed',
        'Expected value via trapezoidal integration of expiry curve × lognormal pdf',
        'R:R ratio for bounded structures, ∞ rendered correctly for unbounded legs',
        'Coherent underlying re-pricing — one "NIFTY −1%" tick cascades through every contract',
        'ITM/Netted/OTM sections clearly separate action items from monitor-only positions',
        'Symbol roots (CRUDEOIL) show contract month as a chip; ≤3 days to expiry flips the chip amber',
      ],
      link: { href: '/admin/derivatives', label: 'Open Derivatives' },
    },
    {
      title: 'Proxy hedges — your GOLDBEES hedges GOLDM, your RELIANCE hedges NIFTY',
      tag: 'Quant math · Major capability',
      color: '#c084fc',
      body: 'A capability the major Indian retail platforms (Sensibull / Streak / Opstra) don\'t offer at all and institutional tools (Bloomberg PRM / IBKR Portfolio Margin) charge thousands per year for. Pick GOLDM in the Underlying picker and your GOLDBEES units automatically surface as an eq leg in Legs — converted to gram-equivalent, then to GOLDM option lots, all from live broker LTPs. Pick NIFTY and any held stock with a β regression on file shows up as a beta-scaled hedge with its R² confidence. Math is derived per-render; nothing coded by hand or operator-tuned.',
      bullets: [
        'DB-backed `hedge_proxies` pair table (proxy_symbol → target_root) — operator edits from /admin/settings; six default pairs seeded on first boot covering GOLDBEES → GOLD/GOLDM, SILVERBEES → SILVER/SILVERM, NIFTYBEES → NIFTY, BANKBEES → BANKNIFTY',
        'Conversion math is fully live: effective_qty = β × market_value / target_spot, where market_value comes from the holding row\'s qty × LTP and target_spot from /api/options/strategy-analytics. No factor stored anywhere — change the LTPs, the chip updates',
        'Lot conversion surfaces in the Lots column AND the PROXY chip label: 1500 GOLDBEES = 0.15 GOLD lots / 1.5 GOLDM lots / 15 GOLDPETAL lots, same proxy, target-specific denominator',
        'β regression: POST /api/admin/hedge-proxies/{id}/compute runs proxy_return = α + β × target_return on 60 days of daily closes; numpy regression yields β + R², written back to the row. MCX targets get a tighter 30-day window auto-applied so fresh-rollover contracts still regress cleanly',
        'Daily background task at 02:30 IST recomputes every row older than 7 days, paces 1s/row to stay under Kite\'s 3 req/sec historical budget. Failed regressions now stamp regression_error too — distinguishes "broken pair" from "stale β" so the operator sees WHY a 7-day blackout is in effect',
        'Stale-β chip on every PROXY tag: amber after 2 days, red after 7 days or any failed regression. Tooltip carries the precise age + the underlying error verbatim',
        'Pathological-β rejection: |β| > 5 gets rejected with a clear log line (bad bar, split day, fat-finger trade); Bloomberg PRM caps to ±3, we\'re slightly more permissive for leveraged ETF proxies',
        'Root-exact symbol resolution distinguishes GOLD ≠ GOLDM ≠ GOLDPETAL ≠ GOLDGUINEA (and SILVER vs SILVERM vs SILVERMIC) — startswith() would silently collide them',
        'For Indian retail this is roughly a 100k+/yr institutional capability landing inside the operator\'s ₹0/month terminal',
      ],
      link: { href: '/admin/derivatives', label: 'See it on Derivatives' },
    },
    {
      title: 'Multi-broker abstraction + Dhan multi-account fix',
      tag: 'Infra',
      color: '#86efac',
      body: 'Single Broker ABC hides Zerodha Kite, Dhan, Groww behind one interface. The 2026-06-16 Dhan multi-account fix was the headline win: two Dhan partner apps + two Kite accounts + one Groww account, all running stable from a single VPS — even though Dhan enforces "one active session per partner app per source IP" at its auth backend.',
      bullets: [
        'IP-sharing across brokers — each Dhan account pairs with a Kite account on the SAME source IP (DH3747+ZJ6294 on IPv6 ::1, DH6847+ZG0790 on IPv4 69.62.78.136). Different brokers maintain independent per-IP session registries, so cross-broker sharing is invisible to each side',
        'PriceBroker auto-failover for quote / LTP / historical — when Dhan returns `{}` for MCX commodities it doesn\'t expose (CRUDEOIL futures etc.), the new `_quote_has_data` predicate treats empty-success as soft failure and falls through to Kite. Eliminates the 8200-strike-fallback spot bug',
        'Multi-account stabilizer in `Connections.rebuild_from_db` — groups Dhan rows by source_ip; if two would collide on the same egress IP, defers the lower-priority row with a clear warning log. Permanent guard rail at the connection layer',
        'Hostinger edge filter documented + worked around — the upstream router only egresses ::1 + IPv4 from the documented /48. The IP-sharing fix uses both working IPs across brokers instead of provisioning new IPs',
        'Per-account source_ip binds the HTTP pool manager via a custom requests.Adapter (IPv6 or IPv4)',
        'Same-day-expiry rollover in the futures-anchor lookup — MCX commodity options expire ~5 business days BEFORE the futures, so the chart now rolls JUN options to the JUL future anchor once the option settles (matches the broker app)',
        'Credentials encrypted at rest (Fernet, HKDF-derived key) — secrets.yaml is bootstrap-only',
      ],
      link: { href: '/admin/brokers', label: 'Brokers (admin)' },
    },
    {
      title: 'Multi-account basket orders + auto profit targets',
      tag: 'Trading',
      color: '#fb923c',
      body: 'Every order can now build a basket of legs across multiple accounts and auto-place a take-profit on fill. The chase engine validates each leg via Kite\'s basket_margin API before execution and handles margin offsets correctly — one order form, parallel execution across accounts.',
      bullets: [
        'Basket-building UI: one symbol per leg, per-leg account dropdown. Margin strip shows per-account Required / Avail / After.',
        'Auto profit target: default +30% (tunable in /admin/settings → algo.default_target_pct). Override per-order inline.',
        'On-fill template attach (per ticket OR per basket) — pick one TP/SL/wing template, every leg auto-arms its bracket OCO when the parent fills',
        'Submit → POST /api/orders/basket: one Kite basket_order call per account in parallel',
        'Spread-aware adaptive chase loop — same code path paper + live, validates via basket_margin before any order touches the broker',
        'REJECTED aborts the chase immediately so a broker reject can\'t loop into a fee-burning retry storm',
        'Per-card Reconcile button — re-syncs ONE order\'s algo row against the broker book (postback miss / network drop / stuck OPEN row), no full sweep needed',
        'Per-tick fill check (bid ≥ limit for SELL, ask ≤ limit for BUY); cancel + re-quote after N attempts',
        'Partial-fill correctness: AlgoOrder.filled_quantity persists across chase partials; exit GTT sizing matches the actual filled portion, not the original ask',
        'MCX qty unit fix: chase loop reverses Kite\'s lot-based status reporting back to contracts before comparing against remaining_qty (1-lot fill on 100-contract order no longer triggers a phantom partial)',
      ],
      link: { href: '/orders', label: 'Open Orders' },
    },
    {
      title: 'Order templates — bracket OCO across every broker',
      tag: 'Trading',
      color: '#fcd34d',
      body: 'Templates pre-define the TP / SL / wing GTTs that arm the moment a parent fills. Pick once on OrderTicket or basket; the backend applies per leg through one unified apply_template_to_order pipeline. Templates now work on Kite, Dhan, AND Groww — Groww\'s no-native-OCO case is emulated via two single-trigger GTTs with a background pair-watcher that cancels the survivor when one side fires.',
      bullets: [
        'Catalog at /automation/templates — TP %, SL %, optional wing GTTs, scale-out ladders, trailing stops',
        'OrderTicket Default/None toggle below the side pills; basket bar carries one above the legs list',
        'Backend fires apply_template_to_order at every fill site — postback handler, chase terminal, reconcile path, paper-engine fill — same idempotency guard everywhere via per-row asyncio.Lock',
        'Templates respect the parent\'s side (BUY parent → SELL TP, SELL parent → BUY TP at fill ± template %)',
        'Multi-broker: Kite native OCO; Dhan native OCO with correct leg-name dispatch on modify; Groww emulated OCO via compound "oco:{a}+{b}" id + 15s pair-watcher background task',
        'Inline warning chip on OrderTicket flags the broker gaps at SUBMIT time ("Groww OCO emulated — 15s race window", "MCX not on Dhan"), not at fill time',
        'Trailing stops now correctly modify both legs on Dhan OCO; Sprint A fix to the silent ENTRY_LEG-only modify_forever bug',
      ],
      link: { href: '/automation/templates', label: 'Open Templates' },
    },
    {
      title: 'Holdings F&O signal — at-a-glance covered-call sizing',
      tag: 'Operator UX',
      color: '#4ade80',
      body: 'The Holdings grid surfaces F&O eligibility AND sized-position viability inline on every row, so the operator scans the book and sees "this stock has options + I hold enough for N covered calls + there\'s already an open derivative here" in one glance — no per-symbol drill-down required.',
      bullets: [
        'Green left stripe on holding rows where the stock has CE/PE listed (F&O underlying)',
        'NL chip immediately after the symbol shows the integer or fractional lot count (e.g. 3L, 1.5L, 0.7L). One-decimal precision when fractional; bare integer when whole-lot',
        'Chip turns amber when the underlying already has an open derivative position (covered call / hedge / spread in play) — warns the operator before they double-write the same underlying',
        'Lot lookup memoized per-symbol; per-render zero-cost after the first hit',
      ],
      link: { href: '/pulse', label: 'Open Pulse' },
    },
    {
      title: 'Unified card UX + page-header modal trio + connection health',
      tag: 'Operator UX',
      color: '#22d3ee',
      body: 'Every card across every page speaks one UX language — Collapse / Default / Fullscreen sit top-right in the same cyan-400 trio, refresh affordances bake into the same icon family, a connection-status badge on every Refresh button shows broker-account health at-a-glance, and a page-header trio (Orders / Charts / Activity) opens canonical modals at the same viewport position regardless of which page launched them. One mental model, every surface.',
      bullets: [
        'Default-mode trio: Collapse → Default → Fullscreen (DefaultSize hidden when not maximized)',
        'Fullscreen mode reverses to Refresh + Default — Collapse is hidden, OrderNotif + AgentNotif bells lift to viewport top-right',
        'Page-header Orders / Charts / Activity modals share one frame (.canonical-modal-overlay/-panel) — same position, same close affordance, same Esc behaviour',
        'Activity modal reuses the same 6-tab log surface (Orders / Agents / Terminal / Ticks / System / News) the Order-modal bottom panel and /console + /automation pages mount — single LogPanel, every callsite',
        'Symbol anchors auto-resolve to the tradeable contract (NIFTY 50 → NIFTY26JUNFUT, CRUDEOIL → CRUDEOILM26JUNFUT) so chart + order modals open with the real future / option, not the spot key',
        'Connection badge on Refresh: green (all broker accounts loaded), amber (partial), red (none) — single 15 s global poll, every Refresh icon subscribes',
        'Charts scale to viewport in fullscreen mode (OptionsPayoff, PriceChart, dashboard equity curve)',
      ],
      link: { href: '/admin/derivatives', label: 'See it on Derivatives' },
    },
    {
      title: 'Real-time alerting with tier-aware audio',
      tag: 'In-app alerting',
      color: '#a78bfa',
      body: 'Two persistent surfaces keep the operator aware. Bell dropdowns in every page header list recent events with one-click drill-down to a rich modal; toast notifications fire on every agent_inapp_notify with a tier-aware Web Audio chirp — critical lands as a descending two-note urgent pattern, info as a single high chirp. Mute toggle in the bell panel persists per browser.',
      bullets: [
        'Order bell + Agent bell on every page header; badge persists while the panel is open so the operator reads items with the unread count for context',
        'Web Audio API generates the tone — no audio file shipped, zero bundle weight',
        'Sound autoplays cleanly after the first user interaction; silently no-ops on cold-load per browser policy',
        'Telegram + email + WebSocket + log channels all wire through the same agent grammar — alert routing is per-agent opt-in',
      ],
      link: { href: '/automation/activity', label: 'View activity feed' },
    },
    {
      title: 'Production deployment',
      tag: 'Ops',
      color: '#c4b5fd',
      body: 'GitHub push → webhook → branch-routed deploy → systemctl restart. Two environments on one box (prod/main + dev/non-main), separate databases, separate log streams, separate broker accounts.',
      bullets: [
        'PostgreSQL 17 + SQLAlchemy 2.x async + asyncpg',
        'Litestar 2.x + msgspec.Struct (≈10× faster than pydantic) + Polars route aggregation',
        'SvelteKit 5 + ag-Grid v33 + hand-rolled SVG charts (no chart library, thin bundle)',
        'Telegram + email alerts dual-routed (operator inbox + public-website form)',
      ],
      link: { href: '/dashboard', label: 'Open Dashboard' },
    },
  ];

  const facts = [
    { val: '~70k', lbl: 'lines of code' },
    { val: '5',    lbl: 'execution modes' },
    { val: '3',    lbl: 'broker adapters' },
    { val: '14+',  lbl: 'loss/risk agents' },
    { val: '24×7', lbl: 'background risk engine' },
    { val: '0',    lbl: 'chart libraries (all hand-rolled SVG)' },
    { val: '1',    lbl: 'engineer (start to ship)' },
  ];
</script>

<svelte:head>
  <title>Rambo Terminal — Tour | RamboQuant</title>
  <meta name="description" content="Rambo Terminal — a walkthrough of RamboQuant's quant trading terminal: 5-mode execution ladder, declarative agent grammar, Black-Scholes derivatives analytics, multi-broker abstraction." />
</svelte:head>

<div class="show" class:show-ready={_mounted}>
  <!-- Canonical page-header — Refresh + Order + Chart + Log icons
       sit alongside the timestamp the same way every other algo
       page renders them. Recruiters/visitors browsing the tour get
       the same chrome the live operator pages carry, so the screen
       above the fold matches what the rest of the platform looks
       like. -->
  <div class="page-header">
    <span class="algo-title-group">
      <h1 class="page-title-chip">Tour</h1>
    </span>
    <span class="algo-ts">{$nowStamp}</span>
    <span class="ml-auto"></span>
    <span class="page-header-actions">
      <RefreshButton onClick={_refresh} loading={_refreshing} label="tour" />
      <PageHeaderActions />
    </span>
  </div>

  <!-- Hero -->
  <header class="show-hero">
    <h1 class="show-title">Rambo Terminal — Tour</h1>
    <p class="show-tag">A production quant trading terminal that runs a real strategy: hold high-conviction stocks long, then use them (and cash) as margin for a toolkit of derivative strategies — covered calls, cash-secured puts, vertical / calendar spreads, collars, wheels, futures, hedges — letting the algo pick the right tool for the market and handle execution + risk. Built end-to-end by one engineer for the RamboQuant LLP partnership — you're looking at the live system, accounts masked.</p>
    <div class="show-facts">
      {#each facts as f}
        <div class="show-fact">
          <div class="show-fact-val">{f.val}</div>
          <div class="show-fact-lbl">{f.lbl}</div>
        </div>
      {/each}
    </div>
  </header>

  <!-- Section cards. Each one teaches a concept and links to the live
       surface where the viewer can poke at it. -->
  <section class="show-grid">
    {#each sections as s, i}
      <article class="show-card" style="--accent: {s.color}">
        <div class="show-card-head">
          <span class="show-card-tag" style="--accent: {s.color}">{s.tag}</span>
          <span class="show-card-num">{String(i + 1).padStart(2, '0')}</span>
        </div>
        <h2 class="show-card-title">{s.title}</h2>
        <p class="show-card-body">{s.body}</p>
        <ul class="show-card-bullets">
          {#each s.bullets as b}
            <li>{b}</li>
          {/each}
        </ul>
        {#if s.link}
          <!-- Use <a href> instead of <button onclick=goto> so screen
               readers, right-click "open in new tab", and crawlers
               all work. Aria-label includes the action verb so the
               trailing "→" glyph isn't read out as text. -->
          <a class="show-card-link" href={s.link.href}
             aria-label={`Open ${s.link.label}`}>
            {s.link.label} →
          </a>
        {/if}
      </article>
    {/each}
  </section>

  <!-- Footer-CTA — get the recruiter to take one of the two next
       actions before they close the tab. -->
  <footer class="show-footer">
    <p class="show-footer-line">Want the source story or a deeper conversation?</p>
    <div class="show-footer-btns">
      <a href="/about" class="show-footer-btn show-footer-btn-alt">↙ About the founder</a>
      <a href="/contact" class="show-footer-btn">Get in touch →</a>
    </div>
  </footer>
</div>

<style>
  .show {
    max-width: 1180px;
    margin: 0 auto;
    /* Top padding tightened (was 1rem) — the algo layout already
       provides ~0.5rem above the main, plus the hero's own 1.25rem
       top padding. The double-stack created an unintentional 2.5rem
       gap above the H1; trimming to 0.25rem here lets the hero
       breathe at its own natural top-padding. */
    padding: 0.25rem 0.5rem 2rem;
    color: var(--algo-slate);
    /* FOUC gate — hidden until onMount sets .show-ready. Stops the
       brief flash of plain-text hero + bullet lists between the
       JS bundle landing and the scoped CSS chunk applying. */
    opacity: 0;
    transition: opacity 0.18s ease-out;
  }
  .show.show-ready {
    opacity: 1;
  }

  /* ── Hero ─────────────────────────────────────────────────────────── */
  .show-hero {
    text-align: center;
    /* Top padding tightened (was 1.5rem) so the H1 sits closer to
       the navbar / banners. The bottom padding stays generous so
       the hero feels intentionally separated from the section
       grid below. */
    padding: 0.5rem 1rem 1.75rem;
    border-bottom: 1px solid rgba(126, 151, 184, 0.18);
    margin-bottom: 1.5rem;
  }
  .show-title {
    font-size: clamp(1.4rem, 4vw, 1.9rem);
    font-weight: 800;
    color: #fbbf24;
    letter-spacing: -0.01em;
    margin: 0 0 0.6rem;
    line-height: 1.15;
  }
  .show-tag {
    font-size: clamp(0.85rem, 2vw, 1rem);
    color: #9bb0d0;
    line-height: 1.6;
    max-width: 42rem;
    margin: 0 auto 1.4rem;
  }
  .show-facts {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: 0.4rem 0.6rem;
    margin-top: 0.5rem;
  }
  .show-fact {
    background: rgba(126, 151, 184, 0.08);
    border: 1px solid rgba(126, 151, 184, 0.20);
    border-radius: 0.3rem;
    padding: 0.45rem 0.7rem;
    text-align: center;
    min-width: 6.5rem;
  }
  .show-fact-val {
    font-size: 1.05rem;
    font-weight: 800;
    color: #fbbf24;
    line-height: 1.1;
    font-variant-numeric: tabular-nums;
  }
  .show-fact-lbl {
    font-size: 0.65rem;
    font-weight: 700;
    color: var(--algo-muted);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-top: 0.2rem;
  }

  /* ── Section grid ──────────────────────────────────────────────────── */
  .show-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(20rem, 1fr));
    gap: 1rem;
  }
  .show-card {
    background: rgba(15, 25, 45, 0.55);
    border: 1px solid rgba(126, 151, 184, 0.18);
    border-top: 3px solid var(--accent);
    border-radius: 0.5rem;
    padding: 1rem 1.1rem 0.9rem;
    display: flex;
    flex-direction: column;
  }
  .show-card-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 0.5rem;
  }
  .show-card-tag {
    font-size: 0.66rem;
    font-weight: 700;
    color: var(--accent);
    text-transform: uppercase;
    letter-spacing: 0.1em;
    padding: 0.15rem 0.5rem;
    /* rgba fallback for Safari < 16.2 (color-mix support landed in
       16.2). Recruiter audience may still be on older iOS. */
    background: rgba(212, 146, 12, 0.12);
    background: color-mix(in srgb, var(--accent) 12%, transparent);
    border: 1px solid rgba(212, 146, 12, 0.35);
    border: 1px solid color-mix(in srgb, var(--accent) 35%, transparent);
    border-radius: 999px;
  }
  .show-card-num {
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    color: var(--algo-muted);
    letter-spacing: 0.05em;
  }
  .show-card-title {
    font-size: 1.05rem;
    font-weight: 800;
    color: #e8efff;
    margin: 0 0 0.45rem;
    line-height: 1.2;
  }
  .show-card-body {
    font-size: 0.85rem;
    color: #9bb0d0;
    line-height: 1.55;
    margin: 0 0 0.6rem;
  }
  .show-card-bullets {
    list-style: none;
    padding: 0;
    margin: 0 0 0.85rem;
    flex: 1;
  }
  .show-card-bullets li {
    font-size: 0.82rem;
    color: var(--algo-slate);
    line-height: 1.45;
    padding: 0.18rem 0 0.18rem 1rem;
    position: relative;
  }
  /* Mobile bump — keep card body legible without pinch-to-zoom; tag
     chips also bumped so they don't read as decorative noise. */
  @media (max-width: 600px) {
    .show-card-body     { font-size: 0.9rem; }
    .show-card-bullets li { font-size: 0.88rem; }
    .show-card-tag      { font-size: 0.65rem; }
  }
  .show-card-bullets li::before {
    content: '▸';
    position: absolute;
    left: 0;
    color: var(--accent);
    font-size: 0.65rem;
    top: 0.28rem;
  }
  .show-card-link {
    background: transparent;
    /* rgba fallback first, color-mix on top for modern browsers. */
    border: 1px solid rgba(212, 146, 12, 0.45);
    border: 1px solid color-mix(in srgb, var(--accent) 45%, transparent);
    color: var(--accent);
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.02em;
    padding: 0.35rem 0.75rem;
    border-radius: 0.3rem;
    cursor: pointer;
    align-self: flex-start;
    transition: background-color 0.1s, border-color 0.1s;
    font-family: inherit;
    text-decoration: none;
  }
  .show-card-link:hover {
    background: rgba(212, 146, 12, 0.14);
    background: color-mix(in srgb, var(--accent) 14%, transparent);
    border-color: var(--accent);
  }

  /* ── Footer CTA ───────────────────────────────────────────────────── */
  .show-footer {
    text-align: center;
    padding: 1.5rem 1rem 0.5rem;
    margin-top: 1.5rem;
    border-top: 1px solid rgba(126, 151, 184, 0.18);
  }
  .show-footer-line {
    font-size: 0.85rem;
    color: #9bb0d0;
    margin: 0 0 0.75rem;
  }
  .show-footer-btns {
    display: flex;
    gap: 0.6rem;
    justify-content: center;
    flex-wrap: wrap;
  }
  .show-footer-btn {
    display: inline-block;
    font-size: 0.78rem;
    font-weight: 700;
    padding: 0.45rem 1rem;
    border-radius: 0.3rem;
    background: rgba(251, 191, 36, 0.10);
    color: #fbbf24;
    border: 1px solid rgba(251, 191, 36, 0.40);
    text-decoration: none;
    transition: background-color 0.1s, border-color 0.1s;
  }
  .show-footer-btn:hover {
    background: rgba(251, 191, 36, 0.20);
    border-color: rgba(251, 191, 36, 0.65);
  }
  .show-footer-btn-alt {
    background: transparent;
    color: #9bb0d0;
    border-color: rgba(126, 151, 184, 0.35);
  }
  .show-footer-btn-alt:hover {
    background: rgba(126, 151, 184, 0.10);
    color: var(--algo-slate);
    border-color: rgba(126, 151, 184, 0.55);
  }
</style>
