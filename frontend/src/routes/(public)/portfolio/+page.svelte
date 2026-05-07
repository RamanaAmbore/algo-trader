<script>
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';
  onMount(() => { goto('/performance', { replaceState: true }); });
</script>
