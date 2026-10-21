/**
 * Stateful mock for the /api/orders/drafts CRUD endpoints (payoffDrafts store).
 *
 * Specs that touch draft positions intercept every drafts call here, so they
 * never create or delete rows in the dev database. The mock records each call
 * (method + id) so a spec can assert exactly which drafts were deleted.
 */

export const DRAFT_ROWS = [
  { id: 9001, symbol: 'NIFTY26OCTFUT', exchange: 'NFO', transaction_type: 'BUY',
    quantity: 75, initial_price: 25000, account: 'ZG0790' },
  { id: 9002, symbol: 'CRUDEOIL26NOVFUT', exchange: 'MCX', transaction_type: 'SELL',
    quantity: 100, initial_price: 6500, account: 'ZG0790' },
];

/**
 * @param {import('@playwright/test').Page} page
 * @param {Array<object>} [initialRows]
 * @returns {Promise<{ rows: object[], calls: Array<{method: string, id: number|null}> }>}
 */
export async function mockDrafts(page, initialRows = DRAFT_ROWS) {
  const state = { rows: initialRows.map((r) => ({ ...r })), calls: [] };
  await page.route(/\/api\/orders\/drafts(\/\d+)?(\?.*)?$/, async (route) => {
    const req = route.request();
    const method = req.method();
    const idMatch = new URL(req.url()).pathname.match(/\/drafts\/(\d+)$/);
    const id = idMatch ? Number(idMatch[1]) : null;
    state.calls.push({ method, id });
    const json = (body, status = 200) => route.fulfill({
      status, contentType: 'application/json', body: JSON.stringify(body),
    });
    if (method === 'GET') return json(state.rows);
    if (method === 'DELETE') {
      state.rows = state.rows.filter((r) => r.id !== id);
      return json({ ok: true });
    }
    if (method === 'POST') {
      const body = req.postDataJSON() || {};
      const row = { id: 9100 + state.calls.length, ...body, initial_price: body.price };
      state.rows.push(row);
      return json({ id: row.id });
    }
    if (method === 'PATCH') {
      const body = req.postDataJSON() || {};
      state.rows = state.rows.map((r) => (r.id === id ? { ...r, ...body, initial_price: body.price } : r));
      return json(state.rows.find((r) => r.id === id) || { id });
    }
    return json({ detail: 'not mocked' }, 405);
  });
  return state;
}
