<script>
  /**
   * TemplateBar — "On fill" template pick + param override row.
   *
   * Renders the Default / None pill toggle, active-template name chip,
   * and the TP% / SL% / Wing parameter override inputs.
   *
   * The outer shell row <div> and the on-fill preview / cap-warn section
   * are intentionally kept in the parent (SymbolPanel) because they depend
   * on many additional parent-only state variables.
   *
   * @prop {object|null}  selectedTemplate          - current resolved template object (read-only display)
   * @prop {object|null}  sideAwareDefault          - side-aware default template (null → disables Default btn)
   * @prop {boolean}      showsWing                 - whether wing fields are visible
   * @prop {boolean}      shellUsingNone            - whether the "None" pill is currently selected
   * @prop {number|''}    tpOverride                - TP% override value ($bindable)
   * @prop {number|''}    slOverride                - SL% override value ($bindable)
   * @prop {number|''}    wingStrikeOffsetOverride  - Wing strike offset override ($bindable)
   * @prop {number|''}    wingPremPctOverride       - Wing premium % override ($bindable)
   * @prop {number|''}    slTrailPctOverride        - Trailing stop % override ($bindable, #30)
   * @prop {'LIMIT'|'MARKET'|''}  tpOrderTypeOverride  - TP order type override ($bindable, #30)
   * @prop {string}       tpScalesJsonOverride      - Scale-out JSON override ($bindable, #30)
   * @prop {() => void}   onSelectDefault           - called when operator clicks Default
   * @prop {() => void}   onSelectNone              - called when operator clicks None
   */
  let {
    selectedTemplate,
    sideAwareDefault,
    showsWing,
    shellUsingNone,
    tpOverride       = $bindable(),
    slOverride       = $bindable(),
    wingStrikeOffsetOverride = $bindable(),
    wingPremPctOverride      = $bindable(),
    slTrailPctOverride       = $bindable(),
    tpOrderTypeOverride      = $bindable(),
    tpScalesJsonOverride     = $bindable(),
    onSelectDefault,
    onSelectNone,
  } = $props();

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
    slTrailPctOverride = '';
    tpOrderTypeOverride = '';
    tpScalesJsonOverride = '';
  }
</script>

