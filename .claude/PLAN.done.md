# Plan: Round 13 — activity viewport gap + 3 ModalShell migrations

## Context

Two cleanup tracks:
1. `activity/+page.svelte` adds `margin: 0 0.5rem 0.5rem` on its outer card, but `.algo-content` already provides `0.5rem` horizontal padding — the side margins are redundant, giving 1rem from the edge instead of the canonical 0.5rem. The border also uses the old weak colour.
2. Three inline backdrop+modal pairs remain that should use `ModalShell`. ModalShell is confirmed context-free (only imports `portal` util) so it's safe in both `(algo)` and `(public)` routes.

## Task

### Part A — Activity page viewport gap fix (1 file, 2 lines)

**File:** `frontend/src/routes/(algo)/activity/+page.svelte`

- Line ~93: `margin: 0 0.5rem 0.5rem 0.5rem` → `margin-bottom: 0.5rem`
  (`.algo-content` already holds the card 0.5rem from both sides)
- Line ~95: `border: 1px solid rgba(255, 255, 255, 0.06)` → `border: 1px solid rgba(126,151,184,0.10)`

---

### Part B — 3 ModalShell migrations

For each: import ModalShell, wrap the panel content in `<ModalShell>`, remove the hand-rolled overlay div and its inline Esc/backdrop-click handlers. Keep all inner panel CSS and content unchanged.

**B1 — IP (Investor Portal) modal — `routes/(algo)/admin/+page.svelte`**

- Open condition: `{#if portalUser}` (line ~1095) — `portalUser` is non-null when open
- Close handler: `closePortal()` function (sets `portalUser = null`)
- Current structure: `.ip-modal-overlay` (backdrop, Esc, backdrop-click) wraps `.ip-modal` (panel with tabs/form)
- Replacement:
  ```svelte
  <ModalShell
    open={!!portalUser}
    onClose={closePortal}
    ariaLabel="Investor Portal"
    zIndex={200}
  >
    <div class="ip-modal" onclick={(e) => e.stopPropagation()}>
      <!-- existing ip-modal content unchanged -->
    </div>
  </ModalShell>
  ```
- Remove: `.ip-modal-overlay` div, its `onclick={closePortal}`, its `onkeydown` Esc handler, `role="dialog"` and `aria-modal="true"` (ModalShell sets these on the overlay)
- Keep: all `.ip-modal-*` inner CSS and content — do NOT restructure the tabs, form, or token table
- Add import: `import ModalShell from '$lib/ModalShell.svelte'`

**B2 — Demo submit modal — `lib/order/OrderTicket.svelte`**

- Open condition: `{#if _demoSubmitOpen}` (line ~2391)
- Close: inline `_demoSubmitOpen = false`
- Current structure: `.ot-demo-overlay` (backdrop + Esc) wraps `.ot-demo-modal` (panel)
- Replacement:
  ```svelte
  <ModalShell
    open={_demoSubmitOpen}
    onClose={() => (_demoSubmitOpen = false)}
    ariaLabel="Demo mode"
    zIndex={300}
  >
    <div class="ot-demo-modal">
      <!-- existing content unchanged -->
    </div>
  </ModalShell>
  ```
- Remove: `.ot-demo-overlay` div, its `onclick`, its `onkeydown` Esc handler, `role="dialog"` and `aria-modal="true"` from `.ot-demo-modal` (ModalShell adds these), `stopPropagation` call on panel div
- Keep: all `.ot-demo-modal`, `.ot-demo-close`, `.ot-demo-title`, `.ot-demo-body`, `.ot-demo-cta` CSS and content
- Add import: `import ModalShell from '$lib/ModalShell.svelte'`

**B3 — Diagram zoom lightbox — `routes/(public)/faq/+page.svelte`**

- Open condition: `{#if _zoomedDiagram}` (line ~248) — `_zoomedDiagram` holds the SVG HTML string
- Close: `_closeZoom()` function (sets `_zoomedDiagram = null`, restores `document.body.style.overflow`)
- Current structure: `.faq-zoom-overlay` (backdrop + Esc on window) wraps `.faq-zoom-panel` → `.faq-zoom-wrap` → `.faq-zoom-svg`
- Note: body overflow is SET somewhere when zoom opens (probably when assigning `_zoomedDiagram`). Verify this in the file and keep that logic in place. ModalShell does NOT manage body overflow — the open path must still set it.
- Replacement:
  ```svelte
  <ModalShell
    open={!!_zoomedDiagram}
    onClose={_closeZoom}
    ariaLabel="Diagram zoom"
    zIndex={1000}
  >
    <div class="faq-zoom-panel">
      <div class="faq-zoom-wrap">
        <button class="faq-zoom-x" onclick={(e) => { e.stopPropagation(); _closeZoom(); }}>×</button>
        <div class="faq-zoom-svg">{@html _zoomedDiagram}</div>
      </div>
    </div>
  </ModalShell>
  ```
- Remove: `.faq-zoom-overlay` div, its `onclick={_closeZoom}`, the `svelte:window onkeydown` Esc handler for `_zoomedDiagram` (ModalShell handles Esc), `role="dialog"` and `aria-modal="true"` (ModalShell sets these)
- Keep: all `.faq-zoom-*` CSS and the body overflow restore in `_closeZoom()`
- Add import: `import ModalShell from '$lib/ModalShell.svelte'`

---

## Agents

- frontend: All parts A + B1 + B2 + B3 in one pass. Read each file before editing.
- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests

- pytest: no
- svelte-check: yes
- playwright: no

## Commit message

refactor(ui): activity viewport gap + 3 ModalShell migrations (IP, demo, faq zoom)

## Done when

- `activity/+page.svelte` outer margin is `margin-bottom: 0.5rem` only; border uses canonical colour
- IP portal modal uses ModalShell; no inline backdrop/Esc handlers remain
- OrderTicket demo modal uses ModalShell; no inline backdrop/Esc handlers remain
- FAQ zoom lightbox uses ModalShell; body overflow handling preserved
- svelte-check 0 errors
