/**
 * decomposeSymbol — pure JS equivalent of backend parse_tradingsymbol.
 *
 * Matches the Python regex shapes in backend/api/algo/derivatives.py so
 * the two parsers agree on every Kite tradingsymbol the app handles.
 *
 * Returns:
 *   {
 *     root:       string,          // underlying root (NIFTY, CRUDEOIL, …)
 *     month:      string|null,     // raw month token (26JUN / 25624 etc.)
 *     monthLabel: string|null,     // human-readable (26 JUN / 25 Jun 24)
 *     strike:     number|null,
 *     optType:    'CE'|'PE'|null,
 *     kind:       'opt'|'fut'|'eq'|'idx',
 *     raw:        string,          // original tradingsymbol
 *   }
 */

/** Short month names, Jan-indexed. Use `MONTHS_SHORT[date.getMonth()]`. */
export const MONTHS_SHORT = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

// Monthly options:  NIFTY25APR22000CE, RELIANCE25APR2800CE
const _OPT_MONTHLY = /^([A-Z]+?)(\d{2})([A-Z]{3})(\d+(?:\.\d+)?)(CE|PE)$/;

// Weekly options — Kite: NIFTY YY M DD STRIKE CE/PE  (M single-digit / O/N/D)
const _OPT_WEEKLY = /^([A-Z]+?)(\d{2})([1-9OND])(\d{2})(\d+(?:\.\d+)?)(CE|PE)$/;

// Monthly futures: CRUDEOIL25JUNFUT
const _FUT_MONTHLY = /^([A-Z]+?)(\d{2})([A-Z]{3})FUT$/;

// Known index roots that should be classified 'idx' rather than 'eq'
const _IDX_ROOTS = new Set([
  'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX',
  'BANKEX', 'NIFTYIT', 'NIFTYMIDCAP', 'INDIAVIX',
]);

// Short month codes for weekly format (single char + 2-digit day)
const _SHORT_MONTH = {
  '1': 'JAN', '2': 'FEB', '3': 'MAR', '4': 'APR',
  '5': 'MAY', '6': 'JUN', '7': 'JUL', '8': 'AUG',
  '9': 'SEP', 'O': 'OCT', 'N': 'NOV', 'D': 'DEC',
};

/** Format a month token into a human-readable label.
 *  Monthly "25APR" → "25 APR", Weekly raw token "25424" → "25 APR 24"
 */
function _monthLabel(yy, mon, dd) {
  if (dd != null) {
    return `${yy} ${mon} ${dd}`;
  }
  return `${yy} ${mon}`;
}

/**
 * @param {string} sym  Kite tradingsymbol (will be uppercased)
 * @returns {{
 *   root: string,
 *   month: string|null,
 *   monthLabel: string|null,
 *   strike: number|null,
 *   optType: 'CE'|'PE'|null,
 *   kind: 'opt'|'fut'|'eq'|'idx',
 *   raw: string,
 * }}
 */
export function decomposeSymbol(sym) {
  if (!sym) {
    return { root: sym || '', month: null, monthLabel: null, strike: null, optType: null, kind: 'eq', raw: sym || '' };
  }
  const raw = sym;
  const s = sym.toUpperCase().trim();

  // Monthly option
  let m = _OPT_MONTHLY.exec(s);
  if (m) {
    const [, root, yy, mon, strikeStr, optType] = m;
    return {
      root,
      month: `${yy}${mon}`,
      monthLabel: _monthLabel(yy, mon, null),
      strike: parseFloat(strikeStr),
      optType: /** @type {'CE'|'PE'} */ (optType),
      kind: 'opt',
      raw,
    };
  }

  // Weekly option
  m = _OPT_WEEKLY.exec(s);
  if (m) {
    const [, root, yy, monCode, dd, strikeStr, optType] = m;
    const monStr = _SHORT_MONTH[monCode] || monCode;
    return {
      root,
      month: `${yy}${monCode}${dd}`,
      monthLabel: _monthLabel(yy, monStr, dd),
      strike: parseFloat(strikeStr),
      optType: /** @type {'CE'|'PE'} */ (optType),
      kind: 'opt',
      raw,
    };
  }

  // Monthly future
  m = _FUT_MONTHLY.exec(s);
  if (m) {
    const [, root, yy, mon] = m;
    return {
      root,
      month: `${yy}${mon}`,
      monthLabel: _monthLabel(yy, mon, null),
      strike: null,
      optType: null,
      kind: 'fut',
      raw,
    };
  }

  // Bare symbol (equity or index) — strip space for NIFTY 50 → NIFTY
  const root = s.replace(/\s+/g, ' ');
  const baseRoot = root.split(' ')[0];
  const kind = _IDX_ROOTS.has(baseRoot) ? 'idx' : 'eq';
  return {
    root: s,
    month: null,
    monthLabel: null,
    strike: null,
    optType: null,
    kind,
    raw,
  };
}