<span class="oes-basket-tpl-pick">
  <span class="oes-basket-tpl-label">Template</span>
  <span class="oes-tpl-toggle"
        class:oes-tpl-toggle-none={shellUsingNone}
        role="group" aria-label="Template attach">
    <button type="button"
            class={'oes-tpl-btn oes-tpl-btn-default' + (!shellUsingNone ? ' on' : '')}
            disabled={!sideAwareDefault}
            title={sideAwareDefault
              ? `Attach ${sideAwareDefault.name} on fill`
              : 'No side-default template configured for this scope'}
            onclick={onSelectDefault}>
      Default
    </button>
    <button type="button"
            class={'oes-tpl-btn oes-tpl-btn-none' + (shellUsingNone ? ' on' : '')}
            title="No template — entry only, no GTT / no wing"
            onclick={onSelectNone}>
      None
    </button>
  </span>
  {#if !shellUsingNone && selectedTemplate}
    <!-- Active template name + description so the operator sees
         WHICH default Default resolved to (relevant when there
         are multiple side-defaults seeded). -->
    <span class="oes-basket-tpl-name" title={selectedTemplate.description || ''}>
      {selectedTemplate.name || selectedTemplate.slug}
    </span>
    <!-- #30 expand toggle — reveals the full param set -->
    <button type="button"
            class="oes-tpl-expand-btn"
            title={_expanded ? 'Collapse template params' : 'Expand all template params'}
            onclick={() => { _expanded = !_expanded; }}>
      {_expanded ? '▴' : '▾'}
    </button>
  {/if}
</span>
{#if !shellUsingNone && selectedTemplate}
  <div class="oes-basket-tpl-params">
    <!-- TP% override -->
    <label class="oes-basket-tpl-param {_tpErr ? 'oes-tpl-param-err' : ''}"
           title="Take-profit % above (BUY) or below (SELL) the fill price.">
      <span>TP%{_tpAsterisk ? '*' : ''}</span>
      <input type="number" step="0.5"
        class:oes-tpl-input-err={!!_tpErr}
        placeholder={selectedTemplate.tp_pct != null ? String(selectedTemplate.tp_pct) : '—'}
        bind:value={tpOverride} />
    </label>
    <!-- SL% override -->
    <label class="oes-basket-tpl-param {_slErr ? 'oes-tpl-param-err' : ''}"
           title="Stop-loss % opposite the TP side.">
      <span>SL%{_slAsterisk ? '*' : ''}</span>
      <input type="number" step="0.5"
        class:oes-tpl-input-err={!!_slErr}
        placeholder={selectedTemplate.sl_pct != null ? String(selectedTemplate.sl_pct) : '—'}
        bind:value={slOverride} />
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
  {#if _tpErr || _slErr || _wingPremErr}
    <div class="oes-tpl-errors">
      {#if _tpErr}<span class="oes-tpl-err-chip">{_tpErr}</span>{/if}
      {#if _slErr}<span class="oes-tpl-err-chip">{_slErr}</span>{/if}
      {#if _wingPremErr}<span class="oes-tpl-err-chip">{_wingPremErr}</span>{/if}
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
      <!-- Trailing stop % -->
      <label class="oes-basket-tpl-param" title="Trailing stop % — SL trigger ratchets toward LTP as it moves favorably.">
        <span>Trail SL%{_trailAsterisk ? '*' : ''}</span>
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
  /* .oes-basket-tpl-label intentionally kept in SymbolPanel — also
     used by the demo-mode row outside this component. */
  /* Default / None two-pill toggle — mirrors the Side toggle in
     OrderTicket so the operator's mental model is the same: Default
     attaches the platform-resolved template, None opts out.
     Distinct color schemes per active state so the operator can tell
     them apart at a glance. Operator: "for default and none, template
     values use a different color scheme for text".
       Default ON → amber (algo primary, "rule is armed")
       None ON    → slate-gray (neutral, "nothing fires post-fill")
     The container's border tracks the active pill so the row itself
     reads as either amber-armed or slate-neutral. */
  .oes-tpl-toggle {
    display: inline-flex;
    height: 1.4rem;
    min-height: 1.4rem;
    border-radius: 3px;
    overflow: hidden;
    background: rgba(8, 14, 28, 0.55);
    border: 1px solid rgba(251, 191, 36, 0.55);
    box-sizing: border-box;
    transition: border-color 0.12s;
  }
  .oes-tpl-toggle-none {
    border-color: rgba(148, 163, 184, 0.55);
  }
  .oes-tpl-btn {
    flex: 0 0 auto;
    padding: 0 0.75rem;
    background: transparent;
    border: 0;
    color: rgba(200, 216, 240, 0.65);
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    font-weight: 800;
    letter-spacing: 0.05em;
    line-height: 1;
    cursor: pointer;
    transition: background 0.12s, color 0.12s, border-color 0.12s;
  }
  .oes-tpl-btn + .oes-tpl-btn {
    border-left: 1px solid rgba(251, 191, 36, 0.30);
  }
  .oes-tpl-toggle-none .oes-tpl-btn + .oes-tpl-btn {
    border-left-color: rgba(148, 163, 184, 0.30);
  }
  .oes-tpl-btn-default:hover:not(.on):not([disabled]) {
    background: rgba(251, 191, 36, 0.08);
    color: #f1f7ff;
  }
  .oes-tpl-btn-none:hover:not(.on):not([disabled]) {
    background: rgba(148, 163, 184, 0.10);
    color: #f1f7ff;
  }
  .oes-tpl-btn-default.on {
    background: rgba(251, 191, 36, 0.24);
    color: var(--algo-amber, var(--c-action));
    text-shadow: 0 0 8px rgba(251, 191, 36, 0.45);
  }
  .oes-tpl-btn-none.on {
    background: rgba(148, 163, 184, 0.22);
    color: #cbd5e1;
    text-shadow: 0 0 6px rgba(148, 163, 184, 0.45);
  }
  .oes-tpl-btn:disabled {
    opacity: 0.35;
    cursor: not-allowed;
  }
  /* Active-template name chip — sits inline next to the Default pill
     so the operator sees WHICH default Default resolved to (relevant
     once 4 side-defaults are seeded). */
  .oes-basket-tpl-name {
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    font-weight: 600;
    color: #f8fafc;
    background: rgba(251, 191, 36, 0.10);
    border: 1px solid rgba(251, 191, 36, 0.32);
    padding: 0.12rem 0.42rem;
    border-radius: 3px;
    letter-spacing: 0.02em;
  }
  /* #30 expand toggle button */
  .oes-tpl-expand-btn {
    background: transparent;
    border: none;
    color: rgba(251, 191, 36, 0.70);
    font-size: var(--fs-xs);
    padding: 0 0.2rem;
    cursor: pointer;
    line-height: 1;
    transition: color 0.12s;
  }
  .oes-tpl-expand-btn:hover {
    color: var(--algo-amber, var(--c-action));
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
  /* On-fill param inputs — amber accent on dark navy. The new
     container gradient already carries an amber wash, so the input
     borders use a solid amber that pops against the gradient and
     reads as algo-primary. Focus state inverts to bright amber with
     an inset glow so the active field jumps out. */
  .oes-basket-tpl-param > input {
    width: 3.6rem;
    height: 1.4rem;
    padding: 0 0.35rem;
    background: rgba(12, 18, 32, 0.82);
    border: 1px solid rgba(251, 191, 36, 0.70);
    border-radius: 3px;
    color: #f8fafc;
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    font-weight: 600;
    text-align: right;
    box-sizing: border-box;
    font-variant-numeric: tabular-nums;
    box-shadow: inset 0 0 0 1px rgba(251, 191, 36, 0.10);
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
    align-items: flex-start;
    gap: 0.5rem 0.6rem;
    margin-left: 0.4rem;
    margin-top: 0.35rem;
    padding: 0.45rem 0.55rem;
    background: rgba(8, 14, 28, 0.55);
    border: 1px solid rgba(251, 191, 36, 0.18);
    border-radius: 4px;
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
    color: rgba(200, 216, 240, 0.65);
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
    min-width: 14rem;
    max-width: 26rem;
    padding: 0.3rem 0.45rem;
    background: rgba(12, 18, 32, 0.82);
    border: 1px solid rgba(251, 191, 36, 0.50);
    border-radius: 3px;
    color: #f8fafc;
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
    color: #cbd5e1;
  }
</style>
