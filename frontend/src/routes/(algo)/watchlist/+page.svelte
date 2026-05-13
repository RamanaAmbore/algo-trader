<script>
  // Thin page wrapper — the unified market-pulse component lives in
  // $lib/MarketPulse.svelte. Both /watchlist and /dashboard compose
  // it with different presets so the merge engine + symbol-cell
  // renderer + format helpers stay in one place.
  import MarketPulse from '$lib/MarketPulse.svelte';
</script>

<svelte:head><title>Watchlist | RamboQuant Analytics</title></svelte:head>

<MarketPulse title="Watchlist" />
