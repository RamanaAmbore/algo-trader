# Plan: Pulse/Derivatives UX — header scrollability, grid radius, connection popup, underlying colour

## Context
Five UX defects reported across Pulse, Derivatives, and the BrokerHealthBadge popup:

1. **Positions header pair button** pushes button group off-screen — `.ch-left` is `flex-shrink: 0` so it can't compress when content grows.
2. **CardHeader left-zone standard** — when header content overflows, it should scroll silently; button group (ch-right) must stay pinned on the right. Missing for all cards app-wide.
3. **ag-Grid column-header corner radius** — ag-theme-quartz adds border-radius to `.ag-root-wrapper` and `.ag-header`, creating a curved inner top edge inside each Pulse bucket card. Should be straight line end-to-end.
4. **BrokerHealthBadge popup** — account cells show a tinted background on active/amber/red rows. Operator: text-only colour-coding (amber/green/neutral), no backgrounds. Also the close button and `border-radius` don't follow canonical modal pattern.
5. **Derivatives byund-grid underlying column** — plain text, no colour. Should use same hash-palette colour coding as the account column in nav/capital/equity grids (text colour only, per unique underlying symbol).

## Agents
- frontend: implement all five fixes across four files (see below)
- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Files to modify

---

### 1. `frontend/src/lib/CardHeader.svelte` — Fix #2 (scrollable ch-left)

**Current `.ch-left`** (line ~152):
```css
.ch-left {
  display: flex;
  align-items: center;
  gap: var(--ch-gap, 0.4rem);
  flex-shrink: 0;
}
```

**New `.ch-left`**:
```css
.ch-left {
  display: flex;
  align-items: center;
  gap: var(--ch-gap, 0.4rem);
  flex: 0 1 auto;
  min-width: 0;
  overflow-x: auto;
  scrollbar-width: none;
}
.ch-left::-webkit-scrollbar { display: none; }
```

`flex: 0 1 auto` — doesn't grow beyond content width (doesn't eat tab space in `ch-middle`), but CAN shrink when total header space is tight. `overflow-x: auto` + `scrollbar-width: none` gives silent horizontal scroll on overflow. This also resolves Fix #1 (positions pair button).

---

### 2. `frontend/src/lib/MarketPulse.svelte` — Fix #3 (ag-Grid header corner radius)

**Existing rule** (around line 4799):
```css
:global(.bucket-grid .ag-root-wrapper),
:global(.summary-grid .ag-root-wrapper),
:global(.funds-grid .ag-root-wrapper) {
  border: none !important;
}
```

**Add `border-radius: 0 !important;`** to the same block, and add a new rule for the header viewport:
```css
:global(.bucket-grid .ag-root-wrapper),
:global(.summary-grid .ag-root-wrapper),
:global(.funds-grid .ag-root-wrapper) {
  border: none !important;
  border-radius: 0 !important;
}
:global(.bucket-grid .ag-header),
:global(.bucket-grid .ag-header-viewport),
:global(.summary-grid .ag-header),
:global(.summary-grid .ag-header-viewport),
:global(.funds-grid .ag-header),
:global(.funds-grid .ag-header-viewport) {
  border-radius: 0 !important;
}
```

---

### 3. `frontend/src/lib/BrokerHealthBadge.svelte` — Fix #4 (text-only account colours + popup chrome)

**A) Remove backgrounds from all `.bh-acct-*` classes.** Keep text colour + font-weight, drop `background`:

```css
/* Before */
:global(.bh-acct-red)    { color: var(--c-short) !important; font-weight: 700 !important; background: var(--c-short-10); }
:global(.bh-acct-amber)  { color: var(--c-action) !important; font-weight: 700 !important; background: rgba(251,191,36,0.10); }
:global(.bh-acct-active) { color: var(--c-info) !important; font-weight: 700 !important; background: rgba(34,211,238,0.08); }

/* After — text only, no background */
:global(.bh-acct-red)    { color: var(--c-short) !important; font-weight: 700 !important; }
:global(.bh-acct-amber)  { color: var(--c-action) !important; font-weight: 700 !important; }
:global(.bh-acct-active) { color: var(--c-long) !important; font-weight: 700 !important; }
```

Change `bh-acct-active` text from cyan (`--c-info`) to green (`--c-long`) so it reads "green = healthy" matching the user's expectation of amber/green/neutral coding.

Also remove `border-radius` and `padding` from `.bh-row-account` (the span wrapper) since they were there to contain the background tint.

**B) Fix popup chrome.** The `.bh-modal` overrides `algo-modal` with `border-radius: 0.6rem`. Remove that override so `algo-modal`'s `6px` applies. Also update `z-index` to use `var(--z-drawer)` (20001) instead of magic `9991`. Fix close button `.bh-close` to match canonical pattern: `border: 1px solid rgba(248,113,113,0.35); border-radius: 3px; color: var(--c-short); width: 1.4rem; height: 1.4rem`.

```css
/* .bh-modal — remove border-radius override, fix z-index */
.bh-modal {
  position: fixed;
  top: 3.2rem;
  right: 0.5rem;
  z-index: var(--z-drawer);   /* was 9991 */
  width: min(96vw, 680px);
  max-height: min(90vh, 480px);
  /* border-radius removed — inherits algo-modal's 6px */
}

/* Canonical close button */
.bh-close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 1.4rem;
  height: 1.4rem;
  border: 1px solid rgba(248, 113, 113, 0.35);
  border-radius: 3px;
  color: var(--c-short);
  font-size: var(--fs-xl);
  cursor: pointer;
  background: transparent;
  transition: background 120ms;
  flex-shrink: 0;
}
.bh-close:hover { background: rgba(248, 113, 113, 0.15); }
```

---

### 4. `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` — Fix #5 (colour underlying column)

The `byund-grid` is a hand-rolled Svelte grid. The underlying cell renders as:
```svelte
<span class="byund-und">{g.underlying}</span>
```

**Import `acctColor` from `$lib/account.js`** (already imported in many files; check if already imported here — if not, add it).

**Apply inline colour to the span:**
```svelte
<span class="byund-und"
      style="color: {acctColor(g.underlying) ?? 'inherit'}">
  {g.underlying}
</span>
```

`acctColor` from `$lib/account.js` uses a djb2-variant hash on the string → maps to `ACCT_PALETTE` (7 colours: amber, sky, violet, green, pink, indigo, fuchsia). Each unique underlying symbol gets a consistent colour across renders.

---

## Commit message
fix(pulse/derivatives/broker): scrollable card header left zone, grid header radius, connection popup canonical style, underlying colour

## Done when
- Positions header: "⟷ Pair" button + account picker scroll silently within header; expand/download/fullscreen buttons stay pinned at right edge on mobile
- All six Pulse grids: column-header top edge is a straight line with no inner radius
- BrokerHealthBadge: active accounts show green text, stale = amber text, error = red text — no tinted backgrounds on any row; popup has 6px radius + canonical red close button + z-index 20001
- Derivatives by-underlying grid: each underlying symbol rendered in its consistent hash palette colour
- svelte-check: 0 errors
