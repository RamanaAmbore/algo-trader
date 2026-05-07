<script>
  // Redirect stub — /admin/paper → /admin/execution?mode=paper
  // Preserves bookmarks and in-app deep-links created before the
  // consolidated Execution page was introduced.
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';
  onMount(() => { goto('/admin/execution?mode=paper', { replaceState: true }); });
</script>
