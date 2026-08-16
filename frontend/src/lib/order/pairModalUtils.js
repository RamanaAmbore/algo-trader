/**
 * Pure utilities for the pair-orders modal and its enabling logic.
 * Extracted for Vitest testability — no Svelte reactivity imports.
 */

/**
 * Compute pair-button state from the full candidate set.
 *
 * @param {Array<{account?:string, symbol?:string, tradingsymbol?:string}>} candidates
 * @param {(c: any) => boolean} isEnabledFn   - _isLegEnabled equivalent
 * @returns {{ pairEnabled: boolean, pairedCandidates: any[], firstCheckedSymbol: string }}
 */
export function computePairEnabled(candidates, isEnabledFn) {
  const enabled = candidates.filter(c => isEnabledFn(c));
  const firstAcct = enabled[0]?.account ?? '';
  const pairedCandidates = firstAcct
    ? enabled.filter(c => c.account === firstAcct).slice(0, 2)
    : [];
  const pairEnabled = pairedCandidates.length >= 2;
  const firstCheckedSymbol =
    pairedCandidates[0]?.symbol ?? pairedCandidates[0]?.tradingsymbol ?? '';
  return { pairEnabled, pairedCandidates, firstCheckedSymbol };
}

/**
 * Compute preview quantities from up to 2 paired candidates.
 *
 * @param {Array<{quantity?:number, qty_pos?:number}>} pairedCandidates
 * @returns {{ qty_a: number, qty_b: number, proposedPairedQty: number, orphanQty: number }}
 */
export function computePairPreview(pairedCandidates) {
  const qty_a = Math.abs(
    pairedCandidates[0]?.quantity ?? pairedCandidates[0]?.qty_pos ?? 0
  );
  const qty_b = Math.abs(
    pairedCandidates[1]?.quantity ?? pairedCandidates[1]?.qty_pos ?? 0
  );
  const proposedPairedQty = Math.min(qty_a, qty_b);
  const orphanQty = Math.abs(qty_a - qty_b);
  return { qty_a, qty_b, proposedPairedQty, orphanQty };
}
