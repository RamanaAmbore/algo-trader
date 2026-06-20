/**
 * ORDER_TABS — single source of truth for the SymbolPanel / orders-page
 * tab strip identifiers and labels.
 *
 * Consumers that need extra visual metadata (dot colour, activeTxt, etc.)
 * should spread these entries and add their own fields on top rather than
 * duplicating the id/label pair.
 */

// Command Line tab retired per operator request — account + symbol now
// live in the header so the standalone command-line surface added no
// affordance beyond what the Ticket and Chain tabs already provide.
// /console retains its own command surface (different lifecycle).
// Operator: "order ticket should be first tab and chain should be
// second tab." Ticket is the single-leg fast path the operator hits
// most often; Chain stays as the second tab for basket-builder flows.
export const ORDER_TABS = /** @type {const} */ ([
  { id: /** @type {'ticket'}  */ ('ticket'),  label: 'Ticket' },
  { id: /** @type {'chain'}   */ ('chain'),   label: 'Chain' },
]);

/** @type {ReadonlyArray<'chain' | 'ticket'>} */
export const ORDER_TAB_IDS = ORDER_TABS.map(t => t.id);
