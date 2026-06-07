<script>
  /**
   * ActionEventsToggle — shared "include action events" pill strip.
   *
   * Replaces the duplicate .filter-btn / .dash-agent-filter-btn
   * implementations on /automation/activity and /dashboard. Palette and
   * layout are the same; CSS lives in app.css as .act-events-*.
   *
   * Usage:
   *   <ActionEventsToggle bind:value={showActions} />
   *   const kinds = $derived(showActions
   *     ? ['agent_fire', 'agent_action_success', 'agent_action_error']
   *     : ['agent_fire']);
   */

  let {
    /** @type {boolean} */
    value = $bindable(false),
  } = $props();
</script>

<div class="act-events-row">
  <button
    type="button"
    class="act-events-btn"
    class:act-events-btn-on={value}
    onclick={() => (value = !value)}>
    {value ? '✓' : ''} {value ? 'Exclude' : 'Include'} action events
  </button>
  <span class="act-events-hint">
    {value
      ? 'showing fires + action successes/errors'
      : 'showing fires only — toggle to include actions'}
  </span>
</div>
