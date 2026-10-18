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
 * `opts.exclude` drops specific keys outright regardless of value — e.g.
 * `agent_kind`, which rides on `AgentEvent.detail` purely as filter
 * plumbing (backend/api/algo/actions.py), not operator-useful context.
 */
export function chipsHtml(
  /** @type {Record<string, unknown> | null | undefined} */ obj,
  /** @type {{ skipEmpty?: boolean, order?: string[], exclude?: string[] }} */ opts = {},
) {
  if (!obj || typeof obj !== 'object') return '';
  const skipEmpty = opts.skipEmpty !== false;
  const exclude = Array.isArray(opts.exclude) ? new Set(opts.exclude) : null;
  const keys = Array.isArray(opts.order) && opts.order.length
    ? opts.order.filter((k) => k in obj)
    : Object.keys(obj);
  const out = [];
  for (const k of keys) {
    if (exclude && exclude.has(k)) continue;
    const v = obj[k];
    if (skipEmpty && (v == null || v === '' || (typeof v === 'object' && !Array.isArray(v)))) continue;
    const display = Array.isArray(v) ? v.join(',') : v;
    out.push(chipHtml(k, display));
  }
  return out.join(' ');
}

/**
 * Render an object as a compact `[key:value, key:value]` text string —
 * no HTML, no `{@html}` consumer needed. Used by surfaces that show
 * the same key:value data inline as plain text (e.g. the
 * AgentNotifications popover panel where the rest of the row is
 * normal Svelte template text).
 *
 * Square brackets instead of JSON's curly braces — operator feedback:
 * "key value pair can use square brackets instead of curly brackets".
 */
export function chipsAsText(
  /** @type {Record<string, unknown> | null | undefined} */ obj,
  /** @type {{ skipEmpty?: boolean }} */ opts = {},
) {
  if (!obj || typeof obj !== 'object') return '';
  const skipEmpty = opts.skipEmpty !== false;
  const parts = [];
  for (const [k, v] of Object.entries(obj)) {
    if (skipEmpty && (v == null || v === '')) continue;
    const display = Array.isArray(v) ? v.join(',') : (typeof v === 'object' ? JSON.stringify(v) : v);
    parts.push(`${k}:${display}`);
  }
  return parts.length ? `[${parts.join(', ')}]` : '';
}

/**
 * Parse a JSON-string trigger_condition / detail field and render as
 * `[key:value, key:value]` plain text. Falls through unchanged when
 * the input isn't a JSON object literal.
 */
export function chipsAsTextFromJson(/** @type {unknown} */ raw, /** @type {{ skipEmpty?: boolean }} */ opts = {}) {
  if (raw == null || raw === '') return '';
  if (typeof raw === 'object') return chipsAsText(/** @type any */ (raw), opts);
  if (typeof raw !== 'string') return String(raw);
  const trimmed = raw.trim();
  if (!trimmed.startsWith('{')) return raw;
  try {
    const obj = JSON.parse(trimmed);
    if (obj && typeof obj === 'object' && !Array.isArray(obj)) {
      return chipsAsText(obj, opts);
    }
    return raw;
  } catch (_) {
    return raw;
  }
}

/**
 * Safely parse a detail/trigger_condition JSON-encoded string into a
 * plain object, for callers that need structured field access (not just
 * chip-HTML rendering) — e.g. LogPanel's Agents-tab Level/account
 * filters, which read `.level` / `.account` out of `AgentEventInfo.detail`
 * (a JSON string, `json.dumps()`'d at backend/api/algo/events.py).
 * Returns `{}` for null/empty/non-object/non-JSON/truncated input
 * (AlertEvent.detail is truncated to 240 chars server-side, so a cut-off
 * JSON string is a real, expected case here) — never throws.
 */
export function parseJsonObject(/** @type {unknown} */ raw) {
  if (raw == null || raw === '') return {};
  if (typeof raw === 'object' && !Array.isArray(raw)) return /** @type {Record<string, unknown>} */ (raw);
  if (typeof raw !== 'string') return {};
  const trimmed = raw.trim();
  if (!trimmed.startsWith('{')) return {};
  try {
    const obj = JSON.parse(trimmed);
    return (obj && typeof obj === 'object' && !Array.isArray(obj)) ? obj : {};
  } catch (_) {
    return {};
  }
}

/**
 * Render a JSON string as chips. Returns the raw string verbatim when
 * the input doesn't parse as a JSON object — so an agent's
 * `trigger_condition` that's a plain sentence falls through unchanged.
 *
 * For condition-tree JSON of the shape {metric, scope, op, value}, this
 * surfaces all four as separate chips: `metric:pnl scope:total op:<= value:-50000`.
 */
export function chipsFromJson(/** @type {unknown} */ raw, /** @type {{ skipEmpty?: boolean, order?: string[], exclude?: string[] }} */ opts = {}) {
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
