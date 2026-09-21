# Plan: Remove dashes from snapshot and legs TOTAL rows; match NavBreakdown format

## Context

NavBreakdown total rows (nav/capital/equity grids) show blank for non-aggregatable
columns. Snapshot and legs TOTAL rows in derivatives/+page.svelte hardcode `'—'`
for those columns. CSS styling (amber, bold, 2px border-top) is already consistent
across all three — only the dash vs blank data representation differs.

## Files to change

| File | Lines | Change |
|---|---|---|
| `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` | 4620–4625 | 6× `—` → `` in legs TOTAL non-aggregatable cells |
| same | 4644–4645 | 2× `—` → `` in legs TOTAL (Qty/OI columns) |
| same | 4655 | `_mergedEv != null ? aggCompact(_mergedEv) : '—'` → `… : ''` |
| same | 4812–4814 | 3× `—` → `` in snapshot TOTAL (LTP, Chg%, P.Close) |
| same | 4818 | `extrinsic === 0 ? '—'` → `extrinsic === 0 ? ''` |
| same | 4820 | `qty_fno \|\| '—'` → `qty_fno \|\| ''` |
| same | 4822 | `!== 0 ? aggCompact(…) : '—'` → `… : ''` |

## Detailed changes

### Legs TOTAL row (lines 4620–4655)

```svelte
<!-- Before: lines 4620–4625 -->
<span class="num">—</span>
<span class="num">—</span><!-- chg % -->
<span class="num">—</span>
<span class="num">—</span>
<span class="num">—</span>
<span class="num">—</span><!-- P.Close -->

<!-- After -->
<span class="num"></span>
<span class="num"></span><!-- chg % -->
<span class="num"></span>
<span class="num"></span>
<span class="num"></span>
<span class="num"></span><!-- P.Close -->
```

```svelte
<!-- Before: lines 4644–4645 -->
<span class="num">—</span>
<span class="num">—</span>

<!-- After -->
<span class="num"></span>
<span class="num"></span>
```

```svelte
<!-- Before: line 4655 -->
{_mergedEv != null ? aggCompact(_mergedEv) : '—'}
<!-- After -->
{_mergedEv != null ? aggCompact(_mergedEv) : ''}
```

### Snapshot TOTAL row (lines 4812–4822)

```svelte
<!-- Before: lines 4812–4814 -->
<span class="num">—</span>
<span class="num">—</span>
<span class="num">—</span>
<!-- After -->
<span class="num"></span>
<span class="num"></span>
<span class="num"></span>
```

```svelte
<!-- Before: line 4818 -->
{positionsDerivedStore.total.extrinsic === 0 ? '—' : aggCompact(positionsDerivedStore.total.extrinsic)}
<!-- After -->
{positionsDerivedStore.total.extrinsic === 0 ? '' : aggCompact(positionsDerivedStore.total.extrinsic)}
```

```svelte
<!-- Before: line 4820 -->
{_byUnderlyingTotal.qty_fno || '—'}
<!-- After -->
{_byUnderlyingTotal.qty_fno || ''}
```

```svelte
<!-- Before: line 4822 -->
{_snapshotTotalEvFull !== 0 ? aggCompact(_snapshotTotalEvFull) : '—'}
<!-- After -->
{_snapshotTotalEvFull !== 0 ? aggCompact(_snapshotTotalEvFull) : ''}
```

## Agents

- frontend: implement all changes above in derivatives/+page.svelte
- backend-test: skip

## Tests

- pytest: no
- svelte-check: yes
- playwright: no

## Commit message

fix(derivatives): remove dashes from snapshot and legs TOTAL rows; match NavBreakdown blank format

## Done when

- Legs TOTAL row shows blank (not —) for LTP, Chg%, P.Close, Qty, OI, and null EV
- Snapshot TOTAL row shows blank (not —) for LTP, Chg%, P.Close, zero extrinsic, missing qty_fno, zero EV
- svelte-check: 0 errors
