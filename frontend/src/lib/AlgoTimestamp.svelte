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
     `flex-wrap: nowrap` (2026-10-23 — was `wrap`; a wrapped second row
     covered the page's first card underneath the fixed-position header)
     with its other two children (`.algo-title-group`, `.page-header-actions`)
     both pinned via `flex-shrink: 0`, so `.ats-root` is the ONE child
     designed to absorb a tight row. Under the OLD `wrap` parent, the
     browser's first resort when content overflowed was to wrap the whole
     row onto a second line rather than shrink any item — `.ats-root`'s
     default `flex-shrink: 1` never got a chance to engage. Under `nowrap`
     there is no wrap fallback, so that same default shrink now does the
     job; `.ats-root` only needed an explicit `min-width: 0` (overriding
     the flex item's automatic minimum, which would otherwise floor at the
     content's own min-content width) for the shrink to go low enough to
     matter. `flex-grow` is deliberately left at its default `0`, not `1`
     — see the CSS rule's own comment for why a grow value here would
     wrongly steal space from an `ml-auto` spacer some host pages render
     after this component. `.ats-scroll`'s `overflow-x: auto` lets the
     clock + badge group scroll horizontally inside the shrunk box instead
     of being clipped. -->
<div class="ats-root">
  <div class="ats-scroll scroll-x">
    <!-- Market-status badge — rendered BEFORE the clock (operator:
         "move market open badge before timestamp"), as a plain <span>,
         not inside the clock button, so it never inherits the button's
         tap-to-toggle behaviour or its desktop `pointer-events: none`.
         Styled as plain colored text, not a button/chip (operator:
         "don't decorate it like button. make it look like normal
         text.") — no background/border/padding/radius, font-size
         inherits from the surrounding timestamp text; only the text
         colour carries the open/closed signal. Still a real click
         target (cursor: pointer, role=button) since it opens the
         InfoHint tooltip. Label switches between M / MCX / EQ depending
         on the compound 4-bucket state. Tooltip is the InfoHint
         hover-preview/click-to-pin popover (hideButton+anchor onto this
         span itself), matching PositionStrip's P/M/C/H convention — not
         a native `title` attribute. The span's own click/Enter/Space
         toggles the pin directly (its own `onclick`, not InfoHint's —
         hideButton mode renders no button of InfoHint's own to attach
         one to), so touch devices (no hover) can still reach the
         tooltip. -->
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
  </div>
</div>

<style>
  .ats-root {
    display: flex;
    align-items: center;
    /* Explicit `0 1 auto` (shrink, no grow, natural basis) — NOT `1 1 0`.
       A `flex-grow: 1` here would make `.ats-root` compete with the
       `ml-auto` spacer some host pages render between it and
       `.page-header-actions` (e.g. `/admin`'s "+ Create User",
       `/admin/tokens`'s "+ New token" — see each page's own "Content-
       action button is LEFT-aligned per canonical header rule" comment):
       a flex-grow item claims free space during the flexible-length
       resolution pass, before `margin-left: auto` ever gets a turn,
       which would drag that content button all the way over to sit
       flush against the action icons instead of staying left-aligned
       right after the timestamp. `shrink: 1` is enough on its own under
       `.page-header`'s `nowrap` — see the template comment above for why
       it didn't engage under the old `wrap`. */
    flex: 0 1 auto;
    min-width: 0;
    overflow: hidden;
  }
  .ats-scroll {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    overflow-y: hidden;
    overscroll-behavior-x: contain;
  }
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
    /* Plain text, not a button/chip (operator: "don't decorate it like
       button. make it look like normal text.") — no background, border,
       radius, or padding; font-size inherits from the surrounding
       timestamp text instead of a dedicated chip size. Colour alone
       carries the open/closed signal, same palette as before. Still a
       real click target (role=button, opens the InfoHint tooltip), so
       cursor stays `pointer` as the one remaining interactive affordance. */
    display: inline;
    white-space: nowrap;
    cursor: pointer;
  }
  .ats-mkt-open   { color: var(--algo-green-text); }
  .ats-mkt-closed { color: var(--algo-dim); }
  .ats-mkt-mcx-only { color: var(--c-action); }
  .ats-mkt-eq-only  { color: var(--algo-market-eq-only-text); }
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
