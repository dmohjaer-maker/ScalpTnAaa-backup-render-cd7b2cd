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
from typing import Optional


@dataclass
class TrailingConfig:
    enabled:         bool  = True
    activation_r:    float = 1.0   # R-multiple that first engages trailing
    step_r:          float = 0.5   # retained for compatibility and telemetry
    lock_buffer_r:   float = 0.15  # minimum real profit locked after activation
    atr_gap_mult:    float = 0.8   # adaptive gap behind the favorable extreme
    min_step_price:  float = 0.05  # minimum price-unit improvement to bother modifying
    min_gap_price:   float = 0.05  # absolute floor for symbols with tiny ATR


def _r2(n: float) -> float:
    return round(n, 2)


def compute_staircase_sl(
    direction:     str,             # "BUY" | "SELL"
    entry:         float,           # original entry price
    risk_distance: float,           # original |entry − initial SL| (defines 1R)
    current_price: float,           # current bid (for BUY) / ask (for SELL)
    atr:           float,           # current ATR in price units (0 disables the floor)
    cfg:           TrailingConfig,
    favorable_extreme: Optional[float] = None,
) -> Optional[float]:
    """Return an adaptive candidate SL price, or None if not yet triggered.

    The caller must still compare the result against the position's live SL
    and only apply it when it is an improvement of at least
    ``cfg.min_step_price`` — this function does not know the current SL, so
    it cannot enforce "never move backwards" on its own.

    ``favorable_extreme`` is the high-water mark for BUY positions or the
    low-water mark for SELL positions. It is supplied by the live loop and
    persisted through successful trail events, so a brief price retracement
    cannot make the stop loosen.
    """
    if not cfg.enabled or risk_distance <= 0 or cfg.step_r <= 0:
        return None

    is_buy = direction.upper() == "BUY"
    profit_distance = (current_price - entry) if is_buy else (entry - current_price)
    extreme = favorable_extreme if favorable_extreme is not None else current_price
    extreme_profit_distance = (
        (extreme - entry) if is_buy else (entry - extreme)
    )
    if profit_distance <= 0 and extreme_profit_distance <= 0:
        return None  # trade is flat or underwater — nothing to protect yet

    # Never trust a malformed/stale mark that is on the wrong side of entry.
    if is_buy:
        extreme = max(entry, extreme)
    else:
        extreme = min(entry, extreme)

    # Use the better of the current quote and the stored extreme. This keeps
    # the pure function safe when a caller has not restored a high-water mark
    # yet, while still never allowing the stop to loosen.
    current_profit_distance = (
        (current_price - entry) if is_buy else (entry - current_price)
    )
    extreme_profit_distance = (
        (extreme - entry) if is_buy else (entry - extreme)
    )
    r_multiple = max(0.0, current_profit_distance, extreme_profit_distance) / risk_distance
    if r_multiple < cfg.activation_r:
        return None

    # Keep a meaningful amount of profit at activation and thereafter. The
    # risk-relative floor prevents a very small ATR from putting the stop
    # exactly at the entry price, while the ATR component adapts to volatility.
    lock_floor = cfg.lock_buffer_r * risk_distance
    adaptive_gap = max(
        cfg.min_gap_price,
        risk_distance * cfg.lock_buffer_r,
        (atr * cfg.atr_gap_mult) if atr and atr > 0 else 0.0,
    )
    if is_buy:
        candidate = max(entry + lock_floor, extreme - adaptive_gap)
    else:
        candidate = min(entry - lock_floor, extreme + adaptive_gap)

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
    # Decimal price steps such as 0.05 are not represented exactly by binary
    # floats; tolerate only the tiny representation error, never a meaningful
    # price difference.
    threshold = max(0.0, min_step_price - 1e-9)
    if direction.upper() == "BUY":
        return (candidate_sl - current_sl) >= threshold
    return (current_sl - candidate_sl) >= threshold
