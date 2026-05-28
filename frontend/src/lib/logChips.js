/**
 * Shared log-chip formatter — renders an object of key→value pairs as
 * a sequence of <span class="log-chip"><span class="log-chip-key">key:</span>value</span>
 * chips matching the existing .log-chip / .log-chip-key styles in
 * LogPanel.svelte. Used by:
 *   - Order log row formatter (status / chase / engine / agent chips)
 *   - Agent log row formatter (trigger_condition JSON expanded to chips)
 *   - Any future log surface that needs the same key:value chip look
 *
 * No colour coding — chips share one neutral palette so the operator's
 * eye lands on values, not on chip backgrounds.
 *
 * Output is plain HTML string so callers can `{@html ...}` it inside a
 * <pre>-rendered log; no Svelte component needed for that path.
 */

/**
 * Escape minimal HTML so a chip value containing < or > or & doesn't
 * break the surrounding {@html} rendering.
 */
function _escape(/** @type {unknown} */ v) {
  return String(v ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/**
 * Render one chip: <span class="log-chip"><span class="log-chip-key">key:</span>value</span>
 */
export function chipHtml(/** @type {string} */ key, /** @type {unknown} */ value) {
  return `<span class="log-chip"><span class="log-chip-key">${_escape(key)}:</span>${_escape(value)}</span>`;
}

/**
 * Render an object of {key: value, …} as a sequence of chips joined by
 * spaces. Falsy / undefined values are dropped so the surface stays
 * scannable (no `engine:undefined` clutter).
 *
 * `opts.skipEmpty=false` keeps empty-string values; defaults to dropping.
 * `opts.order` lets the caller pin the chip order (otherwise insertion
 * order of the object literal wins, which is fine for hand-built dicts).
 */
export function chipsHtml(
  /** @type {Record<string, unknown> | null | undefined} */ obj,
  /** @type {{ skipEmpty?: boolean, order?: string[] }} */ opts = {},
) {
  if (!obj || typeof obj !== 'object') return '';
  const skipEmpty = opts.skipEmpty !== false;
  const keys = Array.isArray(opts.order) && opts.order.length
    ? opts.order.filter((k) => k in obj)
    : Object.keys(obj);
  const out = [];
  for (const k of keys) {
    const v = obj[k];
    if (skipEmpty && (v == null || v === '' || (typeof v === 'object' && !Array.isArray(v)))) continue;
    const display = Array.isArray(v) ? v.join(',') : v;
    out.push(chipHtml(k, display));
  }
  return out.join(' ');
}

/**
 * Render a JSON string as chips. Returns the raw string verbatim when
 * the input doesn't parse as a JSON object — so an agent's
 * `trigger_condition` that's a plain sentence falls through unchanged.
 *
 * For condition-tree JSON of the shape {metric, scope, op, value}, this
 * surfaces all four as separate chips: `metric:pnl scope:total op:<= value:-50000`.
 */
export function chipsFromJson(/** @type {unknown} */ raw, /** @type {{ skipEmpty?: boolean }} */ opts = {}) {
  if (raw == null || raw === '') return '';
  if (typeof raw === 'object') return chipsHtml(/** @type any */ (raw), opts);
  if (typeof raw !== 'string') return _escape(raw);
  const trimmed = raw.trim();
  if (!trimmed.startsWith('{')) return _escape(raw);
  try {
    const obj = JSON.parse(trimmed);
    if (obj && typeof obj === 'object' && !Array.isArray(obj)) {
      return chipsHtml(obj, opts);
    }
    return _escape(raw);
  } catch (_) {
    return _escape(raw);
  }
}
