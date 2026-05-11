<script>
  // Thin page wrapper — the unified market-pulse component lives in
  // $lib/MarketPulse.svelte. Both /watchlist and /dashboard compose
  // it with different presets so the merge engine + symbol-cell
  // renderer + format helpers stay in one place.
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { authStore } from '$lib/stores';
  import MarketPulse from '$lib/MarketPulse.svelte';

  onMount(() => {
    if (!$authStore.user) goto('/signin');
  });
</script>

<MarketPulse title="Watchlist" />
