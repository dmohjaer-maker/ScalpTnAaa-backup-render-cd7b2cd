"""Tests for the closed-candle CHoCH follow-through guard."""

from live_trading.signals.decision_engine import _false_reversal_reason
from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.smc_engine import SmcChoch, SmcResult


def _candle(close: float, open_: float | None = None) -> OHLCV:
    open_price = close if open_ is None else open_
    return OHLCV(
        time="2026-09-04T12:00:00+00:00",
        open=open_price,
        high=max(open_price, close) + 0.20,
        low=min(open_price, close) - 0.20,
        close=close,
        volume=100.0,
    )


def _smc(event: SmcChoch) -> SmcResult:
    return SmcResult(
        timeframe="M5",
        timestamp="2026-09-04T12:00:00+00:00",
        current_price=event.price,
        trend="NEUTRAL",
        bos_signals=[],
        choch_signals=[event],
        order_blocks=[],
        fair_value_gaps=[],
        liquidity_sweeps=[],
        equal_highs=[],
        equal_lows=[],
        mitigation_blocks=[],
        smc_signal=event.type,
        smc_score=0.8,
    )


def test_reversal_waits_for_one_closed_confirmation_candle():
    event = SmcChoch("BUY", 100.0, 2, "2026-09-04T12:00:00+00:00")
    reason = _false_reversal_reason(
        [_candle(99.4), _candle(99.6), _candle(100.3)],
        _smc(event),
        "BUY",
    )
    assert reason is not None
    assert "pending confirmation" in reason


def test_reversal_blocks_when_confirmation_closes_back_below_level():
    event = SmcChoch("BUY", 100.0, 1, "2026-09-04T12:00:00+00:00")
    reason = _false_reversal_reason(
        [_candle(99.4), _candle(100.3, 99.7), _candle(99.8, 100.2)],
        _smc(event),
        "BUY",
    )
    assert reason is not None
    assert "False reversal" in reason


def test_reversal_allows_directional_follow_through():
    event = SmcChoch("SELL", 100.0, 1, "2026-09-04T12:00:00+00:00")
    reason = _false_reversal_reason(
        [_candle(100.6), _candle(99.7, 100.3), _candle(99.4, 99.8)],
        _smc(event),
        "SELL",
    )
    assert reason is None
