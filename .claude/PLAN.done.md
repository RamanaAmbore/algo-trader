---
# Plan: Frontend Component Reuse — Modal Shell Migration + Admin Header Consistency

## Context
Audit of frontend components (cards, panels, modals, grids) revealed three modals that
bypass the existing `ModalShell.svelte` component and re-implement overlay/portal/ESC logic
inline, plus four admin pages using inconsistent section-header class names. The goal is
to eliminate duplicate modal chrome and standardize admin headers without over-engineering.

## Task
Migrate three modals (`ConfirmModal`, `AddToPulseModal`, MarketPulse inline option picker)
to use the existing `ModalShell.svelte` component for overlay/portal/ESC/click-outside
behavior, removing their duplicated overlay CSS and event handlers. Separately, standardize
admin section header markup across four admin pages to use a consistent class name.

## Audit Findings

### P1 — Modal chrome duplicated across 3 components (ModalShell exists and is unused)

**ModalShell.svelte** (`frontend/src/lib/ModalShell.svelte`) — props: `open`, `onClose`,
`ariaLabel`, `usePortal`, `zIndex` (default 200), `clickOutside` (default true), `dim`
(default true), `passthrough`, `children` snippet.
- Renders: fixed-inset backdrop rgba(8,12,20,0.72) + blur(2px), portal to body, ESC key
  handling, click-outside detection.

**ConfirmModal.svelte** (line 114) — builds `.cm-overlay` + `.cm-modal` inline. Has its
own fixed-inset overlay, inline ESC handler, `onclick={_cancel}` on overlay, `z-index:400`.

**AddToPulseModal.svelte** (line 53) — builds `.search-overlay` + `.search-modal` +
`.search-header` (title + × close button) inline. Same pattern.

**MarketPulse.svelte** (line 4206, conditional on `optionPickerUnderlying`) — identical
`.search-overlay` + `.search-modal` + `.search-header` pattern as AddToPulseModal.

### P2 — Admin section headers use three different class names

- `tokens/+page.svelte` line 262: `<h3 class="algo-card-title mb-0">`
- `settings/+page.svelte` line 320: `<h2 class="algo-card-title mb-1 pb-1 border-b ...>`
- `brokers/+page.svelte` line 769: `<h2 class="brokers-h" style="border-bottom:...">`
  (inline style instead of class)
- `admin/+page.svelte` lines 603, 789: `<h3 class="section-heading mb-2">`

These are form section sub-headers WITHIN a card (not card-level headers), so CardHeader
with CardControls is overkill. Fix: standardize all to `<h3 class="section-heading">` and
define the class once in the algo layout CSS or per-page, eliminating inline styles and
inconsistent class names.

## Implementation Plan

### Agent: frontend

**M1 — ConfirmModal → ModalShell**
File: `frontend/src/lib/ConfirmModal.svelte`
- Add `import ModalShell from '$lib/ModalShell.svelte'`
- Replace `{#if _open}<div class="cm-overlay" ...>` wrapper with
  `<ModalShell open={_open} onClose={_cancel} ariaLabel="Confirm action" zIndex={400}>`
- Keep `.cm-modal algo-modal` div and all inner content as ModalShell children
- Remove `onclick={_cancel}` from overlay (now handled by ModalShell `clickOutside=true`)
- Remove `onkeydown` ESC handler from overlay (now handled by ModalShell)
- Remove `.cm-overlay` CSS rule (the panel `.cm-modal` CSS stays — just the backdrop goes)
- Note: `zIndex={400}` required — ConfirmModal must float above OrderTicket (z=300)

**M2 — AddToPulseModal → ModalShell**
File: `frontend/src/lib/AddToPulseModal.svelte`
- Add `import ModalShell from '$lib/ModalShell.svelte'`
- Replace `<div class="search-overlay" ...>` outer wrapper with
  `<ModalShell open={!!open} {onClose} ariaLabel="Add to Pulse">`
  (pass `open` prop from parent — verify actual prop name in file)
- Keep `.search-modal`, `.search-header`, `.search-title`, `.search-close`, `.search-body`
  as ModalShell children
- Remove `onclick={onClose}` and `onkeydown ESC` from the removed overlay div
- Remove `.search-overlay` CSS rule; `.search-modal` + `.search-header` CSS stays
- Check z-index of `.search-modal` and set `zIndex` on ModalShell if > 200

**M3 — MarketPulse option picker → ModalShell**
File: `frontend/src/lib/MarketPulse.svelte`
- Add `import ModalShell from '$lib/ModalShell.svelte'` (check if already imported)
- Replace `{#if optionPickerUnderlying}<div class="search-overlay" ...>` wrapper with
  `<ModalShell open={!!optionPickerUnderlying} onClose={closeOptionPicker}
   ariaLabel="Pick option strike">`
- Keep `.search-modal`, `.search-header`, `.search-title`, `.search-close` as children
- Remove ESC handler and onclick from replaced overlay div
- Remove `.search-overlay` CSS from MarketPulse's local styles (if defined locally)
- Check if `.search-overlay` / `.search-modal` CSS is shared or scoped — if scoped in
  MarketPulse, remove the overlay rule only; keep panel rules

**M4 — Admin section header consistency**
Files: tokens/+page.svelte, settings/+page.svelte, brokers/+page.svelte, admin/+page.svelte
- Standardize all to `<h3 class="section-heading">` (remove mb-0, mb-1, mb-2 inline utils
  and replace with CSS in the class definition if needed)
- For brokers/+page.svelte line 769: remove inline `style="border-bottom:... padding-bottom...
  margin-bottom..."` — move those values into a `.section-heading` CSS rule in that page
- For settings/+page.svelte: keep the `({rows.length})` count span content, just normalize
  the element to h3 + class
- Note: these are FORM section headers, not card-level; CardHeader is NOT appropriate here

## Agents
- frontend: implement M1 + M2 + M3 + M4 as described above
- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: add/update spec to verify ConfirmModal still opens/closes correctly and ESC
  dismisses it; verify AddToPulseModal opens from watchlist button; verify MarketPulse
  option picker opens and ESC closes it

## Tests
- pytest: no
- svelte-check: yes — 0 errors required
- playwright: yes — modal open/ESC/close flows

## Commit message
refactor(ui): migrate ConfirmModal + AddToPulseModal + MarketPulse option picker to ModalShell; standardize admin section headers

## Done when
- svelte-check: 0 errors
- All three modals open, show correct content, dismiss on ESC and backdrop click
- Admin pages render section headers with consistent visual appearance
- No `.cm-overlay`, `.search-overlay` CSS left in migrated files (backdrop CSS removed)
