# Plan: Fix legs grid cell full-height coverage — bars/tints/separators

## Context
In the derivatives legs CSS Grid, `.cand-row` uses `padding: 0.2rem 0.3rem` (container-level) + `align-items: center`. This means each cell `<span>` is only as tall as its text content (~12px), centered inside a taller row. The direction bar `::after` (`top:0; bottom:0`) and `background-color` on `.cand-sym-acct` both reference the span's own height — so they only cover the text zone, leaving the top/bottom padding areas bare.

In the ag-Grid positions/holdings surface: row containers have no vertical padding; each `.ag-cell` fills the full `24px` row height and gets horizontal-only cell padding. `::after` covers the full 24px. Our CSS Grid legs need the same model: **move vertical padding from row to cells**.

## Task
Three CSS changes in `CandidateLegRow.svelte`. No other files needed.

## Agents
- frontend: Make the three changes below
- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Frontend agent task

**File: `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte`**

### Change 1 — Remove vertical padding from row container; switch to stretch

In the `.cand-row` rule (around line 419), make two edits:
- `padding: 0.2rem 0.3rem` → `padding: 0 0.3rem`
- `align-items: center` → `align-items: stretch`

### Change 2 — Restore vertical spacing at cell level + flex vertical centering

After the `.cand-row` rule, add a new rule targeting all direct `<span>` children:
```css
.cand-row > span {
  padding-top: 0.2rem;
  padding-bottom: 0.2rem;
  display: flex;
  align-items: center;
}
```
This restores the same 0.2rem top/bottom breathing room, now at cell level so backgrounds and `::after` extend through it.

### Change 3 — Right-align text in numeric cells under flex

The existing `.cand-row > .num` rule has `justify-self: end` (grid alignment) which no longer makes sense once cells stretch to fill the column width. Replace it with flex text alignment:
- Remove: `justify-self: end;`
- Add: `justify-content: flex-end;`

The `text-align: right`, `min-width: 0`, `overflow: hidden`, `text-overflow: ellipsis`, `white-space: nowrap` lines stay unchanged.

---

After edits, run `cd /Users/ramanambore/projects/ramboq/frontend && npx svelte-check --output machine 2>&1` and fix any errors. Report result.

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(ui): legs cells stretch to full row height — bars/tints/separators cover full cell

## Done when
- Direction bars (green/red `::after` on `.cand-sym-acct`) cover the full row height, not just the text zone
- Account tint (`background-color` on `.cand-sym-acct`) fills the full row height
- Grey separator box-shadows (`.cand-sym-acct` and `.cand-chg-sep`) span the full row height
- Row vertical spacing is unchanged (0.2rem breathing room, now via cell padding)
- Numeric text stays right-aligned
- svelte-check 0 errors
