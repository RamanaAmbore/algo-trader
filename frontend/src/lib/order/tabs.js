/**
 * ORDER_TABS — single source of truth for the SymbolPanel / orders-page
 * tab strip identifiers and labels.
 *
 * Consumers that need extra visual metadata (dot colour, activeTxt, etc.)
 * should spread these entries and add their own fields on top rather than
 * duplicating the id/label pair.
 */

// Command Line tab retired per operator request (2026-06, commit
// a21d5411) — account + symbol now live in the header so the standalone
// command-line surface added no affordance beyond what the Ticket and
// Chain tabs already provide. /console kept its own command surface at
// the time (a different lifecycle).
// Operator: "order ticket should be first tab and chain should be
// second tab." Ticket is the single-leg fast path the operator hits
// most often; Chain stays as the second tab for basket-builder flows.
//
// Restored 2026-10 (Phase 2, Sprint B — Console retirement) — this is
// NOT a reversal of the 2026-06 decision above; it's a different tab.
// `/console` is retired as a standalone route this phase (its content
// moves to /automation's new Console tab, full statement scope). The
// generic agent-CLI grammar (agents.js/AgentCliEditor) already compiles
// a plain `order(...)`/`place_order(...)` statement in addition to
// agent/bracket/token forms, so this tab mounts that SAME editor here,
// scoped via `allowedKinds: ['order']` (enforced in the compiler itself
// — see agents.js:compileStatement) so bracket/agent/token syntax is
// rejected with a clear message rather than silently doing nothing. It
// never reintroduces the old positional-only CommandLineTab/orders.js
// grammar this tab replaced in 2026-06 — that component (and its
// CommandBar.svelte host and orders.js grammar) was deleted entirely
// on 2026-10-17 (operator-confirmed removal — see the 2026-10-17 entry
// in docs/specs/AUTOMATION_SPEC.md's change log); no
// surface in the app mounts it anymore.
export const ORDER_TABS = /** @type {const} */ ([
  { id: /** @type {'ticket'}  */ ('ticket'),  label: 'Ticket' },
  { id: /** @type {'chain'}   */ ('chain'),   label: 'Chain'  },
  { id: /** @type {'chart'}   */ ('chart'),   label: 'Chart'  },
  { id: /** @type {'cli'}     */ ('cli'),     label: 'CLI'    },
]);

/** @type {ReadonlyArray<'chain' | 'ticket' | 'chart' | 'cli'>} */
export const ORDER_TAB_IDS = ORDER_TABS.map(t => t.id);
