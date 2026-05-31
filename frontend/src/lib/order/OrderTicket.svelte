<script>
  // Reusable order-placement ticket — single component for every
  // order op the platform needs (open / close / modify / repeat /
  // cancel) across every instrument (EQ / FUT / OPT / commodities).
  //
  // Phase 1 scope (this file): visual shell + DRAFT mode wired.
  // PAPER / LIVE submit paths come in phase 2 (backend endpoints)
  // and phase 3 (live broker wiring per the existing
  // execution.live.<action> setting flag).
  //
  // Calling pages own:
  //   - the symbol + side + qty defaults (passed as props)
  //   - the onSubmit handler that decides what DRAFT does (typically
  //     append to a local drafts[] array)
  //   - opening / closing the ticket via <{#if showTicket}> wrap
  //
  // The ticket itself owns:
  //   - field state (qty / type / price / trigger / variety …)
  //   - validation (price required when LIMIT, trigger when SL …)
  //   - mode toggle (DRAFT today; PAPER / LIVE in phase 2 / 3)
  //   - viewport-bounded modal positioning
  //
  // Component is intentionally "dumb" — it doesn't know about the
  // page's drafts array, the strategy state, the broker. Every
  // outcome routes through onSubmit(payload).

  import { onMount, untrack } from 'svelte';
  import { get } from 'svelte/store';
  import OrderDepth from './OrderDepth.svelte';
  import Select from '$lib/Select.svelte';
  import { placeTicketOrder, previewOrderMargin, fetchAccounts, fetchFunds, modifyOrder } from '$lib/api';
  import { aggFmt } from '$lib/format';
  import { executionMode } from '$lib/stores';
  import { getInstrument } from '$lib/data/instruments';

  /** @type {{
   *   symbol:    string,
   *   exchange?: string,
   *   side?:     'BUY' | 'SELL',
   *   action?:   'open' | 'close' | 'modify' | 'repeat' | 'cancel',
   *   qty?:      number,
   *   product?:  'CNC' | 'MIS' | 'NRML',
   *   orderType?:'MARKET' | 'LIMIT' | 'SL' | 'SL-M',
   *   variety?:  'regular' | 'co' | 'bo' | 'amo' | 'iceberg',
   *   price?:    number,
   *   trigger?:  number,
   *   lotSize?:  number,
   *   accounts?: string[],
   *   account?:  string,
   *   orderId?:  string,
   *   defaultMode?:    'draft' | 'paper' | 'live',
   *   availableModes?: Array<'draft' | 'paper' | 'live'>,
   *   currentQty?: number,
   *   onSubmit:  (payload: any) => void | Promise<void>,
   *   onClose:   () => void,
   *   onAddToBasket?: ((payload: any) => void) | null,
   *   basketMode?: boolean,
   *   onAccountChange?: (account: string) => void,
   * }} */
  let {
    symbol,
    exchange  = '',
    side      = /** @type {'BUY' | 'SELL'} */ ('BUY'),
    action    = /** @type {'open' | 'close' | 'modify' | 'repeat' | 'cancel'} */ ('open'),
    qty       = 0,
    product   = /** @type {'CNC' | 'MIS' | 'NRML' | undefined} */ (undefined),
    orderType = /** @type {'MARKET' | 'LIMIT' | 'SL' | 'SL-M'} */ ('LIMIT'),
    variety   = /** @type {'regular' | 'co' | 'bo' | 'amo' | 'iceberg'} */ ('regular'),
    price     = /** @type {number | undefined} */ (undefined),
    trigger   = /** @type {number | undefined} */ (undefined),
    lotSize   = 0,
    accounts  = /** @type {string[]} */ ([]),
    account   = '',
    // Existing-order id — required for action='modify'/'cancel' so
    // the submit path knows which order to mutate. Ignored for
    // action='open'.
    orderId   = '',
    // Initial mode pill the ticket opens on. Surfaces with no drafts
    // concept (PerformancePage row click) typically pass 'paper';
    // surfaces with a drafts panel (admin/options) keep 'draft'.
    defaultMode    = /** @type {'draft' | 'paper' | 'live'} */ ('live'),
    // Which mode pills the operator can see. PAPER is no longer a
    // user-facing choice — on dev all orders are paper-only via the
    // branch gate; on prod the per-action execution.live.* flags
    // decide paper vs live for each action. Operators pick between
    // DRAFT (page-local what-if) and LIVE (submit to backend).
    availableModes = /** @type {Array<'draft'|'paper'|'live'>} */ (['draft', 'live']),
    // Signed qty of the operator's existing position when the ticket
    // is opened from a position-row click. Drives the side toggle's
    // ADD/CLOSE labels — operator thinks in "I want to add to this
    // position" or "I want to close this position", not "I want to
    // BUY or SELL". The bottom submit button still shows the resolved
    // BUY/SELL so the actual broker action is unambiguous.
    //   currentQty > 0  → existing LONG  ⇒ BUY pill = ADD,  SELL = CLOSE
    //   currentQty < 0  → existing SHORT ⇒ SELL pill = ADD, BUY  = CLOSE
    //   currentQty == 0 → no existing position ⇒ plain BUY / SELL labels
    currentQty = 0,
    onSubmit,
    onClose,
    // Pushed back to OrderEntryShell when the operator picks a
    // different account from this tab's selector. Shell uses it to
    // sync the other tabs (command / chain) to the same account so
    // they all submit through the same Kite handle.
    onAccountChange = /** @type {((account: string) => void) | null} */ (null),
    // Optional "+ Basket" callback — when set, the ticket renders
    // an "Add to basket" action alongside the primary Submit. The
    // caller pushes the leg into its own basket panel (same pill
    // shape as a quick-add). Set to null/undefined to hide.
    onAddToBasket = /** @type {((payload: any) => void) | null} */ (null),
    // When true (shell's Chain tab is active), the primary Submit
    // button reads "Add to basket" and routes through onAddToBasket
    // instead of firing to the backend. The ticket stays open after
    // add so the operator can adjust the leg and add another.
    basketMode = false,
  } = $props();

  // Derived label map for the side toggle. Keeps the actual _side
  // state as 'BUY' / 'SELL' (the broker payload never changes); only
  // the display label flips between BUY/SELL and ADD/CLOSE.
  const sideLabels = $derived.by(() => {
    if (!currentQty || currentQty === 0) {
      return { BUY: 'BUY', SELL: 'SELL' };
    }
    if (currentQty > 0) {
      // Long position: buying more = ADD, selling = CLOSE.
      return { BUY: 'ADD', SELL: 'CLOSE' };
    }
    // Short position: selling more = ADD, buying back = CLOSE.
    return { BUY: 'CLOSE', SELL: 'ADD' };
  });

  // Derived instrument kind from the tradingsymbol — simple suffix
  // match. Drives which fields show.
  const kind = $derived.by(() => {
    const s = (symbol || '').toUpperCase();
    if (/CE$/.test(s)) return 'CE';
    if (/PE$/.test(s)) return 'PE';
    if (/FUT$/.test(s)) return 'FUT';
    return 'EQ';
  });
  const isOption = $derived(kind === 'CE' || kind === 'PE');
  const isFuture = $derived(kind === 'FUT');
  const isEquity = $derived(kind === 'EQ');

  // Default product based on instrument when caller didn't specify.
  const productVal = $derived(product ?? (isEquity ? 'CNC' : 'NRML'));
  const productOptions = $derived(isEquity
    ? ['CNC', 'MIS']
    : ['NRML', 'MIS']);

  // Local form state — start from prop defaults, then operator edits.
  let _side    = $state(side);

  // Resolved lot size — starts from the prop; may be updated on mount
  // via the instruments cache when the caller didn't supply one.
  let _lotSize = $state(lotSize);

  // Qty path:
  //   - _lotSize > 0  → operator edits in LOTS via [−] [N] [+], the
  //     resolved qty `_lots * _lotSize` flows into _qty. Mirrors the
  //     chain picker so both surfaces read consistently.
  //   - _lotSize == 0 → cash equity / no lot concept; fall back to
  //     raw number input bound directly to _qty.
  // Initial _lots comes from the caller-supplied qty (rounded to the
  // nearest whole lot, floored at 1). When qty is also 0 we start at
  // 1 lot so the ticket opens ready-to-submit.
  let _lots = $state(
    _lotSize > 0
      ? Math.max(1, Math.round((Number(qty) || _lotSize) / _lotSize))
      : 1
  );
  let _qty     = $state(qty || _lotSize || (isEquity ? 1 : 0));
  // Track whether the operator has touched _lots manually. If they
  // haven't, late-resolving _lotSize (instrument cache warming after
  // ticket mount) should recompute _lots from the original `qty` prop
  // so the ticket displays "2 Lots" instead of staying stuck at the
  // fallback "1 Lot". Set true by the +/- steppers + direct input.
  let _lotsTouched = $state(false);
  // When _lotSize transitions from 0 → positive (instrument cache
  // resolved after mount), recompute _lots from the caller's original
  // qty prop. Skipped once the operator has manually adjusted.
  let _prevLotSize = _lotSize;
  $effect(() => {
    if (_prevLotSize === 0 && _lotSize > 0 && !_lotsTouched) {
      _lots = Math.max(1, Math.round((Number(qty) || _lotSize) / _lotSize));
    }
    _prevLotSize = _lotSize;
  });
  // Keep _qty in sync with _lots × _lotSize so submit + validation see
  // the resolved raw quantity. Skipped when _lotSize=0 (operator types
  // qty directly).
  $effect(() => {
    if (_lotSize > 0) _qty = _lots * _lotSize;
  });

  // When the operator flips side, reset to 1 lot (ADD direction) or
  // restore the held qty (CLOSE direction). "ADD" = same direction as
  // the existing position; "CLOSE" = opposite.
  let _prevSide = _side;
  $effect(() => {
    if (!currentQty || currentQty === 0 || _lotSize <= 0) return;
    const cur = _side;
    if (cur === _prevSide) return;
    _prevSide = cur;
    const isAdd = (currentQty > 0 && cur === 'BUY') ||
                  (currentQty < 0 && cur === 'SELL');
    if (isAdd) {
      _lots = 1;
    } else {
      _lots = Math.max(1, Math.round(Math.abs(currentQty) / _lotSize));
    }
  });

  function stepLots(/** @type {number} */ delta) {
    _lots = Math.max(1, Math.floor((Number(_lots) || 1) + delta));
    _lotsTouched = true;  // operator owns _lots from here
  }
  let _type    = $state(orderType);
  let _variety = $state(variety);
  // Validity (Time-in-Force): DAY by default. IOC (Immediate-Or-Cancel)
  // for fast-trading scenarios where partial fills are acceptable but
  // unfilled remainder should drop instead of resting. Kite supports
  // DAY + IOC; TTL/GTT live on different code paths so they're not
  // exposed here. Industry analogue: every order book (IB TWS, ToS,
  // Kite Web) surfaces DAY/IOC as inline pills.
  let _validity = $state('DAY');
  let _price   = $state(price ?? '');
  let _trigger = $state(trigger ?? '');
  let _product = $state(productVal);
  // Initial mode:
  //   1. defaultMode prop wins when the caller explicitly picked one
  //      AND it's in availableModes.
  //   2. Otherwise fall back to the global executionMode store
  //      (operator's last-used mode — synced from /admin/live toggle
  //      in a follow-up). The store normalises shadow/sim/replay to
  //      'paper' for surfaces that only expose draft/paper/live pills.
  //   3. Last resort: first available mode.
  function _resolveInitialMode() {
    const normalise = (/** @type {string} */ m) => {
      if (m === 'sim' || m === 'replay' || m === 'shadow') return 'paper';
      return /** @type {'draft'|'paper'|'live'} */ (m);
    };
    if (availableModes.includes(defaultMode)) return defaultMode;
    const fromStore = normalise(get(executionMode) || 'paper');
    if (availableModes.includes(fromStore)) return fromStore;
    return availableModes[0] || 'draft';
  }
  let _mode    = $state(/** @type {'draft' | 'paper' | 'live'} */ (
    untrack(_resolveInitialMode)
  ));
  // Chase toggle — when on, the backend's paper engine re-quotes
  // the limit each tick until the order fills (or hits the chase-
  // attempt cap). Default ON: industry-standard "fire and forget"
  // workflow. When off, the order rests at the initial limit and
  // only fills if the market naturally crosses it. MARKET / SL-M
  // ignore the toggle (no limit to chase).
  let _chase    = $state(true);
  // Chase aggressiveness — analogous to IBKR Adaptive Algo's
  // Patient / Normal / Urgent. Defaults to 'low' (passive — pegs
  // to your own side, waits for the market) so a plain Submit
  // doesn't accidentally cross the spread. Operator can promote
  // to 'med' / 'high' inline before submit.
  let _chaseAgg = $state(/** @type {'low'|'med'|'high'} */ ('low'));

  // Auto-fill plumbing — the OrderDepth child polls the quote
  // every 1.2 s and bubbles each fresh response here via
  // onDepthQuote. We pre-fill the limit price with the marketable
  // side (BUY → ask, SELL → bid) so the operator doesn't have to
  // type a price every time. Once the operator types into the
  // field, `_priceTouched` flips true and we stop overwriting
  // their input. Flipping side resets to the new marketable side
  // unless they've typed.
  // Caller can pre-supply `price` to suppress auto-fill (e.g. a
  // close-position flow that wants the operator's last limit).
  // Untrack the read so we capture the initial value once — this
  // is intentional, the operator's edits flip the flag from there.
  let _priceTouched = $state(untrack(() => typeof price === 'number' && price > 0));
  /** @type {{ bid: number|null, ask: number|null, ltp: number|null } | null} */
  let _lastQuote = $state(null);

  // Tick size for NSE F&O / equity is ₹0.05; commodities are
  // typically ₹0.05 or coarser (CRUDEOIL ₹1.00, GOLDM ₹1.00). Kite
  // rejects orders whose price isn't an exact tick multiple — the
  // bid/ask from depth ARE tick-aligned, but JS floating-point can
  // turn 590.80 into 590.7999999999999 which Kite then refuses.
  // Snap to the nearest 0.05 + round to 2 decimals to scrub away
  // both float artifacts and any operator-typed extra decimals.
  function _roundToTick(/** @type {number|string} */ px,
                        /** @type {number} */ tick = 0.05) {
    const n = Number(px);
    if (!Number.isFinite(n) || n <= 0) return n;
    return Math.round((n / tick) + Number.EPSILON) * tick;
  }
  function _formatTick(/** @type {number} */ n) {
    // Always render with 2 decimals for paise-aligned ticks. Doesn't
    // round (caller did that already); just stringifies cleanly.
    return Number.isFinite(n) ? Number(n.toFixed(2)) : n;
  }
  function _autoFillFromQuote() {
    if (_priceTouched) return;
    if (_type !== 'LIMIT' && _type !== 'SL') return;
    if (!_lastQuote) return;
    const px = _side === 'BUY' ? _lastQuote.ask : _lastQuote.bid;
    // Fall back to LTP when the corresponding side has no depth
    // (off-hours, illiquid contracts) so the operator isn't left
    // with a blank field.
    const fallback = (px && px > 0) ? px : _lastQuote.ltp;
    if (fallback && fallback > 0) _price = _formatTick(_roundToTick(fallback));
  }
  function onDepthQuote(/** @type {any} */ q) {
    _lastQuote = q ? {
      bid: q.bid ?? null,
      ask: q.ask ?? null,
      ltp: q.ltp ?? null,
    } : null;
    _autoFillFromQuote();
  }
  // Re-fill when the operator flips BUY ⇄ SELL or changes order
  // type (LIMIT ⇄ MARKET ⇄ SL — only LIMIT/SL show the price
  // field, but the helper guards that).
  $effect(() => {
    void _side; void _type;
    _autoFillFromQuote();
  });

  // Self-fetched real account list — backstop for when the caller
  // didn't (or couldn't) supply one. /api/accounts/ is jwt-guarded
  // but doesn't mask, so any signed-in user gets real account_ids
  // even if the page's positions came back masked.
  /** @type {string[]} */
  let _selfAccounts = $state([]);

  // Per-account funds — used to render the "Avail margin" pill next to
  // the account picker so the operator can see whether the chosen
  // account has enough room to place this order. Populated lazily when
  // the modal mounts (PAPER / LIVE submits need it; DRAFT doesn't but
  // the cost is one cached fetch). Each row carries:
  //   { account, cash, avail_margin, used_margin, collateral }
  /** @type {Array<{account:string, cash:number, avail_margin:number,
   *                used_margin:number, collateral:number}>} */
  let _funds = $state([]);
  // Pick the funds row to surface in the pill. Order:
  //   1. exact match on the currently-picked account (most useful)
  //   2. summed totals across every loaded fund row (fallback when
  //      the operator hasn't picked an account yet, or when /accounts
  //      hasn't resolved so the picker is empty)
  //   3. null — funds payload empty (401 / 403 / fetch failure)
  // The earlier "exact match only" rule made the pill silently
  // disappear whenever the account picker collapsed (single-acct
  // case OR demo session before sign-in), even though we already
  // had per-account funds in memory. Falling back to the summed
  // view keeps the operator informed in those edge cases.
  const _accountFunds = $derived.by(() => {
    if (!_funds.length) return null;
    if (_account) {
      const match = _funds.find(r => r.account === _account);
      if (match) return match;
    }
    let cash = 0, am = 0, um = 0, col = 0;
    for (const f of _funds) {
      cash += Number(f?.cash || 0);
      am   += Number(f?.avail_margin || 0);
      um   += Number(f?.used_margin  || 0);
      col  += Number(f?.collateral   || 0);
    }
    return {
      account:      'TOTAL',
      cash, avail_margin: am, used_margin: um, collateral: col,
    };
  });

  // Account list shown by the picker — caller's `accounts` prop
  // wins when populated; otherwise we use whatever we self-fetched.
  // A masked-only list (e.g. ZG####) is treated as empty so we
  // don't pre-pick an unroutable value.
  function _isRealAcct(/** @type {string|null|undefined} */ a) {
    return !!(a && !String(a).includes('#'));
  }
  const _accounts = $derived.by(() => {
    const fromProp = (accounts || []).filter(_isRealAcct);
    if (fromProp.length) return fromProp;
    return _selfAccounts.filter(_isRealAcct);
  });

  // Account — explicit operator choice for which Kite handle the
  // order routes through. Required for PAPER and LIVE; ignored in
  // DRAFT. Initialized empty and reactively seeded from the prop /
  // picker via the effect below — this lets a late-arriving caller
  // account list (the common race: /api/accounts/ resolves AFTER
  // the operator clicks +) auto-select once it lands. A masked
  // ZG#### is unroutable so we never seed it as a default.
  let _account = $state('');

  // Reactive seed:
  //   1. Caller-supplied `account` prop wins when it's a real value.
  //   2. Otherwise, single real account in the picker → pre-pick it.
  //   3. If the seeded value disappears from the picker (caller
  //      flips symbol / picker reloads), reset.
  $effect(() => {
    const propPick = _isRealAcct(account) ? String(account) : '';
    // Seed from prop immediately even if _accounts hasn't resolved yet —
    // the row already carries the correct unmasked account_id and we
    // should trust it rather than waiting for the self-fetch to land.
    if (!_account && propPick) {
      _account = propPick;
      return;
    }
    if (!_account && _accounts.length === 1) {
      _account = _accounts[0];
      return;
    }
    if (_account && _accounts.length && !_accounts.includes(_account)) {
      // Re-check against propPick first so a late-arriving list that
      // confirms the prop doesn't clear it.
      _account = propPick || (_accounts.length === 1 ? _accounts[0] : '');
    }
  });

  // Push picker changes back to the shell so the other tabs (command /
  // chain) re-sync. Guard against the no-op echo (when shell pushes
  // a new prop value, our $effect above sets `_account` to match,
  // which would otherwise fire onAccountChange in a loop). Untrack the
  // callback read.
  let _lastNotifiedAcct = '';
  $effect(() => {
    if (_account && _account !== _lastNotifiedAcct && _account !== account) {
      _lastNotifiedAcct = _account;
      onAccountChange?.(_account);
    } else if (_account === account) {
      _lastNotifiedAcct = _account;
    }
  });

  // Field visibility derived from order type + variety.
  const showLimit   = $derived(_type === 'LIMIT' || _type === 'SL');
  const showTrigger = $derived(_type === 'SL' || _type === 'SL-M');

  // Validation — applied client-side; backend validates again before
  // hitting the broker. Lot-size check protects against rejections
  // for non-multiple quantities (NIFTY lot 50, BANKNIFTY 15, etc.).
  const validationErr = $derived.by(() => {
    if (!Number(_qty) || Number(_qty) <= 0) {
      if (_lotSize > 0) return `Qty required (1 lot = ${_lotSize} for ${symbol})`;
      return 'Qty required';
    }
    if (_lotSize > 0 && Number(_qty) % _lotSize !== 0) {
      return `Qty must be a multiple of lot ${_lotSize}`;
    }
    if (showLimit   && !Number(_price))   return 'Limit price required';
    if (showTrigger && !Number(_trigger)) return 'Trigger price required';
    if (Number(_price) < 0)   return 'Price must be ≥ 0';
    if (Number(_trigger) < 0) return 'Trigger must be ≥ 0';
    if ((_mode === 'paper' || _mode === 'live') && !_account) {
      return 'Pick an account';
    }
    return '';
  });

  /** Build a basket-leg payload from the modal's current state.
   *  Caller (admin/options) folds this into chainBasket so the leg
   *  renders as a regular basket pill alongside quick-adds. */
  function _basketPayload() {
    return {
      side:     _side,
      sym:      symbol,
      exchange: exchange || 'NFO',
      account:  _account,
      lots:     Math.max(1, Number(_lots) || 1),
      lotSize:  Number(_lotSize) || 1,
      product:  _product,
      limit:    showLimit ? Number(_roundToTick(_price)) || 0 : 0,
      chaseAgg: showLimit && _chase ? _chaseAgg : 'low',
    };
  }

  function addToBasket() {
    if (!onAddToBasket) return;
    if (validationErr) return;
    onAddToBasket(_basketPayload());
    onClose();
  }

  let submitting = $state(false);
  /** @type {string} */ let submitErr = $state('');

  // ── Margin / cash preview ────────────────────────────────────────
  // Calls /api/orders/preflight on field change (debounced 350 ms) so
  // the operator sees the SPAN + premium / cash cost of the order BEFORE
  // they click Submit. Mirrors IB TWS's "Order Confirmation / what-if"
  // preview and NinjaTrader's buying-power panel — standard for any
  // trading desk where margin gates the order. Backend reuses the same
  // preflight that the live-order submit path runs, so the numbers the
  // operator sees here are the same numbers the broker will charge.
  let _marginPreview = $state(/** @type {any} */ (null));
  let _marginLoading = $state(false);
  /** @type {ReturnType<typeof setTimeout> | null} */
  let _marginTimer = null;

  $effect(() => {
    // Track everything that materially affects the basket_margin number.
    // (Svelte 5 picks up reads inside this function automatically.)
    const _watchers = [
      _side, _qty, _account, _product, _type, _variety,
      _price, _trigger, symbol, exchange,
    ];
    void _watchers;

    if (_marginTimer) {
      clearTimeout(_marginTimer);
      _marginTimer = null;
    }
    // Skip when the ticket is incomplete — no point hitting Kite with
    // a half-typed form. Also skip drafts (no broker; cost is the limit
    // × qty multiplication which the operator can already see).
    if (!_account || !symbol || Number(_qty) <= 0 || _mode === 'draft') {
      _marginPreview = null;
      _marginLoading = false;
      return;
    }
    _marginLoading = true;
    _marginTimer = setTimeout(async () => {
      try {
        const payload = {
          account: _account,
          tradingsymbol: symbol,
          exchange: exchange || 'NFO',
          quantity: Number(_qty),
          side: _side,
          order_type: _type,
          product: _product,
          variety: _variety,
          validity: _validity,
          price: showLimit ? Number(_price) || 0 : 0,
          trigger_price: showTrigger ? Number(_trigger) || 0 : 0,
        };
        _marginPreview = await previewOrderMargin(payload);
      } catch (e) {
        _marginPreview = { error: (e?.message || 'preview failed').slice(0, 60) };
      } finally {
        _marginLoading = false;
      }
    }, 350);

    return () => {
      if (_marginTimer) {
        clearTimeout(_marginTimer);
        _marginTimer = null;
      }
    };
  });

  // Inline success state — shown briefly inside the modal after a
  // successful PAPER / LIVE submit so the operator sees confirmation
  // before the modal closes. Without it the modal disappears silently
  // and the operator has no idea whether the order actually landed.
  /** @type {string} */ let submitOk = $state('');

  async function submit() {
    if (validationErr) return;
    // ── action='modify' branch ─────────────────────────────────
    // Modifying an existing working order — bypass the
    // place/ticket pipeline entirely. Calls PUT /api/orders/{id}
    // with whatever fields the operator changed (price, qty,
    // order_type, trigger). Mode pills + chase + L/M/H don't
    // apply (those are place-time concerns). Account, symbol,
    // and side are locked in the UI.
    if (action === 'modify') {
      if (!orderId) {
        submitErr = 'Modify path requires an order id.';
        return;
      }
      submitting = true; submitErr = ''; submitOk = '';
      try {
        const payload = {
          account:       _account,
          quantity:      Number(_qty) || undefined,
          price:         showLimit   ? _roundToTick(_price)   : null,
          trigger_price: showTrigger ? _roundToTick(_trigger) : null,
          order_type:    _type,
          variety:       _variety,
          validity:      _validity,
        };
        await modifyOrder(orderId, payload);
        submitOk = `Order #${orderId} modified`;
        // Surface the diff to the caller so the page can refresh
        // its order list / log the change. Modal stays open with the
        // Exit button until the operator dismisses it (was 1.2 s auto-
        // close; operator couldn't read the confirmation in time).
        await onSubmit({ action: 'modify', orderId, ...payload });
      } catch (e) {
        submitErr = /** @type {any} */ (e)?.message || String(e);
      } finally {
        submitting = false;
      }
      return;
    }

    // LIVE submits straight through — no confirm dialog. The pre-submit
    // margin/cost row above the Submit button already tells the operator
    // exactly what they're committing to. Backend still enforces both
    // gates (prod-branch + execution.paper_trading_mode=False) before
    // any broker call, so accidental clicks on dev or in paper mode
    // can't reach Kite.
    const payload = {
      mode:           _mode,
      action,
      symbol,
      exchange,
      side:           _side,
      quantity:       Number(_qty),
      product:        _product,
      order_type:     _type,
      variety:        _variety,
      validity:       _validity,
      price:          showLimit   ? _roundToTick(_price)   : null,
      trigger_price:  showTrigger ? _roundToTick(_trigger) : null,
      account:        _account,
      // Chase only carries on price-bearing order types; MARKET /
      // SL-M ignore it on the backend, but we still ship the flag
      // so the AlgoOrder row records the intent for replay.
      chase:               showLimit ? _chase : false,
      chase_aggressiveness: showLimit && _chase ? _chaseAgg : 'low',
    };
    submitting = true; submitErr = ''; submitOk = '';
    /** @type {any} */
    let brokerResp = null;
    try {
      // PAPER + LIVE both route through the backend. DRAFT hands off
      // to the caller's onSubmit only (no API call).
      if (_mode === 'paper' || _mode === 'live') {
        brokerResp = await placeTicketOrder({
          mode:             _mode,
          side:             _side,
          tradingsymbol:    symbol,
          exchange:         exchange || 'NFO',
          quantity:         Number(_qty),
          product:          _product,
          order_type:       _type,
          variety:          _variety,
          validity:         _validity,
          price:            showLimit   ? _roundToTick(_price)   : null,
          trigger_price:    showTrigger ? _roundToTick(_trigger) : null,
          account:          _account,
          chase:                showLimit ? _chase : false,
          chase_aggressiveness: showLimit && _chase ? _chaseAgg : 'low',
        });
        // Show inline confirmation so the operator sees the order
        // landed; modal stays open until the operator clicks Exit.
        // Backend returns {order_id, mode, status, detail} — surface
        // a verbose summary line with side / qty / symbol / price.
        const oid   = brokerResp?.order_id || '?';
        const px    = showLimit && _price ? `@₹${_roundToTick(_price)}` : '@MKT';
        submitOk = (
          `${(_mode || '').toUpperCase()} ${_side} ${_qty} ${symbol} ${px} · ` +
          `#${oid}`
        );
      }
      // Notify the caller — DRAFT mode appends to drafts[]; PAPER /
      // LIVE let the caller refresh its local view if it wants to.
      // Thread the broker response so the caller can surface order_id /
      // status (e.g. as a non-blocking completion toast) without
      // re-parsing the inline submitOk string.
      await onSubmit({ ...payload, broker_response: brokerResp });
      // DRAFT closes immediately; PAPER / LIVE stay open with an Exit
      // button so the operator can read the placed-order line. The
      // previous auto-close (200 / 600 ms) was racing the operator's
      // gaze — the inline "✓ placed" flash was visible for a heartbeat
      // then gone, leaving them unsure what landed.
      if (_mode === 'draft') {
        onClose();
      }
    } catch (e) {
      submitErr = /** @type {any} */ (e)?.message || String(e);
    } finally {
      submitting = false;
    }
  }

  // Esc to close + backstop /api/accounts/ self-fetch. Runs when
  // the caller didn't supply real accounts (the chain picker pre-
  // /accounts/ load, the per-row buttons before the page poll
  // landed, generic order surfaces that don't know about Kite at
  // all). /accounts is jwt-guarded but doesn't mask, so we get the
  // real account_ids for any signed-in operator. 401 / 403 leaves
  // _selfAccounts empty and the picker collapses gracefully.
  onMount(() => {
    const onKey = (/** @type {KeyboardEvent} */ e) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);

    // Lot-size auto-fill from instruments cache when the caller
    // didn't supply lotSize (= 0). Equity keeps qty=1 raw; F&O
    // must trade in whole lots. Uses the synchronous getInstrument()
    // which returns null before loadInstruments() resolves — in that
    // case the fallback is 1 and the operator can adjust.
    if (_lotSize === 0 && !isEquity && symbol) {
      const inst = getInstrument(symbol.toUpperCase());
      if (inst?.ls && Number(inst.ls) > 0) {
        const ls = Number(inst.ls);
        _lotSize = ls;
        // Seed _lots = 1 and let the $effect derive _qty.
        _lots = Math.max(1, qty > 0 ? Math.round(qty / ls) : 1);
      }
    }
    const propRealCount = (accounts || []).filter(_isRealAcct).length;
    if (!propRealCount) {
      fetchAccounts()
        .then(/** @param {any} r */ (r) => {
          const list = (r?.accounts || [])
            .map(/** @param {any} a */ (a) => String(a?.account_id || ''))
            .filter(Boolean);
          _selfAccounts = list;
        })
        .catch(() => { /* silent — picker just stays empty */ });
    }
    // Funds — drives the "Avail margin" pill next to the account
    // picker. Cached for 30 s on the backend so re-opening the modal
    // is instant. 401 / 403 (anonymous demo) leaves _funds empty and
    // the pill collapses gracefully.
    fetchFunds()
      .then(/** @param {any} r */ (r) => {
        _funds = (r?.rows || []).filter(/** @param {any} f */ (f) =>
          f && f.account && f.account !== 'TOTAL'
        );
      })
      .catch(() => { /* silent — pill stays hidden */ });
    return () => window.removeEventListener('keydown', onKey);
  });
