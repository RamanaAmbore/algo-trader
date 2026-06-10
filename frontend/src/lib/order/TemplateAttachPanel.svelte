<!--
  TemplateAttachPanel — reusable template picker + override fields.

  Used by:
    • OrderTicket.svelte    — operator picks at submit time
    • /automation agent editor — picks at agent-design time (per
                                 `place_order` action node)

  Renders:
    • Template dropdown (system templates first, default starred,
      "— No template (entry only) —" sentinel)
    • Inline summary chip ("TP +30% · SL -20%") for the selected row
    • TP%, SL% override inputs (placeholders show template defaults)
    • Wing strike-offset override (sell_option scope only)

  Mode-aware auto-select: on mount + whenever parentSide /
  parentSymbol changes, picks the is_default template whose scope
  matches the operator's intent (BUY → buy_any default, SELL+option
  → sell_option default). Operator's manual pick wins and is never
  overridden.

  The preview chip ("on fill → TP @ ₹150 · SL @ ₹80 · Wing -500CE")
  lives in the parent (OrderTicket-only — needs reference price +
  the /api/orders/ticket/preview endpoint). This component is the
  inputs surface.

  Industry analogue: NinjaTrader ATM Strategy picker.
-->
<script>
  import { onMount, untrack } from 'svelte';
  import Select from '$lib/Select.svelte';
  import { loadOrderTemplates, orderTemplatesStore } from '$lib/data/templates';

  let {
    // Bindable — operator's current selection (null = no template /
    // entry-only). When the parent reads this back it's an integer id
    // matching an OrderTemplate row, or null.
    templateId               = $bindable(/** @type {number|null} */ (null)),
    // Bindable — per-action override fields. Empty string means
    // "use template default"; numeric means "tweak this single
    // submission". Both Surface (OrderTicket + agent editor) treat
    // the empty case identically.
    tpOverride               = $bindable(/** @type {number|''} */ ('')),
    slOverride               = $bindable(/** @type {number|''} */ ('')),
    wingPremiumPctOverride   = $bindable(/** @type {number|''} */ ('')),
    wingStrikeOffsetOverride = $bindable(/** @type {number|''} */ ('')),

    // Caller context — drives auto-select + wing visibility.
    parentSide   = /** @type {string} */ ('BUY'),
    parentSymbol = /** @type {string} */ (''),

    // Compact mode trims label sizes for narrow rows (agent editor
    // inside an expanded agent row is tight on width).
    compact      = /** @type {boolean} */ (false),
    // Hide the entire panel when no templates have loaded — useful
    // when the parent wants to bail gracefully on demo / pre-load.
    hideWhenEmpty = /** @type {boolean} */ (false),
  } = $props();

  let _templates = $state(/** @type {any[]} */ ([]));
  let _loadError = $state('');

  const selectedTemplate = $derived(
    _templates.find(t => t.id === templateId) || null
  );

  function _appliesToFor(side, sym) {
    if (side === 'SELL' && /\d+(CE|PE)$/i.test(sym || '')) return 'sell_option';
    if (side === 'BUY') return 'buy_any';
    return 'both';
  }

  function _summariseTemplate(t) {
    if (!t) return '';
    const parts = [];
    if (t.tp_pct != null) parts.push(`TP +${t.tp_pct}%`);
    if (t.sl_pct != null) parts.push(`SL -${t.sl_pct}%`);
    if (t.wing_strike_offset != null) parts.push(`Wing +${t.wing_strike_offset}`);
    if (t.wing_premium_pct != null) parts.push(`Wing ${t.wing_premium_pct}% prem`);
    return parts.length ? parts.join(' · ') : '(entry only)';
  }

  function _autoSelect() {
    if (templateId !== null) return;
    if (_templates.length === 0) return;
    const scope = _appliesToFor(parentSide, parentSymbol);
    const match = _templates.find(t =>
      t.is_default && (t.applies_to === scope || t.applies_to === 'both')
    );
    if (match) {
      templateId = match.id;
    } else {
      const none = _templates.find(t => t.slug === 'none');
      if (none) templateId = none.id;
    }
  }

  onMount(async () => {
    try {
      const rows = await loadOrderTemplates();
      _templates = rows.filter(t => t.is_active);
      _autoSelect();
    } catch (e) {
      _loadError = e?.message || 'failed to load templates';
    }
  });

  // Re-sync when /automation/templates edits the catalog while this
  // picker is mounted — no extra fetch, the store push is enough.
  $effect(() => {
    const rows = $orderTemplatesStore;
    if (rows && rows.length) {
      _templates = rows.filter(t => t.is_active);
    }
  });

  // Re-evaluate the auto-select when caller context flips (side or
  // symbol). Operator's explicit pick is preserved via the guard at
  // the top of _autoSelect.
  $effect(() => {
    const _ = `${parentSide}-${parentSymbol}`;
    untrack(() => { _autoSelect(); });
  });

  const wingVisible = $derived(_appliesToFor(parentSide, parentSymbol) === 'sell_option');
