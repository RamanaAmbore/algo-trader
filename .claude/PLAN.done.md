# Plan: Fix chain expiry index — key by tradingsymbol prefix (virtual root)

## Context
The chain tab still hangs because the expiry index is keyed by `inst.u` (Kite's `name` field),
which may differ from what the frontend sends. The frontend derives the underlying by stripping
digits from the tradingsymbol: `"CRUDEOILM26SEP7600PE".replace(/\d.*$/, '')` → `"CRUDEOILM"`.
But Kite's `name` field for CRUDEOILM options is `"CRUDE OIL M"` (spaces, different token).
Space-normalization ("CRUDE OIL M" → "CRUDEOILM") is fragile — it doesn't handle all variants
and the fast-path still returns `[]` silently when any key is missing instead of falling back.

## Fix — two changes

### 1. Key expiry index by tradingsymbol prefix everywhere

Replace the `inst.u`-based key with `re.sub(r'\d.*', '', inst.s)` — mirrors the frontend's
`.replace(/\d.*$/, '')`. Works for all variants automatically:

| Tradingsymbol | Frontend key (strip digits) | Old key (inst.u, broken) |
|---------------|----------------------------|--------------------------|
| CRUDEOIL26SEP7600PE | CRUDEOIL | "CRUDE OIL" |
| CRUDEOILM26SEP7600PE | CRUDEOILM | "CRUDE OIL M" |
| NATURALGAS26SEP400CE | NATURALGAS | "NATURAL GAS" |
| GOLD26DEC75000CE | GOLD | "GOLD" |
| NIFTY26MAR22500CE | NIFTY | "NIFTY" |
| BANKNIFTY26MAR50000CE | BANKNIFTY | "BANKNIFTY" |

**`_build_expiries_index`** in `backend/api/routes/instruments.py`:
```python
import re as _re
def _build_expiries_index(items):
    idx: dict[str, set[str]] = {}
    for inst in items:
        if inst.t not in ("CE", "PE") or not inst.x:
            continue
        key = _re.sub(r'\d.*', '', inst.s)   # strip digits + suffix, matches frontend
        if key:
            idx.setdefault(key, set()).add(inst.x)
    return {u: sorted(xs) for u, xs in idx.items()}
```
Remove the old MCX space-normalization diagnostic log block — no longer needed.

**`_chain_quotes_build_sym_map`** in `backend/api/routes/options.py`:
Same pattern — when building the sym-map, key by `re.sub(r'\d.*', '', inst.s)` for the
underlying dimension, so it matches the expiry index. The existing `inst.u` filter can be
dropped entirely for this purpose; CE/PE rows are already filtered by `inst.t`.

### 2. Fast-path guard — fall through when key not in index

```python
# BEFORE (buggy — returns [] when und not in index)
if _exp_index is not None:
    _expiries = _exp_index.get(und, [])
    return ChainQuotesResponse(expiries=_expiries)   # silent empty, no fallback

# AFTER — only short-circuit when key actually found
if _exp_index is not None and und in _exp_index:
    return ChainQuotesResponse(expiries=_exp_index[und])
# else: fall through to slow-path _chain_quotes_sym_lookup
```

Also remove the `[chain-expiry-fast-path]` debug `logger.info` added earlier — log noise.

## Files to change
- `backend/api/routes/instruments.py` — `_build_expiries_index`: key by `re.sub(r'\d.*', '', inst.s)`; remove old MCX space-normalization block and diagnostic log
- `backend/api/routes/options.py` — `_chain_quotes_build_sym_map`: use same tradingsymbol-prefix key; fast-path guard: `and und in _exp_index`
- `backend/tests/test_chain_quotes.py` — update existing MCX tests to use tradingsymbol-prefix keying; add test: fast-path key-miss falls through to slow-path

## Agents
- backend: Apply both changes above. In `instruments.py`: replace `_build_expiries_index` body
  with the `re.sub(r'\d.*', '', inst.s)` approach; remove the old `raw_u`/space-normalization
  block and the `[expiries-index] normalized MCX spaced names` logger.info. In `options.py`:
  update `_chain_quotes_build_sym_map` to key by tradingsymbol prefix; change the fast-path
  guard to `if _exp_index is not None and und in _exp_index:` with direct `_exp_index[und]`;
  remove the `[chain-expiry-fast-path]` logger.info debug line.
- backend-test: In `test_chain_quotes.py` update MCX tests that checked for space-normalized
  keys — they should now check for tradingsymbol-prefix keys (e.g., `"CRUDEOILM"` from
  `"CRUDEOILM26SEP7600PE"`, not from `inst.u`). Add one test: mock `_cache_peek` to return an
  index without the requested key → verify slow-path is called. Add one test: index has key →
  verify fast-path fires and slow-path is NOT called.

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(chain): key expiry index by tradingsymbol prefix (virtual root) — covers all MCX variants + fix fast-path fallback

## Done when
- CRUDEOIL, CRUDEOILM, NATURALGAS, GOLD, GOLDM, NIFTY expiry pickers all load within 1s
- Fast-path key-miss falls through to slow-path (no silent empty response)
- pytest green
