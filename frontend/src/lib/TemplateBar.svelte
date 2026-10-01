<script>
  import Select from '$lib/Select.svelte';

  /**
   * TemplateBar — "On fill" template pick + param override row.
   *
   * Renders a compact ON/OFF toggle pill (2026-09-30 — was a Default /
   * None / every-named-template dropdown; before that a two-pill
   * toggle). ON always resolves to the side-aware default template
   * (`sideAwareDefault`, via `onSelectDefault`) — the operator never
   * "remembers" a previously picked named template by toggling on;
   * OFF is the explicit "None — entry only" state. A specific named
   * template can still be picked via the compact picker inside the
   * expand panel (`onSelectTemplate`), alongside the TP% / SL% / Wing
   * parameter override inputs.
   *
   * The outer shell row <div> and the on-fill preview / cap-warn section
   * are intentionally kept in the parent (SymbolPanel) because they depend
   * on many additional parent-only state variables.
   *
   * @prop {object|null}  selectedTemplate          - current resolved template object (read-only display)
   * @prop {object|null}  sideAwareDefault          - side-aware default template (null → "Default" resolves to nothing)
   * @prop {any[]}        nonNoneTemplates          - every active template except the seeded "none" sentinel, for the dropdown's named options
   * @prop {boolean}      showsWing                 - whether wing fields are visible
   * @prop {boolean}      shellUsingNone            - whether "None" is currently selected
   * @prop {number|''}    tpOverride                - TP% override value ($bindable)
   * @prop {number|''}    slOverride                - SL% override value ($bindable)
   * @prop {number|''}    wingStrikeOffsetOverride  - Wing strike offset override ($bindable)
   * @prop {number|''}    wingPremPctOverride       - Wing premium % override ($bindable)
   * @prop {number|''}    spreadMaxPctOverride      - Spread% threshold override ($bindable, Chain-only pre-submit gate)
   * @prop {number|''}    slTrailPctOverride        - Trailing stop % override ($bindable, #30)
   * @prop {'LIMIT'|'MARKET'|''}  tpOrderTypeOverride  - TP order type override ($bindable, #30)
   * @prop {string}       tpScalesJsonOverride      - Scale-out JSON override ($bindable, #30)
   * @prop {() => void}   onSelectDefault           - called when operator picks "Default" from the dropdown
   * @prop {() => void}   onSelectNone              - called when operator picks "None" from the dropdown
   * @prop {(id: number) => void} onSelectTemplate  - called when operator picks a specific named template by id
   */
  let {
    selectedTemplate,
    sideAwareDefault,
    nonNoneTemplates = /** @type {any[]} */ ([]),
    showsWing,
    shellUsingNone,
    tpOverride       = $bindable(),
    slOverride       = $bindable(),
    wingStrikeOffsetOverride = $bindable(),
    wingPremPctOverride      = $bindable(),
    spreadMaxPctOverride     = $bindable(),
    slTrailPctOverride       = $bindable(),
    tpOrderTypeOverride      = $bindable(),
    tpScalesJsonOverride     = $bindable(),
    onSelectDefault,
    onSelectNone,
    onSelectTemplate,
  } = $props();

  // ON/OFF toggle state. ON only when the shell isn't explicitly on
  // "None" AND a concrete template is actually resolved — guards the
  // window right after mount where `_sharedTemplateId` is still null
  // (OrderTicket no longer auto-selects, per the Ticket/Chain
  // severance fix) so the toggle never reads "ON" while nothing is
  // actually armed to attach on fill.
  const _toggleOn = $derived(!shellUsingNone && !!selectedTemplate);
  // Button is ALWAYS clickable (2026-09-30, operator: "make Templ
  // always clickable/enabled — never visually disabled"). Clicking to
  // activate with no side-aware default configured is a silent no-op
  // (onSelectDefault() itself no-ops without one) — kept as a plain
  // no-op rather than a disabled affordance; the title still explains
  // the state via `_toggleOnLabel` below.
  const _toggleOnLabel = $derived(
    _toggleOn
      ? (selectedTemplate.name || selectedTemplate.slug || 'Default')
      : (sideAwareDefault ? (sideAwareDefault.name || sideAwareDefault.slug) : 'Default')
  );

  // Expand/collapse state (#30) — persists within session; resets when
  // the parent clears selectedTemplate (i.e. on modal close/symbol change).
  let _expanded = $state(false);

  // Inline validation (#7) — derived from override values.
  // Returns an error string when the override is out-of-range, '' when valid.
  const _tpErr = $derived.by(() => {
    if (shellUsingNone || !selectedTemplate) return '';
    if (tpOverride !== '' && tpOverride != null && Number(tpOverride) <= 0) return 'TP% must be > 0';
    return '';
  });
  const _slErr = $derived.by(() => {
    if (shellUsingNone || !selectedTemplate) return '';
    if (slOverride !== '' && slOverride != null && Number(slOverride) <= 0) return 'SL% must be > 0';
    return '';
  });
  const _wingPremErr = $derived.by(() => {
    if (shellUsingNone || !selectedTemplate) return '';
    if (wingPremPctOverride !== '' && wingPremPctOverride != null && Number(wingPremPctOverride) <= 0) return 'Wing prem% must be > 0';
    return '';
  });
  // Spread% threshold — Chain-tab-only pre-submit gate (OptionChainTab
  // checks the original leg + any offset/wing leg's live bid/ask spread
  // against this threshold before submitting). Rendered alongside TP%/
  // SL% (not gated on showsWing) so the operator always sees/can tune
  // the threshold that WOULD apply if a wing gets attached.
  const _spreadErr = $derived.by(() => {
    if (shellUsingNone || !selectedTemplate) return '';
    if (spreadMaxPctOverride !== '' && spreadMaxPctOverride != null && Number(spreadMaxPctOverride) <= 0) return 'Spread% must be > 0';
    return '';
  });

  // Non-blocking cross-check (#7) — warn when TP% < SL% (exits may overlap).
  const _tpSlWarn = $derived.by(() => {
    if (shellUsingNone || !selectedTemplate) return '';
    const tp = tpOverride !== '' && tpOverride != null ? Number(tpOverride)
             : selectedTemplate.tp_pct != null ? Number(selectedTemplate.tp_pct) : null;
    const sl = slOverride !== '' && slOverride != null ? Number(slOverride)
             : selectedTemplate.sl_pct != null ? Number(selectedTemplate.sl_pct) : null;
    if (tp != null && sl != null && tp < sl) return 'TP% < SL% — exits may overlap';
    return '';
  });

  // #30 — scales JSON inline validator (expanded panel only)
  const _scalesErr = $derived.by(() => {
    if (!tpScalesJsonOverride?.trim()) return '';
    try {
      const arr = JSON.parse(tpScalesJsonOverride);
      if (!Array.isArray(arr)) return 'Must be a JSON array';
      let sumClose = 0;
      for (let i = 0; i < arr.length; i++) {
        const e = arr[i];
        if (!(e.at_pct > 0)) return `Entry ${i + 1}: at_pct must be > 0`;
        if (!(e.close_pct > 0 && e.close_pct <= 100)) return `Entry ${i + 1}: close_pct must be 1–100`;
        sumClose += Number(e.close_pct);
      }
      if (sumClose > 100) return `Sum of close_pct is ${sumClose}% — must be ≤ 100`;
      return '';
    } catch (e) {
      return /** @type {Error} */ (e).message;
    }
  });

  // Asterisk helpers (#30) — show * when override differs from template default
  const _tpAsterisk = $derived(
    selectedTemplate && tpOverride !== '' && tpOverride != null &&
    String(Number(tpOverride)) !== String(selectedTemplate.tp_pct)
  );
  const _slAsterisk = $derived(
    selectedTemplate && slOverride !== '' && slOverride != null &&
    String(Number(slOverride)) !== String(selectedTemplate.sl_pct)
  );
  const _wingStrikeAsterisk = $derived(
    selectedTemplate && wingStrikeOffsetOverride !== '' && wingStrikeOffsetOverride != null &&
    String(Number(wingStrikeOffsetOverride)) !== String(selectedTemplate.wing_strike_offset)
  );
  const _wingPremAsterisk = $derived(
    selectedTemplate && wingPremPctOverride !== '' && wingPremPctOverride != null &&
    String(Number(wingPremPctOverride)) !== String(selectedTemplate.wing_premium_pct)
  );
  // Default placeholder: per-template override once the backend field
  // lands (`wing_max_spread_pct` — same name as the existing global
  // `templates.wing_max_spread_pct` admin setting it overrides), else
  // the global setting's own default value (0.5).
  const _spreadDefault = $derived(selectedTemplate?.wing_max_spread_pct ?? 0.5);
  const _spreadAsterisk = $derived(
    selectedTemplate && spreadMaxPctOverride !== '' && spreadMaxPctOverride != null &&
    String(Number(spreadMaxPctOverride)) !== String(_spreadDefault)
  );
  const _trailAsterisk = $derived(
    selectedTemplate && slTrailPctOverride !== '' && slTrailPctOverride != null &&
    String(Number(slTrailPctOverride)) !== String(selectedTemplate.sl_trail_pct)
  );
  const _tpTypeAsterisk = $derived(
    selectedTemplate && tpOrderTypeOverride !== '' && tpOrderTypeOverride != null &&
    tpOrderTypeOverride !== selectedTemplate.tp_order_type
  );

  function _resetToDefaults() {
    tpOverride = '';
    slOverride = '';
    wingStrikeOffsetOverride = '';
    wingPremPctOverride = '';
    spreadMaxPctOverride = '';
    slTrailPctOverride = '';
    tpOrderTypeOverride = '';
    tpScalesJsonOverride = '';
  }
