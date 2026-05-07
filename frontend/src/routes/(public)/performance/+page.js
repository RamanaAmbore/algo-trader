// Performance page cannot be prerendered — it imports ag-Grid
// (browser-only) and fetches live broker data on mount.
// Override the (public)/+layout.js prerender=true for this page only.
export const prerender = false;
export const ssr = false;
