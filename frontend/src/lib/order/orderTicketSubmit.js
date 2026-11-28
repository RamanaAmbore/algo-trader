// orderTicketSubmit.js — Pure helpers extracted from OrderTicket.svelte submit().
//
// All functions are side-effect-free: no Svelte reactive reads, no module-level
// state. OrderTicket passes a typed `ctx` bag containing all resolved values so
// these helpers can be unit-tested in isolation (follow indicators.test.js pattern).
//
// Section order mirrors the submit() flow:
//   1. classifyIntent      — close vs open detection
//   1b. marginChipMode     — cash/margin × open/close chip-label mode (2026-11)
//   2. buildModifyPayload  — modify-branch request body
//   3. buildOnSubmitPayload — outer onSubmit payload (all modes)
//   4. buildPlacePayload   — placeTicketOrder arguments (paper/live)
//   5. formatPlacementOk   — inline success message after place
//   6. nextTriggerState    — D4: atomic counter-prop dispatch guard
//   7. formatSubmitLabel   — R7: pre-submit button label (pending action)

/**
 * Determine order intent ('close' | 'open') based on the operator's
 * existing position and the direction of the new order.
 *
 *   Long  position (currentQty > 0) + SELL → close
 *   Short position (currentQty < 0) + BUY  → close
 *   Everything else                         → open
 *
 * @param {number} currentQty  Signed held quantity (0 when no position)
 * @param {'BUY'|'SELL'} side  Direction of the new order
 * @returns {'close'|'open'}
 */
export function classifyIntent(currentQty, side) {
  if (Number(currentQty) > 0 && side === 'SELL') return 'close';
  if (Number(currentQty) < 0 && side === 'BUY')  return 'close';
  return 'open';
}

/**
 * Classify which resource (cash vs margin) an order touches, and
 * whether that resource is being consumed (open/add) or returned
 * (close) — drives the margin/cash chip's label in OrderTicket +
 * SymbolPanel (2026-11 real-money audit fix, operator-corrected).
 *
 * Four cases, option legs (equity has its own narrower close case —
 * see below):
 *
 *   BUY  to open/add a LONG option   → 'cash_cost'       (pay premium)
 *   SELL to open/add a SHORT option  → 'margin_required' (margin blocked)
 *   SELL to close a LONG option      → 'cash_released'   (premium returns)
 *   BUY  to close a SHORT option     → 'margin_released' (margin frees up)
 *
 * The exposure side an order TOUCHES is independent of its own
 * direction: a BUY either opens/adds a long (consumes cash) or closes
 * a short (releases margin); a SELL either opens/adds a short
 * (consumes margin) or closes a long (releases cash). This is the
 * corrected model — an earlier draft of this fix wrongly treated every
 * BUY as a cash event regardless of close/open; operator correction:
 * "when you are short and when you close it you release margin."
 *
 * Magnitude awareness (2026-11 audit follow-up, fix #2) — `classifyIntent`
 * only checks the SIGN of the order relative to the held position, not
 * the MAGNITUDE. A SELL of 3 lots against a long 1-lot position does not
 * fully close — it closes the 1 lot (cash released) and OPENS a new
 * 2-lot short for the remainder (margin required). Showing a flat
 * "cash released" label for the WHOLE order overstates the credit and
 * hides the margin requirement for the remainder — the real-money risk
 * this fix closes. `orderQty` (the new order's own contract count — NOT
 * lots) is optional; when omitted, behaviour is byte-identical to
 * before this fix (every existing caller that doesn't pass it keeps the
 * sign-only classification). When supplied, an order is only treated as
 * "closing" when its FULL quantity fits within the held position
 * (`|orderQty| <= |currentQty|`, the same `<=` containment rule
 * `resolveLegHeldContext`/`isCloseIntentLeg` already use) — an oversized
 * close is reclassified as the OPEN-side remainder's resource instead
 * (long + oversized SELL → 'margin_required'; short + oversized BUY →
 * 'cash_cost'), which the existing 4-value enum already expresses with
 * no new literal needed, and is the dominant/larger real exposure the
 * operator needs to see before submitting.
 *
 * @param {{isEquity: boolean, kind: string, side: 'BUY'|'SELL', currentQty: number, orderQty?: number|null}} args
 * @returns {'cash_cost'|'margin_required'|'cash_released'|'margin_released'}
 */
