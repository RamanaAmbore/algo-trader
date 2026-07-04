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
 * @param {{
 *   account:      string,
 *   qty:          number|string,
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
  return {
    account:       ctx.account,
    quantity:      Number(ctx.qty) || undefined,
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
 * @param {{
 *   mode:                       string,
 *   side:                       'BUY'|'SELL',
 *   resolvedSymbol:             string|null,
 *   symbol:                     string,
 *   exchange:                   string,
 *   resolvedExchange:           string,
 *   qty:                        number|string,
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
  return {
    mode:             ctx.mode,
    side:             ctx.side,
    tradingsymbol:    ctx.resolvedSymbol || ctx.symbol,
    exchange:         ctx.exchange || ctx.resolvedExchange || 'NFO',
    quantity:         Number(ctx.qty),
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
