"""
mt5rest Order Executor – GoldScalperPro v4

Places and closes MT5 orders via the mt5rest bridge HTTP API.
"""

from dataclasses import dataclass
from typing import Optional

import aiohttp

from live_trading.logger import get_logger
from live_trading.mt5.connector import _get_session, get_connection

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
    base = get_connection()
    if not base:
        return TradeResult(False, None, "Not connected to mt5rest bridge")

    lot        = _normalise_lot(lot_size)
    order_type = 0 if direction.upper() == "BUY" else 1   # 0=BUY  1=SELL

    log.debug(f"Placing {direction} {lot} lots {symbol}  SL={sl}  TP={tp}")

    payload = {
        "action":       "TRADE_ACTION_DEAL",
        "symbol":       symbol,
        "volume":       lot,
        "type":         order_type,
        "sl":           round(sl, 2),
        "tp":           round(tp, 2),
        "deviation":    deviation,
        "comment":      comment[:32],
        "type_filling": "ORDER_FILLING_IOC",
    }

    try:
        sess = _get_session()
        async with sess.post(f"{base}/order", json=payload) as resp:
            data    = await resp.json(content_type=None)
            retcode = data.get("retcode", -1)

            if retcode == 10009:        # TRADE_RETCODE_DONE
                pos_id = str(data.get("order", data.get("deal", "")))
                log.info(
                    f"Trade opened  id={pos_id}  "
                    f"{direction} {lot} lots  SL={sl}  TP={tp}"
                )
                return TradeResult(True, pos_id, "OK", pos_id)

            msg = data.get("comment", f"retcode={retcode}")
            log.error(f"order rejected: {msg}")
            return TradeResult(False, None, msg)

    except Exception as exc:
        log.error(f"place_market_order error: {exc}")
        return TradeResult(False, None, str(exc))


# ── Close position ────────────────────────────────────────────────────────────

async def close_position(position_id: str, **kwargs) -> TradeResult:
    base = get_connection()
    if not base:
        return TradeResult(False, None, "Not connected to mt5rest bridge")

    try:
        sess = _get_session()
        async with sess.post(
            f"{base}/close_position",
            json={"ticket": int(position_id)},
        ) as resp:
            data    = await resp.json(content_type=None)
            retcode = data.get("retcode", -1)

            if retcode == 10009:
                log.info(f"Position {position_id} closed")
                return TradeResult(True, position_id, "Closed")

            msg = data.get("comment", f"retcode={retcode}")
            log.error(f"close_position failed: {msg}")
            return TradeResult(False, None, msg)

    except Exception as exc:
        log.error(f"close_position error: {exc}")
        return TradeResult(False, None, str(exc))


# ── Modify position ───────────────────────────────────────────────────────────

async def modify_position(
    position_id: str, sl: float, tp: float
) -> TradeResult:
    base = get_connection()
    if not base:
        return TradeResult(False, None, "Not connected to mt5rest bridge")

    try:
        sess = _get_session()
        async with sess.post(
            f"{base}/modify_position",
            json={"ticket": int(position_id), "sl": round(sl, 2), "tp": round(tp, 2)},
        ) as resp:
            data    = await resp.json(content_type=None)
            retcode = data.get("retcode", -1)

            if retcode == 10009:
                log.info(f"Position {position_id} modified  SL={sl}  TP={tp}")
                return TradeResult(True, position_id, "Modified")

            msg = data.get("comment", f"retcode={retcode}")
            log.error(f"modify_position failed: {msg}")
            return TradeResult(False, None, msg)

    except Exception as exc:
        log.error(f"modify_position error: {exc}")
        return TradeResult(False, None, str(exc))
