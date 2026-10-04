/**
 * orderTimelineLogic.js
 *
 * Pure helpers for OrderTimelineDrawer.svelte's event grouping/sort/payload
 * parsing — extracted into an importable module so Vitest can cover the
 * logic without mounting the component (matches the ChaseCard.inflight /
 * orderStatusPredicates pure-function test precedent).
 *
 * Sprint 2a fix (docs/proposals/SPRINT2_LAYER_INTEGRATION.md §1 finding 1,
 * §4.2): the drawer previously read invented fields (`ev.symbol`, `ev.side`,
 * `ev.qty`, `ev.mode`, `ev.created_at`, `ev.price`/`ev.limit_price`) that
 * don't exist on the real `AlgoOrderEventInfo` response
 * (backend/api/routes/orders_helpers.py:591-598): `id, order_id, ts, kind,
 * message, payload_json`.
 *
 * Per-order context (symbol/side/qty/mode) is NOT on the event row — every
 * event-writing call site was checked directly (chase.py:1495-1529,
 * orders_place.py:1937/2173-2190, orders_postback.py:297/561-584, paper.py:
 * 122-139/924-994): none of them consistently puts symbol/side/qty/mode in
 * `payload_json` (the live-ticket "placed" event writes no payload at all).
 * That context must come from the caller's own AlgoOrderInfo lookup
 * (`fetchAlgoOrdersRecent`), passed in here as `orderContextById`, keyed by
 * AlgoOrder id (`transaction_type` → side, `quantity` → qty).
 */

/** Kinds that mark an order's timeline as "done" — matches chase.py /
 *  order_events.py's own terminal vocabulary for fill/unfill/reject/cancel. */
export const TERMINAL_KINDS = new Set(['fill', 'unfill', 'reject', 'cancel']);

/** Safe JSON.parse — `payload_json` can be null, a masked string (demo/
 *  partner callers), or malformed; never throws, always returns a plain
 *  object (never null/array/primitive). */
export function parseEventPayload(payload_json) {
  if (!payload_json) return {};
  try {
    const parsed = JSON.parse(payload_json);
    return (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) ? parsed : {};
  } catch {
    return {};
  }
}

/**
 * Best-available price for display, checked in priority order across the
 * real payload shapes seen in backend event producers:
 *   chase_modify (chase.py), postback/cancel (orders_postback.py,
 *   orders.py), paper "placed" ticket   → payload.price
 *   paper terminal "fill" (paper.py)     → payload.fill_price
 *   paper engine "placed" register       → payload.limit_price / initial_price
 * Returns null (no price chip shown) when none are present — e.g. the live
 * ticket's own "placed" event writes no payload at all.
 */
export function extractEventPrice(payload) {
  if (!payload) return null;
  const v = payload.price ?? payload.fill_price ?? payload.limit_price ?? payload.initial_price;
  return v == null ? null : Number(v);
}

/** One event shaped for display — real fields only (`kind`/`ts`/`message`),
 *  plus a `price` derived from `payload_json`. */
export function toDisplayEvent(ev) {
  const payload = parseEventPayload(ev?.payload_json);
  return {
    id: ev?.id,
    order_id: ev?.order_id,
    ts: ev?.ts ?? '',
    kind: ev?.kind ?? '',
    message: ev?.message ?? '',
    price: extractEventPrice(payload),
  };
}

/** True once ANY event in the list carries a terminal kind. */
export function isTerminalSection(events) {
  return (events ?? []).some((e) => TERMINAL_KINDS.has(e.kind));
}

/** Latest parseable timestamp (ms epoch) across a list of display events;
 *  0 when none parse — pushes unknown-activity sections to the back of
 *  their group rather than crashing the sort. */
export function latestEventTs(events) {
  let max = 0;
  for (const e of events ?? []) {
    const t = Date.parse(e.ts || '');
    if (!Number.isNaN(t) && t > max) max = t;
  }
  return max;
}

/**
 * Group a flat `AlgoOrderEventInfo[]` by `order_id`, attach per-order
 * context (symbol/side/qty/mode) from `orderContextById`, and sort:
 * non-terminal sections first, terminal last; within each group, most
 * recent activity (max event `ts`, not the first/oldest event — events
 * arrive oldest-first from the backend) sorts first.
 *
 * `orderContextById` — `{[order_id]: {symbol, side, qty, mode}}`. Missing
 * context (order not yet in the operator's recent-orders cache) falls back
 * to neutral empty values — deliberately NOT a `'paper'` mode default,
 * which was itself part of the original bug (a live order silently
 * rendering a PAPER pill).
 */
/**
 * Resolves the canonical order id from either row shape OrderBook.svelte
 * merges — broker `OrderRow` carries `order_id`, `AlgoOrderInfo` carries
 * `id` — matching the `o.order_id ?? o.id` pattern already used inline
 * for the `{#each}` key in OrderBook.svelte. Returns null when neither
 * is present (defensive — should not happen for a real row).
 */
export function resolveOrderId(order) {
  return order?.order_id ?? order?.id ?? null;
}

/**
 * Builds a single-entry `orderContext` map (`{[id]: {symbol, side, qty,
 * mode}}`) for OrderTimelineDrawer, from an already-in-memory OrderBook
 * row — no extra fetch needed since the row itself carries
 * symbol/side/qty/mode (unlike the bare event rows). `idOverride` lets a
 * caller supply the id explicitly (e.g. a linked-order chip click, where
 * only the id is known up front and the matching row is looked up
 * separately). Returns `{}` when no id can be resolved at all.
 */
export function buildOrderContextEntry(order, idOverride = null) {
  const id = idOverride ?? resolveOrderId(order);
  if (id == null) return {};
  return {
    [id]: {
      symbol: order?.tradingsymbol || order?.symbol || '',
      side:   order?.transaction_type ?? '',
      qty:    order?.quantity ?? null,
      mode:   order?.mode ?? '',
    },
  };
}

/**
 * Builds the `linkedOrders` shape for OrderTimelineDrawer's chip strip
 * from a raw order row. Returns `null` when none of `parent_order_id` /
 * `child_order_ids` / `basket_tag` are set, so the drawer can gate
 * rendering on a single falsy check rather than three per-field ones.
 */
export function buildLinkedOrders(order) {
  const parent = order?.parent_order_id ?? null;
  const children = Array.isArray(order?.child_order_ids)
    ? order.child_order_ids.filter((id) => id != null)
    : [];
  const basketTag = order?.basket_tag || null;
  if (parent == null && children.length === 0 && !basketTag) return null;
  return { parent_order_id: parent, child_order_ids: children, basket_tag: basketTag };
}

export function groupOrderEvents(events, orderContextById = {}) {
  const map = new Map();
  for (const ev of events ?? []) {
    const id = ev?.order_id;
    if (id == null) continue;
    if (!map.has(id)) {
      const ctx = orderContextById[id] ?? orderContextById[String(id)] ?? {};
      map.set(id, {
        order_id: id,
        symbol: ctx.symbol ?? '',
        side: ctx.side ?? '',
        qty: ctx.qty ?? null,
        mode: ctx.mode ?? '',
        events: [],
      });
    }
    map.get(id).events.push(toDisplayEvent(ev));
  }
  const sections = Array.from(map.values());
  return sections.sort((a, b) => {
    const at = isTerminalSection(a.events);
    const bt = isTerminalSection(b.events);
    if (at !== bt) return at ? 1 : -1;
    return latestEventTs(b.events) - latestEventTs(a.events);
  });
}
