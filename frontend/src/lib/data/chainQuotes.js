/**
 * chainQuotes.js — data-layer helpers for parsing /api/options/chain-quotes
 * rows into the shape consumed by OptionChainTab.svelte.
 *
 * Extracted here so the parsing logic is unit-testable independently of the
 * Svelte component. Visual rendering (e.g. the "(L)" depth-absent indicator)
 * lives in the component; full coverage of that requires Playwright.
 */

/**
 * @typedef {{
 *   bid:          number | null,
 *   ask:          number | null,
 *   sym:          string | null,
 *   ls:           number | null,
 *   exchange:     string | null,
 *   depthAvail:   boolean,
 * }} ChainSideQuote
 *
 * @typedef {{
 *   ce: ChainSideQuote,
 *   pe: ChainSideQuote,
 * }} ChainStrikeQuote
 */

/**
 * Parse a single row from the chain-quotes API response into a typed object.
 *
 * The `depthAvail` field is `true` only when the backend explicitly sends
 * `true`. An absent, null, or undefined field means the backend did not confirm
 * depth is available, so `depthAvail` defaults to `false` (no depth). A value
 * of `false` means the broker returned no real market depth for that side and
 * the bid/ask shown is actually the last traded price.
 *
 * @param {Record<string, unknown>} row  - raw row from r.rows[]
 * @param {string} [exchange]            - exchange from the outer response
 * @returns {[string, ChainStrikeQuote]} - [strike key, parsed quote]
 */
export function parseChainQuoteRow(row, exchange) {
  const key = String(/** @type {unknown} */ (row.k));
  const exch = String(/** @type {unknown} */ (row.exchange) || exchange || '');
  return [
    key,
    {
      ce: {
        bid:        row.ce_bid  == null ? null : Number(row.ce_bid),
        ask:        row.ce_ask  == null ? null : Number(row.ce_ask),
        sym:        /** @type {string|null} */ (row.ce_sym  || null),
        ls:         row.ce_ls   == null ? null : Number(row.ce_ls),
        exchange:   exch,
        depthAvail: row.ce_depth_available === true,
      },
      pe: {
        bid:        row.pe_bid  == null ? null : Number(row.pe_bid),
        ask:        row.pe_ask  == null ? null : Number(row.pe_ask),
        sym:        /** @type {string|null} */ (row.pe_sym  || null),
        ls:         row.pe_ls   == null ? null : Number(row.pe_ls),
        exchange:   exch,
        depthAvail: row.pe_depth_available === true,
      },
    },
  ];
}
