// Symbol-type filter — single source of truth for the four-way
// instrument family picker used by /orders, /charts,
// ChartWorkspace, SymbolPanel, and SymbolSearchInput.
//
// Operator: "now symbol type, either it is all type, or equity,
// futures and options. so, 4 possibilities. so make it consistent
// across all the modals and pages keeping the screen space in mind".
//
// Labels are deliberately short — every consumer renders the
// picker inline next to a symbol search input, so the surrounding
// row stays compact at narrow viewports.

/** @type {Array<{value: 'ALL'|'EQ'|'FUT'|'OPT', label: string}>} */
export const SYM_TYPE_OPTS = [
  { value: 'ALL', label: 'All'     },
  { value: 'EQ',  label: 'Equity'  },
  { value: 'FUT', label: 'Futures' },
  { value: 'OPT', label: 'Options' },
];

/** @typedef {'ALL'|'EQ'|'FUT'|'OPT'} SymbolType */
