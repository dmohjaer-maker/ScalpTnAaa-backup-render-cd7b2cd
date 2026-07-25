"""
mt5rest Order Executor – GoldScalperPro v4

Places and closes MT5 orders via the mt5rest bridge HTTP API.

Endpoints used:
    GET /OrderSendSafe   – place a market order
    GET /OrderCloseSafe  – close an open position by ticket
    GET /OrderModifySafe – modify SL/TP on an open position
"""

from dataclasses import dataclass
from typing import Optional

import aiohttp

from live_trading.logger import get_logger
from live_trading.mt5.connector import _get_session, get_connection, get_conn_id

log = get_logger()


@dataclass
class TradeResult:
    success:     bool
    position_id: Optional[str]
    message:     str
    order_id:    Optional[str] = None


# ── Lot normalisation ─────────────────────────────────────────────────────────

def _normalise_lot(lot: float,
                   vol_min:  float = 0.01,
                   vol_step: float = 0.01,
                   vol_max:  float = 500.0) -> float:
    steps  = round((lot - vol_min) / vol_step)
    result = vol_min + steps * vol_step
    return max(vol_min, min(vol_max, round(result, 4)))


def _is_error(data: dict) -> bool:
    """mt5rest returns {message, code, stackTrace} on errors."""
    return isinstance(data, dict) and "code" in data and "stackTrace" in data


# ── Place market order ────────────────────────────────────────────────────────

async def place_market_order(
    symbol:    str,
    direction: str,       # "BUY" | "SELL"
    lot_size:  float,
    sl:        float,
    tp:        float,
    comment:   str = "GSPv4",
    deviation: int = 30,
) -> TradeResult:
    base    = get_connection()
    conn_id = get_conn_id()
    if not base or not conn_id:
        return TradeResult(False, None, "Not connected to mt5rest bridge")

    lot       = _normalise_lot(lot_size)
    operation = 0 if direction.upper() == "BUY" else 1   # 0=BUY  1=SELL

    log.debug(f"Placing {direction} {lot} lots {symbol}  SL={sl}  TP={tp}")

    params = {
        "id":         conn_id,
        "symbol":     symbol,
        "operation":  operation,
        "volume":     lot,
        "slippage":   deviation,
        "stoploss":   round(sl, 2),
        "takeprofit": round(tp, 2),
        "comment":    comment[:32],
    }

    try:
        sess = _get_session()
        async with sess.get(
            f"{base}/OrderSendSafe",
            params=params,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            data = await resp.json(content_type=None)

            if _is_error(data):
                msg = data.get("message", f"code={data.get('code','?')}")
                log.error(f"OrderSendSafe error: {msg}")
                return TradeResult(False, None, msg)

            ticket = data.get("ticket") if isinstance(data, dict) else None
            if ticket is not None and resp.status == 200:
                pos_id = str(ticket)
                log.info(
                    f"Trade opened  ticket={pos_id}  "
                    f"{direction} {lot} lots  SL={sl}  TP={tp}"
                )
                return TradeResult(True, pos_id, "OK", pos_id)

            msg = f"Unexpected response (status={resp.status}): {str(data)[:200]}"
            log.error(f"place_market_order: {msg}")
            return TradeResult(False, None, msg)

    except Exception as exc:
        log.error(f"place_market_order error: {exc}")
        return TradeResult(False, None, str(exc))


# ── Close position ────────────────────────────────────────────────────────────

async def close_position(position_id: str, deviation: int = 30, **kwargs) -> TradeResult:
    base    = get_connection()
    conn_id = get_conn_id()
    if not base or not conn_id:
        return TradeResult(False, None, "Not connected to mt5rest bridge")

    try:
        sess = _get_session()
        async with sess.get(
            f"{base}/OrderCloseSafe",
            params={
                "id":       conn_id,
                "ticket":   int(position_id),
                "slippage": deviation,
            },
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            data = await resp.json(content_type=None)

            if _is_error(data):
                msg = data.get("message", f"code={data.get('code','?')}")
                log.error(f"OrderCloseSafe error: {msg}")
                return TradeResult(False, None, msg)

            if resp.status == 200:
                log.info(f"Position {position_id} closed")
                return TradeResult(True, position_id, "Closed")

            msg = f"Unexpected response (status={resp.status}): {str(data)[:200]}"
            log.error(f"close_position: {msg}")
            return TradeResult(False, None, msg)

    except Exception as exc:
        log.error(f"close_position error: {exc}")
        return TradeResult(False, None, str(exc))


# ── Modify position ───────────────────────────────────────────────────────────

async def modify_position(
    position_id: str, sl: float, tp: float
) -> TradeResult:
    base    = get_connection()
    conn_id = get_conn_id()
    if not base or not conn_id:
        return TradeResult(False, None, "Not connected to mt5rest bridge")

    try:
        sess = _get_session()
        async with sess.get(
            f"{base}/OrderModifySafe",
            params={
                "id":         conn_id,
                "ticket":     int(position_id),
                "stoploss":   round(sl, 2),
                "takeprofit": round(tp, 2),
            },
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            data = await resp.json(content_type=None)

            if _is_error(data):
                msg = data.get("message", f"code={data.get('code','?')}")
                log.error(f"OrderModifySafe error: {msg}")
                return TradeResult(False, None, msg)

            if resp.status == 200:
                log.info(f"Position {position_id} modified  SL={sl}  TP={tp}")
                return TradeResult(True, position_id, "Modified")

            msg = f"Unexpected response (status={resp.status}): {str(data)[:200]}"
            log.error(f"modify_position: {msg}")
            return TradeResult(False, None, msg)

    except Exception as exc:
        log.error(f"modify_position error: {exc}")
        return TradeResult(False, None, str(exc))
