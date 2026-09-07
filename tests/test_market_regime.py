"""Regression tests for directional regime gating."""

from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.market_regime import (
    _detect_pullback,
    detect_market_regime,
)
from live_trading.signals.trend_engine import TrendResult
from live_trading.signals.wyckoff_engine import WyckoffResult


def _flat_candles(count: int = 80) -> list[OHLCV]:
    return [
        OHLCV(
            time=f"2026-09-08T12:{i // 60:02d}:{i % 60:02d}+00:00",
            open=100.0,
            high=100.2,
            low=99.8,
            close=100.0,
            volume=100.0,
        )
        for i in range(count)
    ]


def _neutral_trend() -> TrendResult:
    return TrendResult(
        ema50=100.0,
        ema100=100.0,
        ema200=100.0,
        trend="NEUTRAL",
        strength="WEAK",
    )


def test_unconfirmed_distribution_phase_cannot_block_long_entries():
    wyckoff = WyckoffResult(
        phase="DISTRIBUTION",
        spring=False,
        upthrust=False,
        volume_confirmed=False,
        wyckoff_signal="NEUTRAL",
        wyckoff_score=0.0,
    )

    result = detect_market_regime(_flat_candles(), _neutral_trend(), wyckoff)

    assert result.regime in {"RANGE", "LOW_VOLATILITY"}
    assert result.rules.allow_long is True


def test_confirmed_distribution_keeps_short_only_context():
    wyckoff = WyckoffResult(
        phase="DISTRIBUTION",
        spring=False,
        upthrust=True,
        volume_confirmed=True,
        wyckoff_signal="SELL",
        wyckoff_score=1.0,
    )

    result = detect_market_regime(_flat_candles(), _neutral_trend(), wyckoff)

    assert result.regime == "DISTRIBUTION"
    assert result.rules.allow_long is False
    assert result.rules.allow_short is True


def test_pullback_requires_consensus_not_one_countertrend_candle():
    candles = _flat_candles(12)
    closes = [100.0, 99.95, 100.0, 100.05, 100.10, 100.15]
    for i, close in enumerate(closes, start=len(candles) - len(closes)):
        candles[i] = OHLCV(
            time=candles[i].time,
            open=close,
            high=close + 0.1,
            low=close - 0.1,
            close=close,
            volume=100.0,
        )

    assert _detect_pullback(candles, TrendResult(
        ema50=100.0,
        ema100=99.0,
        ema200=98.0,
        trend="BULLISH",
        strength="STRONG",
    )) is None