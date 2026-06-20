<script>
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  onMount(() => goto('/automation/agent-templates', { replaceState: true }));
</script>
<p>Redirecting…</p>
