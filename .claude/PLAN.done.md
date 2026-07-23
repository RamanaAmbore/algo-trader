# Plan: Fix timestamp toggle in-place + add gap below page-header on mobile

## Context
Two mobile UX issues found in AlgoTimestamp after the previous spill fix:
1. On mobile tap, the refresh timestamp appears to the *right* of the hidden current timestamp instead of replacing it in the same position. Root cause: `ats-now` and `ats-refresh` are sibling flex items; `opacity:0` hides content but preserves layout slot, so both spans always occupy side-by-side positions.
2. No breathing room between the page-header bottom edge and the first card — `padding-top` is exactly `navbar + header` height with zero buffer.

## Task
1. Stack `ats-now` and `ats-refresh` in the same layout slot so toggle is truly in-place.
2. Add `0.3rem` gap below the page-header on mobile.

## Agents
- backend: skip
- frontend: Two edits:
  **Edit 1 — `frontend/src/lib/AlgoTimestamp.svelte`**
  - Wrap the `ats-now` span and the `{#if _refreshTs}` block in a new `<span class="ats-slot">` wrapper.
  - In the mobile `@media (max-width: 640px)` block add:
    ```css
    .ats-slot { display: grid; }
    .ats-now, .ats-refresh { grid-area: 1 / 1; }
    ```
  - Both spans now share the same grid cell — the opacity-0 one takes up the same space as the visible one, so the toggle is purely a crossfade in place. No change to the JS logic.

  **Edit 2 — `frontend/src/routes/(algo)/+layout.svelte`** (around lines 2135–2139)
  - Change `.algo-content { padding-top: calc(3rem + 1.4rem); }` → `calc(3rem + 1.4rem + 0.3rem)`
  - Change the ps-strip variant `calc(3rem + 1.5rem + 1.4rem)` → `calc(3rem + 1.5rem + 1.4rem + 0.3rem)`
  - Update the comment to reflect the added 0.3rem buffer.

- broker: skip
- doc: skip
- backend-test: skip
- playwright: Update the existing overflow regression test in `frontend/e2e/algo-timestamp.spec.js`:
  - After triggering a refresh (so `_refreshTs` is set), tap the `.ats-group` button on mobile-portrait.
  - Assert the bounding box X of `.ats-refresh` equals (within 4px) the bounding box X of `.ats-now` — confirming both render in the same horizontal position.

## Tests
- pytest: no
- svelte-check: yes
- playwright: yes (mobile-portrait only)

## Commit message
fix(ui): stack ats-group toggle in-place via CSS grid; add 0.3rem gap below mobile page-header

## Done when
- Tap on mobile: refresh timestamp fades in exactly where the current timestamp was (same X position, same size)
- First card on any page has visible breathing room below the page-header on mobile
- svelte-check 0 errors
