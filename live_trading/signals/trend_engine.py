"""
Trend Engine — EMA 50/100/200 alignment
Ported from trendEngine.ts
"""
from dataclasses import dataclass
from typing import List, Literal
from live_trading.signals.gold_engine import OHLCV, calc_ema


@dataclass
class TrendResult:
    ema50: float
    ema100: float
    ema200: float
    trend: Literal["BULLISH", "BEARISH", "NEUTRAL"]
    strength: Literal["STRONG", "MODERATE", "WEAK"]


def analyze_trend(candles: List[OHLCV]) -> TrendResult:
    closes = [c.close for c in candles]
    n = len(closes)

    if n < 210:
        last = closes[-1] if closes else 0.0
        return TrendResult(ema50=last, ema100=last, ema200=last,
                           trend="NEUTRAL", strength="WEAK")

    ema50  = calc_ema(closes, 50)
    ema100 = calc_ema(closes, 100)
    ema200 = calc_ema(closes, 200)
    price  = closes[-1]

    bull = price > ema50 and ema50 > ema100
    bear = price < ema50 and ema50 < ema100

    if bull:
        trend = "BULLISH"
        strength = "STRONG" if ema100 > ema200 else "MODERATE"
    elif bear:
        trend = "BEARISH"
        strength = "STRONG" if ema100 < ema200 else "MODERATE"
    else:
        trend = "NEUTRAL"
        strength = "WEAK"

    return TrendResult(ema50=ema50, ema100=ema100, ema200=ema200,
                       trend=trend, strength=strength)
