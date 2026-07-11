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
    onSelectDefault,
    onSelectNone,
  } = $props();
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
  {/if}
</span>
{#if !shellUsingNone && selectedTemplate}
  <div class="oes-basket-tpl-params">
    <label class="oes-basket-tpl-param" title="Take-profit % above (BUY) or below (SELL) the fill price.">
      <span>TP%</span>
      <input type="number" step="0.5"
        placeholder={selectedTemplate.tp_pct != null ? String(selectedTemplate.tp_pct) : '—'}
        bind:value={tpOverride} />
    </label>
    <label class="oes-basket-tpl-param" title="Stop-loss % opposite the TP side.">
      <span>SL%</span>
      <input type="number" step="0.5"
        placeholder={selectedTemplate.sl_pct != null ? String(selectedTemplate.sl_pct) : '—'}
        bind:value={slOverride} />
    </label>
    {#if showsWing}
      <label class="oes-basket-tpl-param" title="Protective wing BUY at this many strikes away from the parent.">
        <span>Wing strike+</span>
        <input type="number" step="50"
          placeholder={selectedTemplate.wing_strike_offset != null ? String(selectedTemplate.wing_strike_offset) : '—'}
          bind:value={wingStrikeOffsetOverride} />
      </label>
      <label class="oes-basket-tpl-param" title="Wing premium target as a % of the parent's premium.">
        <span>Wing prem%</span>
        <input type="number" step="0.5"
          placeholder={selectedTemplate.wing_premium_pct != null ? String(selectedTemplate.wing_premium_pct) : '—'}
          bind:value={wingPremPctOverride} />
      </label>
    {/if}
  </div>
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
  .oes-basket-tpl-param > span {
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-weight: 700;
    color: rgba(251, 191, 36, 0.85);
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
</style>
