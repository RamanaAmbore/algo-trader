<script>
  import { untrack } from 'svelte';
  import { get } from 'svelte/store';
  import { browser } from '$app/environment';
  import { lastRefreshAt, formatDualTz } from '$lib/stores';
  import { isNseOpen, isMcxOpen, onMarketStatusChange } from '$lib/marketHours';
  import InfoHint from '$lib/InfoHint.svelte';

  let _lastRefresh = $state(browser ? get(lastRefreshAt) : 0);
  let _showRefresh = $state(false);

  let _nowEpoch = $state(browser ? Date.now() : 0);

  // Market-status badge (2026-10-22) — moved here from the order-ticket
  // modal (SymbolPanel.svelte's old 3-pill `.oes-mkt-badge` group) so it's
  // page-wide, ambient information instead of something only visible with
  // an order ticket open. AlgoTimestamp is the one shared component every
  // algo page already renders between the title and the button-group
  // (confirmed 24 route files via `grep -rln "<AlgoTimestamp"`), so
  // extending it here gets every page for free with zero per-page wiring.
  //
  // Data source: the SAME module-level cache in `$lib/marketHours` that
  // RefreshButton.svelte and the (now-removed) SymbolPanel pills already
  // read — fed by the app-wide 5-min `/api/market/status` poll
  // (`startMarketStatusPoller()` in stores.js). No new poller here.
  //
  // Seeded with `browser ? ... : false` (not a bare call) for the same
  // SSR-hydration-stability reason `_nowEpoch` above is gated — both sides
  // of a server/client render must agree so Svelte doesn't warn about a
  // hydration mismatch.
  let _nseOpen = $state(browser ? isNseOpen() : false);
  let _mcxOpen = $state(browser ? isMcxOpen() : false);
  function _recomputeMkt() {
    _nseOpen = isNseOpen();
    _mcxOpen = isMcxOpen();
  }

  // Compound bucket for the single market-status pill — mirrors
  // RefreshButton's `_mktClass` 3-bucket idea (both open / MCX-only open /
  // closed) extended with a 4th bucket ("eq" — NSE open, MCX not yet open,
  // see the `--algo-market-eq-only-*` token in app.css) that RefreshButton's
  // own scheme doesn't distinguish.
  //
  // Collapsed to ONE pill (2026-10-23, operator: "only one badge can be
  // active at a time") — the prior 3-pill layout (M / MCX / EQ shown
  // simultaneously) was mostly redundant: in the "both open" and "closed"
  // states all three pills repeated the same colour, and MCX/EQ only ever
  // added information in the two mixed states, which the compound bucket
  // already disambiguates. One pill whose label switches carries the same
  // information with less header clutter.
  const _mktBucket = $derived(
    _nseOpen && _mcxOpen ? 'both'
    : _mcxOpen            ? 'mcx'
    : _nseOpen            ? 'eq'
    :                       'closed'
  );
  const _mktLabel = $derived(
    _mktBucket === 'mcx' ? 'MCX'
    : _mktBucket === 'eq' ? 'EQ'
    :                       'M'
  );
  const _mktTitle = $derived(
    _mktBucket === 'both'   ? 'Both MCX and non-MCX (NSE/NFO/BSE/CDS) sessions are open'
    : _mktBucket === 'mcx'  ? 'MCX commodity session is open; non-MCX is closed'
    : _mktBucket === 'eq'   ? 'Non-MCX (NSE/NFO/BSE/CDS) session is open; MCX is closed'
    :                         'Both MCX and non-MCX sessions are closed'
  );
  // Tooltip (2026-10-23) — same hover-preview / click-to-pin InfoHint
  // convention PositionStrip.svelte's own single-letter P/M/C/H pills
  // already use (`InfoHint popup panel label=...`), not a native `title`
  // attribute, for visual/UX consistency with that sibling badge family.
  // `hideButton` + `anchor` keeps this pill's own bucket-coloured markup
  // as the trigger instead of letting InfoHint render its separate small
  // "(i)" chip look, which doesn't fit this badge's design.
  const _mktAccent = $derived(
    _mktBucket === 'both'   ? '#4ade80'
    : _mktBucket === 'mcx'  ? '#fbbf24'
    : _mktBucket === 'eq'   ? '#a78bfa'
    :                         '#94a3b8'
  );
  /** @type {HTMLSpanElement | undefined} */
  let _mktPillEl = $state();
  let _mktInfoOpen = $state(false);

  $effect(() => {
    const lr = $lastRefreshAt;
    _lastRefresh = lr;
    if (lr > untrack(() => _nowEpoch)) _nowEpoch = Date.now();
  });

  // Single 30s interval drives both the clock tick AND the market-status
  // re-derive (no second timer, per the "reuse the existing interval"
  // instruction). `onMarketStatusChange` additionally fires `_recomputeMkt`
  // the instant a fresh /api/market/status poll lands, instead of waiting
  // up to 30s for the next tick — closes a real race where this component
  // seeds from the time-of-day fallback a moment before the in-flight poll
  // (already kicked off by the layout) resolves.
  $effect(() => {
    const id = setInterval(() => {
      _nowEpoch = Date.now();
      _recomputeMkt();
    }, 30_000);
    const unsub = browser ? onMarketStatusChange(_recomputeMkt) : null;
    return () => { clearInterval(id); unsub?.(); };
  });

  let _nowTs     = $derived(_nowEpoch ? formatDualTz(new Date(_nowEpoch)) : '');
  let _refreshTs = $derived(_lastRefresh ? formatDualTz(new Date(_lastRefresh)) : null);

  function _toggle() { if (_refreshTs) _showRefresh = !_showRefresh; }

  $effect(() => { if (!_refreshTs && _showRefresh) _showRefresh = false; });
