# Plan: Track 3, Sub-sprint T3.4 — Frontend: remaining Bracket-specific surfaces

## Task

Fourth of 6 sub-sprints removing Bracket/Template/GTT (full detail:
`~/.claude/plans/sparkling-dancing-lobster.md`, "Track 3" section — read it in full, especially
T3.1/T3.2/T3.3's actual findings, all already shipped and live on prod). T3.1-T3.2 deleted the
backend engine entirely; T3.3 removed Bracket from the Chain/Ticket order-entry rows and deleted
`TemplateBar.svelte`. This sub-sprint removes every REMAINING Bracket/Template/GTT-specific UI
surface outside the order-entry flow — including the Automation page's "Brackets" kind-card
(operator flagged this one directly: "in automation, the brackets dropdown should not exist, as
bracket are no longer available").

Remaining surfaces, per the plan's own list:
1. `OrderCard.svelte`'s `_bracketPillState` (the Pending/Tmpl Hold/Active status pill on filled
   orders) — delete.
2. `liveWingBadge.js` / `liveWingTracker.svelte.js` (the Chain strike-cell live-wing confirmation
   badge) — delete, now confirmed fully dead (T3.3's agent already confirmed no OTHER live caller
   exists before leaving them alone that sprint).
3. `brokerCapWarnings.js` (confirmed Bracket/GTT-only — every check keyed off template fields) —
   delete.
4. The Bracket-specific half of `templateAttachToast.js` — delete. **Keep `holdRecord.js` fully**
   — confirmed generic/shared across all 3 hold categories (`EXPIRY_CLOSE`, `AGENT_ORDER`, and
   Track 1's `AGENT_CLOSE`), never delete it.
5. The Automation page's "Brackets" kind-card (`frontend/src/routes/(algo)/automation/+page.svelte`)
   — the `_KIND_OPTIONS = ['agent', 'alert', 'bracket', 'order']` dropdown entry, the `rowKind ===
   'bracket'` branches, the template-catalog warm-up for the Brackets card, the mini-grid rendering
   for Brackets rows. This is the operator's directly-flagged item.
6. `/admin/settings`'s template-exit-hold toggle (`hold.template_exit_released` /
   `expiry_close_hold_enabled` is a DIFFERENT, still-live toggle for `EXPIRY_CLOSE` — do NOT touch
   that one; only the Bracket/`TEMPLATE_EXIT`-specific toggle, if a separate one still exists after
   T3.1 already removed the `TEMPLATE_EXIT` category server-side).
7. The MCP `template_slug` mint-form binding (wherever the MCP admin page lets an operator
   construct a `place_order` call with a `template_slug` param — that param no longer does
   anything server-side since T3.2 removed it from the DSL schema).
8. The Console `bracket(...)` DSL token (`frontend/src/lib/command/grammars/agents.js`).
9. The `automation/guide` doc cards describing Bracket.
10. `positionStateBadge.js` — drop the GTT branch per judgment call 11 in the plan (priority
    becomes "auto-paired > orphan", was "GTT > auto-paired > orphan").
11. Delete/edit the remaining frontend test files (Vitest + Playwright) covering any of the above.
12. Regenerate `agentsCorpus.json` / `seededEventAgents.json` fixtures if the Console grammar
    change (item 8) touches them.

## Agents

- frontend: implement items 1-10 above.

  Before touching the Automation page (item 5), read its CURRENT state in full — it's a large,
  structurally complex file (search for `_KIND_OPTIONS`, `rowKind`, `'bracket'` — CLAUDE.md
  confirms matches at lines ~64, ~123, ~151, ~203, ~393, ~397, ~784, ~900, ~1396, ~1398, ~1463 as
  of this plan's writing, but re-grep fresh, don't trust these line numbers). This card has its
  own template-catalog fetch, its own mini-grid rendering path, and its own row-kind detection —
  removing it cleanly means tracing every one of those, not just deleting the dropdown option
  (a bracket-kind row object could still exist in a user's saved agent list; decide whether to
  filter it from display entirely or show it as a read-only "retired" row — your call, state which
  you chose and why).

  For items 1-4 (OrderCard pill, liveWingBadge, brokerCapWarnings, templateAttachToast's Bracket
  half): confirm via grep that each is GENUINELY Bracket/GTT-only before deleting — e.g.
  `templateAttachToast.js`'s module header (per CLAUDE.md) explicitly separates generic
  hold-toast logic (which must survive) from Bracket-specific toast text (which should not). Read
  `holdRecord.js` first to confirm it's the shared one and does NOT get touched.

  Item 6: grep `/admin/settings` for every hold-related toggle currently rendered. T3.1 already
  removed `HoldCategory.TEMPLATE_EXIT` server-side — confirm whether a frontend toggle for it
  still exists (dead) vs. whether `expiry_close_hold_enabled` is the only one left (which is for
  `EXPIRY_CLOSE`, unrelated, must NOT be touched). Report which case you found.

  Item 7: grep the MCP admin page (`/admin/mcp` or similar) for `template_slug` in any form/mint
  UI. Remove the field; it's a dead param now.

  Item 8: remove the `bracket(...)` token from `agents.js`'s Console grammar — compiler rejection,
  decompiler mapping, and any corpus/fixture referencing it.

  Item 9: find and remove/reword the `automation/guide` doc cards describing Bracket — this is a
  cosmetic content change, not logic.

  Item 10: `positionStateBadge.js` — remove the GTT branch from the priority function (GTT >
  auto-paired > orphan → auto-paired > orphan). Confirm its two known consumers (MarketPulse
  Positions grid, derivatives Legs grid) both still render correctly with the narrowed priority.

  Do NOT touch: anything backend (all done, T3.1/T3.2), `orders_gtt.py` or its frontend viewer (if
  one exists and is independent — check before assuming), the Chain/Ticket order-entry flow
  (already done, T3.3), the live Track-1 `exit-tp-short-option-90` agent infrastructure, the
  covered-call "Held"/"H" badge system (a different concept, explicitly flagged elsewhere in
  CLAUDE.md as "do not touch without a fresh decision").

- backend: skip
- broker: skip
- doc: skip (cosmetic doc-card wording inside item 9 is bundled into the frontend agent's own
  work, not a separate doc-agent task — it's content embedded in a .svelte file, not a .md file)
- backend-test: skip
- playwright: cover items 1-10 — for each deleted/edited surface, confirm the corresponding
  Playwright/Vitest spec is deleted (if testing only dead behavior) or updated (if testing a mix).
  Specifically verify: the Automation page no longer offers a Bracket kind option in its dropdown
  (a real UI assertion, not just a source grep), `positionStateBadge.js`'s narrowed priority
  function has correct test coverage for the two remaining states, and no spec anywhere still
  references `_bracketPillState`/`liveWingBadge`/`brokerCapWarnings` as live functionality.

## Tests

- pytest: no
- svelte-check: yes
- playwright: yes

## Commit message

feat(bracket-removal): T3.4 — remove Automation Brackets kind-card, OrderCard bracket pill, live-wing badge, broker cap warnings, Console bracket() token

## Done when

- The Automation page's kind dropdown no longer offers "bracket" — confirmed via a real UI check,
  not just source grep.
- `OrderCard.svelte` shows no Bracket-related pill state.
- `liveWingBadge.js`/`liveWingTracker.svelte.js`/`brokerCapWarnings.js` are deleted (or confirmed
  to have zero remaining references if something still imports them unexpectedly — report rather
  than silently leaving a dangling import).
- `templateAttachToast.js`'s Bracket-specific half is gone; `holdRecord.js` is fully intact and
  unchanged.
- `/admin/settings` shows no Bracket/`TEMPLATE_EXIT`-specific toggle (only `EXPIRY_CLOSE`'s
  toggle, if that's what's actually there — reported, not assumed).
- MCP admin page's `template_slug` mint-form field is gone.
- Console `bracket(...)` token is rejected by the compiler.
- `positionStateBadge.js` priority is `auto-paired > orphan`, GTT branch gone.
- `automation/guide` no longer describes Bracket.
- svelte-check 0 errors, Playwright + Vitest green.
- `git diff --name-only` touches only frontend files — zero backend, zero Track-1 files.