export function marginChipMode({ isEquity, kind, side, currentQty, orderQty = null }) {
  // A full close requires the WHOLE order to fit inside the held
  // position — an oversized "close" also opens a new position for the
  // remainder, and the margin/cash resource that remainder touches is
  // the one the operator most needs to see (fix #2). Omitted orderQty
  // (not every caller resolves it yet) keeps the pre-fix sign-only rule.
  const fitsWithinHeld = orderQty == null
    || Math.abs(Number(orderQty)) <= Math.abs(Number(currentQty));

  if (isEquity) {
    // Equity close case (2026-11 audit follow-up, fix #3) — selling an
    // existing long equity position releases cash (the sale proceeds),
    // same framing as closing a long option. Every other equity case
    // (opening a fresh buy, or any BUY at all — this app's equity model
    // has no short-sell/margin concept) stays 'cash_cost', deliberately
    // NOT a full mirror of the option branch below: holdings sold today
    // appear as a negative CNC *position* row (see CLAUDE.md's
    // "Holdings sold → P&L splits..." invariant), so symmetrically
    // treating a BUY as "closing a short" here would misclassify a
    // same-day re-buy as releasing margin, which equity never has.
    const closingEquity = side === 'SELL' && Number(currentQty) > 0 && fitsWithinHeld;
    return closingEquity ? 'cash_released' : 'cash_cost';
  }
  const isOption = kind === 'CE' || kind === 'PE';
  if (!isOption) {
    // Futures / unresolved kind — margin-mode only, same as a naked
    // short option open. Closing a future still frees margin, but that
    // framing is explicitly out of scope for this fix (see handback
    // report) — kept as plain 'margin_required' to match pre-fix
    // behaviour for this leg type exactly.
    return 'margin_required';
  }
  const closing = classifyIntent(currentQty, side) === 'close' && fitsWithinHeld;
  const touchesLong = side === 'BUY' ? !closing : closing;
  if (touchesLong) return closing ? 'cash_released' : 'cash_cost';
  return closing ? 'margin_released' : 'margin_required';
}

/**
 * Resolve the chip-label `cashFlowKind` a HOST component (SymbolPanel)
 * should render, given the `meta` object OrderTicket's own `_chipMeta`
 * emits via `onMarginUpdate`.
 *
 * Audit fix (2026-11, duplicated-logic audit) — SymbolPanel.svelte used to
 * fall back to a crude two-value `isCashMode ? 'cash_cost' : 'margin_required'`
 * expression (in TWO places) whenever `meta.cashFlowKind` wasn't present
 * yet — a subset of `marginChipMode`'s real 4-value, close/open-aware
 * decision that could disagree with the canonical classifier on a close
 * order (e.g. a BUY closing a short option releases margin, not "cash" —
 * the crude fallback has no way to express that). `meta.cashFlowKind` is
 * ALWAYS populated by OrderTicket's own `_chipMeta` derived once it has
 * fired at least once; this fallback therefore only matters in the brief
 * window before that first emission (or if `meta` itself is entirely
 * absent). Rather than guessing with the crude mapping, it now calls the
 * SAME canonical `marginChipMode()` with whatever inputs are actually
 * available — never a second, independent classification.
 *
 * @param {{isCashMode?: boolean, cashFlowKind?: 'cash_cost'|'margin_required'|'cash_released'|'margin_released', kind?: string, side?: 'BUY'|'SELL'}|null|undefined} meta
 * @param {{currentQty: number, orderQty?: number|null}} ctx
 *   The SAME resolved currentQty/orderQty OrderTicket itself would use
 *   for this symbol/account/order (SymbolPanel mirrors these via
 *   `_ticketState`, the `onTicketStateChange` payload — see
 *   `_effTicketCurrentQty` / `_ticketState.qty` in SymbolPanel.svelte).
 * @returns {'cash_cost'|'margin_required'|'cash_released'|'margin_released'}
 */