/**
 * Compose the display month token. Mirrors the platform convention:
 *   Monthly contracts → "YYMONDD"  (e.g. "26JUN26") when the instruments
 *                       cache supplies an expiry date for the symbol;
 *                       falls back to "YYMON" (e.g. "26JUN") when the
 *                       cache is cold or the symbol isn't in it.
 *   Weekly contracts  → "YYMONDD"  (e.g. "25APR24") — the day is already
 *                       encoded in the tradingsymbol, no lookup needed.
 *
 * Pure helper — accepts the decomposed shape and a YYYY-MM-DD expiry
 * hint (e.g. from getInstrument(sym).x). Lookup happens at the caller.
 *
 * @param {{ month: string|null, monthLabel: string|null }} d
 * @param {string|null} expiryYmd   YYYY-MM-DD or null/undefined when not known
 * @returns {string}
 */
export function composeMonthToken(d, expiryYmd) {
  if (!d?.month) return '';
  // Weekly token (e.g. "25624" raw → "25APR24" display) already has the
  // day baked into the symbol — strip spaces from monthLabel for the
  // compact display, no cache lookup needed.
  const isMonthly5 = d.month.length === 5 && /^\d{2}[A-Z]{3}$/.test(d.month);
  if (!isMonthly5) {
    return (d.monthLabel || d.month).replace(/\s+/g, '');
  }
  // Monthly form: append DD from the expiry hint when supplied. The
  // tradingsymbol itself omits the day for monthlies (e.g.
  // "NIFTY26JUN22000CE" doesn't tell you it expires on 26-Jun), so we
  // look it up via the instruments cache at the call site and pass it
  // in. Falls back to bare "YYMON" when the cache is cold.
  if (expiryYmd && /^\d{4}-\d{2}-\d{2}$/.test(expiryYmd)) {
    const dd = expiryYmd.slice(8, 10);
    return `${d.month}${dd}`;
  }
  return d.month;
}

/**
 * Format a Kite-style F&O tradingsymbol with hyphens for readability,
 * matching Dhan's display convention. Pure display transform —
 * underlying storage stays Kite-format so backend / broker calls
 * keep working. Equity / index symbols pass through unchanged.
 *
 * Examples (when instruments cache supplies expiry):
 *   NIFTY26JUN22000CE         → NIFTY-26JUN26-22000-CE   (monthly + DD)
 *   NIFTY2542422000CE         → NIFTY-25APR24-22000-CE   (weekly)
 *   NIFTY26JUNFUT             → NIFTY-26JUN26-FUT        (monthly + DD)
 *   RELIANCE                  → RELIANCE
 *   NIFTY 50                  → NIFTY 50
 *
 * Cold-cache fallback (no instruments loaded yet):
 *   NIFTY26JUN22000CE         → NIFTY-26JUN-22000-CE     (no DD)
 *
 * @param {string} sym  Kite tradingsymbol (or any string — non-F&O
 *                      symbols return as-is).
 * @returns {string}    Hyphenated display form, or original on parse failure.
 */
export function formatSymbol(sym) {
  if (!sym || typeof sym !== 'string') return sym || '';
  const d = decomposeSymbol(sym);
  // Dynamic import-friendly: instruments cache lookup is inlined here so
  // formatSymbol stays a sync helper. `getInstrument` returns null when
  // cache isn't loaded; composeMonthToken handles that fallback.
  let expiryYmd = null;
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    expiryYmd = _lookupExpiry(sym);
  } catch (_) { /* cache not available — fall back to bare month */ }

  if (d.kind === 'opt' && d.root && d.month && d.strike != null && d.optType) {
    const middle = composeMonthToken(d, expiryYmd);
    // Render strike as integer when it's whole-number to avoid ".0"
    // tails (22000.0 → 22000). Some Indian F&O contracts carry
    // half-point strikes (banknifty intraweek) — those need the ".5".
    const strikeStr = Number.isInteger(d.strike) ? String(d.strike)
                                                  : String(d.strike);
    return `${d.root}-${middle}-${strikeStr}-${d.optType}`;
  }
  if (d.kind === 'fut' && d.root && d.month) {
    return `${d.root}-${composeMonthToken(d, expiryYmd)}-FUT`;
  }
  // Equity / index / unrecognised — pass through unchanged so the
  // caller can swap formatSymbol() in without breaking non-derivative
  // displays.
  return sym;
}

// Lazy-bound instruments-cache lookup. Set once when the cache module
// finishes loading (called from instruments.js after _byTradingsymbol is
// populated). Decoupled from a static import so formatSymbol can stay
// in this leaf module without introducing a circular dependency.
let _lookupExpiry = (_sym) => null;
export function _setExpiryLookup(fn) {
  if (typeof fn === 'function') _lookupExpiry = fn;
}
