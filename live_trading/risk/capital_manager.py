"""
Capital Manager — Smart SL/TP/LotSize for XAUUSD.

This module is intentionally limited to trade-parameter sizing.  Signal
selection, entry filters, and execution are left untouched.
"""
import os
from dataclasses import dataclass
from math import floor, isfinite
from typing import Optional

DEFAULT_RISK_PCT = 1.0


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


# Initial SL envelope. These remain Render-configurable using the existing
# names so deployment settings stay backward compatible. The effective floor
# is intentionally hard-clamped: an old Render value cannot recreate the
# tiny stops that this manager is responsible for preventing.
ATR_BUFFER_MULT = _bounded_env_float("SL_ATR_BUFFER_MULT", 0.15, 0.10, 0.75)
_configured_min_sl_atr_mult = _bounded_env_float(
    "SL_MIN_ATR_MULT", 1.80, 0.50, 4.00
)
_configured_max_sl_atr_mult = _bounded_env_float(
    "SL_MAX_ATR_MULT", 3.50, 1.00, 6.00
)
# 1.80 ATR is the non-negotiable protection floor for XAUUSD scalps. Keeping
# the environment range backward-compatible lets an existing Render service
# boot while still making its legacy 0.55 value harmless.
HARD_MIN_SL_ATR_MULT = 1.80
MIN_SL_ATR_MULT = max(_configured_min_sl_atr_mult, HARD_MIN_SL_ATR_MULT)
MAX_SL_ATR_MULT = max(_configured_max_sl_atr_mult, MIN_SL_ATR_MULT)
if MIN_SL_ATR_MULT > MAX_SL_ATR_MULT:
    raise ValueError("SL_MIN_ATR_MULT cannot exceed SL_MAX_ATR_MULT")

# Optional advanced sizing knobs.  They have safe defaults and do not require
# any Render environment change.
SPREAD_BUFFER_MULT = _bounded_env_float("SL_SPREAD_BUFFER_MULT", 1.50, 0.50, 6.00)
# Short, reachable scalp targets. Structure is preferred; these bounds prevent
# a distant level from turning into a long-duration swing target.
FIXED_TP_RR = _bounded_env_float("TP_RR", 1.50, 0.80, 6.00)
TP_MIN_RR = _bounded_env_float("TP_MIN_RR", 1.20, 0.80, 4.00)
TP_MAX_RR = _bounded_env_float("TP_MAX_RR", 2.00, 1.00, 8.00)
TP_APPROACH_ATR_MULT = _bounded_env_float("TP_APPROACH_ATR_MULT", 0.15, 0.00, 1.00)
if TP_MIN_RR > TP_MAX_RR:
    raise ValueError("TP_MIN_RR cannot exceed TP_MAX_RR")

LOT_DOLLAR_PER_UNIT = 100
MIN_LOT = 0.01
MAX_LOT = 50.0


@dataclass
class CapitalInput:
    direction: str
    entry_price: float
    atr: float
    account_balance: float
    risk_percent: float = DEFAULT_RISK_PCT
    order_block_top: Optional[float] = None
    order_block_bottom: Optional[float] = None
    swing_high: Optional[float] = None
    swing_low: Optional[float] = None
    resistance_level: Optional[float] = None
    support_level: Optional[float] = None
    # Optional live context. Existing callers need not provide these.
    atr_mean: Optional[float] = None
    spread: float = 0.0
    # Latest confirmed local pivots; preferred over distant BOS levels for scalps.
    micro_swing_high: Optional[float] = None
    micro_swing_low: Optional[float] = None


@dataclass
class CapitalOutput:
    entry_price: float
    stop_loss: float
    take_profit: float
    risk_reward_ratio: float
    trailing_stop_distance: float
    trailing_activation_at: float
    break_even_at: float
    break_even_sl: float
    lot_size: float
    risk_amount: float
    sl_distance_usd: float
    sl_distance_pips: float


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _r2(value: float) -> float:
    return round(value, 2)


def _r4(value: float) -> float:
    return round(value, 4)


def _dynamic_buffer(atr: float, atr_mean: Optional[float], spread: float) -> tuple[float, float]:
    """Return a volatility- and spread-aware cushion beyond structure."""
    mean = atr_mean if atr_mean is not None and isfinite(atr_mean) else atr
    volatility_ratio = _clamp(atr / mean, 0.50, 2.50) if mean > 0 else 1.0
    if volatility_ratio >= 1.0:
        volatility_factor = _clamp(0.90 + 0.55 * (volatility_ratio - 1.0), 0.90, 1.75)
    else:
        volatility_factor = _clamp(0.90 + 0.20 * (volatility_ratio - 1.0), 0.70, 0.90)
    atr_buffer = atr * ATR_BUFFER_MULT * volatility_factor
    safe_spread = spread if isfinite(spread) and spread > 0 else 0.0
    spread_buffer = safe_spread * SPREAD_BUFFER_MULT
    minimum_component = max(atr * 0.12, safe_spread * 2.0)
    return max(atr_buffer, spread_buffer, minimum_component), volatility_ratio


def _select_sl_level(direction: str, entry: float, inp: CapitalInput) -> Optional[float]:
    if direction == "BUY":
        candidates = [
            value for value in (
                inp.micro_swing_low, inp.order_block_bottom, inp.swing_low,
                inp.support_level,
            ) if value is not None and isfinite(value) and value < entry
        ]
        return max(candidates) if candidates else None
    candidates = [
        value for value in (
            inp.micro_swing_high, inp.order_block_top, inp.swing_high,
            inp.resistance_level,
        ) if value is not None and isfinite(value) and value > entry
    ]
    return min(candidates) if candidates else None


