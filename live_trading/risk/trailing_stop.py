"""
Staircase Trailing Stop — GoldScalperPro v4
============================================

Professional, risk-multiple (R) based trailing stop-loss engine.

The problem it solves
----------------------
The robot placed a Stop Loss at trade entry and never moved it again — even
as price ran deep into profit in the trade's favour. A trade that reached
+3R of open profit could still be stopped out at its original, now far too
generous (to the market) and far too risky (to the account) level if price
reversed.

The fix
-------
Once a trade has moved a configurable number of "R" (R = the position's own
original risk distance, i.e. |entry − initial stop loss|, fixed at the
moment the trade was opened) into profit, the stop loss is ratcheted
forward in discrete steps ("stairs") — never backwards — locking in
progressively more of the open profit as the trade continues to run:

    R progress                 Stop loss moves to
    ──────────────────────     ────────────────────────────────────────
    < ACTIVATION_R              unchanged (original entry SL)
    ACTIVATION_R                 entry + LOCK_BUFFER_R  (small *real*
                                  profit locked — not exact break-even,
                                  so normal spread/slippage can't turn a
                                  winner into a loser)
    ACTIVATION_R + STEP_R         entry + (STEP_R + LOCK_BUFFER_R)
    ACTIVATION_R + 2·STEP_R       entry + (2·STEP_R + LOCK_BUFFER_R)
    ...                            ...

A live-ATR safety floor additionally guarantees the stop is never placed
closer to the current price than a small multiple of the current ATR. This
keeps ordinary M5 candle noise from prematurely stopping out a trade whose
staircase step happens to land very close to price during a fast move.

This module is pure and stateless: given the position's own original entry
price, its original (never-moved) risk distance, the current market price,
and the current ATR, it returns either `None` (no change yet) or the new
SL price to apply. It never talks to MT5 directly — the caller
(live_trading/trading/live_loop.py) applies the result via
mt5.executor.modify_position() and is responsible for tracking the
position's original entry/SL baseline so it survives restarts.
"""
from dataclasses import dataclass
from math import floor
from typing import Any, Optional, Sequence


@dataclass
class TrailingConfig:
    enabled:         bool  = True
    activation_r:    float = 1.0   # R-multiple that first engages trailing
    step_r:          float = 0.5   # R-multiple per staircase step
    lock_buffer_r:   float = 0.08  # extra R locked in at every step
    atr_gap_mult:    float = 0.8   # never place SL closer than this × ATR to price
    min_step_price:  float = 0.05  # minimum price-unit improvement to bother modifying
    peak_atr_gap_mult:       float = 1.10  # normal distance behind the best favorable price
    peak_r_gap_mult:         float = 0.60  # minimum distance behind the best favorable price
    structure_buffer_atr:    float = 0.18  # buffer beyond a confirmed swing
    reversal_confirmation_bars: int = 2    # closed candles required to confirm a pullback
    reversal_tighten_atr_mult: float = 0.75  # tighten after reversal confirmation
    swing_lookback:          int = 2        # candles on each side of a swing pivot


def _r2(n: float) -> float:
    return round(n, 2)


def compute_staircase_sl(
    direction:     str,             # "BUY" | "SELL"
    entry:         float,           # original entry price
    risk_distance: float,           # original |entry − initial SL| (defines 1R)
    current_price: float,           # current bid (for BUY) / ask (for SELL)
    atr:           float,           # current ATR in price units (0 disables the floor)
    cfg:           TrailingConfig,
) -> Optional[float]:
    """Return the staircase's candidate SL price, or None if not yet triggered.

    The caller must still compare the result against the position's live SL
    and only apply it when it is an improvement of at least
    ``cfg.min_step_price`` — this function does not know the current SL, so
    it cannot enforce "never move backwards" on its own.
    """
    if not cfg.enabled or risk_distance <= 0 or cfg.step_r <= 0:
        return None

    is_buy = direction.upper() == "BUY"
    profit_distance = (current_price - entry) if is_buy else (entry - current_price)
    if profit_distance <= 0:
        return None  # trade is flat or underwater — nothing to protect yet

    r_multiple = profit_distance / risk_distance
    if r_multiple < cfg.activation_r:
        return None

    steps    = floor((r_multiple - cfg.activation_r) / cfg.step_r)
    locked_r = steps * cfg.step_r + cfg.lock_buffer_r
    locked_dist = locked_r * risk_distance

    candidate = entry + locked_dist if is_buy else entry - locked_dist

    # Live-ATR safety floor: never let the staircase place the stop closer
    # to the current price than atr_gap_mult × ATR, so a step that happens
    # to land right under live price doesn't get shaken out by normal noise.
    if atr and atr > 0:
        gap = atr * cfg.atr_gap_mult
        if is_buy:
            candidate = min(candidate, current_price - gap)
        else:
            candidate = max(candidate, current_price + gap)

    return _r2(candidate)


