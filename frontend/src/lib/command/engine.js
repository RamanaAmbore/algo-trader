// Reusable command-line engine: tokenizer + token-by-token autocomplete + parser.
// Grammar-agnostic. Used by order entry today; by agent builder and admin
// console later.
//
// Grammar shape:
//
//   {
//     verbs: {
//       <verb>: {
//         tokens: [
//           { role, suggest: (prefix, ctx) => string[], required?: boolean|fn(ctx),
//             values?: string[], parse?: (raw) => any, resolve?: (val, ctx) => any }
//         ],
//         kwargs: {
//           <name>: { suggest?, values?, parse?, resolve? }
//         },
//       }
//     },
//     verbSuggest?: (prefix) => string[],   // default: prefix-match verbs
//   }
//
// Suggester functions are async-friendly but should return synchronously when
// possible for snappy typing. If they return a Promise, the caller must await.
//
// A "token" here is a whitespace-delimited atom. `key=value` atoms are parsed
// as kwargs; everything else is positional.

/**
 * Tokenise a command line, tracking where each token starts/ends.
 * Quoted strings ("…" or '…') are kept intact. Returns an array of objects.
 */
export function tokenize(line) {
  const tokens = [];
  let i = 0;
  const n = line.length;
  while (i < n) {
    while (i < n && /\s/.test(line[i])) i++;
    if (i >= n) break;
    const start = i;
    const ch = line[i];
    if (ch === '"' || ch === "'") {
      const quote = ch;
      i++;
      while (i < n && line[i] !== quote) i++;
      if (i < n) i++; // consume closing quote
      tokens.push({ start, end: i, raw: line.slice(start, i),
        text: line.slice(start + 1, i - 1), quoted: true });
    } else {
      while (i < n && !/\s/.test(line[i])) i++;
      const raw = line.slice(start, i);
      const eq = raw.indexOf('=');
      if (eq > 0) {
        tokens.push({ start, end: i, raw, text: raw,
          kwarg: { key: raw.slice(0, eq), value: raw.slice(eq + 1) } });
      } else {
        tokens.push({ start, end: i, raw, text: raw });
      }
    }
  }
  return tokens;
}

/**
 * Call-style tokenizer (Sprint 2, agent CLI grammar) — a SEPARATE mode from
 * `tokenize()` above. `tokenize()` is purely whitespace-delimited and is left
 * byte-for-byte unchanged (orders.js depends on its exact behavior); this is
 * a new, additive export for grammars whose tokens use call syntax —
 * `name(arg1, key=value)` — where `(`, `)`, and `,` must be recognized as
 * structural delimiters even with NO surrounding whitespace, e.g.
 * `order(account, symbol="NIFTY25JULFUT")`.
 *
 * Quote-aware like `tokenize()`, plus one extra refinement `tokenize()` does
 * NOT have: a `key=value` kwarg atom whose value is immediately a quoted
 * string (`key="..."` / `key='...'`, no space before the quote) consumes the
 * ENTIRE quoted literal — including any spaces inside it — as that kwarg's
 * value, with the surrounding quotes stripped and `kwarg.quoted = true`. A
 * spaced `key = "value"` form does NOT get this special fusion (there IS a
 * gap before the quote) — callers that need the spaced form supported treat
 * a standalone `=` punct token + the following quoted/bare token as the
 * same kwarg themselves (this is a parser-level concern, not a tokenizer
 * one, matching the plan's adjacency-rule split of responsibilities).
 */
