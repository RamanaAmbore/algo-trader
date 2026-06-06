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
