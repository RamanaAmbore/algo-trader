<script>
  /**
   * OrderBook — standalone order card feed extracted from LogPanel's
   * `order` tab. Renders OrderCard rows with Cancel / Modify / Reconcile
   * actions. Polls fetchOrders() + fetchAlgoOrdersRecent() on the same
   * visibleInterval cadence LogPanel uses.
   *
   * Props:
   *   orderId?       — when set, narrows display to rows matching this id
   *   accountFilter? — optional external account filter (bindable)
   *   title?         — header label (default 'Order Book')
   *   pollMs?        — polling cadence in ms (default 5000, matching the
   *                    `polling.fast_ms` registry setting's own default —
   *                    see backend/shared/helpers/settings.py. A static
   *                    literal, not a settings-driven read: this
   *                    component's onMount creates its visibleInterval
   *                    synchronously, before any async settings fetch
   *                    could resolve — the same race (algo)/+layout.svelte
   *                    had to explicitly re-arm around for the fill-watch
   *                    backstop (2026-10-02). True settings-driven wiring
   *                    for this cadence would need a getFastPollMs()/
   *                    setFastPollMs() pair in marketDataStores.svelte.js
   *                    (out of scope for this change) plus the same
   *                    re-arm-on-resolve treatment.
   *   statusFilter?  — 'chase'|'open'|'complete'|'rejected_cancelled' —
   *                    INERT for the no-chip-clicked default (2026-09-30):
   *                    both existing call sites pass 'open' as a literal
   *                    default, indistinguishable from not passing it at
   *                    all, so it can no longer double as "the default
   *                    filter" without defeating the first-non-zero-chip
   *                    default below. Kept declared (back-compat, still
   *                    type-checked at call sites) but no longer read by
   *                    `_activeStatus` — see `_defaultActiveId`. The 'gtt'
   *                    variant was removed (2026-11, this CARD's own
   *                    standalone broker-GTT chip/viewer removed per
   *                    operator: "we don't need gtt count status pills")
   *                    — `orders_gtt.py`'s route is NOT orphaned by this:
   *                    `LogPanel.svelte` still calls the `fetchGtts` API
   *                    and feeds it through `matchGtts` for the order
   *                    log's independent `'gtt_broker'` event line (a
   *                    different UI surface for the same "any broker
   *                    GTT, regardless of origin" concept). Only THIS
   *                    card's chip/card-grid presentation of it is gone.
   */
  import { onMount, onDestroy, untrack } from 'svelte';
  import { visibleInterval, formatDualTz, withGuard } from '$lib/stores';
  import { fetchAlgoOrdersRecent, cancelOrder, reconcileSingleOrder, fetchOrderEventsById } from '$lib/api';
  import { ordersStore } from '$lib/data/marketDataStores.svelte.js';
  import { priceFmt } from '$lib/format';
  import { acctColor } from '$lib/account';
  import OrderCard from '$lib/order/OrderCard.svelte';
  import OrderTimelineDrawer from '$lib/order/OrderTimelineDrawer.svelte';
  import { resolveOrderId, buildOrderContextEntry, buildLinkedOrders } from '$lib/order/orderTimelineLogic.js';
  import ChartModal from '$lib/ChartModal.svelte';
  import SymbolPanel from '$lib/SymbolPanel.svelte';
  import SymbolContextMenu from '$lib/SymbolContextMenu.svelte';
  import CardHeader from '$lib/CardHeader.svelte';
  import { toast } from '$lib/data/toastStore.svelte.js';
  import { isDraftOrderRow } from '$lib/data/draftOrderRow.js';
  import { groupPropFor } from '$lib/data/pulseDetached.svelte.js';
  import { isSymbolInWatchlist } from '$lib/data/watchlistContextActions.js';
  import { isSharedContextAction, dispatchSharedContextAction } from '$lib/data/symbolContextDispatch.js';

  /** @type {{
   *   orderId?: string | null,
   *   accountFilter?: string[],
   *   title?: string,
   *   pollMs?: number,
   *   statusFilter?: 'chase'|'open'|'complete'|'rejected_cancelled',
   *   onSymbolClick?: ((ord: any) => void) | null,
   *   isFullscreen?: boolean,
   * }} */
  let {
    orderId       = null,
    accountFilter = /** @type {string[]} */ ([]),
    title         = 'Order Book',
    pollMs        = 5000,
    statusFilter  = /** @type {'chase'|'open'|'complete'|'rejected_cancelled'} */ ('open'),
    onSymbolClick = /** @type {((ord: any) => void) | null} */ (null),
    isFullscreen  = $bindable(false),
  } = $props();

  // ── Data (B1 SWR extension, PLAN.md Part B) ────────────────────────────
  // `orderRows`/`_loading` used to be local $state assigned from this
  // component's own `_loadOrders()` merge. Both the merge logic (broker +
  // algo dedup, chase cancel-and-replace supersede tracking, session-
  // boundary filter) and the fetch itself are now owned by the shared
  // `ordersStore` singleton (marketDataStores.svelte.js) so every mount of
  // this component shares one poll AND paints last-good rows immediately
  // on reload instead of "Loading…" — see that module's own "Orders"
  // section header comment for the full rationale. LogPanel.svelte keeps
  // its own independent order merge (its Orders/Console tabs) — unaffected
  // by this; see that file's own Tier-2 hydrate-and-persist pair (PLAN.md
  // B2) for its treatment of the identical reload-blank problem.
  const orderRows = $derived(ordersStore.value ?? []);
  // `_loading` here means "we have no data yet to show" (cold start with
  // no Tier-2 cache hit, first fetch still pending), NOT "a fetch happens
  // to be in flight right now". `ordersStore.loading` flips `true` at the
  // START of EVERY poll (createDataStore's own `_fetch`, success or not)
  // and back to `false` once it lands — using it directly here would
  // blank the grid to "Loading…" on every single ~5s poll tick (via
  // `_defaultActiveId`'s own `_loading`-gated branch below, which would
  // resolve to `null` → `_applyStatusFilter` returns `[]`), and would ALSO
  // hide an already-hydrated Tier-2 cache hit behind "Loading…" for the
  // brief window between mount and the first fetch resolving — exactly
  // the blank-on-reload problem B1 exists to remove. `ordersStore.value`
  // stays non-null (even an empty array) once ANY fetch has ever
  // succeeded OR failed (stale-while-error keeps the prior value, never
  // resets to null), so this mirrors the original local `_loading`
  // $state's one-shot-false-forever behaviour exactly.
  //
  // `!ordersStore.meta.fetchFailed` (not just `!ordersStore.error`) —
  // `_error` is reset to `null` SYNCHRONOUSLY at the start of every poll
  // attempt (dataStore.svelte.js's `_fetch`), including a retry during a
  // sustained cold-start outage with no cache — without this,
  // `value==null && !error` would flip back `true` for the duration of
  // every retry, flickering the grid between "Loading…" and "No orders
  // today" each poll cycle. `meta.fetchFailed` stays `true` across
  // repeated failures (only cleared by the NEXT success — see
  // markFetchFailedMeta's own doc in dataStore.svelte.js), so once a
  // genuine failure has been recorded, this gate stays stable at `false`.
  const _loading  = $derived(ordersStore.value == null && !ordersStore.error && !ordersStore.meta.fetchFailed);

  // `_internalStatus` is set the moment the operator clicks a chip and
  // stays sticky (exclusive single-status filter) from then on — unchanged
  // behaviour. While it's still null (nothing clicked yet this session),
  // `_activeStatus` resolves via `_defaultActiveId` below instead of the
  // (now-inert) `statusFilter` prop — see its JSDoc above for why.
  let _internalStatus = $state(/** @type {string|null} */ (null));

  // ── Chase cancel-and-replace dedup / row-timestamp / session-boundary
  // filter — all moved to the shared `ordersStore` fetcher in
  // marketDataStores.svelte.js (B1 SWR extension). See that module's
  // `_fetchOrdersMerged`/`isOrderSuperseded`/`_ordersRowTsMs`/
  // `_ordersIsCurrentSessionRow` for the ported-verbatim logic — this
  // component no longer needs its own copy of any of it.

  /**
   * Trigger a background refresh of the shared `ordersStore`.
   * `opts.force=true` bypasses createDataStore's in-flight dedup — used
   * by every MANUAL trigger (CardHeader's refresh button, the post-
   * cancel/post-reconcile reloads below) so an operator-initiated
   * refresh is never silently coalesced into an already-in-flight
   * routine poll's result — mirrors this function's pre-B1 contract,
   * where the raw (unguarded) `_loadOrders()` always ran a genuinely
   * fresh fetch regardless of any poll tick in flight. The routine poll
   * tick (onMount/visibleInterval below) deliberately omits `force` —
   * `createDataStore`'s own in-flight dedup means overlapping ticks are
   * free, no second guard needed.
   * @param {{force?: boolean}} [opts]
   */
  async function _loadOrders(opts) {
    await ordersStore.load(opts);
  }

  // ── Per-order timeline drawer ───────────────────────────────────────
  // A LOCAL instance of OrderTimelineDrawer scoped to one order at a
  // time — distinct from (algo)/+layout.svelte's navbar chase-chip
  // drawer, which shows an aggregate feed across every actively-chasing
  // order via a different endpoint (/events/recent?status=open). Opened
  // by clicking an OrderCard's body (not its action buttons — those
  // stopPropagation already).
  let _timelineOpen    = $state(false);
  let _timelineOrders  = $state(/** @type {any[]} */ ([]));
  let _timelineContext = $state(/** @type {Record<string, {symbol:string, side:string, qty:number|null, mode:string}>} */ ({}));
  let _timelineLinked  = $state(/** @type {{parent_order_id?: number|null, child_order_ids?: number[], basket_tag?: string|null} | null} */ (null));

  /** Opens the drawer for `orderOrId` — either a full row (card click)
   *  or a bare id (linked-chip navigation, see onSelectLinked below).
   *  Looks the id up against the already-loaded `orderRows` first so a
   *  linked parent/child chip shows that order's own real symbol/side/
   *  qty/mode/links, not an empty "unknown" context — `orderRows` is the
   *  full merged broker+algo list (not status-filtered), so a linked
   *  order is present there even when it's hidden by the current status
   *  chip. Falls back to the bare id when it isn't found (e.g. a linked
   *  order outside today's session window). */
  async function _openOrderTimeline(/** @type {any} */ orderOrId) {
    const id = (orderOrId && typeof orderOrId === 'object')
      ? resolveOrderId(orderOrId)
      : orderOrId;
    if (id == null) return;
    const full = orderRows.find(o => String(resolveOrderId(o)) === String(id)) || orderOrId;
    _timelineContext = buildOrderContextEntry(full, id);
    _timelineLinked  = buildLinkedOrders(full);
    _timelineOpen    = true;
    try {
      _timelineOrders = await fetchOrderEventsById(id);
    } catch (_) {
      _timelineOrders = [];
    }
  }

  function _downloadCsv() {
    const rows = filteredOrderRows;
    if (!rows.length) return;
    const headers = ['order_id','symbol','exchange','transaction_type','quantity','price','status','timestamp'];
    const csv = [
      headers.join(','),
      ...rows.map(r => [
        JSON.stringify(r.order_id    ?? r.id           ?? ''),
        JSON.stringify(r.tradingsymbol ?? r.symbol     ?? ''),
        JSON.stringify(r.exchange    ?? ''),
        JSON.stringify(r.transaction_type ?? ''),
        JSON.stringify(r.quantity    ?? ''),
        JSON.stringify(r.price       ?? r.fill_price   ?? r.initial_price ?? ''),
        JSON.stringify(r.status      ?? ''),
        JSON.stringify(r.order_timestamp ?? r.created_at ?? ''),
      ].join(','))
    ].join('\n');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
    a.download = 'orders.csv';
    a.click();
  }

  // ── Interval management ───────────────────────────────────────────────
  /** @type {Array<() => void>} */
  const _intervals = [];

  onMount(() => {
    // In-flight guard on the poll cadence (audit fix, 2026-10-02) — a slow
    // response (network blip, broker lag) could previously still be
    // in-flight when the next visibleInterval tick fired, stacking a
    // second concurrent fetch for the same resource. Mirrors LogPanel's
    // `_every()` (LogPanel.svelte:392-404): the immediate call and every
    // interval tick share the SAME guarded instance. Belt-and-suspenders
    // now that `_loadOrders` delegates to the shared `ordersStore` (B1 —
    // see marketDataStores.svelte.js): `createDataStore`'s own in-flight
    // dedup already makes two overlapping, non-forced `.load()` calls
    // share one fetch for free, so this wrapper is mostly redundant but
    // harmless to keep.
    //
    // Manual triggers (_cancelRow/_reconcileRow's post-action reload,
    // CardHeader's onRefresh button) deliberately keep calling
    // `_loadOrders({ force: true })` below, NOT this guarded wrapper —
    // an operator-initiated refresh must never be silently coalesced into
    // an already-in-flight routine poll's result just because a tick
    // happens to be in-flight at that instant.
    const _guardedLoadOrders = withGuard(_loadOrders);
    _guardedLoadOrders();
    if (pollMs > 0 && typeof document !== 'undefined') {
      const teardown = visibleInterval(() => { _guardedLoadOrders(); }, pollMs);
      _intervals.push(teardown);
    }
  });

  onDestroy(() => {
    for (const teardown of _intervals) teardown();
  });

  // ── Filter predicates (mirrors LogPanel exactly) ───────────────────────
  /** @type {Record<string, (st: string) => boolean>} */
  const _STATUS_PREDICATES = {
    open:      st => st === 'OPEN' || st === 'TRIGGER PENDING' || st === 'TRIGGER_PENDING',
    // 'COMPLETE' = broker (Kite) vocabulary, 'FILLED' = AlgoOrder vocabulary
    // (ALGO_ORDER_FINAL_STATUSES in backend/api/models.py) — algo-only fills
    // (paper/sim/replay/shadow, or an algo-tracked live order before the
    // broker's own COMPLETE status lands) never carry 'COMPLETE'.
    complete:  st => st === 'COMPLETE' || st === 'FILLED',
    // Rejected + Cancelled merged into one chip (2026-09-30, operator
    // instruction) — single combined predicate, single chip. UNFILLED
    // joined here too (audit fix, 2026-10): it's a terminal give-up with
    // no further position effect (strategies.py already treats it as
    // "closed" alongside REJECTED/CANCELLED), and `_isInFlight` below
    // deliberately does NOT offer Reconcile for it — this file already
    // treats UNFILLED as resolved, not as something still needing
    // operator attention the way HELD/CANCEL_FAILED do (see `held` below).
    rejected_cancelled: st => st === 'REJECTED' || st === 'CANCELLED' || st === 'UNFILLED',
    // Held/Attention (audit fix, 2026-10) — HELD (an operator-review hold,
    // `order_hold_gate.py`) and CANCEL_FAILED (a cancel request failed —
    // the order MAY STILL BE LIVE AT THE BROKER) matched NO predicate
    // above and were completely invisible in this view before this fix,
    // only ever shown in HeldOrdersCard's own (HELD-only) list. Grouped
    // together because `backend/api/models.py`'s ALGO_ORDER_FINAL_STATUSES
    // excludes both from "truly final" for the identical reason: a late
    // event can still change the outcome, so a possibly-still-live,
    // unprotected order must never be hidden from the main order book.
    held: st => st === 'HELD' || st === 'CANCEL_FAILED',
  };

  /**
   * "Chase in flight" = still working (OPEN/TRIGGER PENDING) AND the
   * chase engine has cancelled-and-replaced it at least once
   * (`attempts > 0`). A plain resting order the chase engine has never
   * touched doesn't count — mirrors `OrderCard.svelte`'s existing
   * `chase:#N` chip precedent (`order.attempts != null && order.attempts > 0`).
   */
  function _isChaseInFlight(/** @type {any} */ o) {
    if (!_STATUS_PREDICATES.open((o?.status || '').toUpperCase())) return false;
    return Number(o?.attempts || 0) > 0;
  }

  function _applyAccountFilter(rows, /** @type {string[]} */ filter) {
    if (!filter || filter.length === 0) return rows;
    const want = new Set(filter);
    return rows.filter(o => want.has(String(o?.account || '')));
  }

  function _applyStatusFilter(rows, /** @type {string|null|undefined} */ filter) {
    // `null` means "no chip resolved" (nothing clicked AND every chip's
    // count is zero) — show nothing, not everything, matching "show only
    // that chip's rows" (there's no chip to attribute `rows` to). The old
    // LogPanel-style "falsy filter = show all" convention only ever fired
    // here when `_activeStatus` was unresolved, which never happened pre-
    // 2026-09-30 (the `statusFilter` prop always supplied a real value).
    if (filter == null) return [];
    if (filter === 'chase') return rows.filter(_isChaseInFlight);
    const pred = _STATUS_PREDICATES[filter];
    if (!pred) return rows;
    return rows.filter(o => pred((o?.status || '').toUpperCase()));
  }

  function _applyOrderIdFilter(rows, /** @type {string|null} */ id) {
    if (!id) return rows;
    return rows.filter(o => {
      const oid = String(o?.order_id || o?.id || '');
      return oid === String(id);
    });
  }

  const filteredOrderRows = $derived.by(() => {
    let rows = orderRows || [];
    rows = _applyAccountFilter(rows, accountFilter);
    rows = _applyStatusFilter(rows, _activeStatus);
    rows = _applyOrderIdFilter(rows, orderId);
    return rows;
  });

  // ── Cancel / Modify / Reconcile actions (mirrors LogPanel) ────────────
  /** @type {Set<string>} */
  let _cancelling  = $state(new Set());
  /** @type {Set<string>} */
  let _reconciling = $state(new Set());
  /** @type {string} */
  let _cancelErr   = $state('');

  /**
   * Returns true when the order is an OPEN broker order that can be acted
   * on (Modify/Cancel). Sprint 2a fix — this used to test `!o?.mode` as a
   * proxy for "is this a bare broker row" (broker rows carried no `mode`
   * field, algo rows always did), which happened to work only because
   * nothing else about a broker row's shape had changed yet. Now that
   * `AlgoOrderInfo` rows carry more surfaced fields (`broker_order_id`,
   * `source`, `agent_id`) — and any future broker-row enrichment could
   * just as easily add a `mode`-shaped field of its own — an implicit
   * "absence of a field" test is the wrong long-term signal for origin.
   * `_rowOrigin` is stamped explicitly in `_loadOrders` at the one point
   * each row's real source (fetchOrders() vs fetchAlgoOrdersRecent()) is
   * known, so this check never depends on which fields happen to be
   * present or absent on either shape.
   */
  function _isOpenBroker(/** @type {any} */ o) {
    const st = (o?.status || '').toUpperCase();
    return (st === 'OPEN' || st === 'TRIGGER PENDING' || st === 'TRIGGER_PENDING') && o?._rowOrigin === 'broker';
  }

  /** Returns true when the order is in-flight and reconciling is meaningful.
   *  Excludes drafts (2026-10 round 2) — a draft row's status is always
   *  'OPEN' but it never reached a broker, so `reconcileSingleOrder`
   *  would be called with no real broker_order_id to reconcile against. */
  function _isInFlight(/** @type {any} */ o) {
    if (isDraftOrderRow(o)) return false;
    const st = (o?.status || '').toUpperCase();
    return st === 'OPEN' || st === 'TRIGGER PENDING'
        || st === 'CANCEL_FAILED' || st === 'PARTIAL';
  }

  async function _cancelRow(/** @type {any} */ o) {
    const key = String(o.order_id || o.id || '');
    if (!key || _cancelling.has(key)) return;
    // Stale-broker-id guard (plan: starry-tickling-dolphin, frontend
    // item 3) — `mark_killed`-driven Cancel is keyed by broker_order_id;
    // if chase has already cancelled-and-replaced this exact id since the
    // POLL that rendered this Cancel button, sending Cancel now would
    // target a dead order while chase keeps working the new one.
    //
    // Re-checking `isOrderSuperseded`'s underlying data (marketDataStores.
    // svelte.js's `_ordersBackendSupersededIds`/`_ordersSupersededBrokerIds`)
    // against that SAME poll's state (the one `brokerRowsVisible` already
    // used to decide this row belongs in the grid at all) can never fire
    // here — a row only reaches a clickable Cancel button once that exact
    // check has already passed for the current poll. The race this guards
    // against happens strictly AFTER that poll and BEFORE the click, so
    // this deliberately does its OWN independent re-fetch of
    // `fetchAlgoOrdersRecent` right now (NOT a call to `isOrderSuperseded`
    // itself, which only ever reflects the LAST poll's snapshot) instead of
    // trusting anything already sitting in the shared `ordersStore`'s
    // last-polled value. `o._algoId` (stamped onto the merged row by
    // `ordersStore`'s fetcher) is how this row's algo counterpart is
    // re-resolved against that fresh fetch.
    const _oid = String(o?.order_id || '');
    if (_oid && o?._algoId != null) {
      try {
        const _fresh = await fetchAlgoOrdersRecent(100, 'all');
        const _freshRows = Array.isArray(_fresh) ? _fresh : [];
        const _freshRow = _freshRows.find(r => r?.id === o._algoId);
        const _freshBid = _freshRow ? String(_freshRow?.broker_order_id || '') : '';
        const _movedOn = !!_freshRow && !!_freshBid && _freshBid !== _oid;
        const _freshSuperseded = _freshRows.some(r =>
          Array.isArray(r?.superseded_broker_order_ids)
          && r.superseded_broker_order_ids.map(String).includes(_oid));
        if (_movedOn || _freshSuperseded) {
          toast.warning('Order superseded — refresh to retry', { timeoutMs: 4000 });
          await _loadOrders({ force: true });
          return;
        }
      } catch (_) {
        // The re-fetch itself failed (network blip) — a safety check must
        // never become the reason a genuine, needed cancel never goes
        // out. Fall through and proceed with the cancel attempt as if
        // the guard had passed.
      }
    }
    _cancelling = new Set([..._cancelling, key]);
    _cancelErr = '';
    try {
      await cancelOrder(o.order_id, o.account, o.variety || 'regular');
      await _loadOrders({ force: true });
    } catch (e) {
      _cancelErr = /** @type {any} */ (e)?.message || 'cancel failed';
      setTimeout(() => { _cancelErr = ''; }, 3000);
    } finally {
      const next = new Set(_cancelling);
      next.delete(key);
      _cancelling = next;
    }
  }

  function _requestModify(/** @type {any} */ o, /** @type {HTMLElement | null} */ el) {
    el?.dispatchEvent(new CustomEvent('lp:modify-order', {
      detail: o,
      bubbles: true,
      composed: true,
    }));
  }

  async function _reconcileRow(/** @type {any} */ o) {
    const key = String(o.order_id || o.id || '');
    if (!key || !o?.account || _reconciling.has(key)) return;
    _reconciling = new Set([..._reconciling, key]);
    try {
      const res = await reconcileSingleOrder(o.order_id, o.account);
      if (res?.updated) await _loadOrders({ force: true });
    } catch (e) {
      _cancelErr = /** @type {any} */ (e)?.message || 'reconcile failed';
      setTimeout(() => { _cancelErr = ''; }, 3000);
    } finally {
      const next = new Set(_reconciling);
      next.delete(key);
      _reconciling = next;
    }
  }

  // ── Status counts (computed once per render, not 5× inline) ─────────
  // Reads the ACCOUNT-FILTERED rows (not raw orderRows) — already
  // session-boundary-filtered by the shared `ordersStore` fetcher (see
  // marketDataStores.svelte.js) — so every count here is
  // scoped to today's session AND the current account filter, matching
  // the grid it labels. Without this, selecting an account could default-highlight a chip
  // (e.g. Chase) whose only rows belong to a DIFFERENT account, showing
  // an empty list underneath.
  const _countScopedOrderRows = $derived.by(() => _applyAccountFilter(orderRows, accountFilter));
  // Displayed chip badge numbers (2026-10 round 2) — exclude drafts from
  // every count (`mode==='draft'`, status always 'OPEN'). A draft never
  // reached a broker, so it must not inflate "Open" (or any other chip)
  // the way a genuine resting/filled/rejected order does. Relabel, don't
  // hide: `_applyStatusFilter`/`_STATUS_PREDICATES` below are deliberately
  // UNCHANGED, so a draft row still renders inside whichever chip's list
  // its real status naturally routes it to (see OrderCard.svelte for the
  // DRAFT pill that replaces its status badge there) — only the NUMBER
  // shown on the chip excludes it.
  const _statusCounts = $derived.by(() => ({
    held:                _countScopedOrderRows.filter(o => !isDraftOrderRow(o) && _STATUS_PREDICATES.held((o.status || '').toUpperCase())).length,
    chase:               _countScopedOrderRows.filter(_isChaseInFlight).length,
    open:                _countScopedOrderRows.filter(o => !isDraftOrderRow(o) && _STATUS_PREDICATES.open((o.status || '').toUpperCase())).length,
    complete:            _countScopedOrderRows.filter(o => !isDraftOrderRow(o) && _STATUS_PREDICATES.complete((o.status || '').toUpperCase())).length,
    rejected_cancelled:  _countScopedOrderRows.filter(o => !isDraftOrderRow(o) && _STATUS_PREDICATES.rejected_cancelled((o.status || '').toUpperCase())).length,
  }));

  // Display counts (draft-EXCLUDED, same as `_statusCounts`), for
  // the FIRST default-resolution pass below.
  const _displayCountsById = $derived.by(() => ({ ..._statusCounts }));

  // Raw (draft-INCLUSIVE) counts — second-pass FALLBACK for default-chip
  // resolution only, never for the displayed badge numbers. Without this
  // fallback, an account whose only "open-shaped" rows are drafts would
  // resolve `_defaultActiveId` to null (every chip reads 0 once drafts
  // are excluded from `_statusCounts`/`_displayCountsById`), hiding the
  // draft row entirely on page load — the same "keep visible"
  // requirement the count fix above must not undo.
  const _rawCountsById = $derived.by(() => ({
    held:               _countScopedOrderRows.filter(o => _STATUS_PREDICATES.held((o.status || '').toUpperCase())).length,
    chase:              _statusCounts.chase,
    open:               _countScopedOrderRows.filter(o => _STATUS_PREDICATES.open((o.status || '').toUpperCase())).length,
    complete:           _countScopedOrderRows.filter(o => _STATUS_PREDICATES.complete((o.status || '').toUpperCase())).length,
    rejected_cancelled: _countScopedOrderRows.filter(o => _STATUS_PREDICATES.rejected_cancelled((o.status || '').toUpperCase())).length,
  }));

  // ── Default chip resolution (2026-09-30, extended 2026-10 for Held) ────
  // Fixed display order for the 5 chips. When nothing has been explicitly
  // clicked (`_internalStatus === null`), the SINGLE chip highlighted +
  // shown is the first one in this order whose count is non-zero — not a
  // union of every non-zero chip. `_statusCounts` is `$derived`, so
  // `_countsById` and `_defaultActiveId` recompute automatically on every
  // poll tick/status change with no extra wiring — e.g. Open emptying
  // while Filled still has rows moves the highlight+list to Filled on the
  // very next tick. `held` sits FIRST — a possibly-still-live, unprotected
  // order (HELD / CANCEL_FAILED) outranks every other bucket for default
  // visibility.
  const CHIP_ORDER = ['held', 'chase', 'open', 'complete', 'rejected_cancelled'];
  // Gated on `_loading` (the ORDERS fetch flag) — `_loading` clears only
  // once `_loadOrders` has run at least once (success or freeze-on-
  // failure), by which point these counts are trustworthy for picking
  // the first-non-zero chip.
  //
  // Two passes (2026-10 round 2): the FIRST pass reads `_displayCountsById`
  // (draft-excluded) so a draft sitting in an otherwise-empty "Open"
  // bucket never steals the default highlight away from a REAL non-zero
  // bucket elsewhere (e.g. 3 genuinely Filled orders + 1 draft must
  // default to "Filled", not an "Open" chip whose badge reads 0). The
  // SECOND pass falls back to `_rawCountsById` (draft-inclusive) only
  // when every display count is zero — the draft-only, no-real-orders
  // case — so the draft row still gets a default chip to render under
  // instead of resolving to null.
  const _defaultActiveId = $derived(_loading ? null : (
    CHIP_ORDER.find(id => _displayCountsById[id] > 0)
    ?? CHIP_ORDER.find(id => _rawCountsById[id] > 0)
    ?? null
  ));

  // Explicit click wins (sticky, exclusive filter — unchanged behaviour);
  // otherwise fall back to the single first-non-zero chip above. Never
  // reads the `statusFilter` prop (see its JSDoc).
  const _activeStatus = $derived(_internalStatus ?? _defaultActiveId);

  // ── Symbol panel / chart modal / context menu state ───────────────────
  let _symPanelSym  = $state('');
  let _symPanelExch = $state('');
  /** @type {{ symbol: string, exchange: string, x: number, y: number, watchlistInfo?: { inWatchlist: boolean } | null } | null} */
  let _ctxMenu = $state(null);
  /** @type {'place-order' | 'chart' | null} */
  let _ctxAction = $state(null);
  let _ctxSym  = $state('');
  let _ctxExch = $state('');

  /** Open the shared context menu, then resolve watchlist membership
   *  async (2026-10-22 parity — this surface has no watchlist state of
   *  its own, unlike MarketPulse). `watchlist` prop stays null until
   *  resolved, which hides Add/Remove until then. */
  function _openOrderBookCtxMenu(symbol, exchange, x, y) {
    _ctxMenu = { symbol, exchange, x, y, watchlistInfo: null };
    isSymbolInWatchlist(symbol).then((inWatchlist) => {
      if (_ctxMenu && _ctxMenu.symbol === symbol) {
        _ctxMenu = { ..._ctxMenu, watchlistInfo: { inWatchlist } };
      }
    });
  }
