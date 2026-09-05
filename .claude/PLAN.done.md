# Plan: Order chip contrast fix — more defined containers, legible secondary text

## Context

Operator reported that order chips in the chain basket and order ticket are hard to read:
- Background is not visible enough to create a chip container feel (6% alpha = nearly transparent)
- Symbol text (`chain-basket-sym`) uses `var(--algo-slate)` ≈ mid-gray which fades into the dark surface
- Qty text uses `var(--text-muted)` + `opacity: 0.85` = double-muted, barely legible at `var(--fs-xs)`
- The order ticket symbol chip (`.ot-sym-chip`) at 10% amber similarly lacks container definition

Direction approved: **more defined container** — increase background alpha to ~14%, symbol text near-white, qty text single-muted without opacity modifier.

---

## Files and exact changes

### 1. `frontend/src/app.css` — add missing alpha design tokens

After the existing `--c-long-10` and `--c-short-10` lines (around line 269-275), add:

```css
--c-long-14:  rgba(74, 222, 128, 0.14);    /* medium green bg — chip container */
--c-short-14: rgba(248, 113, 113, 0.14);   /* medium red bg  — chip container */
```

### 2. `frontend/src/lib/order/OptionChainTab.svelte` — 4 changes (lines ~1502-1510)

**2a. BUY chip background:**
```css
/* before */
.chain-basket-leg-buy  { color: var(--c-long); background: var(--c-long-06); }
/* after */
.chain-basket-leg-buy  { color: var(--c-long); background: var(--c-long-14); }
```

**2b. SELL chip background:**
```css
/* before */
.chain-basket-leg-sell { color: var(--c-short); background: var(--c-short-06); }
/* after */
.chain-basket-leg-sell { color: var(--c-short); background: var(--c-short-14); }
```

**2c. Symbol text inside chip (line ~1509):**
```css
/* before */
.chain-basket-sym { color: var(--algo-slate); font-weight: 600; }
/* after */
.chain-basket-sym { color: #e2e8f0; font-weight: 600; }
```

**2d. Qty text inside chip (line ~1510) — drop double-muting:**
```css
/* before */
.chain-basket-qty { color: var(--text-muted); font-size: var(--fs-xs); opacity: 0.85; font-variant-numeric: tabular-nums; }
/* after */
.chain-basket-qty { color: var(--c-muted); font-size: var(--fs-xs); font-variant-numeric: tabular-nums; }
```

### 3. `frontend/src/lib/order/OrderTicket.svelte` — 1 change (line ~3178)

**3a. Symbol chip background — use existing 14% token:**
```css
/* before */
.ot-sym-chip {
  ...
  background: rgba(251, 191, 36, 0.10);
  border: 1px solid rgba(251, 191, 36, 0.35);
  color: var(--c-action);
  ...
}
/* after */
.ot-sym-chip {
  ...
  background: var(--c-action-14);
  border: 1px solid rgba(251, 191, 36, 0.45);
  color: #fef9c3;   /* amber-100: clearly bright against 14% amber bg */
  ...
}
```

---

## Agents

- frontend: Apply all changes above to `app.css`, `OptionChainTab.svelte`, `OrderTicket.svelte`. Run svelte-check to confirm 0 errors. No logic changes — CSS only.
- backend-test: skip
- backend: skip
- playwright: skip

## Tests

- pytest: no
- svelte-check: yes
- playwright: no

## Commit message

fix(ui): improve order chip contrast — stronger bg, near-white symbol text, single-muted qty
- Add --c-long-14 / --c-short-14 design tokens to app.css (14% alpha variants)
- Basket leg chips: 6%→14% bg, algo-slate→#e2e8f0 symbol text, drop opacity: 0.85 on qty
- Order ticket sym chip: 10%→14% amber bg, amber border tightened, text #fef9c3

## Done when

1. Basket BUY/SELL chips show a clearly visible green/red container (not just a border)
2. Symbol text inside chips is near-white (#e2e8f0) and legible at small size
3. Qty text is readable (--c-muted, no opacity modifier)
4. Order ticket symbol chip has a stronger amber container with bright text
5. svelte-check 0 errors