export function resolveChipCashFlowKind(meta, { currentQty, orderQty = null }) {
  if (meta && meta.cashFlowKind) return meta.cashFlowKind;
  // Side genuinely unknown (no chipMeta emitted yet) — pass through as-is
  // rather than guessing BUY/SELL; marginChipMode's own branches already
  // degrade safely (treats it as "not closing") for a side that matches
  // neither literal, same as the pre-fix fallback's side-agnostic default.
  const side = /** @type {'BUY'|'SELL'} */ (meta?.side ?? null);
  return marginChipMode({
    isEquity: meta?.kind === 'EQ',
    kind: meta?.kind ?? '',
    side,
    currentQty,
    orderQty,
  });
}

/**
 * Build the request body for action='modify' (PUT /api/orders/{id}).
 *
 * Defense-in-depth (audit C1, 2026-09): `ctx.qty` is the ticket's resolved
 * quantity (`_lots × _lotSize`), which can silently diverge from the order's
 * real size when the lot-size resolves late or the row's raw `quantity`
 * field carries a unit mismatch (see feedback_option_math_qty_vs_lots).
 * A price/trigger-only edit must never re-send a recomputed `quantity` — so
 * `quantity` is included in the payload ONLY when it genuinely differs from
 * `ctx.originalQty` (the order's untouched quantity, echoed straight from
 * the broker row into the ticket's `qty` prop). When `originalQty` isn't
 * supplied, falls back to always including it (legacy behaviour).
 *
 * @param {{
 *   account:      string,
 *   qty:          number|string,
 *   originalQty?: number|string|null,
 *   showLimit:    boolean,
 *   showTrigger:  boolean,
 *   roundToTick:  (v: number|string) => number,
 *   price:        number|string,
 *   trigger:      number|string,
 *   type:         string,
 *   variety:      string,
 *   validity:     string,
 * }} ctx
 * @returns {object}
 */
export function buildModifyPayload(ctx) {
  const nextQty = Number(ctx.qty) || 0;
  const origQty = ctx.originalQty != null && ctx.originalQty !== ''
    ? Number(ctx.originalQty)
    : null;
  const qtyChanged = origQty == null || nextQty !== origQty;
  return {
    account:       ctx.account,
    quantity:      qtyChanged ? (nextQty || undefined) : undefined,
    price:         ctx.showLimit   ? ctx.roundToTick(ctx.price)   : null,
    trigger_price: ctx.showTrigger ? ctx.roundToTick(ctx.trigger) : null,
    order_type:    ctx.type,
    variety:       ctx.variety,
    validity:      ctx.validity,
  };
}

/**
 * Build the payload threaded into `onSubmit(payload)` for all modes
 * (draft / paper / live). This is the outer shape that the caller
 * always receives; `broker_response` is merged in at call time.
 *
 * @param {{
 *   mode:         string,
 *   action:       string,
 *   symbol:       string,
 *   exchange:     string,
 *   side:         'BUY'|'SELL',
 *   qty:          number|string,
 *   product:      string,
 *   type:         string,
 *   variety:      string,
 *   validity:     string,
 *   showLimit:    boolean,
 *   showTrigger:  boolean,
 *   roundToTick:  (v: number|string) => number,
 *   price:        number|string,
 *   trigger:      number|string,
 *   account:      string,
 *   chase:        boolean,
 *   chaseAgg:     string,
 * }} ctx
 * @returns {object}
 */
export function buildOnSubmitPayload(ctx) {
  return {
    mode:           ctx.mode,
    action:         ctx.action,
    symbol:         ctx.symbol,
    exchange:       ctx.exchange,
    side:           ctx.side,
    quantity:       Number(ctx.qty),
    product:        ctx.product,
    order_type:     ctx.type,
    variety:        ctx.variety,
    validity:       ctx.validity,
    price:          ctx.showLimit   ? ctx.roundToTick(ctx.price)   : null,
    trigger_price:  ctx.showTrigger ? ctx.roundToTick(ctx.trigger) : null,
    account:        ctx.account,
    chase:               ctx.showLimit ? ctx.chase : false,
    chase_aggressiveness: ctx.showLimit && ctx.chase ? ctx.chaseAgg : 'low',
  };
}

