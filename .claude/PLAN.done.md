# Plan: Log panel — single-row header, rename, scrollable tabs, vertical-only expand

## Context

The recent H1 ActivityLogSurface refactor introduced a CardHeader row ABOVE LogPanel's
own tab row in card/card-wide contexts (dashboard, orders). This created a two-row
header on those surfaces. `/automation/activity` also had its own two-row variant.
The operator wants one row everywhere — all chrome (label + tabs + dropdowns + buttons)
in a single flex row inside LogPanel itself, CardHeader gone from this surface entirely.

Additionally:
- "Activity" / "ACTIVITY" label text → "Log" / "LOG" across all surfaces
- Surfaces missing a label (SymbolPanel, SimulatorPanel, ReplayPanel, /console,
  /activity, /automation) need a "Log" label added
- Tab strip + dropdowns should be horizontally scrollable (overflow-x: auto, no scrollbar)
  so narrow panels don't clip tabs
- Expand button: vertical only — grow height inside the document flow (push content
  down), width stays as the column width. No viewport-wide fixed-position overlay.
  Collapse stays as-is (hides body, single header row remains visible).

Component names unchanged (ActivityLogSurface, ActivityLogModal, LogPanel).
CSS class names unchanged. Only label display strings and layout change.

---

## Changes

### 1. `frontend/src/lib/ActivityLogSurface.svelte`

**Remove CardHeader entirely.** The block that conditionally renders `<CardHeader>`
above `<LogPanel>` (introduced in H1) must be deleted. Pass `label` directly to
LogPanel. Remove `hideControls`, `hideSearch`, `hideDownload` props from the LogPanel
call — LogPanel owns all buttons again.

Also: change any `label="ACTIVITY"` / `label="Activity"` props passed through
ActivityLogSurface to `label="Log"`.

### 2. `frontend/src/lib/LogPanel.svelte`

**A. Remove `hideControls`, `hideSearch`, `hideDownload` props** — they were only
needed when CardHeader stole those buttons. Restore unconditional rendering of Search,
Download, Collapse, Expand buttons in the tab row for card/card-wide contexts.

**B. Rename label text** — any place the label prop value is hardcoded inside LogPanel
(e.g. modal bell label "Activity"), change to "Log".

**C. Scrollable tab strip** — on `.lp-tab-strip-wrap`:
```css
.lp-tab-strip-wrap {
  overflow-x: auto;
  scrollbar-width: none;          /* Firefox */
  -webkit-overflow-scrolling: touch;
}
.lp-tab-strip-wrap::-webkit-scrollbar { display: none; }
```

**D. Vertical-only expand** — Replace the viewport-fullscreen expand with a height-only
tall mode. In the expand button handler, instead of setting `isFullscreen = true`
(which triggers `.fs-card-on` fixed inset), set a new boolean `_isTall`. Apply:
```css
.lp-tall {
  height: min(85vh, 1100px);
  overflow-y: auto;
}
```
on the LogPanel's root section element when `_isTall`. Width unchanged.
DefaultSizeButton resets `_isTall = false`. Collapse (`isCollapsed`) still hides the
body as before.

Remove the `isFullscreen` state and `.fs-card-on` usage from LogPanel. The fullscreen
backdrop portal and body-scroll-lock go with it.

> Note: `isFullscreen` / `.fs-card-on` may be used by OTHER card types (CardHeader-based
> cards on dashboard/orders/derivatives). Only remove it from LogPanel's own expand path.
> Audit grep: `grep -rn "isFullscreen\|fs-card-on" frontend/src/lib/LogPanel.svelte` —
> remove those; leave CardHeader.svelte's fullscreen untouched.

### 3. All mount sites — add "Log" label where missing

Files to update (pass `label="Log"` to ActivityLogSurface or LogPanel):
- `frontend/src/routes/(algo)/activity/+page.svelte`
- `frontend/src/routes/(algo)/automation/+page.svelte`
- `frontend/src/routes/(algo)/automation/activity/+page.svelte` (remove CardHeader render here too; let ActivityLogSurface handle it)
- `frontend/src/routes/(algo)/console/+page.svelte`
- `frontend/src/lib/SymbolPanel.svelte` (bottom panel LogPanel mount)
- `frontend/src/lib/execution/SimulatorPanel.svelte`
- `frontend/src/lib/execution/ReplayPanel.svelte`

Existing mounts already using `label="ACTIVITY"` or `label="Activity"`:
- `frontend/src/routes/(algo)/dashboard/+page.svelte` → change to `label="Log"`
- `frontend/src/routes/(algo)/orders/+page.svelte` → change to `label="Log"`
- `frontend/src/routes/(algo)/+layout.svelte` (ActivityLogModal) → label "Activity" → "Log"

### 4. `frontend/src/lib/ActivityLogModal.svelte` (if it has its own label text)

Change any hardcoded "Activity" display text to "Log".

---

### 5. Button group consistency — LogPanel, ChartModal, SymbolPanel

**Canonical standard** (CardControls): 1.4rem × 1.4rem buttons, 13px icons, 1.6px stroke,
`var(--algo-cyan-bg)` (0.08 opacity) resting bg, `gap: 0.3rem`, border-radius 3px.

Gaps to fix:
- `frontend/src/lib/ChartModal.svelte` `.cm-actions`: gap 0.45rem → **0.3rem**
- `frontend/src/lib/SymbolPanel.svelte` `.oes-right-group`: gap 0.45rem → **0.3rem**

LogPanel icon size:
- `frontend/src/lib/LogPanel.svelte` `.lp-card-btn` SVGs: width/height 11 → **13**, keeps
  viewBox="0 0 16 16" — same as CardControls

SymbolPanel chart button:
- `.oes-chart-btn`: background `var(--c-info-14)` (rgba 0.14) → **`var(--algo-cyan-bg)`** (0.08)
- `.oes-chart-btn` SVG: stroke-width 2 → **1.6**, width/height 14 → **13**

Button sequence in modal headers is already correct:
- ChartModal: [refresh][×] — action before close ✓
- SymbolPanel: [chart][clear][×] — actions before close ✓
- LogPanel card: [search][download][collapse][expand] — all cyan ✓

Add shared CSS token in `frontend/src/app.css`:
```css
.canonical-card-btn-group {
  display: flex;
  align-items: center;
  gap: 0.3rem;
  flex-shrink: 0;
}
```
Apply `.canonical-card-btn-group` to `.cm-actions`, `.oes-right-group`, `.lp-card-btns`
in place of their local gap/flex declarations (or just align them to 0.3rem — either works).

---

## Agents

- frontend: Implement all changes above across LogPanel, ActivityLogSurface, all mount
  sites, and ActivityLogModal. Read each file before editing. Verify single-row layout
  by checking that no CardHeader is rendered above LogPanel anywhere. Verify label="Log"
  or label="Log" passed at every mount site.

- playwright: skip (visual layout; svelte-check is the gate)

- backend: skip

- backend-test: skip

- doc: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
refactor(log-panel): single-row header, Log rename, scrollable tabs, vertical-only expand — remove CardHeader from ActivityLogSurface

## Done when
- Zero two-row log panel headers anywhere (CardHeader never renders above LogPanel)
- All log panel surfaces show "Log" label chip in the single tab row
- Tab strip + account dropdown horizontally scrollable with no visible scrollbar
- Expand button grows panel height in-place (no viewport-wide overlay); collapse hides body
- svelte-check: 0 errors