export function tokenizeCallStyle(line) {
  const tokens = [];
  let i = 0;
  const n = line.length;
  const isDelim = (ch) => ch === '(' || ch === ')' || ch === ',';
  while (i < n) {
    while (i < n && /\s/.test(line[i])) i++;
    if (i >= n) break;
    const start = i;
    const ch = line[i];
    if (ch === '"' || ch === "'") {
      const quote = ch;
      i++;
      while (i < n && line[i] !== quote) i++;
      if (i < n) i++; // consume closing quote
      tokens.push({ start, end: i, raw: line.slice(start, i),
        text: line.slice(start + 1, i - 1), quoted: true });
      continue;
    }
    if (isDelim(ch)) {
      i++;
      tokens.push({ start, end: i, raw: ch, text: ch, punct: ch });
      continue;
    }
    // Generic atom: scan until whitespace, a structural delimiter, or a
    // quote char — UNLESS we're sitting right after a `key=` boundary with
    // no gap before the quote, in which case fuse the quoted literal in.
    while (i < n && !/\s/.test(line[i]) && !isDelim(line[i]) && line[i] !== '"' && line[i] !== "'") {
      i++;
    }
    const nameEnd = i;
    const raw0 = line.slice(start, nameEnd);
    const eq = raw0.indexOf('=');
    if (eq > 0 && i < n && (line[i] === '"' || line[i] === "'")) {
      const key = raw0.slice(0, eq);
      const quote = line[i];
      const qStart = i;
      i++;
      while (i < n && line[i] !== quote) i++;
      if (i < n) i++; // consume closing quote
      const value = line.slice(qStart + 1, Math.max(qStart + 1, i - 1));
      tokens.push({ start, end: i, raw: line.slice(start, i), text: line.slice(start, i),
        kwarg: { key, value, quoted: true } });
      continue;
    }
    if (eq > 0) {
      tokens.push({ start, end: nameEnd, raw: raw0, text: raw0,
        kwarg: { key: raw0.slice(0, eq), value: raw0.slice(eq + 1) } });
    } else {
      tokens.push({ start, end: nameEnd, raw: raw0, text: raw0 });
    }
  }
  return tokens;
}

/**
 * Fuzzy-subsequence match (Sprint 2, agent CLI grammar) — true when every
 * character of `typed` appears, in order, in `candidate` (case-insensitive),
 * not necessarily contiguously — command-palette style (`mnpnl` matches
 * `mean_pnl`). Exported standalone — opt-in, does not change `suggestAt()`'s
 * own default `.startsWith()` filtering, so orders.js's existing suggestion
 * behavior is untouched.
 */
export function fuzzySubsequenceMatch(typed, candidate) {
  if (!typed) return true;
  const t = String(typed).toLowerCase();
  const c = String(candidate).toLowerCase();
  let ti = 0;
  for (let ci = 0; ci < c.length && ti < t.length; ci++) {
    if (c[ci] === t[ti]) ti++;
  }
  return ti === t.length;
}

/**
 * Rank + filter `candidates` by fuzzy-subsequence match against `prefix`.
 * Exact-prefix matches sort first (score 0), then by how early the first
 * typed character appears, then alphabetically. Returns `candidates`
 * unfiltered (shallow-copied) when `prefix` is empty.
 */
export function fuzzyFilter(prefix, candidates) {
  if (!prefix) return candidates.slice();
  const p = String(prefix).toLowerCase();
  return candidates
    .filter(c => fuzzySubsequenceMatch(p, c))
    .map(c => ({ c, score: _fuzzyScore(p, String(c).toLowerCase()) }))
    .sort((a, b) => a.score - b.score || a.c.localeCompare(b.c))
    .map(x => x.c);
}

function _fuzzyScore(p, c) {
  if (c.startsWith(p)) return 0;
  const idx = c.indexOf(p[0]);
  return idx < 0 ? 1000 : idx + 1;
}

/** Find the token containing cursorPos (or the one being typed). */
export function tokenAtCursor(tokens, cursorPos, line) {
  // If cursor is immediately after a token (no trailing space), we're still editing that token
  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i];
    if (cursorPos >= t.start && cursorPos <= t.end) return { index: i, token: t, editing: true };
  }
  // Cursor is in whitespace — next token position
  const trimmed = line.slice(0, cursorPos);
  const trailing = trimmed.endsWith(' ') || trimmed === '';
  return {
    index: tokens.length,
    token: null,
    editing: trailing,
  };
}

