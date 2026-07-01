<!--
  StrategyPicker — small inline Select that binds to the global
  `selectedStrategyId` store. Mount on any page that wants to
  scope its views to one strategy at a time.

  Auto-loads strategies on mount; refreshes when `bump` changes.
  The "—" option clears the filter (selectedStrategyId = null).

  Usage:
    <StrategyPicker label="Strategy" />
-->
<script>
  import { onMount } from 'svelte';
  import { selectedStrategyId } from '$lib/stores';
  import { fetchStrategies } from '$lib/api';
  import Select from '$lib/Select.svelte';

  /**
   * @typedef {object} Props
   * @property {string} [label]
   * @property {number} [bump]
   */
  /** @type {Props} */
  let { label = 'Strategy', bump = 0 } = $props();

  /** @type {{id: number, slug: string, name: string}[]} */
  let _strategies = $state([]);
  let _loaded = $state(false);

  async function _load() {
    try {
      const r = await fetchStrategies({ activeOnly: true });
      _strategies = Array.isArray(r?.rows)
        ? r.rows.map(s => ({ id: s.id, slug: s.slug, name: s.name }))
        : [];
    } catch {
      _strategies = [];
    } finally {
      _loaded = true;
    }
  }

  onMount(_load);
  $effect(() => {
    if (bump > 0) _load();
  });

  const _options = $derived([
    { value: '', label: 'All strategies' },
    ..._strategies.map(s => ({ value: String(s.id), label: s.slug })),
  ]);

  function _onChange(/** @type {string|number} */ v) {
    const n = v === '' || v == null ? null : Number(v);
    selectedStrategyId.set(Number.isFinite(n) && n > 0 ? n : null);
  }
</script>

<!-- Picker hidden entirely when the operator has no strategies yet —
     no point showing a single-option control. -->
{#if _loaded && _strategies.length > 0}
  <div class="sp-wrap">
    <label class="sp-label" for="sp-strategy-select">{label}</label>
    <Select id="sp-strategy-select"
            value={$selectedStrategyId == null ? '' : String($selectedStrategyId)}
            ariaLabel="Filter by strategy"
            onValueChange={_onChange}
            options={_options} />
  </div>
{/if}

<style>
  .sp-wrap {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    margin-left: 0.5rem;
  }
  .sp-label {
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: var(--text-muted);
    font-family: var(--font-numeric);
  }
</style>
