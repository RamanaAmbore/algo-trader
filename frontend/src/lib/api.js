/**
 * API helpers — thin wrappers around the Litestar REST endpoints.
 * All functions return plain JS objects matching the Pydantic response schemas.
 *
 * Auth: protected endpoints require a JWT stored in sessionStorage as 'ramboq_token'.
 * A 401 response clears the token and redirects to /signin.
 *
 * Base URL resolves to the Vite dev-proxy (/api → http://localhost:8000)
 * in dev mode, and to the same origin in production.
 */

const BASE = '/api';

import { authStore } from '$lib/stores';

/** Return auth headers if a token is present, empty object otherwise. */
function _authHeaders() {
  const token = authStore.getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function _hasToken() { return !!authStore.getToken(); }

/** Handle 401 — clear the token and (only if there was one) redirect
 *  to signin. An anonymous demo session can hit endpoints that 401
 *  for unauthenticated requests; those shouldn't bounce the visitor
 *  to /signin (they're browsing the public algo demo, not a stale
 *  admin session). The redirect now fires only when a token actually
 *  existed — i.e., a session expired. */
function _handle401() {
  const hadToken = !!authStore.getToken();
  authStore.logout();
  if (hadToken && typeof window !== 'undefined') {
    window.location.href = '/signin';
  }
}

// ── Error handling: friendly UI message + raw console log ────────────
// Anonymous sessions on prod = demo. We treat them as such for two
// reasons: (1) error messages should explain "this is read-only" rather
// than "Unauthorized"; (2) raw console output must be masked so account
// IDs / tokens don't leak to a recruiter who opens devtools.
function _isAnonymous() { return !authStore.getToken(); }

// Patterns we mask before printing to console in anonymous (demo)
// sessions. Backend already masks accounts via mask_column() in row
// data; this is a defence-in-depth net for stack traces or error
// detail strings that might still carry the raw values.
const _SECRET_PATTERNS = [
  { re: /\bZ[A-Z]\d{4,8}\b/g, sub: 'Z#####' },                            // account IDs (ZG0790, ZJ6294, …)
  { re: /\b[A-Z0-9]{32,}\b/g, sub: '<key>' },                             // long uppercase tokens / keys
  { re: /\bbearer\s+[a-zA-Z0-9._-]{20,}\b/gi, sub: 'bearer <token>' },    // JWT-shaped strings
  { re: /\b[A-Za-z0-9._-]+@[A-Za-z0-9.-]+\.[A-Z]{2,}\b/gi, sub: '<email>' }, // email addresses
];

function _maskForDemoLog(/** @type {unknown} */ value) {
  if (typeof value === 'string') {
    let out = value;
    for (const { re, sub } of _SECRET_PATTERNS) out = out.replace(re, sub);
    return out;
  }
  if (value && typeof value === 'object') {
    try {
      return JSON.parse(_maskForDemoLog(JSON.stringify(value)));
    } catch (_) { return value; }
  }
  return value;
}

/** Log the raw error to the browser console for debugging. In an
 *  anonymous (demo) session, account IDs and secrets are masked first
 *  so accidental leaks via console.error never show real values. */
function _logApiError(/** @type {string} */ path,
                     /** @type {number|null} */ status,
                     /** @type {unknown} */ raw) {
  const safe = _isAnonymous() ? _maskForDemoLog(raw) : raw;
  // Use console.warn rather than .error so a transient 5xx during a
  // poll doesn't tag every page with the red-error glyph in devtools.
  console.warn(`[api] ${path}${status ? ` (${status})` : ' (network)'}:`, safe);
}

// Strip HTTP-method boilerplate and clamp to one banner line. Full
// detail still lives in the console — this is just for display.
// Non-string inputs (e.g. structured 422 detail = {blocked: [...]})
// are summarised before regex-replace so .replace never blows up.
const _METHOD_PREFIX_RE = /^(GET|POST|PUT|PATCH|DELETE)\s+\S+\s+failed:\s*/i;
function _trimDetail(/** @type {unknown} */ s) {
  let str;
  if (typeof s === 'string') {
    str = s;
  } else if (s && typeof s === 'object') {
    const o = /** @type {any} */ (s);
    if (Array.isArray(o.blocked) && o.blocked.length) {
      str = o.blocked[0]?.reason || 'Order blocked.';
    } else {
      str = o.reason || o.message || JSON.stringify(o);
    }
  } else {
    str = String(s ?? '');
  }
  const cleaned = str.replace(_METHOD_PREFIX_RE, '');
  return cleaned.length > 60 ? cleaned.slice(0, 57) + '…' : cleaned;
}

/** Translate a fetch failure into a short UI string. Pages render
 *  this verbatim in a small banner — keep it terse (~25-35 chars) so
 *  the layout doesn't shift. Full detail goes to the console via
 *  _logApiError; operators open devtools for the long form.
 *
 *  Demo mode (anonymous on prod) gets a single soft message regardless
 *  of underlying status. A recruiter doesn't need to see "Server busy"
 *  or "Permission denied" — every error reads the same way: "Demo
 *  mode — feature unavailable". The raw error still goes to
 *  console.warn for operators inspecting devtools.
 */
function _friendlyError(/** @type {number|null} */ status,
                        /** @type {string|null} */ detail) {
  const isAnon = _isAnonymous();
  if (isAnon) {
    // 404 typically means "page no longer exists" (e.g. stale bookmark
    // to a deleted stub) rather than a feature gate — show a clearer
    // hint than the generic demo banner.
    if (status === 404) return 'Not available.';
    // 401 / 403 in demo IS the gating mechanism — surface that.
    if (status === 401 || status === 403) {
      return 'Demo mode — feature unavailable.';
    }
    // Every other error in demo (5xx, network, 429) is transient and
    // unrelated to gating — fall through to the same handling auth
    // users get so the operator/recruiter sees 'Server busy' not
    // 'Demo mode' when the broker is having a moment.
  }
  if (status === 401) {
    if (detail) return _trimDetail(detail);
    return 'Session expired.';
  }
  if (status === 403) {
    if (detail) return _trimDetail(detail);
    return 'Not allowed.';
  }
  if (status === 404)              return 'Not available.';
  if (status === 429)              return 'Too many requests.';
  // 5xx — backend detail wins when present so specific upstream
  // problems (e.g. "Broker (Kite) is temporarily unavailable") read
  // accurately instead of the generic "Server busy" line. Falls back
  // to the generic when the server didn't supply a detail.
  if (status && status >= 500)     return detail ? _trimDetail(detail) : 'Server busy — retry.';
  if (status == null || status === 0) return 'No connection.';
  if (detail) return _trimDetail(detail);
  return 'Request rejected.';
}

/** One fetch wrapper to rule them all. Replaces ~15 hand-rolled
 *  fetch+401+error blocks, and routes every error through the
 *  friendly-message + masked-log pipeline.
 *
 *  Callers may pass an `AbortSignal` via `opts.signal` so they can
 *  cancel in-flight requests (e.g. component unmount, per-call timeout).
 *  An AbortError is re-thrown as-is so the caller can distinguish
 *  intentional cancellation from a real network failure. */
async function _request(/** @type {string} */ method,
                        /** @type {string} */ path,
                        /** @type {{auth?: boolean, body?: unknown, signal?: AbortSignal}} */ opts = {}) {
  const { auth = false, body, signal } = opts;
  /** @type {Record<string, string>} */
  const headers = auth ? { ..._authHeaders() } : {};
  /** @type {RequestInit} */
  const init = { method, headers };
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  // Apply default 15 s timeout when the caller did not supply their own signal.
  // Prevents any single hung fetch from stranding the page indefinitely.
  // Callers that supply their own AbortController (e.g. NavTab with per-fetch
  // cancel) keep full control — their signal takes precedence.
  const _defaultAc = signal ? null : new AbortController();
  const _timeoutId = _defaultAc
    ? setTimeout(() => _defaultAc.abort(), 15_000)
    : null;
  init.signal = signal ?? _defaultAc?.signal;
  let res;
  try {
    res = await fetch(`${BASE}${path}`, init);
  } catch (e) {
    if (_timeoutId != null) clearTimeout(_timeoutId);
    // Re-throw AbortError so callers can detect intentional cancellation.
    if (/** @type {any} */ (e)?.name === 'AbortError') throw e;
    _logApiError(path, null, /** @type {any} */ (e)?.message || e);
    throw new Error(_friendlyError(null, null));
  }
  if (_timeoutId != null) clearTimeout(_timeoutId);
  if (res.status === 401) {
    _handle401();
    const friendly = _friendlyError(401, null);
    _logApiError(path, 401, friendly);
    throw new Error(friendly);
  }
  if (!res.ok) {
    const errBody = await res.json().catch(() => ({}));
    const detail = errBody?.detail || res.statusText || null;
    _logApiError(path, res.status, detail);
    const err = new Error(_friendlyError(res.status, detail));
    /** @type {any} */ (err).status = res.status;
    /** @type {any} */ (err).detail = detail;
    throw err;
  }
  if (res.status === 204) return null;
  return res.json();
}

// Method-specific shortcuts over _request — same friendly-error +
// masked-log pipeline as everything else.
const _get   = (/** @type {string} */ path, /** @type {any} */ opts = {}) =>
  _request('GET', path, opts);
const _post  = (/** @type {string} */ path, /** @type {any} */ body, /** @type {any} */ opts = {}) =>
  _request('POST', path, { ...opts, body });
const _put   = (/** @type {string} */ path, /** @type {any} */ body, /** @type {any} */ opts = {}) =>
  _request('PUT', path, { ...opts, body });
const _patch = (/** @type {string} */ path, /** @type {any} */ body, /** @type {any} */ opts = {}) =>
  _request('PATCH', path, { ...opts, body });
const _del   = (/** @type {string} */ path, /** @type {any} */ opts = {}) =>
  _request('DELETE', path, opts);

/** POST /api/auth/login */
export const login = (username, password) =>
  _post('/auth/login', { username, password });

/** POST /api/auth/register */
export const register = (payload) =>
  _post('/auth/register', payload);

/** POST /api/auth/forgot-password */
export const forgotPassword = (identifier) =>
  _post('/auth/forgot-password', { identifier });

/** POST /api/auth/reset-password */
export const resetPassword = (token, password) =>
  _post('/auth/reset-password', { token, password });

/** POST /api/auth/change-password — force-change after an admin-issued reset. */
export const changePassword = (password) =>
  _post('/auth/change-password', { password }, { auth: true });

/** GET /api/auth/me/nav — operator's NAV slice (share_pct × firm NAV). */
export const fetchMyNav = () => _get('/auth/me/nav', { auth: true });

/** GET /api/auth/whoami — role + capability bootstrap. No auth required;
 *  anonymous demo sessions get back `{ role: 'demo', caps: [...] }`. */
export const fetchWhoami = () => _get('/auth/whoami', { auth: _hasToken() });

/** GET /api/auth/firm-nav — public unauthenticated firm-aggregate NAV.
 *  Used by NavCard on /performance for anonymous visitors so they see
 *  the live firm NAV without signing in. Backend caches at 30 s. */
export const fetchFirmNavPublic = () => _get('/auth/firm-nav', { auth: false });

/** POST /api/auth/impersonate/{username} — start a support session as
 *  the target user. Returns a LoginResponse-shaped object with a fresh
 *  30-min JWT carrying imp_by=actor. Caller should hand the response
 *  to authStore.startImpersonation. */
export const impersonateUser = (username) =>
  _post(`/auth/impersonate/${encodeURIComponent(username)}`, null, { auth: true });

/** POST /api/auth/stop-impersonate — end the current support session.
 *  Returns a LoginResponse for the ORIGINAL actor. Caller hands the
 *  response to authStore.stopImpersonation. */
export const stopImpersonation = () =>
  _post('/auth/stop-impersonate', null, { auth: true });

// ── Public data endpoints (read-only — no JWT required) ──────────────────────
// Pass auth header if available — backend masks accounts for non-admin
// Pass `fresh=true` to make the server bypass its 30-second cache and
// pull a live broker snapshot. The Refresh button uses it; page mount
// + WebSocket-driven auto-refresh rely on the cached value.
/**
 * Build query string for the three book endpoints.
 * `skipLtp` (per-exchange close-snapshot lifecycle, Jul 2026) forces the
 * daily_book snapshot path even when a segment is open. RefreshButton
 * passes this during both-markets-closed clicks so cash/margins/holdings
 * refresh from the broker while LTPs stay frozen at the snapshot value.
 */
function _bookQs(fresh, skipLtp) {
  const q = [];
  if (fresh)   q.push('fresh=1');
  if (skipLtp) q.push('skip_ltp=1');
  return q.length ? `?${q.join('&')}` : '';
}
export const fetchHoldings  = ({ fresh = false, skipLtp = false } = {}) =>
  _get(`/holdings/${_bookQs(fresh, skipLtp)}`, { auth: _hasToken() });
export const fetchPositions = ({ fresh = false, skipLtp = false } = {}) =>
  _get(`/positions/${_bookQs(fresh, skipLtp)}`, { auth: _hasToken() });
export const fetchFunds     = ({ fresh = false, skipLtp = false } = {}) =>
  _get(`/funds/${_bookQs(fresh, skipLtp)}`, { auth: _hasToken() });

// ── Protected endpoints (require JWT — order mutations) ───────────────────────
export const fetchOrders    = () => _get('/orders/',    { auth: true });
// auth: optional — demo sessions (no JWT) can now reach /api/accounts/ via
// auth_or_demo_guard and receive masked account codes. Passing the token when
// present ensures admin callers get unmasked codes; anonymous viewers get
// D1####/ZG####/etc. so the account picker is populated on demo pages.
export const fetchAccounts  = () => _get('/accounts/', { auth: _hasToken() });

// ── Public endpoints (no JWT needed) ─────────────────────────────────────────
export const fetchMarket = () => _get('/market/');
export const fetchNews   = () => _get('/news/');
export const fetchAbout  = () => _get('/config/about');

// ── Agent endpoints (admin) ───────────────────────────────────────────────────
export const fetchAgents      = () => _get('/agents/', { auth: true });

// ── Grammar tokens (admin) ────────────────────────────────────────────────────
// The Agent-grammar catalog — condition / notify / action tokens backing every
// agent. System tokens are toggle-only; custom tokens support full CRUD.
export const fetchGrammarTokens = (grammar) =>
  _get(`/admin/grammar/tokens${grammar ? `?grammar=${encodeURIComponent(grammar)}` : ''}`,
       { auth: true });
export const patchGrammarToken  = (id, payload) =>
  _patch(`/admin/grammar/tokens/${id}`, payload, { auth: true });
export const createGrammarToken = (payload) =>
  _post('/admin/grammar/tokens', payload, { auth: true });
export const deleteGrammarToken = (id) =>
  _del(`/admin/grammar/tokens/${id}`, { auth: true });
export const reloadGrammarRegistry = () => _post('/admin/grammar/reload', {}, { auth: true });

// ── Agent templates — reusable notify / condition saved sub-trees ────
// System templates toggle-only; custom support full CRUD. URL kept at
// /admin/fragments for back-compat; underlying model + module renamed
// to AgentTemplate / template_registry in v2.1.
export const fetchAgentTemplates = (kind) =>
  _get(`/admin/fragments/${kind ? `?kind=${encodeURIComponent(kind)}` : ''}`,
       { auth: true });
export const createAgentTemplate = (payload) =>
  _post('/admin/fragments/', payload, { auth: true });
export const patchAgentTemplate  = (id, payload) =>
  _patch(`/admin/fragments/${id}`, payload, { auth: true });
export const deleteAgentTemplate = (id) =>
  _del(`/admin/fragments/${id}`, { auth: true });
export const reloadAgentTemplates = () =>
  _post('/admin/fragments/reload', {}, { auth: true });
// Pre-v2.1 names kept as aliases — remove in v2.2.
export const fetchAgentFragments = fetchAgentTemplates;
export const createAgentFragment = createAgentTemplate;
export const patchAgentFragment  = patchAgentTemplate;
export const deleteAgentFragment = deleteAgentTemplate;
export const reloadFragments     = reloadAgentTemplates;

// ── Order templates — TP/SL/Wing exit-rule presets attached at OrderTicket
// submit time. System rows are toggle + tune; custom rows full CRUD.
export const fetchOrderTemplates = () =>
  _get('/admin/templates/', { auth: true });
export const fetchOrderTemplate  = (id) =>
  _get(`/admin/templates/${id}`, { auth: true });
export const createOrderTemplate = (payload) =>
  _post('/admin/templates/', payload, { auth: true });
export const patchOrderTemplate  = (id, payload) =>
  _patch(`/admin/templates/${id}`, payload, { auth: true });
export const deleteOrderTemplate = (id) =>
  _del(`/admin/templates/${id}`, { auth: true });

// Pre-submit preview: returns the TemplatePlan that would be applied if
// this exact payload were submitted. Used by OrderTicket to show
// "Will place TP @ ₹X · SL @ ₹Y · Wing -500CE" inline.
export const previewTicketTemplate = (payload) =>
  _post('/orders/ticket/preview', payload, { auth: true });

// ── Hedge proxies (admin) ───────────────────────────────────────────────
// Cross-reference table for proxy hedges (GOLDBEES → GOLD etc.). Backs
// the /admin/derivatives Underlying picker's proxy-aware Tier 4 + the
// proxy-eq leg math. Stage 2 lifted out of the frontend static const.
export const fetchHedgeProxies   = () => _get('/admin/hedge-proxies/', { auth: true });

/** Strategies (slice 6) — list / detail / mutate. Demo can READ. */
export const fetchStrategies      = ({ activeOnly = false } = {}) =>
  _get(`/strategies/${activeOnly ? '?active_only=1' : ''}`, { auth: _hasToken() });
export const fetchStrategy        = (id) =>
  _get(`/strategies/${id}`, { auth: _hasToken() });
export const createStrategy       = (payload) =>
  _post('/strategies/', payload, { auth: true });
export const updateStrategy       = (id, payload) =>
  _patch(`/strategies/${id}`, payload, { auth: true });
export const deleteStrategy       = (id) =>
  _del(`/strategies/${id}`, { auth: true });
export const fetchStrategyLots    = (id, { includeClosed = true, limit = 500 } = {}) => {
  const q = new URLSearchParams();
  if (!includeClosed) q.set('include_closed', '0');
  if (limit) q.set('limit', String(limit));
  return _get(`/strategies/${id}/lots${q.toString() ? '?' + q.toString() : ''}`,
              { auth: _hasToken() });
};
export const fetchStrategySnapshots = (id, { days = 90 } = {}) =>
  _get(`/strategies/${id}/snapshots?days=${Number(days) || 90}`,
       { auth: _hasToken() });
export const fetchStrategyMetrics = (id, { days = 90 } = {}) =>
  _get(`/strategies/${id}/metrics?days=${Number(days) || 90}`,
       { auth: _hasToken() });

/** Firm NAV (slice 7j) — daily aggregate.
 *  Pass `signal` to cancel a pending fetch (e.g. component unmount or
 *  a caller-managed AbortController timeout). */
export const fetchNavHistory = ({ days = 90, signal = undefined } = {}) =>
  _get(`/nav/?days=${Number(days) || 90}`, { auth: _hasToken(), signal });
export const fetchNavLatest  = () =>
  _get('/nav/latest', { auth: _hasToken() });
export const triggerNavCompute = () =>
  _post('/nav/compute', {}, { auth: true });
/** Per-investor NAV slice (slice 7k). Requires authenticated user. */
export const fetchMyNavSlice    = () =>
  _get('/nav/me',         { auth: true });
export const fetchMyNavHistory  = ({ days = 90 } = {}) =>
  _get(`/nav/me/history?days=${Number(days) || 90}`, { auth: true });

/** Investor portal admin (slice 7L) — token mint/revoke per LP.
 *  Returned token from mint is shown ONCE; subsequent list calls
 *  surface only a preview. Mirrors MCP token mint UX. */
export const fetchInvestorTokens = (/** @type {number} */ userId) =>
  _get(`/admin/users/${userId}/investor-tokens`, { auth: true });
export const mintInvestorToken   = (/** @type {number} */ userId,
                                    /** @type {{expires_in_days?:number, note?:string}} */ body = {}) =>
  _post(`/admin/users/${userId}/investor-tokens`, body, { auth: true });
export const revokeInvestorToken = (/** @type {number} */ userId, /** @type {number} */ tokenId) =>
  _del(`/admin/users/${userId}/investor-tokens/${tokenId}`, { auth: true });

/** Monthly statement audit (slice 7M continuation). */
export const fetchStatementAudit = ({ year = 0, month = 0 } = {}) => {
  const q = new URLSearchParams();
  if (year)  q.set('year',  String(year));
  if (month) q.set('month', String(month));
  const tail = q.toString() ? `?${q}` : '';
  return _get(`/admin/statements/${tail}`, { auth: true });
};
export const sendStatementNow = (/** @type {{user_id:number, year:number, month:number}} */ body) =>
  _post('/admin/statements/send', body, { auth: true });
export const deleteStatementRow = (/** @type {number} */ rowId) =>
  _del(`/admin/statements/${rowId}`, { auth: true });

/** Investor events — subscription / redemption / bootstrap journal
 *  (slice 7N). Passive log today; the next slice consumes these
 *  events for units-based NAV math. */
export const fetchInvestorEvents = (/** @type {number} */ userId) =>
  _get(`/admin/users/${userId}/investor-events`, { auth: true });
export const createInvestorEvent = (/** @type {number} */ userId,
                                    /** @type {{event_type:string, event_date:string, amount:number, nav_per_unit:number, note?:string}} */ body) =>
  _post(`/admin/users/${userId}/investor-events`, body, { auth: true });
export const deleteInvestorEvent = (/** @type {number} */ userId, /** @type {number} */ eventId) =>
  _del(`/admin/users/${userId}/investor-events/${eventId}`, { auth: true });

/** History — multi-day forensic surface (orders / trades / funds).
 *  Gated by view_audit cap (admin / risk / ops). */
const _toQs = (params) => {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') q.set(k, String(v));
  }
  const qs = q.toString();
  return qs ? '?' + qs : '';
};
export const fetchHistoryOrders = (params = {}) =>
  _get(`/admin/history/orders${_toQs(params)}`, { auth: true });
