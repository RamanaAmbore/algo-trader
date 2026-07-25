# Plan: Full-screen card modal — X sync, modal chrome, page-header button

## Context

The current full-screen card implementation uses `position: fixed; inset: 2rem` with a
`backdrop-filter: blur(2px)` — the card expands in-place and blurs the content behind
it. Operators find this confusing because:
(a) the card looks like it just resized in the layout, not like a modal opened;
(b) the close action is a Windows "restore-down" icon (two overlapping rectangles) that
    is not recognisably a close / X button;
(c) there is no page-header fullscreen button — only per-card card-header buttons exist;
(d) when both entry paths should exist (card header and page header), the close button
    (rightmost card button) must be visually in sync across both.

## Intended outcome

1. Full-screen card looks unmistakably like a **modal**: dark overlay + soft modal-border
   chrome + a prominent ✕ button pinned to the top-right corner of the modal.
2. The ✕ in the top-right corner and the rightmost button in the card header are the
   **same visual X** (both call the same dismiss action).
3. A **page-header fullscreen button** appears on each page's action bar; clicking it
   expands the page's "primary card" (or the last card the operator interacted with) into
   the same modal at the same inset.
4. No behaviour changes for backdrop click or Escape key — these already work.

---

## Key files

| File | Role |
|---|---|
| `frontend/src/lib/FullscreenButton.svelte` | Entry button + backdrop portal + keyboard/click handlers |
| `frontend/src/lib/DefaultSizeButton.svelte` | Exit button (currently Windows restore-down icon) |
| `frontend/src/lib/CardControls.svelte` | Button cluster that toggles between the two |
| `frontend/src/app.css` lines 1774-1857 | `.fs-card-on` + `.fs-backdrop` global styles |
| `frontend/src/lib/stores.js` | Global state — need to add `activeCardStore` |
| `frontend/src/routes/(algo)/+layout.svelte` | Page-level keyboard shortcut `F` |

---

## Changes

### 1. `frontend/src/app.css` — stronger modal chrome

Replace the current `.fs-backdrop` and `.fs-card-on` rules:

```css
.fs-backdrop {
  /* was: backdrop-filter: blur(2px) only */
  background: rgba(0, 0, 0, 0.55);       /* ← dark overlay, clearly a modal */
  backdrop-filter: blur(3px);
  position: fixed;
  inset: 0;
  z-index: 9998;
}

.fs-card-on {
  position: fixed !important;
  inset: 1.5rem !important;               /* slightly tighter — feels more modal */
  z-index: 9999 !important;
  max-width: none !important;
  max-height: none !important;
  overflow: auto !important;
  border-radius: 0.75rem !important;      /* ← rounder corners = modal feel */
  box-shadow: 0 16px 64px rgba(0,0,0,0.80),
              0 0 0 1.5px rgba(251,191,36,0.55) !important;
  animation: fs-pop-in 0.15s ease-out;   /* ← subtle scale-in */
}
@keyframes fs-pop-in {
  from { transform: scale(0.97); opacity: 0.6; }
  to   { transform: scale(1);    opacity: 1; }
}
```

### 2. `frontend/src/lib/FullscreenButton.svelte` — add pinned ✕ overlay

Inside the backdrop portal (already created in `onMount`), add a second element: a
floating ✕ button pinned to the top-right corner of the fullscreen card.

```html
<!-- inside the backdrop portal div, alongside the existing close-on-click handler -->
<button
  class="fs-modal-close-btn"
  aria-label="Close fullscreen"
  on:click={() => { isFullscreen = false; }}
>✕</button>
```

CSS (add to `app.css`):
```css
.fs-modal-close-btn {
  position: fixed;
  top: 1rem;
  right: 1rem;
  z-index: 10001;           /* above fs-card-on (9999) */
  width: 2rem;
  height: 2rem;
  border-radius: 9999px;
  background: rgba(30,41,59,0.90);
  border: 1px solid rgba(255,255,255,0.15);
  color: #e2e8f0;
  font-size: 1rem;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background 0.15s;
}
.fs-modal-close-btn:hover { background: rgba(239,68,68,0.75); }
```

The ✕ button is rendered by FullscreenButton (which owns the portal) and is
destroyed when `isFullscreen = false` because the portal is cleaned up in its
`onDestroy` / reactive cleanup.

### 3. `frontend/src/lib/DefaultSizeButton.svelte` — change icon to ✕

Replace the Windows restore-down SVG path with a simple ✕:

```svelte
<!-- Replace existing <svg> with: -->
<span class="fs-x-icon" aria-hidden="true">✕</span>
```

Or use a proper X SVG (cross, not restore-down). Style to match the pinned ✕ button:
```css
.fs-x-icon { font-size: 1.1rem; line-height: 1; }
```

