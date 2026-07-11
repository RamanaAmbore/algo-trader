/**
 * Broker capability → warning string helper.
 *
 * Single source of truth for the "Groww OCO emulated", "Dhan can't trail",
 * "MCX template won't attach", and "GTT fires via poll" warning text so
 * OrderTicket (single-account) and SymbolPanel (multi-account basket
 * aggregation) both render the same vocabulary.
 *
 * Pure function — no Svelte dependencies. Pass in the template + caps +
 * exchange; get back a string (empty when no warnings apply).
 */

/**
 * Build a `· `-joined warning string for ONE (template, caps, exchange)
 * combination. Returns '' when the broker covers every aspect of the
 * template natively.
 *
 * @param {any} t  selected OrderTemplate row
 * @param {any} c  BrokerCapabilities for the routing account
 * @param {string} exchange  resolved exchange (e.g. 'NFO', 'MCX', 'NSE')
 * @returns {string}
 */
export function capWarningFor(t, c, exchange) {
  if (!t || !c) return '';
  const wantsOco        = (t.tp_pct != null && t.sl_pct != null);
  const wantsTrail      = t.sl_trail_pct != null;
  // wantsGttComplex: needs OCO, trail, or scale-ladder — features that go
  // beyond a single native trigger. Used to decide whether the poll-lag
  // warning is meaningful: a TP-only template on a broker with gtt_single
  // support is working as designed and the poll lag is expected behaviour,
  // not a gap the operator needs to act on.
  const wantsGttComplex = wantsOco || wantsTrail || !!t.tp_scales_json;
  // wantsGtt: any GTT-type exit at all (including TP-only or SL-only).
  const wantsGtt        = wantsGttComplex || t.tp_pct != null || t.sl_pct != null;
  const isMcx           = ['MCX', 'NCO'].includes(String(exchange || '').toUpperCase());
  const display         = c.display_name || 'broker';

  // Poll-lag warning: only raise it when the template needs a complex GTT
  // (OCO, trail, or scale-ladder) OR when the broker has no native single-
  // GTT support. A TP-only template on a broker with gtt_single=true uses
  // that feature as intended; the poll lag is expected, not a gap.
  const checks = [
    { when: wantsOco && !c.gtt_oco,                                                          msg: `${display} OCO emulated — ~15s race window` },
    { when: wantsTrail && !c.gtt_modify,                                                      msg: `${display} can't trail — SL stays fixed` },
    { when: !!t.tp_scales_json && !c.gtt_single,                                              msg: `${display} has no GTT — scale-out won't attach` },
    { when: isMcx && c.gtt_supports_mcx === false,                                            msg: `${display} GTT can't run on MCX — template won't attach` },
    { when: c.postback_gtt === 'poll_only' && (wantsGttComplex || !c.gtt_single) && wantsGtt, msg: `${display} GTT fires via poll — up to ~15s detection lag` },
  ];
  return checks.filter(x => x.when).map(x => x.msg).join(' · ');
}

/**
 * Aggregate cap warnings across N (account, caps, exchange) tuples. Used
 * by SymbolPanel when a basket spans 2+ accounts on different brokers
 * (Kite + Dhan + Groww etc) — the operator sees the union of broker-
 * specific gaps before submit. De-dupes strings: a "Groww OCO emulated"
 * warning fires once even when 3 Groww legs are in the basket. Each
 * surviving warning gets prefixed with the account that triggered it
 * so the operator can map back to which leg needs attention.
 *
 * @param {any} template  selected OrderTemplate row (shell template)
 * @param {Array<{account:string, caps:any, exchange:string}>} accountCaps
 * @returns {string}
 */
export function aggregateCapWarnings(template, accountCaps) {
  if (!template || !accountCaps || accountCaps.length === 0) return '';
  /** @type {Map<string, string[]>} */
  const byMsg = new Map();
  for (const { account, caps, exchange } of accountCaps) {
    const w = capWarningFor(template, caps, exchange);
    if (!w) continue;
    // Each capWarningFor call returns ` · `-joined messages — split + dedupe.
    for (const piece of w.split(' · ')) {
      const k = piece.trim();
      if (!k) continue;
      if (!byMsg.has(k)) byMsg.set(k, []);
      byMsg.get(k).push(account);
    }
  }
  if (byMsg.size === 0) return '';
  /** @type {string[]} */
  const out = [];
  for (const [msg, accts] of byMsg) {
    // Tag the message with the account(s) it applies to so the
    // operator can identify which leg to fix. Short masked form so
    // long basket strips don't blow up the chip horizontally.
    const tag = accts.length === 1 ? accts[0] : `${accts.length} accts`;
    out.push(`[${tag}] ${msg}`);
  }
  return out.join(' · ');
}
