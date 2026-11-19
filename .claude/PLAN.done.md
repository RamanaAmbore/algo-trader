# Plan: Derivatives page — Legs dropdown width, Greeks grid decoration, Risk & EV four-column layout with header

## Task

Three coupled UI tweaks on `/admin/derivatives` (`frontend/src/routes/(algo)/admin/derivatives/+page.svelte`), CSS-only plus one small markup addition:

1. **`.cand-view-select`** (Legs view-picker dropdown) — reduce width ~30%: `flex: 0 1 11rem; min-width: 6.5rem;` → `flex: 0 1 7.7rem; min-width: 4.5rem;`. No markup change.

2. **Greeks (position) card decoration** — `.opt-kv-greeks .kv-k-greek` (5 header cells, Δ Γ Θ 𝒱 ρ) and `.opt-kv-greeks .kv-v` (5 value cells) currently shrink-wrap (`inline-flex`) with no header-row decoration, unlike `.cand-headrow`/`.byund-headrow` elsewhere in this file. Add, scoped under `.opt-kv-greeks` only (do NOT touch the shared base `.kv-k-greek`/`.kv-v` rules — Risk & EV reuses those same base classes):
   - `.opt-kv-greeks .kv-k-greek`: add `display:flex; width:100%; box-sizing:border-box; padding:0.15rem 0.35rem; background:var(--grid-header-bg); border-bottom:1px solid var(--algo-amber-border-soft);` — keep existing font-size/font-weight.
   - `.opt-kv-greeks .kv-v` (rule already exists ~line 7150): add `width:100%; box-sizing:border-box; padding:0.15rem 0.35rem; border-bottom:1px solid rgba(126,151,184,0.10);` — keep existing font-size/text-align:left/display:inline-flex/align-items:center (2026-11-03 vertical-centering fix must survive).
   - No `justify-content:center` — keep left-alignment so the existing "value aligns under its own header" left-edge test keeps passing.

3. **Risk & expected value card — responsive N columns (param, value, param, value, ...) + header row.**
   Current: `.opt-kv-risk { grid-template-columns: max-content minmax(max-content, 1fr); }`,
   `.kv-pair { display: contents; }`, 6 (or 5) metrics one-per-row, explicitly no header row.
   Operator clarified mid-task: the 4-column (2-pair) shape below is the **mobile** baseline; on
   **desktop**, where the card has more width, the "param name, value" column pair should **repeat
   again** if space allows (3 pairs instead of 2) — not stay hardcoded at 2 forever.
   - **Layout, responsive via `@media`** (matching this file's own existing viewport-breakpoint
     convention — no container queries are used anywhere else in this codebase, so none are
     introduced here):
     - Default (mobile and up, the floor): 4 tracks (2 pairs) —
       `grid-template-columns: max-content minmax(max-content, 1fr) max-content minmax(max-content, 1fr);`
     - `@media (min-width: 1180px)` (same desktop threshold this file already uses for the
       Payoff+Legs 2-up row, line ~7244 — reused for consistency, not a new magic number): widen to
       6 tracks (3 pairs) — `grid-template-columns: repeat(3, max-content minmax(max-content, 1fr));`
     - Everything else on `.opt-kv-risk` unchanged at both tiers (gaps, `overflow-x:auto`,
       `min-width:0`, `flex:1`, `align-content:center`). `.kv-pair { display: contents; }` and
       `.opt-kv-risk .kv-v { display:inline-flex; align-items:center; text-align:left; }` stay
       exactly as-is — **no markup change to the 6 `.kv-pair` divs**. Row-major auto-placement
       re-flows automatically at each tier: 4 tracks → (R:R, RoR) / (BE, POP) / (EV, EV/cost), 3
       rows; 6 tracks → (R:R, RoR, BE) / (POP, EV, EV/cost), 2 rows. Chosen over a flex-pair or
       `repeat(auto-fit, ...)` redesign specifically to preserve the `minmax(max-content,1fr)`
       overflow-safety invariant (a long value like 3 breakevens must force the grid to scroll,
       never silently ellipsis-clip — see the existing forced-overflow Playwright test) and to keep
       the header-cell count deterministic (see below) rather than depending on a runtime-computed
       fit count.
   - **Header row (new)**: add 6 `<span class="kv-risk-colhdr" role="columnheader">` cells — text
     "Metric", "Value", "Metric", "Value", "Metric", "Value" — immediately before the first
     `.kv-pair` div inside `<div class="opt-kv opt-kv-risk">`. Document-order auto-placement lands
     these in row 1, pushing the `.kv-pair`s to rows 2+ automatically — no explicit `grid-row`. The
     3rd pair's header cells (the 5th/6th spans) are hidden by default and revealed at the same
     1180px breakpoint, so the header's visible cell count always matches the grid's current track
     count — they can never disagree, since both are driven by the identical media query:
     ```css
     .opt-kv-risk .kv-risk-colhdr:nth-child(5),
     .opt-kv-risk .kv-risk-colhdr:nth-child(6) {
       display: none;
     }
     @media (min-width: 1180px) {
       .opt-kv-risk .kv-risk-colhdr:nth-child(5),
       .opt-kv-risk .kv-risk-colhdr:nth-child(6) {
         display: flex;
       }
     }
     ```
     Decorate all 6 with the shared `--grid-header-*` tokens (fuller header typography than Greeks,
     since these are plain text labels not Greek glyphs):
     ```css
     .opt-kv-risk .kv-risk-colhdr {
       display: flex;
       width: 100%;
       box-sizing: border-box;
       padding: 0.15rem 0.35rem;
       background: var(--grid-header-bg);
       border-bottom: 1px solid var(--algo-amber-border-soft);
       font-size: var(--grid-header-font-size);
       font-weight: var(--grid-header-font-weight);
       letter-spacing: var(--grid-header-letter-spacing);
       text-transform: uppercase;
       color: var(--text-muted);
     }
     ```

