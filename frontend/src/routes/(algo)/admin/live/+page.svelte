<script>
  // Redirect stub — /admin/live → /admin/execution?mode=live
  // Preserves bookmarks and in-app deep-links created before the
  // consolidated Execution page was introduced.
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';
  onMount(() => { goto('/admin/execution?mode=live', { replaceState: true }); });
</script>
