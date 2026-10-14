# Plan: Frontend reusability sweep — shared CSS utilities, PnlAnalysis tab verification, modal-stacking regression coverage

## Task

Act on the frontend look-and-feel reusability audit (operator: "consider all of them for
implementation"), as refined by a 6-agent council review (architect/risk/perf/ux/devil/research).
Implement the council-approved, low-risk items now (shared CSS utility extraction for
icon-buttons and label decoration, a conditional chart-frame adoption, conditional color-token
matching); verify one item the council found was already shipped; explicitly do NOT bulk-migrate
one low-value item; and explicitly do NOT touch the two-modal-mechanism divergence in this pass —
instead add the regression test coverage risk's review found missing, with zero code change to
the overlay/modal files themselves. Any further action on the modal-mechanism item requires a
separate, explicit operator go-ahead after reviewing the new tests and a concrete proposal — it
is deliberately split out of "all of them" per risk's and devil's findings that its risk profile
was unknown at the time that instruction was given.

## Agents

- backend: skip
- frontend: Dispatch as TWO separate agents in this wave — they must not touch overlapping files.

  **Agent A — CSS utility extraction (items 1, 3, 4, 7):**
  1. Extract a shared `.algo-icon-btn` base class into `app.css` carrying the byte-identical
     rule currently duplicated in `RefreshButton.svelte`, `FullscreenButton.svelte`,
     `DefaultSizeButton.svelte`, `GridDownloadButton.svelte`, `GridSearchButton.svelte`
     (`display:inline-flex; align-items:center; justify-content:center; width:1.4rem;
     height:1.4rem; padding:0; margin:0; background:var(--algo-cyan-bg);
     border:1px solid var(--algo-cyan-border); border-radius:3px;`). Each component keeps its
     own icon markup/click-handler/active-state logic — only the shared box-model/border rule
     moves to the class, applied via `class="algo-icon-btn"` alongside each component's existing
     class. Zero visual change expected — this is pure deduplication.
  2. Extract a shared `.algo-label-decor` utility class (`border-left: 3px solid currentColor;
     padding-left: 0.35rem;`) and apply it to: MarketPulse's `.mp-bucket-label`, SymbolPanel's
     `.oes-modal-name`, `orders/+page.svelte`'s `.oc-entry-label`, and `CardHeader.svelte`'s
     `.ch-title`. Each site keeps its own color rule (`.mp-bucket-label-positions`'s sky tone,
     `.oes-modal-name`'s amber, etc.) — only the shared border-left+padding declaration is
     removed from each and replaced by the new class. Zero visual change expected.
  3. Before touching `MultiPriceChart.svelte`: diff `.algo-chart-frame`'s box-model properties
     (border/border-radius/background/box-sizing) against `.mpc-shell`'s current literal
     (`border: 1px solid rgba(251,191,36,0.18); border-radius: 4px;`). Only apply the shared
     class if every property matches exactly or the difference is provably cosmetic-neutral
     (e.g. `.algo-chart-frame` doesn't carry padding, which `.mpc-shell` may keep locally).
     If anything doesn't line up, do NOT force it — report the discrepancy and leave
     `MultiPriceChart.svelte` unchanged rather than risk a resize/redraw regression (per perf
     council's specific flag).
  4. In `NavTab.svelte`, for each of its 4 hardcoded `rgba(...)` border colors (lines ~330, 368,
     390, 429), check for an exact or visually-indistinguishable match against existing
     `--algo-rose-*`/`--algo-cyan-*` (or other) tokens in `app.css`. Swap only colors with a
     confirmed match (computed-style comparison, not just eyeballing the rgba string); leave any
     unmatched literal as-is rather than force a swap.

  **Agent B — PnlAnalysis tab verification (item 6):**
  NOT a migration — UX council confirmed via grep that `PnlAnalysis.svelte` already renders
  `<AlgoTabs compact>` (shipped in commit 77eb0e51; the old hand-rolled `.tab-strip` is already
  retired, per the comment at line ~1180). This agent's only job: load the Performance/PnlAnalysis
  view in a real browser (or via Playwright) and confirm the tab strip renders and behaves
  correctly with no visual regression. Make NO code changes unless a real, concrete defect is
  found — if one is found, report it rather than fixing it silently, since this task was scoped
  as verification-only.

- broker: skip
- doc: skip
- backend-test: skip
- playwright: **Item 5 — modal-overlay stacking regression coverage, test-authoring ONLY.**
  Zero code changes to any overlay/modal file in this task (`ModalShell.svelte`,
  `OrderTicket.svelte`, `OrderTimelineDrawer.svelte`, `OrderPairModal.svelte`, `SymbolPanel.svelte`).
  Write new Playwright tests covering the CURRENT (already-working, not-yet-covered) behavior of
  the overlay stack that nests `OrderTicket` inside `SymbolPanel`, and `OrderTimelineDrawer`/
  `OrderPairModal`'s own nested overlays:
  1. Pressing Escape closes only the topmost open overlay layer, never a parent/ancestor layer —
     regression test for the exact failure class `OrderPairModal.svelte`'s own code comment
     (lines 13-14) references as a prior real incident ("Escape exited a parent [ticket]").
  2. Clicking a nested overlay's own backdrop does not close an ancestor overlay.
  3. Submit still fires correctly through all stacked layers (order ticket submission is not
     broken by any overlay nesting).
  These tests assert CURRENT behavior — they must all pass against the unmodified codebase. Do
  not merge, reconcile, or refactor `ModalShell.svelte` vs. the hand-rolled local overlays
  (`ot-overlay`, `otd-backdrop`, `opm-overlay`) in this task. That decision is explicitly deferred
  — see "Done when."

## Tests

- pytest: no
- svelte-check: yes
- playwright: yes

## Commit message

refactor(frontend): extract shared CSS utilities (algo-icon-btn, algo-label-decor), verify PnlAnalysis tabs, add modal-stacking regression coverage

## Done when

- `.algo-icon-btn` exists in `app.css`; all 5 CardControls sibling buttons use it; zero visual
  change (confirmed via existing Playwright coverage of these buttons still passing).
- `.algo-label-decor` exists; all 4 label sites (MarketPulse, SymbolPanel, orders page,
  CardHeader) reference it instead of duplicating the border-left+padding rule; zero visual
  change.
- `MultiPriceChart.svelte` either adopts `.algo-chart-frame` (box-model confirmed matching, zero
  visual change) or is left unchanged with the discrepancy reported — either outcome is
  acceptable, a forced mismatched swap is not.
- `NavTab.svelte`'s 4 hardcoded colors are swapped only where a token match was confirmed; any
  unmatched color is left as a literal.
- PnlAnalysis's AlgoTabs rendering confirmed regression-free (verification only, no code change
  unless a real defect was found and explicitly reported).
- The amber-color-token bypass (406 raw literals) is explicitly NOT bulk-migrated in this pass —
  adopted only as a forward convention for files touched by future work.
- New Playwright coverage exists and passes for Escape/backdrop-click/Submit correctness across
  the OrderTicket/OrderTimelineDrawer/OrderPairModal/SymbolPanel overlay stack, against CURRENT,
  unmodified overlay behavior.
- The two modal-overlay mechanisms (`ModalShell.svelte` vs. hand-rolled local overlays) are NOT
  merged or reconciled in this pass — explicitly deferred pending a separate, dedicated operator
  decision after reviewing the new regression tests and a concrete before/after proposal.
- svelte-check 0 errors, all new/updated Playwright specs green.