def _select_tp_level(direction: str, entry: float, inp: CapitalInput) -> Optional[float]:
    if direction == "BUY":
        candidates = [
            value for value in (
                inp.micro_swing_high, inp.order_block_top, inp.swing_high,
                inp.resistance_level,
            ) if value is not None and isfinite(value) and value > entry
        ]
        return min(candidates) if candidates else None
    candidates = [
        value for value in (
            inp.micro_swing_low, inp.order_block_bottom, inp.swing_low,
            inp.support_level,
        ) if value is not None and isfinite(value) and value < entry
    ]
    return max(candidates) if candidates else None


def _calc_smart_sl(direction: str, entry: float, atr: float, inp: CapitalInput) -> float:
    # A failed/zero ATR must never collapse SL onto the entry price.
    safe_atr = atr if isfinite(atr) and atr > 0 else max(entry * 0.001, 0.01)
    buffer, volatility_ratio = _dynamic_buffer(safe_atr, inp.atr_mean, inp.spread)
    safe_spread = inp.spread if isfinite(inp.spread) and inp.spread > 0 else 0.0

    # In elevated volatility, give the stop progressively more breathing room
    # while never reducing it below the hard safety floor. This protects the
    # trade from ordinary XAUUSD wick noise without changing the entry.
    adaptive_min_multiplier = _clamp(
        MIN_SL_ATR_MULT + max(volatility_ratio - 1.0, 0.0) * 0.35,
        MIN_SL_ATR_MULT,
        MAX_SL_ATR_MULT,
    )
    minimum_distance = max(
        safe_atr * adaptive_min_multiplier,
        safe_spread * SPREAD_BUFFER_MULT * 1.5,
    )
    maximum_distance = max(
        minimum_distance,
        safe_atr * _clamp(
            MAX_SL_ATR_MULT + max(volatility_ratio - 1.0, 0.0) * 0.50,
            adaptive_min_multiplier,
            MAX_SL_ATR_MULT,
        ),
    )

    level = _select_sl_level(direction, entry, inp)
    structure_distance = abs(entry - level) if level is not None else 0.0

    # Do not pull a stop inside a distant structural invalidation level. If it
    # is beyond the current volatility envelope, use a volatility fallback.
    if level is None or structure_distance > maximum_distance:
        # No usable nearby structure: use the upper end of the volatility
        # envelope rather than falling back to a fragile minimum stop.
        distance = maximum_distance
    else:
        distance = _clamp(
            max(structure_distance + buffer, minimum_distance),
            minimum_distance,
            maximum_distance,
        )

    return _r2(entry - distance if direction == "BUY" else entry + distance)


def _calc_structural_tp(
    direction: str, entry: float, sl_distance: float, atr: float, inp: CapitalInput,
) -> tuple[Optional[float], float]:
    if sl_distance <= 0:
        return None, 0.0
    level = _select_tp_level(direction, entry, inp)
    if level is None:
        return None, 0.0
    raw_distance = abs(level - entry)
    approach = max(
        max(atr, 0.0) * TP_APPROACH_ATR_MULT,
        max(inp.spread, 0.0),
    )
    target_distance = raw_distance - approach
    rr = target_distance / sl_distance if sl_distance > 0 else 0.0
    if target_distance <= 0 or rr < TP_MIN_RR or rr > TP_MAX_RR:
        return None, 0.0
    target = entry + target_distance if direction == "BUY" else entry - target_distance
    return _r2(target), target_distance


def _calc_lot_size(sl_dist_usd: float, balance: float, risk_pct: float) -> tuple[float, float]:
    if not isfinite(sl_dist_usd) or sl_dist_usd <= 0 or balance <= 0 or risk_pct <= 0:
        return MIN_LOT, 0.0
    risk_amount = balance * risk_pct / 100.0
    raw_lot = risk_amount / (sl_dist_usd * LOT_DOLLAR_PER_UNIT)
    # Round down before the executor's broker-step normalisation so the
    # requested risk is never increased by ordinary rounding.
    lot_size = floor(_clamp(raw_lot, MIN_LOT, MAX_LOT) * 10_000) / 10_000
    lot_size = max(MIN_LOT, min(MAX_LOT, lot_size))
    actual_risk = _r2(lot_size * sl_dist_usd * LOT_DOLLAR_PER_UNIT)
    return _r4(lot_size), actual_risk


def calc_trade_parameters(inp: CapitalInput) -> CapitalOutput:
    entry = inp.entry_price
    direction = inp.direction
    atr = inp.atr if isfinite(inp.atr) and inp.atr > 0 else max(entry * 0.001, 0.01)

    sl = _calc_smart_sl(direction, entry, atr, inp)
    sl_dist = _r2(abs(entry - sl))
    sl_pips = _r2(sl_dist * 100)

    tp, tp_dist = _calc_structural_tp(direction, entry, sl_dist, atr, inp)
    if tp is None:
        tp_dist = sl_dist * FIXED_TP_RR
        tp = _r2(entry + tp_dist if direction == "BUY" else entry - tp_dist)
    rr = _r2(tp_dist / sl_dist) if sl_dist > 0 else 0.0

    lot, risk = _calc_lot_size(sl_dist, inp.account_balance, inp.risk_percent)
    be_dist = sl_dist
    be_at = _r2(entry + be_dist if direction == "BUY" else entry - be_dist)

    return CapitalOutput(
        entry_price=_r2(entry),
        stop_loss=sl,
        take_profit=tp,
        risk_reward_ratio=rr,
        trailing_stop_distance=_r2(sl_dist * 0.5),
        trailing_activation_at=be_at,
        break_even_at=be_at,
        break_even_sl=_r2(entry),
        lot_size=lot,
        risk_amount=risk,
        sl_distance_usd=sl_dist,
        sl_distance_pips=sl_pips,
    )
