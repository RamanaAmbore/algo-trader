/**
 * ORDER_TABS — single source of truth for the SymbolPanel / orders-page
 * tab strip identifiers and labels.
 *
 * Consumers that need extra visual metadata (dot colour, activeTxt, etc.)
 * should spread these entries and add their own fields on top rather than
 * duplicating the id/label pair.
 */

export const ORDER_TABS = /** @type {const} */ ([
  { id: /** @type {'chain'}   */ ('chain'),   label: 'Chain' },
  { id: /** @type {'ticket'}  */ ('ticket'),  label: 'Order ticket' },
  { id: /** @type {'command'} */ ('command'), label: 'Command line' },
]);

/** @type {ReadonlyArray<'chain' | 'ticket' | 'command'>} */
export const ORDER_TAB_IDS = ORDER_TABS.map(t => t.id);