/** Count positional (non-kwarg) tokens before index i. */
function _positionalIndex(tokens, i) {
  let n = 0;
  for (let j = 0; j < i; j++) if (!tokens[j].kwarg) n++;
  return n;
}

/** Advance posIdx past optional positional specs whose fixed `values` don't match token text.
 *  When `forSuggest` is true (cursor is at this position), stop at optional specs so they
 *  can be offered as suggestions — don't skip them. */
function _skipOptionalSpecs(verb, posIdx, tokenText, accumulatedCtx = {}, forSuggest = false) {
  while (posIdx < verb.tokens.length) {
    const spec = verb.tokens[posIdx];
    if (typeof spec.required === 'function') {
      if (!spec.required(accumulatedCtx)) { posIdx++; continue; }
      break;
    }
    const req = spec.required;
    const hasFixedValues = Array.isArray(spec.values) && spec.values.length > 0;
    if (!req && hasFixedValues) {
      // When suggesting (empty prefix or partial), show this spec's values
      if (forSuggest) break;
      const up = String(tokenText).toUpperCase();
      const inList = spec.values.some(v => String(v).toUpperCase() === up);
      if (!inList) { posIdx++; continue; }
    }
    break;
  }
  return posIdx;
}

/** Map an ordered list of positional token texts to their matched spec index. */
function _alignPositionalSpecs(verb, positionalTexts) {
  const out = [];
  const ctx = {};
  let posIdx = 0;
  for (const text of positionalTexts) {
    posIdx = _skipOptionalSpecs(verb, posIdx, text, ctx);
    const spec = verb.tokens[posIdx];
    if (spec) {
      try { ctx[spec.role] = spec.parse ? spec.parse(text) : text; }
      catch { ctx[spec.role] = text; }
    }
    out.push(posIdx);
    posIdx++;
  }
  return out;
}

/**
 * Generate suggestions at the cursor position.
 * Returns { suggestions: string[], role: string|null, hint: string|null }.
 */
