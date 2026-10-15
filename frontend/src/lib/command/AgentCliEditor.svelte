<script>
  /**
   * AgentCliEditor — Sprint 4 UI for the agent CLI grammar
   * (frontend/src/lib/command/grammars/agents.js, Sprints 1-3). Renders a
   * single textarea + a fixed-position dropdown panel anchored directly
   * below it (screener.in-style lookahead — NOT caret-pixel-tracked,
   * see .claude/PLAN.md / ~/.claude/plans/purrfect-marinating-pixel.md)
   * + either a red error list or a green-bordered read-only JSON preview
   * of the compiled payload.
   *
   * Dropdown (Sprint [CLI dropdown], 2026-10): gated on candidate-list
   * size (see `_showDropdown`), keyboard-navigable (ArrowUp/Down/Enter/
   * Tab/Escape, see `handleKeydown`), and auto-advances through a
   * statement's structure on `applySuggestion` — accepting a
   * parameterized token auto-opens `(`, a param name auto-inserts `=`,
   * a value fill auto-inserts `, ` — by re-deriving `suggestAgentCliAt`
   * at the new cursor position via ordinary reactivity, never by
   * extending that function's own contract.
   *
   * Props:
   *   value      — bindable CLI text.
   *   catalog    — already-fetched catalog from `fetchAgentCatalog()`
   *                (this component NEVER calls it itself — the parent
   *                fetches once and passes it down so toggling
   *                Structured↔CLI repeatedly never refetches).
   *   lotSizeOf  — `(symbol) => number|null` lot-size resolver, threaded
   *                into `compileAgentCliStatement`'s opts.
   *   mode       — 'paper'|'live', threaded as opts.mode for a bare
   *                order statement.
   *   onCompile  — `(result) => void`, fired on every successful compile
   *                (debounced ~150ms) AFTER the operator has actually
   *                edited the text — never on mount/prop-reseed, so
   *                switching tabs can't silently overwrite whatever the
   *                Structured tab's own JSON textareas currently hold.
   *   onOrderSubmit — `(result) => void`, fired when the operator clicks
   *                "Place order" for a `kind:'order'` compile result, OR
   *                "Cancel orders (LIVE)" for a `kind:'cancel_all_orders'`
   *                compile result (Sprint [cancel-all-cli], 2026-10) —
   *                same prop, same button element; the host branches on
   *                `result.kind` to decide which backend route to call.
   *   allowedKinds — Sprint B (2026-10, Phase 2) — optional array
   *                restricting compiled statement kinds (see
   *                `compileStatement`'s own docstring in agents.js for
   *                the full contract). Threaded straight into this
   *                component's own debounced compile call so an order-
   *                scoped mount's error list shows the real scope
   *                rejection live, not just at submit time. `undefined`
   *                (the default) means no restriction.
   *   placeholder — textarea placeholder text. Defaults to the full
   *                WHEN/ALERT/DO example; order-scoped mounts override
   *                this with a plain `order(...)` example so the
   *                placeholder itself signals the narrower scope.
   *   info       — Sprint C (2026-10, Phase 2 "Automation redesign") —
   *                `string[]`, zero or more non-fatal hint/status lines
   *                rendered ABOVE the compile-error/preview area —
   *                e.g. "Editing agent 'foo-bar' — Save will update, not
   *                create." when the host has entered edit mode. Purely
   *                presentational; this component never populates it
   *                itself. Omit/empty = no info block rendered.
   *   submitStatus — Sprint C — `{ok: boolean, text: string} | null`, the
   *                outcome of the LAST actual backend submit attempt (as
   *                opposed to client-side compile errors, which are
   *                always shown via the existing `.agent-cli-errors`
   *                block regardless of this prop). Rendered as a small
   *                persistent status line — distinct from (and in
   *                addition to) any toast the host also fires, since a
   *                toast alone disappears too fast to serve as a record
   *                of "did this actually save." The host sets/clears this
   *                — this component never calls a submit endpoint itself.
   *   showCompileErrors — Sprint I Phase 1 (2026-10-11, console dispatch
   *                layer) — `true` (default, preserving every existing
   *                mount's behaviour unchanged) shows the `.agent-cli-
   *                errors` block exactly as before. `false` suppresses
   *                it: used by Console's unified box when the typed text
   *                classifies as legacy-grammar syntax (buy/sell/cancel/
   *                modify, shell) — this grammar correctly fails to
   *                compile that text, and showing a red "Errors:" block
   *                for text that was never meant to compile here would
   *                be confusing, not informative. Autocomplete
   *                suggestions are NOT gated by this prop — they stay
   *                live regardless, since the operator may still be
   *                mid-way through typing a real statement (e.g. `ord`
   *                before the `(`).
   */

  import { tick } from 'svelte';
  import {
    compileAgentCliStatement, suggestAgentCliAt, describeErrorPosition,
    decompileAgent, decompileEventAgent, decompileBracket, decompileToken,
    lookupAgentCliEntry, isInsideOpenCall,
  } from '$lib/command/grammars/agents.js';
  import { pushLayer, popLayer } from '$lib/utils/layerStack.js';
  import { getAccountsSync } from '$lib/data/accounts.js';
  import { suggestTradingsymbols } from '$lib/data/instruments.js';

  /** @type {{
   *   value: string, catalog: any, lotSizeOf?: (sym: string) => number|null,
   *   mode?: 'paper'|'live',
   *   onCompile?: (result: any) => void,
   *   onOrderSubmit?: (result: any) => void,
   *   allowedKinds?: string[],
   *   placeholder?: string,
   *   info?: string[],
   *   submitStatus?: {ok: boolean, text: string} | null,
   *   showCompileErrors?: boolean,
   * }} */
  let {
    value = $bindable(''), catalog = null, lotSizeOf = undefined,
    mode = 'paper', onCompile = undefined, onOrderSubmit = undefined,
    allowedKinds = undefined,
    placeholder = 'GIVEN p = pnl@positions.total\nmsg_txt = "Loss: " + p\nWHEN p <= -50000\nALERT msg = msg_txt, telegram\nDO order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1)',
    info = [], submitStatus = null,
    showCompileErrors = true,
  } = $props();

  /** @type {HTMLTextAreaElement|null} */
  let textareaEl = $state(null);
  let cursorPos = $state(0);
  // Sprint [CLI dropdown] — whether the textarea currently has focus.
  // `_showDropdown` is gated on this too so the panel doesn't linger
  // on screen (visually overlapping the result area below, including
  // the "Place order" button) once the operator clicks/tabs away —
  // see each dropdown row's own `onmousedown` preventDefault for why
  // clicking a row itself never flips this false before `onclick` runs.
  let _focused = $state(false);
  // LABEL (not index) of whichever suggestion pill is currently hovered or
  // keyboard-focused — drives the small description detail line below the
  // row (Sprint 11). Deliberately keyed by the suggestion's own string,
  // not its position: the suggestion list re-filters on every keystroke
  // (same `suggestResult` derived below), so an index captured while the
  // mouse rested on pill 3 would silently point at whatever suggestion
  // happens to occupy slot 3 AFTER a re-filter — including a completely
  // different token — the moment the operator keeps typing without the
  // mouse ever leaving the pill (no `mouseleave` fires in that case).
  // Looking the label back up in the CURRENT `suggestResult.suggestions`
  // below means a label that has since dropped out of the list simply
  // shows no description, never a stale/wrong one. `null` when nothing
  // is hovered/focused.
  let _hoverLabel = $state(null);
  // Flips true on the FIRST real operator edit (typed char or applied
  // suggestion) after mount/prop-reseed — gates `onCompile` so a freshly
  // decompiled/seeded `value` never fires a write-back into the parent's
  // JSON fields before the operator has touched anything.
  let dirty = $state(false);

  // Sprint [CLI dropdown] (2026-10) — keyboard-navigation index into the
  // CURRENT `suggestResult.suggestions` list (clamped, never wraps). `-1`
  // means "no row is explicitly active yet" — the sentinel that keeps
  // Enter/Tab from hijacking a bare keystroke the moment the dropdown
  // shows ANY candidates (including the common case where the list is a
  // single exact match of what's already typed, e.g. right after typing
  // a complete scope chain) — per the brief's own wording, Enter/Tab only
  // intercept "when a suggestion is active", distinct from "the dropdown
  // is open" (see `handleKeydown`). Reset to -1 whenever the suggestion
  // list is recomputed (see the `$effect` below `suggestResult`'s own
  // declaration) so a stale index from a just-superseded list can never
  // point at the wrong row after a keystroke re-filters it. ArrowDown/Up
  // (see `handleKeydown`) are what first move this off -1.
  let _activeIndex = $state(-1);
  // Cursor offset + full text snapshot captured at the moment the
  // operator last pressed Escape to dismiss the dropdown — dismissal is
  // scoped to that EXACT (position, text) pair, not "dismissed until
  // something else happens": moving the cursor OR typing further
  // immediately re-qualifies the dropdown to reappear (see `_dismissed`
  // below), and an auto-advance-triggered reopen at a DIFFERENT position
  // (e.g. right after an auto-inserted `(`) is never suppressed by a
  // manual dismissal recorded at the position before it. `null` when
  // nothing has been dismissed (or a dismissal has since been superseded).
  let _dismissedAt = $state(null);

  /** @type {{ok:boolean, errors:any[], kind:string|null, agent:any, ticket:any, basket:any, template:any, fragment:any, cancelAll:any}|null} */
  let compiled = $state(null);

  $effect(() => {
    const text = value;
    const cat = catalog;
    const m = mode === 'live' ? 'live' : 'paper';
    const wasDirty = dirty;
    const timer = setTimeout(() => {
      if (!cat) { compiled = null; return; }
      // Sprint H follow-up (operator-reported, 2026-10-11) — an empty or
      // whitespace-only textarea (on mount, or after the operator clears
      // it back out) used to still reach `compileAgentCliStatement`, which
      // fails with a confusing "expected a name (line 1, col 1)" parse
      // error — surfaced immediately in the result area even though the
      // operator hasn't typed anything (they were only looking at the
      // grayed placeholder text). There is nothing meaningful to parse
      // here, so skip the compile call entirely (not just hide the error
      // display) and leave the result area in its normal "nothing
      // compiled yet" state, same as the `!cat` guard right above.
      if (!text || !text.trim()) { compiled = null; return; }
      let result;
      try {
        result = compileAgentCliStatement(text, cat, { mode: m, lotSizeOf, allowedKinds });
      } catch (e) {
        result = { ok: false, errors: [{ message: e?.message || 'compile failed' }], kind: null, agent: null, ticket: null, basket: null, template: null, fragment: null, cancelAll: null };
      }
      compiled = result;
      if (result.ok && wasDirty) onCompile?.(result);
    }, 150);
    return () => clearTimeout(timer);
  });

  function _updateCursor() {
    cursorPos = textareaEl ? (textareaEl.selectionStart ?? value.length) : value.length;
  }

  function handleInput() {
    dirty = true;
    _updateCursor();
    _autoCorrectCase();
  }

  const suggestResult = $derived.by(() => {
    if (!catalog) return { suggestions: [], descriptions: [], labels: [], replaceRange: [cursorPos, cursorPos], kind: null };
    // `account=` has no static enum in the grammar schema (real account
    // ids are runtime, operator-specific data) — threaded in here from
    // the live account list so the dropdown still shows real choices
    // instead of forcing free-typing (2026-10-15, operator request).
    // `symbol=` is the same case but with a ~90k-row universe — a lazy
    // resolver (not an eager array) so every suggestResult recompute
    // doesn't pay for the full instrument list; `_paramOrEnumSuggestion`
    // only calls it once 3+ characters are typed.
    // 2026-10-15 — `allowedKinds` (already threaded into the COMPILE
    // call's own opts below) now ALSO reaches the suggestion engine, so
    // an order-scoped mount's dropdown never offers a bare statement
    // kind the compiler is guaranteed to reject (operator: "does the
    // dropdown for orders cli, non order related tokens functions").
    const opts = {
      accountIds: getAccountsSync().map((a) => a.account_id),
      suggestSymbols: (prefix) => suggestTradingsymbols(prefix, 50),
      allowedKinds,
    };
    try { return suggestAgentCliAt(value, cursorPos, catalog, opts); }
    catch { return { suggestions: [], descriptions: [], labels: [], replaceRange: [cursorPos, cursorPos], kind: null }; }
  });

  // Operator request (2026-10, follow-up to the "DO not showing as
  // selected" fix above `suggestAgentCliAt`): once the text just typed
  // exactly matches — case-insensitively — the dropdown's SOLE surviving
  // candidate, rewrite it to that candidate's own canonical casing (e.g.
  // "do"/"Do" self-corrects to "DO" the instant it's unambiguous),
  // instead of requiring the operator to pick it from the dropdown by
  // hand just to fix casing. In practice this is "uppercase" for the
  // clause-head/connective keywords (stored in the catalog/candidate
  // lists as "WHEN"/"ALERT"/"DO"/"GIVEN"/etc.) and a no-op for ordinary
  // metric/scope/channel/action names, which are already lowercase in
  // both what an operator naturally types and the catalog itself.
  // Reads the SAME `suggestResult` the dropdown renders from (no second
  // `suggestAgentCliAt` call) — Svelte 5's `$derived` is pull-based, so
  // this already reflects the just-updated `value`/`cursorPos` by the
  // time `handleInput` reads it, synchronously, no extra tick needed.
  // Deliberately gated to EXACTLY ONE candidate and a FULL (same-length)
  // case-insensitive match — a still-ambiguous partial (e.g. "ale" with
  // only "ALERT" matching so far) must never be silently "completed" by
  // this; it only fires at the exact moment typing could not possibly
  // mean anything else.
  async function _autoCorrectCase() {
    const { suggestions, replaceRange } = suggestResult;
    if (!replaceRange || suggestions.length !== 1) return;
    const [start, end] = replaceRange;
    if (end <= start || end > value.length) return;
    const typed = value.slice(start, end);
    const canonical = suggestions[0];
    if (typed === canonical) return; // already correctly cased
    if (typed.toLowerCase() !== canonical.toLowerCase()) return; // not a full match yet
    value = value.slice(0, start) + canonical + value.slice(end);
    const newCursor = start + canonical.length;
    await tick();
    textareaEl?.setSelectionRange(newCursor, newCursor);
    cursorPos = newCursor;
  }

  // Every time `suggestResult` is recomputed (new object identity on every
  // keystroke/cursor-move — `_suggestAgentCliAtImpl` never returns a cached
  // reference), the candidate list has effectively changed underneath
  // `_activeIndex` — reset to "nothing EXPLICITLY active" rather than risk
  // pointing at an unrelated row a stale index happened to still be
  // in-range for. Deliberately stays -1 here (NOT reset to 0) — Enter's
  // handler below gates on this exact state to decide whether a bare
  // Enter should insert a newline (multi-line GIVEN/WHEN/ALERT/DO
  // statements need this) or accept a suggestion; defaulting this to 0
  // would silently hijack every first Enter press after a dropdown opens.
  // The row-0-highlighted-by-default / Tab-accepts-it behavior (2026-10-15,
  // operator: "when multiple tokens match, in the dropdown the first
  // match should be highlighted, if tab entered it should be selected")
  // is handled separately below via `_displayActiveIndex` (visual only)
  // and the Tab-specific branch in `handleKeydown` — neither touches
  // Enter's behavior.
  $effect(() => { suggestResult; _activeIndex = -1; });

  // Visual-only "which row looks highlighted" — row 0 whenever nothing has
  // been explicitly arrow-navigated/hovered yet (so the operator can SEE
  // what Tab will pick), falling back to the real `_activeIndex` once they
  // navigate. Never read by Enter's handler — see the effect above.
  const _displayActiveIndex = $derived(_activeIndex >= 0 ? _activeIndex : (suggestResult.suggestions.length ? 0 : -1));

  // Suppress the compile-error block while the operator is still actively
  // typing INSIDE an unclosed call (2026-10-15, operator: "while selecting
  // order(, it also shows error message right bracket required, that give
  // wrong information... User might be selecting more keywords and values
  // before typing )"). A statement like `order(account="ZG0790"` is
  // compiled (debounced) on every keystroke same as always, and ALWAYS
  // fails with "expected ')'" until the closing paren lands — that's not a
  // real defect, it's the expected shape of an in-progress statement, so
  // showing it as an error is actively misleading mid-edit. Gated on
  // `_focused` too: once the operator clicks away leaving a call genuinely
  // unclosed, that abandoned state IS worth surfacing as an error again.
  const _suppressErrorsWhileOpenCall = $derived.by(() => {
    if (!_focused) return false;
    try { return isInsideOpenCall(value, cursorPos); } catch { return false; }
  });

  // Same reasoning, different trigger (2026-10-15, operator: "given a =
  // 1+1 is generating error") — `GIVEN a = 1+1` alone always fails with
  // "an agent must have at least one of ALERT or DO" until a WHEN/ALERT/
  // DO clause is added, same as `order(...)` always failed with
  // "expected ')'" until closed. Matched by exact message rather than a
  // broader "any error while focused" suppression, so a genuinely wrong
  // statement (e.g. a typo'd metric name) still surfaces immediately —
  // only this one "more is coming" message is deferred.
  const _MISSING_ALERT_OR_DO_MSG = 'an agent must have at least one of ALERT or DO';
  const _suppressMissingAlertOrDo = $derived.by(() => {
    if (!_focused || !compiled || compiled.ok) return false;
    return compiled.errors?.some((e) => e.message === _MISSING_ALERT_OR_DO_MSG) ?? false;
  });

  // Sprint [CLI dropdown] — how many characters of the CURRENT partial
  // word have actually been typed, derived the same way the suggestion
  // engine itself derives it (`cursor - replaceRange[0]`) rather than
  // re-tokenizing the text a second time here.
  const _partialLen = $derived.by(() => {
    const start = suggestResult.replaceRange ? suggestResult.replaceRange[0] : cursorPos;
    return Math.max(0, cursorPos - start);
  });

  // Gating (PLAN item 2) — a long candidate list (metrics are the largest,
  // ~40-50 entries) is visual noise with nothing typed yet; a short list
  // (top-level keywords, a function's own param names, a short enum) is
  // exactly the "show me what's possible here" case the operator asked
  // for, so it shows immediately on 0 characters.
  // Bug fix (2026-10, operator: "after order( ... the dropdown should
  // show the keyword labels like account, and other keywords") — the
  // stated intent above ("a function's own param names... shows
  // immediately") never actually held for `order(...)`: its param list
  // has 27 entries, comfortably over the 10-item threshold meant only
  // for the genuinely huge metric/scope lists, so it silently fell into
  // the "wait for 3 chars" bucket the moment it grew past other calls'
  // smaller param sets — confirmed live (zero dropdown after `order(`,
  // despite `suggestAgentCliAt` itself correctly returning all 27 param
  // suggestions). `kind === 'param'` is now exempt from the size gate
  // entirely, matching the comment's original intent for every call's
  // own param-name list regardless of how many params it has.
  // Bug fix #2 (2026-10, same day, operator: "when [you] press into cli
  // text area, it should show the list of tokens ... similarly in
  // console cli") — the SAME class of bug hit the bare top-level
  // action/keyword list (`kind: 'token'`) on a genuinely EMPTY textarea:
  // 16 entries (order/bracket-via-WHEN/ALERT/DO/the action catalog),
  // also over the 10-item threshold, so clicking into a fresh CLI box
  // showed nothing until 3 characters were typed — confirmed live the
  // same way. Originally exempted ONLY for the true start-of-statement
  // case (`value.trim() === ''`), not every `'token'` suggestion
  // generally — a bare metric/scope-ref list midway through a longer
  // statement was deliberately left gated.
  // Bug fix #3 (2026-10-15, operator: "when WHEN is typed, the dropdown
  // should show the possible values like always and other tokens... does
  // it happen now?") — confirmed live it did NOT: `suggestAgentCliAt`
  // correctly returns `["pnl", "positions", ..., "always"]` for `"WHEN "`
  // (kind:'token'), but `value.trim() === ''` is false there (the
  // textarea isn't empty, it has "WHEN " in it), so bug #2's narrow fix
  // never covered this — any "next token not started yet" position
  // right after a keyword stayed gated exactly like the original #2 bug.
  // Widened from `value.trim() === ''` to `_partialLen === 0` — "0
  // characters typed for the upcoming token" is the real, general
  // condition the very first comment on this block already described
  // ("shows immediately on 0 characters"); the dropdown's own scrollable
  // max-height (`.agent-cli-dropdown-list`, 15rem) makes even a genuinely
  // large metric/scope list (~40-50 entries) usable here now, the same
  // way it was extended for `symbol=`'s ~90k-row list.
  const _LARGE_LIST_THRESHOLD = 10;
  const _MIN_CHARS_FOR_LARGE_LIST = 3;
  const _gatedBySize = $derived.by(() =>
    suggestResult.kind !== 'param'
    && !(suggestResult.kind === 'token' && _partialLen === 0)
    && suggestResult.suggestions.length > _LARGE_LIST_THRESHOLD && _partialLen < _MIN_CHARS_FOR_LARGE_LIST);

  // Escape dismissal is scoped to the EXACT (cursor, text) pair recorded
  // at dismissal time — see `_dismissedAt`'s own declaration above.
  const _dismissed = $derived.by(() =>
    _dismissedAt != null && _dismissedAt.pos === cursorPos && _dismissedAt.text === value);

  const _showDropdown = $derived.by(() =>
    _focused && suggestResult.suggestions.length > 0 && !_gatedBySize && !_dismissed);

  // Escape-stack coordinator (layerStack.js, same pattern Select.svelte's
  // own dropdown uses) — this dropdown is its own dismissible layer,
  // pushed/popped with `_showDropdown`. Without this, Escape would bubble
  // straight to whatever host modal AgentCliEditor is mounted inside
  // (SymbolPanel's order-ticket modal, ConsoleTab, …) and close THAT
  // instead of just dismissing the dropdown — the coordinator's
  // capture-phase `stopPropagation()` ensures only the topmost layer
  // (this dropdown, while open) reacts, never both.
  /** @type {string | null} */
  let _escapeLayerId = null;
  $effect(() => {
    if (_showDropdown) {
      _escapeLayerId = pushLayer(() => { _dismissedAt = { pos: cursorPos, text: value }; });
    } else {
      if (_escapeLayerId) { popLayer(_escapeLayerId); _escapeLayerId = null; }
      // The dropdown's own rows (and their `mouseleave`/`blur` handlers)
      // are removed from the DOM the instant this flips false — a mouse
      // (not keyboard-focus) hover doesn't get a synthetic `mouseleave`
      // on removal, so without this, `_hoverDesc` below could keep
      // showing a stale description for a row that's no longer visible.
      _hoverLabel = null;
    }
  });

  /** Sprint [CLI dropdown] auto-advance (PLAN item 4) — decides what, if
   *  anything, to auto-insert right after the literal suggestion text,
   *  based on `suggestResult.kind` as it was BEFORE this insertion (the
   *  classification of what the operator just accepted), re-using the
   *  SAME catalog-entry-lookup / open-call-detection machinery
   *  `suggestAgentCliAt` itself uses internally (`lookupAgentCliEntry` /
   *  `isInsideOpenCall`) rather than re-deriving either from scratch.
   *  Deliberately does NOT re-call `suggestAgentCliAt` here — the next
   *  render's own `suggestResult` derivation already does that at the
   *  settled cursor position, which is the whole point of driving this
   *  off ordinary reactivity instead of extending the suggest function's
   *  contract.
   *
   *  Returns the extra text to insert (possibly '') — never a closing
   *  `)`, which stays manual per the design brief. */
  // Top-level clause-head keywords / boolean connective WORDS — the only
  // `kind:'token'` candidates that are both (a) never a catalog entry and
  // (b) always followed by more text with no glue character, so (unlike
  // every OTHER bare-token candidate — a metric before its own `@`, a
  // scope-family root before `.`, a bound label before a trailing `,`)
  // they're the one case under `kind:'token'` that should ALSO get the
  // trailing space `kind:'keyword'` gets below. `_bareNameCandidates`'s
  // GIVEN/WHEN/ALERT/DO/AND/OR/NOT candidates all route through the
  // `kind:'token'` branch (merged with ordinary metric/channel/action
  // candidates — see `_suggestAgentCliAtImpl`'s branch (e)), NOT
  // `kind:'keyword'` (reserved for the clause-BOUNDARY branch (d), a
  // separate position with no other candidates to merge with).
  const _WORD_KEYWORDS = Object.freeze(['given', 'when', 'alert', 'do', 'and', 'or', 'not']);

  function _autoAdvanceExtra(kind, insertedText, preInsertText, preInsertCursor) {
    if (kind === 'param') return '=';
    if (kind === 'enum') return ', ';
    if (kind === 'token') {
      // `kind:'token'` is overloaded — it's ALSO what a free-text value
      // fill inside an open call's argument list returns (a bound label
      // standing in for an enum/string value, `_paramOrEnumSuggestion`'s
      // `eqIdx >= 0` branch) when there's no real enum list to classify
      // it as `'enum'` instead. `isInsideOpenCall` (checked against the
      // cursor position BEFORE this insertion, same position
      // `suggestResult` itself was computed from) is what disambiguates
      // that from an ordinary top-level bare-name acceptance.
      if (isInsideOpenCall(preInsertText, preInsertCursor)) return ', ';
      if (_WORD_KEYWORDS.includes(String(insertedText).toLowerCase())) return ' ';
      // Otherwise this was a top-level metric/scope-root/channel/action/
      // label/debug-arg name. Whether to auto-open `(` depends on WHICH
      // catalog section it resolves to — the compiler enforces two
      // DIFFERENT arity conventions (see `_compileMetricCallString`'s own
      // comment, agents.js ~3636): a metric/scope call is EXACT arity —
      // ANY declared param (`mean_pnl`'s `minutes`, never marked
      // `required` in the real schema) makes a bare, parenless call a
      // compile error ("'mean_pnl' requires argument(s)") — so
      // `paramKeys.length > 0` alone is the correct, sufficient signal
      // there. An action/channel call, by contrast, uses PER-KEY
      // `required` (optional params genuinely may be omitted) — real
      // `telegram` carries one optional `gate=` kwarg, and `parseCall`
      // makes a cosmetically-empty call, `telegram()`, a hard PARSE ERROR
      // ("a zero-argument token is always written bare") — so for this
      // section only `paramKeys.length > 0` would misfire, silently
      // handing the operator a guaranteed-broken statement the moment
      // they move on without filling it in. Gating action/channel calls
      // on "has at least one REQUIRED param" keeps `telegram`/`email`/
      // `log` bare (matching the worked example,
      // `ALERT msg = msg_txt, telegram`) while `order(`/`place_order(`
      // (account/symbol/side/qty all `required: true`) still auto-opens.
      const key = String(insertedText).toLowerCase();
      const isMetricOrScope = !!(catalog && ((catalog.metrics && catalog.metrics.has(key)) || (catalog.scopes && catalog.scopes.has(key))));
      const entry = lookupAgentCliEntry(insertedText, catalog);
      if (!entry) return '';
      const keys = entry.paramKeys || [];
      const shouldOpen = isMetricOrScope ? keys.length > 0 : keys.some((k) => entry.paramsSchema?.[k]?.required);
      return shouldOpen ? '(' : '';
    }
    if (kind === 'keyword') {
      // Clause-BOUNDARY keyword/connective (branch (d) — a position with
      // NO other candidates to merge with, e.g. right after a complete
      // leaf like `p <= -50000`) — never a catalog entry, so no `(` is
      // possible; auto-append a trailing space (screener.in's own
      // `selected.name + " "` convention) so the next word doesn't glue
      // onto it.
      return ' ';
    }
    return '';
  }

  async function applySuggestion(s) {
    const [start, end] = suggestResult.replaceRange || [cursorPos, cursorPos];
    const preInsertText = value;
    const preInsertCursor = cursorPos;
    const extra = _autoAdvanceExtra(suggestResult.kind, s, preInsertText, preInsertCursor);
    const before = value.slice(0, start);
    const after = value.slice(end);
    value = before + s + extra + after;
    dirty = true;
    const newCursor = start + s.length + extra.length;
    await tick();
    textareaEl?.focus();
    textareaEl?.setSelectionRange(newCursor, newCursor);
    cursorPos = newCursor;
  }

  /** Sprint [CLI dropdown] (PLAN item 3) — ArrowUp/ArrowDown move
   *  `_activeIndex` (clamped, never wraps, starts at -1 = "nothing
   *  active" — see its own declaration above); Enter/Tab accept the
   *  ACTIVE row only (`_activeIndex >= 0`), same path as a click — a bare
   *  Enter/Tab with nothing explicitly navigated-to is left to behave
   *  completely normally (newline / focus-move), even while the dropdown
   *  is showing, per the brief's own distinction between "the dropdown is
   *  open" and "a suggestion is active". Escape is handled entirely by
   *  the layerStack effect above (capture-phase, fires before this
   *  bubble-phase handler would ever see it) — not duplicated here. When
   *  the dropdown isn't showing at all (gated out, or genuinely empty),
   *  every key below is left untouched too. */
  function handleKeydown(e) {
    if (!_showDropdown) return;
    const list = suggestResult.suggestions;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      _activeIndex = Math.min(_activeIndex + 1, list.length - 1);
      return;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      _activeIndex = Math.max(_activeIndex - 1, 0);
      return;
    }
    // Tab auto-selects the top (best-match) suggestion when nothing has
    // been arrow-navigated yet (2026-10-15, operator: "the tab should
    // automatically select the matching token in the dropdown") — Tab is
    // the operator's "accept the obvious completion" key, distinct from
    // Enter (which still requires an explicit arrow-key selection, per
    // the brief's original "dropdown open" vs "suggestion active"
    // distinction above). This is also what makes the chained
    // auto-advance work for a function like `order`: applySuggestion's
    // existing `_autoAdvanceExtra('token', ...)` branch already inserts
    // `(` for a call with required params, and the next keystroke's
    // reactive `suggestResult` recompute (cursor now inside an open call)
    // already yields kind:'param' — that chain was always wired, it just
    // never fired because Tab previously did nothing without a prior
    // arrow-key press.
    if (e.key === 'Tab' && _activeIndex < 0 && list.length) {
      e.preventDefault();
      applySuggestion(list[0]);
      return;
    }
    if (e.key === 'Enter' || e.key === 'Tab') {
      if (_activeIndex < 0) return; // nothing active — let the key behave normally
      const s = list[_activeIndex];
      if (s == null) return;
      e.preventDefault();
      applySuggestion(s);
    }
  }

  function handlePlaceOrder() {
    if (!compiled?.ok) return;
    // Sprint [cancel-all-cli] (2026-10) — `cancel_all_orders(...)` reuses
    // this SAME submit button + `onOrderSubmit` prop plumbing rather than
    // a parallel button/prop; the host (OrderCliTab/ConsoleTab) branches
    // on `compiled.kind` to decide whether to call the ticket/basket
    // order-placement path or the new `/api/orders/cancel-all` route.
    if (compiled.kind === 'order' || compiled.kind === 'cancel_all_orders') onOrderSubmit?.(compiled);
  }

  // Sprint 20 (item 5) — visual position indicator for a compile/parse
  // error that carries a real `position` (an ever-growing share of them,
  // after this sprint's `_err()` position-threading work). `\n` is
  // swapped 1-for-1 for `⏎` so the caret's character-offset alignment
  // under the snippet stays correct even when the error sits near a
  // line break in a multi-line statement (`describeErrorPosition` itself
  // stays UI-agnostic — this swap is presentation-only, done here).
  function _posIndicator(err) {
    if (typeof err.position !== 'number') return null;
    const d = describeErrorPosition(value, err.position);
    if (!d) return null;
    return { ...d, snippet: d.snippet.replace(/\n/g, '⏎') };
  }

  // Description of whichever suggestion pill is currently hovered/focused,
  // or '' when nothing is hovered or the hovered suggestion has no
  // backend-provided description (e.g. an enum value, or an undescribed
  // token/param) — callers never render an empty detail box for ''.
  const _hoverDesc = $derived.by(() => {
    if (_hoverLabel == null) return '';
    const i = suggestResult.suggestions.indexOf(_hoverLabel);
    return i >= 0 ? (suggestResult.descriptions?.[i] || '') : '';
  });

  // Sprint 14 — previously returned null for 'event'/'bracket'/'token',
  // so the JSON preview silently stayed blank for those compile kinds
  // even though they'd compiled successfully (only 'agent'/'order' had a
  // preview). Now uniform across every compile kind this editor can
  // produce.
  const previewPayload = $derived.by(() => {
    if (!compiled?.ok) return null;
    if (compiled.kind === 'agent' || compiled.kind === 'event') return compiled.agent;
    if (compiled.kind === 'order') return compiled.ticket || { groups: compiled.basket?.groups };
    if (compiled.kind === 'bracket') return compiled.template;
    if (compiled.kind === 'token') return compiled.fragment;
    // Sprint [cancel-all-cli] — shows the resolved scope/account/symbol/
    // side the operator is about to submit; this preview pane IS the
    // confirm step for a destructive fleet-wide cancel (no separate
    // confirm modal — see the compile function's own doc).
    if (compiled.kind === 'cancel_all_orders') return compiled.cancelAll?.params || null;
    return null;
  });

  // Sprint C (2026-10, Phase 2 "Automation redesign") — the "Generated
  // statement(s)" section, item 4 of the result-area redesign. Re-runs the
  // compiled JSON back through the matching DECOMPILER so the operator
  // sees the canonical, normalized CLI text the compiler actually resolved
  // to (e.g. `mean_pnl(minutes=30)` -> `mean_pnl(30)` normalization),
  // never just the raw text they typed. For `kind:'order'` there is no
  // decompiler at all (a bare order/basket statement has no agent/bracket
  // JSON shape to decompile back FROM) — shown instead as a compact
  // `order(...)`-shaped SUMMARY of each leg that will actually be
  // submitted, built locally here rather than inventing a real order
  // decompiler for a display-only purpose. A basket renders one summary
  // line PER LEG, not one combined blob, per the redesign's own
  // instruction. Best-effort: any decompile failure here silently yields
  // no canonical-text section (the JSON preview below still renders) —
  // this is a display nicety, never a gate on submit.
  function _legSummaryText(/** @type {any} */ leg) {
    if (!leg) return '';
    const side = leg.side || leg.transaction_type || '?';
    const parts = [`account=${JSON.stringify(leg.account ?? '')}`, `symbol=${JSON.stringify(leg.symbol ?? '')}`, `side=${side}`];
    if (leg.lots != null) parts.push(`lots=${leg.lots}`);
    else if (leg.qty != null) parts.push(`qty=${leg.qty}`);
    if (leg.price != null) parts.push(`price=${leg.price}`);
    if (leg.order_type != null) parts.push(`order_type=${leg.order_type}`);
    return `order(${parts.join(', ')})`;
  }

  const canonicalLines = $derived.by(() => {
    if (!compiled?.ok || !catalog) return null;
    try {
      if (compiled.kind === 'agent') {
        const r = decompileAgent(compiled.agent, catalog, { lotSizeOf });
        return r.text ? [r.text] : null;
      }
      if (compiled.kind === 'event') {
        const r = decompileEventAgent(compiled.agent, catalog, { lotSizeOf });
        return r.text ? [r.text] : null;
      }
      if (compiled.kind === 'bracket') {
        const r = decompileBracket(compiled.template, catalog, { lotSizeOf });
        return r.text ? [r.text] : null;
      }
      if (compiled.kind === 'token') {
        const r = decompileToken(compiled.fragment, catalog, { lotSizeOf });
        return r.text ? [r.text] : null;
      }
      if (compiled.kind === 'order') {
        if (compiled.ticket) return [_legSummaryText(compiled.ticket)];
        if (compiled.basket) {
          const lines = (compiled.basket.groups || []).flatMap((g) => (g.legs || []).map(_legSummaryText));
          return lines.length ? lines : null;
        }
      }
      if (compiled.kind === 'cancel_all_orders') {
        const p = compiled.cancelAll?.params || {};
        const parts = [`scope=${JSON.stringify(p.scope ?? 'total')}`];
        if (p.account != null) parts.push(`account=${JSON.stringify(p.account)}`);
        if (p.symbol != null) parts.push(`symbol=${JSON.stringify(p.symbol)}`);
        if (p.side != null) parts.push(`side=${p.side}`);
        return [`cancel_all_orders(${parts.join(', ')})`];
      }
    } catch { /* display-only — never block the JSON preview on this */ }
    return null;
  });
