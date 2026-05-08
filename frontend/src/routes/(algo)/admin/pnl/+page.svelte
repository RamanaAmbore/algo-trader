<script>
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  onMount(() => goto('/dashboard?tab=pnl', { replaceState: true }));
</script>

<svelte:head>
  <title>P&L · RamboQuant</title>
  <meta http-equiv="refresh" content="0;url=/dashboard?tab=pnl" />
</svelte:head>

<p style="font-family:ui-monospace,monospace;font-size:0.7rem;color:#7e97b8;padding:1rem">
  Redirecting to <a href="/dashboard?tab=pnl" style="color:#fbbf24">/dashboard?tab=pnl</a>…
</p>
