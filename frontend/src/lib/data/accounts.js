// Broker accounts cache — loaded from /api/accounts/ on first use.
// Small list (~5 rows) so we just keep it in memory per page load; no
// IndexedDB needed.

import { fetchAccounts } from '$lib/api';
import { writable } from 'svelte/store';

let _accounts = null;
let _defaultAccount = '';
let _defaultSymbol = '';
let _loadPromise = null;

// Reactive stores so callers can subscribe and get the value the
// moment loadAccounts() resolves. Used by PageHeaderActions to avoid
// the "modal opens with NIFTY 50 hardcoded fallback, then race-flips
// to CRUDEOIL once the fetch lands" UX glitch the operator hit.
export const defaultSymbolStore  = writable('');
export const defaultAccountStore = writable('');
export const accountsReadyStore  = writable(false);

// ── Recently-used symbol + account ─────────────────────────────────
// Operator: "let orders page and chart page use default symbol if
// there is no recent symbol is used in charts or orders. if any
// symbol is used in charts or orders either in model, or page, the
// symbol should be defaulted to that. similarly the account in
// orders page should be defaulted to recent order account."
//
// Resolution chain on every reader: recent → settings default → ''.
// Persisted in localStorage so the value survives a hard refresh.
const _LS_RECENT_SYMBOL  = 'ramboq.recent.symbol';
const _LS_RECENT_ACCOUNT = 'ramboq.recent.account';

function _lsRead(/** @type {string} */ key) {
  if (typeof window === 'undefined') return '';
  try { return localStorage.getItem(key) || ''; } catch { return ''; }
}
function _lsWrite(/** @type {string} */ key, /** @type {string} */ value) {
  if (typeof window === 'undefined') return;
  try {
    if (value) localStorage.setItem(key, value);
    else       localStorage.removeItem(key);
  } catch { /* ignore */ }
}

// Bare underlying names that operators search for to BUILD a
// derivative ticket — they're NOT directly tradable (you trade
// CRUDEOIL26JUNFUT, not "CRUDEOIL"; you trade NIFTY26JUN22000CE,
// not "NIFTY"). Persisting one as the "recent symbol" leaves the
// /orders + chart pages opening with a bare name that can't be
// placed and surfaces no valid quote. Operator: "order entry is
// showing crudeoil for symbol which is not a valid symbol. it
// should be corrected everywhere".
const _BARE_UNDERLYINGS = new Set([
  // Index underlyings (NSE)
  'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'NIFTYNXT50',
  'SENSEX', 'BANKEX',
  // MCX commodities
  'CRUDEOIL', 'CRUDEOILM', 'GOLD', 'GOLDM', 'GOLDPETAL', 'GOLDGUINEA',
  'SILVER', 'SILVERM', 'SILVERMIC',
  'COPPER', 'NATURALGAS', 'NATGASMINI', 'ZINC', 'LEAD', 'LEADMINI',
  'ALUMINIUM', 'ALUMINI', 'NICKEL', 'MENTHA', 'COTTON', 'CARDAMOM',
  // CDS / currency underlyings
  'USDINR', 'EURINR', 'GBPINR', 'JPYINR',
]);

function _isTradableSymbol(/** @type {string} */ s) {
  const v = String(s || '').trim().toUpperCase();
  if (!v) return false;
  return !_BARE_UNDERLYINGS.has(v);
}

// Pre-filter any stale bare-underlying value already persisted in
// localStorage before the validation existed (e.g. operator had
// "CRUDEOIL" set from a prior session). Keeps the recentSymbolStore
// initial value in sync with what resolveSymbol returns.
const _initialSym = _lsRead(_LS_RECENT_SYMBOL);
const _bootSym    = _isTradableSymbol(_initialSym) ? _initialSym : '';
if (typeof window !== 'undefined' && _initialSym && !_bootSym) {
  try { localStorage.removeItem(_LS_RECENT_SYMBOL); } catch { /* ignore */ }
}
export const recentSymbolStore  = writable(_bootSym);
export const recentAccountStore = writable(_lsRead(_LS_RECENT_ACCOUNT));

/** Record the symbol the operator just used (search-picked OR
 *  submitted on an order). Empty / whitespace-only inputs are
 *  ignored so an accidental clear doesn't wipe the persisted value.
 *  Bare underlying names (NIFTY / CRUDEOIL / GOLD / …) are also
 *  rejected — they're not directly tradable; persisting one as the
 *  "recent" surface defaults every page to an unplaceable symbol. */
export function setRecentSymbol(/** @type {string} */ sym) {
  const v = String(sym || '').trim().toUpperCase();
  if (!v) return;
  if (!_isTradableSymbol(v)) return;
  recentSymbolStore.set(v);
  _lsWrite(_LS_RECENT_SYMBOL, v);
}

/** Record the account the operator just placed an order from. */
export function setRecentAccount(/** @type {string} */ acct) {
  const v = String(acct || '').trim();
  if (!v) return;
  recentAccountStore.set(v);
  _lsWrite(_LS_RECENT_ACCOUNT, v);
}

/** Resolution chain: recent → fallback. Operator: "Remove crudeoil
 *  symbol as default symbol. remove the setting completely. The
 *  symbol should be updated from the latest symbol used or clear
 *  from the context for modals". orders.default_symbol setting
 *  retired; modals open with the recent symbol or empty. */
export function resolveSymbol(/** @type {string} */ fallback = '') {
  const r = _lsRead(_LS_RECENT_SYMBOL);
  // Defensive: an existing localStorage entry for a bare underlying
  // (e.g. operator's prior session had "CRUDEOIL" set before this
  // validation existed) is also stripped so /orders + chart pages
  // don't keep opening with an unplaceable symbol after the upgrade.
  if (r && _isTradableSymbol(r)) return r.toUpperCase();
  return String(fallback || '').toUpperCase();
}

/** Resolution chain: recent → settings default → fallback. */
export function resolveAccount(/** @type {string} */ fallback = '') {
  const r = _lsRead(_LS_RECENT_ACCOUNT);
  if (r) return r;
  if (_defaultAccount) return _defaultAccount;
  return fallback || '';
}

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
    defaultAccountStore.set(_defaultAccount);
    defaultSymbolStore.set(_defaultSymbol);
    accountsReadyStore.set(true);
    return _accounts;
  })();
  try { return await _loadPromise; }
  finally { _loadPromise = null; }
}

// Kick off the fetch as soon as the module is evaluated (browser-only)
// so by the time PageHeaderActions / SymbolPanel mount, the default
// symbol + account are already available. Module-level cache prevents
// duplicate fetches; the import side-effect adds no cost when the
// page never opens an order modal.
if (typeof window !== 'undefined') {
  loadAccounts().catch(() => { /* silent */ });
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