def _candle_value(candle: Any, name: str) -> Optional[float]:
    """Read OHLC values from either an OHLCV object or a mapping.

    The live connector returns dataclass-like OHLCV objects, while the small
    unit tests and backtest utilities commonly use dictionaries.  Keeping the
    adapter here makes the trailing algorithm independent of either format.
    """
    try:
        value = candle.get(name) if isinstance(candle, dict) else getattr(candle, name)
        return float(value)
    except (AttributeError, TypeError, ValueError):
        return None


def _reversal_confirmation_count(
    candles: Sequence[Any],
    direction: str,
    required: int,
) -> int:
    """Count consecutive closed candles that oppose the open direction."""
    if required <= 0:
        return 0

    opposing = 0
    for candle in reversed(candles):
        open_price = _candle_value(candle, "open")
        close_price = _candle_value(candle, "close")
        if open_price is None or close_price is None or open_price == close_price:
            break
        is_opposing = close_price < open_price if direction.upper() == "BUY" else close_price > open_price
        if not is_opposing:
            break
        opposing += 1
        if opposing >= required:
            break
    return opposing


def _latest_confirmed_swing(
    candles: Sequence[Any],
    direction: str,
    lookback: int,
) -> Optional[float]:
    """Return the latest confirmed swing low/high, excluding the live candle.

    A pivot is only considered confirmed after ``lookback`` candles have
    closed on both sides of it.  This deliberately avoids moving a stop from
    an unconfirmed wick that can disappear before the bar closes.
    """
    if lookback < 1 or len(candles) < (lookback * 2 + 2):
        return None

    # The last item may still be the currently forming candle on some broker
    # bridges, so never use it as one side of a pivot.
    last_confirmable = len(candles) - 2
    is_buy = direction.upper() == "BUY"
    for index in range(last_confirmable - lookback, lookback - 1, -1):
        center = _candle_value(candles[index], "low" if is_buy else "high")
        if center is None:
            continue

        values = [
            _candle_value(candles[j], "low" if is_buy else "high")
            for j in range(index - lookback, index + lookback + 1)
        ]
        if any(value is None for value in values):
            continue
        if is_buy and center == min(values):
            return center
        if not is_buy and center == max(values):
            return center
    return None