</script>

<!-- `.ats-root` / `.ats-scroll` — fixed-shell / inner-scroll split, the
     exact pattern PositionStrip.svelte's `.ps-strip` / `.ps-strip-scroll`
     uses for NavStrip's own mobile-overflow fix. `.page-header` is
     `flex-wrap: wrap`, so a plain content-sized flex item just wraps to a
     new line once it doesn't fit rather than shrinking — `.ats-root`'s
     explicit `min-width: 0` overrides the flex item's automatic minimum
     (which would otherwise floor at the content's own min-content width)
     so the browser shrinks this item instead of wrapping the whole header,
     and `.ats-scroll`'s `overflow-x: auto` lets the clock + badge group
     scroll horizontally inside that shrunk box instead of being clipped or
     forcing the row to wrap. -->
<div class="ats-root">
  <div class="ats-scroll">
    <button
      type="button"
      class="ats-group"
      onclick={_toggle}
      onkeydown={(e) => e.key === 'Enter' && _toggle()}
      style="touch-action: manipulation; user-select: none; -webkit-tap-highlight-color: transparent;">
      <span class="ats-slot">
        <span class="ats-now" class:ats-mobile-hide={_showRefresh}>{_nowTs}</span>
        {#if _refreshTs}
          <span class="ats-sep" aria-hidden="true">|</span>
          <span class="ats-refresh" class:ats-mobile-hide={!_showRefresh}>{_refreshTs}</span>
        {/if}
      </span>
    </button>

    <!-- Market-status badge — one short pill rendered as a plain <span>,
         not inside the clock button, so it never inherits the button's
         tap-to-toggle behaviour or its desktop `pointer-events: none`.
         Label switches between M / MCX / EQ depending on the compound
         4-bucket state; colour carries the same signal. Tooltip is the
         InfoHint hover-preview/click-to-pin popover (hideButton+anchor
         onto this pill itself), matching PositionStrip's P/M/C/H
         convention — not a native `title` attribute. The pill's own
         click/Enter/Space toggles the pin directly (its own `onclick`,
         not InfoHint's — hideButton mode renders no button of InfoHint's
         own to attach one to), so touch devices (no hover) can still
         reach the tooltip. -->
    <span class="ats-mkt-badge" data-testid="algo-ts-market-badge">
      <span class="ats-mkt-pill"
            class:ats-mkt-open={_mktBucket === 'both'}
            class:ats-mkt-mcx-only={_mktBucket === 'mcx'}
            class:ats-mkt-eq-only={_mktBucket === 'eq'}
            class:ats-mkt-closed={_mktBucket === 'closed'}
            data-testid="algo-ts-market-badge-any"
            role="button" tabindex="0"
            aria-label="Market status details"
            onclick={() => { _mktInfoOpen = !_mktInfoOpen; }}
            onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); _mktInfoOpen = !_mktInfoOpen; } }}
            bind:this={_mktPillEl}>
        {_mktLabel}
      </span>
      <InfoHint popup hideButton panel
        anchor={_mktPillEl} bind:open={_mktInfoOpen}
        accentColor={_mktAccent} title="Market status" text={_mktTitle} />
    </span>
  </div>
