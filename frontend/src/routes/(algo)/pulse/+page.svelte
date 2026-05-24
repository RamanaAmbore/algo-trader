<script>
  // Thin page wrapper — the unified market-pulse component lives in
  // $lib/MarketPulse.svelte. Both /pulse and /dashboard compose it
  // with different presets so the merge engine + symbol-cell renderer
  // + format helpers stay in one place.
  import MarketPulse from '$lib/MarketPulse.svelte';
</script>

<svelte:head><title>Market Pulse | RamboQuant Analytics</title></svelte:head>

<!-- accountPicker=true mounts the broker-account MultiSelect in the
     toolbar (right cluster, next to source toggles). Empty selection
     = all accounts; otherwise positions + holdings inputs to
     buildUnified are scoped to the chosen set. Watchlist + option
     underlyings stay visible. -->
<MarketPulse title="Market Pulse" flat={true} accountPicker={true} />