export const fetchHistoryTrades = (params = {}) =>
  _get(`/admin/history/trades${_toQs(params)}`, { auth: true });
export const fetchHistoryFunds  = (params = {}) =>
  _get(`/admin/history/funds${_toQs(params)}`,  { auth: true });
export const backfillHistoryFunds = (/** @type {{account:string, from_date:string, to_date:string}} */ body) =>
  _post('/admin/history/funds/backfill', body, { auth: true });

/** GET /api/admin/audit — paginated audit log with filters. Gated
 *  by the `view_audit` cap server-side (admin / risk / ops). */
export const fetchAuditLog = (params = {}) => {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') q.set(k, String(v));
  }
  const qs = q.toString();
  return _get(`/admin/audit/${qs ? '?' + qs : ''}`, { auth: true });
};
export const createHedgeProxy    = (payload) =>
  _post('/admin/hedge-proxies/', payload, { auth: true });
export const updateHedgeProxy    = (id, payload) =>
  _patch(`/admin/hedge-proxies/${id}`, payload, { auth: true });
export const deleteHedgeProxy    = (id) =>
  _del(`/admin/hedge-proxies/${id}`, { auth: true });
// Stage 3 — run a 60-day daily-returns regression for the pair on the
// server and write β + R² back to the row.
export const computeHedgeProxy   = (id) =>
  _post(`/admin/hedge-proxies/${id}/compute`, {}, { auth: true });

