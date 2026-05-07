<script>
  // Redirect stub — /admin/shadow → /admin/execution?mode=shadow
  // Preserves bookmarks and in-app deep-links created before the
  // consolidated Execution page was introduced.
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';
  onMount(() => { goto('/admin/execution?mode=shadow', { replaceState: true }); });
</script>
