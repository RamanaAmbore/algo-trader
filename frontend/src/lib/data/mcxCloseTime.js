/**
 * mcxCloseTime.js — DST-aware IST close time for MCX non-agri commodities
 * (bullion/energy/base metals — CRUDEOIL, NATURALGAS, GOLD, SILVER,
 * COPPER, etc.).
 *
 * MCX revises this close twice a year, tracking the US daylight-saving
 * calendar (confirmed via Zerodha/Upstox/ICICI Direct MCX trading-hours
 * bulletins):
 *   - US DST active             → 23:30 IST
 *   - US standard time (winter) → 23:55 IST
 *
 * This is the frontend mirror of the backend's
 * `mcx_non_agri_close_time()` (`backend/api/algo/derivatives.py`) and
 * MUST stay behaviourally identical to it — see that function's own
 * docstring and CLAUDE.md's "MCX DST-aware non-agri close time" entry
 * for the full incident writeup.
 *
 * Resolved via the REAL US Eastern-time DST transition through the
 * browser's own `Intl` timezone database — never a hand-rolled
 * "2nd Sunday of March / 1st Sunday of November" calculation, and never
 * a hardcoded date range or a hardcoded standard-offset constant — so
 * this stays correct in every future year with zero manual maintenance,
 * matching the backend's own `zoneinfo`-based design intent exactly.
 *
 * Deliberately a standalone module with no `$app/environment` import
 * (unlike `marketHours.js`, which this module feeds) so it can be
 * unit-tested directly under Vitest with no SvelteKit mocking needed.
 *
 * Pure utility — no module-level mutable state beyond the two small
 * memoization caches below, which only ever grow by one entry per
 * distinct calendar year / IST calendar date actually queried (at most
 * a few hundred entries over a browser tab's lifetime) and exist purely
 * so this can be called safely from tight render loops
 * (`isMcxOpen()`/`isMarketOpen()` are documented as staying synchronous
 * for exactly that reason).
 */

const _US_EASTERN_TZ = 'America/New_York';

/**
 * Minutes a given zone's wall clock is offset from UTC at `instant`,
 * e.g. -300 for America/New_York in EST (UTC-5), -240 in EDT (UTC-4).
 * Uses the standard "format in the target zone, re-interpret as UTC,
 * diff against the real UTC instant" technique — works with only
 * `Intl.DateTimeFormat.formatToParts()`, so it doesn't depend on the
 * newer (Safari 15.4+) `timeZoneName: 'shortOffset'/'longOffset'`
 * support some browsers still lack.
 */