// ── Code metrics (admin) ─────────────────────────────────────────────────
// Per-release snapshot history for `/admin/metrics`. Read-only — rows
// are produced by `scripts/capture_metrics.py`, never via HTTP.
export const fetchCodeMetricsList = (limit = 50, offset = 0) =>
  _get(`/admin/code-metrics/?limit=${limit}&offset=${offset}`, { auth: true });
export const fetchCodeMetricsDetail = (releaseTag) =>
  _get(`/admin/code-metrics/${encodeURIComponent(releaseTag)}`, { auth: true });
export const fetchCodeMetricsTrend = (metric, limit = 50) =>
  _get(`/admin/code-metrics/trends?metric=${encodeURIComponent(metric)}&limit=${limit}`, { auth: true });

// ── Perf snapshots (admin) ──────────────────────────────────────────────
/** GET /api/admin/perf/history?page=<name>&days=<N>
 *  Returns { page_or_route, rows: [{captured_at, loc, cc_max, ...}] }
 */
export const fetchPerfHistory = (page, days = 30) =>
  _get(`/admin/perf/history?page=${encodeURIComponent(page)}&days=${days}`, { auth: true });
/** GET /api/admin/perf/latest — one row per (side, page_or_route). */
export const fetchPerfLatest = () =>
  _get('/admin/perf/latest', { auth: true });
