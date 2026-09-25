// orderTicketSubmit.js — Pure helpers extracted from OrderTicket.svelte submit().
//
// All functions are side-effect-free: no Svelte reactive reads, no module-level
// state. OrderTicket passes a typed `ctx` bag containing all resolved values so
// these helpers can be unit-tested in isolation (follow indicators.test.js pattern).
//
// Section order mirrors the submit() flow:
//   1. numericOverride     — template param coercion
//   2. classifyIntent      — close vs open detection
//   3. buildModifyPayload  — modify-branch request body
//   4. buildOnSubmitPayload — outer onSubmit payload (all modes)
//   5. buildPlacePayload   — placeTicketOrder arguments (paper/live)
//   6. formatPlacementOk   — inline success message after place
//   7. nextTriggerState    — D4: atomic counter-prop dispatch guard
//   8. formatSubmitLabel   — R7: pre-submit button label (pending action)

/**
 * Coerce a template-override field.
 * Empty string means "use template default" → null.
 * Any other value is converted to a Number.
 *
 * @param {number|string} v
 * @returns {number|null}
 */
export function numericOverride(v) {
  return v !== '' ? Number(v) : null;
}

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
 *   templateId:                 number|null,
 *   tpOverride:                 number|string,
 *   slOverride:                 number|string,
 *   wingPremPctOverride:        number|string,
 *   wingStrikeOffsetOverride:   number|string,
 *   strategyId:                 number|null,
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
    product:          ctx.product,
    order_type:       ctx.type,
    variety:          ctx.variety,
    validity:         ctx.validity,
    price:            ctx.showLimit   ? ctx.roundToTick(ctx.price)   : null,
    trigger_price:    ctx.showTrigger ? ctx.roundToTick(ctx.trigger) : null,
    account:          ctx.account,
    chase:                ctx.showLimit ? ctx.chase : false,
    chase_aggressiveness: ctx.showLimit && ctx.chase ? ctx.chaseAgg : 'low',
    template_id:                  ctx.templateId,
    tp_pct_override:              numericOverride(ctx.tpOverride),
    sl_pct_override:              numericOverride(ctx.slOverride),
    wing_premium_pct_override:    numericOverride(ctx.wingPremPctOverride),
    wing_strike_offset_override:  numericOverride(ctx.wingStrikeOffsetOverride),
    strategy_id:                  ctx.strategyId,
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
  if (ctx.basketCount > 0) return `Submit (${ctx.basketCount})`;
  if (!ctx.side) return 'Submit';
  const qty = Number(ctx.qty) || 0;
  const qtySuffix = qty > 0 ? ` ${qty}` : '';
  const cq = Number(ctx.currentQty) || 0;
  if (cq === 0) return `Submit · ${ctx.side}${qtySuffix}`;
  // ADD = same direction as the existing position; CLOSE = opposite.
  const verb = (cq > 0 ? (ctx.side === 'BUY' ? 'ADD' : 'CLOSE')
                       : (ctx.side === 'BUY' ? 'CLOSE' : 'ADD'));
  return `Submit · ${verb} · ${ctx.side}${qtySuffix}`;
}