export function suggestAt(line, cursorPos, grammar, context = {}) {
  const tokens = tokenize(line);
  const at = tokenAtCursor(tokens, cursorPos, line);
  const activeIdx = at.editing ? at.index : -1;

  // Token 0: verb
  if (activeIdx === 0 || (activeIdx < 0 && tokens.length === 0)) {
    const prefix = activeIdx === 0 && tokens[0] ? tokens[0].text : '';
    const all = grammar.verbSuggest
      ? grammar.verbSuggest(prefix)
      : Object.keys(grammar.verbs).filter(v => v.toUpperCase().startsWith(prefix.toUpperCase()));
    return { suggestions: all, role: 'verb', hint: null };
  }

  // Resolve verb
  const verbTok = tokens[0];
  if (!verbTok) return { suggestions: [], role: null, hint: null };
  const verbName = verbTok.text.toLowerCase();
  const verb = grammar.verbs[verbName];
  if (!verb) return { suggestions: [], role: null, hint: `Unknown verb: ${verbName}` };

  // Active token is a kwarg (contains '=')?
  const activeTok = activeIdx >= 0 ? tokens[activeIdx] : null;
  if (activeTok && activeTok.raw.includes('=')) {
    const [k, v] = activeTok.raw.split('=');
    const spec = verb.kwargs && verb.kwargs[k];
    if (!spec) {
      // Suggest kwarg names
      const kwNames = Object.keys(verb.kwargs || {});
      return {
        suggestions: kwNames
          .filter(n => n.startsWith(k))
          .map(n => `${n}=`),
        role: 'kwarg-key', hint: null,
      };
    }
    let values = [];
    if (spec.values) values = spec.values.filter(x => x.toUpperCase().startsWith((v || '').toUpperCase())).map(x => `${k}=${x}`);
    else if (spec.suggest) values = (spec.suggest(v || '', _buildCtx(verb, tokens, context, verbName)) || []).map(x => `${k}=${x}`);
    return { suggestions: values, role: `kwarg:${k}`, hint: spec.hint || null };
  }

  // Positional token — figure out which spec the user is typing.
  // Walk through already-typed positional tokens (before activeIdx) and
  // align to specs, including any "optional skip". The current cursor token
  // then goes to the spec index right after the last aligned one (or,
  // if its text already matches an optional spec's values, to that spec).
  const priorPositional = [];
  const stopIdx = activeIdx >= 0 ? activeIdx : tokens.length;
  for (let i = 1; i < stopIdx; i++) {
    if (!tokens[i].kwarg) priorPositional.push(tokens[i].text);
  }
  const priorSpecIdxs = _alignPositionalSpecs(verb, priorPositional);
  let startPos = (priorSpecIdxs.length > 0 ? priorSpecIdxs[priorSpecIdxs.length - 1] + 1 : 0);
  const prefix = activeTok ? activeTok.text : '';
  const priorCtx = _buildCtx(verb, tokens.slice(0, Math.max(1, stopIdx)), context, verbName);

  // If the spec at startPos is optional AND the prefix doesn't match its values,
  // surface BOTH the optional values AND the next required spec's suggestions
  // (so users discover the optional token while still typing toward required).
  const optSpec = verb.tokens[startPos];
  const isOptional = optSpec
    && !(typeof optSpec.required === 'function' ? optSpec.required(priorCtx) : optSpec.required)
    && Array.isArray(optSpec.values);
  if (isOptional) {
    const matchesOpt = optSpec.values.some(v => String(v).toUpperCase().startsWith(prefix.toUpperCase()));
    if (matchesOpt && prefix) {
      // Partial match of optional values → show only the optional
      const vals = optSpec.values.filter(x => x.toUpperCase().startsWith(prefix.toUpperCase()));
      return { suggestions: vals, role: optSpec.role, hint: optSpec.hint || null };
    }
    // Empty prefix: show ONLY the optional values (not the next spec's large list)
    return {
      suggestions: optSpec.values,
      role: optSpec.role,
      hint: optSpec.hint || null,
    };
  }
  startPos = _skipOptionalSpecs(verb, startPos, prefix, priorCtx);
  const spec = verb.tokens[startPos];
  if (!spec) {
    // Past last positional — suggest kwarg names (exclude already-used ones)
    const usedKw = new Set(tokens.filter(t => t.kwarg).map(t => t.kwarg.key));
    const kwNames = Object.keys(verb.kwargs || {}).filter(n => !usedKw.has(n));
    return {
      suggestions: kwNames.filter(n => n.startsWith(prefix)).map(n => `${n}=`),
      role: 'kwarg-key', hint: null,
    };
  }
  const ctx = _buildCtx(verb, tokens, context, verbName);
  let values = [];
  if (spec.values) values = spec.values.filter(x => x.toUpperCase().startsWith(prefix.toUpperCase()));
  else if (spec.suggest) values = spec.suggest(prefix, ctx) || [];
  return { suggestions: values, role: spec.role, hint: spec.hint || null };
}

/** Build a context object from already-typed positional tokens. */
function _buildCtx(verb, tokens, extraContext, verbName) {
  const ctx = { ...extraContext };
  if (verbName) ctx._verb = verbName;
  // Collect positional tokens and their alignment to specs
  const positionalTexts = [];
  for (let i = 1; i < tokens.length; i++) {
    const t = tokens[i];
    if (t.kwarg) {
      ctx[t.kwarg.key] = t.kwarg.value;
    } else {
      positionalTexts.push(t.text);
    }
  }
  const specIdxs = _alignPositionalSpecs(verb, positionalTexts);
  for (let i = 0; i < positionalTexts.length; i++) {
    const spec = verb.tokens[specIdxs[i]];
    if (spec) {
      const raw = positionalTexts[i];
      ctx[spec.role] = spec.parse ? spec.parse(raw) : raw;
    }
  }
  return ctx;
}

