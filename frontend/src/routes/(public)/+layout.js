// Public routes are prerendered at build time so Googlebot sees
// real HTML content on first crawl — no JS required to index them.
// The (algo) group keeps ssr=false / prerender=false from the root
// layout.js since those pages need client-only real-time rendering.
export const prerender = true;
export const ssr = true;
