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
export const ORDER_TABS = /** @type {const} */ ([
  { id: /** @type {'chain'}   */ ('chain'),   label: 'Chain' },
  { id: /** @type {'ticket'}  */ ('ticket'),  label: 'Order ticket' },
]);

/** @type {ReadonlyArray<'chain' | 'ticket'>} */
export const ORDER_TAB_IDS = ORDER_TABS.map(t => t.id);
