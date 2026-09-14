/**
 * positionsDayPnlStore — thin shim delegating to positionsDerivedStore.
 *
 * Historical consumers (derivatives/_fnoDayPnlByRoot, NavCard, NavBreakdown,
 * MarketPulse) import this module by name. The shim keeps those imports
 * working without churn while positionsDerivedStore becomes the SSOT.
 *
 * `total` and `byKey` now read from positionsDerivedStore which:
 *   - uses the same 4 Hz symbolTickCount + 250ms throttle cadence
 *   - respects the same setFromPulse() pulse-override contract
 *   - adds byRootPositions / byRootHoldings / expiryTotal on top
 *
 * setFromPulse() is forwarded into positionsDerivedStore so MarketPulse
 * only needs to call one surface.
 */

export { positionsDerivedStore as positionsDayPnlStore } from '$lib/data/positionsDerivedStore.svelte.js';