</script>

<div class="ob-root" class:fs-card-on={isFullscreen}>

<!-- Header -->
<CardHeader title={title} showSearch={false} bind:isFullscreen
  detectOverflow={false}
  onRefresh={() => { _loadOrders({ force: true }); }}
  onDownload={_downloadCsv}
>
  {#snippet left()}
    <span class="ob-count">{filteredOrderRows.length} order{filteredOrderRows.length !== 1 ? 's' : ''}</span>
  {/snippet}
</CardHeader>

<!-- Order card grid -->
  <div class="ob-status-bar scroll-x">
    {#each [
      // Item 3 (2026-11-02, PLAN.md) — "Held" → "Pending" for the
      // template-exit-hold mechanism (order_hold_gate.py); "Attn" (the
      // CANCEL_FAILED half of this combined chip) is unaffected — a
      // failed cancel is a genuine attention-needed state, not a
      // release-pending one.
      { id: 'held',               label: 'Pending/Attn',       status: 'held',    count: _statusCounts.held },
      { id: 'chase',              label: 'Chase',              status: 'chase',   count: _statusCounts.chase },
      { id: 'open',               label: 'Open',               status: 'running', count: _statusCounts.open },
      { id: 'complete',           label: 'Filled',             status: 'active',  count: _statusCounts.complete },
      { id: 'rejected_cancelled', label: 'Rejected/Cancelled', status: 'error',   count: _statusCounts.rejected_cancelled },
    ] as f}
      <button type="button" class="ob-sc" class:is-active={_activeStatus === f.id}
        data-status={f.status}
        onclick={() => { _internalStatus = f.id; }}>
        <span class="ob-sc-n">{f.count}</span>
        <span class="ob-sc-l">{f.label}</span>
      </button>
    {/each}
  </div>
