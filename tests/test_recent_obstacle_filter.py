"""Tests for provisional support/resistance zones near the newest candles."""

from types import SimpleNamespace

from live_trading.signals.decision_engine import _entry_obstacle_block_reason
from live_trading.signals.gold_engine import OHLCV


def candle(index: int, low: float, high: float, close: float) -> OHLCV:
    return OHLCV(
        time=f"2026-08-18T00:{index:02d}:00Z",
        open=close,
        high=high,
        low=low,
        close=close,
        volume=100,
    )


def empty_smc() -> SimpleNamespace:
    return SimpleNamespace(
        equal_lows=[],
        equal_highs=[],
        bos_signals=[],
        choch_signals=[],
        order_blocks=[],
    )


def test_sell_is_blocked_by_repeated_unconfirmed_floor():
    candles = [
        candle(0, 100.00, 101.00, 100.60),
        candle(1, 100.10, 101.10, 100.70),
        candle(2, 100.02, 100.90, 100.45),
        candle(3, 100.08, 100.85, 100.40),
        candle(4, 100.04, 100.80, 100.30),
        candle(5, 100.06, 100.75, 100.25),
        candle(6, 100.05, 100.70, 100.20),
        candle(7, 100.03, 100.65, 100.15),
    ]

    reason = _entry_obstacle_block_reason(
        candles, empty_smc(), "SELL", entry=100.20, atr=1.0
    )

    assert reason is not None
    assert "support" in reason


def test_sell_is_not_blocked_by_single_trend_extending_low():
    candles = [
        candle(0, 105.0, 106.0, 105.5),
        candle(1, 104.0, 105.0, 104.5),
        candle(2, 103.0, 104.0, 103.5),
        candle(3, 102.0, 103.0, 102.5),
        candle(4, 101.0, 102.0, 101.5),
        candle(5, 100.0, 101.0, 100.5),
        candle(6, 99.0, 100.0, 99.5),
        candle(7, 98.0, 99.0, 98.5),
    ]

    reason = _entry_obstacle_block_reason(
        candles, empty_smc(), "SELL", entry=98.5, atr=1.0
    )

    assert reason is None


def test_exact_support_price_is_blocked():
    candles = [
        candle(0, 100.00, 101.00, 100.40),
        candle(1, 100.04, 100.90, 100.30),
        candle(2, 100.01, 100.80, 100.20),
        candle(3, 100.03, 100.70, 100.10),
        candle(4, 100.02, 100.60, 100.05),
        candle(5, 100.01, 100.55, 100.02),
        candle(6, 100.00, 100.50, 100.01),
        candle(7, 100.02, 100.45, 100.03),
    ]

    reason = _entry_obstacle_block_reason(
        candles, empty_smc(), "SELL", entry=100.00, atr=1.0
    )

    assert reason is not None


def test_buy_is_blocked_by_repeated_unconfirmed_ceiling():
    candles = [
        candle(0, 100.0, 101.00, 100.40),
        candle(1, 99.9, 100.90, 100.30),
        candle(2, 100.1, 100.98, 100.45),
        candle(3, 100.2, 100.92, 100.50),
        candle(4, 100.3, 100.96, 100.55),
        candle(5, 100.4, 100.94, 100.60),
        candle(6, 100.5, 100.95, 100.65),
        candle(7, 100.6, 100.93, 100.70),
    ]

    reason = _entry_obstacle_block_reason(
        candles, empty_smc(), "BUY", entry=100.70, atr=1.0
    )

    assert reason is not None
    assert "resistance" in reason