</div>

<style>
  .ats-root {
    display: flex;
    align-items: center;
    min-width: 0;
    overflow: hidden;
  }
  .ats-scroll {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    overflow-x: auto;
    overflow-y: hidden;
    -webkit-overflow-scrolling: touch;
    scrollbar-width: none;
    overscroll-behavior-x: contain;
  }
  .ats-scroll::-webkit-scrollbar { display: none; }
  .ats-group {
    background: none;
    border: none;
    padding: 0;
    font: inherit;
    color: inherit;
    text-align: inherit;
    cursor: default;
    pointer-events: none;
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    flex-shrink: 0;
  }
  .ats-slot {
    display: inline-flex;
    gap: 0.3rem;
  }
  .ats-now {
    color: var(--c-info);
    font-size: inherit;
  }
  .ats-refresh {
    color: var(--algo-amber, #fbbf24);
    font-size: inherit;
  }
  .ats-sep {
    color: var(--text-muted);
    font-size: inherit;
    opacity: 0.5;
  }
  /* Market-status badge — one compact pill, label switches (M / MCX / EQ);
     colour alone carries the open/closed signal (title attr carries the
     full sentence for a11y + hover). Palette: open = the app's canonical
     green (same tokens the removed SymbolPanel pills used); closed =
     the same muted/slate tokens RefreshButton's `.rf-mkt-closed` already
     resolves to byte-for-byte (`--algo-muted-bg`/`--algo-muted-border` ==
     rgba(126,151,184,…), `--algo-dim` == #94a3b8); MCX-only = the same
     amber tokens RefreshButton's `.rf-mkt-mcx` already uses
     (`--c-action-14`/`--algo-amber-border`/`--c-action`); Eq-only = the
     new `--algo-market-eq-only-*` token (see app.css) for the one bucket
     RefreshButton's scheme can't distinguish. */
  .ats-mkt-badge {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    flex-shrink: 0;
  }
  .ats-mkt-pill {
    display: inline-flex;
    align-items: center;
    flex-shrink: 0;
    padding: 0.08rem 0.3rem;
    border-radius: 3px;
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.02em;
    white-space: nowrap;
    cursor: pointer;
  }
  .ats-mkt-open {
    background: var(--algo-green-bg-mid);
    border: 1px solid var(--algo-green-border);
    color: var(--algo-green-text);
  }
  .ats-mkt-closed {
    background: var(--algo-muted-bg);
    border: 1px solid var(--algo-muted-border);
    color: var(--algo-dim);
  }
  .ats-mkt-mcx-only {
    background: var(--c-action-14);
    border: 1px solid var(--algo-amber-border);
    color: var(--c-action);
  }
  .ats-mkt-eq-only {
    background: var(--algo-market-eq-only-bg);
    border: 1px solid var(--algo-market-eq-only-border);
    color: var(--algo-market-eq-only-text);
  }
  @media (max-width: 640px) {
    .ats-group {
      cursor: pointer;
      pointer-events: auto;
      font-size: var(--fs-sm);
    }
    .ats-slot { display: grid; }
    .ats-now, .ats-refresh { grid-area: 1 / 1; }
    .ats-now, .ats-refresh {
      transition: opacity 0.15s ease;
    }
    .ats-mobile-hide {
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.15s ease;
    }
    .ats-sep { display: none; }
  }
</style>
