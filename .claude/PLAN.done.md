# Plan: LogPanel + Modal Consistency Polish (Cycle 4)

## Context
Six remaining inconsistencies after the CardHeader unification work:
BellIcon was removed from LogPanel in a prior cycle; the fullscreen button SVG doesn't
match the canonical FullscreenButton icon; the collapse binding is broken (display:contents
overrides the `hidden` attribute); the modal context shows × close instead of DefaultSize icon;
Snapshot label may carry extra decoration; OrderTicket modal size is inconsistent with other
canonical full-sheet modals.

## Task
1. Re-add BellIcon (always) before the Log label in LogPanel's CardHeader left snippet.
2. Sync LogPanel's lp-fs-btn SVG path to match FullscreenButton exactly.
3. Replace the × close button in modal context with the DefaultSize overlapping-rect icon.
4. Fix collapse button: `.lp-body-wrap { display: contents }` has higher specificity than
   the UA `[hidden] { display: none }`, so the `hidden` attribute is silently ignored.
   Add `.lp-body-wrap[hidden] { display: none; }` to LogPanel's scoped CSS.
5. Verify Snapshot label has no extra decoration beyond standard ch-title.
6. Align OrderTicket standalone-modal size with canonical full-sheet layout (the same
   `--modal-sheet-top`-anchored, full-width panel that ChartModal and ActivityLogModal use).

## Agents
- backend: skip
- frontend: Fix all six issues in `frontend/src/lib/LogPanel.svelte`,
  verify `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` Snapshot card,
  and update `frontend/src/lib/order/OrderTicket.svelte` modal size.
  Details below.
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Frontend agent brief

### 1 — BellIcon in LogPanel left snippet
File: `frontend/src/lib/LogPanel.svelte`

Add import at top of `<script>`:
```js
import BellIcon from '$lib/icons/BellIcon.svelte';
```

Inside the `{#if label}` block, add a `{#snippet left()}` to the CardHeader call
(CardHeader already accepts a `left` snippet via `{@render left?.()}` at line 109):
```svelte
{#snippet left()}
  <BellIcon width="12" height="12" class="lp-bell-icon" />
{/snippet}
```
Add CSS: `.lp-bell-icon { flex-shrink: 0; }` (BellIcon is already orange — no colour
override needed; it matches the Log button in PageHeaderActions).

### 2 — Sync fullscreen icon
File: `frontend/src/lib/LogPanel.svelte`, around line 1460

Change the `lp-fs-btn` SVG from:
```
d="M3 3h4M3 3v4M13 3h-4M13 3v4M3 13h4M3 13v-4M13 13h-4M13 13v-4" stroke-width="1.6"
```
to match FullscreenButton exactly:
```
d="M2 6V2h4M14 6V2h-4M2 10v4h4M14 10v4h-4" stroke-width="1.5"
```

### 3 — Modal right button: × → DefaultSize icon
File: `frontend/src/lib/LogPanel.svelte`, around line 1451–1454

In the `{#if context === 'modal'}` branch of `{#snippet right()}`, replace the × text
button with the DefaultSize overlapping-rectangle SVG (same markup as DefaultSizeButton.svelte):
```svelte
<button type="button" class="lp-default-btn"
        title="Restore to card"
        aria-label="Restore to card"
        onclick={() => onClose?.()}>
  <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
    <rect x="2.5" y="5.5" width="8" height="8" rx="0.8"
      fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>
    <path d="M5.5 5.5V2.5h8v8h-3"
      fill="none" stroke="currentColor" stroke-width="1.5"
      stroke-linecap="round" stroke-linejoin="round"/>
  </svg>
</button>
```
Rename CSS class from `.lp-close-btn` to `.lp-default-btn` (or add .lp-default-btn rules;
retain existing cyan-palette style block — `.lp-fs-btn, .lp-close-btn` → extend to include
`.lp-default-btn`). Remove the red border/hover that was on `.lp-close-btn`; use the
standard cyan palette (same as `.lp-fs-btn`).

### 4 — Fix collapse button (display:contents vs hidden)
File: `frontend/src/lib/LogPanel.svelte`, style section around line 2398

The `.lp-body-wrap { display: contents }` scoped rule overrides the UA `[hidden] { display: none }`.
Add one rule immediately after the existing `.lp-body-wrap` block:
```css
.lp-body-wrap[hidden] { display: none; }
```
This selector (0,2,0 specificity) beats both the UA `[hidden]` (0,1,0) and the base
`.lp-body-wrap { display: contents }` (0,1,0) so the hidden attribute takes effect.

### 5 — Snapshot label
File: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`

The Snapshot card already uses `<CardHeader title="Snapshot">` — the standard ch-title
amber pill is already applied. Check for any additional label decoration (local `.snap-*`
CSS or `label` prop duplication, extra badges). Remove any non-standard decoration found.
If none found, no change needed.

### 6 — OrderTicket modal size consistency
File: `frontend/src/lib/order/OrderTicket.svelte`

Currently: ModalShell wraps ot-modal with `align-items: center; justify-content: center` —
the ticket appears as a small 28rem centered popup. All other fullscreen modals (ChartModal,
ActivityLogModal) use the canonical sheet layout: top-anchored at `--modal-sheet-top`,
full viewport width, full remaining height.

First, identify where the "order entry full screen button" lives. Grep for FullscreenButton
usage in the orders page / order entry panel. If it uses `fs-card-on` (CSS card-fullscreen),
convert it to open a canonical modal instead.

For the OrderTicket standalone modal itself: change the ModalShell+ot-modal pattern to
use a canonical-modal-overlay approach, matching ActivityLogModal:
- Wrap in a div with class `canonical-modal-overlay` + `use:portal` (like ChartModal)
  OR use ModalShell with `passthrough={true}` and put `canonical-modal-panel` on the inner div
- Inner content: keep the ot-modal as a centred column within the sheet (add
  `align-items: center; padding-top: 2rem` on the canonical-modal-panel, or use a
  nested wrapper that constrains width to `min(28rem, calc(100vw - 2rem))`)
- This makes the overlay backdrop + panel position identical to ChartModal and
  ActivityLogModal while keeping the ticket form at its natural 28rem width

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(log-panel): restore BellIcon, sync fullscreen icon, fix collapse, DefaultSize in modal, OrderTicket sheet layout

## Done when
- BellIcon appears left of "LOG" label in every LogPanel context (page, card, modal)
- Fullscreen button on LogPanel renders identical icon to FullscreenButton on Holdings card
- Clicking collapse on dashboard LogPanel hides the body; clicking again reveals it
- Modal context right button shows overlapping-rect icon (not ×), tooltip "Restore to card"
- Snapshot label has standard ch-title amber pill, no other decoration
- Opening any fullscreen modal (LogPanel, OrderTicket, ChartModal) shows the same
  top-anchored full-sheet layout
- svelte-check passes with 0 errors
