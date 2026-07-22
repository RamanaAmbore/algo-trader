# Plan: Card label consistency + dashboard gap + OrderTicket refresh button layout

## Context

Three focused fixes:

1. **Dashboard gap (double-margin)**: `.dash-activity` carries `margin-bottom: 0.6rem` but
   the global `bucket-card + bucket-card { margin-top: 0.6rem }` already handles inter-card
   spacing. In a flex container, adjacent margins don't collapse — both fire, producing a 1.2rem
   gap instead of the standard 0.6rem. The inline comment at line 2582 even notes the global
   rule handles this, but the local margin was never removed.

2. **OrderTicket refresh button invisible/wrong position**: `.ot-header` uses
   `justify-content: space-between` with THREE children — `.ot-symbol`, `.ot-refresh-btn`,
   `.ot-close`. With space-between and 3 items, the refresh button lands dead-center in the
   header, far from the close button. It needs to be grouped with the close button on the right.

3. **Card label font inconsistency**: `legs-header` in derivatives page uses
   `font-family: monospace` while `.ch-title` (CardHeader) uses `font-family: inherit`.
   All other card-like labels should use the same font-family.

## Agents
- backend: skip
- frontend: All three changes below.
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Frontend agent brief

### Change A — Remove duplicate margin from `.dash-activity`
File: `frontend/src/routes/(algo)/dashboard/+page.svelte`

Find `.dash-activity` CSS block (around line 2858). Remove `margin-bottom: 0.6rem;` from it.
The global `bucket-card + bucket-card` rule in app.css already provides the 0.6rem gap.

```css
/* BEFORE */
.dash-activity {
  margin-bottom: 0.6rem;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

/* AFTER */
.dash-activity {
  display: flex;
  flex-direction: column;
  min-height: 0;
}
```

### Change B — Group refresh + close buttons in OrderTicket header
File: `frontend/src/lib/order/OrderTicket.svelte`

**Template** (around line 1941): The `.ot-header` currently has three direct flex children:
```svelte
<div class="ot-header">
  <div class="ot-symbol">…</div>
  <button class="ot-refresh-btn">…</button>
  <button class="ot-close">×</button>
</div>
```

Wrap the two buttons in a group div:
```svelte
<div class="ot-header">
  <div class="ot-symbol">…</div>
  <div class="ot-header-actions">
    <button class="ot-refresh-btn">…</button>
    <button class="ot-close">×</button>
  </div>
</div>
```

**CSS**: Add `.ot-header-actions` near `.ot-close` rules:
```css
.ot-header-actions {
  display: flex;
  align-items: center;
  gap: 0.3rem;
  flex-shrink: 0;
  margin-left: auto;
}
```

The existing `.ot-header { justify-content: space-between; }` now only has two children
(symbol div + actions group), so symbol takes remaining space and actions hug the right.

### Change C — Sync `legs-header` font-family in derivatives page
File: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`

Find `.legs-header` CSS (around line 5625). Change `font-family: monospace` to
`font-family: inherit` to match `.ch-title`'s standard:

```css
/* BEFORE */
.legs-header {
  ...
  font-family: monospace;
  ...
}

/* AFTER */
.legs-header {
  ...
  font-family: inherit;
  ...
}
```

After all changes:
```bash
cd /Users/ramanambore/projects/ramboq/frontend && npx svelte-check --output machine 2>&1 | tail -5
```

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(ui): remove double gap on dash-activity, group OT refresh+close, sync legs-header font

## Done when
- Gap between LogPanel and agent activity panel on dashboard matches all other inter-card gaps
- OrderTicket modal header shows: symbol | [refresh] [×] — both buttons right-aligned together
- Legs section header in derivatives page uses inherit font-family matching ch-title
- svelte-check: 0 errors