/** GET /api/admin/perf/regressions?days=7&threshold_pct=10 */
export const fetchPerfRegressions = (days = 7, threshold_pct = 10) =>
  _get(`/admin/perf/regressions?days=${days}&threshold_pct=${threshold_pct}`, { auth: true });

// ── Settings (admin) ────────────────────────────────────────────────────
export const fetchSettings     = () => _get('/admin/settings/', { auth: true });
export const updateSetting     = (key, value) =>
  _patch(`/admin/settings/${encodeURIComponent(key)}`,
         { value: String(value) },
         { auth: true });
export const resetSetting      = (key) =>
  _post(`/admin/settings/${encodeURIComponent(key)}/reset`, {}, { auth: true });
export const fetchAgentEvents = (slug, n = 50) => _get(`/agents/${slug}/events?n=${n}`, { auth: true });
export const fetchRecentAgentEvents = (n = 100) => _get(`/agents/events/recent?n=${n}`, { auth: true });
export const createAgent      = (payload) => _post('/agents/', payload, { auth: true });

// Dry-validate a condition tree against the grammar registry. Returns
// { ok: bool, errors: string[], grammar: 'v2' }.
export const validateAgentCondition = (condTree) =>
  _post('/agents/validate-condition', condTree, { auth: true });

// Draft an agent JSON from a natural-language prompt — the operator
// reviews the resulting draft + warnings before saving. Returns
// { draft, errors[], warnings[], why_summary }.
export const aiDraftAgent = (prompt) =>
  _post('/agents/ai-draft', { prompt }, { auth: true });

// ── Market simulator control plane (/api/simulator/*) ─────────────────
// Gated by cap_in_<branch>.simulator in backend_config.yaml. Default:
// dev on, prod off. Server returns 400 when the flag is off.
export const fetchSimScenarios    = () => _get('/simulator/scenarios', { auth: true });
export const fetchSimStatus       = () => _get('/simulator/status', { auth: true });
// `opts` may include:
//   seed_mode: 'scripted' | 'live' | 'live+scenario'
//   agent_ids: number[]   (restrict isolation to these agents)
//   positions_every_n_ticks: number | null
//     — positions cadence override; null = fall back to scenario YAML or
//       the DB setting `simulator.positions_every_n_ticks`.
//   market_state_preset: one of pre_open | at_open | mid_session |
//       pre_close | at_close | post_close | expiry_day, or null to use
//       the scenario's YAML value.
//   pct_overrides: array of per-tick decimal pct values (0.05 = 5%).
//       Replaces each pct-typed move's `value` in that tick; null entries
//       keep the scenario YAML default.
//   symbols: array of tradingsymbols to restrict the sim to. After
//       seeding, positions whose symbol isn't in this list are dropped.
//       Empty / null = all positions.
export const startSim             = (scenario, rate_ms = 2000, opts = {}) =>
  _post('/simulator/start',
        { scenario, rate_ms,
          seed_mode:               opts.seed_mode || 'scripted',
          agent_ids:               opts.agent_ids || null,
          positions_every_n_ticks: opts.positions_every_n_ticks ?? null,
          market_state_preset:     opts.market_state_preset || null,
          pct_overrides:           opts.pct_overrides           || null,
          symbols:                 opts.symbols                 || null,
          spread_pct:              opts.spread_pct              ?? null,
          custom_positions:        opts.custom_positions        || null,
          record_mode:             !!opts.record_mode,
          recording_label:         opts.recording_label || null },
        { auth: true });
export const stopSim              = () => _post('/simulator/stop', {}, { auth: true });
export const stepSim              = () => _post('/simulator/step', {}, { auth: true });
export const runSimCycle          = () => _post('/simulator/run-cycle', {}, { auth: true });
export const clearSimArtefacts    = () => _post('/simulator/clear', {}, { auth: true });
export const seedSimLive          = () => _post('/simulator/seed-live', {}, { auth: true });

// Sim recordings — deterministic event log per run. SimReplayDriver
// (Phase 2c) consumes a recording row and re-emits the events into
// the same SimDriver display buffers so the operator's screen during
// playback is identical to the original run.
export const fetchSimRecordings   = (limit = 50) =>
  _get(`/simulator/recordings?limit=${limit}`, { auth: true });
export const fetchSimRecording    = (id) =>
  _get(`/simulator/recordings/${id}`, { auth: true });
export const deleteSimRecording   = (id) =>
  _del(`/simulator/recordings/${id}`, { auth: true });
// Sim-recording replay controls (distinct from /replay/* below which
// drives the historical-OHLCV Backtest tab).
export const fetchSimReplayStatus = () =>
  _get('/simulator/replay/status', { auth: true });
export const startSimReplay       = (recordingId, speed = 1.0) =>
  _post('/simulator/replay/start', { recording_id: recordingId, speed }, { auth: true });
export const stopSimReplay        = () =>
  _post('/simulator/replay/stop', {}, { auth: true });
export const pauseSimReplay       = () =>
  _post('/simulator/replay/pause', {}, { auth: true });
export const resumeSimReplay      = () =>
  _post('/simulator/replay/resume', {}, { auth: true });
export const stepSimReplay        = () =>
  _post('/simulator/replay/step', {}, { auth: true });

// Sim GTT book — place / list / cancel template-driven triggers inside
// the active sim. The orchestrator path (OrderTicket template fan-out)
// reaches this via the same endpoints used by integration tests.
export const fetchSimGtts         = () => _get('/simulator/gtt', { auth: true });
export const placeSimGtt          = (payload) =>
  _post('/simulator/gtt', payload, { auth: true });
