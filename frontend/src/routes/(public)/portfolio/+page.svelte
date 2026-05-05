<script>
  import { onMount } from 'svelte';
  import { authStore, clientTimestamp } from '$lib/stores';
  import { goto } from '$app/navigation';

  onMount(() => {
    if (!$authStore.token) { goto('/signin'); return; }
  });

  /** Format a number as Indian currency. */
  function inr(v) {
    if (v == null) return '—';
    return '₹' + Number(v).toLocaleString('en-IN', { maximumFractionDigits: 0 });
  }

  function pct(v) {
    if (v == null) return '—';
    return Number(v).toFixed(2) + '%';
  }

  function pnlClass(v) {
    if (v == null) return '';
    return v < 0 ? 'text-red-600' : 'text-green-700';
  }
</script>

<div class="text-[0.65rem] text-muted mb-2">{clientTimestamp()}</div>

{#if $authStore.user}
  <div class="w-full space-y-4">

    <!-- Coming-soon banner -->
    <div class="rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900 leading-relaxed">
      Portfolio dashboard is coming soon — your partner contribution and profit-sharing terms are summarised below. Live NAV and YTD performance will be wired in shortly.
    </div>

    <!-- Partner summary card -->
    <div class="bg-white rounded-lg border border-gray-200 shadow-sm p-5">
      <h2 class="page-heading">Partner Summary</h2>
      <div class="grid grid-cols-2 sm:grid-cols-3 gap-4 text-sm">
        <div>
          <div class="field-label">Partner</div>
          <div class="font-semibold text-text">{$authStore.user.display_name}</div>
        </div>
        <div>
          <div class="field-label">Capital Contribution</div>
          <div class="font-semibold text-text">{inr($authStore.user.contribution)}</div>
        </div>
        <div>
          <div class="field-label">Role</div>
          <div class="font-semibold text-text capitalize">{$authStore.user.role}</div>
        </div>
      </div>
    </div>

    <!-- Profit structure info -->
    <div class="bg-white rounded-lg border border-gray-200 shadow-sm p-5">
      <h2 class="page-heading">Performance Share Structure</h2>
      <div class="space-y-2 text-sm text-text/80 leading-relaxed">
        <p>
          Profit is calculated annually on <strong>31 March</strong>.
          LLP expenses are deducted at actuals before NAV and profit calculation.
        </p>
        <p>
          Partners receive a <strong>threshold return</strong> first. Excess profit is split
          <strong>50:50</strong> between the partner and the LLP.
        </p>
        <p>
          The <strong>first tier</strong> carries a 10% profit threshold.
          Each additional tier increases the threshold by 0.25%.
        </p>
        <p>
          If annual profit falls below the threshold, the closing NAV becomes
          the new reference NAV for the next year. Losses carry forward until NAV
          recovers to the last reference NAV.
        </p>
        <p>
          Redemption requests are accepted once a year by <strong>28 February</strong>,
          processed after the 31 March NAV calculation.
        </p>
      </div>
    </div>

    <!-- Links -->
    <div class="flex gap-3">
      <button onclick={() => goto('/performance')} class="btn-primary">View Portfolio Performance</button>
      <button onclick={() => goto('/faq')}         class="btn-secondary">FAQ & Process Flows</button>
    </div>

  </div>
{:else}
  <p class="text-text/40 text-sm">Please sign in to view your portfolio.</p>
{/if}
