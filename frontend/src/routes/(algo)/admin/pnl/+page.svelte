<script>
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';
  onMount(() => { goto('/dashboard?tab=pnl', { replaceState: true }); });
</script>
