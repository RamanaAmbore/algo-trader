<!--
  StaleBanner — small consistent banner for "fetch failed; data below
  may be stale" across pages that poll API data.

  Two visual tones depending on whether the operator has prior data
  to fall back on:

    • hasData=true  → ⏳ amber-toned info banner. Last-good payload is
                       still on screen; the operator just needs to know
                       the next refresh didn't land.
    • hasData=false → ⚠ red warning. First load failed; nothing on
                       screen to fall back to.

  Use as:
    <StaleBanner {error} hasData={rows.length > 0} label="Agents" />

  The `label` is what's being refreshed (e.g. "Agents", "Alerts",
  "Brokers"). Stays optional — falls back to a generic "Data" prefix.
-->
<script>
  /** @type {{ error: string, hasData?: boolean, label?: string }} */
  let { error = '', hasData = true, label = 'Data' } = $props();
</script>

{#if error}
  {#if hasData}
    <div class="stale-banner stale-banner-info" role="status">
      <span class="stale-banner-icon" aria-hidden="true">⏳</span>
      <span class="stale-banner-text">
        {label} refresh failed — <span class="font-mono">{error}</span>.
        Showing last-good data.
      </span>
    </div>
  {:else}
    <div class="stale-banner stale-banner-error" role="alert">
      <span class="stale-banner-icon" aria-hidden="true">⚠</span>
      <span class="stale-banner-text">
        {label} unavailable — <span class="font-mono">{error}</span>.
      </span>
    </div>
  {/if}
{/if}

<style>
  .stale-banner {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.4rem 0.6rem;
    margin-bottom: 0.5rem;
    border-radius: 3px;
    font-size: 0.65rem;
    line-height: 1.3;
    border: 1px solid;
  }
  .stale-banner-info {
    background: var(--algo-amber-bg);
    border-color: var(--algo-amber-border-soft);
    color: var(--algo-amber-text);
  }
  .stale-banner-error {
    background: var(--algo-red-bg);
    border-color: var(--algo-red-border-soft);
    color: var(--algo-red-text);
  }
  .stale-banner-icon {
    font-size: 0.85rem;
    flex-shrink: 0;
  }
  .stale-banner-text {
    flex: 1;
    min-width: 0;
    word-break: break-word;
  }
</style>
