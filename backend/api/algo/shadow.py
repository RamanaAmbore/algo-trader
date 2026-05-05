"""
Shadow trade engine — logs the exact Kite payload without executing.

Shadow mode sits between Paper and Live on the confidence ladder:
  Paper → Shadow → Live

Paper: validates via basket_margin, then runs a simulated fill lifecycle.
Shadow: validates via basket_margin, logs the exact kite.place_order kwargs,
        but never calls the broker. No fill lifecycle — just a record of
        "what Kite would have received".
Live: calls the broker for real.

The engine is a singleton on prod, similar to get_prod_paper_engine().
"""

from __future__ import annotations

import uuid
from typing import Optional

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


class ShadowTradeEngine:
    """Logs what Kite would have received + validates via basket_margin."""

    def __init__(self) -> None:
        self._shadow_count: int = 0

    async def capture_order(
        self,
        agent,
        action_type: str,
        resolved: dict,
    ) -> dict:
        """
        Build the exact kite.place_order kwargs, validate via basket_margin,
        write an AlgoOrder(mode='shadow') row. Never calls the broker.

        Returns {ok, margin_info, payload, algo_order_id}.
        """
        from backend.api.database import async_session
        from backend.api.models import AlgoOrder
        from backend.shared.brokers import get_broker

        account = str(resolved["account"])
        symbol = str(resolved["symbol"])
        side = str(resolved["side"])
        qty = int(resolved["qty"] or 0)
        price = resolved.get("price")
        exchange = resolved.get("exchange") or "NFO"
        product = resolved.get("product") or "NRML"

        # Build the exact payload that kite.place_order would receive
        kite_payload = {
            "variety": resolved.get("variety") or "regular",
            "exchange": exchange,
            "tradingsymbol": symbol,
            "transaction_type": side,
            "quantity": qty,
            "product": product,
            "order_type": "LIMIT",
            "price": price,
            "validity": "DAY",
            "tag": "ramboq-shadow",
        }

        # Validate via basket_margin — async-safe (Kite SDK is sync requests)
        ok, reason = True, "shadow — not executed"
        try:
            import asyncio, json as _json
            broker = get_broker(account)
            if qty > 0 and price is not None and symbol and exchange:
                basket_order = {
                    "exchange": exchange,
                    "tradingsymbol": symbol,
                    "transaction_type": side,
                    "quantity": qty,
                    "order_type": "LIMIT",
                    "product": product,
                    "price": price,
                    "variety": resolved.get("variety") or "regular",
                }
                loop = asyncio.get_running_loop()
                bm_result = await loop.run_in_executor(
                    None, broker.kite.basket_margin, [basket_order]
                )
                # Capture the actual initial/final margin numbers from the
                # basket_margin response rather than discarding them.
                try:
                    bm_summary = _json.dumps({
                        "initial_margin": bm_result.get("initial") if isinstance(bm_result, dict) else None,
                        "final_margin":   bm_result.get("final")   if isinstance(bm_result, dict) else None,
                        "raw":            bm_result,
                    }, default=str)[:240]
                except Exception:
                    bm_summary = str(bm_result)[:240]
                reason = f"basket_margin OK — {bm_summary}"
        except Exception as e:
            ok = False
            reason = str(e)[:240]

        status = "SHADOW_OK" if ok else "SHADOW_REJECTED"
        fake_id = "SHADOW-" + uuid.uuid4().hex[:12]

        price_str = f"@₹{price:,.2f}" if price is not None else "@MARKET"
        pretty = (
            f"[SHADOW] {agent.slug} → {action_type}: {side} {qty} "
            f"{symbol} {price_str} · acct={account}"
            + ("" if ok else f" · REJECTED ({reason})")
        )
        logger.warning(pretty)

        algo_order_id = None
        try:
            async with async_session() as s:
                import json
                row = AlgoOrder(
                    account=account,
                    symbol=symbol,
                    exchange=exchange,
                    transaction_type=side,
                    quantity=qty,
                    initial_price=(float(price) if price is not None else None),
                    status=status,
                    engine="shadow",
                    mode="shadow",
                    broker_order_id=fake_id,
                    detail=pretty + f"\n--- KITE PAYLOAD ---\n{json.dumps(kite_payload, indent=2)}",
                )
                s.add(row)
                await s.commit()
                algo_order_id = row.id
        except Exception as e:
            logger.error(f"[SHADOW] write failed: {e}")

        self._shadow_count += 1
        return {
            "ok": ok,
            "margin_info": reason,
            "payload": kite_payload,
            "algo_order_id": algo_order_id,
            "detail": pretty,
        }


# ═════════════════════════════════════════════════════════════════════════
#  Singleton
# ═════════════════════════════════════════════════════════════════════════

_shadow_engine: Optional[ShadowTradeEngine] = None


def get_shadow_engine() -> ShadowTradeEngine:
    global _shadow_engine
    if _shadow_engine is None:
        _shadow_engine = ShadowTradeEngine()
    return _shadow_engine
