"""
Capital Manager — Smart SL/TP/LotSize for XAUUSD.
Ported from capitalManager.ts
"""
import os
from dataclasses import dataclass
from typing import Optional

DEFAULT_RISK_PCT    = 1.0


def _bounded_env_float(name: str, default: float, lo: float, hi: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if not lo <= value <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}, got {value}")
    return value


# Structure-aware initial stop settings. The old 0.5 ATR floor was too close
# for normal M5 XAUUSD noise; these are tunable on Render without code edits.
ATR_BUFFER_MULT     = _bounded_env_float("SL_ATR_BUFFER_MULT", 0.50, 0.10, 1.50)
MIN_SL_ATR_MULT     = _bounded_env_float("SL_MIN_ATR_MULT",     1.00, 0.50, 2.50)
MAX_SL_ATR_MULT     = _bounded_env_float("SL_MAX_ATR_MULT",     3.50, 1.50, 6.00)
if MIN_SL_ATR_MULT > MAX_SL_ATR_MULT:
    raise ValueError("SL_MIN_ATR_MULT cannot exceed SL_MAX_ATR_MULT")

FIXED_TP_RR         = 2.00
LOT_DOLLAR_PER_UNIT = 100
MIN_LOT             = 0.01
MAX_LOT             = 50.0


@dataclass
class CapitalInput:
    direction:           str    # BUY | SELL
    entry_price:         float
    atr:                 float
    account_balance:     float
    risk_percent:        float = DEFAULT_RISK_PCT
    order_block_top:     Optional[float] = None
    order_block_bottom:  Optional[float] = None
    swing_high:          Optional[float] = None
    swing_low:           Optional[float] = None
    resistance_level:    Optional[float] = None
    support_level:       Optional[float] = None


@dataclass
class CapitalOutput:
    entry_price:            float
    stop_loss:              float
    take_profit:            float
    risk_reward_ratio:      float
    trailing_stop_distance: float
    trailing_activation_at: float
    break_even_at:          float
    break_even_sl:          float
    lot_size:               float
    risk_amount:            float
    sl_distance_usd:        float
    sl_distance_pips:       float


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def _r2(n: float) -> float: return round(n, 2)
def _r4(n: float) -> float: return round(n, 4)


def _calc_smart_sl(direction: str, entry: float, atr: float, inp: CapitalInput) -> float:
    buffer = atr * ATR_BUFFER_MULT
    min_sl = atr * MIN_SL_ATR_MULT
    max_sl = atr * MAX_SL_ATR_MULT
    raw_sl = None

    if direction == "BUY":
        cands = []
        if inp.order_block_bottom is not None and inp.order_block_bottom < entry:
            cands.append(inp.order_block_bottom)
        if inp.swing_low is not None and inp.swing_low < entry:
            cands.append(inp.swing_low)
        if inp.support_level is not None and inp.support_level < entry:
            cands.append(inp.support_level)
        if cands:
            level  = max(cands)
            raw_sl = entry - (entry - level + buffer)
    else:
        cands = []
        if inp.order_block_top is not None and inp.order_block_top > entry:
            cands.append(inp.order_block_top)
        if inp.swing_high is not None and inp.swing_high > entry:
            cands.append(inp.swing_high)
        if inp.resistance_level is not None and inp.resistance_level > entry:
            cands.append(inp.resistance_level)
        if cands:
            level  = min(cands)
            raw_sl = entry + (level - entry + buffer)

    fallback  = atr * 1.5
    sl_dist   = abs(entry - raw_sl) if raw_sl is not None else fallback
    clamped   = _clamp(sl_dist, min_sl, max_sl)
    return _r2(entry - clamped if direction == "BUY" else entry + clamped)


def _calc_lot_size(sl_dist_usd: float, balance: float, risk_pct: float):
    if sl_dist_usd <= 0:
        return MIN_LOT, 0.0
    risk_amount = balance * risk_pct / 100
    raw_lot     = risk_amount / (sl_dist_usd * LOT_DOLLAR_PER_UNIT)
    lot_size    = _r4(_clamp(raw_lot, MIN_LOT, MAX_LOT))
    actual_risk = _r2(lot_size * sl_dist_usd * LOT_DOLLAR_PER_UNIT)
    return lot_size, actual_risk


def calc_trade_parameters(inp: CapitalInput) -> CapitalOutput:
    entry      = inp.entry_price
    direction  = inp.direction
    atr        = inp.atr
    risk_pct   = inp.risk_percent

    sl         = _calc_smart_sl(direction, entry, atr, inp)
    sl_dist    = _r2(abs(entry - sl))
    sl_pips    = _r2(sl_dist * 100)

    tp_dist    = sl_dist * FIXED_TP_RR
    tp         = _r2(entry + tp_dist if direction == "BUY" else entry - tp_dist)
    rr         = _r2(tp_dist / sl_dist) if sl_dist > 0 else 0.0

    lot, risk  = _calc_lot_size(sl_dist, inp.account_balance, risk_pct)

    # Break-even trigger: price must move 1× SL distance in our favour before
    # we can safely move the stop to entry.  Trailing stop activates at the
    # same level.  These are informational fields — the live loop does not yet
    # implement automatic BE/trailing moves; they're displayed on the panel.
    be_dist    = sl_dist * 1.0
    be_at      = _r2(entry + be_dist if direction == "BUY" else entry - be_dist)
    trail_act  = be_at
    trail_dist = _r2(sl_dist * 0.5)   # trail distance = half of original SL

    return CapitalOutput(
        entry_price=_r2(entry),
        stop_loss=sl,
        take_profit=tp,
        risk_reward_ratio=rr,
        trailing_stop_distance=trail_dist,
        trailing_activation_at=trail_act,
        break_even_at=be_at,
        break_even_sl=entry,
        lot_size=lot,
        risk_amount=risk,
        sl_distance_usd=sl_dist,
        sl_distance_pips=sl_pips,
    )
