# Frontend Audit — ChartWorkspace + SymbolPanel + OrderTicket (2026-07-11)

## P1 — Correctness / dead weight

| File:line | Issue | Fix |
|---|---|---|
| SymbolPanel:1846-1907 | **Entire orders panel is dead** — `_orders`, `_algoRejected`, `_ordersPending`, `_ordersCompleted`, `_fmtEventTime`, `_loadOrdersData`, `_ordersPoll`, `PENDING_STATUSES`, `_bottomTab` — no template consumer. `onMount` starts a `visibleInterval` firing `fetchOrders` + `fetchAlgoOrdersRecent` every 3s with silent `catch(_)`. `<ActivityLogSurface>` already owns orders display. | Delete all state + functions + imports (`placeTicketOrder`, `fetchOrders`, `fetchAlgoOrdersRecent`, `logTime`) + poll. ~60 lines + 4 imports. Kills 2 API calls per 3s. |
| SymbolPanel:1833-1843, 1980-1989 | `handleCmdAddToBasket` / `_cmdOrderProps` — Command tab retired (comment at 2202 confirms). Never invoked. | Delete + collapse `_ticketProps` to static-object form. |
| ChartWorkspace:54, 64-66 | Imports `searchByPrefix`, `suggestUnderlyings`, `findEquity`, `getInstrument`, `fetchChartSymbols` — never referenced. Chart uses `<SymbolSearchInput>` instead. | Remove unused imports. |

## P2 — Large structural improvements

### Cross-file dedup (highest ROI)
| What | Files | Lines saved |
|---|---|---|
| `_appliesToFor` duplicated verbatim | OrderTicket:696, SymbolPanel:833 | Extract to `$lib/data/templateScope.js` |
| Chase L/M/H trio identical markup | OrderTicket:2376-2393, SymbolPanel:2062-2075 | Extract `<ChaseAggPicker>` component |
| Debounced preview `$effect` pattern | OrderTicket:1811, SymbolPanel:670 | Extract `$lib/data/useTemplatePreview.svelte.js` |
| Chart tooltip markup | ChartWorkspace:1928, 2313 | Extract `<ChartTooltip>` (props: rows array) | ~60 lines |

### Per-file
| File | What | Lines | Est reduction |
|---|---|---|---|
| SymbolPanel | Extract `BasketPill.svelte` + `LegOverrideEditor.svelte` | 2557-2790 | ~230 lines |
| SymbolPanel | Extract `TemplateBar.svelte` | 2353-2527 | ~160 lines |
| SymbolPanel | Extract `ModalCommonActions.svelte` (footer) | 2813-3023 | ~210 lines |
| ChartWorkspace | Move path builders → `$lib/chart/paths.js` (pure) | 1006-1187 | ~230 lines |
| ChartWorkspace | Extract `RsiPanel.svelte` + `MacdPanel.svelte` | 2126-2223 | ~100 lines |
| ChartWorkspace | Move `_signalMarkers`/`_signalLayout` → `$lib/chart/signalMarkers.js` | 1212-1311 | ~100 lines |
| OrderTicket | Extract `OrderKnobsRow.svelte` (7 Select fields) | 2053-2183 | ~100 lines |
| OrderTicket | Extract `BareUnderlyingPicker.svelte` | 1955-2001 | ~46 lines |

## P3 — Palette / format cleanup

| Files | Issue |
|---|---|
| ChartWorkspace: 30+ SVG hex colors | Replace with `var(--c-long)`, `var(--c-short)`, `var(--c-info)` etc. |
| SymbolPanel: 43 hex in style + TABS metadata line 1977 | Same |
| OrderTicket: 24 hex in template + style | Same |
| SymbolPanel:2508 | `'₹' + Number(v).toLocaleString('en-IN')` → use `priceFmt` from format.js |
| SymbolPanel:2516 | `.toFixed(2)` → `priceFmt` |

## Svelte 5 migration
All three files are complete — 0 `$:`, 0 `writable/readable`. No gaps.

## Estimated line reduction
- SymbolPanel: ~600 lines (script + template)
- ChartWorkspace: ~430 lines (script)
- OrderTicket: ~300 lines (template + script)