</script>

{#if !(hideWhenEmpty && _templates.length === 0)}
  <div class="tap-card" class:tap-compact={compact}>
    <div class="tap-row tap-row-picker">
      <label class="tap-label">Template <span class="tap-label-sub">(exit rules)</span></label>
      <div class="tap-picker">
        <Select
          options={[
            { value: '', label: '— No template (entry only) —' },
            ...(_templates.map(t => ({
              value: String(t.id),
              label: `${t.name}${t.is_default ? ' ★' : ''}${t.slug === 'none' ? '' : '  — ' + _summariseTemplate(t)}`,
            })))
          ]}
          value={templateId === null ? '' : String(templateId)}
          onValueChange={(v) => { templateId = v === '' ? null : Number(v); }}
        />
      </div>
    </div>

    <div class="tap-row tap-row-overrides">
      <label class="tap-tpl-field">
        <span>TP%</span>
        <input type="number" class="tap-input"
               step="0.5"
               placeholder={selectedTemplate?.tp_pct != null ? String(selectedTemplate.tp_pct) : '—'}
               bind:value={tpOverride} />
      </label>
      <label class="tap-tpl-field">
        <span>SL%</span>
        <input type="number" class="tap-input"
               step="0.5"
               placeholder={selectedTemplate?.sl_pct != null ? String(selectedTemplate.sl_pct) : '—'}
               bind:value={slOverride} />
      </label>
      {#if wingVisible}
        <label class="tap-tpl-field">
          <span>Wing strike+</span>
          <input type="number" class="tap-input"
                 step="50"
                 placeholder={selectedTemplate?.wing_strike_offset != null ? String(selectedTemplate.wing_strike_offset) : '—'}
                 bind:value={wingStrikeOffsetOverride} />
        </label>
        <label class="tap-tpl-field">
          <span>Wing prem%</span>
          <input type="number" class="tap-input"
                 step="0.5"
                 placeholder={selectedTemplate?.wing_premium_pct != null ? String(selectedTemplate.wing_premium_pct) : '—'}
                 bind:value={wingPremiumPctOverride} />
        </label>
      {/if}
    </div>

    {#if _loadError}
      <div class="tap-err">⚠ templates: {_loadError}</div>
    {/if}
  </div>
{/if}

<style>
  .tap-card {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }
  .tap-compact { gap: 0.3rem; }

  .tap-row {
    display: flex;
    gap: 0.45rem;
    align-items: center;
    flex-wrap: wrap;
  }
  .tap-row-picker { width: 100%; }
  .tap-row-overrides { gap: 0.55rem; }

  .tap-label {
    font-size: 0.65rem;
    color: rgba(180,200,230,0.75);
    font-weight: 600;
    font-family: ui-monospace, monospace;
    letter-spacing: 0.02em;
    flex-shrink: 0;
  }
  .tap-label-sub {
    opacity: 0.55;
    font-weight: 400;
    font-size: 0.55rem;
    margin-left: 0.2rem;
  }
  .tap-picker {
    flex: 1 1 auto;
    min-width: 0;
  }

  .tap-tpl-field {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    font-size: 0.62rem;
    color: rgba(180,200,230,0.75);
    font-family: ui-monospace, monospace;
  }
  .tap-tpl-field span {
    font-weight: 600;
    letter-spacing: 0.03em;
  }
  .tap-input {
    width: 4.2rem;
    min-width: 4.2rem;
    padding: 0.3rem 0.5rem;
    font-size: 0.72rem;
    color: #e5e7eb;
    background: rgba(15,23,41,0.7);
    border: 1px solid rgba(180,200,230,0.20);
    border-radius: 4px;
    font-family: ui-monospace, monospace;
    text-align: right;
  }
  .tap-input:focus {
    outline: none;
    border-color: rgba(251,191,36,0.55);
    background: rgba(15,23,41,0.85);
  }

  .tap-err {
    font-size: 0.6rem;
    color: #fca5a5;
    padding: 0.25rem 0.4rem;
    background: rgba(248,113,113,0.08);
    border: 1px solid rgba(248,113,113,0.30);
    border-radius: 3px;
  }
</style>