def compute_smart_trailing_sl(
    direction: str,
    entry: float,
    risk_distance: float,
    current_price: float,
    peak_price: float,
    current_sl: float,
    atr: float,
    candles: Sequence[Any],
    cfg: TrailingConfig,
) -> Optional[float]:
    """Compute a peak-and-structure-aware trailing stop candidate.

    The stop has three cooperating protections:

    * a favorable-price trail that follows the highest high/lowest low reached,
    * a confirmed swing level so candle structure, not only ticks, matters,
    * a faster tightening pass after consecutive opposing closed candles.

    The caller still applies the candidate only when it improves the live SL.
    This function never loosens a stop and never proposes a stop beyond the
    broker-side close price.
    """
    if (
        not cfg.enabled
        or risk_distance <= 0
        or cfg.activation_r <= 0
        or cfg.peak_atr_gap_mult <= 0
        or current_price <= 0
    ):
        return None

    is_buy = direction.upper() == "BUY"
    profit_distance = (current_price - entry) if is_buy else (entry - current_price)
    if profit_distance <= 0 or profit_distance / risk_distance < cfg.activation_r:
        return None

    favorable_peak = max(peak_price, current_price) if is_buy else min(peak_price, current_price)
    live_atr = max(float(atr or 0.0), 0.0)
    peak_gap = max(
        risk_distance * cfg.peak_r_gap_mult,
        live_atr * cfg.peak_atr_gap_mult,
        cfg.min_step_price,
    )
    candidate = favorable_peak - peak_gap if is_buy else favorable_peak + peak_gap

    # A confirmed swing is a structural floor/ceiling.  It only tightens the
    # stop if that structure is already in profit; otherwise it would recreate
    # the original problem and leave the stop far below the trade.
    swing = _latest_confirmed_swing(candles, direction, cfg.swing_lookback)
    if swing is not None and live_atr > 0:
        structure_candidate = (
            swing - live_atr * cfg.structure_buffer_atr
            if is_buy
            else swing + live_atr * cfg.structure_buffer_atr
        )
        if (is_buy and structure_candidate > entry) or (not is_buy and structure_candidate < entry):
            candidate = max(candidate, structure_candidate) if is_buy else min(candidate, structure_candidate)

    confirmed_bars = _reversal_confirmation_count(
        candles, direction, cfg.reversal_confirmation_bars,
    )
    if confirmed_bars >= cfg.reversal_confirmation_bars and live_atr > 0:
        last_close = _candle_value(candles[-1], "close") if candles else None
        if last_close is not None:
            reversal_candidate = (
                last_close - live_atr * cfg.reversal_tighten_atr_mult
                if is_buy
                else last_close + live_atr * cfg.reversal_tighten_atr_mult
            )
            candidate = max(candidate, reversal_candidate) if is_buy else min(candidate, reversal_candidate)

    # Always lock a small real profit after activation, while respecting the
    # close-side price and a volatility gap to prevent instant re-entry noise.
    lock_distance = max(cfg.lock_buffer_r * risk_distance, cfg.min_step_price)
    profit_floor = entry + lock_distance if is_buy else entry - lock_distance
    candidate = max(candidate, profit_floor) if is_buy else min(candidate, profit_floor)

    if live_atr > 0:
        close_gap = live_atr * max(cfg.atr_gap_mult, 0.1)
        safe_close_side = current_price - close_gap if is_buy else current_price + close_gap
        candidate = min(candidate, safe_close_side) if is_buy else max(candidate, safe_close_side)

    # In very volatile conditions the ATR safety cap can pull the candidate
    # back through the small profit lock.  Do not move the stop to a fragile
    # break-even-or-worse area; wait until price has created enough room.
    if (is_buy and candidate < profit_floor) or (not is_buy and candidate > profit_floor):
        return None

    # A live SL is an additional safety floor.  The normal caller checks this
    # via should_apply(), but retaining it here makes the pure helper safe when
    # used by a backtest or a second execution path.
    if current_sl > 0:
        candidate = max(candidate, current_sl) if is_buy else min(candidate, current_sl)

    return _r2(candidate)


def r_multiple_of(direction: str, entry: float, risk_distance: float, current_price: float) -> float:
    """Convenience helper for logging/telemetry — how many R's a trade is up."""
    if risk_distance <= 0:
        return 0.0
    is_buy = direction.upper() == "BUY"
    profit_distance = (current_price - entry) if is_buy else (entry - current_price)
    return round(profit_distance / risk_distance, 3)


def should_apply(direction: str, current_sl: float, candidate_sl: Optional[float], min_step_price: float) -> bool:
    """True only if candidate_sl is a real, meaningful improvement over current_sl.

    This is what guarantees the stop only ever moves in the trade's favour —
    a candidate that is equal to, or worse than, the live SL is rejected.
    """
    if candidate_sl is None:
        return False
    if direction.upper() == "BUY":
        return (candidate_sl - current_sl) >= min_step_price
    return (current_sl - candidate_sl) >= min_step_price