/**
 * Build the arguments object for placeTicketOrder() (paper / live paths).
 *
 * v2 API convention (2026-07-08): request `quantity` is LOTS for F&O
 * (lotSize > 1) and raw shares for equity (lotSize <= 1). Backend
 * multiplies lots × lot_size to derive contracts internally. This
 * eliminates the class of qty/lot confusion that caused the CRUDEOIL
 * 100× oversize incident (2026-07-01).
 *
 * @param {{
 *   mode:                       string,
 *   side:                       'BUY'|'SELL',
 *   resolvedSymbol:             string|null,
 *   symbol:                     string,
 *   exchange:                   string,
 *   resolvedExchange:           string,
 *   qty:                        number|string,
 *   lots:                       number,
 *   lotSize:                    number,
 *   currentQty:                 number,
 *   product:                    string,
 *   type:                       string,
 *   variety:                    string,
 *   validity:                   string,
 *   showLimit:                  boolean,
 *   showTrigger:                boolean,
 *   roundToTick:                (v: number|string) => number,
 *   price:                      number|string,
 *   trigger:                    number|string,
 *   account:                    string,
 *   chase:                      boolean,
 *   chaseAgg:                   string,
 *   strategyId:                 number|null,
 *   draftId:                    number|null,
 * }} ctx
 * @returns {object}
 */
export function buildPlacePayload(ctx) {
  // F&O send LOTS; equity sends raw shares (Qty). lotSize > 1 marks
  // an F&O contract with a real lot convention. lotSize == 1 (equity)
  // and lotSize == 0 (cash equity) both fall through to raw qty.
  const isFO = Number(ctx.lotSize) > 1;
  const requestQty = isFO
    ? Math.max(1, Number(ctx.lots) || 1)
    : Number(ctx.qty);
  return {
    mode:             ctx.mode,
    side:             ctx.side,
    tradingsymbol:    ctx.resolvedSymbol || ctx.symbol,
    exchange:         ctx.exchange || ctx.resolvedExchange || 'NFO',
    quantity:         requestQty,
    lot_size_hint:    ctx.lotSize > 0 ? Number(ctx.lotSize) : null,
    intent:           classifyIntent(ctx.currentQty, ctx.side),
    // Sprint 2b (docs/proposals/SPRINT2_LAYER_INTEGRATION.md §3/§4.4) —
    // this is OrderTicket.svelte's one and only placement call site
    // (Chain/CommandLine submit through their own request literals, not
    // this helper), so 'ticket' is always correct here regardless of
    // which tab opened the ticket.
    source:           'ticket',
    product:          ctx.product,
    order_type:       ctx.type,
    variety:          ctx.variety,
    validity:         ctx.validity,
    price:            ctx.showLimit   ? ctx.roundToTick(ctx.price)   : null,
    trigger_price:    ctx.showTrigger ? ctx.roundToTick(ctx.trigger) : null,
    account:          ctx.account,
    chase:                ctx.showLimit ? ctx.chase : false,
    chase_aggressiveness: ctx.showLimit && ctx.chase ? ctx.chaseAgg : 'low',
    strategy_id:                  ctx.strategyId,
    // Draft cleanup (2026-10) — set only when this ticket originated from
    // a server-persisted draft row (see payoffDrafts.svelte.js). On
    // CONFIRMED success the backend deletes the draft itself; on any
    // failure it's left untouched (backend/api/schemas.py:TicketOrderRequest.draft_id).
    draft_id:                     ctx.draftId ?? null,
  };
}

/**
 * Build the inline success message shown in the modal after a placed order.
 * Backend returns {order_id, mode, status, detail}.
 *
 * Callers pass a pre-formatted `symbolLabel` (result of `formatSymbol(symbol)`)
 * so this helper has no Svelte/$lib dependency and can be unit-tested in Node.
 *
 * @param {{
 *   mode:         string,
 *   side:         string,
 *   qty:          number|string,
 *   symbolLabel:  string,
 *   showLimit:    boolean,
 *   price:        number|string,
 *   roundedPrice: number,
 *   orderId:      string|number,
 * }} ctx
 * @returns {string}
 */