export const cancelSimGtt         = (gttId) =>
  _del(`/simulator/gtt/${encodeURIComponent(gttId)}`, { auth: true });

// Agent-generated orders from the algo_orders table. Returns live + sim
// by default; pass `mode='live'` or `'sim'` to scope. Used by the Order
// tab of the LogPanel on /agents and /admin/simulator.
export const fetchAlgoOrdersRecent = (n = 100, mode = 'all') =>
  _get(`/orders/algo/recent?n=${n}&mode=${mode}`, { auth: true });

/** GET /api/orders/chases/active — every algo_orders row currently in
 *  OPEN state across paper / live / shadow modes. Used by the Chase
 *  card on /orders to surface in-flight chases the operator can kill.
 */
export const fetchActiveChases = () =>
  _get('/orders/chases/active', { auth: true });

/** POST /api/orders/chases/{id}/kill — best-effort cancel of an
 *  in-flight chase (paper: engine flip; live: broker.cancel_order).
 *  Admin-only. */
export const killChase = (algoOrderId) =>
  _post(`/orders/chases/${algoOrderId}/kill`, {}, { auth: true });

/** POST /api/orders/algo/reconcile — admin sweep that syncs stale
 *  OPEN rows against the broker. Returns {scanned, updated, missing}. */
export const reconcileAlgoOrders = () =>
  _post('/orders/algo/reconcile', {}, { auth: true });
/** POST /api/orders/{algo_order_id}/retry-template — re-run
 *  apply_template_to_order against an already-filled parent. Useful
 *  when the initial attach failed silently (e.g. wing scan returned
 *  no candidate). Idempotent: bails when already attached / not
 *  filled / no template was set. */
export const retryTemplateAttach = (algoOrderId) =>
  _post(`/orders/${algoOrderId}/retry-template`, {}, { auth: true });
/** POST /api/orders/{broker_order_id}/reconcile — re-sync ONE order
 *  against the broker. Body `{account}` tells the route which Kite
 *  handle to query. Returns
 *  {broker_order_id, broker_status, algo_status, updated, note}. */
export const reconcileSingleOrder = (brokerOrderId, account) =>
  _post(`/orders/${brokerOrderId}/reconcile`, { account }, { auth: true });
// Synthesize-and-start — scenario generated live from the agent's condition
// tree. Preferred over manually picking a scenario when the goal is "test
// this specific agent."
export const startSimForAgent     = (agentId, rate_ms = 2000) =>
  _post(`/simulator/start-for-agent/${agentId}?rate_ms=${rate_ms}`, {}, { auth: true });
export const fetchSimEvents       = (n = 50) => _get(`/simulator/events/recent?limit=${n}`, { auth: true });
export const fetchSimOrders       = (n = 50) => _get(`/simulator/orders/recent?limit=${n}`, { auth: true });
export const fetchSimTicks        = (n = 100) => _get(`/simulator/ticks/recent?limit=${n}`, { auth: true });

// ── Simulator iteration framework (Phase 1 endpoints) ─────────────────
// `/start-run` runs N iterations sequentially with optional regime
// round-robin + seed derivation. Each iteration's slug + summary is
// persisted to `sim_iterations`. The form on /admin/simulator iteration
// tab pre-fills from `/defaults`, validates, and submits to /start-run.
export const fetchSimDefaults     = () =>
  _get('/simulator/defaults', { auth: true });

/**
 * @param {{
 *   iterations:               number,
 *   max_minutes:              number,
 *   regimes:                  string[],
 *   agent_ids?:               number[] | null,
 *   seed?:                    number | null,
 *   force_close_on_timeout?:  boolean,
 *   seed_mode?:               string,
 *   rate_ms?:                 number | null,
 *   spread_pct?:              number | null,
 *   custom_positions?:        any[] | null,
 * }} payload
 */
export const startSimRun          = (payload) =>
  _post('/simulator/start-run', payload, { auth: true });

export const fetchSimIterations   = (run_id, limit = 50) => {
  const q = new URLSearchParams();
  if (run_id != null) q.set('run_id', String(run_id));
  if (limit)          q.set('limit', String(limit));
  const qs = q.toString();
  return _get(`/simulator/iterations${qs ? '?' + qs : ''}`, { auth: true });
};

export const fetchSimIteration    = (slug) =>
  _get(`/simulator/iterations/${encodeURIComponent(slug)}`, { auth: true });

export const replaySimIteration   = (slug) =>
  _post(`/simulator/iterations/${encodeURIComponent(slug)}/replay`, {}, { auth: true });

export const updateAgent     = (slug, payload) => _put(`/agents/${slug}`, payload, { auth: true });
export const activateAgent   = (slug) => _put(`/agents/${slug}/activate`, undefined, { auth: true });
export const deactivateAgent = (slug) => _put(`/agents/${slug}/deactivate`, undefined, { auth: true });
export const deleteAgent     = (slug) => _del(`/agents/${slug}`, { auth: true });
export const interpretAgent  = (command) => _post('/agents/interpret', { command }, { auth: true });

// ── Order mutations (protected) ───────────────────────────────────────────────
// Note: `placeOrder` (POST /orders/place) was retired during the Phase 2/4
// unification — every order surface now opens the shared OrderTicket and
// submits via `placeTicketOrder` → /api/orders/ticket. The backend endpoint
// still exists for any external scripts that may be hitting it directly,
// but no frontend code path calls it.
export const modifyOrder = (orderId, payload) => _put(`/orders/${orderId}`, payload, { auth: true });

// ── Admin endpoints (require admin JWT) ──────────────────────────────────────
export const fetchUsers = () => _get('/admin/users', { auth: true });

// ── Watchlist (any logged-in user) ───────────────────────────────────────────
export const fetchWatchlists      = ()      => _get('/watchlist/', { auth: true });
export const fetchWatchlist       = (id)    => _get(`/watchlist/${id}`, { auth: true });
export const createWatchlist      = (name)  => _post('/watchlist/', { name }, { auth: true });
export const renameWatchlist      = (id, payload) =>
  _patch(`/watchlist/${id}`, payload, { auth: true });
export const deleteWatchlist      = (id)    => _del(`/watchlist/${id}`, { auth: true });
export const addWatchlistItem     = (id, tradingsymbol, exchange, alias = null) =>
  _post(`/watchlist/${id}/items`,
        { tradingsymbol, exchange, ...(alias ? { alias } : {}) },
        { auth: true });
export const removeWatchlistItem  = (wlId, itemId) =>
  _del(`/watchlist/${wlId}/items/${itemId}`, { auth: true });
export const reorderWatchlistItem = (wlId, itemId, sortOrder) =>
  _patch(`/watchlist/${wlId}/items/${itemId}`, { sort_order: sortOrder }, { auth: true });
/** Rename / set alias on a watchlist item.
 *  alias='' clears the alias; non-empty sets it. */
export const renameWatchlistItem  = (wlId, itemId, alias) =>
  _patch(`/watchlist/${wlId}/items/${itemId}`, { alias }, { auth: true });
export const fetchWatchlistQuotes = (id)    => _get(`/watchlist/${id}/quotes`, { auth: true });
export const fetchMovers          = ()      => _get('/watchlist/movers', { auth: true });
/** POST /api/quote/batch — fetch LTP/bid/ask/day-change for arbitrary keys. */
export const batchQuote           = (keys)  => _post('/quote/batch', { keys }, { auth: _hasToken() });

/** POST /api/quotes/sparkline
 *  symbols: [{tradingsymbol, exchange}, …]
 *  days: number of daily closes to return (default 5) */
export const fetchSparklines = (symbols, days = 5) =>
  _post('/quotes/sparkline', { symbols, days }, { auth: _hasToken() });
export const createUser = (payload) => _post('/admin/users', payload, { auth: true });