<div class="ob-scroll">
  {#if filteredOrderRows.length}
    <div class="oc-book-grid">
      {#each filteredOrderRows as o (o.order_id ?? o.id)}
        {@const _oKey = String(o.order_id || o.id || '')}
        <OrderCard order={o}
          onSymbolClick={(ord) => {
            if (onSymbolClick) { onSymbolClick(ord); return; }
            _symPanelSym = ord.tradingsymbol || ord.symbol || '';
            _symPanelExch = ord.exchange || '';
          }}
          onSymbolContext={(ord, e) => { _openOrderBookCtxMenu(ord.tradingsymbol || ord.symbol || '', ord.exchange || '', /** @type {MouseEvent} */ (e).clientX, /** @type {MouseEvent} */ (e).clientY); }}
          onCardClick={_openOrderTimeline}>
          {#snippet actions(ord)}
            <div class="lp-oc-actions" role="group" aria-label="Order actions">
              {#if _isOpenBroker(ord)}
                <button type="button" class="lp-oc-btn lp-oc-modify"
                  title="Modify order"
                  aria-label="Modify"
                  onclick={(e) => { e.stopPropagation(); _requestModify(ord, e.currentTarget?.closest('.ob-scroll')); }}>
                  <svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                    <path d="M11.5 2.5l2 2L5 13H3v-2L11.5 2.5z" stroke="currentColor" stroke-width="1.6"
                          stroke-linecap="round" stroke-linejoin="round"/>
                  </svg>
                </button>
                <button type="button" class="lp-oc-btn lp-oc-cancel"
                  title="Cancel order"
                  aria-label="Cancel"
                  disabled={_cancelling.has(_oKey)}
                  onclick={(e) => { e.stopPropagation(); _cancelRow(ord); }}>
                  <svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                    <path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.8"
                          stroke-linecap="round"/>
                  </svg>
                </button>
              {/if}
              {#if _isInFlight(ord)}
                <button type="button" class="lp-oc-btn lp-oc-reconcile"
                  title="Reconcile with broker"
                  aria-label="Reconcile"
                  disabled={_reconciling.has(_oKey)}
                  onclick={(e) => { e.stopPropagation(); _reconcileRow(ord); }}>
                  <svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                    <path d="M3 8a5 5 0 0 1 8.6-3.5M13 8a5 5 0 0 1-8.6 3.5"
                      stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
                    <path d="M11.5 2v3h-3M4.5 14v-3h3"
                      stroke="currentColor" stroke-width="1.5"
                      stroke-linecap="round" stroke-linejoin="round" />
                  </svg>
                </button>
              {/if}
            </div>
          {/snippet}
        </OrderCard>
      {/each}
      {#if _cancelErr}
        <div class="log-row log-agent-failed">{_cancelErr}</div>
      {/if}
    </div>
  {:else}
    <div class="log-debug py-2 text-center">
      {#if _loading}
        Loading…
      {:else if orderRows.length > 0}
        No orders match the current filters.
      {:else}
        No orders today.
      {/if}
    </div>
  {/if}
</div>

{#if _symPanelSym && !onSymbolClick}
  <SymbolPanel
    symbol={_symPanelSym}
    exchange={_symPanelExch}
    onSubmit={() => {}}
    onClose={() => { _symPanelSym = ''; _symPanelExch = ''; }}
  />
{/if}

{#if _ctxMenu}
  <SymbolContextMenu
    symbol={_ctxMenu?.symbol}
    exchange={_ctxMenu?.exchange}
    x={_ctxMenu?.x}
    y={_ctxMenu?.y}
    showOpenInOptions
    showCopySymbol
    group={groupPropFor(_ctxMenu?.symbol)}
    watchlist={_ctxMenu?.watchlistInfo ?? null}
    onClose={() => { _ctxMenu = null; }}
    onAction={(action, sym, exch) => {
      if (isSharedContextAction(action)) {
        dispatchSharedContextAction(action, sym, exch);
        _ctxMenu = null;
        return;
      }
      _ctxSym  = sym;
      _ctxExch = exch;
      _ctxAction = /** @type {any} */ (action);
      _ctxMenu = null;
    }}
  />
{/if}

{#if _ctxAction === 'chart'}
  <ChartModal
    symbol={_ctxSym}
    exchange={_ctxExch}
    onClose={() => { _ctxAction = null; }}
  />
{/if}

{#if _ctxAction === 'place-order'}
  <SymbolPanel
    symbol={_ctxSym}
    exchange={_ctxExch}
    onSubmit={() => {}}
    onClose={() => { _ctxAction = null; }}
  />
{/if}

<OrderTimelineDrawer
  open={_timelineOpen}
  orders={_timelineOrders}
  orderContext={_timelineContext}
  linkedOrders={_timelineLinked}
  onClose={() => { _timelineOpen = false; }}
  onSelectLinked={(linkedId) => _openOrderTimeline(linkedId)}
/>

</div>

<style>
  /* Fullscreen wrapper — transparent by default (display:contents passes
     layout through to children); promotes onto the SHARED `.fs-card-on`
     global pattern (app.css) when fullscreen, instead of a hand-rolled
     fixed overlay (audit fix, 2026-10-02 — was `.ob-fs { position: fixed;
     inset: 0; z-index: 9000; }`, a second, competing fullscreen mechanism
     that ignored the live-measured `--fs-card-top` chrome offset
     `DefaultSizeButton.svelte` sets, so a fullscreened OrderBook painted
     from the viewport's true top edge (y=0) and covered the real navbar
     + page-header + NavStrip. Its z-index 9000 also sat BELOW the shared
     `.fs-backdrop`/`.fs-backdrop-catch` tier (9998) that DefaultSizeButton
     portals to document.body on ANY card's fullscreen entry — including
     OrderBook's own, since its CardHeader already mounts CardControls →
     FullscreenButton/DefaultSizeButton unconditionally — so the dim
     backdrop rendered ON TOP of OrderBook's own fullscreen content.
     `.fs-card-on` fixes both: it reads `--fs-card-top` for the inset and
     sits at the correct z-index 9999 tier.

     OrderBook keeps its OWN `.fs-card-on` application (not just relying
     on a host wrapper) because it's mounted in two different contexts:
     the host `<section>` on /orders ALSO carries `class:fs-card-on` (its
     own wrapper, bound to the same isFullscreen), but `SymbolPanel.svelte`
     embeds OrderBook directly with no such wrapper at all — there,
     `.ob-root`'s own `.fs-card-on` is the ONLY fullscreen mechanism. On
     /orders this produces two nested elements both resolving to the
     identical `--fs-card-top`-derived rect (harmless, visually a no-op
     duplicate — not the escaping/overlap bug `.ob-fs` had, since both
     now agree on the same offset+z-index). Only `display`, layout and
     background/padding are added here; inset/z-index/overflow all come
     from the shared global rule. Modals inside (ChartModal, SymbolPanel,
     etc.) use fixed positioning themselves so display:contents doesn't
     trap them in the non-fullscreen case. */
  .ob-root { display: contents; }
  .ob-root.fs-card-on {
    display: flex;
    flex-direction: column;
    /* #0f1c36 is a one-off fullscreen-card tone, not reused anywhere else
       in the app (confirmed via grep) and distinct enough from the
       --algo-bg-elev1/elev2 surface tokens to not be a drop-in match —
       kept as a plain literal rather than inventing a new single-use
       token (`--algo-navy` was referenced but never defined). */
    background: #0f1c36;
    padding: 0.5rem;
  }
  .ob-root.fs-card-on .ob-scroll { flex: 1 1 0; min-height: 0; }

  .ob-count {
    font-size: var(--fs-md);
    color: rgba(255,255,255,0.3);
    font-variant-numeric: tabular-nums;
    margin-left: 0.25rem;
  }

  .ob-scroll {
    overflow-y: auto;
    flex: 1 1 0;
    min-height: 0;
    padding: 0.4rem 0.2rem;
  }

  .ob-scroll :global(.oc-book-grid) {
    display: grid;
    /* minmax(0, 1fr) — NOT bare `1fr` (shorthand for minmax(auto, 1fr)).
       A bare `1fr` track floors at the grid ITEM's min-content width —
       for an OrderCard that's the unwrapped max-content width of its top
       summary row (txn-type + account + symbol + status pill). The
       symbol span's own `min-w-0 truncate` can never free up room until
       the TRACK ITSELF is allowed to shrink below that floor. Confirmed
       live via Playwright at 360px: card right edge measured past the
       viewport, status pill clipped off-screen entirely (2026-10-23
       fix). The 640px/1024px multi-column breakpoints below already
       used `minmax(0, 1fr)` and were never affected — only this
       single-column base rule had the bare-`1fr` trap. */
    grid-template-columns: minmax(0, 1fr);
    gap: 0.5rem;
  }
  @media (min-width: 640px) {
    .ob-scroll :global(.oc-book-grid) { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  }
  @media (min-width: 1024px) {
    .ob-scroll :global(.oc-book-grid) { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  }
  /* Backstop (belt-and-suspenders) — even with the minmax(0, 1fr) track
     fix above, a grid ITEM's own default `min-width: auto` can still
     floor it at content size in edge cases (e.g. a future non-wrapping
     addition to the card). Pin the grid-item shape this grid renders
     (OrderCard's `.order-card`) to `min-width: 0` directly, matching
     LogPanel's `.lp-multicol .log-row { min-width: 0 }` convention for
     the identical CSS-grid-intrinsic-sizing trap. */
  .ob-scroll :global(.oc-book-grid .order-card) {
    min-width: 0;
  }

  /* 2026-10-23 fix — was `display:grid; grid-template-columns:
     repeat(6, minmax(0, 1fr))` with `.ob-sc-l` ellipsis-truncating the
     label text to fit. At 320-375px that squeezed "Rejected/Cancelled"
     (the longest label) into unreadable fragments — a silent-clip
     violation of this app's overflow-not-clip convention (see
     AlgoTabs.svelte's `.algo-tabs-strip` for the reference pattern this
     mirrors). Now a horizontally-scrolling flex row: every chip keeps
     its full, untruncated label; the row itself scrolls on narrow
     viewports instead of the page. */
  .ob-status-bar {
    display: flex;
    flex-wrap: nowrap;
    gap: 0.25rem;
    padding: 0.3rem 0.4rem 0.2rem;
    max-width: 100%;
    min-width: 0;
    overflow-y: hidden;
  }

  /* Status filter chips — match .oc-filter-card chrome from the orders page.
     Navy gradient base, status-tinted gradient overlay via data-status,
     amber inset ring on .is-active. `flex: 1 0 auto` (not a bare
     `flex-shrink: 0`) so the 6 chips still stretch to fill a wide
     desktop card evenly (matching the pre-2026-10-23 grid's equal-width
     look) while never shrinking below their own label's content width
     on a narrow viewport — the row scrolls instead. */
  .ob-sc {
    display: flex;
    flex: 1 0 auto;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 0.15rem;
    padding: 0.45rem 0.6rem;
    white-space: nowrap;
    background:
      linear-gradient(180deg,
        rgba(255, 255, 255, 0.04) 0%,
        rgba(255, 255, 255, 0.00) 30%,
        rgba(0, 0, 0, 0.08) 100%),
      linear-gradient(180deg, #2c3a5a 0%, #1a2740 100%);
    border: 1px solid rgba(255, 255, 255, 0.10);
    border-radius: 5px;
    box-shadow:
      0 1px 0 rgba(255, 255, 255, 0.06) inset,
      0 2px 4px rgba(0, 0, 0, 0.25);
    color: var(--algo-slate, #94a3b8);
    font-family: var(--font-numeric);
    cursor: pointer;
    transition: border-color 0.12s, transform 0.12s, box-shadow 0.12s;
  }
  .ob-sc:hover {
    border-color: rgba(255, 255, 255, 0.30);
    transform: translateY(-1px);
  }
  .ob-sc.is-active {
    box-shadow:
      0 0 0 2px rgba(251, 191, 36, 0.55) inset,
      0 1px 0 rgba(255, 255, 255, 0.06) inset,
      0 2px 4px rgba(0, 0, 0, 0.25);
  }

  /* Status-tinted backgrounds + borders per data-status value. */
  .ob-sc[data-status="running"] {
    background:
      linear-gradient(180deg,
        rgba(251, 191, 36, 0.18) 0%,
        rgba(251, 191, 36, 0.06) 60%,
        rgba(0, 0, 0, 0.08) 100%),
      linear-gradient(180deg, #2c3a5a 0%, #1a2740 100%);
    border-color: rgba(251, 191, 36, 0.60);
  }
  .ob-sc[data-status="active"] {
    background:
      linear-gradient(180deg,
        rgba(74, 222, 128, 0.18) 0%,
        rgba(74, 222, 128, 0.04) 60%,
        rgba(0, 0, 0, 0.08) 100%),
      linear-gradient(180deg, #2c3a5a 0%, #1a2740 100%);
    border-color: rgba(74, 222, 128, 0.60);
  }
  .ob-sc[data-status="error"] {
    background:
      linear-gradient(180deg,
        rgba(248, 113, 113, 0.18) 0%,
        rgba(248, 113, 113, 0.04) 60%,
        rgba(0, 0, 0, 0.08) 100%),
      linear-gradient(180deg, #2c3a5a 0%, #1a2740 100%);
    border-color: rgba(248, 113, 113, 0.60);
  }
  /* Chase chip — sky/info tint, deliberately distinct from Open's amber
     so "resting, untouched" vs "actively being re-quoted by the chase
     engine" read apart at a glance. */
  .ob-sc[data-status="chase"] {
    background:
      linear-gradient(180deg,
        rgba(125, 211, 252, 0.18) 0%,
        rgba(125, 211, 252, 0.05) 60%,
        rgba(0, 0, 0, 0.08) 100%),
      linear-gradient(180deg, #2c3a5a 0%, #1a2740 100%);
    border-color: rgba(125, 211, 252, 0.55);
  }
  /* Held/Attention chip — violet tint (audit fix, 2026-10), the one
     remaining palette color not already claimed by a sibling chip
     (sky=Chase, amber=Open, green=Filled, red=Rejected/Cancelled).
     Covers HELD (operator-review hold) and CANCEL_FAILED (a
     cancel request failed — order may still be live at the broker): both
     need an unmistakably distinct, attention-grabbing color since they
     represent orders needing operator action, not a routine resting or
     already-resolved state. */
  .ob-sc[data-status="held"] {
    background:
      linear-gradient(180deg,
        rgba(192, 132, 252, 0.18) 0%,
        rgba(192, 132, 252, 0.05) 60%,
        rgba(0, 0, 0, 0.08) 100%),
      linear-gradient(180deg, #2c3a5a 0%, #1a2740 100%);
    border-color: rgba(192, 132, 252, 0.55);
  }

  /* Count number — bigger + color-coded by status. 2026-09 font-size
     audit: 1.1rem falls in the gap between --fs-xl (0.85rem/13.6px)
     and --fs-2xl (1.55rem/24.8px) — no existing token lands within
     ~4px without a visible resize of this live chase-queue counter.
     Flagged, not auto-mapped; left as a deliberate literal pending an
     operator call on either a new "stat" tier token or accepting the
     nearest existing one. */
  .ob-sc-n {
    font-weight: 800;
    font-size: 1.1rem;
    line-height: 1;
    color: var(--algo-slate, #94a3b8);
    font-variant-numeric: tabular-nums;
    text-shadow: 0 1px 2px rgba(0, 0, 0, 0.45);
  }
  .ob-sc[data-status="running"]   .ob-sc-n { color: var(--c-action, #fbbf24); }
  .ob-sc[data-status="active"]    .ob-sc-n { color: var(--c-long, #4ade80); }
  .ob-sc[data-status="error"]     .ob-sc-n { color: var(--c-short, #f87171); }
  .ob-sc[data-status="chase"]     .ob-sc-n { color: var(--algo-sky, #7dd3fc); }
  .ob-sc[data-status="held"]      .ob-sc-n { color: var(--algo-violet, #c084fc); }

  /* No overflow/text-overflow/max-width here (2026-10-23 fix, removed) —
     the row now scrolls horizontally instead of ellipsis-clipping the
     label; see `.ob-status-bar`'s comment above. `white-space: nowrap`
     stays so a label never wraps onto a second line inside its chip. */
  .ob-sc-l {
    font-size: var(--fs-xs, 0.6rem);
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--algo-muted, rgba(255,255,255,0.4));
    line-height: 1;
    white-space: nowrap;
  }

  /* 5 status chips (Held-Attn/Chase/Open/Filled/Rejected-Cancelled) —
     below 600px, tighten padding + letter-spacing so more of the
     horizontally-scrolling row is visible before the operator needs to
     swipe; the row itself now absorbs any remaining width via
     `.ob-status-bar`'s `overflow-x: auto` rather than clipping
     "Rejected/Cancelled" (the longest label) into illegible fragments. */
  @media (max-width: 600px) {
    .ob-sc { padding: 0.35rem 0.2rem; }
    .ob-sc-l { font-size: var(--fs-xs); letter-spacing: 0.03em; }
  }
</style>
