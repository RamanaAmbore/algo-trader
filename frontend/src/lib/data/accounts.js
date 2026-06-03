// Broker accounts cache — loaded from /api/accounts/ on first use.
// Small list (~5 rows) so we just keep it in memory per page load; no
// IndexedDB needed.

import { fetchAccounts } from '$lib/api';

let _accounts = null;
let _defaultAccount = '';
let _defaultSymbol = '';
let _loadPromise = null;

export async function loadAccounts() {
  if (_accounts) return _accounts;
  if (_loadPromise) return _loadPromise;
  _loadPromise = (async () => {
    try {
      const data = await fetchAccounts();
      _accounts = (data && data.accounts) || [];
      _defaultAccount = String((data && data.default_account) || '');
      _defaultSymbol  = String((data && data.default_symbol)  || '');
    } catch (e) {
      _accounts = [];
      _defaultAccount = '';
      _defaultSymbol = '';
    }
    return _accounts;
  })();
  try { return await _loadPromise; }
  finally { _loadPromise = null; }
}

/** Operator-configured default broker account
 *  (orders.default_account setting). Empty string when unset or when
 *  the configured value isn't in the loaded set. Synchronous reader —
 *  callers must have already loadAccounts()-ed. */
export function getDefaultAccount() {
  return _defaultAccount;
}

/** Operator-configured default symbol (orders.default_symbol setting).
 *  May be a bare underlying ("NIFTY" / "CRUDEOIL" / "GOLD") which the
 *  modal callers resolve to a tradeable contract, or a full tradeable
 *  symbol. Synchronous reader. */
export function getDefaultSymbol() {
  return _defaultSymbol;
}

export function getAccountsSync() {
  return _accounts || [];
}

/** Suggest account IDs matching the given prefix (case-insensitive). */
export function suggestAccounts(prefix, limit = 20) {
  const list = _accounts || [];
  const p = (prefix || '').toUpperCase();
  const matches = list
    .map(a => a.account_id)
    .filter(id => !p || id.toUpperCase().startsWith(p));
  return matches.slice(0, limit);
}

/** Look up an account's masked display name. */
export function getDisplay(accountId) {
  const list = _accounts || [];
  const m = list.find(a => a.account_id === accountId);
  return m ? m.display : accountId;
}
