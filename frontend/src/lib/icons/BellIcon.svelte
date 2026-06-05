<script>
  /**
   * BellIcon — shared notification bell SVG used by PageHeaderActions (Log
   * button) and ActivityLogModal (title icon).
   *
   * Flat orange fill (#fb923c = orange-400) with a darker outline accent
   * (#b45309 = amber-700, replacing the raw #9a3412 that was duplicated
   * across two call-sites). No radial gradient — reads as a friendly
   * flat icon, not a rendered 3-D object. Matches PageHeaderActions.pha-log
   * resting color so the button and the modal title are visually in sync.
   *
   * Props:
   *   width?   — SVG width (default "14")
   *   height?  — SVG height (default "14")
   *   class?   — additional class applied to the <svg> element
   */

  let {
    width  = '14',
    height = '14',
    'class': klass = '',
  } = $props();
</script>

<!-- Bell body · clapper · mounting knob all in the same warm-orange family.
     Outline uses amber-700 (#b45309) as the darker 2-tone accent instead
     of the raw #9a3412 that was hard-coded in both previous callsites. -->
<svg {width} {height} viewBox="0 0 16 16" aria-hidden="true" class={klass}>
  <path d="M8 2c-2.4 0-4 1.9-4 4.2 0 2.1-.8 3.6-1.7 4.5-.3.3-.1.8.3.8h10.8c.4 0 .6-.5.3-.8-.9-.9-1.7-2.4-1.7-4.5C12 3.9 10.4 2 8 2z"
        fill="#fb923c"
        stroke="#b45309" stroke-width="0.4"
        stroke-linejoin="round" />
  <path d="M6.6 13c.2.8.8 1.3 1.4 1.3.6 0 1.2-.5 1.4-1.3z"
        fill="#fb923c" stroke="#b45309" stroke-width="0.4"
        stroke-linejoin="round" />
  <circle cx="8" cy="2.1" r="0.55" fill="#fb923c"
          stroke="#b45309" stroke-width="0.4" />
</svg>