</script>

<span class="oes-basket-tpl-pick">
  <!-- Single toggle button (2026-09-30 — operator: "make Templ look like
       a button which can be active or inactive based on button press,
       default active"). RE-WIRED to the real logic (2026-09-30, same
       day) after the actual root cause was found and fixed: a bug in
       loadOrderTemplates() (frontend/src/lib/data/templates.js) that
       permanently cached a transient fetch failure as an empty template
       list for the rest of the browser tab's session — unrelated to
       this component's own logic, which a temporary debug bypass had
       correctly ruled out first. Active (amber-filled) = template
       attach ON, resolves to the side-aware default. Inactive (dim,
       slate) = None — entry only. Default state on mount is active
       whenever a side-aware default resolves (see _toggleOn above). -->
  <button type="button"
          class="oes-tpl-button"
          class:active={_toggleOn}
          title={_toggleOn
            ? (selectedTemplate.description || `Attached: ${_toggleOnLabel}`)
            : 'No template — entry only, no TP/SL/Wing attach (click to attach the side-aware default)'}
          onclick={() => { if (_toggleOn) { onSelectNone?.(); } else { onSelectDefault?.(); } }}>
    <span class="oes-tpl-button-dot" aria-hidden="true"></span>
    Template
  </button>
  {#if !shellUsingNone && selectedTemplate}
    <!-- #30 expand toggle — reveals the full param set.
         Operator (2026-09-30): "template expand button look too
         small. make it either big or use ^ and inverted ^" — the old
         ▴/▾ glyphs at --fs-xs (0.55rem ≈ 9px) read as barely-visible
         specks.
         Follow-up (2026-09-30, same day): "make cap and reverse cap
         symbols after templated should be better looking visible
         symbols" — a literal "^" text character rendered thin, small,
         and font-dependent (no reliable crisp "caret" glyph across
         fonts). Replaced with a pure-CSS chevron (.oes-tpl-expand-icon
         below — two border edges meeting at a corner, rotated), which
         renders identically and crisply in every browser regardless
         of font, and scales cleanly with border-width instead of
         relying on glyph hinting. Rotated 180deg via the
         .oes-tpl-expand-open class when expanded (inverted chevron =
         pointing up = "click to collapse"). -->
    <button type="button"
            class="oes-tpl-expand-btn"
            class:oes-tpl-expand-open={_expanded}
            title={_expanded ? 'Collapse template params' : 'Expand all template params'}
            onclick={() => { _expanded = !_expanded; }}>
      <span class="oes-tpl-expand-icon" aria-hidden="true"></span>
    </button>
  {/if}
</span>
{#if !shellUsingNone && selectedTemplate}
  <div class="oes-basket-tpl-params">
    <!-- TP% override -->
    <label class="oes-basket-tpl-param {_tpErr ? 'oes-tpl-param-err' : ''}"
           title="Take-profit % above (BUY) or below (SELL) the fill price.">
      <span>TP %{_tpAsterisk ? '*' : ''}</span>
      <input type="number" step="0.5"
        class:oes-tpl-input-err={!!_tpErr}
        placeholder={selectedTemplate.tp_pct != null ? String(selectedTemplate.tp_pct) : '—'}
        bind:value={tpOverride} />
    </label>
    <!-- SL% override -->
    <label class="oes-basket-tpl-param {_slErr ? 'oes-tpl-param-err' : ''}"
           title="Stop-loss % opposite the TP side.">
      <span>SL %{_slAsterisk ? '*' : ''}</span>
      <input type="number" step="0.5"
        class:oes-tpl-input-err={!!_slErr}
        placeholder={selectedTemplate.sl_pct != null ? String(selectedTemplate.sl_pct) : '—'}
        bind:value={slOverride} />
    </label>
    <!-- Spread% override — Chain tab's pre-submit gate threshold. Shown
         alongside TP%/SL% (not gated on showsWing) so the operator can
         see/tune the threshold regardless of whether a wing is
         currently configured; OptionChainTab only RUNS the gate when a
         wing/offset leg actually resolves. -->
    <label class="oes-basket-tpl-param {_spreadErr ? 'oes-tpl-param-err' : ''}"
           title="Max bid/ask spread % allowed on the original + offset leg before Chain submit warns.">
      <span>Spread %{_spreadAsterisk ? '*' : ''}</span>
      <input type="number" step="0.5"
        class:oes-tpl-input-err={!!_spreadErr}
        placeholder={String(_spreadDefault)}
        bind:value={spreadMaxPctOverride} />
    </label>
    {#if showsWing}
      <label class="oes-basket-tpl-param {_wingPremErr ? 'oes-tpl-param-err' : ''}"
             title="Protective wing BUY at this many strikes away from the parent.">
        <span>Wing strike+{_wingStrikeAsterisk ? '*' : ''}</span>
        <input type="number" step="50"
          placeholder={selectedTemplate.wing_strike_offset != null ? String(selectedTemplate.wing_strike_offset) : '—'}
          bind:value={wingStrikeOffsetOverride} />
      </label>
      <label class="oes-basket-tpl-param {_wingPremErr ? 'oes-tpl-param-err' : ''}"
             title="Wing premium target as a % of the parent's premium.">
        <span>Wing prem%{_wingPremAsterisk ? '*' : ''}</span>
        <input type="number" step="0.5"
          class:oes-tpl-input-err={!!_wingPremErr}
          placeholder={selectedTemplate.wing_premium_pct != null ? String(selectedTemplate.wing_premium_pct) : '—'}
          bind:value={wingPremPctOverride} />
      </label>
    {/if}
  </div>

  <!-- Inline validation errors (#7) -->
  {#if _tpErr || _slErr || _wingPremErr || _spreadErr}
    <div class="oes-tpl-errors">
      {#if _tpErr}<span class="oes-tpl-err-chip">{_tpErr}</span>{/if}
      {#if _slErr}<span class="oes-tpl-err-chip">{_slErr}</span>{/if}
      {#if _wingPremErr}<span class="oes-tpl-err-chip">{_wingPremErr}</span>{/if}
      {#if _spreadErr}<span class="oes-tpl-err-chip">{_spreadErr}</span>{/if}
    </div>
  {/if}
  <!-- TP% < SL% cross-check warning (non-blocking) -->
  {#if _tpSlWarn && !_tpErr && !_slErr}
    <div class="oes-tpl-errors">
      <span class="oes-tpl-warn-chip">{_tpSlWarn}</span>
    </div>
  {/if}

  <!-- #30 Expanded panel — full param set -->
  {#if _expanded}
    <div class="oes-tpl-expanded">
      <!-- Specific-template picker (2026-09-30) — the toggle above only
           ever resolves ON to the side-aware Default; this lets the
           operator explicitly override with one SPECIFIC named
           template instead, same options shape the old primary
           dropdown built from `nonNoneTemplates` (Default/None rows
           dropped — those are the toggle itself now). -->
      {#if nonNoneTemplates.length > 0}
        <label class="oes-tpl-pick-specific" title="Pick a specific named template instead of the side-aware default.">
          <span class="oes-basket-tpl-param-label">Specific tmpl</span>
          <span class="oes-tpl-pick-specific-select">
            <Select
              value={selectedTemplate ? String(selectedTemplate.id) : ''}
              options={nonNoneTemplates.map(t => ({ value: String(t.id), label: t.name || t.slug || `#${t.id}` }))}
              ariaLabel="Pick specific template"
              placeholder="Choose…"
              onValueChange={(v) => { if (v) onSelectTemplate?.(Number(v)); }} />
          </span>
        </label>
      {/if}
      <!-- Trailing stop % -->
      <label class="oes-basket-tpl-param" title="Trailing stop % — SL trigger ratchets toward LTP as it moves favorably.">
        <span>Trail SL %{_trailAsterisk ? '*' : ''}</span>
        <input type="number" step="0.5"
          placeholder={selectedTemplate.sl_trail_pct != null ? String(selectedTemplate.sl_trail_pct) : '—'}
          bind:value={slTrailPctOverride} />
      </label>
      <!-- TP order type toggle -->
      <span class="oes-tpl-type-toggle" role="group" aria-label="TP order type">
        <span class="oes-basket-tpl-param-label">TP type{_tpTypeAsterisk ? '*' : ''}</span>
        <button type="button"
                class={'oes-tpl-type-btn' + ((!tpOrderTypeOverride || tpOrderTypeOverride === '') ? '' : tpOrderTypeOverride === 'LIMIT' ? ' on' : '')}
                onclick={() => { tpOrderTypeOverride = 'LIMIT'; }}>LIMIT</button>
        <button type="button"
                class={'oes-tpl-type-btn' + (tpOrderTypeOverride === 'MARKET' ? ' on' : '')}
                onclick={() => { tpOrderTypeOverride = 'MARKET'; }}>MKT</button>
      </span>
      <!-- Scale-out ladder JSON -->
      <div class="oes-tpl-scales-wrap">
        <label class="oes-tpl-scales-label" title={"Scale-out ladder — JSON array of [{\"at_pct\": N, \"close_pct\": M}] entries. close_pct must sum to ≤ 100."}>
          <span class="oes-basket-tpl-param-label">Scale ladder (JSON)</span>
          <textarea class="oes-tpl-scales-input"
                    class:oes-tpl-input-err={!!_scalesErr}
                    rows="3"
                    placeholder={selectedTemplate.tp_scales_json ?? '[{"at_pct": 30, "close_pct": 50}]'}
                    bind:value={tpScalesJsonOverride}></textarea>
        </label>
        {#if _scalesErr}
          <span class="oes-tpl-err-chip">{_scalesErr}</span>
        {/if}
      </div>
      <!-- Reset link -->
      <button type="button" class="oes-tpl-reset-link"
              onclick={_resetToDefaults}
              title="Reset all overrides to the template's default values">
        Reset to template defaults
      </button>
    </div>
  {/if}
{/if}

<style>
  .oes-basket-tpl-pick {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    font-family: monospace;
    font-size: var(--fs-sm);
    color: var(--algo-muted);
  }
  /* Single toggle button (2026-09-30 — replaces the old Default/None
     two-button pill; operator: "make Templ look like a button which
     can be active or inactive based on button press, default active").
     Same detuned amber intensity the rest of this component's chrome
     already uses (border 0.28, active fill 0.22, active text
     var(--algo-amber)) — matches .oes-tpl-toggle-btn.on's retired
     palette and .oes-tpl-type-btn.on below, per the 2026-09-29 palette
     pass noted further down (NOT the more saturated 100%-opacity amber
     ChaseAggPicker uses for its own `on` state). */
  .oes-tpl-button {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    height: var(--ctl-h, 1.55rem);
    padding: 0 0.55rem;
    /* OFF is now a solid, clearly-visible slate state (2026-09-30 —
       was near-transparent + 65%-alpha text, which read as "barely
       there" rather than a deliberate OFF choice; part of why the
       whole control was hard to spot at a glance). */
    background: rgba(148, 163, 184, 0.10);
    border: 1px solid rgba(148, 163, 184, 0.35);
    border-radius: 3px;
    color: var(--algo-slate);
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.03em;
    cursor: pointer;
    box-sizing: border-box;
    transition: background 0.12s, color 0.12s, border-color 0.12s;
  }
  .oes-tpl-button:hover:not(.active) {
    color: var(--algo-slate);
    background: rgba(148, 163, 184, 0.18);
    border-color: rgba(148, 163, 184, 0.5);
  }
  .oes-tpl-button.active {
    background: rgba(251, 191, 36, 0.22);
    border-color: rgba(251, 191, 36, 0.55);
    color: var(--algo-amber, var(--c-action));
  }
  /* Button is always clickable/enabled (2026-09-30) — the old
     :disabled rule (dimmed opacity + not-allowed cursor) is gone since
     the `disabled` HTML attribute is never set. */
  /* Status dot — small colored circle, amber when ON, slate when OFF,
     for an at-a-glance cue independent of the text/border color. */
  .oes-tpl-button-dot {
    width: 0.4rem;
    height: 0.4rem;
    border-radius: 50%;
    background: var(--algo-slate-muted);
    flex-shrink: 0;
  }
  .oes-tpl-button.active .oes-tpl-button-dot {
    background: var(--algo-amber, var(--c-action));
  }
  /* Palette pass (2026-09-29, operator: "template palette not
     consistent with rest of order elements") — TemplateBar was using
     amber at 0.50-0.70 alpha throughout (dropdown border, expand icon,
     param labels, input borders/focus, expanded-panel chrome), far more
     saturated than the rest of the order-entry surface's own amber
     accents (e.g. OrderTicket's `.ot-input` border sits at 0.25). Toned
     every STRUCTURAL/chrome use down to that same light intensity;
     genuine semantic color (red errors, the amber warning chip) is
     untouched — those already matched the rest of the app's error/warn
     convention and weren't the inconsistency. The toggle above (added
     2026-09-30) follows the same convention from the start. */
  /* #30 expand toggle button — bumped from --fs-xs (0.55rem, "too
     small") to a 1.1rem square hit-area, brightened from slate-muted
     to the amber action color so it actually reads as a clickable
     affordance (2026-09-30, operator request). */
  .oes-tpl-expand-btn {
    background: transparent;
    border: none;
    color: var(--c-action, var(--algo-amber));
    width: 1.1rem;
    height: 1.1rem;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 0;
    cursor: pointer;
    line-height: 1;
    transition: color 0.12s;
  }
  .oes-tpl-expand-btn:hover {
    color: var(--algo-amber-bright, #fcd34d);
  }
  /* Pure-CSS chevron (2026-09-30, operator: "make cap and reverse cap
     symbols after templated should be better looking visible
     symbols") — replaces the plain "^" text character, which
     rendered thin/small and depended on the browser's font for how
     crisp/visible it looked. Classic two-border-corner technique:
     a box with only its right+bottom edges visible forms an "L"
     corner; rotating that corner points it in any direction with a
     perfectly crisp, font-independent line. rotate(45deg) here is
     the base "point down" shape (0.65rem across the two visible
     edges, matching the 2px stroke weight used by other icon-style
     affordances in this app). */
  .oes-tpl-expand-icon {
    display: inline-block;
    width: 0.4rem;
    height: 0.4rem;
    border-right: 2px solid currentColor;
    border-bottom: 2px solid currentColor;
    transform: rotate(45deg);
    /* Nudges the rotated box back to the button's optical center —
       a plain rotate(45deg) box's bounding area sits slightly high
       relative to its visual corner. */
    margin-top: -0.1rem;
  }
  /* Down-pointing chevron (45deg base, above) = collapsed state,
     inviting "click to expand downward". Adding 180deg on the BUTTON
     (composes with the icon's own fixed 45deg -> 225deg = pointing
     up) = expanded state, inviting "click to collapse back up".
     Mirrors the old ▴/▾ semantics with a crisper, more visible icon. */
  .oes-tpl-expand-btn.oes-tpl-expand-open {
    transform: rotate(180deg);
  }
  /* Parameter override row — sits inline with the Select. Each
     param is a tight label+input pair. The input is bare-monospace
     for density; placeholder shows the template's value so the
     operator sees what the value would be without overrides. */
  .oes-basket-tpl-params {
    display: inline-flex;
    align-items: center;
    gap: 0.45rem;
    flex-wrap: wrap;
    margin-left: 0.4rem;
  }
  .oes-basket-tpl-param {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    font-family: monospace;
    font-size: var(--fs-xs);
    color: var(--algo-muted);
  }
  /* Asterisk override indicator in label span (#30) */
  .oes-basket-tpl-param > span,
  .oes-basket-tpl-param-label {
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-weight: 700;
    color: rgba(251, 191, 36, 0.85);
    font-family: monospace;
    font-size: var(--fs-xs);
  }
  /* Error state label color (#7) */
  .oes-tpl-param-err > span {
    color: rgba(248, 113, 113, 0.90);
  }
  /* On-fill param inputs — restyled to match the order ticket's price
     field (.ot-input, OrderTicket.svelte) (2026-09-30, operator:
     "make tp% and sl% values... look like price values in order
     ticket") — was a semi-transparent dark background with a strong
     amber border + inset glow, visually a different "kind" of input
     from the ticket's own Price/Trigger/Lots fields. Height
     deliberately UNCHANGED (still 1.4rem) — that value is part of a
     separately tested "all toolbar controls share one height tier"
     invariant (Fix #3, this file's own CSS further down + the live
     test asserting Select trigger/Template toggle/DTE chip/this input
     all render at the same height); matching .ot-input's own
     min-height: 1.7rem here would break that tier. Hover/focus states
     intentionally left as-is — only the RESTING look (what the operator
     called "values") was in scope. */
  .oes-basket-tpl-param > input {
    width: 3.6rem;
    height: 1.4rem;
    padding: 0 0.35rem;
    background: #1d2a44;
    border: 1px solid rgba(251, 191, 36, 0.25);
    border-radius: 3px;
    /* A3 (2026-09 audit) — same as .oes-basket-tpl-name above. */
    color: var(--algo-slate);
    font-family: var(--font-numeric);
    font-size: var(--fs-md);
    text-align: right;
    box-sizing: border-box;
    font-variant-numeric: tabular-nums;
    color-scheme: dark;
    transition: border-color 0.12s, background 0.12s, box-shadow 0.12s;
  }
  .oes-basket-tpl-param > input:hover {
    border-color: rgba(251, 191, 36, 0.95);
  }
  .oes-basket-tpl-param > input:focus {
    outline: none;
    border-color: var(--algo-amber, var(--c-action));
    background: rgba(28, 22, 8, 0.92);
    box-shadow: inset 0 0 0 1px rgba(251, 191, 36, 0.55),
                0 0 0 2px rgba(251, 191, 36, 0.20);
  }
  .oes-basket-tpl-param > input::placeholder {
    color: rgba(251, 191, 36, 0.75);
    font-style: italic;
  }
  /* Error border on inputs (#7) */
  .oes-tpl-input-err {
    border-color: rgba(248, 113, 113, 0.80) !important;
    box-shadow: inset 0 0 0 1px rgba(248, 113, 113, 0.25) !important;
  }
  .oes-tpl-input-err:focus {
    border-color: #f87171 !important;
    box-shadow: inset 0 0 0 1px rgba(248, 113, 113, 0.45),
                0 0 0 2px rgba(248, 113, 113, 0.18) !important;
  }
  /* Inline error / warning chips (#7) */
  .oes-tpl-errors {
    display: flex;
    flex-wrap: wrap;
    flex-basis: 100%;
    gap: 0.3rem;
    margin-left: 0.4rem;
    margin-top: 0.15rem;
  }
  .oes-tpl-err-chip {
    font-family: monospace;
    font-size: var(--fs-xs);
    color: #f87171;
    background: rgba(248, 113, 113, 0.10);
    border: 1px solid rgba(248, 113, 113, 0.32);
    padding: 0.10rem 0.38rem;
    border-radius: 3px;
  }
  .oes-tpl-warn-chip {
    font-family: monospace;
    font-size: var(--fs-xs);
    color: #fbbf24;
    background: rgba(251, 191, 36, 0.10);
    border: 1px solid rgba(251, 191, 36, 0.32);
    padding: 0.10rem 0.38rem;
    border-radius: 3px;
  }
  /* #30 Expanded panel */
  .oes-tpl-expanded {
    display: flex;
    flex-wrap: wrap;
    flex-basis: 100%;
    align-items: flex-start;
    gap: 0.5rem 0.6rem;
    margin-left: 0.4rem;
    margin-top: 0.35rem;
    padding: 0.45rem 0.55rem;
    background: rgba(8, 14, 28, 0.55);
    border: 1px solid rgba(251, 191, 36, 0.18);
    border-radius: 4px;
  }
  /* Specific-template picker (2026-09-30) — sits first inside the
     expand panel so the operator can override the ON toggle's
     side-aware default with one specific named template. */
  .oes-tpl-pick-specific {
    display: inline-flex;
    flex-direction: column;
    gap: 0.2rem;
  }
  .oes-tpl-pick-specific-select {
    min-width: 9rem;
    max-width: 13rem;
  }
  /* TP type mini-toggle (#30) */
  .oes-tpl-type-toggle {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    height: 1.4rem;
  }
  .oes-tpl-type-btn {
    padding: 0 0.45rem;
    height: 1.4rem;
    background: rgba(12, 18, 32, 0.82);
    border: 1px solid rgba(251, 191, 36, 0.40);
    border-radius: 3px;
    /* A3 (2026-09 audit) — stale rgba(200,216,240,α); alpha preserved. */
    color: color-mix(in srgb, var(--algo-slate) 65%, transparent);
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    font-weight: 700;
    cursor: pointer;
    transition: background 0.12s, color 0.12s;
  }
  .oes-tpl-type-btn.on {
    background: rgba(251, 191, 36, 0.22);
    color: var(--algo-amber, var(--c-action));
    border-color: rgba(251, 191, 36, 0.65);
  }
  /* Scale-out JSON textarea (#30) */
  .oes-tpl-scales-wrap {
    width: 100%;
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }
  .oes-tpl-scales-label {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }
  .oes-tpl-scales-input {
    width: 100%;
    min-width: min(14rem, 100%);
    max-width: 26rem;
    padding: 0.3rem 0.45rem;
    background: rgba(12, 18, 32, 0.82);
    border: 1px solid rgba(251, 191, 36, 0.50);
    border-radius: 3px;
    /* A3 (2026-09 audit) — same as .oes-basket-tpl-name above. */
    color: var(--algo-slate);
    font-family: var(--font-numeric), monospace;
    font-size: var(--fs-xs);
    resize: vertical;
    box-sizing: border-box;
    transition: border-color 0.12s;
  }
  .oes-tpl-scales-input:focus {
    outline: none;
    border-color: var(--algo-amber, var(--c-action));
  }
  .oes-tpl-scales-input::placeholder {
    color: rgba(251, 191, 36, 0.55);
    font-style: italic;
  }
  /* Reset link (#30) */
  .oes-tpl-reset-link {
    background: transparent;
    border: none;
    color: rgba(148, 163, 184, 0.75);
    font-family: monospace;
    font-size: var(--fs-xs);
    text-decoration: underline;
    cursor: pointer;
    padding: 0;
    transition: color 0.12s;
    align-self: flex-end;
  }
  .oes-tpl-reset-link:hover {
    /* A3 (2026-09 audit) — was flat hex #cbd5e1; now var(--algo-slate). */
    color: var(--algo-slate);
  }
</style>
