"""Closed-candle break-and-retest entry gate tests."""

from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.retest_gate import evaluate_break_retest
from live_trading.signals.smc_engine import (
    SmcBos,
    SmcChoch,
    SmcResult,
)


def candle(i: int, open_: float, high: float, low: float, close: float) -> OHLCV:
    return OHLCV(
        time=f"2026-08-18T00:{i:02d}:00Z",
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=100,
    )


def smc(direction: str, level: float, bar_index: int) -> SmcResult:
    return SmcResult(
        timeframe="M5",
        timestamp="2026-08-18T00:00:00Z",
        current_price=level,
        trend="BEARISH" if direction == "SELL" else "BULLISH",
        bos_signals=[
            SmcBos(direction, level, bar_index, "2026-08-18T00:01:00Z")
        ],
        choch_signals=[],
        order_blocks=[],
        fair_value_gaps=[],
        liquidity_sweeps=[],
        equal_highs=[],
        equal_lows=[],
        mitigation_blocks=[],
        smc_signal=direction,
        smc_score=1.0,
    )


def base_candles() -> list[OHLCV]:
    return [
        candle(0, 100.0, 100.8, 99.2, 100.0),
        candle(1, 100.0, 100.7, 99.3, 100.1),
        candle(2, 100.1, 100.8, 99.4, 100.0),
        candle(3, 100.0, 100.6, 99.2, 100.1),
    ]


def test_sell_waits_for_retest_after_break():
    candles = base_candles() + [
        candle(4, 100.0, 100.2, 98.7, 98.8),
    ]
    result = evaluate_break_retest(candles, smc("SELL", 99.0, 4), "SELL")
    assert not result.allowed
    assert "waiting for a later retest" in result.reason


def test_sell_allows_retest_rejection():
    candles = base_candles() + [
        candle(4, 100.0, 100.2, 98.7, 98.8),
        candle(5, 98.8, 99.15, 98.0, 98.25),
    ]
    result = evaluate_break_retest(candles, smc("SELL", 99.0, 4), "SELL")
    assert result.allowed
    assert result.retest_touched
    assert result.rejection_confirmed


def test_sell_blocks_close_back_above_broken_support():
    candles = base_candles() + [
        candle(4, 100.0, 100.2, 98.7, 98.8),
        candle(5, 98.8, 99.4, 98.6, 99.2),
    ]
    result = evaluate_break_retest(candles, smc("SELL", 99.0, 4), "SELL")
    assert not result.allowed
    assert "closed back through" in result.reason


def test_buy_allows_mirrored_retest_rejection():
    candles = [
        candle(0, 100.0, 100.8, 99.2, 100.0),
        candle(1, 100.0, 100.7, 99.3, 100.1),
        candle(2, 100.1, 100.8, 99.4, 100.0),
        candle(3, 100.0, 100.6, 99.2, 100.1),
        candle(4, 98.9, 100.4, 98.8, 100.3),
        candle(5, 100.3, 101.0, 100.0, 100.75),
    ]
    result = evaluate_break_retest(candles, smc("BUY", 100.0, 4), "BUY")
    assert result.allowed
    assert result.rejection_confirmed


def test_retest_expires_after_max_bars():
    candles = base_candles() + [
        candle(4, 100.0, 100.2, 98.7, 98.8),
        candle(5, 98.8, 98.9, 98.2, 98.5),
        candle(6, 98.5, 98.8, 98.1, 98.4),
        candle(7, 98.4, 98.7, 98.0, 98.3),
    ]
    result = evaluate_break_retest(candles, smc("SELL", 99.0, 4), "SELL", max_bars=2)
    assert not result.allowed
    assert "expired" in result.reason