</script>

<div class="ot-overlay" role="dialog" aria-modal="true" aria-label="Place order"
     onclick={onClose}>
  <div class="ot-modal" role="document" onclick={(e) => e.stopPropagation()}>
    <div class="ot-header">
      <div class="ot-symbol">
        <span class="ot-symbol-text">{symbol}</span>
        <span class="ot-symbol-meta">
          {exchange ? exchange + ' · ' : ''}
          {kind}{_lotSize ? ' · lot ' + _lotSize : ''}
          {action !== 'open' ? ' · ' + action.toUpperCase() : ''}
        </span>
      </div>
      <button type="button" class="ot-close" title="Close" aria-label="Close" onclick={onClose}>×</button>
    </div>

    <!-- Side toggle — locked when modifying an existing order
         (Kite doesn't support flipping side on a working order;
         the operator has to cancel + re-place). Click is a no-op
         in that case + the button visibly reads as disabled. -->
    <div class="ot-row">
      <!-- When the operator opens this ticket from a current
           position (currentQty != 0), the pills swap labels to ADD /
           CLOSE — what they're actually thinking. The underlying
           _side state stays as 'BUY' / 'SELL' so the broker payload
           never changes; only the visible glyph flips. The bottom
           submit button continues to show the resolved BUY/SELL so
           the actual broker action is unambiguous. -->
      <div class="ot-side-block">
        <span class="ot-label">Side</span>
        <div class="ot-side-toggle" class:ot-locked={action === 'modify'}>
          <button type="button" class="ot-side-btn ot-side-buy"  class:on={_side === 'BUY'}
                  disabled={action === 'modify'}
                  title={currentQty
                    ? (sideLabels.BUY + ' (places a BUY order)')
                    : 'BUY this contract'}
                  onclick={() => action !== 'modify' && (_side = 'BUY')}>{sideLabels.BUY}</button>
          <button type="button" class="ot-side-btn ot-side-sell" class:on={_side === 'SELL'}
                  disabled={action === 'modify'}
                  title={currentQty
                    ? (sideLabels.SELL + ' (places a SELL order)')
                    : 'SELL this contract'}
                  onclick={() => action !== 'modify' && (_side = 'SELL')}>{sideLabels.SELL}</button>
        </div>
      </div>
      <div class="ot-qty-block">
        {#if _lotSize > 0}
          <!-- Lots-driven qty input — only +/− steppers, no dropdown.
               Operator preference + the dropdown was spilling out of
               the row on narrow viewports. Format mirrors the chain
               picker exactly: [−] N [+] (× 50 = 50). The N is a tiny
               read-only display; for big jumps, the operator can
               click + repeatedly or open the underlying contract via
               another path. -->
          <label class="ot-label" for="ot-lots">Lots</label>
          <div class="ot-lots-row">
            <button type="button" class="ot-lots-step"
                    onclick={() => stepLots(-1)}
                    disabled={_lots <= 1}
                    aria-label="Decrease lots">−</button>
            <span class="ot-lots-val" id="ot-lots" aria-label="Lots">{_lots}</span>
            <button type="button" class="ot-lots-step"
                    onclick={() => stepLots(1)}
                    aria-label="Increase lots">+</button>
            <span class="ot-meta">(× {_lotSize} = {_qty})</span>
          </div>
        {:else}
          <label class="ot-label" for="ot-qty">Qty</label>
          <input id="ot-qty" type="number" class="ot-input ot-num"
                 step="1" min="1"
                 bind:value={_qty} />
        {/if}
      </div>
    </div>

    <!-- Account selector — required for PAPER + LIVE so the operator
         picks WHICH Kite handle the order routes to. Reads from the
         derived `_accounts` list so a late-arriving caller account
         list (or the self-fetch backstop) auto-populates without
         remounting the ticket. -->
    {#if _accounts.length}
      <div class="ot-row">
        <div class="ot-label-block">
          <span class="ot-label">Account</span>
          {#if _accounts.length === 1}
            <input id="ot-account" type="text" class="ot-input ot-account-readonly"
                   value={_account} readonly />
          {:else}
            <Select id="ot-account"
                    bind:value={_account}
                    placeholder="Pick an account…"
                    ariaLabel="Account"
                    options={_accounts.map(a => ({ value: a, label: a }))} />
          {/if}
        </div>
      </div>
    {/if}

    <!-- Per-account funds pill — sits ABOVE the Type/Product row so
         the operator always sees Avail margin + Cash before picking
         a side. Lifted out of the account-list gate so it surfaces
         even when the picker is empty (single-account scenarios, or
         a delayed /accounts fetch); falls back to the summed totals
         across every loaded fund row when no specific account is
         selected. Negative margin (margin debt) flips the pill red. -->
    {#if _accountFunds}
      <div class="ot-funds" class:ot-funds-low={_accountFunds.avail_margin < 0}
           title={_accountFunds.account === 'TOTAL'
             ? 'Sum across every loaded broker account'
             : `Funds for ${_accountFunds.account}`}>
        {#if _accountFunds.account === 'TOTAL'}
          <span class="ot-funds-k">Total</span>
        {/if}
        <span class="ot-funds-k">Avail margin</span>
        <span class="ot-funds-v">₹{aggFmt(_accountFunds.avail_margin || 0)}</span>
        <span class="ot-funds-sep">·</span>
        <span class="ot-funds-k">Cash</span>
        <span class="ot-funds-v">₹{aggFmt(_accountFunds.cash || 0)}</span>
        {#if _accountFunds.used_margin > 0}
          <span class="ot-funds-sep">·</span>
          <span class="ot-funds-k">Used</span>
          <span class="ot-funds-v">₹{aggFmt(_accountFunds.used_margin || 0)}</span>
        {/if}
      </div>
    {/if}

    <!-- Type + Product pills — kept on a single row even on narrow
         viewports. Earlier each label sat ABOVE its pill row, and
         the row + the pills both wrapped — the modal felt cluttered.
         Now: inline `Type:` / `Product:` labels next to compact
         pills, ot-pills nowrap, ot-row nowrap. Pills shrink slightly
         (font 0.6 → 0.55rem, padding tightened) to leave headroom. -->
    <div class="ot-row ot-row-tight">
      <div class="ot-label-inline">
        <label class="ot-label">Type</label>
        <div class="ot-pills ot-pills-nowrap">
          {#each ['MARKET', 'LIMIT', 'SL', 'SL-M'] as t}
            <button type="button" class="ot-pill" class:on={_type === t}
                    onclick={() => _type = /** @type {any} */ (t)}>{t}</button>
          {/each}
        </div>
      </div>
      <div class="ot-label-inline">
        <label class="ot-label">Product</label>
        <div class="ot-pills ot-pills-nowrap">
          {#each productOptions as p}
            <button type="button" class="ot-pill" class:on={_product === p}
                    onclick={() => _product = /** @type {any} */ (p)}>{p}</button>
          {/each}
        </div>
      </div>
    </div>

    <!-- Variety + Validity — REG / AMO / CO are backend-wired in
         routes/orders.py (_VARIETIES = {regular, amo, co}). BO +
         iceberg are surfaced disabled with an aria-disabled hint so
         the operator can see them on the roadmap without being able
         to submit something the backend will reject. Validity:
         DAY (default) + IOC (Immediate-Or-Cancel) — both pass
         through directly to Kite's `validity` field. -->
    <div class="ot-row ot-row-tight">
      <div class="ot-label-inline">
        <label class="ot-label">Variety</label>
        <div class="ot-pills ot-pills-nowrap">
          <button type="button" class="ot-pill" class:on={_variety === 'regular'}
            onclick={() => _variety = 'regular'}>REG</button>
          <button type="button" class="ot-pill" class:on={_variety === 'amo'}
            onclick={() => _variety = 'amo'}
            title="After-Market Order — places at session start.">AMO</button>
          <button type="button" class="ot-pill" class:on={_variety === 'co'}
            onclick={() => _variety = 'co'}
            title="Cover Order — built-in stop-loss leg.">CO</button>
          <button type="button" class="ot-pill ot-pill-disabled" disabled aria-disabled="true"
            title="Bracket Order — backend not wired yet.">BO</button>
          <button type="button" class="ot-pill ot-pill-disabled" disabled aria-disabled="true"
            title="Iceberg — backend not wired yet.">ICE</button>
        </div>
      </div>
      <div class="ot-label-inline">
        <label class="ot-label">Validity</label>
        <div class="ot-pills ot-pills-nowrap">
          <button type="button" class="ot-pill" class:on={_validity === 'DAY'}
            onclick={() => _validity = 'DAY'}
            title="Order rests in the book until end-of-day or fill.">DAY</button>
          <button type="button" class="ot-pill" class:on={_validity === 'IOC'}
            onclick={() => _validity = 'IOC'}
            title="Immediate-Or-Cancel — fills what it can immediately, drops the remainder.">IOC</button>
        </div>
      </div>
    </div>

    <!-- Price + Trigger (conditional) -->
    {#if showLimit || showTrigger}
      <div class="ot-row">
        {#if showLimit}
          <div class="ot-label-block">
            <label class="ot-label" for="ot-price">
              Limit price
              {#if !_priceTouched && _price !== '' && _price != null}
                <span class="ot-price-auto" title="Pre-filled from {_side === 'BUY' ? 'top ask' : 'top bid'} on the depth ladder. Edit to override; click ↺ to re-arm auto-fill.">auto</span>
              {/if}
              {#if _priceTouched && _lastQuote}
                <button type="button" class="ot-price-reset"
                        title="Re-arm auto-fill — restore {_side === 'BUY' ? 'top ask' : 'top bid'}"
                        aria-label="Reset price to depth"
                        onclick={() => { _priceTouched = false; _autoFillFromQuote(); }}>↺</button>
              {/if}
            </label>
            <input id="ot-price" type="number" class="ot-input ot-num"
                   step="0.05"
                   bind:value={_price}
                   oninput={() => { _priceTouched = true; }} />
          </div>
        {/if}
        {#if showTrigger}
          <div class="ot-label-block">
            <label class="ot-label" for="ot-trigger">Trigger</label>
            <input id="ot-trigger" type="number" class="ot-input ot-num"
                   step="0.05"
                   bind:value={_trigger} />
          </div>
        {/if}
      </div>
    {/if}

    <!-- Depth — also bubbles its quote tick up via `onQuote` so the
         ticket can keep the limit price aligned with the marketable
         side (BUY → top ask, SELL → top bid). Operator edits to the
         price field freeze the auto-fill until they hit the ↺ button
         next to the field label. -->
    <OrderDepth {symbol} {exchange} onQuote={onDepthQuote} />

    <!-- Mode selector + chase — only relevant when *placing* a new
         order. action='modify' bypasses the place-pipeline entirely
         (PUT /api/orders/{id} hits the broker directly), so neither
         mode nor chase apply there; the whole row is hidden. -->
    {#if action !== 'modify'}
    <div class="ot-mode-row">
      <!-- Mode pills only render when there's an actual choice. With
           only one mode available (e.g. ['live']) there's nothing to
           pick — the operator just clicks Submit. The row stays
           rendered for the chase / aggressiveness controls below. -->
      {#if availableModes.length > 1}
        <span class="ot-label">Mode</span>
        <div class="ot-mode-pills">
          {#if availableModes.includes('draft')}
            <button type="button" class="ot-mode-pill ot-mode-draft" class:on={_mode === 'draft'}
                    onclick={() => _mode = 'draft'}>DRAFT</button>
          {/if}
          <!-- PAPER pill not in default availableModes — dev is paper-
               only via the branch gate; on prod the per-action
               execution.live.* flags decide. Calling sites that need
               PAPER as an explicit choice can still pass it via
               availableModes. -->
          {#if availableModes.includes('paper')}
            <button type="button" class="ot-mode-pill ot-mode-paper" class:on={_mode === 'paper'}
                    title="Routes through the prod paper engine — real bid/ask, no broker hit"
                    onclick={() => _mode = 'paper'}>PAPER</button>
          {/if}
          {#if availableModes.includes('live')}
            <button type="button" class="ot-mode-pill ot-mode-live" class:on={_mode === 'live'}
                    title="Submit to backend. On dev always routes to paper. On prod, routed to LIVE only when the per-action execution.live.* flag is on."
                    onclick={() => _mode = 'live'}>LIVE</button>
          {/if}
        </div>
      {/if}

      <!-- Chase toggle — only meaningful for limit-bearing orders.
           When ON, the engine re-quotes the limit each tick until
           the order fills. The aggressiveness pills below set HOW
           it re-quotes:
             L (patient) — sit on your own side, wait for the market
             M (balanced) — peg to the midpoint
             H (urgent) — cross the spread to take liquidity
           Mirrors IBKR's Adaptive Algo Patient / Normal / Urgent. -->
      {#if showLimit}
        <label class="ot-chase-toggle"
               title={_chase
                 ? 'Chase ON — re-quote the limit each tick until filled'
                 : 'Chase OFF — order rests at the initial limit; fills only if the market crosses'}>
          <input type="checkbox" bind:checked={_chase} />
          <span class="ot-chase-label" class:on={_chase}>CHASE</span>
        </label>
        {#if _chase}
          <div class="ot-chase-agg" role="group" aria-label="Chase aggressiveness">
            <button type="button"
                    class="ot-chase-agg-pill ot-chase-agg-low"
                    class:on={_chaseAgg === 'low'}
                    title="Low — patient. SELL pegs to ASK, BUY pegs to BID. Order rests on your own side; fills only if the market lifts it."
                    onclick={() => _chaseAgg = 'low'}>L</button>
            <button type="button"
                    class="ot-chase-agg-pill ot-chase-agg-med"
                    class:on={_chaseAgg === 'med'}
                    title="Medium — peg to midpoint of bid+ask. Fills when the inside moves halfway in your favour."
                    onclick={() => _chaseAgg = 'med'}>M</button>
            <button type="button"
                    class="ot-chase-agg-pill ot-chase-agg-high"
                    class:on={_chaseAgg === 'high'}
                    title="High — urgent. SELL pegs to BID, BUY pegs to ASK. Crosses the spread to take liquidity on the next tick."
                    onclick={() => _chaseAgg = 'high'}>H</button>
          </div>
        {/if}
      {/if}
    </div>
    {/if}

    {#if validationErr}
      <div class="ot-err">{validationErr}</div>
    {/if}
    {#if submitErr}
      <!-- Surface backend rejections (preflight 422, 503, broker errors)
           inline. Silent failure was causing operators to believe orders
           had been placed when they hadn't. -->
      <div class="ot-err">{submitErr}</div>
    {/if}

    <div class="ot-footer">
      <!-- Left side of the footer: margin preview BEFORE submit, placed-
           order summary AFTER a successful submit. Same vertical slot;
           replaces what used to be a separate row above the footer. -->
      <div class="ot-footer-info">
        {#if submitOk}
          <div class="ot-ok">✓ {submitOk}</div>
        {:else if _mode !== 'draft' && _account && Number(_qty) > 0 && symbol}
          {#if _marginLoading && !_marginPreview}
            <span class="ot-margin-label">Computing margin…</span>
          {:else if _marginPreview?.error}
            <span class="ot-margin-err">⚠ {_marginPreview.error}</span>
          {:else if _marginPreview}
            {@const _d = _marginPreview.diagnostics ?? {}}
            {@const _required  = Number(_d.basket_margin_used)  || 0}
            {@const _available = _d.available_margin}
            {@const _shortfall = Number(_d.margin_shortfall)    || 0}
            {@const _label = (_side === 'BUY' && (_type === 'LIMIT' || _type === 'MARKET') && isOption)
                                ? 'COST'   /* long-option debit = premium × qty */
                                : 'MARGIN' /* SPAN + exposure / shorts / futures */}
            <div class="ot-margin-row">
              <span class="ot-margin-label">{_label}</span>
              <span class="ot-margin-value">₹{aggFmt(_required)}</span>
            </div>
            {#if typeof _available === 'number'}
              <div class="ot-margin-row ot-margin-row-sub">
                <span class="ot-margin-label">Avail</span>
                <span class="ot-margin-value">₹{aggFmt(_available)}</span>
              </div>
              <!-- "After" = what the operator will have left if they
                   click Submit. Most actionable single number on this
                   surface — answers "can I afford this AND still have
                   buffer?". Coloured by remaining-margin band:
                     ≥40 % avail → calm sky
                     10–40 %    → amber warning
                     <10 % / negative → red. -->
              {@const _after = _available - _required}
              {@const _afterPct = _available > 0 ? (_after / _available) * 100 : 0}
              {@const _afterCls = _after < 0 ? 'ot-margin-row-err'
                                  : _afterPct < 10 ? 'ot-margin-row-err'
                                  : _afterPct < 40 ? 'ot-margin-row-warn'
                                  : 'ot-margin-row-sub'}
              <div class="ot-margin-row {_afterCls}">
                <span class="ot-margin-label">After</span>
                <span class="ot-margin-value">{_after < 0 ? '−' : ''}₹{aggFmt(Math.abs(_after))}</span>
              </div>
            {/if}
            {#if _shortfall > 0}
              <div class="ot-margin-row ot-margin-row-err">
                <span class="ot-margin-label">Short</span>
                <span class="ot-margin-value">−₹{aggFmt(_shortfall)}</span>
              </div>
            {/if}
            {#if (_marginPreview.blocked || []).length}
              {#each _marginPreview.blocked as _b}
                <div class="ot-margin-blocked">⚠ {_b.reason}</div>
              {/each}
            {/if}
          {/if}
        {/if}
      </div>

      <!-- Right side of the footer: buttons. After a successful submit
           collapses to a single Exit button — modal stays open until
           the operator dismisses it, so they can read the placed-order
           line above. -->
      {#if submitOk}
        <button type="button" class="ot-exit"
                onclick={onClose}>Exit</button>
      {:else}
        <button type="button" class="ot-exit" onclick={onClose}>Exit</button>
        {#if onAddToBasket && action === 'open'}
          <!-- "+ Basket" — stages the leg into the caller's basket
               panel instead of placing now. Shown only when the
               caller wired onAddToBasket (currently /admin/options
               chain `(i)` flow); other callers see just Cancel +
               Place. -->
          <button type="button" class="ot-basket"
                  disabled={!!validationErr || submitting}
                  title="Add this leg to the basket — place every leg together later"
                  onclick={addToBasket}>+ Basket</button>
        {/if}
        {#if basketMode && action !== 'modify'}
          <!-- basketMode: primary action is "Add to basket" — no backend hit. -->
          <button type="button" class="ot-submit ot-submit-basket-mode"
                  disabled={!!validationErr}
                  onclick={addToBasket}>
            + Add to basket
          </button>
        {:else}
          <button type="button" class="ot-submit"
                  class:ot-submit-buy={_side === 'BUY'}
                  class:ot-submit-sell={_side === 'SELL'}
                  disabled={!!validationErr || submitting}
                  onclick={submit}>
            {#if submitting}…{:else if action === 'modify'}Modify{orderId ? ' · #' + orderId : ''}{:else if _mode === 'draft'}Save draft{:else if sideLabels[_side] === 'CLOSE'}Close · {_side.toLowerCase()}{:else if sideLabels[_side] === 'ADD'}Add · {_side.toLowerCase()}{:else}Place {_side.toLowerCase()}{/if}
          </button>
        {/if}
      {/if}
    </div>
  </div>
</div>

<style>
  .ot-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.55);
    display: flex;
    align-items: center;
    justify-content: center;
    /* z-index 300: above the OrderTimelineDrawer (z=200), the
       PositionStrip (z=49), the navbar (z=50), and any per-page
       sticky LogPanel that might be in the operator's way when the
       ticket opens. Operators reported the Submit row getting
       clipped by bottom panels — this guarantees the ticket sits
       on top. */
    z-index: 300;
    padding: 1rem;
  }
  .ot-modal {
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    border: 1px solid rgba(251,191,36,0.35);
    border-radius: 8px;
    padding: 0.85rem 1rem;
    width: min(28rem, calc(100vw - 2rem));
    max-height: calc(100vh - 2rem);
    overflow-y: auto;
    color: #c8d8f0;
    font-family: ui-monospace, monospace;
    box-shadow: 0 12px 32px rgba(0,0,0,0.6);
  }

  .ot-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 0.5rem;
    padding-bottom: 0.55rem;
    border-bottom: 1px solid rgba(251,191,36,0.15);
    margin-bottom: 0.6rem;
  }
  .ot-symbol-text {
    font-size: 0.9rem;
    font-weight: 700;
    color: #c8d8f0;
    display: block;
  }
  .ot-symbol-meta {
    font-size: 0.6rem;
    color: #a3b9d0;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .ot-close {
    background: transparent;
    border: 1px solid rgba(255,255,255,0.15);
    color: #c8d8f0;
    width: 1.55rem;
    height: 1.55rem;
    border-radius: 3px;
    cursor: pointer;
    font-size: 1rem;
    line-height: 1;
  }
  .ot-close:hover { border-color: #f87171; color: #f87171; }

  .ot-row {
    display: flex;
    gap: 0.6rem;
    margin-bottom: 0.5rem;
    flex-wrap: wrap;
    /* All row children stack label-above-control. align-items:flex-start
       keeps the label baselines on the same y-axis even when one child
       wraps to a taller stack (e.g. price + auto/reset chips). */
    align-items: flex-start;
  }
  .ot-label-block { flex: 1 1 0; min-width: 0; }
  .ot-label {
    /* Section-header treatment so labels read as form structure
       cues, distinct from values / pills / numeric inputs. Amber
       weight 700 (was muted-slate 400-ish) + slightly larger
       letter-spacing — matches the way headings on the algo theme
       cards lead with amber. Operator: "make labels look different
       from others in the order window with depth". */
    display: block;
    font-size: 0.62rem;
    color: #fbbf24;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-weight: 700;
    margin-bottom: 0.18rem;
    opacity: 0.85;
  }

  /* "auto" chip next to the Limit price label — flags that the
     value is being fed from the depth ladder and the operator
     hasn't typed anything yet. Tiny pill so it doesn't compete
     with the input below. */
  .ot-price-auto {
    display: inline-block;
    margin-left: 0.4rem;
    padding: 0 0.3rem;
    border-radius: 2px;
    background: rgba(74,222,128,0.15);
    border: 1px solid rgba(74,222,128,0.45);
    color: #4ade80;
    font-size: 0.6rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    vertical-align: 1px;
  }
  /* Re-arm-auto button — visible only after the operator has
     touched the price field. Click → reset _priceTouched and
     re-fill from the latest quote. */
  .ot-price-reset {
    margin-left: 0.4rem;
    padding: 0 0.35rem;
    height: 0.95rem;
    line-height: 1;
    border-radius: 2px;
    border: 1px solid rgba(125,211,252,0.55);
    background: rgba(125,211,252,0.10);
    color: #7dd3fc;
    font-size: 0.6rem;
    font-weight: 700;
    cursor: pointer;
    vertical-align: 1px;
  }
  .ot-price-reset:hover {
    background: rgba(125,211,252,0.22);
    border-color: rgba(125,211,252,0.85);
  }

  /* Side block — "Side" label + BUY/SELL pill pair. The label
     mirrors the "Lots" / "Qty" label structure in the sibling
     ot-qty-block so the pills + the lots-row line up on the same
     baseline. Without the label the pills would float up to the
     top of the row while Lots data sat below its own label. */
  .ot-side-block { display: flex; flex-direction: column; }
  .ot-side-block .ot-label { margin-bottom: 0.18rem; }

  /* Side toggle (BUY / SELL / ADD / CLOSE). Fixed height + flex
     centring so it lines up exactly with the lots row + steppers
     in the sibling .ot-qty-block. The earlier padding-based sizing
     left the side pill ~0.13rem taller than the 1.5rem steppers,
     so the BUY/SELL label and the [−] N [+] glyphs sat at slightly
     different y-centres. */
  .ot-side-toggle {
    display: flex;
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 3px;
    overflow: hidden;
    height: 1.7rem;
  }
  .ot-side-btn {
    padding: 0 0.75rem;
    background: transparent;
    border: 0;
    color: #a3b9d0;
    font-size: 0.72rem;
    font-weight: 700;
    cursor: pointer;
    flex: 1 1 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    line-height: 1;
  }
  .ot-side-buy.on  { background: rgba(74,222,128,0.18);  color: #4ade80; }
  .ot-side-sell.on { background: rgba(248,113,113,0.18); color: #f87171; }
  /* Locked side toggle (action='modify') — Kite doesn't support
     flipping side on a working order; the button visibly reads as
     disabled and the click is a no-op. */
  .ot-side-toggle.ot-locked .ot-side-btn:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }

  /* Stacks label above the [−] N [+] row so the block lines up with
     the sibling .ot-side-block (also label-on-top). Previously the
     label sat INLINE next to the row via flex-end alignment, which
     made the Side toggle's pills (label-on-top) and the Lots row
     (label-inline) start at different y-coordinates. */
  .ot-qty-block {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 0;
    flex: 1 1 0;
    min-width: 0;
  }
  .ot-qty-block .ot-label { margin: 0 0 0.18rem; }
  .ot-qty-block .ot-meta { font-size: 0.65rem; color: #a3b9d0; }

  /* [−] [1 ▼] [+] (× 50 = 50) — lots-driven Qty UI. Sits inline on
     a single row; nowrap so the +/− and the dropdown can never
     break onto two lines on narrow viewports. Height pinned to
     1.7rem to match the .ot-side-toggle so the [−] N [+] glyphs
     and the BUY/SELL pill share the same y-baseline + y-centre. */
  .ot-lots-row {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    flex-wrap: nowrap;
    height: 1.7rem;
  }
  .ot-lots-step {
    width: 1.7rem;
    height: 1.7rem;
    padding: 0;
    border-radius: 3px;
    border: 1px solid rgba(251,191,36,0.45);
    background: rgba(251,191,36,0.10);
    color: #fbbf24;
    font-family: monospace;
    font-size: 0.9rem;
    font-weight: 700;
    line-height: 1;
    cursor: pointer;
    flex: 0 0 auto;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    /* Rapid taps on iOS Safari otherwise get swallowed by the
       double-tap-to-zoom gesture — manipulation lets every tap
       through cleanly. user-select: none prevents accidental text
       selection between fast taps. */
    touch-action: manipulation;
    -webkit-user-select: none;
    user-select: none;
  }
  .ot-lots-step:hover:not(:disabled) {
    background: rgba(251,191,36,0.22);
    border-color: rgba(251,191,36,0.75);
  }
  .ot-lots-step:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }
  /* Lots count display — replaces the dropdown that spilled out of
     the row on narrow viewports. Compact pill, amber + bold,
     matching the chain picker's `.chain-quick-lots-val` style. */
  .ot-lots-val {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 1.8rem;
    padding: 0 0.35rem;
    height: 1.4rem;
    flex: 0 0 auto;
    color: #fbbf24;
    font-family: monospace;
    font-weight: 700;
    font-size: 0.8rem;
    font-variant-numeric: tabular-nums;
    text-align: center;
  }
  .ot-qty-block .ot-lots-row .ot-meta {
    /* Meta tag sits inline next to the [+] button without padding
       below — was inheriting `.ot-meta { padding-bottom: 0.5rem }`
       from the cash-equity Qty path which mis-aligned it on the
       lots row. */
    padding-bottom: 0;
    white-space: nowrap;
  }

  .ot-input {
    width: 100%;
    background: #1d2a44;
    border: 1px solid rgba(251,191,36,0.25);
    border-radius: 3px;
    padding: 0.3rem 0.45rem;
    color: #c8d8f0;
    font-size: 0.7rem;
    font-family: monospace;
  }
  .ot-input:focus { outline: none; border-color: #fbbf24; }
  .ot-num { text-align: right; }

  /* Pill toggles (Type, Product, Variety) */
  .ot-pills { display: flex; gap: 0.15rem; flex-wrap: wrap; }
  .ot-pills-nowrap { flex-wrap: nowrap; }
  .ot-pill {
    padding: 0.2rem 0.4rem;
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 3px;
    color: #a3b9d0;
    font-size: 0.55rem;
    font-weight: 600;
    cursor: pointer;
    flex: 0 0 auto;
    white-space: nowrap;
  }
  .ot-pill.on {
    background: rgba(251,191,36,0.18);
    border-color: rgba(251,191,36,0.55);
    color: #fbbf24;
  }
  /* Disabled-pill — visible but unreachable. Used for Variety BO + ICE
     where backend wiring isn't done yet; operator sees them on the
     roadmap without being able to click. Tooltip explains why. */
  .ot-pill-disabled,
  .ot-pill[disabled] {
    opacity: 0.35;
    cursor: not-allowed;
    pointer-events: auto; /* keep title tooltip reachable */
  }
  .ot-pill-disabled:hover,
  .ot-pill[disabled]:hover {
    background: rgba(255,255,255,0.04);
    color: #a3b9d0;
    border-color: rgba(255,255,255,0.12);
  }
  /* Inline label + pill row: labels sit next to pills (instead of
     stacking above), so Type and Product fit on the same line
     within the modal's 28 rem width. nowrap on the ot-row level
     keeps the two blocks side by side; the modal scrolls
     horizontally only as a last-resort safety net. */
  .ot-label-inline {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    flex: 0 0 auto;
    min-width: 0;
  }
  .ot-label-inline .ot-label {
    margin: 0;
  }
  .ot-row-tight {
    flex-wrap: nowrap;
    gap: 0.5rem;
    overflow-x: auto;
  }

  /* Mode row */
  .ot-mode-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin: 0.7rem 0;
    padding-top: 0.5rem;
    border-top: 1px solid rgba(255,255,255,0.08);
  }
  .ot-mode-pills { display: flex; gap: 0.25rem; }
  .ot-mode-pill {
    padding: 0.2rem 0.55rem;
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 3px;
    color: #a3b9d0;
    font-size: 0.65rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    cursor: pointer;
  }
  .ot-mode-pill:disabled { opacity: 0.4; cursor: not-allowed; }
  .ot-mode-draft.on { background: rgba(192,132,252,0.18); border-color: rgba(192,132,252,0.55); color: #c084fc; }
  .ot-mode-paper.on { background: rgba(125,211,252,0.18); border-color: rgba(125,211,252,0.55); color: #7dd3fc; }
  .ot-mode-live.on  { background: rgba(74,222,128,0.18);  border-color: rgba(74,222,128,0.55);  color: #4ade80; }

  /* Chase toggle — pushed to the row's far right (margin-left: auto)
     so it sits opposite the mode pills. Native checkbox + label
     pill; the label tints amber when ON to match the rest of the
     ticket's "active state" treatment. */
  .ot-chase-toggle {
    margin-left: auto;
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    cursor: pointer;
    user-select: none;
  }
  .ot-chase-toggle input[type="checkbox"] {
    accent-color: #fbbf24;
    width: 0.85rem;
    height: 0.85rem;
    cursor: pointer;
  }
  .ot-chase-label {
    font-family: monospace;
    font-size: 0.65rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    padding: 0.15rem 0.4rem;
    border-radius: 3px;
    border: 1px solid rgba(255,255,255,0.12);
    color: #a3b9d0;
    background: rgba(255,255,255,0.04);
  }
  .ot-chase-label.on {
    background: rgba(251,191,36,0.18);
    border-color: rgba(251,191,36,0.55);
    color: #fbbf24;
  }

  /* Aggressiveness segment — three square pills (L · M · H) sitting
     immediately right of the CHASE checkbox. Color graduates from
     sky-blue (low/patient) → amber (med) → red (high/urgent) so
     the operator's eye lands on the urgency level without reading
     the glyph. Only the active pill carries the filled bg. */
  .ot-chase-agg {
    display: inline-flex;
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 3px;
    overflow: hidden;
    margin-left: 0.3rem;
  }
  .ot-chase-agg-pill {
    width: 1.4rem;
    height: 1.1rem;
    padding: 0;
    border: 0;
    border-right: 1px solid rgba(255,255,255,0.12);
    background: rgba(255,255,255,0.04);
    color: #a3b9d0;
    font-family: monospace;
    font-size: 0.6rem;
    font-weight: 700;
    line-height: 1;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    transition: background 0.1s, color 0.1s, border-color 0.1s;
  }
  .ot-chase-agg-pill:last-child { border-right: 0; }
  .ot-chase-agg-pill:hover { color: #c8d8f0; background: rgba(255,255,255,0.08); }
  .ot-chase-agg-low.on  { background: rgba(125,211,252,0.20); color: #7dd3fc; }
  .ot-chase-agg-med.on  { background: rgba(251,191,36,0.20);  color: #fbbf24; }
  .ot-chase-agg-high.on { background: rgba(248,113,113,0.20); color: #f87171; }

  .ot-err {
    background: rgba(248,113,113,0.10);
    border: 1px solid rgba(248,113,113,0.4);
    color: #f87171;
    padding: 0.35rem 0.55rem;
    border-radius: 3px;
    font-size: 0.62rem;
    margin: 0.4rem 0;
  }
  /* ── Margin / cash preview ─────────────────────────────────────────
     Rows live inline inside .ot-footer-info, to the left of the
     Cancel/Submit buttons. No outer container — the footer's top
     border + the buttons' visual weight provide enough containment. */
  .ot-margin-row {
    /* Label + value sit side-by-side with a small gap — earlier this
       row used `justify-content: space-between` which stretched the
       two ends across the full footer-info width, leaving a wide gap
       between MARGIN/Avail/Short labels and their amounts. Operator
       wants them clustered tightly so the eye reads "MARGIN ₹X / Avail
       ₹Y / Short ₹Z" as a compact stack, not three wide rows. */
    display: flex;
    align-items: baseline;
    gap: 0.4rem;
    font-variant-numeric: tabular-nums;
  }
  .ot-margin-row + .ot-margin-row { margin-top: 0.05rem; }
  .ot-margin-label {
    color: #fbbf24;
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    font-size: 0.58rem;
  }
  .ot-margin-value {
    color: #c8d8f0;
    font-weight: 700;
    font-size: 0.7rem;
  }
  /* Sub-row — Available, etc. Lower contrast so the primary row dominates. */
  .ot-margin-row-sub .ot-margin-label { color: rgba(200,216,240,0.55); font-weight: 500; }
  .ot-margin-row-sub .ot-margin-value { color: rgba(200,216,240,0.75); font-weight: 500; font-size: 0.62rem; }
  /* Shortfall row — red so the operator sees they can't afford this. */
  .ot-margin-row-err .ot-margin-label,
  .ot-margin-row-err .ot-margin-value { color: #f87171; font-weight: 700; }
  /* "After" row when remaining margin is in the 10-40 % band — amber
     warning instead of red. Sub-row size so it doesn't compete with
     the headline COST/MARGIN row above. */
  .ot-margin-row-warn .ot-margin-label,
  .ot-margin-row-warn .ot-margin-value { color: #fbbf24; font-weight: 600; font-size: 0.62rem; }
  /* Preflight blockers (segment inactive, freeze qty, etc.) */
  .ot-margin-blocked {
    color: #f87171;
    font-size: 0.58rem;
    margin-top: 0.2rem;
    line-height: 1.35;
  }
  .ot-margin-err {
    color: rgba(248,113,113,0.85);
    font-size: 0.6rem;
  }
  /* Placed-order summary line — lives inside .ot-footer-info, to the
     left of the Exit button. Compact (no vertical margin, smaller pad
     than the prior block-level version) so the footer doesn't shove
     the form fields off-screen. */
  .ot-ok {
    background: rgba(74,222,128,0.10);
    border: 1px solid rgba(74,222,128,0.45);
    color: #4ade80;
    padding: 0.3rem 0.5rem;
    border-radius: 3px;
    font-size: 0.65rem;
    font-weight: 700;
    line-height: 1.3;
    word-break: break-word;
  }
  /* Readonly single-account display — matches the custom Select
     trigger's metrics so single-account vs multi-account UIs sit at
     the same height. Cursor: default keeps it visually distinct from
     editable inputs. */
  .ot-account-readonly {
    color: #c8d8f0;
    background: rgba(255,255,255,0.04);
    cursor: default;
    min-height: 1.55rem;
    padding: 0.25rem 0.5rem 0.25rem 0.4rem;
    font-size: 0.62rem;
  }

  /* Funds pill — appears under the Account input. Compact 12px-ish
     row of `Avail margin ₹X · Cash ₹Y · Used ₹Z`. Sky-blue tint to
     read as info, flips red when margin goes negative (margin debt
     — the operator's about to get a Kite rejection). */
  .ot-funds {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 0.3rem 0.4rem;
    margin-top: 0.4rem;
    padding: 0.3rem 0.5rem;
    border-radius: 3px;
    background: rgba(125,211,252,0.08);
    border: 1px solid rgba(125,211,252,0.25);
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.7rem;
    line-height: 1.2;
  }
  .ot-funds-k {
    color: #a3b9d0;
    font-size: 0.65rem;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }
  .ot-funds-v {
    color: #c8d8f0;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }
  .ot-funds-sep {
    color: #a3b9d0;
    opacity: 0.5;
  }
  .ot-funds-low {
    background: rgba(248,113,113,0.10);
    border-color: rgba(248,113,113,0.35);
  }
  .ot-funds-low .ot-funds-v { color: #f87171; }

  .ot-footer {
    display: flex;
    gap: 0.75rem;
    align-items: center;
    padding-top: 0.6rem;
    border-top: 1px solid rgba(255,255,255,0.08);
  }
  /* Left side of the footer: flex-grows to occupy free space so the
     buttons stay pinned to the right edge regardless of how long the
     margin / placed-order text gets. Anchored-left content (margin
     rows, ok line) reads naturally next to the right-aligned buttons. */
  .ot-footer-info {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
  }
  .ot-footer-info:empty { display: none; }
  .ot-exit,
  .ot-basket,
  .ot-submit {
    padding: 0.45rem 1rem;
    border-radius: 3px;
    font-size: 0.72rem;
    font-weight: 700;
    cursor: pointer;
    border: 1px solid transparent;
    flex-shrink: 0;
  }
  /* Exit — replaces Cancel + Submit after a successful order, and also
     stands in for Cancel before submit. Outlined champagne — reads as
     "close when ready", not as another primary action. */
  .ot-exit {
    background: rgba(212,146,12,0.12);
    border-color: rgba(212,146,12,0.55);
    color: #d4920c;
  }
  .ot-exit:hover { background: rgba(212,146,12,0.22); border-color: rgba(212,146,12,0.85); }
  /* Stage-into-basket — outlined amber, distinct from the filled
     green/red Submit. Reads as a secondary action: the operator
     can stack legs before placing the whole basket together. */
  .ot-basket {
    background: rgba(251,191,36,0.10);
    border-color: rgba(251,191,36,0.55);
    color: #fbbf24;
  }
  .ot-basket:hover:not(:disabled) {
    background: rgba(251,191,36,0.20);
    border-color: rgba(251,191,36,0.85);
  }
  .ot-basket:disabled { opacity: 0.45; cursor: not-allowed; }
  .ot-submit {
    background: #fbbf24;
    color: #0c1830;
  }
  .ot-submit-buy  { background: #4ade80; }
  .ot-submit-sell { background: #f87171; }
  /* Basket-mode primary action — amber outlined, distinct from the
     green/red fill so the operator reads "stage, don't fire yet". */
  .ot-submit-basket-mode {
    background: rgba(74,222,128,0.12);
    color: #4ade80;
    border: 1px solid rgba(74,222,128,0.55);
  }
  .ot-submit-basket-mode:hover:not(:disabled) {
    background: rgba(74,222,128,0.22);
    border-color: rgba(74,222,128,0.85);
  }
  .ot-submit:disabled { opacity: 0.45; cursor: not-allowed; }
  .ot-submit-basket-mode:disabled { opacity: 0.45; cursor: not-allowed; }
</style>