function _tzOffsetMinutesAt(/** @type {Date} */ instant, /** @type {string} */ timeZone) {
  const dtf = new Intl.DateTimeFormat('en-US', {
    timeZone,
    hourCycle: 'h23',
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
  const parts = dtf.formatToParts(instant);
  /** @type {Record<string, string>} */
  const map = {};
  for (const p of parts) if (p.type !== 'literal') map[p.type] = p.value;
  const asUtcMs = Date.UTC(
    Number(map.year), Number(map.month) - 1, Number(map.day),
    Number(map.hour), Number(map.minute), Number(map.second),
  );
  return (asUtcMs - instant.getTime()) / 60000;
}

/** @type {Map<number, number>} */
const _standardOffsetByYearCache = new Map();

/**
 * The zone's standard-time (winter, non-DST) UTC offset in minutes for
 * `year`, derived from the zone itself — never a hardcoded "-300"
 * literal. Jan 1 and Jul 1 of any given year are each guaranteed to sit
 * outside the OTHER season's window for any northern-hemisphere
 * "spring forward, fall back" zone (DST, where observed, always spans
 * some contiguous stretch of spring through autumn), so the smaller
 * (more-negative, further-behind-UTC) of the two offsets is always the
 * standard-time one, whichever calendar month that happens to be.
 */
function _standardOffsetMinutesForYear(/** @type {number} */ year) {
  const cached = _standardOffsetByYearCache.get(year);
  if (cached !== undefined) return cached;
  const janOffset = _tzOffsetMinutesAt(new Date(Date.UTC(year, 0, 1, 12, 0, 0)), _US_EASTERN_TZ);
  const julOffset = _tzOffsetMinutesAt(new Date(Date.UTC(year, 6, 1, 12, 0, 0)), _US_EASTERN_TZ);
  const std = Math.min(janOffset, julOffset);
  _standardOffsetByYearCache.set(year, std);
  return std;
}

/**
 * True iff US Eastern time is observing DST at `instant`. Mirrors the
 * backend's `anchor.dst()` truthiness check (`zoneinfo`), but derived
 * from a real UTC-offset comparison rather than a `.dst()` accessor —
 * JS `Date` objects have no equivalent of Python's `zoneinfo` `.dst()`.
 */
export function isUsEasternDst(/** @type {Date} */ instant) {
  const offset = _tzOffsetMinutesAt(instant, _US_EASTERN_TZ);
  return offset > _standardOffsetMinutesForYear(instant.getUTCFullYear());
}

/** @type {Map<string, {hour: number, minute: number}>} */
const _closeTimeCache = new Map();

/**
 * IST close time for MCX non-agri commodities on the IST calendar date
 * (year, month, day) — identical rule to the backend's
 * `mcx_non_agri_close_time(date_)`.
 *
 * Anchored at UTC-noon of the same Y/M/D rather than ET-noon
 * specifically (the backend anchors at noon *ET* on `date_`) — DST
 * itself flips at 02:00 US local time, nowhere near noon in either
 * zone, so anchoring a few hours either side of noon can never land on
 * the wrong side of a transition; this is the same deliberate
 * simplification the backend's own docstring describes (it tolerates
 * up to ~a day or two of IST↔ET anchor slack around the transition
 * weekend itself, since the transition happens only once a year and
 * this only ever needs to pick between two IST close times).
 *
 * @param {number} year
 * @param {number} month 1-indexed (January = 1), matching `Date`'s
 *   human convention rather than JS's own 0-indexed `getMonth()`.
 * @param {number} day
 * @returns {{hour: number, minute: number}}
 */
export function mcxNonAgriCloseTime(year, month, day) {
  const key = `${year}-${month}-${day}`;
  const cached = _closeTimeCache.get(key);
  if (cached) return cached;
  const anchor = new Date(Date.UTC(year, month - 1, day, 12, 0, 0));
  const result = isUsEasternDst(anchor) ? { hour: 23, minute: 30 } : { hour: 23, minute: 55 };
  _closeTimeCache.set(key, result);
  return result;
}

/**
 * Same as `mcxNonAgriCloseTime()`, flattened to IST minute-of-day
 * (e.g. 1410 for 23:30, 1435 for 23:55) — the convention
 * `marketHours.js` uses for every other segment boundary.
 */
export function mcxNonAgriCloseMinuteOfDay(year, month, day) {
  const t = mcxNonAgriCloseTime(year, month, day);
  return t.hour * 60 + t.minute;
}

/**
 * Resolve the real IST close-moment for an expiry date, given which
 * exchange the contract is on. NSE/NFO/BSE/CDS close at 15:30 IST;
 * MCX non-agri commodities close at the DST-aware time above (23:30/
 * 23:55), never a flat 15:30.
 *
 * Extracted as its own small, directly-testable function because a
 * caller that hardcodes 15:30 for an MCX contract truncates its real
 * trading day by up to ~8 hours — checking between 15:30 and midnight
 * IST on the day BEFORE the contract's real expiry then floors
 * "days to expiry" to 0 a full day early, i.e. "expires today" when
 * the real expiry is tomorrow (confirmed live-reported bug, 2026-11-26,
 * in `OptionChainTab.svelte`'s own DTE chip).
 *
 * @param {string} expiryIso YYYY-MM-DD
 * @param {boolean} isMcx
 * @returns {Date}
 */
export function resolveExpiryCloseDateTime(expiryIso, isMcx) {
  const [y, m, d] = expiryIso.split('-').map(Number);
  const { hour, minute } = isMcx ? mcxNonAgriCloseTime(y, m, d) : { hour: 15, minute: 30 };
  const hh = String(hour).padStart(2, '0');
  const mm = String(minute).padStart(2, '0');
  return new Date(`${expiryIso}T${hh}:${mm}:00+05:30`);
}