export function formatPlacementOk(ctx) {
  const px = ctx.showLimit && ctx.price ? `@₹${ctx.roundedPrice}` : '@MKT';
  return (
    `${(ctx.mode || '').toUpperCase()} ${ctx.side} ${ctx.qty} ${ctx.symbolLabel} ${px} · ` +
    `#${ctx.orderId}`
  );
}

/**
 * D4 fix (2026-09) — atomic guard for the counter-prop dispatch pattern
 * host pages use to fire OrderTicket's submit without a function-ref
 * binding (`triggerSubmit++`). The bug: the previous inline effect only
 * updated its "last seen" counter when it did NOT early-return on
 * `submitting`, so once `submitting` flipped back to `false` the effect
 * re-ran, still saw the stale mismatch from the ORIGINAL click, and
 * fired `submit()` a second time — a delayed duplicate order from what
 * felt like one click plus one impatient re-click.
 *
 * This helper makes the "have I seen this trigger value" bookkeeping
 * atomic with the guard check: the caller updates its `seen` counter
 * from the returned value UNCONDITIONALLY, every call, regardless of
 * whether `fire` is true — so a rerun after `submitting` flips false
 * never re-evaluates against a stale trigger value.
 *
 * @param {number} prevSeen   Last trigger value this guard has recorded.
 *                            -1 means "never seen a trigger yet" (initial
 *                            mount) — never fires on the first render.
 * @param {number} trigger    Current value of the host's counter prop.
 * @param {boolean} submitting Whether a submit is already in flight.
 * @returns {{ fire: boolean, seen: number }}
 *          `seen` — the caller's new "last seen" value (always `trigger`).
 *          `fire` — true only when this is a genuinely new trigger value
 *          AND no submit is currently in flight.
 */
export function nextTriggerState(prevSeen, trigger, submitting) {
  const fire = prevSeen >= 0 && trigger !== prevSeen && !submitting;
  return { fire, seen: trigger };
}

/**
 * R7 fix (2026-09) — the Submit button's label should always reflect
 * the REAL pending action about to fire (side, verb, qty), not just a
 * bare "Submit" — this was the root of the operator's original report
 * ("close buy close sell buttons don't work"): the CLOSE/BUY pills are
 * side SELECTORS, not submit buttons, and with chase on (the default
 * for LIMIT/SL tickets) the actual submit button just said "Submit"
 * with no hint of what it would do.
 *
 * Mirrors `formatPlacementOk`'s existing "LIVE BUY 75 X at-price" convention
 * so the pre-submit label and the post-submit success line read
 * consistently. Symbol is intentionally NOT included — callers put it
 * in a `title` tooltip instead (the label must fit a <600px footer).
 *
 * @param {{
 *   side:        'BUY'|'SELL'|null,
 *   currentQty:  number,
 *   qty:         number|string,
 *   basketCount: number,
 * }} ctx
 * @returns {string}
 */
export function formatSubmitLabel(ctx) {
  // Chain tab (basket legs staged) — operator (2026-09-29): "in chain
  // the button label should submit" — plain label, no leg count.
  if (ctx.basketCount > 0) return 'Submit';
  // Bare fallback for the no-side edge case (button is effectively
  // inert here — side always defaults to BUY in practice). Only place
  // this function still returns the word "Submit" at all.
  if (!ctx.side) return 'Submit';
  const cq = Number(ctx.currentQty) || 0;
  // Lot size dropped from the label — operator (2026-09-29): "remove
  // lot size from submit buttons in order ticket". The stepper next to
  // the button already shows qty; repeating it here was redundant.
  // "Submit" prefix dropped entirely — operator (2026-09-30): "for
  // order ticket the submit button should not have Submit in the
  // label". Side/verb alone is sufficient context (SideToggle's own
  // pills already show the same ADD/CLOSE + BUY/SELL text).
  if (cq === 0) return ctx.side;
  // ADD = same direction as the existing position; CLOSE = opposite.
  const verb = (cq > 0 ? (ctx.side === 'BUY' ? 'ADD' : 'CLOSE')
                       : (ctx.side === 'BUY' ? 'CLOSE' : 'ADD'));
  // Verb/side pair uses a slash — operator (2026-09-29): "add . sell
  // should be add/sell, close . buy as close/buy... the space dot
  // space should be a slash."
  return `${verb}/${ctx.side}`;
}
