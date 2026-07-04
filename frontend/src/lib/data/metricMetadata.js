/**
 * metricMetadata.js — single source of truth for metric label, explanation,
 * ideal range, impact, and fix guidance.
 *
 * Two groups:
 *  - Per-file / per-route metrics (used by /admin/perf) keyed by
 *    PerfSnapshot column names (loc, cc_max, cc_avg, effect_count,
 *    state_count, derived_count, lcp_ms, tbt_ms, heap_mb,
 *    route_p50_ms, route_p95_ms, route_qps).
 *
 *  - Aggregate / project-wide metrics (used by /admin/metrics)
 *    keyed by CodeMetricsSnapshot column names (backend_loc,
 *    backend_complexity_avg, backend_complexity_max,
 *    backend_stale_count, backend_coverage_pct, frontend_loc,
 *    frontend_complexity_avg, frontend_duplicated_lines,
 *    frontend_stale_count, bug_count_since_last_release,
 *    test_backend_max_s, test_backend_total_wall_time_s).
 *
 * Each entry shape: { label, what, ideal, impact, fix }
 */

// ── Per-file / per-route keys (PerfSnapshot) ──────────────────────────────

export const METRIC_META = {

  // --- Code-structure metrics ---

  loc: {
    label: 'LOC',
    what: 'Lines of code in the component/route file (excluding blank + comments).',
    ideal: 'Component < 1500 · Page < 3000 · Route < 1500',
    impact: 'Large files are harder to review, test, and refactor. Correlates with defect density.',
    fix: 'Extract cohesive sub-components / helper modules. Aim to keep single-file responsibility.',
  },

  cc_max: {
    label: 'cc_max',
    what: 'Highest cyclomatic complexity of any function in the file (branch count + 1).',
    ideal: '< 10 green · 10–20 yellow · > 20 red · > 50 critical',
    impact: 'High-cc functions accumulate hidden state, retry loops, and edge cases — where bugs cluster.',
    fix: 'Decompose into helpers with cc < 15 each. Table-drive branches, extract validators.',
  },

  cc_avg: {
    label: 'cc_avg',
    what: 'Average cyclomatic complexity across all functions in the file.',
    ideal: '< 8 green · 8–15 yellow · > 15 red',
    impact: 'File-wide readability. High average signals systemic complexity, not just one outlier.',
    fix: 'Refactor pattern-matching branches into strategy tables. Push complexity to the edges.',
  },

  hotspot_cc: {
    label: 'Hotspot cc',
    what: 'Cyclomatic complexity of the worst individual function (not file average).',
    ideal: '< 20',
    impact: 'One 200-cc function anchors the whole file to red-zone maintenance cost.',
    fix: 'Decompose the function into pure helpers. Each helper cc < 20.',
  },

  effect_count: {
    label: '$effect',
    what: 'Number of Svelte 5 reactive $effect blocks in the component.',
    ideal: '< 10 green · 10–20 yellow · > 20 red',
    impact: 'Each $effect is a mini state machine. Too many → reactive graph tangles, loops, effect_update_depth_exceeded errors.',
    fix: 'Consolidate related side-effects. Wrap $state writes inside $effect in untrack() to prevent self-loops.',
  },

  state_count: {
    label: '$state',
    what: 'Number of Svelte 5 $state declarations in the component.',
    ideal: '< 30 green · 30–50 yellow · > 50 red',
    impact: 'Reactive-graph size scales with state count. Downstream re-renders explode when many pieces of state change per tick.',
    fix: 'Group related state into a single reactive object. Move derived state to $derived. Extract sub-components with their own state.',
  },

  derived_count: {
    label: '$derived',
    what: 'Number of Svelte 5 $derived declarations.',
    ideal: '< 30 typical · > 50 signals over-derivation',
    impact: 'High derived count is often OK — Svelte 5 handles it efficiently — but signals fragile chains.',
    fix: 'Consolidate multi-step derivations. Memoize expensive ones with $derived.by.',
  },

  // --- Web Vitals / runtime metrics ---

  lcp_ms: {
    label: 'LCP',
    what: 'Largest Contentful Paint — time until the largest above-fold element renders (ms). Core Web Vitals metric.',
    ideal: '< 2500 green · 2500–4000 amber · > 4000 red',
    impact: 'Users perceive slow load. Google search ranks lower for poor LCP.',
    fix: 'Defer non-critical hydration. Reduce blocking JS/CSS on entry. Preload hero content. SSR the above-fold.',
  },

  tbt_ms: {
    label: 'TBT',
    what: 'Total Blocking Time — sum of all long-task durations (> 50 ms) during load. Core Web Vitals metric.',
    ideal: '< 200 green · 200–600 amber · > 600 red',
    impact: 'Main thread frozen — clicks unresponsive, animations stutter.',
    fix: 'Break long computations with await + yield. Move heavy work to Web Workers. Debounce polls.',
  },

  heap_mb: {
    label: 'JS Heap',
    what: 'V8 heap size after page settled (MB, Chrome-only via performance.memory).',
    ideal: '< 50 typical · > 100 signals leak',
    impact: 'Memory pressure → GC pauses → laggy scroll + animation. Long-lived tabs OOM.',
    fix: 'Audit closures capturing large arrays. Add onDestroy teardowns for subscriptions + timers.',
  },

  // --- Backend route metrics ---

  route_p50_ms: {
    label: 'Route p50',
    what: 'Median request latency for the backend route (ms).',
    ideal: '< 200 green · 200–500 amber · > 500 red',
    impact: 'Slow routes gate every consumer.',
    fix: 'Index the WHERE columns. Batch broker calls. Reduce N+1 queries.',
  },

  route_p95_ms: {
    label: 'Route p95',
    what: '95th percentile request latency (ms). Tail latency — worst-case for most users.',
    ideal: '< 500 green · 500–1500 amber · > 1500 red',
    impact: 'p95 spikes are the "occasional slowness" users complain about.',
    fix: 'Add timeouts on outbound calls. Circuit-break failing dependencies. Cache tier for cold reads.',
  },

  route_qps: {
    label: 'QPS',
    what: 'Requests per second sustained on the route.',
    ideal: '< 20 typical retail · > 100 signals abuse or missing cache',
    impact: 'High QPS drains broker rate-limit budgets + DB pool.',
    fix: 'Client-side caching. Batch endpoints. Debounce polls.',
  },

  // ── Aggregate / project-wide keys (CodeMetricsSnapshot) ──────────────────

  backend_loc: {
    label: 'BE LOC',
    what: 'Total lines of code across all backend Python source files (excluding blank + comments).',
    ideal: 'Monitor trend — growth > 5% per sprint without new features is a smell',
    impact: 'Unchecked growth signals feature creep or inadequate extraction. Larger codebases take longer to load and review.',
    fix: 'Retire dead code paths. Extract standalone utilities to shared modules. Remove commented-out blocks.',
  },

  backend_complexity_avg: {
    label: 'BE cx avg',
    what: 'Mean cyclomatic complexity across all backend functions.',
    ideal: '< 8 green · 8–15 yellow · > 15 red',
    impact: 'File-wide average masks outliers but reveals systemic design quality.',
    fix: 'Decompose handler logic into pure helpers. Prefer table-driven dispatch over nested if-chains.',
  },

  backend_complexity_max: {
    label: 'BE cx max',
    what: 'Highest cyclomatic complexity of any single backend function.',
    ideal: '< 20 green · 20–50 amber · > 50 red',
    impact: 'The worst function sets the ceiling for testability across the whole backend.',
    fix: 'Identify the offending function via the capture script hotspot output. Decompose it first.',
  },

  backend_stale_count: {
    label: 'BE stale',
    what: 'Count of backend code patterns flagged as stale — dead imports, unreachable branches, TODO markers older than one sprint.',
    ideal: '0 ideal · > 10 review required',
    impact: 'Stale code confuses new contributors and can mask latent bugs in rarely-executed branches.',
    fix: 'Run stale-code grep from capture script. Remove dead imports, delete unreachable branches, resolve TODOs.',
  },

  backend_coverage_pct: {
    label: 'BE cov %',
    what: 'Percentage of backend Python lines covered by the pytest suite.',
    ideal: '> 70% green · 50–70% amber · < 50% red',
    impact: 'Low coverage means regressions in untested paths slip past CI.',
    fix: 'Add targeted tests for uncovered routes and broker-adapter branches. Focus on error paths first.',
  },

  frontend_loc: {
    label: 'FE LOC',
    what: 'Total lines of code across all frontend Svelte + JS source files (excluding blank + comments).',
    ideal: 'Monitor trend — growth > 5% per sprint without new features warrants review',
    impact: 'Svelte bundle size and initial parse time scale roughly with LOC. Hydration overhead grows.',
    fix: 'Extract reusable components. Move pure computation to standalone .js helpers (unbundled from Svelte context).',
  },

  frontend_complexity_avg: {
    label: 'FE cx avg',
    what: 'Mean cyclomatic complexity across all frontend functions and reactive blocks.',
    ideal: '< 8 green · 8–15 yellow · > 15 red',
    impact: 'Complex frontend logic drives reactive-graph depth and effect count, causing subtle re-render bugs.',
    fix: 'Flatten nested conditionals. Prefer $derived over $effect for computed values.',
  },

  frontend_duplicated_lines: {
    label: 'FE dup',
    what: 'Number of duplicated lines detected by the static analysis tool.',
    ideal: '< 200 green · 200–500 amber · > 500 red',
    impact: 'Duplicate code means bug fixes must be applied in multiple places. Increases maintenance cost.',
    fix: 'Extract shared logic to $lib helpers. Replace repeated markup blocks with parameterised components.',
  },

  frontend_stale_count: {
    label: 'FE stale',
    what: 'Count of frontend patterns flagged as stale — unused imports, dead conditional branches, legacy TODO comments.',
    ideal: '0 ideal · > 10 review required',
    impact: 'Stale patterns inflate bundle size and confuse reactive-graph analysis.',
    fix: 'Run unused-import lint. Delete dead snippets. Resolve TODOs tagged for previous sprints.',
  },

  bug_count_since_last_release: {
    label: 'Bugs',
    what: 'Number of commits since the previous release tag whose message contains "fix:" or "bug:".',
    ideal: '0 ideal · > 5 investigate root cause · > 10 release blocked',
    impact: 'High bug rate between releases signals either insufficient pre-release testing or design instability.',
    fix: 'Add targeted Playwright or pytest spec for each fix to prevent recurrence.',
  },

  test_backend_max_s: {
    label: 'Slowest test (BE)',
    what: 'Duration of the single slowest pytest test in the suite (seconds).',
    ideal: '< 3 s green · 3–10 s amber · > 10 s red',
    impact: 'One slow test serialises the CI run. Breaks the fast-feedback loop for developer iteration.',
    fix: 'Identify via pytest --durations=10. Mock slow broker calls or move to integration-test tier.',
  },

  test_backend_total_wall_time_s: {
    label: 'BE test wall time',
    what: 'Total wall-clock time to run the full pytest suite (seconds).',
    ideal: '< 60 s green · 60–180 s amber · > 180 s red',
    impact: 'Long test runs delay CI feedback and discourage running tests locally before push.',
    fix: 'Parallelise with pytest-xdist (-n auto). Mock broker I/O. Move DB-heavy tests to a separate slow-suite.',
  },
};
