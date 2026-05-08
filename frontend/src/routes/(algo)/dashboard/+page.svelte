<script>
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';
  import { page } from '$app/state';

  onMount(() => {
    const tab = page.url.searchParams.get('tab');
    const target = tab === 'pnl' ? '/admin/pnl' : '/performance';
    goto(target, { replaceState: true });
  });
</script>