export const approveUser    = (username) => _put(`/admin/users/${username}/approve`,   undefined, { auth: true });
export const rejectUser     = (username) => _put(`/admin/users/${username}/reject`,    undefined, { auth: true });
export const updateUser     = (username, payload) => _put(`/admin/users/${username}`, payload, { auth: true });
export const suspendUser    = (username) => _put(`/admin/users/${username}/suspend`,   undefined,           { auth: true });
export const reinstateUser  = (username) => _put(`/admin/users/${username}/reinstate`, undefined,           { auth: true });
export const terminateUser  = (username) => _put(`/admin/users/${username}/terminate`, undefined,           { auth: true });
export const toggleDesignated = (username, makeDesignated) =>
  _put(`/admin/users/${username}/toggle-designated`, { designated: !!makeDesignated }, { auth: true });
export const adminResetPassword = (username, password) =>
  _put(`/admin/users/${username}/reset-password`,  { password },                        { auth: true });
export const resendVerification = (username) =>
  _post(`/admin/users/${username}/resend-verification`, {},                              { auth: true });
export const markVerified      = (username) =>
  _put(`/admin/users/${username}`, { email_verified: true },                              { auth: true });

export const cancelOrder = (orderId, account, variety = 'regular') => {
  const params = new URLSearchParams({ account, variety });
  return _del(`/orders/${orderId}?${params}`, { auth: true });
};

// ── Charts (admin-guarded) ────────────────────────────────────────────────────

/** GET /api/charts/symbols?mode=… — list symbols with captured ticks. */
export async function fetchChartSymbols(mode) {
  return _get(`/charts/symbols?mode=${encodeURIComponent(mode)}`, { auth: true });
}

/** GET /api/charts/price-history — ticks + AlgoOrder lifecycle markers. */
export async function fetchChartPriceHistory(mode, symbol, since = null, limit = 600) {
  const p = new URLSearchParams({ mode, symbol, limit: String(limit) });
  if (since) p.set('since', since);
  return _get(`/charts/price-history?${p}`, { auth: true });
}

/** GET /api/charts/paper-status — prod paper engine snapshot for /admin/paper. */
export async function fetchPaperStatus() {
  return _get('/charts/paper-status', { auth: true });
}

/** GET /api/instruments/ — Kite master instrument dump.
 *  Returns { cycle_date, count, items }. Called by instruments.js on a
 *  cache miss; result is persisted to IndexedDB so this fires at most
 *  once per trading day per browser. Token is optional — the endpoint
 *  is accessible to anonymous demo sessions. */
export async function fetchInstruments() {
  return _get('/instruments/', { auth: _hasToken() });
}

/** GET /api/quote — single-symbol quote with top-5 depth. Used by
 *  OrderTicket / OrderDepth to render the bid/ask ladder while the
 *  ticket is open. Polls every ~1 s for live depth. */
export async function fetchQuote(exchange, tradingsymbol) {
  const p = new URLSearchParams({ exchange, tradingsymbol });
  return _get(`/quote/?${p}`, { auth: true });
}

/** POST /api/orders/ticket — operator-initiated order from the
 *  reusable <OrderTicket>. Phase 2: only mode='paper' is wired —
 *  routes through the prod paper engine. mode='live' returns 501
 *  until phase 3. mode='draft' is client-side, never reaches here. */
export async function placeTicketOrder(payload) {
  return _post('/orders/ticket', payload, { auth: true });
}

/** POST /api/orders/preflight — pre-submit cost/margin estimate. Reuses
 *  the live-order safety preflight to compute basket_margin (required)
 *  + available_margin via Kite. OrderTicket calls this on field change
 *  (debounced) to show "MARGIN ₹X" / "AVAILABLE ₹Y" inline above Submit.
 *
 *  Returns `{ok, blocked[], diagnostics:{basket_margin_used,
 *  available_margin, margin_shortfall}}`. Never throws on Kite errors —
 *  returns the structured response and the caller handles empty fields. */
export async function previewOrderMargin(payload) {
  return _post('/orders/preflight', payload, { auth: true });
}

/** POST /api/orders/basket — place all legs in one atomic backend call.
 *  Request:  { groups: [{ account, legs: [...] }] }
 *  Response: { groups: [{ account, basket_id, results: [...], margin_required, margin_available }] } */
export async function placeBasket(groups) {
  return _post('/orders/basket', { groups }, { auth: true });
}

/** POST /api/orders/basket/margin — per-account margin check without placing.
 *  Same shape as placeBasket request.
 *  Response: { groups: [{ account, required, available, shortfall, error }] }
 *
 *  In-memory 1.5 s cache keyed by JSON-stringified payload so rapid
 *  picker changes don't fire redundant round-trips. */
const _basketMarginCache = new Map();
export async function fetchBasketMargin(groups) {
  const key = JSON.stringify(groups);
  const cached = _basketMarginCache.get(key);
  if (cached && Date.now() - cached.ts < 1500) return cached.data;
  const data = await _post('/orders/basket/margin', { groups }, { auth: true });
  _basketMarginCache.set(key, { ts: Date.now(), data });
  return data;
}

/** GET /api/charts/intraday-equity?n=… — rolling intraday P&L buffer.
 *  Returns `{points: [{ts, day_pnl, cum_pnl}]}`. One point per ~5 min
 *  tick during market hours; wiped on IST date rollover. Admin-guarded. */
export const fetchIntradayEquity = (n = 200) =>
  _get(`/charts/intraday-equity?n=${n}`, { auth: true });

/** GET /api/charts/batch — N charts in one round-trip. Returns
 *  `{mode, charts: [ChartResponse, …]}` in the order of `symbols`. */
export async function fetchChartBatch(mode, symbols, since = null, limit = 600) {
  if (!symbols?.length) return { mode, charts: [] };
  const p = new URLSearchParams({
    mode,
    symbols: symbols.join(','),
    limit: String(limit),
  });
  if (since) p.set('since', since);
  return _get(`/charts/batch?${p}`, { auth: true });
}

// ── Broker accounts (admin CRUD) ─────────────────────────────────────

/** GET /api/admin/broker-health — per-account auth/freshness state for the
 *  broker health badge in the navbar. Returns green/amber/red per account
 *  derived from _FETCH_HEALTH / fetch_health_snapshot() tracking. */
export const fetchBrokerHealth = () => _get('/admin/broker-health', { auth: true });

/** GET /api/admin/broker-connection-events — paginated broker connection audit log.
 *  @param {{ account?: string, event_type?: string, since?: string, limit?: number }} params */
export const fetchBrokerConnectionEvents = (params = {}) => {
  const qs = new URLSearchParams();
  if (params.account)    qs.set('account',    params.account);
  if (params.event_type) qs.set('event_type', params.event_type);
  if (params.since)      qs.set('since',      params.since);
  qs.set('limit', String(params.limit ?? 200));
  return _get(`/admin/broker-connection-events?${qs}`, { auth: true });
};

/** GET /api/admin/brokers/order — {account_id: display_order} map for
 *  canonical UI ordering (accountSort.js / accountDisplayOrder store). */
export const fetchBrokerOrder = () => _get('/admin/brokers/order', { auth: true });

/** GET /api/admin/brokers — list every broker account (no secrets). */
export const fetchBrokerAccounts = () => _get('/admin/brokers', { auth: true });

/** GET /api/admin/brokers/{account} — single account metadata. */
export const fetchBrokerAccount = (acct) =>
  _get(`/admin/brokers/${encodeURIComponent(acct)}`, { auth: true });

/** GET /api/admin/brokers/{account}/capabilities — Sprint C: broker
 *  capability matrix (gtt_single / gtt_oco / gtt_modify / display_name
 *  / etc) so OrderTicket can render inline warnings on attach. Pure
 *  read of a dataclass; no broker round-trip. */
