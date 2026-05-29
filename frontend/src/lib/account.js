/**
 * Shared per-account color palette + hash mapping.
 *
 * Each operator account (ZG0790 / ZJ6294 / …) gets a stable colour
 * via a djb2 hash mod the palette length. The same code lands on the
 * same colour everywhere in the UI — PerformancePage account column
 * stripes, MarketPulse right-grid symbol cell tint, etc.
 *
 * TOTAL rows + null accounts return null (caller uses transparent /
 * no tint).
 */

export const ACCT_PALETTE = [
  '#fbbf24', // amber
  '#7dd3fc', // sky
  '#a78bfa', // violet
  '#4ade80', // green
  '#f472b6', // pink
  '#a5b4fc', // indigo
  '#f0abfc', // fuchsia
];

/** @param {string | null | undefined} account */
export function acctColor(account) {
  if (!account || account === 'TOTAL') return null;
  let h = 5381;
  for (let i = 0; i < account.length; i++) {
    h = ((h << 5) + h) ^ account.charCodeAt(i);
    h = h >>> 0; // force unsigned 32-bit
  }
  return ACCT_PALETTE[h % ACCT_PALETTE.length];
}

/**
 * Pick the lead account from a row's `accounts` Set / array. Used
 * when colour-coding the symbol cell — a multi-account row is rare
 * but real (same symbol held in 2 accounts), so we tint by the first
 * account and let the rendered Account column show the full list.
 *
 * @param {{accounts?: Set<string> | string[]} | null | undefined} row
 * @returns {string | null}
 */
export function leadAccount(row) {
  if (!row) return null;
  const accts = row.accounts;
  if (!accts) return null;
  if (accts instanceof Set) {
    const it = accts.values().next();
    return it.done ? null : String(it.value || '');
  }
  if (Array.isArray(accts) && accts.length > 0) return String(accts[0]);
  return null;
}