/**
 * Parse a complete command line into { verb, args, kwargs, errors }.
 * `args` is keyed by token role. Each value goes through spec.parse and
 * spec.resolve (if provided, resolve runs with full ctx after parse).
 */
export function parse(line, grammar, context = {}) {
  const tokens = tokenize(line);
  if (tokens.length === 0) return { verb: null, args: {}, kwargs: {}, errors: ['empty'] };
  const verbName = tokens[0].text.toLowerCase();
  const verb = grammar.verbs[verbName];
  if (!verb) return { verb: null, args: {}, kwargs: {}, errors: [`unknown verb: ${verbName}`] };

  const args = {};
  const kwargs = {};
  const errors = [];
  let posIdx = 0;

  for (let i = 1; i < tokens.length; i++) {
    const t = tokens[i];
    if (t.kwarg) {
      const spec = (verb.kwargs && verb.kwargs[t.kwarg.key]) || null;
      let val = t.kwarg.value;
      if (spec && spec.parse) {
        try { val = spec.parse(val); } catch (e) { errors.push(`${t.kwarg.key}: ${e.message}`); }
      }
      kwargs[t.kwarg.key] = val;
    } else {
      posIdx = _skipOptionalSpecs(verb, posIdx, t.text, { ...context, ...args, ...kwargs });
      const spec = verb.tokens[posIdx];
      if (!spec) { errors.push(`unexpected token: ${t.text}`); continue; }
      let val = t.text;
      if (spec.parse) {
        try { val = spec.parse(val); } catch (e) { errors.push(`${spec.role}: ${e.message}`); }
      }
      args[spec.role] = val;
      posIdx++;
    }
  }

  // Check required tokens
  const fullCtx = { ...context, ...args, ...kwargs };
  for (let i = 0; i < verb.tokens.length; i++) {
    const spec = verb.tokens[i];
    const required = typeof spec.required === 'function' ? spec.required(fullCtx) : spec.required;
    if (required && !(spec.role in args)) errors.push(`missing ${spec.role}`);
  }

  // Resolve hooks (can read full ctx — useful for symbol lookup)
  for (const spec of verb.tokens) {
    if (spec.resolve && spec.role in args) {
      try { args[spec.role] = spec.resolve(args[spec.role], fullCtx); }
      catch (e) { errors.push(`${spec.role}: ${e.message}`); }
    }
  }
  if (verb.kwargs) {
    for (const [k, spec] of Object.entries(verb.kwargs)) {
      if (spec.resolve && k in kwargs) {
        try { kwargs[k] = spec.resolve(kwargs[k], fullCtx); }
        catch (e) { errors.push(`${k}: ${e.message}`); }
      }
    }
  }

  return { verb: verbName, args, kwargs, errors };
}

/** Replace the active token in `line` with `replacement`, return new line + cursor. */
export function applySuggestion(line, cursorPos, replacement) {
  // A suggestion may be a value+label like "500 (5 lots × 100)" — only insert
  // the value portion (first whitespace-delimited token, or before " (").
  const parenIdx = replacement.indexOf(' (');
  const value = parenIdx > 0 ? replacement.slice(0, parenIdx) : replacement;
  const tokens = tokenize(line);
  const at = tokenAtCursor(tokens, cursorPos, line);
  if (at.token) {
    const before = line.slice(0, at.token.start);
    const after = line.slice(at.token.end);
    const needsSpace = !after.startsWith(' ') && !value.endsWith('=');
    const insert = value + (needsSpace ? ' ' : '');
    return { line: before + insert + after, cursor: before.length + insert.length };
  }
  // Insert at cursor (new token)
  const before = line.slice(0, cursorPos);
  const after = line.slice(cursorPos);
  const needsLeadSpace = before.length > 0 && !before.endsWith(' ');
  const needsTrailSpace = !after.startsWith(' ') && !value.endsWith('=');
  const insert = (needsLeadSpace ? ' ' : '') + value + (needsTrailSpace ? ' ' : '');
  return { line: before + insert + after, cursor: before.length + insert.length };
}