The result: both the pinned top-right ✕ and the card-header rightmost button show the
same `✕` symbol, making them visually in sync.

### 4. `frontend/src/lib/stores.js` — add `activeCardStore`

```js
// Tracks which card (by label) has been most recently focused/opened fullscreen.
// Page-header fullscreen button reads this to expand the right card.
export const activeCardStore = writable({ label: null, open: null });
// `open` is a callback: () => void — set by each card when it mounts/becomes focused.
```

### 5. `frontend/src/lib/CardControls.svelte` (or `CardHeader.svelte`) — register with store

When a card becomes fullscreen (or is interacted with), call:
```js
import { activeCardStore } from '$lib/stores.js';

function onCardFocus() {
  activeCardStore.set({ label, open: () => { isFullscreen = true; } });
}
```

Trigger `onCardFocus` on: card header click, any card interaction (mouseenter on
card header is simplest and low-noise).

### 6. New component: `frontend/src/lib/PageFullscreenButton.svelte`

A small button that reads `activeCardStore` and opens the active card fullscreen:

```svelte
<script>
  import { activeCardStore } from '$lib/stores.js';
  function expand() {
    if ($activeCardStore.open) $activeCardStore.open();
  }
</script>
<button class="page-fs-btn" title="Expand active card ({$activeCardStore.label ?? 'none'})"
  on:click={expand} disabled={!$activeCardStore.open}>
  <!-- same four-arrows-outward SVG as FullscreenButton -->
</button>
```

### 7. Wire `<PageFullscreenButton>` into page action areas

Add `<PageFullscreenButton>` to the shared `PageHeaderActions` slot or equivalent
in affected pages. Start with the admin/dashboard page (`+layout.svelte` or
`dashboard/+page.svelte`) and extend to derivatives, perf, and other algo pages.

The button sits adjacent to the existing Orders/Charts/Activity buttons.

---

## Agents

- frontend: Implement all 7 changes above. Read each file before editing. Run
  `svelte-check` after.

- playwright: Write/update Playwright tests covering the full-screen modal behaviour
  (see Tests section below).

- doc: skip

- backend: skip

- backend-test: skip

## Tests

### Standing rule (applies to ALL future changes)
Every fix or feature must ship with a new test case or an updated existing test that
proves the behaviour and prevents regression. No exceptions — "small" changes still
need a test.

### Playwright specs for this change (`frontend/tests/`)

1. **`test_fullscreen_card_modal.spec.ts`** (new):
   - `test_card_header_button_opens_modal`: click FullscreenButton on any card →
     assert `.fs-card-on` exists in DOM, `.fs-backdrop` has `opacity > 0`,
     `.fs-modal-close-btn` is visible in the top-right corner.
   - `test_pinned_x_closes_modal`: with card in fullscreen → click `.fs-modal-close-btn`
     → assert `.fs-card-on` is no longer in DOM.
   - `test_card_header_x_closes_modal`: with card in fullscreen → click the rightmost
     button in the card header → assert fullscreen dismissed.
   - `test_both_x_buttons_show_same_symbol`: assert `.fs-modal-close-btn` innerText and
     the rightmost card header button innerText both equal `✕`.
   - `test_page_header_fullscreen_button_expands_active_card`: hover card header to
     register it as active → click `PageFullscreenButton` in page header →
     assert that card becomes `.fs-card-on`.
   - `test_escape_key_closes_modal`: open fullscreen → press Escape →
     assert fullscreen dismissed (regression guard for existing keyboard shortcut).
   - `test_backdrop_click_closes_modal`: open fullscreen → click outside card (on
     backdrop) → assert fullscreen dismissed.
   - `test_backdrop_is_dark_not_just_blur`: with fullscreen open → measure computed
     `background-color` of `.fs-backdrop` → assert alpha channel > 0.4 (dark overlay,
     not just blur).

2. **Update existing `test_card_controls.spec.ts`** (if it exists) or add inline to the
   new file: assert that `DefaultSizeButton` (rightmost button in fullscreen state)
   renders ✕ text and NOT the old restore-down SVG path string.

- pytest: no
- svelte-check: yes
- playwright: yes

## Commit message
feat(ui): fullscreen card modal — dark overlay, X sync, page-header expand button

## Done when
- Dark backdrop clearly visible when any card is fullscreen (not just blur)
- Pinned ✕ button appears top-right of the fullscreen card
- Rightmost card button shows ✕ icon (not Windows restore-down)
- Both ✕ locations dismiss fullscreen
- PageFullscreenButton exists and expands the last-focused card
- PageFullscreenButton wired into at least the dashboard/admin layout
- All 8 new Playwright specs pass
- svelte-check 0 errors