</script>

<div class="agent-cli-editor">
  <div class="agent-cli-input-wrap">
    <textarea
      bind:this={textareaEl}
      bind:value
      oninput={handleInput}
      onclick={_updateCursor}
      onkeyup={_updateCursor}
      onfocus={() => { _focused = true; _updateCursor(); }}
      onblur={() => { _focused = false; }}
      onkeydown={handleKeydown}
      class="agent-cli-textarea field-input font-mono text-[length:var(--fs-sm)]"
      rows="6"
      spellcheck="false"
      {placeholder}
    ></textarea>

    {#if _showDropdown}
      <div class="agent-cli-dropdown" role="listbox" aria-label="CLI suggestions" data-testid="agent-cli-dropdown">
        <div class="agent-cli-dropdown-list">
          {#each suggestResult.suggestions as s, i (s)}
            <!-- Sprint 15 — `labels[i]` (set only for a 'column' suggestion,
                 e.g. "P&L (₹)") is shown INSTEAD of the literal insertable
                 text `s` (e.g. "positions.total.pnl") when present — the
                 operator reads the short label, but `applySuggestion` below
                 always inserts the real `s` string, never the label.
                 `onmousedown|preventDefault` keeps the TEXTAREA focused
                 through the whole click (the button never steals focus),
                 so `_focused` never flips false and `_showDropdown` never
                 collapses out from under the click before `onclick` fires. -->
            <button type="button" class="agent-cli-dropdown-row" class:is-active={i === _displayActiveIndex}
                    role="option" aria-selected={i === _displayActiveIndex}
                    data-testid="agent-cli-dropdown-row"
                    onmousedown={(e) => e.preventDefault()}
                    onclick={() => applySuggestion(s)}
                    onmouseenter={() => { _hoverLabel = s; _activeIndex = i; }}
                    onmouseleave={() => { _hoverLabel = null; }}
                    onfocus={() => { _hoverLabel = s; _activeIndex = i; }}
                    onblur={() => { _hoverLabel = null; }}>{suggestResult.labels?.[i] || s}</button>
          {/each}
        </div>
        {#if _hoverDesc}
          <!-- Moved INSIDE .agent-cli-dropdown (2026-10 CardHeader/grid
               consistency pass, Part E) — previously rendered in normal
               document flow right after .agent-cli-input-wrap closed, which
               shifted the compile-errors/JSON-preview area below it every
               time a suggestion row was hovered/focused. .agent-cli-dropdown
               is already position:absolute (does not affect layout), and
               this description now lives as a sibling to the scrollable
               .agent-cli-dropdown-list — outside that list's own
               max-height/overflow-y, so it's never clipped by the list's
               scroll area and always stays visible as a footer row within
               the same overlay. -->
          <div class="agent-cli-suggest-desc">{_hoverDesc}</div>
        {/if}
      </div>
    {/if}
  </div>

  <!-- Sprint C (2026-10, Phase 2 "Automation redesign") — the result area
       now holds up to four distinct sections: (1) compile errors [1] —
       unchanged markup/classes, existing specs select on them; (2) info/
       hint lines [1] (editing-mode banner, future normalization notes);
       (3) the compiled JSON preview + newly-added canonical generated-
       statement text [1]; (4) the post-SUBMIT (backend call) result,
       distinct from client-side compile errors above. Wrapped in ONE
       scrollable container so a long statement's full result never
       pushes the rest of the page down — same `max-height` + `overflow-y:
       auto` convention /automation's own `.agent-group-grid`/
       `.automation-mini-grid` scroll caps already use. -->
  <div class="agent-cli-result">
    {#if info && info.length}
      <div class="agent-cli-info">
        {#each info as line}<div class="agent-cli-info-line">{line}</div>{/each}
      </div>
    {/if}

    {#if compiled && !compiled.ok && showCompileErrors && !_suppressErrorsWhileOpenCall && !_suppressMissingAlertOrDo}
      <div class="agent-cli-errors">
        <div class="agent-cli-errors-title">Errors:</div>
        <ul class="list-disc ml-4">
          {#each compiled.errors as err}
            <li>
              {err.message}{typeof err.position === 'number' ? ` (line ${_posIndicator(err)?.line}, col ${_posIndicator(err)?.column})` : ''}
              {#if _posIndicator(err)}
                {@const p = _posIndicator(err)}
                <div class="agent-cli-error-pos">
                  <div class="agent-cli-error-pos-snippet">{p.truncatedStart ? '…' : ''}{p.snippet}{p.truncatedEnd ? '…' : ''}</div>
                  <div class="agent-cli-error-pos-caret" style="--caret-col: {p.caretOffset + (p.truncatedStart ? 1 : 0)}">^</div>
                </div>
              {/if}
            </li>
          {/each}
        </ul>
      </div>
    {:else if previewPayload != null}
      <div class="agent-cli-preview">
        {#if canonicalLines?.length}
          <div class="agent-cli-canonical">
            <div class="agent-cli-canonical-title">Generated statement{canonicalLines.length > 1 ? 's' : ''}:</div>
            {#each canonicalLines as line, i (i)}
              <pre class="agent-cli-canonical-line">{line}</pre>
            {/each}
          </div>
        {/if}
        <pre class="agent-cli-preview-json">{JSON.stringify(previewPayload, null, 2)}</pre>
        {#if compiled?.kind === 'order'}
          <button type="button" class="btn-primary agent-cli-place-order" onclick={handlePlaceOrder}>
            {#if compiled.ticket}
              <!-- Single-leg ticket: `mode` IS the real client-requested
                   field (server still gates it against the kill-switch). -->
              Place order ({mode === 'live' ? 'LIVE' : 'PAPER'})
            {:else}
              <!-- Multi-leg basket: BasketOrderRequest has no client mode
                   field at all — resolved server-side, unknown here. -->
              Place basket order
            {/if}
          </button>
        {:else if compiled?.kind === 'cancel_all_orders'}
          <!-- Sprint [cancel-all-cli] (2026-10) — SAME button element,
               reusing `agent-cli-place-order` + `handlePlaceOrder`, per
               the brief ("don't add a parallel button"). No "/PAPER"
               suffix: `POST /api/orders/cancel-all` is live-only (no
               mode param, never routes through PaperTradeEngine — see
               `compileCancelAllOrdersStmt`'s own doc), and the compiler
               already refuses this statement outright in paper mode, so
               this button only ever renders once that gate has passed.
               `--close-btn-danger-bg`/`--c-short` (destructive-action
               tokens, same family PositionStrip's own close-position
               button uses) signal this is a destructive cancel, distinct
               from the green `btn-primary` order-placement button. -->
          <button type="button" class="agent-cli-place-order agent-cli-cancel-all" onclick={handlePlaceOrder}>
            Cancel orders (LIVE)
          </button>
        {/if}
      </div>
    {/if}

    {#if submitStatus}
      <div class="agent-cli-submit-status" class:is-error={!submitStatus.ok}>{submitStatus.text}</div>
    {/if}
  </div>
</div>

<style>
  .agent-cli-editor { display: flex; flex-direction: column; gap: 0.4rem; }
  /* Sprint C — scrolls internally (never grows unbounded) once a long
     decompiled/typed statement exceeds the visible rows — same
     overflow-y: auto convention the result area below uses. */
  .agent-cli-textarea { width: 100%; resize: vertical; overflow-y: auto; }
  /* Sprint [CLI dropdown] (2026-10) — positioning context for the
     fixed-position (relative-to-this-wrapper) dropdown panel below;
     replaces the old below-textarea `.agent-cli-suggest-row` pill list
     entirely (removed, not kept alongside this). */
  .agent-cli-input-wrap { position: relative; }
  /* Screener.in-style lookahead dropdown — anchored directly below the
     textarea via `position: absolute` on THIS wrapper (not caret-pixel-
     tracked, deliberate scope decision per .claude/PLAN.md), full width,
     real rows (not pills) so a long candidate list (metrics top out
     around 40-50 entries) scrolls properly instead of wrapping into an
     unbounded pill grid. Chrome mirrors Select.svelte's own dropdown
     panel (`.rbq-select-panel`) for visual consistency with the rest of
     the app's dropdown affordances. */
  .agent-cli-dropdown {
    position: absolute;
    top: 100%;
    left: 0;
    right: 0;
    margin-top: 0.2rem;
    z-index: var(--z-dropdown);
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    border: 1.5px solid rgba(251,191,36,0.35);
    border-radius: 4px;
    box-shadow: 0 10px 28px rgba(0,0,0,0.55), inset 0 1px 0 rgba(255,255,255,0.06);
    /* overflow: hidden (not scrolling itself — see .agent-cli-dropdown-list
       below) so the rounded corners stay clean; the panel's own height is
       now driven by its content (list + optional description footer),
       not fixed, since only the list portion is height-capped. */
    overflow: hidden;
  }
  /* Part E (2026-10 CardHeader/grid consistency pass) — the
     max-height/overflow-y that used to live on .agent-cli-dropdown
     itself moved down to just this row-list wrapper, so the
     description footer below (.agent-cli-suggest-desc) sits OUTSIDE
     the scrollable area and is never clipped/scrolled away — it's a
     flex sibling, always visible whenever `_hoverDesc` is set. */
  .agent-cli-dropdown-list {
    max-height: 15rem;
    overflow-y: auto;
  }
  .agent-cli-dropdown-row {
    display: block;
    width: 100%;
    text-align: left;
    padding: 0.3rem 0.55rem;
    font-size: var(--fs-sm);
    font-family: var(--font-mono, monospace);
    color: var(--algo-slate);
    background: none;
    border: none;
    border-left: 2px solid transparent;
    cursor: pointer;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .agent-cli-dropdown-row:hover,
  .agent-cli-dropdown-row.is-active {
    background: rgba(251,191,36,0.15);
    color: var(--c-action);
    border-left-color: var(--c-action);
  }
  .agent-cli-suggest-desc {
    font-size: var(--fs-xs);
    color: var(--algo-muted);
    /* Footer row within .agent-cli-dropdown (Part E, moved from normal
       document flow below the input — see template comment above). A
       top border + background separate it visually from the row list
       above it, matching the dropdown's own amber-accent border family. */
    padding: 0.35rem 0.55rem;
    border-top: 1px solid rgba(251,191,36,0.20);
    background: rgba(15,23,42,0.35);
    /* Sprint 15 — a param's description can carry a generated
       required_if/mutually-exclusive line appended on its own newline
       (see _paramConstraintText in agents.js) — preserve it. */
    white-space: pre-line;
  }
  .agent-cli-errors {
    padding: 0.4rem 0.6rem; border-radius: 0.3rem;
    background: var(--algo-red-bg-mid);
    border: 1px solid var(--algo-red-border-soft);
    color: var(--algo-red-text-bright);
    font-size: var(--fs-sm);
  }
  .agent-cli-errors-title { font-weight: 600; margin-bottom: 0.2rem; }
  /* Sprint C — the shared result container: errors / info / preview /
     submit-status sections all live inside this one scrollable box so
     a long result never pushes the rest of the host page down. Modest
     height bump over the old single-section layout (was implicitly
     capped at .agent-cli-preview-json's own 220px) since up to 4
     sections can now stack; scrollbar only appears once content
     actually overflows. */
  .agent-cli-result {
    display: flex; flex-direction: column; gap: 0.4rem;
    max-height: 26rem;
    overflow-y: auto;
  }
  .agent-cli-info {
    padding: 0.35rem 0.6rem; border-radius: 0.3rem;
    background: var(--algo-sky-bg-soft);
    border: 1px solid var(--algo-sky-border-soft);
    color: var(--algo-sky);
    font-size: var(--fs-sm);
  }
  .agent-cli-info-line + .agent-cli-info-line { margin-top: 0.15rem; }
  .agent-cli-canonical {
    margin-bottom: 0.4rem;
    padding-bottom: 0.4rem;
    border-bottom: 1px dashed var(--algo-green-border-soft);
  }
  .agent-cli-canonical-title {
    font-weight: 600;
    font-size: var(--fs-xs);
    color: var(--algo-muted);
    margin-bottom: 0.15rem;
  }
  .agent-cli-canonical-line {
    font-family: var(--font-mono, monospace);
    font-size: var(--fs-sm);
    white-space: pre-wrap;
    margin: 0 0 0.2rem 0;
  }
  .agent-cli-submit-status {
    padding: 0.35rem 0.6rem; border-radius: 0.3rem;
    background: var(--algo-green-bg);
    border: 1px solid var(--algo-green-border-soft);
    color: var(--c-long, #4ade80);
    font-size: var(--fs-sm);
    font-weight: 600;
  }
  .agent-cli-submit-status.is-error {
    background: var(--algo-red-bg-mid);
    border-color: var(--algo-red-border-soft);
    color: var(--algo-red-text-bright);
  }
  /* Sprint 20 (item 5) — visual position indicator: a horizontally-
     scrollable monospace snippet window with a `^` caret aligned under
     the exact error offset via a `ch`-unit margin (one `ch` ≈ one
     monospace character width, so the caret lines up with the snippet
     above regardless of the surrounding font-size) — `overflow-x: auto`
     keeps this readable on a <600px phone viewport without wrapping
     and losing caret alignment. */
  .agent-cli-error-pos {
    margin: 0.15rem 0 0.3rem 0;
    overflow-x: auto;
    font-family: var(--font-mono, monospace);
    font-size: var(--fs-xs);
  }
  .agent-cli-error-pos-snippet { white-space: pre; }
  .agent-cli-error-pos-caret { white-space: pre; margin-left: calc(var(--caret-col) * 1ch); color: var(--algo-amber); }
  .agent-cli-preview {
    padding: 0.4rem 0.6rem; border-radius: 0.3rem;
    background: var(--algo-green-bg);
    border: 1px solid var(--algo-green-border-soft);
  }
  .agent-cli-preview-json {
    font-family: var(--font-mono, monospace);
    font-size: var(--fs-sm);
    white-space: pre-wrap;
    margin: 0 0 0.4rem 0;
    /* Sprint C — no own max-height/overflow here anymore: the outer
       `.agent-cli-result` is now the single scroll container (avoids a
       nested-scrollbar-inside-a-scrollbar layout). */
  }
  .agent-cli-place-order { font-size: var(--fs-md); padding: 0.25rem 0.9rem; }
  /* Sprint [cancel-all-cli] (2026-10) — destructive-action chrome,
     same token family as PositionStrip's `.ps-bd-close` live-close
     button (border/color/background all from the shared
     --close-btn-danger-* ramp, never a one-off literal). Overrides
     `.btn-primary`'s green chrome since this button does NOT use that
     class (see template — it's a sibling, not a `btn-primary` variant). */
  .agent-cli-cancel-all {
    border: 1px solid var(--algo-red-border-soft);
    color: var(--c-short);
    background: var(--close-btn-danger-bg);
    border-radius: 4px;
    font-weight: 600;
    transition: background 0.1s;
  }
  .agent-cli-cancel-all:hover { background: var(--close-btn-danger-bg-hover); }
</style>
