# Plan: Track 3, Sub-sprint T3.3 — Frontend Chain/Ticket row consolidation + safety fixes

## Task

Third of 6 sub-sprints removing Bracket/Template/GTT (full detail:
`~/.claude/plans/sparkling-dancing-lobster.md`, "Track 3" section — read it in full, especially
T3.1's and T3.2's actual findings, both already shipped and live on prod). T3.1/T3.2 deleted the
entire backend engine (`template_attach.py`, `bracket_registry.py`, the background safety-net
tasks, the Simulator's GTT engine, the DSL schema params). Nothing on the backend calls into
Bracket/template logic anymore — `/orders/ticket/preview` is stubbed (returns an empty plan, not
deleted, since the frontend still calls it every Chain-tab field change).

This sub-sprint's scope is purely frontend:
1. Merge CHASE into `OptionChainTab.svelte`'s `.oct-toolbar` (the Chain tab's Expiry/DTE/DRAFT
   row — DRAFT has lived there since 2026-11-03). Drop BRACKET entirely from both Chain's own row
   (in `SymbolPanel.svelte`) and Ticket's `.oes-chase-row` (in `OrderTicket.svelte`) — the ask is
   to remove Bracket everywhere, not scope it to Chain only. Ticket's row becomes CHASE+DRAFT with
   no further consolidation needed.
2. Relocate the Spread% override control (lives inside `TemplateBar.svelte` today, feeds Chain's
   live pre-submit bid/ask-spread risk gate — a genuine, Bracket-independent risk control that
   happens to share a UI home with Bracket's TP/SL/Wing inputs). Default: give it its own small
   control in the merged Chain row next to Expiry. Fallback if that's awkward to fit: the global
   default with no override — flag this rather than silently drop the control.
3. Neutralize the `localStorage`-persisted Bracket preference (`_readTemplPref`) and stop sending
   `template_id` in any Chain basket-leg request. One-time cleanup of the stale `localStorage` key
   on load.
4. Delete `TemplateBar.svelte` once nothing mounts it.
5. Reword the Draft InfoHint text that references Bracket's now-gone mutual-exclusion (Bracket
   ON used to turn Draft off and vice versa — that relationship no longer exists).
6. Update the e2e specs this directly breaks (listed below).

## Agents

