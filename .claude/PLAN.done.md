# Plan: LogPanel + ActivityHeaderFilters polish

## Task
Four targeted edits across two files:
1. **ActivityHeaderFilters** — close the gap between the two dropdowns (flush, not 0.3rem apart).
2. **LogPanel lp-card-btns reorder** — move Download before Collapse/Fullscreen in the label-based button group.
3. **LogPanel double-scroll fix** — add `overflow-x: visible` on `.lp-tab-strip-wrap :global(.algo-tabs-strip)` to stop AlgoTabs from absorbing the scroll before the outer wrapper.
4. **LogPanel JS-applied row stripes** — replace `:nth-child(odd)` with index-based `lp-row-odd/lp-row-even` classes so stripe colour survives search filtering.

## Agents
- backend: skip
- frontend: implement all four changes to the two files
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Concrete implementation notes

### ActivityHeaderFilters.svelte — TWO gap values to zero

There are two `.act-filters` gap declarations:
- Line 90 (base rule): `gap: 0.3rem;` → `gap: 0;`
- Line 145 (mobile media query): `gap: 0.3rem;` → `gap: 0;` (or remove the line — it becomes redundant)

Both must change or the mobile override silently reintroduces the gap at ≤520px.

### LogPanel lp-card-btns reorder (label-based block only, lines ~1433–1469)

Current order inside `{#if label}` → `.lp-card-btns`:
1. Search button
2. `{#if context !== 'page'}` → Close OR Collapse+Fullscreen
3. Download button

Target order:
1. Search button
2. Download button
3. `{#if context !== 'page'}` → Close OR Collapse+Fullscreen

Move the Download `<button>` block (lines ~1458–1468) to sit immediately after the Search button, before the `{#if context !== 'page'}` block.

**Scope**: `lp-card-btns-legacy` block (lines ~1472–1526) is a different branch (mounts without `label` prop) and uses a different button set. Task did NOT ask to touch it. Leave untouched.

### LogPanel overflow-x fix — add in `<style>`

Inside the existing `.lp-tab-strip-wrap` rule (around line 1778), add a sibling rule:

```css
.lp-tab-strip-wrap :global(.algo-tabs-strip) {
  overflow-x: visible;
}
```

### LogPanel stripe approach — post-filter index injection

**Strategy**: Apply stripe after the search filter, using a second `.map` step on the final array, replacing the html string's class attribute. This avoids changing `_logRow()` or `_renderSimLine()` signatures and correctly re-stripes after rows are dropped by the filter.

Pattern for each derived array (`_agentRows`, `_simRows`, `_sysRows`, `_connRows`):
```js
.filter(r => _rowMatchesSearch(r.html))
.map((r, i) => {
  const stripe = i % 2 === 0 ? 'lp-row-odd' : 'lp-row-even';
  return { ...r, html: r.html.replace('<div class="log-row ', `<div class="log-row ${stripe} `) };
})
```

For `_terminalHtml()` — apply stripe post-sort+post-search-filter just before `.join('')` (around line 1287):
```js
const striped = all.map((x, i) => {
  const stripe = i % 2 === 0 ? 'lp-row-odd' : 'lp-row-even';
  return { ...x, html: x.html.replace('<div class="log-row ', `<div class="log-row ${stripe} `) };
});
return striped.length
  ? striped.map(x => x.html).join('')
  : '<div class="log-row log-debug">...</div>';
```

**CSS change**: Replace the existing `:nth-child(odd)` rule (line 1956):
```css
:global(.log-panel.log-rows .log-row:nth-child(odd)) {
  background: var(--ag-odd-row-background-color, rgba(13,22,42,0.30));
}
```
with:
```css
:global(.log-panel.log-rows .log-row.lp-row-odd) {
  background: var(--ag-odd-row-background-color, rgba(13,22,42,0.30));
}
:global(.log-panel.log-rows .log-row.lp-row-even) {
  background: transparent;
}
```

**Empty-state rows** (`log-row log-debug`) are always a single solo row — no stripe needed, leave them untouched.

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(log-panel): flush filter gap, reorder download btn, fix tab scroll, index-based row stripes

## Done when
- ActivityHeaderFilters: zero gap between account and level dropdowns on all viewports
- LogPanel: Download button appears before Collapse/Fullscreen in label-based card button group
- LogPanel: `.lp-tab-strip-wrap :global(.algo-tabs-strip)` has `overflow-x: visible` in CSS
- LogPanel: row stripes use `lp-row-odd/lp-row-even` classes applied post-filter; `nth-child(odd)` rule removed
- `svelte-check` passes