Update the comment blocks above `.opt-kv-greeks` and `.opt-kv-risk` to record this as a dated 2026-11-19 follow-up to the 2026-11-18 restructure already documented there (don't delete that history), including the mid-task reversal on the Risk header-row question (first "no header expected", then operator explicitly said "it can have header too").

## Agents

- **frontend**: Make all CSS + the one markup change above in `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`. Then update the Playwright specs that hard-pin the exact shapes being changed, in the same commit:
  - `frontend/e2e/derivatives_greeks_risk_restructure.spec.js`:
    - The static "Risk block: exactly 6 .kv-pair rows..., no header row" test must flip: still exactly 6 `.kv-pair`s, but now assert all 6 `.kv-risk-colhdr` header cells exist, in document order before the first `.kv-pair`, with the right text ("Metric"/"Value" ×3).
    - The static "Risk grid CSS: unconditional 2-column grid..." test's `grid-template-columns` regex → match the new default 4-track value, plus a new assertion that a `@media (min-width: 1180px)` block exists for `.opt-kv-risk` widening to 6 tracks (`repeat(3, max-content minmax(max-content, 1fr))`), and that the same breakpoint also toggles `.kv-risk-colhdr:nth-child(5)`/`:nth-child(6)` to `display: flex`. `pairCssMatch[1]).toMatch(/display:\s*contents/)` stays as-is.
    - The live "Risk: no header row — each pair on its own row" test → rewrite into two viewport cases: at the default/mobile viewport, 2 pairs per row (4 distinct left-edge columns, only 4 header cells visible); at a ≥1180px desktop viewport, 3 pairs per row (6 distinct left-edge columns, all 6 header cells visible). Both cases assert the header row renders above the first data row.
    - Leave every Greeks-side test in this file untouched.
  - `frontend/e2e/alignment_overflow_audit.spec.js`: the `.opt-kv-risk is a strict 2-column grid...` test's `grid-template-columns` regex + description → update to the default 4-track shape plus the new `@media (min-width: 1180px)` 6-track override; `display:\s*contents` check unchanged.
  - `frontend/e2e/derivatives_greeks_risk_vertical_centering.spec.js`: verify it still passes unchanged (its assertions target `.opt-kv-risk .kv-v`'s inline-flex/align-items and `.opt-kv-risk`'s own flex-grow/align-content — none of which this plan removes). Only touch it if a real failure surfaces.
  - `frontend/e2e/derivatives_smoke_candidatelegrow.spec.js`, `derivatives_greek_header_chip_infohint.spec.js`, `derivatives_tick_flash.spec.js`: markup/InfoHint/tf-cell presence only — confirm green, don't edit unless a real failure surfaces.
  - Run the full affected spec files locally and fix any real failures before handing off:
    `npx playwright test e2e/derivatives_greeks_risk_restructure.spec.js e2e/alignment_overflow_audit.spec.js e2e/derivatives_greeks_risk_vertical_centering.spec.js e2e/derivatives_smoke_candidatelegrow.spec.js e2e/derivatives_greek_header_chip_infohint.spec.js e2e/derivatives_tick_flash.spec.js`
- **backend**: skip.
- **broker**: skip.
- **doc**: Add one dated (2026-11-19) addendum under the existing 2026-11-18 "Derivatives Legs grid — St column removed... Greeks and Risk cards rebuilt as CSS grids" entry in `CLAUDE.md`: dropdown width change, Greeks header/value decoration, Risk & EV's 4-track layout + new header row (and why 4-track was chosen over a flex-pair redesign — preserves the overflow-safety invariant).
- **backend-test**: skip.
- **playwright**: covered by the frontend agent above (bundled — same file, same task).

## Tests

- pytest: no
- svelte-check: yes
- playwright: yes — the 6 spec files listed above

## Commit message

```
style(derivatives): shrink Legs dropdown, decorate Greeks grid, add 4-column header to Risk & EV
```

## Done when

- `.cand-view-select` is ~30% narrower.
- Greeks header row (Δ Γ Θ 𝒱 ρ) and value row show background band / border decoration matching `.cand-headrow`/`.byund-headrow`.
- Risk & expected value renders a "Metric | Value | Metric | Value" header row, same decoration, followed by 3 rows of 2 metrics each (6 metrics; 5 when EV/cost is absent, row 3 half-empty).
- All 6 listed Playwright spec files pass; no pre-existing test left red, silently skipped, or deleted.
- CLAUDE.md has a dated entry for this change.