- frontend: implement items 1-6 above.

  Before touching `SymbolPanel.svelte`/`OrderTicket.svelte`/`OptionChainTab.svelte`, read their
  CURRENT state — several recent entries in CLAUDE.md document delicate effect-gating around
  `templateId`/Bracket state in these exact files (search CLAUDE.md for "Order Ticket /
  Bracket-template re-wire", "Bracket default scope", "Chain Bracket scope change") — those
  entries describe a NOW-DEAD feature (Bracket itself), so the gating logic they describe should
  be DELETED along with the feature, not preserved. Confirm via grep that `templateId`,
  `_sharedTemplateId`, `TemplateBar`, `_onTemplSelectDefault`/`_onBracketSelectDefault`/etc. are
  fully removed from both files once done — a half-removed binding would leave dead reactive
  state.

  1. `OptionChainTab.svelte`: merge CHASE controls into the `.oct-toolbar` row alongside
     Expiry/DTE/DRAFT. Reuse the exact same CHASE toggle/picker markup and binding
     `SymbolPanel.svelte` already has for Chain (don't rebuild it) — just relocate it into this
     toolbar. `SymbolPanel.svelte`'s own `.oes-chase-row` or equivalent for Chain should no longer
     render a separate CHASE row once this lands.
  2. Drop BRACKET from Chain's row (`SymbolPanel.svelte`) and from Ticket's `.oes-chase-row`
     (`OrderTicket.svelte`). Remove `<TemplateBar>` mounts, `templateId`/`_sharedTemplateId`
     bindings, `_onTemplSelectDefault`/`_onSelectNone`/`_onSelectTemplate` and the Bracket-side
     mutual-exclusion handlers (`_onBracketSelectDefault`/`_onBracketSelectNone`/
     `_onDraftToggleChange`/`_onBracketSelectTemplate`). Ticket's row becomes CHASE+DRAFT only.
  3. Relocate the Spread% control from `TemplateBar.svelte` into its own small control in the
     merged Chain row (next to Expiry), preserving the exact gate behavior (same state variable,
     same default, same live pre-submit check) — just move where it's rendered and remove its
     dependency on `TemplateBar.svelte`'s own markup/state.
  4. `_readTemplPref`/the Bracket `localStorage` key: stop reading/writing it; add a one-time
     removal of the stale key on load (e.g. in the Chain/Ticket mount effect) so old browser
     sessions don't carry dead state forever. Stop constructing/sending `template_id` on any
     Chain basket leg or Ticket submission.
  5. Delete `TemplateBar.svelte` once grep confirms zero remaining imports/mounts of it anywhere
     in `frontend/src/`.
  6. Find and reword the Draft InfoHint text (search for "Bracket" near Draft's tooltip/InfoHint
     in `SymbolPanel.svelte`/`OrderTicket.svelte`) that describes the old mutual-exclusion
     behavior — that relationship no longer applies since Bracket is gone; Draft's tooltip should
     describe only what Draft itself does now.

  Do NOT touch: `OrderCard.svelte`'s `_bracketPillState`, `liveWingBadge.js`, `brokerCapWarnings.js`,
  the Automation page's Brackets kind-card, `admin/settings`'s template-exit-hold toggle, the MCP
  `template_slug` mint-form binding, the Console `bracket(...)` DSL token, `positionStateBadge.js`'s
  GTT branch — all of those are explicitly T3.4 (next sub-sprint, do not scope-creep into it).
  Do NOT touch anything backend (already done, T3.1/T3.2) or the live, already-shipped
  `exit-tp-short-option-90` agent infrastructure (Track 1, unrelated).

- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: update the specs this directly breaks:
  `frontend/e2e/chain_draft_toggle_on_expiry_row.spec.js`,
  `frontend/e2e/order_ticket_chase_bracket_draft_tooltips.spec.js`,
  `frontend/e2e/order_ticket_bracket_scope_ssot_and_clear_policy.spec.js`,
  `frontend/e2e/template_bar_smoke.spec.js`, `frontend/e2e/template_dropdown.spec.js`,
  `frontend/e2e/template_toggle_off.spec.js`, `frontend/e2e/ticket_no_default_template.spec.js`,
  `frontend/e2e/bracket_scope_reset_pending_label_and_held_toast.spec.js`,
  `frontend/e2e/chase_card_bracket_hold_pending_release.spec.js`,
  `frontend/e2e/order_ticket_kite_market_tp_cap_warning.spec.js`,
  `frontend/e2e/chain_spread_gate.spec.js`, `frontend/e2e/chain_live_wing_confirmation_badge.spec.js`.
  For each: if the spec tests ONLY Bracket-specific behavior that no longer exists, delete it. If
  it tests a MIX (e.g. `chain_spread_gate.spec.js` likely tests the Spread% control, which
  survives relocated — not deleted), rewrite the Bracket-specific assertions and keep/update the
  surviving ones. `chain_live_wing_confirmation_badge.spec.js` tests a feature that is now fully
  dead (Bracket wings can't fire since nothing attaches them) — delete it, and confirm via grep
  that `liveWingBadge.js`/`liveWingTracker.svelte.js` have zero other live callers before flagging
  them as a T3.4 deletion candidate (don't delete them in this sprint — that's T3.4's job — just
  don't leave a NEW reference to them in this sprint's own work).

## Tests

- pytest: no
- svelte-check: yes
- playwright: yes

## Commit message

feat(bracket-removal): T3.3 — merge CHASE into Chain's Expiry row, drop BRACKET from Chain/Ticket, relocate Spread%

## Done when

- Chain tab shows Expiry + DTE + CHASE + DRAFT in one row, with zero Bracket remnant (no
  `<TemplateBar>`, no `templateId` binding, no Bracket toggle/picker).
- Ticket tab shows CHASE + DRAFT only.
- Spread% control is still reachable and functionally unchanged (same gate, same default), now
  living in the merged Chain row instead of `TemplateBar.svelte`.
- No stray `template_id` is ever constructed or sent from the frontend.
- `TemplateBar.svelte` is deleted, confirmed via grep zero remaining imports.
- The stale Bracket `localStorage` key is cleaned up on load for old sessions.
- Draft's InfoHint text no longer references Bracket's mutual-exclusion.
- svelte-check 0 errors, Playwright specs green (updated/deleted as appropriate).
- `git diff --name-only` touches only frontend files — zero backend, zero
  `exit-tp-short-option-90`/Track-1 files.
