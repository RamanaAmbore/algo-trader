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
   *   statusFilter?  — 'chase'|'open'|'complete'|'rejected_cancelled'|'gtt' —
   *                    INERT for the no-chip-clicked default (2026-09-30):
   *                    both existing call sites pass 'open' as a literal
   *                    default, indistinguishable from not passing it at
   *                    all, so it can no longer double as "the default
   *                    filter" without defeating the first-non-zero-chip
   *                    default below. Kept declared (back-compat, still
   *                    type-checked at call sites) but no longer read by
   *                    `_activeStatus` — see `_defaultActiveId`.
   */
  import { onMount, onDestroy, untrack } from 'svelte';
  import { visibleInterval, formatDualTz, withGuard } from '$lib/stores';
  import { isCurrentTradingSession } from '$lib/dateFormat.js';
  import { fetchOrders, fetchAlgoOrdersRecent, cancelOrder, reconcileSingleOrder, fetchGtts, cancelGtt } from '$lib/api';
  import { priceFmt } from '$lib/format';
  import { acctColor } from '$lib/account';
  import OrderCard from '$lib/order/OrderCard.svelte';
  import ChartModal from '$lib/ChartModal.svelte';
  import SymbolPanel from '$lib/SymbolPanel.svelte';
  import SymbolContextMenu from '$lib/SymbolContextMenu.svelte';
  import CardHeader from '$lib/CardHeader.svelte';
  import { toast } from '$lib/data/toastStore.svelte.js';
  import { noteAttachObservation } from '$lib/data/templateAttachToast.js';
  import { noteOrderPollFills } from '$lib/data/orderFillDetector.js';

  /** @type {{
   *   orderId?: string | null,
   *   accountFilter?: string[],
   *   title?: string,
   *   pollMs?: number,
   *   statusFilter?: 'chase'|'open'|'complete'|'rejected_cancelled'|'gtt',
   *   onSymbolClick?: ((ord: any) => void) | null,
   *   isCollapsed?: boolean,
   *   isFullscreen?: boolean,
   * }} */
  let {
    orderId       = null,
    accountFilter = /** @type {string[]} */ ([]),
    title         = 'Order Book',
    pollMs        = 5000,
    statusFilter  = /** @type {'chase'|'open'|'complete'|'rejected_cancelled'|'gtt'} */ ('open'),
    onSymbolClick = /** @type {((ord: any) => void) | null} */ (null),
    isCollapsed   = $bindable(false),
    isFullscreen  = $bindable(false),
  } = $props();

  // ── Data ─────────────────────────────────────────────────────────────
  let orderRows = $state(/** @type {any[]} */ ([]));
  let _loading  = $state(true);

  // `_internalStatus` is set the moment the operator clicks a chip and
  // stays sticky (exclusive single-status filter) from then on — unchanged
  // behaviour. While it's still null (nothing clicked yet this session),
  // `_activeStatus` resolves via `_defaultActiveId` below instead of the
  // (now-inert) `statusFilter` prop — see its JSDoc above for why.
  let _internalStatus = $state(/** @type {string|null} */ (null));

  /**
   * Row timestamp → epoch-ms, tolerant of both source field conventions
   * this merged view carries: Kite's `order_timestamp` ("YYYY-MM-DD
   * HH:MM:SS", IST, no offset — Safari's Date.parse rejects the
   * space-separated non-ISO form outright, so normalize to ISO+offset
   * explicitly rather than passing the raw string through) and
   * AlgoOrder's `created_at` (naive UTC `isoformat()`, no trailing 'Z' —
   * Date.parse without an explicit offset is spec'd as LOCAL time, so
   * append 'Z' explicitly rather than relying on engine defaults).
   * Returns NaN when unparseable.
   */
  function _rowTsMs(/** @type {any} */ o) {
    if (o?.order_timestamp) {
      const s = String(o.order_timestamp).trim();
      const m = s.match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})/);
      return m ? Date.parse(`${m[1]}T${m[2]}+05:30`) : Date.parse(s);
    }
    if (o?.created_at) {
      const s = String(o.created_at).trim();
      if (!s) return NaN;
      return /[Zz]|[+-]\d{2}:\d{2}$/.test(s) ? Date.parse(s) : Date.parse(`${s}Z`);
    }
    return NaN;
  }

  /**
   * Drops TERMINAL rows from a PRIOR trading session (08:00 IST rollover —
   * see `isCurrentTradingSession`). This is a display-only filter applied
   * upstream to `orderRows` itself (not just the rendered grid) so every
   * downstream consumer — status counts, the active status filter, CSV
   * export — automatically only ever sees today's-session terminal orders
   * with no separate/duplicate filtering logic.
   *
   * Still-working rows (OPEN/TRIGGER PENDING) are NEVER dropped by this
   * filter regardless of age — operator explicit instruction (2026-09-30,
   * reversing the initial default): "keep them visible until reconciled."
   * A resting order from a prior session is exactly the kind of thing an
   * operator needs to SEE and act on (cancel / reconcile), not have
   * silently disappear at the next 08:00 rollover.
   */
  function _isCurrentSessionRow(/** @type {any} */ o) {
    if (_STATUS_PREDICATES.open((o?.status || '').toUpperCase())) return true;
    const ms = _rowTsMs(o);
    if (!Number.isFinite(ms)) return true; // can't judge — keep, don't hide data we can't classify
    return isCurrentTradingSession(ms);
  }

  async function _loadOrders() {
    // Merge broker orders + algo orders so the book carries the same data
    // LogPanel's order tab renders — broker rows plus paper/sim/shadow rows
    // that never reach the broker. Broker rows win on duplicate order_id.
    try {
      const [brokerResp, algoResp] = await Promise.allSettled([
        fetchOrders(),
        fetchAlgoOrdersRecent(100, 'all'),
      ]);
      // Audit fix — "cancelled orders showing as OPEN again": fetchOrders()
      // (the broker book — authoritative for CANCELLED/COMPLETE/REJECTED,
      // confirmed at the exchange) can transiently reject (network blip,
      // 5xx). Pre-fix, a rejected brokerResp silently fell through to
      // brokerRows=[], so EVERY row in the merged view came from the algo
      // book instead — whose status field can lag a just-confirmed
      // cancel/fill until the postback/reconcile pass catches up. The next
      // poll after a genuine cancel could then render that order back as
      // OPEN purely because the broker fetch (not the order) failed. Freeze
      // to the last-known-good merged view instead of recomputing from
      // partial data — matches this app's staleness-freeze convention
      // elsewhere (positions/holdings/NAV).
      if (brokerResp.status !== 'fulfilled') {
        _loading = false;
        return;
      }
      const brokerRows = Array.isArray(brokerResp.value?.rows) ? brokerResp.value.rows : [];
      const algoRows = (algoResp.status === 'fulfilled' && Array.isArray(algoResp.value))
        ? algoResp.value
        : [];
      const brokerIds = new Set(brokerRows.map(o => String(o?.order_id || '')));
      // Dedup key fix (Sprint 2a, docs/proposals/SPRINT2_LAYER_INTEGRATION.md
      // §1 finding 2 / §2) — `AlgoOrderInfo` never carried an `order_id`
      // field (that's a broker-only shape); comparing against
      // `o.order_id || o.id` silently fell through to the algo row's own
      // internal DB `id`, which can never equal a broker `order_id` — the
      // dedup never matched and a live, algo-tracked order rendered TWICE
      // (once as a bare broker row, once as an algo row). `broker_order_id`
      // is now a real surfaced field on `AlgoOrderInfo` and is the actual
      // shared identity key between the two row shapes. An algo row with
      // no `broker_order_id` has no broker counterpart yet (paper/sim/
      // shadow, or a live order whose placement hasn't reached the broker
      // book on this particular poll) and must stay in algoOnly.
      const algoOnly  = algoRows.filter(o => {
        const bid = String(o?.broker_order_id || '');
        return !bid || !brokerIds.has(bid);
      });
      // Session-boundary filter applied HERE (before assigning orderRows,
      // not down in the filteredOrderRows derived chain) — a still-OPEN
      // order resting since a prior session (rare — e.g. a GTT-adjacent
      // order) is judgment-called to hide here too, same as terminal
      // rows. No clear precedent in this app scopes to "keep showing a
      // stale resting order in a live order-book grid" (expiry_freeze.py's
      // position-freeze precedent is about serving the last-known
      // POSITION snapshot when data is otherwise unavailable, a different
      // situation from a live, always-available order feed) — flagged for
      // reversal if the operator wants stale-OPEN rows to stay visible.
      //
      // `_rowOrigin` stamped here (not inferred later from shape) is the
      // explicit origin flag `_isOpenBroker` below reads instead of the
      // old `!o?.mode` heuristic — see that function's comment for why
      // the heuristic needed replacing now rather than later.
      const merged = [
        ...brokerRows.map(o => ({ ...o, _rowOrigin: 'broker' })),
        ...algoOnly.map(o => ({ ...o, _rowOrigin: 'algo' })),
      ].filter(_isCurrentSessionRow);
      merged.sort((a, b) => {
        const ta = _rowTsMs(a) || 0;
        const tb = _rowTsMs(b) || 0;
        return tb - ta;
      });
      orderRows = merged;
      // Channel-agnostic fresh-books trigger (2026-09-30 follow-up to
      // c90a9d04) — fires a shared bookChanged bump the moment ANY row
      // in this poll transitions to FILLED/COMPLETE, regardless of
      // whether a WS event ever arrived for it (live broker-order-book
      // TTL refresh, 5-min open_order_watchdog sweep, admin reconcile —
      // none of those backend paths broadcast today). See
      // orderFillDetector.js header for the full channel inventory.
      noteOrderPollFills(merged);
      // Trading-critical "template did not attach" toast (2026-09-30) —
      // evaluated on every merged row BEFORE status-chip filtering, so
      // it fires regardless of which status bucket the operator has
      // selected. See templateAttachToast.js for the live-only gate +
      // time-based debounce this relies on.
      for (const o of merged) {
        if (noteAttachObservation(o)) {
          const oid = o?.id ?? o?.order_id;
          toast.warning(`Order #${oid} template did not attach — check Order Book`, { timeoutMs: 5000 });
        }
      }
    } catch (_) { /* keep last-good */ } finally {
      _loading = false;
    }
  }

  // ── Standalone broker GTTs ("GTT" chip) ────────────────────────────────
  // A DIFFERENT concept from the per-filled-order attached-exit-GTT bits
  // OrderCard already renders (`attached_gtts_json`) — this is whatever's
  // independently resting at the broker right now, regardless of how it
  // got there. Loaded + polled separately from `_loadOrders` (own
  // try/catch, own freeze-to-last-good) so a GTT-route hiccup never
  // blanks the order grid and vice versa.
  let _gttRows = $state(/** @type {any[]} */ ([]));

  async function _loadGtts() {
    try {
      const resp = await fetchGtts();
      const rows = Array.isArray(resp?.gtts) ? resp.gtts : [];
      _gttRows = rows;
    } catch (_) { /* keep last-good — same staleness-freeze convention as _loadOrders */ }
  }

  function _downloadCsv() {
    if (_activeStatus === 'gtt') {
      const rows = _filteredGttRows;
      if (!rows.length) return;
      const headers = ['gtt_id','account','symbol','exchange','status','trigger_values','last_price','created_at'];
      const csv = [
        headers.join(','),
        ...rows.map(r => [
          JSON.stringify(r.gtt_id ?? ''),
          JSON.stringify(r.account ?? ''),
          JSON.stringify(r.tradingsymbol ?? ''),
          JSON.stringify(r.exchange ?? ''),
          JSON.stringify(r.status ?? ''),
          JSON.stringify((r.trigger_values || []).join('/')),
          JSON.stringify(r.last_price ?? ''),
          JSON.stringify(r.created_at ?? ''),
        ].join(','))
      ].join('\n');
      const a = document.createElement('a');
      a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
      a.download = 'gtts.csv';
      a.click();
      return;
    }
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
    // interval tick share the SAME guarded instance. Two independent
    // guards (own `_running` flag each, per withGuard's contract) so a
    // slow GTT fetch never blocks the orders poll and vice versa — they
    // already have separate freeze-to-last-good semantics.
    //
    // Manual triggers (_cancelRow/_reconcileRow's post-action reload,
    // _cancelGttRow's post-cancel reload, CardHeader's onRefresh button)
    // deliberately keep calling the RAW _loadOrders/_loadGtts below, NOT
    // these guarded wrappers — an operator-initiated refresh must never be
    // silently dropped (guarded call returns undefined, no-op) just
    // because a routine poll tick happens to be in-flight at that instant.
    const _guardedLoadOrders = withGuard(_loadOrders);
    const _guardedLoadGtts = withGuard(_loadGtts);
    _guardedLoadOrders();
    _guardedLoadGtts();
    if (pollMs > 0 && typeof document !== 'undefined') {
      const teardown = visibleInterval(() => { _guardedLoadOrders(); _guardedLoadGtts(); }, pollMs);
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
    // instruction) — single combined predicate, single chip.
    rejected_cancelled: st => st === 'REJECTED' || st === 'CANCELLED',
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
    // GTT rows live in `_gttRows`/`_filteredGttRows`, not `orderRows` —
    // the GTT chip never matches an order row.
    if (filter === 'gtt') return [];
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

  // Kite's GTT book (and Dhan/Groww's normalised mirror of it) can return
  // historical rows that already fired or were cancelled/expired/disabled
  // at the broker — those aren't "standing" GTTs and must NOT inflate the
  // GTT chip's count (which would also wrongly make GTT the first-non-zero
  // default, or let Cancel render on an already-dead GTT). Unknown/blank
  // status values are treated as live (fail-open towards showing data the
  // operator might still need to act on, same spirit as other predicates
  // in this file keeping unclassifiable rows visible rather than hiding
  // them).
  const _GTT_TERMINAL_STATUSES = new Set(['triggered', 'cancelled', 'deleted', 'disabled', 'expired']);
  function _isLiveGtt(/** @type {any} */ g) {
    return !_GTT_TERMINAL_STATUSES.has((g?.status || '').toLowerCase());
  }

  // GTT rows filtered the same way orders are (account scope — GTTs aren't
  // subject to the order-id / status-predicate filters above) PLUS the
  // live-only filter above.
  const _filteredGttRows = $derived.by(() =>
    _applyAccountFilter(_gttRows || [], accountFilter).filter(_isLiveGtt));

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

  /** Returns true when the order is in-flight and reconciling is meaningful. */
  function _isInFlight(/** @type {any} */ o) {
    const st = (o?.status || '').toUpperCase();
    return st === 'OPEN' || st === 'TRIGGER PENDING'
        || st === 'CANCEL_FAILED' || st === 'PARTIAL';
  }

  async function _cancelRow(/** @type {any} */ o) {
    const key = String(o.order_id || o.id || '');
    if (!key || _cancelling.has(key)) return;
    _cancelling = new Set([..._cancelling, key]);
    _cancelErr = '';
    try {
      await cancelOrder(o.order_id, o.account, o.variety || 'regular');
      await _loadOrders();
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
      if (res?.updated) await _loadOrders();
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
  // session-boundary-filtered in _loadOrders — so every count here is
  // scoped to today's session AND the current account filter, matching
  // the grid it labels AND `_filteredGttRows` (also account-filtered).
  // Without this, selecting an account could default-highlight a chip
  // (e.g. Chase) whose only rows belong to a DIFFERENT account, showing
  // an empty list underneath.
  const _countScopedOrderRows = $derived.by(() => _applyAccountFilter(orderRows, accountFilter));
  const _statusCounts = $derived.by(() => ({
    chase:               _countScopedOrderRows.filter(_isChaseInFlight).length,
    open:                _countScopedOrderRows.filter(o => _STATUS_PREDICATES.open((o.status || '').toUpperCase())).length,
    complete:            _countScopedOrderRows.filter(o => _STATUS_PREDICATES.complete((o.status || '').toUpperCase())).length,
    rejected_cancelled:  _countScopedOrderRows.filter(o => _STATUS_PREDICATES.rejected_cancelled((o.status || '').toUpperCase())).length,
  }));

  // ── Default chip resolution (2026-09-30) ────────────────────────────────
  // Fixed display order for the 5 chips. When nothing has been explicitly
  // clicked (`_internalStatus === null`), the SINGLE chip highlighted +
  // shown is the first one in this order whose count is non-zero — not a
  // union of every non-zero chip. Both `_statusCounts` and
  // `_filteredGttRows.length` are `$derived`, so `_countsById` and
  // `_defaultActiveId` recompute automatically on every poll tick/status
  // change with no extra wiring — e.g. Open emptying while Filled still
  // has rows moves the highlight+list to Filled on the very next tick.
  const CHIP_ORDER = ['chase', 'open', 'complete', 'rejected_cancelled', 'gtt'];
  const _countsById = $derived.by(() => ({ ..._statusCounts, gtt: _filteredGttRows.length }));
  // Gated on `_loading` (the ORDERS fetch flag) so the default can't
  // transiently resolve to 'gtt' purely because the independent GTT poll
  // happened to land before the first orders poll — `_loading` clears
  // only once `_loadOrders` has run at least once (success or freeze-on-
  // failure), by which point `_countsById`'s order-derived fields are
  // trustworthy for picking the first-non-zero chip.
  const _defaultActiveId = $derived(_loading ? null : (CHIP_ORDER.find(id => _countsById[id] > 0) ?? null));

  // Explicit click wins (sticky, exclusive filter — unchanged behaviour);
  // otherwise fall back to the single first-non-zero chip above. Never
  // reads the `statusFilter` prop (see its JSDoc).
  const _activeStatus = $derived(_internalStatus ?? _defaultActiveId);

  // ── Cancel a standalone broker GTT ──────────────────────────────────────
  /** @type {Set<string>} */
  let _cancellingGtt = $state(new Set());
  /** @type {string} */
  let _gttCancelErr = $state('');

  async function _cancelGttRow(/** @type {any} */ g) {
    const key = `${g.account}:${g.gtt_id}`;
    if (!g?.gtt_id || _cancellingGtt.has(key)) return;
    _cancellingGtt = new Set([..._cancellingGtt, key]);
    _gttCancelErr = '';
    try {
      await cancelGtt(g.gtt_id, g.account, g.exchange);
      await _loadGtts();
    } catch (e) {
      _gttCancelErr = /** @type {any} */ (e)?.message || 'cancel failed';
      setTimeout(() => { _gttCancelErr = ''; }, 3000);
    } finally {
      const next = new Set(_cancellingGtt);
      next.delete(key);
      _cancellingGtt = next;
    }
  }

  /** GTT status → chip-style `data-status` tint, mirroring OrderCard's
   *  `_statusDataAttr` idiom (active/resting → running amber, triggered →
   *  complete green, cancelled/deleted → rejected_cancelled's red,
   *  disabled/expired → inactive grey). `_filteredGttRows` already drops
   *  every terminal status via `_isLiveGtt`, so in practice this always
   *  returns 'running' for what actually renders — kept defensive (not
   *  dead code removed) in case a row's status races between polls. */
  function _gttStatusDataAttr(/** @type {string} */ status) {
    const s = (status || '').toLowerCase();
    if (s === 'triggered') return 'complete';
    if (s === 'cancelled' || s === 'deleted') return 'error';
    if (s === 'disabled' || s === 'expired') return 'inactive';
    return 'running'; // active / unknown — still resting at the broker
  }

  // ── Symbol panel / chart modal / context menu state ───────────────────
  let _symPanelSym  = $state('');
  let _symPanelExch = $state('');
  /** @type {{ symbol: string, exchange: string, x: number, y: number } | null} */
  let _ctxMenu = $state(null);
  /** @type {'place-order' | 'chart' | null} */
  let _ctxAction = $state(null);
  let _ctxSym  = $state('');
  let _ctxExch = $state('');
</script>

<div class="ob-root" class:fs-card-on={isFullscreen}>

<!-- Header -->
<CardHeader title={title} showSearch={false} bind:isCollapsed bind:isFullscreen
  detectOverflow={false}
  onRefresh={() => { _loadOrders(); _loadGtts(); }}
  onDownload={_downloadCsv}
>
  {#snippet left()}
    {#if _activeStatus === 'gtt'}
      <span class="ob-count">{_filteredGttRows.length} gtt{_filteredGttRows.length !== 1 ? 's' : ''}</span>
    {:else}
      <span class="ob-count">{filteredOrderRows.length} order{filteredOrderRows.length !== 1 ? 's' : ''}</span>
    {/if}
  {/snippet}
</CardHeader>

<!-- Order card grid -->
{#if !isCollapsed}
  <div class="ob-status-bar">
    {#each [
      { id: 'chase',              label: 'Chase',              status: 'chase',   count: _statusCounts.chase },
      { id: 'open',               label: 'Open',               status: 'running', count: _statusCounts.open },
      { id: 'complete',           label: 'Filled',             status: 'active',  count: _statusCounts.complete },
      { id: 'rejected_cancelled', label: 'Rejected/Cancelled', status: 'error',   count: _statusCounts.rejected_cancelled },
      { id: 'gtt',                label: 'GTT',                status: 'gtt',     count: _filteredGttRows.length },
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
  {#if _activeStatus === 'gtt'}
    {#if _filteredGttRows.length}
      <div class="oc-book-grid">
        {#each _filteredGttRows as g (`${g.account}:${g.gtt_id}`)}
          {@const _gKey = `${g.account}:${g.gtt_id}`}
          <div class="algo-status-card gtt-card text-left p-2.5" data-status={_gttStatusDataAttr(g.status)}>
            <div class="flex items-center justify-start gap-2 mb-0.5">
              <span class="font-semibold text-xs min-w-0 truncate">
                <!-- `.oc-acct` (shared with OrderCard.svelte) reads
                     --acct-color inline, same pattern as OrderCard's own
                     span (order/OrderCard.svelte ~line 173) — without
                     this inline style the stripe falls back to
                     `transparent` and never shows a colour (2026-09-30
                     consistency-pass fix). -->
                <span class="oc-acct" style={g.account ? `--acct-color: ${acctColor(g.account) || 'transparent'};` : ''}>{g.account}</span>
                <span class="text-[var(--algo-slate)]">{g.tradingsymbol || '—'}</span>
              </span>
              <span class="algo-status-pill ml-auto flex-shrink-0">{(g.status || '').toUpperCase() || 'GTT'}</span>
            </div>
            <div class="flex flex-wrap items-center gap-y-1">
              {#if g.exchange}<span class="log-chip"><span class="log-chip-key">ex:</span>{g.exchange}</span>{/if}
              <span class="log-chip"><span class="log-chip-key">trigger:</span>{(g.trigger_values || []).map(v => priceFmt(v)).join(' / ') || '—'}</span>
              {#if g.last_price}<span class="log-chip"><span class="log-chip-key">ltp:</span>{priceFmt(g.last_price)}</span>{/if}
              {#if g.trigger_type}<span class="log-chip"><span class="log-chip-key">type:</span>{g.trigger_type}</span>{/if}
            </div>
            <div class="lp-oc-actions mt-1" role="group" aria-label="GTT actions">
              <button type="button" class="lp-oc-btn lp-oc-cancel"
                title="Cancel GTT"
                aria-label="Cancel"
                disabled={_cancellingGtt.has(_gKey)}
                onclick={(e) => { e.stopPropagation(); _cancelGttRow(g); }}>
                <svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                  <path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.8"
                        stroke-linecap="round"/>
                </svg>
              </button>
            </div>
          </div>
        {/each}
        {#if _gttCancelErr}
          <div class="log-row log-agent-failed">{_gttCancelErr}</div>
        {/if}
      </div>
    {:else}
      <div class="log-debug py-2 text-center">
        {#if _loading}
          Loading…
        {:else}
          No standing GTT orders.
        {/if}
      </div>
    {/if}
  {:else if filteredOrderRows.length}
    <div class="oc-book-grid">
      {#each filteredOrderRows as o (o.order_id ?? o.id)}
        {@const _oKey = String(o.order_id || o.id || '')}
        <OrderCard order={o}
          onSymbolClick={(ord) => {
            if (onSymbolClick) { onSymbolClick(ord); return; }
            _symPanelSym = ord.tradingsymbol || ord.symbol || '';
            _symPanelExch = ord.exchange || '';
          }}
          onSymbolContext={(ord, e) => { _ctxMenu = { symbol: ord.tradingsymbol || ord.symbol || '', exchange: ord.exchange || '', x: /** @type {MouseEvent} */ (e).clientX, y: /** @type {MouseEvent} */ (e).clientY }; }}>
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
{/if}

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
    onClose={() => { _ctxMenu = null; }}
    onAction={(action, sym, exch) => {
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
    background: var(--algo-navy, #0f1c36);
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
    grid-template-columns: 1fr;
    gap: 0.5rem;
  }
  @media (min-width: 640px) {
    .ob-scroll :global(.oc-book-grid) { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  }
  @media (min-width: 1024px) {
    .ob-scroll :global(.oc-book-grid) { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  }

  .ob-status-bar {
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    gap: 0.25rem;
    padding: 0.3rem 0.4rem 0.2rem;
  }

  /* Status filter chips — match .oc-filter-card chrome from the orders page.
     Navy gradient base, status-tinted gradient overlay via data-status,
     amber inset ring on .is-active. */
  .ob-sc {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 0.15rem;
    padding: 0.45rem 0.6rem;
    min-width: 0;
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
  /* GTT chip — cyan tint (2026-09-30), the one existing palette color not
     already claimed by a sibling chip (amber=Open, green=Filled,
     red=Rejected/Cancelled, sky=Chase) — standing broker-side GTTs are a
     structurally distinct concept (not an AlgoOrder status at all) so they
     read as neither "resting order" (sky) nor "filled" (green). */
  .ob-sc[data-status="gtt"] {
    background:
      linear-gradient(180deg,
        rgba(34, 211, 238, 0.18) 0%,
        rgba(34, 211, 238, 0.05) 60%,
        rgba(0, 0, 0, 0.08) 100%),
      linear-gradient(180deg, #2c3a5a 0%, #1a2740 100%);
    border-color: rgba(34, 211, 238, 0.55);
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
  .ob-sc[data-status="gtt"]       .ob-sc-n { color: var(--algo-cyan, #22d3ee); }

  .ob-sc-l {
    font-size: var(--fs-xs, 0.6rem);
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--algo-muted, rgba(255,255,255,0.4));
    line-height: 1;
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* 5 status chips (Chase/Open/Filled/Rejected-Cancelled/GTT) need to fit
     a 320-375px phone viewport without forcing the grid wider than the
     card — tighten padding + letter-spacing below 600px so the labels
     truncate gracefully instead of overflowing. "Rejected/Cancelled" is
     the longest label in the row and relies on .ob-sc-l's existing
     overflow:hidden + ellipsis to degrade instead of wrapping/overflowing. */
  @media (max-width: 600px) {
    .ob-sc { padding: 0.35rem 0.2rem; }
    .ob-sc-l { font-size: 0.55rem; letter-spacing: 0.03em; }
  }
</style>
