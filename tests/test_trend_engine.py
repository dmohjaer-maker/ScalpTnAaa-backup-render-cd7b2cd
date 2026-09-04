"""Regression test for using a 200-candle live analysis window."""

from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.trend_engine import analyze_trend


def test_200_candles_produces_a_real_trend_result():
    candles = [
        OHLCV(
            time=f"2026-09-04T12:{i // 60:02d}:{i % 60:02d}+00:00",
            open=100.0 + i * 0.02,
            high=100.1 + i * 0.02,
            low=99.9 + i * 0.02,
            close=100.0 + i * 0.02,
            volume=100.0,
        )
        for i in range(200)
    ]
    result = analyze_trend(candles)
    assert result.trend == "BULLISH"
    assert result.ema200 > 0