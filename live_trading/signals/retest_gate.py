"""
Break-and-retest entry gate.

The decision engine can identify a valid directional structure break, but a
fresh break is vulnerable to a fast false-breakout.  This module keeps the
entry gate pure and candle-close based: a trade is allowed only on a later
retest candle that rejects the broken structure level.
"""

from dataclasses import dataclass
from typing import List, Literal

from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.smc_engine import SmcResult


Direction = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class RetestResult:
    allowed: bool
    direction: str
    reason: str
    level: float | None = None
    breakout_bar: int | None = None
    bars_since_breakout: int | None = None
    atr: float = 0.0
    zone: float = 0.0
    close_buffer: float = 0.0
    retest_touched: bool = False
    rejection_confirmed: bool = False

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "direction": self.direction,
            "reason": self.reason,
            "level": self.level,
            "breakout_bar": self.breakout_bar,
            "bars_since_breakout": self.bars_since_breakout,
            "atr": round(self.atr, 4),
            "zone": round(self.zone, 4),
            "close_buffer": round(self.close_buffer, 4),
            "retest_touched": self.retest_touched,
            "rejection_confirmed": self.rejection_confirmed,
        }


def _atr(candles: List[OHLCV], period: int = 14) -> float:
    """Return a bounded simple ATR using only closed candles."""
    if len(candles) < 2:
        return 0.0
    trs = []
    for current, previous in zip(candles[1:], candles[:-1]):
        trs.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    window = trs[-period:]
    return sum(window) / len(window) if window else 0.0


def _body_ratio(candle: OHLCV) -> float:
    candle_range = candle.high - candle.low
    return abs(candle.close - candle.open) / candle_range if candle_range > 0 else 0.0


def _latest_directional_break(smc: SmcResult, direction: Direction):
    events = [
        event
        for event in [*smc.bos_signals, *smc.choch_signals]
        if event.type == direction
    ]
    return max(events, key=lambda event: event.bar_index) if events else None


def evaluate_break_retest(
    candles: List[OHLCV],
    smc: SmcResult,
    direction: str,
    *,
    max_bars: int = 3,
    zone_atr_mult: float = 0.20,
    close_buffer_atr: float = 0.05,
    min_body_atr: float = 0.15,
) -> RetestResult:
    """
    Evaluate whether the latest closed candle confirms a break-and-retest.

    For SELL, the directional SMC event price is treated as broken support:
    price must first close below it, then retest it from underneath and close
    bearish below the level.  BUY is the exact mirror image.
    """
    if direction not in {"BUY", "SELL"}:
        return RetestResult(False, direction, "Retest gate requires a directional signal")
    if max_bars < 1:
        return RetestResult(False, direction, "Retest gate max_bars must be at least 1")
    if len(candles) < 3:
        return RetestResult(False, direction, "Waiting for enough closed candles")

    event = _latest_directional_break(smc, direction)  # type: ignore[arg-type]
    if event is None:
        return RetestResult(
            False,
            direction,
            f"No confirmed {direction} BOS/CHoCH available for retest",
        )
    if event.bar_index < 0 or event.bar_index >= len(candles):
        return RetestResult(
            False,
            direction,
            "Structure event is outside the current candle window",
        )

    bars_since = len(candles) - 1 - event.bar_index
    atr = _atr(candles)
    if atr <= 0:
        return RetestResult(
            False,
            direction,
            "Cannot evaluate retest without a positive ATR",
            level=event.price,
            breakout_bar=event.bar_index,
            bars_since_breakout=bars_since,
        )

    zone = atr * max(zone_atr_mult, 0.0)
    close_buffer = atr * max(close_buffer_atr, 0.0)
    base = dict(
        direction=direction,
        level=event.price,
        breakout_bar=event.bar_index,
        bars_since_breakout=bars_since,
        atr=atr,
        zone=zone,
        close_buffer=close_buffer,
    )

    if bars_since < 1:
        return RetestResult(
            False,
            reason="Breakout confirmed; waiting for a later retest candle",
            **base,
        )
    if bars_since > max_bars:
        return RetestResult(
            False,
            reason="Retest setup expired before confirmation",
            **base,
        )

    level = event.price
    after_break = candles[event.bar_index + 1 :]
    prior_bars = after_break[:-1]
    current = candles[-1]

    # Any close back through the broken level invalidates the setup.  This
    # prevents a later candle from reviving a false breakout.
    if direction == "SELL":
        invalidated = any(c.close > level + close_buffer for c in prior_bars)
    else:
        invalidated = any(c.close < level - close_buffer for c in prior_bars)
    if invalidated:
        return RetestResult(
            False,
            reason="Setup invalidated by a prior close back through the broken level",
            **base,
        )

    if direction == "SELL":
        touched = current.high >= level - zone and current.low <= level + zone
        rejection = (
            touched
            and current.close < level - close_buffer
            and current.close < current.open
            and abs(current.close - current.open) >= atr * max(min_body_atr, 0.0)
            and _body_ratio(current) >= 0.35
        )
        invalid_current = current.close > level + close_buffer
    else:
        touched = current.low <= level + zone and current.high >= level - zone
        rejection = (
            touched
            and current.close > level + close_buffer
            and current.close > current.open
            and abs(current.close - current.open) >= atr * max(min_body_atr, 0.0)
            and _body_ratio(current) >= 0.35
        )
        invalid_current = current.close < level - close_buffer

    if invalid_current:
        return RetestResult(
            False,
            reason="Retest failed: candle closed back through the broken level",
            retest_touched=touched,
            **base,
        )
    if not touched:
        return RetestResult(
            False,
            reason="Waiting for price to retest the broken level",
            **base,
        )
    if not rejection:
        return RetestResult(
            False,
            reason="Retest touched the level but rejection candle was not confirmed",
            retest_touched=True,
            **base,
        )

    return RetestResult(
        True,
        reason="Break-and-retest rejection confirmed",
        retest_touched=True,
        rejection_confirmed=True,
        **base,
    )