export const fetchBrokerCapabilities = (acct) =>
  _get(`/admin/brokers/${encodeURIComponent(acct)}/capabilities`, { auth: true });

/** POST /api/admin/brokers — create a new account. */
export async function createBrokerAccount(payload) {
  return _post('/admin/brokers', payload, { auth: true });
}

/** PATCH /api/admin/brokers/{account} — partial update. Empty secrets
 *  fields mean "leave unchanged" so the operator can edit one credential
 *  without re-typing the rest. */
export const updateBrokerAccount = (acct, payload) =>
  _patch(`/admin/brokers/${encodeURIComponent(acct)}`, payload, { auth: true });

/** DELETE /api/admin/brokers/{account}. */
export const deleteBrokerAccount = (acct) =>
  _del(`/admin/brokers/${encodeURIComponent(acct)}`, { auth: true });

/** POST /api/admin/brokers/{account}/test — try profile() and report. */
export async function testBrokerAccount(acct) {
  return _post(`/admin/brokers/${encodeURIComponent(acct)}/test`, {}, { auth: true });
}

/** POST /api/admin/brokers/{account}/restore-priority — reset poll_priority
 *  to 'hot', clear auto-downgrade stamps, bump next_poll to now. */
export async function restoreBrokerPriority(acct) {
  return _post(`/admin/brokers/${encodeURIComponent(acct)}/restore-priority`, {}, { auth: true });
}


// ── Admin outbound email (admin/designated) ─────────────────────────────────

/** POST /api/admin/email-partners — blast a plain-text message to selected partners.
 *  Body: { recipients: string[] | 'all-partners' | 'all-designated' | 'all',
 *           subject: string, body: string }
 *  Returns: { sent_count, failed_count, total, event_id, failures[] } */
export const sendPartnerEmail = (body) =>
  _post('/admin/email-partners', body, { auth: true });

/** GET /api/admin/email-events?n=25 — recent outbound-email audit rows. */
export const fetchEmailEvents = (n = 25) =>
  _get(`/admin/email-events?n=${n}`, { auth: true });

/** POST /api/options/strategy-analytics — multi-leg aggregate analytics. */
export async function fetchStrategyAnalytics(legs, opts = {}) {
  return _post('/options/strategy-analytics',
    {
      legs: (legs || []).map(l => ({
        symbol:   String(l.symbol || '').trim().toUpperCase(),
        qty:      Number(l.qty),
        avg_cost: l.avg_cost == null || l.avg_cost === '' ? null : Number(l.avg_cost),
        ltp:      l.ltp      == null || l.ltp      === '' ? null : Number(l.ltp),
        iv:       l.iv       == null || l.iv       === '' ? null : Number(l.iv),
        // ISO expiry override from the instruments cache — wins over
        // the parser's last-Thursday inference. Critical for MCX
        // commodities (GOLDM expires on the 5th, CRUDEOIL on 19-20).
        expiry:   l.expiry   == null || l.expiry === '' ? null : String(l.expiry),
      })),
      spot:        opts.spot        ?? null,
      span_pct:    opts.span_pct    ?? null,
      points:      opts.points      ?? 51,
      // Default 0 slices — operator removed the T-33% / T-67%
      // intermediate curves from the payoff chart. The backend
      // helpers + IntermediateCurve schema stay in place; flip
      // this to 1-5 to re-enable the overlay.
      time_slices: opts.time_slices ?? 0,
    },
    { auth: true });
}

/** GET /api/options/spot — lightweight underlying spot for the chain
 *  picker's ATM highlight + auto-scroll. Returns spot, source tag,
 *  and yesterday's close. Falls through to a 502 when the broker is
 *  unreachable; callers swallow and skip the highlight. */
export async function fetchOptionsSpot(underlying, expiry = null) {
  const u = encodeURIComponent(String(underlying || '').toUpperCase());
  const e = expiry ? `&expiry=${encodeURIComponent(expiry)}` : '';
  return _get(`/options/spot?underlying=${u}${e}`, { auth: true });
}

/** GET /api/options/chain-quotes — per-strike CE + PE LTPs for the
 *  chain picker's grid. One round-trip drives every LTP cell on the
 *  page. Returns `{underlying, expiry, rows: [{k, ce_ltp, pe_ltp}]}`;
 *  empty rows when the broker is unreachable so callers render the
 *  grid without LTPs rather than failing. */
export async function fetchChainQuotes(underlying, expiry) {
  const u = encodeURIComponent(String(underlying || '').toUpperCase());
  const e = encodeURIComponent(String(expiry || ''));
  return _get(`/options/chain-quotes?underlying=${u}&expiry=${e}`,
              { auth: true });
}

/** GET /api/options/historical — Kite OHLCV candles for any symbol.
 *  Backs the SymbolChartModal so the operator can chart any symbol
 *  from any list. Returns `{symbol, instrument_token, interval,
 *  bars: [{ts, open, high, low, close, volume}]}`. Empty bars[] on
 *  cache miss / broker unreachable; never 5xx. */
/**
 * @param {string} symbol
 * @param {{days?: number, interval?: string, exchange?: string}} [opts]
 */
export async function fetchOptionsHistorical(symbol,
                                              { days = 30,
                                                interval = 'day',
                                                exchange = undefined } = {}) {
  const p = new URLSearchParams({
    symbol:   String(symbol),
    days:     String(days),
    interval: String(interval),
  });
  if (exchange) p.set('exchange', String(exchange));
  return _get(`/options/historical?${p}`, { auth: true });
}

// ── Replay / Backtest ─────────────────────────────────────────────────
export const fetchReplayStatus  = () => _get('/replay/status', { auth: true });
export const startReplay        = (payload) => _post('/replay/start', payload, { auth: true });
export const stopReplay         = () => _post('/replay/stop', {}, { auth: true });
export const fetchReplayResults = () => _get('/replay/results', { auth: true });
export const fetchReplayOrders  = (n = 50) => _get(`/replay/orders/recent?limit=${n}`, { auth: true });
export const clearReplayData    = () => _post('/replay/clear', {}, { auth: true });

// ── Live ──────────────────────────────────────────────────────────────
// Returns: { branch, enabled, paper_trading_mode, effective_mode, shadow_mode }
// paper_trading_mode: true = PAPER (safe default), false = LIVE (real orders).
// effective_mode: 'dev_paper' | 'paper' | 'shadow' | 'live' | 'mixed'
export const fetchLiveStatus    = () => _get('/live/status', { auth: true });

// ── Admin logs ────────────────────────────────────────────────────────
/** GET /api/admin/logs?n=… — tail the API log file. */
export const fetchAdminLogs = (n = 100) =>
  _get(`/admin/logs?n=${n}&_t=${Date.now()}`, { auth: true });

/** GET /api/admin/logs/conn?n=… — tail conn_service's log file
 * (KiteTicker, watchdog, broker rebuild events).
 *
 * Appends a per-call cache-buster (_t=Date.now()) so browser caches
 * NEVER hold a stale response. Operator: "conn log is not updated".
 * Same pattern is now applied to fetchAdminLogs for parity. */
export const fetchAdminConnLogs = (n = 100) =>
  _get(`/admin/logs/conn?n=${n}&_t=${Date.now()}`, { auth: true });

// ── Contact (public) ──────────────────────────────────────────────────
/** POST /api/contact/ — public contact form submission. */
export const submitContact = (payload) => _post('/contact/', payload);

// ── Admin: Alerts history ─────────────────────────────────────────────
/**
 * GET /api/admin/alerts/history — paginated agent-event history.
 * Params: { limit, agent_slug, since_minutes, event_type, sim_mode }
 * Returns { events: AgentEvent[] } or AgentEvent[] directly.
 */
