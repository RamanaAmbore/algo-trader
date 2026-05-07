<script>
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';
  onMount(() => { goto('/admin/execution?mode=sim', { replaceState: true }); });
</script>