export async function fetchAlertsHistory(params = {}) {
  const p = new URLSearchParams();
  if (params.limit)         p.set('limit',        String(params.limit));
  if (params.agent_slug)    p.set('agent_slug',   String(params.agent_slug));
  // since_minutes=0 must pass through (means "no time filter") — only
  // skip when the caller didn't supply the param at all.
  if (params.since_minutes != null) p.set('since_minutes', String(params.since_minutes));
  if (params.event_type)    p.set('event_type',   String(params.event_type));
  if (params.sim_mode != null) p.set('sim_mode',  String(params.sim_mode));
  return _get(`/admin/alerts/history?${p}`, { auth: true });
}

// ── Admin: System health ──────────────────────────────────────────────
/**
 * GET /api/admin/health — system diagnostics snapshot.
 * Returns { branch, git_hash, git_subject, uptime, brokers,
 *           db, cache, sim, paper }.
 */
export const fetchSystemHealth = () => _get('/admin/health', { auth: true });

// Persistence refresh-cycle mode (slice Z). Three states:
//   off  — normal cache → DB → broker hierarchy
//   soft — non-ticker stores bypass cache+DB; broker fetch + write-back
//   hard — soft + ticker recycle on transition (in-memory _tick_map rebuild)
export const fetchPersistenceMode = () =>
  _get('/admin/persistence/mode', { auth: true });
export const setPersistenceMode = (mode) =>
  _post(`/admin/persistence/mode/${mode}`, undefined, { auth: true });
export const invalidatePersistence = (store, opts = {}) => {
  const qs = new URLSearchParams({ store });
  if (opts.symbol)   qs.set('symbol',   opts.symbol);
  if (opts.exchange) qs.set('exchange', opts.exchange);
  return _post(`/admin/persistence/invalidate?${qs}`, undefined, { auth: true });
};

// ── Execution mode (admin) ────────────────────────────────────────────
/** GET /api/admin/execution/mode — returns { mode, branch, allowed_modes }. */
export const fetchExecutionMode = () => _get('/admin/execution/mode', { auth: true });

/** POST /api/admin/execution/mode — body { mode }. Returns 200 or 403. */
export const setExecutionMode = (mode) =>
  _post('/admin/execution/mode', { mode }, { auth: true });

// ── Order events (open orders for chase chip) ─────────────────────────
/** GET /api/orders/events/recent?limit=N&status=open — recent order events. */
export const fetchOrderEvents = (limit = 50, status = null) => {
  const p = new URLSearchParams({ limit: String(limit) });
  if (status) p.set('status', status);
  return _get(`/orders/events/recent?${p}`, { auth: true });
};


/**
 * POST /api/admin/pnl/upload-csv  (multipart/form-data)
 * Caller builds and passes the FormData directly — not routed through
 * _request because multipart requires no Content-Type header override.
 */
export async function uploadPnlCsv(formData) {
  const token = /** @type {any} */ (typeof sessionStorage !== 'undefined'
    ? sessionStorage.getItem('ramboq_token')
    : null);
  const res = await fetch(`${BASE}/admin/pnl/upload-csv`, {
    method: 'POST',
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    body: formData,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(_friendlyError(res.status, body?.detail ?? null));
  }
  return res.json();
}

// ── Unified log feed ──────────────────────────────────────────────────
/**
 * GET /api/logs/unified — merged order-event + agent-event stream.
 * filter: {
 *   kinds?:    string[],
 *   accounts?: string[],
 *   since?:    string,
 *   simMode?:  true | false | null   — true = sim-only, false = real-only,
 *                                       null/undefined = both (default)
 * }
 */
export const fetchUnifiedLog = (filter = {}, limit = 50) => {
  const p = new URLSearchParams({ limit: String(limit) });
  if (filter.kinds?.length)    p.set('kinds',    filter.kinds.join(','));
  if (filter.accounts?.length) p.set('accounts', filter.accounts.join(','));
  if (filter.since)            p.set('since',    filter.since);
  if (filter.simMode === true)  p.set('sim_mode', 'true');
  if (filter.simMode === false) p.set('sim_mode', 'false');
  return _get(`/logs/unified?${p}`, { auth: true });
};

// ── Admin: P&L benchmarks ────────────────────────────────────────────
/**
 * GET /api/admin/pnl/benchmarks
 * Params: { from_date, to_date, symbols }
 * symbols is a comma-separated list, e.g. "NIFTY 50,SENSEX"
 * Returns PnlBenchmarkResponse: { from_date, to_date, series: [...] }
 */
export function fetchPnlBenchmarks(params = {}) {
  const p = new URLSearchParams();
  if (params.from_date) p.set('from_date', String(params.from_date));
  if (params.to_date)   p.set('to_date',   String(params.to_date));
  if (params.symbols)   p.set('symbols',   String(params.symbols));
  return _get(`/admin/pnl/benchmarks?${p}`, { auth: true });
}

// ── Admin: Per-agent P&L attribution ─────────────────────────────────
/**
 * GET /api/admin/pnl/by-agent — P&L attribution grouped by agent.
 * Params: { period: 'today'|'week'|'month'|'all', mode: 'all'|'live'|'paper' }
 * Returns { agents: [{ agent_slug, agent_name, orders, filled,
 *                      gross_pnl, win_pct, avg_slippage }] }
 */
export async function fetchAgentPnL(params = {}) {
  const p = new URLSearchParams();
  if (params.period) p.set('period', String(params.period));
  if (params.mode)   p.set('mode',   String(params.mode));
  return _get(`/admin/pnl/by-agent?${p}`, { auth: true });
}

// ── Research threads (Lab page) ──────────────────────────────────────
/** GET /api/research/threads — list summaries. */
export const fetchResearchThreads = (symbol = null, limit = 100) => {
  const p = new URLSearchParams();
  if (symbol) p.set('symbol', String(symbol).toUpperCase());
  if (limit)  p.set('limit', String(limit));
  return _get(`/research/threads?${p}`, { auth: true });
};
/** GET /api/research/threads/{id} — full transcript + thesis. */
export const fetchResearchThread = (id) =>
  _get(`/research/threads/${id}`, { auth: true });
/** POST /api/research/threads — create a new thread. */
export const createResearchThread = (payload) =>
  _post('/research/threads', payload, { auth: true });
/** PATCH /api/research/threads/{id} — update title/thesis/transcript/draft_agent. */
export const updateResearchThread = (id, payload) =>
  _patch(`/research/threads/${id}`, payload, { auth: true });
/** DELETE /api/research/threads/{id} — remove. */
export const deleteResearchThread = (id) =>
  _del(`/research/threads/${id}`, { auth: true });
/** GET /api/research/drafts — threads with linked inactive agents (joined view). */
export const fetchResearchDrafts = (limit = 200) =>
  _get(`/research/drafts?limit=${limit}`, { auth: true });
/** POST /api/research/threads/{id}/promote — create an inactive draft agent. */
export const promoteResearchThread = (id, payload) =>
  _post(`/research/threads/${id}/promote`, payload, { auth: true });
/** POST /api/research/confirm-token — mint a 60s single-use token for one specific order. */
export const mintConfirmToken = (payload) =>
  _post('/research/confirm-token', payload, { auth: true });

/** GET /api/research/audit — forensic trail of MCP-initiated mutations. */
export const fetchResearchAudit = (filters = {}) => {
  const p = new URLSearchParams();
  if (filters.tool)       p.set('tool',       String(filters.tool));
  if (filters.status)     p.set('status',     String(filters.status));
  if (filters.since)      p.set('since',      String(filters.since));
  if (filters.request_id) p.set('request_id', String(filters.request_id));
  if (filters.limit)      p.set('limit',      String(filters.limit));
  return _get(`/research/audit?${p}`, { auth: true });
};
